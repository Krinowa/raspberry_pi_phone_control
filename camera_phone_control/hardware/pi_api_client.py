"""One HTTP boundary for the existing Pi API. Physical input is never retried."""

import http.client
import json
import math
import os
import re
import socket
import time
from urllib.parse import urlsplit

from automation.coordinates import unit

DEFAULT_BASE_URL = "http://192.168.68.61:8766"
MAX_RESPONSE_BYTES = 65536

# Only log "slow" requests above this threshold.
SLOW_REQUEST_SECONDS = float(os.environ.get("PI_API_SLOW_REQUEST_SECONDS", "0.5"))


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
    # Class attributes keep these observable on mocks/specs while each real
    # client receives its own values below.
    last_command_id = None
    last_action_status = None
    last_action_http_status = None

    def __init__(
        self,
        base_url=None,
        connect_timeout=None,
        read_timeout=None,
    ):
        base_url = (
            base_url
            if base_url is not None
            else os.environ.get(
                "PI_API_BASE_URL",
                DEFAULT_BASE_URL,
            )
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
                "PI_API_BASE_URL must be an HTTP(S) origin "
                "without credentials or a path."
            )

        self.base_url = f"{parsed.scheme}://{parsed.netloc}".lower()

        self.host = parsed.hostname
        self.port = port
        self.scheme = parsed.scheme

        self.connect_timeout = timeout_value(
            (
                connect_timeout
                if connect_timeout is not None
                else os.environ.get(
                    "PI_API_CONNECT_TIMEOUT",
                    "2",
                )
            ),
            "PI_API_CONNECT_TIMEOUT",
        )

        self.read_timeout = timeout_value(
            (
                read_timeout
                if read_timeout is not None
                else os.environ.get(
                    "PI_API_READ_TIMEOUT",
                    "5",
                )
            ),
            "PI_API_READ_TIMEOUT",
        )

        self.last_command_id = None
        self.last_action_status = None
        self.last_action_http_status = None

    def _retain_action_metadata(
        self,
        result,
        http_status,
    ):
        """Remember safe metadata from the most recent accepted input action."""

        self.last_command_id = None
        self.last_action_status = None
        self.last_action_http_status = http_status

        command_id = result.get("command_id")
        status = result.get("status")

        if (
            isinstance(command_id, str)
            and 1 <= len(command_id) <= 256
            and all(ord(char) >= 32 and ord(char) != 127 for char in command_id)
        ):
            self.last_command_id = command_id

        self.last_action_status = status if isinstance(status, str) else None

    def _request(
        self,
        method,
        path,
        payload=None,
    ):
        connection_type = (
            http.client.HTTPSConnection
            if self.scheme == "https"
            else http.client.HTTPConnection
        )

        connection = connection_type(
            self.host,
            self.port,
            timeout=self.connect_timeout,
        )

        suffix = (
            " Input may have occurred; no retry attempted." if method == "POST" else ""
        )

        started = time.monotonic()
        phase = "connect"

        try:
            # -----------------------------------------------------------------
            # Connect
            # -----------------------------------------------------------------

            connection.connect()

            connected_at = time.monotonic()
            connect_elapsed = connected_at - started

            # After TCP connect, use the longer read timeout.
            if connection.sock is None:
                raise PiApiError(f"Pi API {path} connected without a socket." + suffix)

            connection.sock.settimeout(self.read_timeout)

            # -----------------------------------------------------------------
            # Build request
            # -----------------------------------------------------------------

            body = (
                json.dumps(
                    payload,
                    allow_nan=False,
                ).encode("utf-8")
                if payload is not None
                else None
            )

            headers = {
                "Accept": "application/json",
            }

            if body is not None:
                headers["Content-Type"] = "application/json"

            # -----------------------------------------------------------------
            # Send request
            # -----------------------------------------------------------------

            phase = "request"

            connection.request(
                method,
                path,
                body=body,
                headers=headers,
            )

            request_sent_at = time.monotonic()

            # -----------------------------------------------------------------
            # Wait for HTTP response headers
            # -----------------------------------------------------------------

            phase = "response"

            response = connection.getresponse()

            response_at = time.monotonic()

            response_wait = response_at - request_sent_at

            # -----------------------------------------------------------------
            # Check HTTP status
            # -----------------------------------------------------------------

            if not 200 <= response.status < 300:
                total_elapsed = time.monotonic() - started

                print(
                    f"[pi-api] HTTP ERROR "
                    f"{method} {path} "
                    f"status={response.status} "
                    f"elapsed={total_elapsed:.3f}s",
                    flush=True,
                )

                raise PiApiError(
                    f"Pi API {path} returned " f"HTTP {response.status}." + suffix
                )

            # -----------------------------------------------------------------
            # Read response body
            # -----------------------------------------------------------------

            phase = "body"

            raw = response.read(MAX_RESPONSE_BYTES + 1)

            body_finished_at = time.monotonic()

            body_elapsed = body_finished_at - response_at

            total_elapsed = body_finished_at - started

            # -----------------------------------------------------------------
            # Timing diagnostics
            # -----------------------------------------------------------------

            if total_elapsed >= SLOW_REQUEST_SECONDS:
                print(
                    f"[pi-api] SLOW "
                    f"{method} {path} "
                    f"status={response.status} "
                    f"total={total_elapsed:.3f}s "
                    f"connect={connect_elapsed:.3f}s "
                    f"response_wait={response_wait:.3f}s "
                    f"body={body_elapsed:.3f}s "
                    f"connect_timeout={self.connect_timeout:.3f}s "
                    f"read_timeout={self.read_timeout:.3f}s",
                    flush=True,
                )

            if len(raw) > MAX_RESPONSE_BYTES:
                raise PiApiError("Pi API response exceeded " "the size limit." + suffix)

            # -----------------------------------------------------------------
            # Parse JSON
            # -----------------------------------------------------------------

            try:
                result = json.loads(raw)
            except (
                ValueError,
                UnicodeError,
            ):
                raise PiApiError(
                    f"Pi API {path} returned " "invalid JSON." + suffix
                ) from None

            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise PiApiError(
                    f"Pi API {path} returned "
                    "an invalid response "
                    "(expected boolean ok)." + suffix
                )

            # -----------------------------------------------------------------
            # Pi application-level failure
            # -----------------------------------------------------------------

            if not result["ok"]:
                # Error bodies can echo /text payloads.
                # Classify useful failures, but never copy
                # arbitrary Pi response text into UI/logs.

                error = result.get(
                    "error",
                    "",
                )

                detail = " Check the Pi service " "and phone connection."

                if isinstance(error, str) and "connect" in error.lower():
                    detail = " Check the Bluetooth " "HID connection to " "the phone."

                raise PiApiError(f"Pi rejected {path}." + detail + suffix)

            # -----------------------------------------------------------------
            # Retain async metadata
            # -----------------------------------------------------------------

            if method == "POST":
                self._retain_action_metadata(
                    result,
                    response.status,
                )

            return result

        except (
            socket.timeout,
            TimeoutError,
        ):
            elapsed = time.monotonic() - started

            print(
                f"[pi-api] TIMEOUT "
                f"{method} {path} "
                f"phase={phase} "
                f"elapsed={elapsed:.3f}s "
                f"connect_timeout="
                f"{self.connect_timeout:.3f}s "
                f"read_timeout="
                f"{self.read_timeout:.3f}s",
                flush=True,
            )

            raise PiApiError(f"Pi API {path} timed out." + suffix) from None

        except PiApiError:
            # Preserve already-sanitized Pi API errors.
            raise

        except (
            OSError,
            http.client.HTTPException,
        ) as exc:
            elapsed = time.monotonic() - started

            print(
                f"[pi-api] ERROR "
                f"{method} {path} "
                f"phase={phase} "
                f"elapsed={elapsed:.3f}s "
                f"type={type(exc).__name__} "
                f"error={exc}",
                flush=True,
            )

            raise PiApiError(
                f"Cannot reach Pi API "
                f"for {path}. "
                "Check the Pi address "
                "and private Wi-Fi." + suffix
            ) from None

        finally:
            connection.close()

    def health(self):
        return self._request(
            "GET",
            "/health",
        )

    def status(self):
        result = self._request(
            "GET",
            "/status",
        )

        if type(result.get("connected")) is not bool:
            raise PiApiError("Pi /status response " "is missing boolean connected.")

        return result

    def tap(
        self,
        x,
        y,
    ):
        return self._request(
            "POST",
            "/tap",
            {
                "x": unit(x),
                "y": unit(y),
            },
        )

    def swipe(
        self,
        x1,
        y1,
        x2,
        y2,
    ):
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

    def type_text(
        self,
        text,
    ):
        validate_text(text)

        return self._request(
            "POST",
            "/text",
            {
                "text": text,
            },
        )

    def key(
        self,
        key,
    ):
        if not isinstance(key, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{0,31}",
            key,
        ):
            raise ValueError("A named keyboard key " "is required.")

        return self._request(
            "POST",
            "/key",
            {
                "key": key.upper(),
            },
        )

    def home(self):
        return self._request(
            "POST",
            "/home",
        )

    def back(self):
        return self._request(
            "POST",
            "/back",
        )

    def wake(self):
        return self._request(
            "POST",
            "/wake",
        )
