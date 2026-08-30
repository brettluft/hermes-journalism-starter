#!/usr/bin/env python3
"""Write-free authenticated HTTP access to canonical draft renditions."""

from __future__ import annotations

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import math
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
from typing import Callable, Mapping

from draft_publish import (
    MAX_TTL_SECONDS,
    PRODUCTION_KEY_PATH,
    PublishError,
    _read_key,
    canonical_signature_payload,
    initialize_key,
)
from draft_store import DraftError, DraftStore, PRODUCTION_DRAFT_ROOT, validate_draft_id


DEFAULT_PORT = 8080
BIND_ADDRESS = "0.0.0.0"
MAX_REQUEST_LINE = 8192
MAX_HEADER_BYTES = 16384
MAX_HEADER_COUNT = 50
REQUEST_TIMEOUT_SECONDS = 10
MAX_REQUEST_WORKERS = 32
CONNECTION_DEADLINE_SECONDS = 10.0
MAX_UNIX_SECONDS = 2**63 - 1
SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
EXPIRY_RE = re.compile(r"^(?:0|[1-9][0-9]{0,18})$", re.ASCII)
HEALTH_BODY = b"ok\n"
ERROR_BODIES = {
    400: b"bad request\n",
    403: b"forbidden\n",
    404: b"not found\n",
    405: b"method not allowed\n",
    408: b"request timeout\n",
    414: b"bad request\n",
    417: b"bad request\n",
    431: b"bad request\n",
    500: b"service unavailable\n",
    501: b"method not allowed\n",
    503: b"service unavailable\n",
    505: b"bad request\n",
}
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


def resolve_port(environ: Mapping[str, str] | None = None) -> int:
    """Resolve a bounded decimal listener port without consulting other state."""
    values = os.environ if environ is None else environ
    raw = values.get("PORT")
    if raw is None:
        return DEFAULT_PORT
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
        raise ValueError("invalid port")
    port = int(raw, 10)
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    return port


def _safe_now(clock: Callable[[], float]) -> int | None:
    try:
        value = clock()
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0 or value > MAX_UNIX_SECONDS):
            return None
        return int(value)
    except Exception:
        return None


class DraftLibraryHTTPServer(ThreadingHTTPServer):
    """Thread-per-request server whose construction has no persistent side effects."""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler],
                 *, store: DraftStore, key_path: Path,
                 clock: Callable[[], float], worker_limit: int = MAX_REQUEST_WORKERS,
                 connection_deadline: float = CONNECTION_DEADLINE_SECONDS) -> None:
        if (isinstance(worker_limit, bool) or not isinstance(worker_limit, int)
                or not 1 <= worker_limit <= MAX_REQUEST_WORKERS):
            raise ValueError("invalid worker limit")
        if (isinstance(connection_deadline, bool)
                or not isinstance(connection_deadline, (int, float))
                or not math.isfinite(connection_deadline)
                or not 0 < connection_deadline <= CONNECTION_DEADLINE_SECONDS):
            raise ValueError("invalid connection deadline")
        self.store = store
        self.key_path = Path(key_path)
        self.clock = clock
        self.worker_limit = worker_limit
        self.connection_deadline = float(connection_deadline)
        self._worker_slots = threading.BoundedSemaphore(worker_limit)
        self._worker_state_lock = threading.Lock()
        self._worker_states: dict[int, dict[str, object]] = {}
        self._active_worker_count = 0
        self._peak_active_worker_count = 0
        super().__init__(server_address, handler_class)

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(REQUEST_TIMEOUT_SECONDS)
        return request, client_address

    @property
    def active_worker_count(self) -> int:
        with self._worker_state_lock:
            return self._active_worker_count

    @property
    def peak_active_worker_count(self) -> int:
        with self._worker_state_lock:
            return self._peak_active_worker_count

    def process_request(self, request, client_address) -> None:
        """Start a worker only when a fixed slot is immediately available."""
        if not self._worker_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return

        registered = False
        try:
            state: dict[str, object] = {"active": True, "request": request}
            request_key = id(request)

            def expire() -> None:
                # Cancellation and expiry are mutually exclusive, and this closure
                # retains the exact socket object rather than a reusable descriptor.
                with self._worker_state_lock:
                    if not state["active"]:
                        return
                    try:
                        request.shutdown(socket.SHUT_RDWR)  # type: ignore[attr-defined]
                    except OSError:
                        pass

            timer = threading.Timer(self.connection_deadline, expire)
            timer.daemon = True
            state["timer"] = timer
            with self._worker_state_lock:
                self._worker_states[request_key] = state
                self._active_worker_count += 1
                self._peak_active_worker_count = max(
                    self._peak_active_worker_count, self._active_worker_count)
            registered = True
            timer.start()
            super().process_request(request, client_address)
        except BaseException:
            if registered:
                self._finish_worker(request)
            else:
                self._worker_slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._finish_worker(request)

    def _finish_worker(self, request) -> None:
        timer: threading.Timer | None = None
        with self._worker_state_lock:
            state = self._worker_states.pop(id(request), None)
            if state is None:
                return
            state["active"] = False
            candidate = state.get("timer")
            if isinstance(candidate, threading.Timer):
                timer = candidate
            self._active_worker_count -= 1
        if timer is not None:
            timer.cancel()
        self._worker_slots.release()

    def server_close(self) -> None:
        # Wake partial readers during close. Worker finally blocks retain sole
        # ownership of timer cancellation and exactly-once permit release.
        with self._worker_state_lock:
            requests = [state["request"] for state in self._worker_states.values()]
        for request in requests:
            try:
                request.shutdown(socket.SHUT_RDWR)  # type: ignore[attr-defined]
            except OSError:
                pass
        super().server_close()

    def handle_error(self, request, client_address) -> None:
        # Request targets, signatures, paths, and exception details never enter logs.
        del request, client_address


