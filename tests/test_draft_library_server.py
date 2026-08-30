import hashlib
import hmac
import http.client
import importlib.util
import io
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/draft-publishing/scripts"
SCRIPT = SCRIPTS / "draft_library_server.py"


def load_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("draft_library_server_under_test", SCRIPT)
        if spec is None or spec.loader is None:
            raise ImportError("draft library server is absent")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class DraftLibraryServerTests(unittest.TestCase):
    NOW = 2_000_000_000

    def setUp(self):
        self.module = load_module()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.draft_root = base / "drafts"
        self.key_path = base / "draft-library/hmac.key"
        self.store = self.module.DraftStore(self.draft_root)
        self.server = self.module.build_server(
            ("127.0.0.1", 0), store=self.store, key_path=self.key_path,
            clock=lambda: self.NOW,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.port = self.server.server_address[1]

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, method, target, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def raw_request(self, request_line):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.addCleanup(sock.close)
        sock.sendall(request_line + b"\r\nHost: example.test\r\n\r\n")
        response = http.client.HTTPResponse(sock)
        response.begin()
        result = response.status, dict(response.getheaders()), response.read()
        sock.close()
        return result

    def initialize(self, key=b"k" * 32):
        self.module.initialize_key(self.key_path, lambda _size: key)

    def target(self, draft_id="one", *, expires=None, key=b"k" * 32):
        expires = self.NOW + 60 if expires is None else expires
        payload = self.module.canonical_signature_payload(draft_id, expires)
        signature = hmac.new(key, payload, hashlib.sha256).hexdigest()
        return f"/draft/{draft_id}?" + urlencode({"expires": str(expires), "sig": signature})

    def assert_security_headers(self, headers):
        lowered = {name.lower(): value for name, value in headers.items()}
        self.assertEqual(lowered["x-content-type-options"], "nosniff")
        self.assertEqual(lowered["referrer-policy"], "no-referrer")
        self.assertEqual(lowered["cache-control"], "private, no-store")
        self.assertIn("default-src 'none'", lowered["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", lowered["content-security-policy"])
        self.assertNotIn("server", lowered)
        self.assertNotIn("date", lowered)

    def test_health_is_public_constant_and_construction_is_write_free(self):
        self.assertFalse(self.draft_root.exists())
        self.assertFalse(self.key_path.parent.exists())
        before = sorted(str(path.relative_to(self.temporary.name))
                        for path in Path(self.temporary.name).rglob("*"))
        first = self.request("GET", "/healthz")
        self.initialize()
        second = self.request("GET", "/healthz")
        self.assertEqual(first[0], 200)
        self.assertEqual(first[2], second[2])
        self.assertLessEqual(len(first[2]), 16)
        self.assertNotIn(b"key", first[2].lower())
        self.assertNotIn(b"draft", first[2].lower())
        self.assertEqual(before, [])
        self.assert_security_headers(first[1])

    def test_every_get_content_route_is_503_before_setup_without_writes(self):
        before = set(Path(self.temporary.name).rglob("*"))
        for target in ("/", "/draft/one?expires=1&sig=" + "0" * 64,
                       "/source.txt", "/newsroom.json", "/debug"):
            with self.subTest(target=target):
                status, headers, body = self.request("GET", target)
                self.assertEqual(status, 503)
                self.assertEqual(body, b"service unavailable\n")
                self.assert_security_headers(headers)
        self.assertEqual(set(Path(self.temporary.name).rglob("*")), before)

    def test_valid_signed_request_returns_exact_rendition(self):
        self.store.save("one", "Title", "Exact body")
        expected = self.store.read_rendition("one")
        self.initialize()
        status, headers, body = self.request("GET", self.target())
        self.assertEqual(status, 200)
        self.assertEqual(body, expected)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assert_security_headers(headers)

    def test_http_never_serves_a_snapshot_renamed_under_another_draft_id(self):
        self.store.save("one", "Private", "SECRET RENAMED RENDITION")
        self.store.save("two", "Public", "PUBLIC")
        displaced = self.draft_root / "two-original"
        (self.draft_root / "two").rename(displaced)
        (self.draft_root / "one").rename(self.draft_root / "two")
        self.initialize()

        status, _headers, body = self.request("GET", self.target("two"))
        self.assertEqual(status, 404)
        self.assertNotIn(b"SECRET RENAMED RENDITION", body)

    def test_noncanonical_raw_request_targets_are_rejected_before_normalization(self):
        self.store.save("one", "Title", "CONFIDENTIAL RENDITION")
        self.initialize()
        canonical_target = self.target().encode("ascii")
        status, _headers, body = self.raw_request(b"GET " + canonical_target + b" HTTP/1.1")
        self.assertEqual((status, body), (200, self.store.read_rendition("one")))

        draft_suffix = canonical_target.removeprefix(b"/draft/")
        malformed_lines = (
            b"GET //healthz HTTP/1.1",
            b"GET ///healthz HTTP/1.1",
            b"GET ////healthz HTTP/1.1",
            b"GET ////////healthz HTTP/1.1",
            b"GET //draft/" + draft_suffix + b" HTTP/1.1",
            b"GET ///draft/" + draft_suffix + b" HTTP/1.1",
            b"GET ////draft/" + draft_suffix + b" HTTP/1.1",
            b"GET http://example.test/healthz HTTP/1.1",
            b"GET http://example.test" + canonical_target + b" HTTP/1.1",
            b"GET https://example.test/healthz HTTP/1.1",
            b"GET \\healthz HTTP/1.1",
            b"GET /\\healthz HTTP/1.1",
            b"GET /draft\\" + draft_suffix + b" HTTP/1.1",
            b"GET\t/healthz HTTP/1.1",
            b"GET\t /healthz HTTP/1.1",
            b"GET /healthz\tHTTP/1.1",
            b"GET /healthz HTTP/1.1\t",
            b"GET  /healthz HTTP/1.1",
            b"GET /healthz  HTTP/1.1",
            b" GET /healthz HTTP/1.1",
        )
        for request_line in malformed_lines:
            with self.subTest(request_line=request_line):
                status, headers, body = self.raw_request(request_line)
                self.assertEqual(status, 400)
                self.assertEqual(body, b"bad request\n")
                self.assertNotIn(b"CONFIDENTIAL RENDITION", body)
                self.assert_security_headers(headers)

        self.assertEqual(self.raw_request(b"GET /healthz HTTP/1.1")[0], 200)
        self.assertEqual(self.raw_request(b"GET " + canonical_target + b" HTTP/1.1")[0], 200)

    def test_dynamic_key_setup_and_rotation(self):
        self.store.save("one", "Title", "Body")
        old = self.target(key=b"k" * 32)
        self.assertEqual(self.request("GET", old)[0], 503)
        self.initialize(b"k" * 32)
        self.assertEqual(self.request("GET", old)[0], 200)
        self.key_path.write_bytes(b"r" * 32)
        self.assertEqual(self.request("GET", old)[0], 403)
        self.assertEqual(self.request("GET", self.target(key=b"r" * 32))[0], 200)

    def test_malformed_permissive_symlinked_and_hardlinked_keys_fail_closed(self):
        self.store.save("one", "Title", "Body")
        self.key_path.parent.mkdir(mode=0o700)
        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(b"k" * 32)
        cases = []
        self.key_path.write_bytes(b"short")
        cases.append("short")
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(self.request("GET", self.target())[0], 503)
        self.key_path.write_bytes(b"k" * 32)
        self.key_path.chmod(0o644)
        self.assertEqual(self.request("GET", self.target())[0], 503)
        self.key_path.unlink()
        self.key_path.symlink_to(outside)
        self.assertEqual(self.request("GET", self.target())[0], 503)
        self.key_path.unlink()
        os.link(outside, self.key_path)
        self.key_path.chmod(0o600)
        self.assertEqual(self.request("GET", self.target())[0], 503)

    def test_expiry_signature_and_query_validation(self):
        self.store.save("one", "Title", "Body")
        self.initialize()
        valid_sig = self.target().split("sig=", 1)[1]
        malformed = (
            f"/draft/one?expires={self.NOW}&sig={valid_sig}",
            self.target(expires=self.NOW + 86401),
            f"/draft/one?expires=x&sig={valid_sig}",
            f"/draft/one?expires=999999999999999999999999&sig={valid_sig}",
            f"/draft/one?expires={self.NOW + 60}&sig=abc",
            f"/draft/one?expires={self.NOW + 60}&sig={valid_sig.upper()}",
            f"/draft/one?expires={self.NOW + 60}&sig={valid_sig}&sig={valid_sig}",
            f"/draft/one?expires={self.NOW + 60}&sig={valid_sig}&extra=x",
            f"/draft/one?expires={self.NOW + 60}",
            f"/draft/one?sig={valid_sig}",
            f"/draft/one?expires={self.NOW + 60}&sig={valid_sig}#fragment",
        )
        for target in malformed:
            with self.subTest(target=target):
                self.assertIn(self.request("GET", target)[0], (400, 403))

    def test_path_and_method_attacks_never_serve(self):
        self.store.save("one", "Title", "Body")
        self.initialize()
        attacks = (
            "/draft/%2fone", "/draft/one%2fsource.txt", "/draft/one%5csource",
            "/draft/%2e%2e", "/draft/..", "/draft/one/", "/draft/one.txt",
            "/draft/on%C3%A9", "/draft/one%00x", "/draft/one/source.txt",
            "/draft/one?expires%3d1&sig=" + "0" * 64,
            "/source.txt", "/publication.json", "/newsroom.json", "/draft/",
        )
        for target in attacks:
            with self.subTest(target=target):
                self.assertNotEqual(self.request("GET", target)[0], 200)
        for method in ("HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
            with self.subTest(method=method):
                status, headers, _body = self.request(method, self.target(), body=b"secret")
                self.assertEqual(status, 405)
                self.assert_security_headers(headers)

    def test_authentication_precedes_existence_read_and_prevents_oracle(self):
        self.store.save("exists", "Title", "Body")
        self.initialize()
        wrong = "0" * 64
        targets = [f"/draft/{draft_id}?expires={self.NOW + 60}&sig={wrong}"
                   for draft_id in ("exists", "absent")]
        responses = [self.request("GET", target) for target in targets]
        self.assertEqual([(r[0], r[2]) for r in responses],
                         [(403, b"forbidden\n"), (403, b"forbidden\n")])
        with mock.patch.object(self.store, "read_rendition") as read:
            self.request("GET", targets[0])
            read.assert_not_called()
        self.assertEqual(self.request("GET", self.target("absent"))[0], 404)

    def test_concurrent_requests_disconnect_and_oversized_input_do_not_stop_server(self):
        self.store.save("one", "Title", "Body")
        self.initialize()
        target = self.target()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.request("GET", target)[0]))
                   for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(results, [200] * 12)

        sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        sock.sendall(("GET " + "/" + "a" * 9000 + " HTTP/1.1\r\nHost: x\r\n\r\n").encode())
        sock.close()
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        sock.sendall(("GET /healthz HTTP/1.1\r\nX-Large: " + "x" * 70000 + "\r\n\r\n").encode())
        sock.close()
        self.assertEqual(self.request("GET", "/healthz")[0], 200)

    def test_worker_limit_and_absolute_deadline_reclaim_slow_drips(self):
        limited = self.module.build_server(
            ("127.0.0.1", 0), store=self.store, key_path=self.key_path,
            clock=lambda: self.NOW, worker_limit=3, connection_deadline=0.5,
        )
        thread = threading.Thread(target=limited.serve_forever, daemon=True)
        thread.start()
        sockets = []
        stop_drip = threading.Event()
        drippers = []
        try:
            port = limited.server_address[1]
            for _ in range(3):
                sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                sock.settimeout(1)
                sock.sendall(b"G")
                sockets.append(sock)
                def drip(connection=sock):
                    while not stop_drip.wait(0.03):
                        try:
                            connection.sendall(b"E")
                        except OSError:
                            return
                dripper = threading.Thread(target=drip, daemon=True)
                dripper.start()
                drippers.append(dripper)

            deadline = time.monotonic() + 2
            while limited.active_worker_count != 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(limited.active_worker_count, 3)
            rejected = []
            for _ in range(12):
                excess = socket.create_connection(("127.0.0.1", port), timeout=2)
                excess.settimeout(1)
                excess.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
                rejected.append(excess.recv(1))
                excess.close()
            self.assertEqual(rejected, [b""] * 12)
            self.assertLessEqual(limited.peak_active_worker_count, 3)

            deadline = time.monotonic() + 2
            while limited.active_worker_count and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(limited.active_worker_count, 0)
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            self.assertEqual((response.status, response.read()), (200, b"ok\n"))
            connection.close()
        finally:
            stop_drip.set()
            for sock in sockets:
                sock.close()
            for dripper in drippers:
                dripper.join(1)
            limited.shutdown()
            limited.server_close()
            thread.join(5)

    def test_worker_permits_survive_success_disconnect_and_exception_paths(self):
        limited = self.module.build_server(
            ("127.0.0.1", 0), store=self.store, key_path=self.key_path,
            clock=lambda: self.NOW, worker_limit=2, connection_deadline=1,
        )
        thread = threading.Thread(target=limited.serve_forever, daemon=True)
        thread.start()
        try:
            port = limited.server_address[1]
            for _ in range(8):
                clients = []
                for _index in range(2):
                    client = socket.create_connection(("127.0.0.1", port), timeout=2)
                    client.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
                    clients.append(client)
                for client in clients:
                    response = http.client.HTTPResponse(client)
                    response.begin()
                    self.assertEqual((response.status, response.read()), (200, b"ok\n"))
                    client.close()

            client = socket.create_connection(("127.0.0.1", port), timeout=2)
            client.sendall(b"GET /healthz HTTP/1.1\r\n")
            client.close()
            self.initialize()
            with mock.patch.object(self.store, "read_rendition", side_effect=RuntimeError("secret")):
                client = socket.create_connection(("127.0.0.1", port), timeout=2)
                client.sendall(("GET " + self.target() + " HTTP/1.1\r\nHost: x\r\n\r\n").encode())
                client.close()

            deadline = time.monotonic() + 2
            while limited.active_worker_count and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(limited.active_worker_count, 0)
            for _ in range(2):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                connection.close()
            self.assertLessEqual(limited.peak_active_worker_count, 2)
        finally:
            limited.shutdown()
            limited.server_close()
            thread.join(5)

    def test_logs_never_contain_targets_signatures_content_paths_or_tracebacks(self):
        self.store.save("private-id", "PRIVATE-TITLE", "PRIVATE-CONTENT")
        self.initialize()
        signed = self.target("private-id")
        capture = io.StringIO()
        with redirect_stdout(capture), redirect_stderr(capture):
            self.request("GET", signed.replace("sig=", "unknown=", 1))
            self.request("GET", signed[:-1] + ("0" if signed[-1] != "0" else "1"))
            self.request("GET", "/private/config/path")
        logged = capture.getvalue()
        self.assertEqual(logged, "")
        for secret in (signed, signed.split("sig=", 1)[1], "private-id",
                       "PRIVATE-TITLE", "PRIVATE-CONTENT", "Traceback"):
            self.assertNotIn(secret, logged)

    def test_port_validation(self):
        for value, expected in ((None, 8080), ("1", 1), ("65535", 65535), ("08080", 8080)):
            with self.subTest(value=value):
                environment = {} if value is None else {"PORT": value}
                self.assertEqual(self.module.resolve_port(environment), expected)
        for value in ("", "0", "65536", "-1", "+1", "1.0", " 80", "x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.module.resolve_port({"PORT": value})


if __name__ == "__main__":
    unittest.main()
