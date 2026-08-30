#!/usr/bin/env python3
"""Narrow authenticated client for Spacefast snapshot publication."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_ORIGIN = "https://api.spacefast.com"
PUBLISH_URL = API_ORIGIN + "/v1/publish"
TIMEOUT_SECONDS = 10
MAX_RENDITION_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TITLE_BYTES = 4096
MAX_TITLE_CHARACTERS = 255
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,254}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_STATUSES = frozenset({"active", "activated", "complete", "completed", "live",
                               "success", "succeeded"})


class SpacefastError(Exception):
    """A sanitized Spacefast failure represented only by a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"SpacefastError({self.code!r})"


class NoRedirectHandler(HTTPRedirectHandler):
    """Spacefast's direct publish operation documents no redirect requirement."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _credential(environ: Mapping[str, str], name: str, code: str,
                *, identifier: bool = False) -> str:
    value = environ.get(name)
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(character) < 33 or ord(character) == 127 for character in value)
            or (identifier and not IDENTIFIER_RE.fullmatch(value))):
        raise SpacefastError(code)
    return value


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise SpacefastError(code)
    return value


def _safe_url(value: Any) -> str:
    if (not isinstance(value, str) or value != value.strip() or not value.isascii()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise SpacefastError("spacefast_invalid_response")
    malformed = False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        malformed = True
        parsed = None
        port = None
    if malformed:
        raise SpacefastError("spacefast_invalid_response") from None
    assert parsed is not None
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (parsed.scheme != "https" or not hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or port not in (None, 443)
            or not (hostname == "spacefast.app" or hostname.endswith(".spacefast.app")
                    or hostname == "spacefast.com" or hostname.endswith(".spacefast.com"))):
        raise SpacefastError("spacefast_invalid_response")
    return value


def _strict_json(raw: bytes) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result
    try:
        document = json.loads(
            raw.decode("utf-8", "strict"), object_pairs_hook=unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
        )
        # Keep response validation well below the interpreter recursion limit.
        pending = [(document, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 64:
                raise ValueError("excessive nesting")
            if isinstance(value, dict):
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
        return document
    except (ValueError, UnicodeError, RecursionError, json.JSONDecodeError):
        raise SpacefastError("spacefast_invalid_response") from None


def _multipart(payload: dict[str, Any], rendition: bytes, digest: str) -> tuple[bytes, str]:
    boundary = "spacefast-snapshot-" + digest[:32]
    payload_bytes = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                               separators=(",", ":")).encode("ascii")
    body = b"".join((
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="payload"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        payload_bytes, b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="files"; filename="index.html"\r\n',
        b"Content-Type: text/html; charset=utf-8\r\n\r\n",
        rendition, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ))
    return body, "multipart/form-data; boundary=" + boundary


class SpacefastClient:
    def __init__(self, *, opener: Any = None,
                 environ: Mapping[str, str] | None = None) -> None:
        self._opener = build_opener(NoRedirectHandler()) if opener is None else opener
        self._environ = os.environ if environ is None else environ

    def validate_configuration(self) -> None:
        """Validate runtime credentials without retaining or exposing their values."""
        _credential(self._environ, "SPACEFAST_TOKEN", "spacefast_credentials_missing")
        _credential(self._environ, "SPACEFAST_TEAM_ID", "spacefast_team_invalid", identifier=True)

    def publish(self, title: str, rendition: bytes, rendition_sha256: str, *,
                draft_id: str = "draft", space_id: str | None = None) -> dict[str, str]:
        token = _credential(self._environ, "SPACEFAST_TOKEN",
                            "spacefast_credentials_missing")
        team_id = _credential(self._environ, "SPACEFAST_TEAM_ID",
                              "spacefast_team_invalid", identifier=True)
        if not isinstance(title, str):
            raise SpacefastError("spacefast_invalid_request")
        try:
            title_bytes = title.encode("utf-8", "strict")
        except UnicodeError as error:
            raise SpacefastError("spacefast_invalid_request") from error
        if (not title_bytes or len(title) > MAX_TITLE_CHARACTERS
                or len(title_bytes) > MAX_TITLE_BYTES or "\x00" in title
                or not isinstance(rendition, bytes) or not rendition
                or len(rendition) > MAX_RENDITION_BYTES
                or not isinstance(rendition_sha256, str)
                or not DIGEST_RE.fullmatch(rendition_sha256)
                or hashlib.sha256(rendition).hexdigest() != rendition_sha256):
            raise SpacefastError("spacefast_invalid_request")
        target = "new"
        if space_id is not None:
            space_id = _identifier(space_id, "spacefast_invalid_space_id")
            target = space_id
        if not isinstance(draft_id, str) or not draft_id or len(draft_id) > 128:
            raise SpacefastError("spacefast_invalid_request")

        payload: dict[str, Any] = {
            "channel": "live",
            "config": {"spa": False},
            "publishMode": "snapshot",
            "space": {"title": title},
            "teamId": team_id,
        }
        if space_id is not None:
            payload["spaceId"] = space_id
        body, content_type = _multipart(payload, rendition, rendition_sha256)
        operation = hashlib.sha256(
            ("v1\n" + draft_id + "\n" + target + "\n" + rendition_sha256).encode("utf-8")
        ).hexdigest()
        request = Request(PUBLISH_URL, data=body, method="POST", headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + token,
            "Content-Type": content_type,
            "Idempotency-Key": "hermes-draft-v1-" + operation,
        })
        try:
            with self._opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                if response.geturl() != PUBLISH_URL:
                    raise SpacefastError("spacefast_redirect_refused")
                if response.status != 201:
                    raise SpacefastError("spacefast_http_error")
                content_header = response.headers.get("Content-Type", "")
                if content_header.split(";", 1)[0].strip().lower() != "application/json":
                    raise SpacefastError("spacefast_wrong_content_type")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise SpacefastError("spacefast_response_too_large")
        except SpacefastError:
            raise
        except HTTPError as error:
            code = "spacefast_redirect_refused" if 300 <= error.code < 400 else "spacefast_http_error"
            raise SpacefastError(code) from None
        except (TimeoutError, socket.timeout):
            raise SpacefastError("spacefast_timeout") from None
        except (URLError, OSError):
            raise SpacefastError("spacefast_network_error") from None
        except Exception:
            raise SpacefastError("spacefast_transport_error") from None

        try:
            document = _strict_json(raw)
            data = document["data"]
            returned_space = data["space"]
            version = data["version"]
            returned_id = _identifier(returned_space["id"], "spacefast_invalid_response")
            _identifier(version["id"], "spacefast_invalid_response")
            live_url = _safe_url(returned_space["liveUrl"])
            immutable_url = _safe_url(version["immutableUrl"])
            activation = data.get("activation")
            next_step = data.get("next")
            terminal = (isinstance(activation, dict)
                        and isinstance(activation.get("status"), str)
                        and activation["status"].lower() in TERMINAL_STATUSES
                        and next_step is None)
            if not terminal or (space_id is not None and returned_id != space_id):
                raise SpacefastError("spacefast_invalid_response")
        except SpacefastError:
            raise
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise SpacefastError("spacefast_invalid_response") from None
        return {"space_id": returned_id, "live_url": live_url,
                "immutable_url": immutable_url}