class DraftLibraryRequestHandler(BaseHTTPRequestHandler):
    """An exact two-route handler with generic responses and no request logging."""

    protocol_version = "HTTP/1.1"
    server_version = ""
    sys_version = ""

    def log_message(self, format: str, *args) -> None:
        del format, args

    def log_error(self, format: str, *args) -> None:
        del format, args

    def address_string(self) -> str:
        return "client"

    def handle_expect_100(self) -> bool:
        self._respond(417, ERROR_BODIES[417])
        return False

    def handle_one_request(self) -> None:
        """Apply a tighter request-target bound than the standard library default."""
        try:
            self.raw_requestline = self.rfile.readline(MAX_REQUEST_LINE + 1)
            if len(self.raw_requestline) > MAX_REQUEST_LINE:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.close_connection = True
                self._respond(414, ERROR_BODIES[414])
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self._raw_request_target_is_canonical():
                # Reject before parse_request() can collapse a target beginning
                # with "//" or accept ambiguous whitespace separators.
                self.requestline = ""
                self.request_version = "HTTP/1.0"
                self.command = ""
                self.close_connection = True
                self._respond(400, ERROR_BODIES[400])
                return
            if not self.parse_request():
                return
            if self.request_version not in {"HTTP/1.0", "HTTP/1.1"}:
                self._respond(505, ERROR_BODIES[505])
                return
            if not self._headers_acceptable():
                self._respond(431, ERROR_BODIES[431])
                return
            if self.command == "GET":
                self.do_GET()
            else:
                self._refuse_method()
            try:
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                self.close_connection = True
        except TimeoutError:
            self.close_connection = True
            try:
                self._respond(408, ERROR_BODIES[408])
            except (BrokenPipeError, ConnectionError, OSError):
                pass
        except (BrokenPipeError, ConnectionError, OSError):
            self.close_connection = True

    def _raw_request_target_is_canonical(self) -> bool:
        """Validate origin-form request-target bytes before stdlib normalization."""
        line = self.raw_requestline
        if line.endswith(b"\r\n"):
            line = line[:-2]
        elif line.endswith(b"\n"):
            line = line[:-1]

        # HTTP whitespace splitting is deliberately too permissive here: tabs,
        # repeated spaces, and leading/trailing spaces can make intermediaries
        # disagree about which bytes constitute the target.
        parts = line.split(b" ")
        if (len(parts) != 3 or not all(parts)
                or not all(0x20 <= byte <= 0x7E for byte in line)):
            return False
        _method, target, _version = parts

        # Only an unambiguous origin-form is accepted.  In particular,
        # BaseHTTPRequestHandler rewrites leading "//" to "/" for security;
        # rejecting the raw form prevents it from inheriting a canonical route
        # or signed draft capability after that rewrite.
        return (
            target.startswith(b"/")
            and not target.startswith(b"//")
            and b"\\" not in target
            and all(0x21 <= byte <= 0x7E for byte in target)
        )

    def _headers_acceptable(self) -> bool:
        try:
            items = list(self.headers.raw_items())
            total = sum(len(name) + len(value) + 4 for name, value in items)
            return len(items) <= MAX_HEADER_COUNT and total <= MAX_HEADER_BYTES
        except Exception:
            return False

    def send_error(self, code: int, message=None, explain=None) -> None:
        del message, explain
        mapped = code if code in ERROR_BODIES else (405 if code == 501 else 400)
        self._respond(mapped, ERROR_BODIES[mapped])

    def _respond(self, status: int, body: bytes, *, content_type: str = "text/plain; charset=utf-8") -> None:
        self.close_connection = True
        try:
            # BaseHTTPRequestHandler suppresses every header for HTTP/0.9. This
            # service refuses 0.9 and still emits the generic hardened response.
            if getattr(self, "request_version", "") == "HTTP/0.9":
                self.request_version = "HTTP/1.0"
            self.send_response_only(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Connection", "close")
            if status == 405:
                self.send_header("Allow", "GET")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionError, OSError):
            self.close_connection = True

    def _refuse_method(self) -> None:
        self._respond(405, ERROR_BODIES[405])

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._respond(200, HEALTH_BODY)
            return

        server = self.server
        assert isinstance(server, DraftLibraryHTTPServer)
        try:
            key = _read_key(server.key_path, missing_ok=True)
        except PublishError:
            key = None
        if key is None:
            self._respond(503, ERROR_BODIES[503])
            return

        parsed = self._parse_content_target()
        if parsed is None:
            return
        draft_id, expires, signature = parsed
        now = _safe_now(server.clock)
        if now is None or expires <= now or expires - now > MAX_TTL_SECONDS:
            self._respond(400, ERROR_BODIES[400])
            return

        try:
            payload = canonical_signature_payload(draft_id, expires)
            expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
        except (DraftError, PublishError, ValueError, OverflowError):
            self._respond(400, ERROR_BODIES[400])
            return
        if not hmac.compare_digest(signature, expected):
            self._respond(403, ERROR_BODIES[403])
            return

        try:
            rendition = server.store.read_rendition(draft_id)
        except (DraftError, OSError):
            self._respond(404, ERROR_BODIES[404])
            return
        self._respond(200, rendition, content_type="text/html; charset=utf-8")

    def _parse_content_target(self) -> tuple[str, int, str] | None:
        target = self.path
        if (not isinstance(target, str) or not target.isascii() or "#" in target
                or target.count("?") != 1):
            self._respond(400, ERROR_BODIES[400])
            return None
        path, query = target.split("?", 1)
        prefix = "/draft/"
        if not path.startswith(prefix):
            self._respond(404, ERROR_BODIES[404])
            return None
        draft_id = path[len(prefix):]
        # Raw URL syntax is intentionally not decoded or normalized.
        if not draft_id or "%" in draft_id or "/" in draft_id or "\\" in draft_id:
            self._respond(400, ERROR_BODIES[400])
            return None
        try:
            validate_draft_id(draft_id)
        except DraftError:
            self._respond(400, ERROR_BODIES[400])
            return None

        parts = query.split("&")
        if len(parts) != 2:
            self._respond(400, ERROR_BODIES[400])
            return None
        values: dict[str, str] = {}
        for part in parts:
            if part.count("=") != 1:
                self._respond(400, ERROR_BODIES[400])
                return None
            name, value = part.split("=", 1)
            if name not in {"expires", "sig"} or name in values:
                self._respond(400, ERROR_BODIES[400])
                return None
            values[name] = value
        if set(values) != {"expires", "sig"}:
            self._respond(400, ERROR_BODIES[400])
            return None
        if not EXPIRY_RE.fullmatch(values["expires"]):
            self._respond(400, ERROR_BODIES[400])
            return None
        expires = int(values["expires"], 10)
        if expires > MAX_UNIX_SECONDS or not SIGNATURE_RE.fullmatch(values["sig"]):
            self._respond(400, ERROR_BODIES[400])
            return None
        return draft_id, expires, values["sig"]


