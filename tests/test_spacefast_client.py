import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import socket
import sys
import traceback
import unittest
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/draft-publishing/scripts/spacefast_client.py"


def load_module():
    spec = importlib.util.spec_from_file_location("spacefast_client_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError("Spacefast client module is absent")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


class FakeResponse:
    def __init__(self, body, *, status=201, content_type="application/json", url=None):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._url = url or "https://api.spacefast.com/v1/publish"

    def read(self, size=-1):
        return self._body.read(size) if hasattr(self._body, "read") else self._read_bytes(size)

    def _read_bytes(self, size):
        data = self._body
        if size < 0:
            self._body = b""
            return data
        result, self._body = data[:size], data[size:]
        return result

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


class SpacefastClientTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.token = "TOKEN-SECRET-MARKER"
        self.team = "team_0123456789abcdef"
        self.space = "spc_0123456789abcdef"
        self.success = {
            "data": {
                "space": {"id": self.space, "liveUrl": "https://news.spacefast.app/"},
                "version": {"id": "ver_0123456789", "immutableUrl": "https://ver-1.spacefast.app/"},
                "activation": {"status": "succeeded"},
                "next": None,
                "access": {"token": "MUST-NOT-ESCAPE"},
            }
        }

    def client(self, opener, environ=None):
        return self.module.SpacefastClient(
            opener=opener,
            environ={"SPACEFAST_TOKEN": self.token, "SPACEFAST_TEAM_ID": self.team}
            if environ is None else environ,
        )

    @staticmethod
    def digest(content):
        return hashlib.sha256(content).hexdigest()

    def test_credentials_are_required_and_validated_before_transport(self):
        bad = ({}, {"SPACEFAST_TOKEN": "", "SPACEFAST_TEAM_ID": self.team},
               {"SPACEFAST_TOKEN": "x", "SPACEFAST_TEAM_ID": ""},
               {"SPACEFAST_TOKEN": "x", "SPACEFAST_TEAM_ID": "bad/team"},
               {"SPACEFAST_TOKEN": " x", "SPACEFAST_TEAM_ID": self.team})
        for environ in bad:
            opener = FakeOpener()
            with self.subTest(environ=environ), self.assertRaises(self.module.SpacefastError):
                self.client(opener, environ).publish("Title", b"<html>safe</html>", "a" * 64)
            self.assertEqual(opener.calls, [])

    def test_create_is_authenticated_constant_origin_snapshot_and_minimal_multipart(self):
        response = FakeResponse(json.dumps(self.success).encode())
        opener = FakeOpener(response)
        rendition = b"<!doctype html>PUBLIC RENDITION"
        result = self.client(opener).publish("Safe title", rendition, self.digest(rendition))
        self.assertEqual(result, {
            "space_id": self.space,
            "live_url": "https://news.spacefast.app/",
            "immutable_url": "https://ver-1.spacefast.app/",
        })
        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, "https://api.spacefast.com/v1/publish")
        self.assertEqual(request.method, "POST")
        self.assertGreater(timeout, 0)
        self.assertTrue(math.isfinite(timeout))
        self.assertLessEqual(timeout, 10)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["authorization"], "Bearer " + self.token)
        self.assertLessEqual(len(headers["idempotency-key"]), 255)
        body = request.data
        self.assertIn(b'"publishMode":"snapshot"', body)
        self.assertIn(('"teamId":"%s"' % self.team).encode(), body)
        self.assertIn(b'"title":"Safe title"', body)
        self.assertIn(b'filename="index.html"', body)
        self.assertIn(b'name="files"', body)
        self.assertIn(b"PUBLIC RENDITION", body)
        for forbidden in (b"source.txt", b"rendition.html", b"publication.json", b"newsroom.json",
                          b"sources.json", b"/opt/data", b"HMAC", b"signature", b"MUST-NOT-ESCAPE"):
            self.assertNotIn(forbidden, body)
        self.assertNotIn(self.token.encode(), body)

    def test_update_reuses_exact_space_id_and_idempotency_is_deterministic(self):
        first = FakeOpener(FakeResponse(json.dumps(self.success).encode()))
        self.client(first).publish("Title", b"HTML", self.digest(b"HTML"), space_id=self.space,
                                   draft_id="story-1")
        request = first.calls[0][0]
        self.assertIn(('"spaceId":"%s"' % self.space).encode(), request.data)
        key = dict((k.lower(), v) for k, v in request.header_items())["idempotency-key"]
        retry = FakeOpener(FakeResponse(json.dumps(self.success).encode()))
        self.client(retry).publish("Title", b"HTML", self.digest(b"HTML"), space_id=self.space,
                                   draft_id="story-1")
        self.assertEqual(dict((k.lower(), v) for k, v in retry.calls[0][0].header_items())["idempotency-key"], key)
        create = FakeOpener(FakeResponse(json.dumps(self.success).encode()))
        self.client(create).publish("Title", b"HTML", self.digest(b"HTML"), draft_id="story-1")
        self.assertNotEqual(dict((k.lower(), v) for k, v in create.calls[0][0].header_items())["idempotency-key"], key)

    def test_response_id_mismatch_and_malformed_terminal_shape_are_rejected(self):
        cases = []
        mismatch = json.loads(json.dumps(self.success))
        mismatch["data"]["space"]["id"] = "spc_some_other_space"
        cases.append(mismatch)
        pending = json.loads(json.dumps(self.success))
        pending["data"]["activation"] = {"status": "pending"}
        pending["data"]["next"] = {"poll": True}
        cases.append(pending)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(self.module.SpacefastError):
                self.client(FakeOpener(FakeResponse(json.dumps(payload).encode()))).publish(
                    "Title", b"HTML", self.digest(b"HTML"), space_id=self.space)

    def test_terminal_response_query_urls_are_rejected_without_secret_exposure(self):
        secret = "QUERY-SECRET-MARKER"
        for container, field in (("space", "liveUrl"), ("version", "immutableUrl")):
            payload = json.loads(json.dumps(self.success))
            payload["data"][container][field] += "?access_token=" + secret
            opener = FakeOpener(FakeResponse(json.dumps(payload).encode(), status=201))
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                        self.module.SpacefastError, "^spacefast_invalid_response$") as raised:
                    self.client(opener).publish("Title", b"HTML", self.digest(b"HTML"))
                public_error = str(raised.exception) + repr(raised.exception)
                self.assertNotIn(secret, public_error)
                self.assertNotIn("access_token", public_error)

    def test_malformed_ports_and_ascii_control_urls_have_no_secret_error_chain(self):
        secret = "URL-SECRET-MARKER"
        urls = (
            f"https://news.spacefast.app:{secret}/",
            f"https://news.spacefast.app/path\x00{secret}",
            f"https://news.spacefast.app/path\x1f{secret}",
            f"https://news.spacefast.app/path\x7f{secret}",
        )
        for url in urls:
            payload = json.loads(json.dumps(self.success))
            payload["data"]["space"]["liveUrl"] = url
            with self.subTest(url=repr(url)), self.assertRaisesRegex(
                    self.module.SpacefastError, "^spacefast_invalid_response$") as raised:
                self.client(FakeOpener(FakeResponse(json.dumps(payload).encode()))).publish(
                    "Title", b"HTML", self.digest(b"HTML"))
            error = raised.exception
            exposed = str(error) + repr(error) + repr(error.__cause__) + repr(error.__context__)
            exposed += "".join(traceback.format_exception(error))
            self.assertNotIn(secret, exposed)
            self.assertIsNone(error.__cause__)

    def test_json_is_strict_bounded_and_sanitized(self):
        secret = "JSON-SECRET-MARKER"
        valid = json.dumps(self.success).encode()
        malformed = (
            b'{"data":{},"data":{"secret":"' + secret.encode() + b'"}}',
            b'{"data":{"number":NaN,"secret":"' + secret.encode() + b'"}}',
            (b'[' * 5000) + b'"' + secret.encode() + b'"' + (b']' * 5000),
            valid[:-1] + b',"leak":"' + secret.encode() + b'" trailing}',
        )
        for raw in malformed:
            with self.subTest(length=len(raw)), self.assertRaisesRegex(
                    self.module.SpacefastError, "^spacefast_invalid_response$") as raised:
                self.client(FakeOpener(FakeResponse(raw))).publish(
                    "Title", b"HTML", self.digest(b"HTML"))
            error = raised.exception
            exposed = str(error) + repr(error) + repr(error.__cause__) + repr(error.__context__)
            exposed += "".join(traceback.format_exception(error))
            self.assertNotIn(secret, exposed)
            self.assertIsNone(error.__cause__)

    def test_network_http_redirect_content_json_size_and_url_failures_are_sanitized(self):
        errors_and_responses = [
            FakeOpener(error=socket.timeout("TOKEN-SECRET-MARKER")),
            FakeOpener(error=URLError("TOKEN-SECRET-MARKER")),
            FakeOpener(error=HTTPError("https://api.spacefast.com/v1/publish", 500,
                                       "TOKEN-SECRET-MARKER", {}, io.BytesIO(b"secret body"))),
            FakeOpener(FakeResponse(b"{}", status=302, url="http://evil.test/")),
            FakeOpener(FakeResponse(b"{}", content_type="text/html")),
            FakeOpener(FakeResponse(b"not-json")),
            FakeOpener(FakeResponse(b"x" * (1024 * 1024 + 1))),
            FakeOpener(FakeResponse(json.dumps(self.success).encode(), url="https://evil.test/publish")),
        ]
        for opener in errors_and_responses:
            with self.subTest(opener=opener), self.assertRaises(self.module.SpacefastError) as raised:
                self.client(opener).publish("Title", b"HTML", self.digest(b"HTML"))
            public = str(raised.exception) + repr(raised.exception)
            self.assertNotIn(self.token, public)
            self.assertNotIn("secret body", public)
            self.assertNotIn("evil.test", public)

    def test_request_and_title_are_bounded_before_transport(self):
        for title, rendition in (("x" * 256, b"ok"), ("é" * 256, b"ok"),
                                 ("x" * 5000, b"ok"),
                                 ("ok", b"x" * (8 * 1024 * 1024 + 1))):
            opener = FakeOpener()
            with self.subTest(title=len(title), rendition=len(rendition)), self.assertRaises(self.module.SpacefastError):
                self.client(opener).publish(title, rendition, "a" * 64)
            self.assertEqual(opener.calls, [])

        opener = FakeOpener(FakeResponse(json.dumps(self.success).encode()))
        rendition = b"ok"
        self.client(opener).publish("é" * 255, rendition, self.digest(rendition))
        self.assertEqual(len(opener.calls), 1)

    def test_default_opener_has_a_redirect_blocker(self):
        client = self.module.SpacefastClient(environ={
            "SPACEFAST_TOKEN": "x", "SPACEFAST_TEAM_ID": self.team})
        self.assertTrue(any(isinstance(handler, self.module.NoRedirectHandler)
                            for handler in client._opener.handlers))


if __name__ == "__main__":
    unittest.main()
