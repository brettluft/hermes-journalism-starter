#!/usr/bin/env python3
"""Local draft publication policy, explicit key setup, and signed links."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import hmac
import html
import importlib.util
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from typing import Any, Callable, Mapping, NoReturn
from urllib.parse import urlencode, urlsplit

from draft_store import (DraftError, DraftStore, PRODUCTION_DRAFT_ROOT,
                         PublicationCommitUncertain, SaveCommitUncertain,
                         validate_draft_id)
from spacefast_client import SpacefastClient, SpacefastError


PRODUCTION_CONFIG_PATH = Path("/opt/data/newsroom/newsroom.json")
PRODUCTION_KEY_PATH = Path("/opt/data/newsroom/draft-library/hmac.key")
KEY_BYTES = 32
MAX_TTL_SECONDS = 86400
DEFAULT_TTL_SECONDS = 3600
HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _load_newsroom_validator() -> Any:
    """Load the bundled authoritative validator from its fixed hyphenated skill path."""
    validator_path = (
        Path(__file__).resolve().parents[2]
        / "newsroom-setup/scripts/newsroom_config.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_bundled_newsroom_config_for_draft_publishing", validator_path
    )
    if spec is None or spec.loader is None:
        raise ImportError("bundled newsroom validator unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublishError(Exception):
    """An expected failure containing only a stable public code."""


class JsonArgumentParser(argparse.ArgumentParser):
    def print_help(self, file: Any = None) -> None:
        """Keep argparse's help exit while preserving the one-JSON CLI contract."""
        target = sys.stdout if file is None else file
        commands: list[str] = []
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                commands.extend(action.choices)
        payload: dict[str, Any] = {
            "commands": sorted(commands),
            "ok": True,
            "usage": self.format_usage().strip(),
        }
        target.write(json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ) + "\n")

    def error(self, message: str) -> NoReturn:
        del message
        raise PublishError("invalid_arguments")


def _safe_directory(path: Path, *, missing_ok: bool = False) -> int | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        if missing_ok:
            return None
        raise PublishError("library_not_initialized")
    except OSError as error:
        raise PublishError("unsafe_key_state") from error
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        os.close(fd)
        raise PublishError("unsafe_key_state")
    return fd


def _ensure_key_parent(path: Path) -> None:
    """Create the key directory without following any directory symlink."""
    absolute = path.absolute()
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current,
                )
            except FileNotFoundError:
                try:
                    try:
                        os.mkdir(component, 0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                    child = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=current,
                    )
                except OSError as error:
                    raise PublishError("init_failed") from error
            except OSError as error:
                raise PublishError("unsafe_key_state") from error
            os.close(current)
            current = child
    finally:
        os.close(current)