# Explicit method functions make direct handler introspection fail closed too.
for _method in ("HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"):
    setattr(DraftLibraryRequestHandler, f"do_{_method}", DraftLibraryRequestHandler._refuse_method)


def build_server(address: tuple[str, int] = (BIND_ADDRESS, DEFAULT_PORT), *,
                 store: DraftStore | None = None,
                 draft_root: Path = PRODUCTION_DRAFT_ROOT,
                 key_path: Path = PRODUCTION_KEY_PATH,
                 clock: Callable[[], float] = time.time,
                 worker_limit: int = MAX_REQUEST_WORKERS,
                 connection_deadline: float = CONNECTION_DEADLINE_SECONDS,
                 ) -> DraftLibraryHTTPServer:
    """Construct the listener without creating or modifying persistent state."""
    selected_store = DraftStore(draft_root) if store is None else store
    return DraftLibraryHTTPServer(
        address, DraftLibraryRequestHandler,
        store=selected_store, key_path=Path(key_path), clock=clock,
        worker_limit=worker_limit, connection_deadline=connection_deadline,
    )


def main() -> int:
    try:
        port = resolve_port()
        server = build_server((BIND_ADDRESS, port))
    except (OSError, ValueError):
        print("draft library unavailable", file=sys.stderr)
        return 1
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
