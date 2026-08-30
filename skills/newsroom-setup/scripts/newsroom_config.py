#!/usr/bin/env python3
"""Validate and persist profile-owned newsroom configuration."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Iterator, NoReturn
import uuid
from urllib.parse import urlsplit


KINDS = ("newsroom", "sources")
SECRET_KEYS = {"api_key", "password", "secret", "token"}
LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
SOURCE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
HOSTNAME_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
TIMEZONE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
URL_SCHEME_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
INVALID_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
LOCK_ATTEMPTS = 50
LOCK_DELAY_SECONDS = 0.1


class ConfigError(Exception):
    """An expected user-facing configuration error."""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ConfigError(message)


def default_newsroom() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "revision": 0,
        "setup_status": "draft",
        "newsroom": {
            "name": "Unnamed newsroom",
            "timezone": "Etc/UTC",
            "report_languages": ["en"],
        },
        "coverage": {"places": [], "public_bodies": [], "beats": []},
        "preferences": {
            "preserve_original_language": True,
            "require_primary_source_citations": True,
            "allow_unofficial_fallbacks": False,
        },
    }


def default_sources() -> dict[str, Any]:
    return {"schema_version": 1, "revision": 0, "sources": []}


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be an object")
    return value


def require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a nonempty string")
    return value


def reject_secret_keys(value: Any, location: str = "document") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.lower() in SECRET_KEYS:
                raise ConfigError(f"forbidden field name at {location}: {key}")
            reject_secret_keys(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_secret_keys(child, f"{location}[{index}]")


def reject_unicode_surrogates(value: Any) -> None:
    """Reject strings that cannot be encoded as valid UTF-8."""
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ConfigError("document contains an invalid Unicode surrogate")
    elif isinstance(value, dict):
        for key, child in value.items():
            reject_unicode_surrogates(key)
            reject_unicode_surrogates(child)
    elif isinstance(value, list):
        for child in value:
            reject_unicode_surrogates(child)


def validate_common(document: Any) -> dict[str, Any]:
    document = require_object(document, "document")
    reject_unicode_surrogates(document)
    reject_secret_keys(document)
    if document.get("schema_version") != 1 or isinstance(document.get("schema_version"), bool):
        raise ConfigError("schema_version must be 1")
    revision = document.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise ConfigError("revision must be an integer")
    if revision < 0:
        raise ConfigError("revision must be a nonnegative integer")
    return document


def validate_languages(value: Any, label: str) -> None:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{label} must be a nonempty array")
    for language in value:
        if not isinstance(language, str) or not LANGUAGE_RE.fullmatch(language):
            raise ConfigError(f"{label} contains an invalid language tag")


def is_iana_style_timezone(value: str) -> bool:
    if URL_SCHEME_PREFIX_RE.match(value) or any(character.isspace() for character in value):
        return False
    components = value.split("/")
    return (
        len(components) >= 2
        and all(components)
        and all(
            TIMEZONE_COMPONENT_RE.fullmatch(component)
            and set(component) != {"."}
            for component in components
        )
    )


def is_valid_hostname(value: str) -> bool:
    hostname = value[:-1] if value.endswith(".") else value
    if not hostname or len(hostname) > 253:
        return False
    return all(HOSTNAME_LABEL_RE.fullmatch(label) for label in hostname.split("."))


def is_https_url(value: str) -> bool:
    if any(
        character == "\\"
        or character.isspace()
        or ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        for character in value
    ) or INVALID_PERCENT_RE.search(value):
        return False
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False

        authority = parsed.netloc
        if authority.startswith("["):
            match = re.fullmatch(r"\[([^]]+)](?::([0-9]+))?", authority)
            if not match:
                return False
            ipaddress.ip_address(match.group(1))
            port_text = match.group(2)
        else:
            if authority.count(":") > 1:
                return False
            hostname, separator, port_text = authority.rpartition(":")
            if not separator:
                hostname, port_text = authority, None
            if not is_valid_hostname(hostname):
                return False

        if port_text is not None and not (
            re.fullmatch(r"[0-9]+", port_text) and 1 <= int(port_text) <= 65535
        ):
            return False
        # Accessing these properties also invokes urllib's malformed-host and
        # malformed-port checks.
        if parsed.hostname is None:
            return False
        parsed.port
    except (ValueError, UnicodeError):
        return False
    return True


def validate_newsroom(document: Any) -> None:
    document = validate_common(document)
    if document.get("setup_status") not in {"draft", "partial", "complete"}:
        raise ConfigError("setup_status must be draft, partial, or complete")

    details = require_object(document.get("newsroom"), "newsroom")
    require_nonempty_string(details.get("name"), "newsroom.name")
    timezone_name = require_nonempty_string(details.get("timezone"), "newsroom.timezone")
    if not is_iana_style_timezone(timezone_name):
        raise ConfigError("newsroom.timezone must be IANA-style text containing '/'")
    validate_languages(details.get("report_languages"), "newsroom.report_languages")

    coverage = require_object(document.get("coverage"), "coverage")
    for field in ("places", "public_bodies", "beats"):
        if not isinstance(coverage.get(field), list):
            raise ConfigError(f"coverage.{field} must be an array")

    preferences = require_object(document.get("preferences"), "preferences")
    for field in (
        "preserve_original_language",
        "require_primary_source_citations",
        "allow_unofficial_fallbacks",
    ):
        if not isinstance(preferences.get(field), bool):
            raise ConfigError(f"preferences.{field} must be a boolean")


def is_rfc3339(value: Any) -> bool:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_sources(document: Any) -> None:
    document = validate_common(document)
    source_list = document.get("sources")
    if not isinstance(source_list, list):
        raise ConfigError("sources must be an array")
    seen_ids: set[str] = set()
    for index, raw_source in enumerate(source_list):
        label = f"sources[{index}]"
        source = require_object(raw_source, label)
        source_id = require_nonempty_string(source.get("id"), f"{label}.id")
        if not SOURCE_ID_RE.fullmatch(source_id) or source_id in {".", ".."}:
            raise ConfigError(f"{label}.id is not a safe ID")
        if source_id in seen_ids:
            raise ConfigError(f"duplicate source ID: {source_id}")
        seen_ids.add(source_id)

        official_url = require_nonempty_string(source.get("official_url"), f"{label}.official_url")
        if not is_https_url(official_url):
            raise ConfigError(f"{label}.official_url must be an HTTPS URL")

        authority = source.get("authority_status")
        if authority not in {"official", "official_mirror", "non_official"}:
            raise ConfigError(f"{label}.authority_status is invalid")
        status = source.get("status")
        if status not in {"candidate", "active", "inactive"}:
            raise ConfigError(f"{label}.status is invalid")

        record_types = require_object(source.get("record_types"), f"{label}.record_types")
        for record_name, availability in record_types.items():
            require_nonempty_string(record_name, f"{label}.record_types key")
            if availability not in {"verified", "partial", "unavailable", "unknown"}:
                raise ConfigError(f"{label}.record_types.{record_name} is invalid")
        validate_languages(source.get("languages"), f"{label}.languages")

        if status == "active" and authority in {"official", "official_mirror"}:
            if not is_rfc3339(source.get("last_validated_at")):
                raise ConfigError(
                    f"{label}.last_validated_at must be RFC3339 for an active official source"
                )
        elif "last_validated_at" in source and not is_rfc3339(source["last_validated_at"]):
            raise ConfigError(f"{label}.last_validated_at must be RFC3339 when present")


def validate_document(kind: str, document: Any) -> None:
    if kind == "newsroom":
        validate_newsroom(document)
    elif kind == "sources":
        validate_sources(document)
    else:
        raise ConfigError(f"unsupported kind: {kind}")


def validate_profile_completion(
    newsroom_document: dict[str, Any], sources_document: dict[str, Any]
) -> None:
    """Enforce the invariant that makes a persisted complete state meaningful."""
    if newsroom_document.get("setup_status") != "complete":
        return
    has_active_validated_official = any(
        source.get("status") == "active"
        and source.get("authority_status") in {"official", "official_mirror"}
        and is_rfc3339(source.get("last_validated_at"))
        for source in sources_document.get("sources", [])
        if isinstance(source, dict)
    )
    if not has_active_validated_official:
        raise ConfigError(
            "setup_status=complete requires at least one active validated official "
            "or official_mirror source"
        )


def read_document_with_content(path: Path) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise ConfigError(f"input is not a regular file: {path}")
    try:
        content = path.read_bytes()
        value = json.loads(
            content.decode("utf-8"),
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {constant}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read JSON from {path}: {error}") from error
    except ValueError as error:
        raise ConfigError(f"cannot read JSON from {path}: {error}") from error
    return require_object(value, "document"), content


def read_document(path: Path) -> dict[str, Any]:
    return read_document_with_content(path)[0]


def strict_json_loads(content: str | bytes) -> Any:
    return json.loads(
        content,
        parse_constant=lambda constant: (_ for _ in ()).throw(
            ValueError(f"invalid JSON constant: {constant}")
        ),
    )


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def make_event(
    action: str,
    kind: str,
    revision: int,
    old_content: bytes,
    new_content: bytes,
) -> dict[str, Any]:
    return {
        "action": action,
        "kind": kind,
        "revision": revision,
        "old_sha256": sha256(old_content),
        "new_sha256": sha256(new_content),
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def command_status(root: Path) -> tuple[dict[str, Any], int]:
    documents: dict[str, dict[str, Any]] = {}
    states: dict[str, dict[str, Any]] = {}
    profile_error = None
    with writer_lock(root) as store:
        for kind in KINDS:
            try:
                document, _ = _json_at(store.fd, f"{kind}.json")
                validate_document(kind, document)
                documents[kind] = document
                states[kind] = {"valid": True, "revision": document["revision"]}
            except ConfigError as error:
                states[kind] = {"valid": False, "error": str(error)}
        if len(documents) == len(KINDS):
            try:
                validate_profile_completion(documents["newsroom"], documents["sources"])
            except ConfigError as error:
                profile_error = str(error)
    valid = all(state["valid"] for state in states.values()) and profile_error is None
    result = {"ok": valid, "command": "status", **states}
    if profile_error is not None:
        result["error"] = profile_error
    return result, 0 if valid else 1


def command_validate(kind: str, input_path: Path) -> dict[str, Any]:
    document = read_document(input_path)
    validate_document(kind, document)
    return {"ok": True, "valid": True, "kind": kind, "revision": document["revision"]}


# Linux persistence layer. All mutable newsroom names are resolved relative to an
# already-open directory descriptor; no security decision relies on a prior path
# lookup.
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
JOURNAL_NAME = ".config-transaction.json"
JOURNAL_FIELDS = frozenset({"version", "transaction_id", "kind", "old", "new", "event"})
AUDIT_EVENT_FIELDS = frozenset({
    "action", "kind", "revision", "old_sha256", "new_sha256", "timestamp", "transaction_id",
})


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def _check_entry(directory_fd: int, name: str, *, directory: bool = False) -> bool:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    wanted = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if stat.S_ISLNK(info.st_mode) or not wanted or (not directory and info.st_nlink != 1):
        raise ConfigError(f"unsafe newsroom persistence entry: {name}")
    return True


def _open_regular(directory_fd: int, name: str, flags: int, mode: int = 0o600) -> int:
    try:
        descriptor = os.open(name, flags | NOFOLLOW | NONBLOCK, mode, dir_fd=directory_fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EISDIR, errno.ENOTDIR, errno.ENXIO):
            raise ConfigError(f"unsafe newsroom persistence entry: {name}") from error
        raise
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise ConfigError(f"unsafe newsroom persistence entry: {name}")
    return descriptor


def _read_at(directory_fd: int, name: str) -> bytes:
    descriptor = _open_regular(directory_fd, name, os.O_RDONLY)
    try:
        chunks = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _atomic_write_at(
    directory_fd: int,
    name: str,
    content: bytes,
    *,
    fsync_failpoint: str | None = None,
    replace_failpoint: str | None = None,
    directory_fsync_failpoint: str | None = None,
) -> None:
    _check_entry(directory_fd, name)
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = _open_regular(
            directory_fd, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        _write_all(descriptor, content)
        if fsync_failpoint:
            _failpoint(fsync_failpoint)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if replace_failpoint:
            _failpoint(replace_failpoint)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        if directory_fsync_failpoint:
            _failpoint(directory_fsync_failpoint)
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _json_at(directory_fd: int, name: str) -> tuple[dict[str, Any], bytes]:
    try:
        content = _read_at(directory_fd, name)
        value = strict_json_loads(content.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read newsroom JSON: {name}") from error
    return require_object(value, "document"), content


class RootStore:
    def __init__(self, path: Path, create: bool = False):
        self.path = path
        self.fd = -1
        home = path.parent
        if create:
            home.mkdir(parents=True, exist_ok=True)
        home_fd = os.open(home, os.O_RDONLY | DIRECTORY | NOFOLLOW)
        try:
            if create:
                try:
                    os.mkdir(path.name, 0o700, dir_fd=home_fd)
                    os.fsync(home_fd)
                except FileExistsError:
                    pass
            _check_entry(home_fd, path.name, directory=True)
            self.fd = os.open(path.name, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=home_fd)
            if not stat.S_ISDIR(os.fstat(self.fd).st_mode):
                raise ConfigError("newsroom root must be a real directory")
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ConfigError("newsroom root must be a real directory") from error
            raise
        finally:
            os.close(home_fd)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def audit_fd(self, create: bool = False) -> int:
        if create:
            try:
                os.mkdir("audit", 0o700, dir_fd=self.fd)
                os.fsync(self.fd)
            except FileExistsError:
                pass
        _check_entry(self.fd, "audit", directory=True)
        try:
            descriptor = os.open("audit", os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=self.fd)
        except OSError as error:
            raise ConfigError("audit path must be a real directory") from error
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ConfigError("audit path must be a real directory")
        return descriptor


def resolve_root(home_argument: str | None) -> Path:
    home_text = home_argument or os.environ.get("HERMES_HOME")
    if not home_text:
        raise ConfigError("--home or HERMES_HOME is required")
    home = Path(home_text).expanduser()
    if not home.is_absolute():
        home = Path.cwd() / home
    # Canonicalize HOME once; everything below it is descriptor-relative.
    return home.resolve(strict=False) / "newsroom"


def _failpoint(name: str) -> None:
    if os.environ.get("NEWSROOM_CONFIG_FAILPOINT") == name:
        raise OSError(errno.EIO, f"injected failure: {name}")


@contextmanager
def writer_lock(root: Path) -> Iterator[RootStore]:
    store = RootStore(root, create=True)
    lock_fd = -1
    try:
        _check_entry(store.fd, ".config.lock")
        lock_fd = _open_regular(store.fd, ".config.lock", os.O_RDWR | os.O_CREAT)
        deadline = time.monotonic() + LOCK_ATTEMPTS * LOCK_DELAY_SECONDS
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ConfigError("timed out waiting for newsroom configuration lock")
                time.sleep(LOCK_DELAY_SECONDS)
        if os.environ.get("NEWSROOM_CONFIG_TEST_HOLD_LOCK"):
            time.sleep(30)
        _recover(store)
        yield store
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)  # closing releases flock; the shared inode remains
        store.close()


def _ensure_audit(store: RootStore) -> None:
    audit_fd = store.audit_fd(create=True)
    try:
        if not _check_entry(audit_fd, "config-events.jsonl"):
            _atomic_write_at(audit_fd, "config-events.jsonl", b"")
    finally:
        os.close(audit_fd)


def _audit_once(store: RootStore, event: dict[str, Any], inject: bool = False) -> None:
    audit_fd = store.audit_fd(create=False)
    try:
        name = "config-events.jsonl"
        raw = _read_at(audit_fd, name)
        valid_end = 0
        found = False
        for line in raw.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                break
            try:
                parsed = strict_json_loads(line)
                if not isinstance(parsed, dict):
                    break
            except (UnicodeError, ValueError, json.JSONDecodeError):
                break
            valid_end += len(line)
            found = found or parsed.get("transaction_id") == event["transaction_id"]
        rebuilt = raw[:valid_end]
        if not found:
            rebuilt += (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        if inject:
            _failpoint("audit_append")  # legacy name; failure occurs before replacement
        _atomic_write_at(
            audit_fd,
            name,
            rebuilt,
            replace_failpoint="audit_replace" if inject else None,
        )
    finally:
        os.close(audit_fd)


def _decode_transaction(raw: bytes) -> dict[str, Any]:
    try:
        transaction = require_object(strict_json_loads(raw.decode("utf-8")), "transaction")
        if set(transaction) != JOURNAL_FIELDS:
            raise ValueError("unexpected transaction journal fields")
        if transaction.get("version") != 1 or isinstance(transaction.get("version"), bool):
            raise ValueError("unsupported journal version")
        transaction_id = transaction.get("transaction_id")
        kind = transaction.get("kind")
        event = require_object(transaction.get("event"), "transaction event")
        if set(event) != AUDIT_EVENT_FIELDS:
            raise ValueError("unexpected transaction event fields")
        if not isinstance(transaction_id, str) or not transaction_id or kind not in KINDS:
            raise ValueError("invalid transaction identity")
        if event.get("transaction_id") != transaction_id:
            raise ValueError("transaction ID mismatch")
        action = event.get("action")
        if action not in {"apply", "undo"} or event.get("kind") != kind:
            raise ValueError("transaction action or kind mismatch")
        for field in ("old", "new"):
            encoded = transaction.get(field)
            if not isinstance(encoded, str):
                raise ValueError("invalid transaction content")
            transaction[field] = base64.b64decode(encoded, validate=True)
        old_document = require_object(
            strict_json_loads(transaction["old"].decode("utf-8")), "old transaction document"
        )
        new_document = require_object(
            strict_json_loads(transaction["new"].decode("utf-8")), "new transaction document"
        )
        validate_document(kind, old_document)
        validate_document(kind, new_document)
        old_revision = old_document.get("revision")
        new_revision = new_document.get("revision")
        if (
            not isinstance(old_revision, int)
            or isinstance(old_revision, bool)
            or not isinstance(new_revision, int)
            or isinstance(new_revision, bool)
            or new_revision != old_revision + 1
        ):
            raise ValueError("invalid transaction revision relation")
        if event.get("revision") != new_revision or isinstance(event.get("revision"), bool):
            raise ValueError("event revision mismatch")
        if event.get("old_sha256") != sha256(transaction["old"]):
            raise ValueError("old content hash mismatch")
        if event.get("new_sha256") != sha256(transaction["new"]):
            raise ValueError("new content hash mismatch")
        if not is_rfc3339(event.get("timestamp")):
            raise ValueError("invalid event timestamp")
    except (ConfigError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError("invalid newsroom transaction journal") from error
    return transaction


def _complete_transaction(
    store: RootStore,
    transaction: dict[str, Any],
    inject: bool,
    *,
    target_already_replaced: bool = False,
) -> None:
    kind = transaction["kind"]
    target = f"{kind}.json"
    backup = f"{kind}.json.previous"
    _check_entry(store.fd, target)
    _check_entry(store.fd, backup)
    _atomic_write_at(
        store.fd,
        backup,
        transaction["old"],
        replace_failpoint="backup_replace" if inject else None,
    )
    if not target_already_replaced:
        _atomic_write_at(
            store.fd,
            target,
            transaction["new"],
            replace_failpoint="target_replace" if inject else None,
        )
    _audit_once(store, transaction["event"], inject=inject)
    if inject:
        _failpoint("journal_unlink")
    os.unlink(JOURNAL_NAME, dir_fd=store.fd)
    if inject:
        _failpoint("final_directory_fsync")
    os.fsync(store.fd)


def _recover(store: RootStore) -> None:
    if not _check_entry(store.fd, JOURNAL_NAME):
        return
    transaction = _decode_transaction(_read_at(store.fd, JOURNAL_NAME))
    target = f"{transaction['kind']}.json"
    if not _check_entry(store.fd, target):
        # This journal format only represents mutations of initialized files;
        # neither authenticated state permits an absent target.
        raise ConfigError("transaction journal does not match current target")
    try:
        current = _read_at(store.fd, target)
    except FileNotFoundError as error:
        # The entry disappeared between validation and descriptor-relative open.
        raise ConfigError("transaction journal does not match current target") from error
    if current == transaction["old"]:
        target_already_replaced = False
    elif current == transaction["new"]:
        target_already_replaced = True
    else:
        raise ConfigError("transaction journal does not match current target")
    _ensure_audit(store)
    _complete_transaction(
        store,
        transaction,
        inject=False,
        target_already_replaced=target_already_replaced,
    )


def _mutate(store: RootStore, action: str, kind: str, old_content: bytes, new_content: bytes,
            revision: int) -> None:
    transaction_id = uuid.uuid4().hex
    event = make_event(action, kind, revision, old_content, new_content)
    event["transaction_id"] = transaction_id
    transaction = {
        "version": 1, "transaction_id": transaction_id, "kind": kind,
        "old": base64.b64encode(old_content).decode("ascii"),
        "new": base64.b64encode(new_content).decode("ascii"), "event": event,
    }
    _atomic_write_at(
        store.fd,
        JOURNAL_NAME,
        json_bytes(transaction),
        fsync_failpoint="journal_write_fsync",
        directory_fsync_failpoint="directory_fsync",
    )
    _failpoint("staged_write")
    _complete_transaction(store, _decode_transaction(json_bytes(transaction)), inject=True)


def command_init(root: Path) -> dict[str, Any]:
    with writer_lock(root) as store:
        created = []
        for kind, document in (("newsroom", default_newsroom()), ("sources", default_sources())):
            name = f"{kind}.json"
            if not _check_entry(store.fd, name):
                _atomic_write_at(store.fd, name, json_bytes(document))
                created.append(kind)
        _check_entry(store.fd, JOURNAL_NAME)
        _ensure_audit(store)
    return {"ok": True, "command": "init", "created": created}


def inspect_kind_at(store: RootStore, kind: str) -> dict[str, Any]:
    try:
        document, _ = _json_at(store.fd, f"{kind}.json")
        validate_document(kind, document)
        return {"valid": True, "revision": document["revision"]}
    except ConfigError as error:
        return {"valid": False, "error": str(error)}


def inspect_kind(root: Path, kind: str) -> dict[str, Any]:
    store = None
    try:
        store = RootStore(root)
        return inspect_kind_at(store, kind)
    except ConfigError as error:
        return {"valid": False, "error": str(error)}
    finally:
        if store is not None:
            store.close()


def command_apply(root: Path, kind: str, input_path: Path) -> dict[str, Any]:
    candidate = read_document(input_path)
    validate_document(kind, candidate)
    with writer_lock(root) as store:
        current, old_content = _json_at(store.fd, f"{kind}.json")
        validate_document(kind, current)
        revision = current["revision"] + 1
        replacement = dict(candidate)
        replacement["revision"] = revision
        validate_document(kind, replacement)
        counterpart_kind = "sources" if kind == "newsroom" else "newsroom"
        counterpart, _ = _json_at(store.fd, f"{counterpart_kind}.json")
        validate_document(counterpart_kind, counterpart)
        profile = {kind: replacement, counterpart_kind: counterpart}
        validate_profile_completion(profile["newsroom"], profile["sources"])
        _ensure_audit(store)
        _mutate(store, "apply", kind, old_content, json_bytes(replacement), revision)
    return {"ok": True, "command": "apply", "kind": kind, "revision": revision}


def command_undo(root: Path, kind: str) -> dict[str, Any]:
    with writer_lock(root) as store:
        current, old_content = _json_at(store.fd, f"{kind}.json")
        previous, _ = _json_at(store.fd, f"{kind}.json.previous")
        validate_document(kind, current)
        validate_document(kind, previous)
        revision = current["revision"] + 1
        restored = dict(previous)
        restored["revision"] = revision
        validate_document(kind, restored)
        counterpart_kind = "sources" if kind == "newsroom" else "newsroom"
        counterpart, _ = _json_at(store.fd, f"{counterpart_kind}.json")
        validate_document(counterpart_kind, counterpart)
        profile = {kind: restored, counterpart_kind: counterpart}
        validate_profile_completion(profile["newsroom"], profile["sources"])
        _ensure_audit(store)
        _mutate(store, "undo", kind, old_content, json_bytes(restored), revision)
    return {"ok": True, "command": "undo", "kind": kind, "revision": revision}


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)

    init_parser = commands.add_parser("init")
    init_parser.add_argument("--home")
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--home")

    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("--kind", required=True, choices=KINDS)
    validate_parser.add_argument("--input", required=True, type=Path)

    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("--home")
    apply_parser.add_argument("--kind", required=True, choices=KINDS)
    apply_parser.add_argument("--input", required=True, type=Path)

    undo_parser = commands.add_parser("undo")
    undo_parser.add_argument("--home")
    undo_parser.add_argument("--kind", required=True, choices=KINDS)
    return parser


def main(arguments: list[str] | None = None) -> int:
    try:
        options = build_parser().parse_args(arguments)
        if options.command == "validate":
            result = command_validate(options.kind, options.input)
            code = 0
        else:
            root = resolve_root(options.home)
            if options.command == "init":
                result, code = command_init(root), 0
            elif options.command == "status":
                result, code = command_status(root)
            elif options.command == "apply":
                result, code = command_apply(root, options.kind, options.input), 0
            elif options.command == "undo":
                result, code = command_undo(root, options.kind), 0
            else:
                raise ConfigError(f"unsupported command: {options.command}")
    except ConfigError as error:
        result = {"ok": False, "valid": False, "error": str(error)}
        code = 1
    except Exception:
        result = {"ok": False, "valid": False, "error": "internal configuration error"}
        code = 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
