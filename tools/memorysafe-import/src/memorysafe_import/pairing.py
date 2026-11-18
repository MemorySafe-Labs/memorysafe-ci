"""The client for the server's `/import-pairing/*` endpoints (stdlib only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import __version__

DEFAULT_SERVER = "https://memory.memorysafe.ca"
TIMEOUT = 30
UPLOAD_TIMEOUT = 300  # the body is up to 10 MB, read and checked before the server answers


class PairingError(Exception):
    """A refusal or a failure; `str(e)` is the sentence to show, `.code` the server's code."""

    def __init__(self, message: str, code: str = "error", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status  # the HTTP status, 0 when there was no answer


class NotSupported(PairingError):
    """The server answered 404 on start: it predates code import."""


@dataclass
class Started:
    device_secret: str
    code: str
    link_url: str
    expires_in: int


class Client:
    def __init__(self, server: str = DEFAULT_SERVER, *, urlopen: Callable[..., Any] = urllib.request.urlopen):
        self.server = server.rstrip("/")
        self._urlopen = urlopen

    def _call(self, method: str, path: str, secret: Optional[str] = None, body: Optional[dict] = None,
              timeout: int = TIMEOUT) -> dict:
        headers = {"User-Agent": f"memorysafe-import/{__version__}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if secret is not None:
            headers["Authorization"] = f"Bearer {secret}"
        request = urllib.request.Request(self.server + path, data=data, headers=headers, method=method)
        try:
            response = self._urlopen(request, timeout=timeout)
            try:
                raw = response.read()
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
        except urllib.error.HTTPError as error:
            try:
                raw = error.read()
            except OSError:
                raw = b""
            code, message = "http_%d" % error.code, "The server answered HTTP %d." % error.code
            try:
                detail = json.loads(raw.decode("utf-8"))["error"]
                code, message = str(detail["code"]), str(detail["message"])
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                pass
            if error.code == 404 and path == "/import-pairing/start":
                raise NotSupported(message, code, error.code) from None
            raise PairingError(message, code, error.code) from None
        except (urllib.error.URLError, OSError) as error:
            raise PairingError(f"Could not reach {self.server}: {getattr(error, 'reason', error)}", "network") from None
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            raise PairingError("The server sent an answer this tool cannot read.", "bad_response") from None
        if not isinstance(parsed, dict):
            raise PairingError("The server sent an answer this tool cannot read.", "bad_response")
        return parsed

    def start(self, host_label: str, declared: int) -> Started:
        data = self._call("POST", "/import-pairing/start", body={"host_label": host_label, "declared": declared})
        try:
            return Started(str(data["device_secret"]), str(data["code"]), str(data["link_url"]), int(data["expires_in"]))
        except (KeyError, TypeError, ValueError):
            raise PairingError("The server sent an answer this tool cannot read.", "bad_response") from None

    def poll(self, secret: str) -> "tuple[str, Optional[str]]":
        data = self._call("POST", "/import-pairing/poll", secret, {})
        return str(data.get("status", "")), data.get("account")

    def upload(self, secret: str, export: dict) -> str:
        data = self._call("POST", "/import-pairing/upload", secret, export, timeout=UPLOAD_TIMEOUT)
        inner = data.get("import")
        found = data.get("import_id") or (inner.get("id") if isinstance(inner, dict) else None)
        return str(found or "")

    def progress(self, secret: str) -> dict:
        data = self._call("GET", "/import-pairing/import", secret)
        inner = data.get("import")
        return inner if isinstance(inner, dict) else {}

    def cancel(self, secret: str) -> None:
        self._call("POST", "/import-pairing/cancel", secret, {})
