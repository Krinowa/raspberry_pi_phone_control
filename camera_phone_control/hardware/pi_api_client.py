"""One HTTP boundary for the existing Pi API. Physical input is never retried."""

import http.client
import json
import math
import os
import re
import socket
from urllib.parse import urlsplit

from automation.coordinates import unit

DEFAULT_BASE_URL = "http://192.168.68.61:8766"
MAX_RESPONSE_BYTES = 65536


class PiApiError(RuntimeError):
    """Safe-to-display Pi error: never includes typed text or response bodies."""


def timeout_value(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive finite number.") from None

    if isinstance(value, bool) or not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive finite number.")
    return result


def validate_text(text):
    if not isinstance(text, str) or not 1 <= len(text) <= 4096:
        raise ValueError("Text must contain 1–4096 characters.")

    if any(not " " <= char <= "~" for char in text):
        raise ValueError("Pi text input currently supports printable ASCII characters.")


class PiApiClient:
    def __init__(self, base_url=None, connect_timeout=None, read_timeout=None):
        base_url = (
            base_url
            if base_url is not None
            else os.environ.get("PI_API_BASE_URL", DEFAULT_BASE_URL)
        )

        if not isinstance(base_url, str):
            raise ValueError("PI_API_BASE_URL must be an HTTP(S) URL.")
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except ValueError:
            raise ValueError("PI_API_BASE_URL is invalid.") from None

        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "PI_API_BASE_URL must be an HTTP(S) origin without credentials or a path."
            )

        self.base_url = f"{parsed.scheme}://{parsed.netloc}".lower()
        self.host, self.port, self.scheme = parsed.hostname, port, parsed.scheme
        self.connect_timeout = timeout_value(
            (
                connect_timeout
                if connect_timeout is not None
                else os.environ.get("PI_API_CONNECT_TIMEOUT", "2")
            ),
            "PI_API_CONNECT_TIMEOUT",
        )

        self.read_timeout = timeout_value(
            (
                read_timeout
                if read_timeout is not None
                else os.environ.get("PI_API_READ_TIMEOUT", "5")
            ),
            "PI_API_READ_TIMEOUT",
        )

    def _request(self, method, path, payload=None):
        connection_type = (
            http.client.HTTPSConnection
            if self.scheme == "https"
            else http.client.HTTPConnection
        )

        connection = connection_type(self.host, self.port, timeout=self.connect_timeout)
        suffix = (
            " Input may have occurred; no retry attempted." if method == "POST" else ""
        )

        try:
            # Separate connect and read timeouts; no proxy, redirect, or retry layer.
            connection.connect()
            connection.sock.settimeout(self.read_timeout)
            body = (
                json.dumps(payload, allow_nan=False).encode("utf-8")
                if payload is not None
                else None
            )
            headers = {"Accept": "application/json"}

            if body is not None:
                headers["Content-Type"] = "application/json"

            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()

            if not 200 <= response.status < 300:
                raise PiApiError(
                    f"Pi API {path} returned HTTP {response.status}." + suffix
                )

            raw = response.read(MAX_RESPONSE_BYTES + 1)

            if len(raw) > MAX_RESPONSE_BYTES:
                raise PiApiError("Pi API response exceeded the size limit." + suffix)
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                raise PiApiError(
                    f"Pi API {path} returned invalid JSON." + suffix
                ) from None

            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise PiApiError(
                    f"Pi API {path} returned an invalid response (expected boolean ok)."
                    + suffix
                )

            if not result["ok"]:
                # Error bodies can echo /text payloads. Classify useful failures,
                # but never copy arbitrary Pi response text into UI or logs.
                error = result.get("error", "")
                detail = " Check the Pi service and phone connection."
                if isinstance(error, str) and "connect" in error.lower():
                    detail = " Check the Bluetooth HID connection to the phone."
                raise PiApiError(f"Pi rejected {path}." + detail + suffix)
            return result
        except (socket.timeout, TimeoutError):
            raise PiApiError(f"Pi API {path} timed out." + suffix) from None
        except (OSError, http.client.HTTPException):
            raise PiApiError(
                f"Cannot reach Pi API for {path}. Check the Pi address and private Wi-Fi."
                + suffix
            ) from None
        finally:
            connection.close()

    def health(self):
        return self._request("GET", "/health")

    def status(self):
        result = self._request("GET", "/status")
        if type(result.get("connected")) is not bool:
            raise PiApiError("Pi /status response is missing boolean connected.")
        return result

    def tap(self, x, y):
        return self._request("POST", "/tap", {"x": unit(x), "y": unit(y)})

    def swipe(self, x1, y1, x2, y2):
        return self._request(
            "POST",
            "/swipe",
            {
                "x1": unit(x1),
                "y1": unit(y1),
                "x2": unit(x2),
                "y2": unit(y2),
            },
        )

    def type_text(self, text):
        validate_text(text)
        return self._request("POST", "/text", {"text": text})

    def key(self, key):
        if not isinstance(key, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{0,31}", key
        ):
            raise ValueError("A named keyboard key is required.")
        return self._request("POST", "/key", {"key": key.upper()})

    def home(self):
        return self._request("POST", "/home")

    def back(self):
        return self._request("POST", "/back")

    def wake(self):
        return self._request("POST", "/wake")
