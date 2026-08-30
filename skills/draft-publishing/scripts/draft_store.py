#!/usr/bin/env python3
"""Canonical local draft storage and deliberately small inert HTML rendering."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import html
import json
import os
from pathlib import Path
import re
import stat
import unicodedata
import uuid
from typing import Callable, Any
from urllib.parse import urlsplit


PRODUCTION_DRAFT_ROOT = Path("/opt/data/newsroom/drafts")
MAX_TITLE_BYTES = 4096
MAX_SOURCE_BYTES = 1024 * 1024
MAX_RENDITION_BYTES = MAX_SOURCE_BYTES * 8
MAX_METADATA_BYTES = 4096
DRAFT_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
SPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,254}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FILES = ("source.txt", "rendition.html", "publication.json")
RENAME_EXCHANGE = 2
STAGE_NAME_RE = re.compile(r"^\.stage-[0-9a-f]{32}$")
MAX_STAGE_CANDIDATES = 64
MAX_STAGE_ENTRIES = len(FILES) + 1


class DraftError(Exception):
    """A sanitized local draft failure."""


class PublicationCommitUncertain(DraftError):
    """The namespace changed but directory durability could not be confirmed."""

    def __init__(self, *, intended_visible: bool = False) -> None:
        self.intended_visible = intended_visible
        super().__init__("publication_commit_uncertain")


class SaveCommitUncertain(DraftError):
    """The draft namespace changed but root-directory durability is unknown."""

    def __init__(self) -> None:
        super().__init__("save_commit_uncertain")


def _rename_exchange(parent_fd: int, left: str, right: str) -> None:
    """Atomically exchange two names in one open directory via Linux renameat2."""
    if (not isinstance(left, str) or not isinstance(right, str)
            or not left or not right or "/" in left or "/" in right
            or "\x00" in left or "\x00" in right):
        raise OSError(errno.EINVAL, "invalid exchange name")
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as error:
        raise OSError(errno.ENOSYS, "renameat2 unavailable") from error
    renameat2.argtypes = (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_fd, os.fsencode(left), parent_fd, os.fsencode(right), RENAME_EXCHANGE
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number or errno.EIO, "atomic directory exchange failed")


def validate_draft_id(draft_id: Any) -> str:
    if not isinstance(draft_id, str) or not DRAFT_ID_RE.fullmatch(draft_id):
        raise DraftError("invalid_draft_id")
    return draft_id


def _validate_text(value: Any, label: str, limit: int, *, multiline: bool) -> bytes:
    if not isinstance(value, str):
        raise DraftError(f"invalid_{label}")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError as error:
        raise DraftError(f"invalid_{label}") from error
    if not encoded or len(encoded) > limit:
        raise DraftError(f"invalid_{label}")
    for character in value:
        if multiline and character in "\n\r\t":
            continue
        if unicodedata.category(character).startswith("C"):
            raise DraftError(f"invalid_{label}")
    return encoded


def render_html(title: str, source: str) -> str:
    """Render escaped text into a deterministic heading/paragraph/list subset."""
    safe_title = html.escape(title, quote=True)
    blocks: list[str] = []
    paragraphs = re.split(r"\n[ \t]*\n", source)
    for paragraph in paragraphs:
        lines = paragraph.splitlines() or [""]
        if all(line.startswith(("- ", "* ")) for line in lines if line) and any(lines):
            items = "".join(
                f"<li>{html.escape(line[2:], quote=True)}</li>" for line in lines if line
            )
            blocks.append(f"<ul>{items}</ul>")
        elif len(lines) == 1 and re.fullmatch(r"#{1,3} .+", lines[0]):
            count = len(lines[0]) - len(lines[0].lstrip("#"))
            text = html.escape(lines[0][count + 1 :], quote=True)
            blocks.append(f"<h{count + 1}>{text}</h{count + 1}>")
        else:
            text = "<br>\n".join(html.escape(line, quote=True) for line in lines)
            blocks.append(f"<p>{text}</p>")
    body = "\n".join(blocks)
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        f"<title>{safe_title}</title>\n"
        "</head>\n<body>\n<article>\n"
        f"<h1>{safe_title}</h1>\n{body}\n"
        "</article>\n</body>\n</html>\n"
    )


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise DraftError("unsafe_storage") from error
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        os.close(fd)
        raise DraftError("unsafe_storage")
    return fd


def _verify_directory_path(fd: int, path: Path) -> None:
    """Require the nofollow path to still name this exact private directory."""
    descriptor_info = os.fstat(fd)
    try:
        path_info = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise DraftError("unsafe_storage") from error
    if (not stat.S_ISDIR(descriptor_info.st_mode)
            or descriptor_info.st_uid != os.geteuid()
            or stat.S_IMODE(descriptor_info.st_mode) != 0o700
            or not stat.S_ISDIR(path_info.st_mode)
            or path_info.st_uid != os.geteuid()
            or stat.S_IMODE(path_info.st_mode) != 0o700
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)):
        raise DraftError("unsafe_storage")


def _mkdir_root(path: Path) -> None:
    """Walk and create without ever following a directory symlink."""
    absolute = path.absolute()
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        components = absolute.parts[1:]
        for index, component in enumerate(components):
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
                    raise DraftError("storage_unavailable") from error
            except OSError as error:
                raise DraftError("unsafe_storage") from error
            os.close(current)
            current = child
            if index == len(components) - 1:
                info = os.fstat(current)
                if (info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    raise DraftError("unsafe_storage")
    finally:
        os.close(current)


def _open_child_directory(parent_fd: int, name: str) -> int:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    except OSError as error:
        raise DraftError("unsafe_draft_state") from error
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_nlink != 2
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        os.close(fd)
        raise DraftError("unsafe_draft_state")
    return fd


def _open_draft_lock(root_fd: int, draft_id: str, *, create: bool) -> int:
    """Open the stable, private per-draft lock without following links."""
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(f".lock-{draft_id}", flags, 0o600, dir_fd=root_fd)
    except OSError as error:
        raise DraftError("unsafe_draft_state") from error
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
        os.close(fd)
        raise DraftError("unsafe_draft_state")
    return fd


def _bounded_snapshot_names(fd: int) -> set[str]:
    """List at most one entry beyond the fixed snapshot shape."""
    names: set[str] = set()
    try:
        with os.scandir(fd) as entries:
            for entry in entries:
                names.add(entry.name)
                if len(names) >= MAX_STAGE_ENTRIES:
                    raise DraftError("unsafe_draft_state")
    except DraftError:
        raise
    except OSError as error:
        raise DraftError("unsafe_draft_state") from error
    return names


def _strict_json_object(raw: bytes) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    return json.loads(
        raw.decode("ascii", "strict"), object_pairs_hook=unique_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
    )


def _safe_spacefast_url(value: Any) -> bool:
    if (not isinstance(value, str) or value != value.strip() or not value.isascii()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower().rstrip(".")
    return (parsed.scheme == "https" and bool(hostname) and parsed.username is None
            and parsed.password is None and not parsed.query and not parsed.fragment
            and port in (None, 443)
            and (hostname == "spacefast.app" or hostname.endswith(".spacefast.app")
                 or hostname == "spacefast.com" or hostname.endswith(".spacefast.com")))


def _validate_publication_metadata(metadata: Any, expected_draft_id: str | None) -> None:
    if not isinstance(metadata, dict) or set(metadata) not in (
            {"draft_id", "schema_version", "space_id"},
            {"draft_id", "schema_version", "space_id", "spacefast"}):
        raise ValueError("invalid metadata")
    if (type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1
            or (metadata["space_id"] is not None
                and (not isinstance(metadata["space_id"], str)
                     or not SPACE_ID_RE.fullmatch(metadata["space_id"])))):
        raise ValueError("invalid metadata")
    metadata_draft_id = validate_draft_id(metadata["draft_id"])
    if expected_draft_id is not None and validate_draft_id(expected_draft_id) != metadata_draft_id:
        raise ValueError("snapshot identity mismatch")
    if "spacefast" in metadata:
        publication = metadata["spacefast"]
        required = {"space_id", "live_url", "immutable_url", "rendition_sha256", "published_at"}
        if (not isinstance(publication, dict) or set(publication) != required
                or publication["space_id"] != metadata["space_id"]
                or not isinstance(publication["space_id"], str)
                or not SPACE_ID_RE.fullmatch(publication["space_id"])
                or not _safe_spacefast_url(publication["live_url"])
                or not _safe_spacefast_url(publication["immutable_url"])
                or not isinstance(publication["rendition_sha256"], str)
                or not SHA256_RE.fullmatch(publication["rendition_sha256"])
                or type(publication["published_at"]) is not int
                or not 0 <= publication["published_at"] <= 2**63 - 1):
            raise ValueError("invalid metadata")


def _validate_snapshot_content(snapshot: dict[str, bytes],
                               expected_draft_id: str | None) -> None:
    """Require a snapshot to be consistent and, when published, ID-bound."""
    try:
        source = snapshot["source.txt"].decode("utf-8", "strict")
        _validate_text(source, "source", MAX_SOURCE_BYTES, multiline=True)
        rendition = snapshot["rendition.html"].decode("utf-8", "strict")
        metadata = _strict_json_object(snapshot["publication.json"])
        _validate_publication_metadata(metadata, expected_draft_id)
        canonical_metadata = json.dumps(
            metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii") + b"\n"
        if snapshot["publication.json"] != canonical_metadata:
            raise ValueError("noncanonical metadata")
        match = re.fullmatch(
            r'<!doctype html>\n<html lang="en">\n<head>\n'
            r'<meta charset="utf-8">\n'
            r'<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            r'<title>([^<]*)</title>\n</head>\n<body>\n<article>\n'
            r'<h1>([^<]*)</h1>\n.*</article>\n</body>\n</html>\n',
            rendition, flags=re.DOTALL,
        )
        if match is None or match.group(1) != match.group(2):
            raise ValueError("invalid rendition")
        title = html.unescape(match.group(1))
        _validate_text(title, "title", MAX_TITLE_BYTES, multiline=False)
        if render_html(title, source) != rendition:
            raise ValueError("inconsistent rendition")
    except (DraftError, KeyError, TypeError, ValueError, UnicodeError) as error:
        raise DraftError("unsafe_draft_state") from error


def _read_snapshot_files(root_fd: int, draft_id: str) -> dict[str, bytes]:
    """Read one directory descriptor safely; caller retains the shared lock."""
    draft_fd = _open_child_directory(root_fd, draft_id)
    try:
        if _bounded_snapshot_names(draft_fd) != set(FILES):
            raise DraftError("unsafe_draft_state")
        limits = {
            "source.txt": MAX_SOURCE_BYTES,
            "rendition.html": MAX_RENDITION_BYTES,
            "publication.json": MAX_METADATA_BYTES,
        }
        snapshot: dict[str, bytes] = {}
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        for filename in FILES:
            try:
                file_fd = os.open(filename, flags, dir_fd=draft_fd)
            except OSError as error:
                raise DraftError("unsafe_draft_state") from error
            try:
                info = os.fstat(file_fd)
                maximum = limits[filename]
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > maximum):
                    raise DraftError("unsafe_draft_state")
                chunks: list[bytes] = []
                remaining = maximum + 1
                while remaining:
                    chunk = os.read(file_fd, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                content = b"".join(chunks)
                if len(content) > maximum:
                    raise DraftError("unsafe_draft_state")
                snapshot[filename] = content
            except OSError as error:
                raise DraftError("unsafe_draft_state") from error
            finally:
                os.close(file_fd)
        return snapshot
    finally:
        os.close(draft_fd)


def _validate_snapshot(parent_fd: int, name: str,
                       expected_draft_id: str | None) -> None:
    snapshot = _read_snapshot_files(parent_fd, name)
    _validate_snapshot_content(snapshot, expected_draft_id)


def _scavenge_abandoned_stages(root_fd: int) -> None:
    """Validate every exact internal stage before removing any of them."""
    candidates: list[str] = []
    try:
        with os.scandir(root_fd) as entries:
            for entry in entries:
                if STAGE_NAME_RE.fullmatch(entry.name):
                    candidates.append(entry.name)
                    if len(candidates) > MAX_STAGE_CANDIDATES:
                        raise DraftError("unsafe_draft_state")
    except DraftError:
        raise
    except OSError as error:
        raise DraftError("unsafe_draft_state") from error

    for name in candidates:
        # Stages have no request-facing name. Their strictly validated metadata
        # remains their identity until a save publishes them canonically.
        _validate_snapshot(root_fd, name, None)
    for name in candidates:
        # Revalidate immediately before descriptor-relative deletion. A changed
        # candidate is never treated as the object checked in the first pass.
        _validate_snapshot(root_fd, name, None)
        _remove_snapshot(root_fd, name)
        try:
            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise DraftError("unsafe_draft_state") from error
        else:
            raise DraftError("unsafe_draft_state")
    if candidates:
        try:
            os.fsync(root_fd)
        except OSError as error:
            raise DraftError("save_failed") from error


def _remove_snapshot(parent_fd: int, name: str) -> None:
    """Remove only a private staging snapshot with fixed children."""
    try:
        fd = _open_child_directory(parent_fd, name)
    except DraftError:
        return
    try:
        for filename in FILES:
            try:
                os.unlink(filename, dir_fd=fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(fd)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


class DraftPublicationTransaction:
    """Exclusive per-draft publication transaction using the stable draft lock."""

    def __init__(self, store: "DraftStore", draft_id: str) -> None:
        self._store = store
        self.draft_id = validate_draft_id(draft_id)
        self._root_fd: int | None = None
        self._lock_fd: int | None = None
        self.snapshot: dict[str, bytes] = {}
        self._active = False

    def __enter__(self) -> "DraftPublicationTransaction":
        root_fd = _open_directory(self._store.root)
        lock_fd: int | None = None
        try:
            _verify_directory_path(root_fd, self._store.root)
            lock_fd = _open_draft_lock(root_fd, self.draft_id, create=False)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            _verify_directory_path(root_fd, self._store.root)
            snapshot = _read_snapshot_files(root_fd, self.draft_id)
            _validate_snapshot_content(snapshot, self.draft_id)
            self._root_fd = root_fd
            self._lock_fd = lock_fd
            self.snapshot = dict(snapshot)
            self._active = True
            return self
        except DraftError:
            raise
        except OSError as error:
            raise DraftError("unsafe_draft_state") from error
        finally:
            if not self._active:
                try:
                    if lock_fd is not None:
                        try:
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        except OSError:
                            pass
                        try:
                            os.close(lock_fd)
                        except OSError:
                            pass
                finally:
                    try:
                        os.close(root_fd)
                    except OSError:
                        pass

    def update_publication(self, publication: dict[str, Any]) -> None:
        if not self._active or self._root_fd is None:
            raise DraftError("publication_transaction_inactive")
        self._store._update_publication_locked(self._root_fd, self.draft_id, publication)

    def __exit__(self, _exception_type: Any, _exception: Any, _traceback: Any) -> None:
        self._active = False
        try:
            if self._lock_fd is not None:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(self._lock_fd)
                except OSError:
                    pass
        finally:
            self._lock_fd = None
            if self._root_fd is not None:
                try:
                    os.close(self._root_fd)
                except OSError:
                    pass
                self._root_fd = None


class DraftStore:
    """Descriptor-relative store; injected roots are for Python tests only."""

    def __init__(self, root: Path = PRODUCTION_DRAFT_ROOT,
                 *, failpoint: Callable[[str], None] | None = None) -> None:
        self.root = Path(root)
        self._failpoint = failpoint or (lambda _stage: None)

    def save_file(self, draft_id: str, title: str, source_path: Path) -> dict[str, Any]:
        fd: int | None = None
        try:
            fd = os.open(
                Path(source_path),
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            )
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_mode & 0o022
                    or info.st_size > MAX_SOURCE_BYTES):
                raise DraftError("invalid_source")
            chunks: list[bytes] = []
            remaining = MAX_SOURCE_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        except OSError:
            raise DraftError("invalid_source") from None
        finally:
            if fd is not None:
                os.close(fd)
        if len(raw) > MAX_SOURCE_BYTES:
            raise DraftError("invalid_source")
        try:
            source = raw.decode("utf-8", "strict")
        except UnicodeError:
            raise DraftError("invalid_source") from None
        return self.save(draft_id, title, source)

    def read_snapshot(self, draft_id: str) -> dict[str, bytes]:
        """Read a complete application snapshot under the writer's stable lock."""
        draft_id = validate_draft_id(draft_id)
        root_fd = _open_directory(self.root)
        lock_fd: int | None = None
        try:
            _verify_directory_path(root_fd, self.root)
            lock_fd = _open_draft_lock(root_fd, draft_id, create=False)
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
            _verify_directory_path(root_fd, self.root)
            snapshot = _read_snapshot_files(root_fd, draft_id)
            _validate_snapshot_content(snapshot, draft_id)
            return snapshot
        except DraftError:
            raise
        except OSError as error:
            raise DraftError("unsafe_draft_state") from error
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(lock_fd)
            os.close(root_fd)

    def read_rendition(self, draft_id: str) -> bytes:
        """Return only the rendition from the coordinated snapshot read path."""
        return self.read_snapshot(draft_id)["rendition.html"]

    def publication_transaction(self, draft_id: str) -> DraftPublicationTransaction:
        """Own the stable exclusive draft lock from snapshot through metadata commit."""
        return DraftPublicationTransaction(self, draft_id)

    def update_publication(self, draft_id: str, publication: dict[str, Any]) -> None:
        """Atomically replace publication state through the exclusive transaction API."""
        with self.publication_transaction(draft_id) as transaction:
            transaction.update_publication(publication)

    def _update_publication_locked(self, root_fd: int, draft_id: str,
                                   publication: dict[str, Any]) -> None:
        """Commit allowlisted metadata while the caller owns the stable draft lock."""
        metadata_object = {"draft_id": draft_id, "schema_version": 1,
                           "space_id": publication.get("space_id")
                           if isinstance(publication, dict) else None,
                           "spacefast": publication}
        try:
            _validate_publication_metadata(metadata_object, draft_id)
            metadata = json.dumps(metadata_object, ensure_ascii=True, sort_keys=True,
                                  separators=(",", ":")).encode("ascii") + b"\n"
        except (DraftError, TypeError, ValueError, UnicodeError):
            raise DraftError("invalid_publication") from None

        draft_fd: int | None = None
        temporary = f".publication-{uuid.uuid4().hex}"
        temporary_exists = False
        replaced = False
        try:
            _verify_directory_path(root_fd, self.root)
            snapshot = _read_snapshot_files(root_fd, draft_id)
            _validate_snapshot_content(snapshot, draft_id)
            draft_fd = _open_child_directory(root_fd, draft_id)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            file_fd = os.open(temporary, flags, 0o600, dir_fd=draft_fd)
            temporary_exists = True
            try:
                view = memoryview(metadata)
                while view:
                    count = os.write(file_fd, view)
                    if count <= 0:
                        raise OSError(errno.EIO, "short write")
                    view = view[count:]
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
            self._failpoint("publication_swap")
            self._failpoint("publication_before_replace")
            _verify_directory_path(root_fd, self.root)
            os.replace(temporary, "publication.json", src_dir_fd=draft_fd, dst_dir_fd=draft_fd)
            temporary_exists = False
            replaced = True
            self._failpoint("publication_after_replace")
            _verify_directory_path(root_fd, self.root)
            try:
                _verify_directory_path(root_fd, self.root)
                self._failpoint("publication_directory_fsync")
                os.fsync(draft_fd)
            except OSError:
                intended_visible = False
                try:
                    _verify_directory_path(root_fd, self.root)
                    updated = _read_snapshot_files(root_fd, draft_id)
                    _validate_snapshot_content(updated, draft_id)
                    intended_visible = updated["publication.json"] == metadata
                except DraftError:
                    pass
                raise PublicationCommitUncertain(
                    intended_visible=intended_visible) from None
            _verify_directory_path(root_fd, self.root)
            updated = _read_snapshot_files(root_fd, draft_id)
            _validate_snapshot_content(updated, draft_id)
            if updated["publication.json"] != metadata:
                raise PublicationCommitUncertain()
            _verify_directory_path(root_fd, self.root)
        except DraftError as error:
            if replaced and not isinstance(error, PublicationCommitUncertain):
                raise PublicationCommitUncertain() from None
            raise
        except OSError as error:
            if replaced:
                raise PublicationCommitUncertain() from None
            raise DraftError("publication_update_failed") from error
        finally:
            if temporary_exists and draft_fd is not None:
                try:
                    os.unlink(temporary, dir_fd=draft_fd)
                except OSError:
                    pass
            if draft_fd is not None:
                os.close(draft_fd)

    def save(self, draft_id: str, title: str, source: str) -> dict[str, Any]:
        draft_id = validate_draft_id(draft_id)
        title_bytes = _validate_text(title, "title", MAX_TITLE_BYTES, multiline=False)
        source_bytes = _validate_text(source, "source", MAX_SOURCE_BYTES, multiline=True)
        rendition = render_html(title, source).encode("utf-8")
        metadata = json.dumps(
            {"draft_id": draft_id, "schema_version": 1, "space_id": None},
            ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii") + b"\n"
        del title_bytes

        _mkdir_root(self.root)
        root_fd = _open_directory(self.root)
        stage = f".stage-{uuid.uuid4().hex}"
        stage_exists = False
        lock_fd: int | None = None
        root_trusted = True
        namespace_mutated = False
        commit_durable = False

        def verify_root() -> None:
            nonlocal root_trusted
            try:
                _verify_directory_path(root_fd, self.root)
            except DraftError:
                root_trusted = False
                raise

        try:
            verify_root()
            lock_fd = _open_draft_lock(root_fd, draft_id, create=True)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            verify_root()
            # This is the only path that owns both locks: always take the stable
            # per-draft lock first so waiting on a publication cannot hold root.
            fcntl.flock(root_fd, fcntl.LOCK_EX)
            verify_root()
            _scavenge_abandoned_stages(root_fd)
            verify_root()
            # Preserve publication identity across later local source saves. This
            # read occurs under the same stable writer lock used for the exchange.
            try:
                os.stat(draft_id, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                current = _read_snapshot_files(root_fd, draft_id)
                _validate_snapshot_content(current, draft_id)
                metadata = current["publication.json"]
            self._failpoint("create")
            verify_root()
            try:
                os.mkdir(stage, 0o700, dir_fd=root_fd)
                stage_exists = True
            except OSError as error:
                raise DraftError("save_failed") from error
            stage_fd = _open_child_directory(root_fd, stage)
            try:
                for filename, content in zip(FILES, (source_bytes, rendition, metadata)):
                    self._failpoint("write")
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                    file_fd = os.open(filename, flags, 0o600, dir_fd=stage_fd)
                    try:
                        view = memoryview(content)
                        while view:
                            written = os.write(file_fd, view)
                            if written <= 0:
                                raise OSError(errno.EIO, "short write")
                            view = view[written:]
                        self._failpoint("fsync")
                        os.fsync(file_fd)
                    finally:
                        os.close(file_fd)
                os.fsync(stage_fd)
            except (OSError, DraftError) as error:
                if isinstance(error, DraftError):
                    raise
                raise DraftError("save_failed") from error
            finally:
                os.close(stage_fd)

            _validate_snapshot(root_fd, stage, draft_id)

            exists = False
            try:
                os.stat(draft_id, dir_fd=root_fd, follow_symlinks=False)
                exists = True
            except FileNotFoundError:
                pass
            if exists:
                _validate_snapshot(root_fd, draft_id, draft_id)
            self._failpoint("replace")
            verify_root()
            try:
                if exists:
                    self._failpoint("swap")
                    verify_root()
                    _rename_exchange(root_fd, stage, draft_id)
                    namespace_mutated = True
                    verify_root()
                    # The canonical name now refers to the complete new snapshot;
                    # staging refers to the old one and is only cleanup material.
                    self._failpoint("after_exchange")
                else:
                    verify_root()
                    os.rename(stage, draft_id, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                    namespace_mutated = True
                    verify_root()
                    stage_exists = False
                verify_root()
                self._failpoint("root_fsync")
                os.fsync(root_fd)
                verify_root()
                commit_durable = True
            except OSError as error:
                raise DraftError("save_failed") from error
            if stage_exists:
                verify_root()
                try:
                    self._failpoint("cleanup")
                    _remove_snapshot(root_fd, stage)
                    stage_exists = False
                    os.fsync(root_fd)
                except OSError:
                    # Cleanup is not part of the commit. The exchanged old snapshot
                    # may safely remain under its unguessable staging name.
                    pass
            verify_root()
            return {"draft_id": draft_id, "saved": True}
        except DraftError as error:
            if namespace_mutated and not commit_durable and not isinstance(error, SaveCommitUncertain):
                raise SaveCommitUncertain() from None
            raise
        except KeyboardInterrupt:
            if namespace_mutated and not commit_durable:
                raise SaveCommitUncertain() from None
            raise
        except OSError as error:
            if namespace_mutated and not commit_durable:
                raise SaveCommitUncertain() from None
            raise DraftError("save_failed") from error
        finally:
            if (root_trusted and stage_exists
                    and not (namespace_mutated and not commit_durable)):
                try:
                    _remove_snapshot(root_fd, stage)
                except OSError:
                    pass
            try:
                fcntl.flock(root_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                if lock_fd is not None:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    os.close(lock_fd)
            finally:
                os.close(root_fd)