def _read_key(path: Path, *, missing_ok: bool = False) -> bytes | None:
    parent_fd = _safe_directory(path.parent, missing_ok=missing_ok)
    if parent_fd is None:
        return None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(path.name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise PublishError("library_not_initialized")
        except OSError as error:
            raise PublishError("unsafe_key_state") from error
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise PublishError("unsafe_key_state")
            chunks: list[bytes] = []
            remaining = KEY_BYTES + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            key = b"".join(chunks)
            if len(key) != KEY_BYTES:
                raise PublishError("unsafe_key_state")
            return key
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def initialize_key(path: Path = PRODUCTION_KEY_PATH,
                   random_bytes: Callable[[int], bytes] = secrets.token_bytes) -> bool:
    """Create a raw 256-bit key atomically; never replace an existing key."""
    path = Path(path)
    _ensure_key_parent(path.parent)
    parent_fd = _safe_directory(path.parent)
    assert parent_fd is not None
    temporary = f".key-{os.getpid()}-{secrets.token_hex(8)}"
    temporary_created = False
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        existing = _read_key(path, missing_ok=True)
        if existing is not None:
            return False
        key = random_bytes(KEY_BYTES)
        if not isinstance(key, bytes) or len(key) != KEY_BYTES:
            raise PublishError("init_failed")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        try:
            fd = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
            temporary_created = True
            view = memoryview(key)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError(errno.EIO, "short write")
                view = view[count:]
            os.fsync(fd)
        except OSError as error:
            raise PublishError("init_failed") from error
        finally:
            if fd is not None:
                os.close(fd)
        try:
            os.link(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                    follow_symlinks=False)
        except FileExistsError:
            # A racing creator must still have produced a fully valid key.
            _read_key(path)
            return False
        except OSError as error:
            raise PublishError("init_failed") from error
        os.unlink(temporary, dir_fd=parent_fd)
        temporary_created = False
        os.fsync(parent_fd)
        _read_key(path)
        return True
    finally:
        if temporary_created:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
        try:
            fcntl.flock(parent_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(parent_fd)


def canonical_signature_payload(draft_id: str, expires: int) -> bytes:
    draft_id = validate_draft_id(draft_id)
    if not isinstance(expires, int) or isinstance(expires, bool) or expires < 0:
        raise PublishError("invalid_expiry")
    return f"v1\nGET\n/draft/{draft_id}\n{expires}".encode("ascii")


def _valid_dns_hostname(hostname: str) -> bool:
    if not hostname or len(hostname) > 253 or not hostname.isascii():
        return False
    if hostname.endswith("."):
        hostname = hostname[:-1]
    labels = hostname.split(".")
    if len(labels) < 2 or any(not HOST_LABEL_RE.fullmatch(label) for label in labels):
        return False
    try:
        ipaddress.ip_address(hostname)
        return False
    except ValueError:
        pass
    return hostname.lower() != "localhost" and not hostname.lower().endswith(".localhost")


def resolve_base_url(environ: Mapping[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    explicit = values.get("DRAFT_LIBRARY_BASE_URL")
    if explicit is not None:
        if not isinstance(explicit, str) or explicit != explicit.strip() or not explicit.isascii():
            raise PublishError("invalid_base_url")
        try:
            parsed = urlsplit(explicit)
            port = parsed.port
        except ValueError as error:
            raise PublishError("invalid_base_url") from error
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username is not None
                or parsed.password is not None or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or not parsed.hostname
                or not _valid_dns_hostname(parsed.hostname)
                or (port is not None and not 1 <= port <= 65535)):
            raise PublishError("invalid_base_url")
        authority = parsed.hostname.lower().rstrip(".") + (f":{port}" if port is not None else "")
        return f"https://{authority}"
    domain = values.get("RAILWAY_PUBLIC_DOMAIN")
    if (not isinstance(domain, str) or domain != domain.strip() or not domain.isascii()
            or not _valid_dns_hostname(domain)):
        raise PublishError("base_url_unavailable")
    return "https://" + domain.lower().rstrip(".")


def _read_json_regular(path: Path, validator: Any, maximum: int = 1024 * 1024) -> Any:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as error:
        raise PublishError("config_unavailable") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022
                or info.st_size > maximum):
            raise PublishError("invalid_config")
        data = b""
        while len(data) <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        if len(data) > maximum:
            raise PublishError("invalid_config")
        try:
            return validator.strict_json_loads(data.decode("utf-8", "strict"))
        except Exception as error:
            raise PublishError("invalid_config") from error
    finally:
        os.close(fd)


class DraftPublisher:
    def __init__(self, *, draft_root: Path = PRODUCTION_DRAFT_ROOT,
                 key_path: Path = PRODUCTION_KEY_PATH,
                 config_path: Path = PRODUCTION_CONFIG_PATH,
                 clock: Callable[[], float] = time.time,
                 environ: Mapping[str, str] | None = None,
                 validator_loader: Callable[[], Any] = _load_newsroom_validator,
                 spacefast_factory: Callable[[Mapping[str, str]], Any] | None = None) -> None:
        self.store = DraftStore(draft_root)
        self.key_path = Path(key_path)
        self.config_path = Path(config_path)
        self.clock = clock
        self.environ = os.environ if environ is None else environ
        self._validator_loader = validator_loader
        self._spacefast_factory = spacefast_factory or (
            lambda values: SpacefastClient(environ=values))

    def initialize(self, *, random_bytes: Callable[[int], bytes] = secrets.token_bytes) -> dict[str, bool]:
        return {"initialized": initialize_key(self.key_path, random_bytes)}

    def status(self) -> dict[str, bool]:
        key = _read_key(self.key_path, missing_ok=True)
        return {"library_initialized": key is not None}

    def load_draft_config(self) -> dict[str, str]:
        try:
            validator = self._validator_loader()
        except Exception as error:
            raise PublishError("invalid_config") from error
        document = _read_json_regular(self.config_path, validator)
        try:
            validator.validate_document("newsroom", document)
            style = validator.effective_editorial_style(document["newsroom"])
            config = validator.effective_drafts_config(document)
            if not isinstance(style, dict) or not isinstance(config, dict):
                raise TypeError("validator returned malformed effective configuration")
        except Exception as error:
            raise PublishError("invalid_config") from error
        if style.get("confirmed_by_editor") is not True:
            raise PublishError("editorial_style_unconfirmed")
        return config

    def save_file(self, draft_id: str, title: str, source_path: Path) -> dict[str, Any]:
        self.load_draft_config()
        return self.store.save_file(draft_id, title, source_path)

    def link(self, draft_id: str, ttl_seconds: int,
             *, editor_approved: bool = False) -> dict[str, Any]:
        draft_id = validate_draft_id(draft_id)
        if (not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool)
                or not 1 <= ttl_seconds <= MAX_TTL_SECONDS):
            raise PublishError("invalid_ttl")
        config = self.load_draft_config()
        policy = config["publishing_policy"]
        if config["destination"] not in {"library", "both"}:
            raise PublishError("library_destination_disabled")
        if policy == "never":
            raise PublishError("publication_disabled")
        if policy == "ask_each_time" and not editor_approved:
            raise PublishError("approval_required")
        # Authorization applies only to a currently complete, ID-bound snapshot.
        # Validate it before reading key material or computing any capability.
        self.store.read_snapshot(draft_id)
        key = _read_key(self.key_path)
        assert key is not None
        try:
            clock_value = self.clock()
            if (isinstance(clock_value, bool)
                    or not isinstance(clock_value, (int, float))
                    or not math.isfinite(clock_value)
                    or clock_value < 0):
                raise ValueError("invalid clock value")
            now = int(clock_value)
        except Exception as error:
            raise PublishError("invalid_clock") from error
        if now < 0 or now > (2**63 - 1) - ttl_seconds:
            raise PublishError("invalid_clock")
        expires = now + ttl_seconds
        payload = canonical_signature_payload(draft_id, expires)
        signature = hmac.new(key, payload, hashlib.sha256).hexdigest()
        base = resolve_base_url(self.environ)
        query = urlencode({"expires": str(expires), "sig": signature})
        return {"expires": expires, "url": f"{base}/draft/{draft_id}?{query}"}

    def publish(self, draft_id: str, *, editor_approved: bool = False) -> dict[str, Any]:
        validate_draft_id(draft_id)
        config = self.load_draft_config()
        if config["publishing_policy"] == "never":
            raise PublishError("publication_disabled")
        destination = config["destination"]
        result: dict[str, Any] = {}
        if destination in {"library", "both"}:
            try:
                link = self.link(draft_id, DEFAULT_TTL_SECONDS,
                                 editor_approved=editor_approved)
                result["library"] = {"status": "published", **link}
            except PublishError as error:
                if str(error) in {"editorial_style_unconfirmed", "invalid_config"}:
                    raise
                result["library"] = {"status": str(error)}
        if destination in {"spacefast", "both"}:
            current_config = self.load_draft_config()
            if current_config["publishing_policy"] == "never":
                raise PublishError("publication_disabled")
            if current_config["destination"] not in {"spacefast", "both"}:
                result["spacefast"] = {"status": "spacefast_destination_disabled"}
            elif current_config["spacefast_setup_status"] != "configured":
                result["spacefast"] = {"status": "spacefast_not_configured"}
            elif not editor_approved:
                # auto_private applies only to the local Draft Library.
                result["spacefast"] = {"status": "approval_required"}
            else:
                try:
                    client = self._spacefast_factory(self.environ)
                    validate_runtime = getattr(client, "validate_configuration", None)
                    if validate_runtime is not None:
                        validate_runtime()
                    with self.store.publication_transaction(draft_id) as transaction:
                        snapshot = transaction.snapshot
                        rendition = snapshot["rendition.html"]
                        digest = hashlib.sha256(rendition).hexdigest()
                        metadata = json.loads(snapshot["publication.json"].decode("ascii"))
                        space_id = metadata.get("space_id")
                        decoded = rendition.decode("utf-8", "strict")
                        match = re.search(r"<title>([^<]*)</title>", decoded)
                        if match is None:
                            raise PublishError("unsafe_draft_state")
                        title = html.unescape(match.group(1))
                        clock_value = self.clock()
                        if (isinstance(clock_value, bool)
                                or not isinstance(clock_value, (int, float))
                                or not math.isfinite(clock_value) or clock_value < 0
                                or clock_value > 2**63 - 1):
                            raise PublishError("invalid_clock")
                        remote = client.publish(title, rendition, digest, draft_id=draft_id,
                                                space_id=space_id)
                        publication = {**remote, "rendition_sha256": digest,
                                       "published_at": int(clock_value)}
                        try:
                            transaction.update_publication(publication)
                        except PublicationCommitUncertain as error:
                            if error.intended_visible:
                                result["spacefast"] = {
                                    "status": "published", "durability": "uncertain", **remote}
                            else:
                                result["spacefast"] = {
                                    "status": "publication_commit_uncertain", **remote}
                        else:
                            result["spacefast"] = {"status": "published", **remote}
                except SpacefastError as error:
                    result["spacefast"] = {"status": str(error)}
                except DraftError as error:
                    status = ("publication_commit_uncertain"
                              if str(error) == "publication_commit_uncertain"
                              else "unsafe_draft_state")
                    result["spacefast"] = {"status": status}
                except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                    result["spacefast"] = {"status": "unsafe_draft_state"}
                except PublishError as error:
                    result["spacefast"] = {"status": str(error)}
        return result


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(prog="draft_publish.py", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("status")
    save = commands.add_parser("save")
    save.add_argument("--id", required=True)
    save.add_argument("--title", required=True)
    save.add_argument("--input", required=True)
    link = commands.add_parser("link")
    link.add_argument("--id", required=True)
    link.add_argument("--ttl-seconds", required=True, type=int)
    link.add_argument("--editor-approved", action="store_true")
    publish = commands.add_parser("publish")
    publish.add_argument("--id", required=True)
    publish.add_argument("--editor-approved", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        publisher = DraftPublisher()
        code = 0
        if arguments.command == "init":
            payload: dict[str, Any] = {"ok": True, **publisher.initialize()}
        elif arguments.command == "status":
            payload = {"ok": True, **publisher.status()}
        elif arguments.command == "save":
            saved = publisher.save_file(arguments.id, arguments.title, Path(arguments.input))
            payload = {"ok": True, **saved}
        elif arguments.command == "link":
            payload = {"ok": True, **publisher.link(
                arguments.id, arguments.ttl_seconds,
                editor_approved=arguments.editor_approved)}
        else:
            targets = publisher.publish(
                arguments.id, editor_approved=arguments.editor_approved)
            complete = bool(targets) and all(
                target.get("status") == "published" for target in targets.values()
            )
            payload = {"ok": complete, "targets": targets}
            if not complete:
                code = 1
    except (PublishError, DraftError) as error:
        payload = {"error": str(error), "ok": False}
        code = 1
    except Exception:
        payload = {"error": "operation_failed", "ok": False}
        code = 1
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
