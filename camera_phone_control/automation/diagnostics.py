"""Bounded, redacted application logs for the authenticated diagnostics UI."""

from __future__ import annotations

import logging
import os
import re
import threading
from collections import deque
from datetime import datetime, timezone

_SECRET_FIELD = re.compile(
    r"(?i)\b(password|passwd|token|secret|totp|authorization|credential|api[_ -]?key|control[_ -]?token)"
    r"([\"']?\s*[:=]\s*)([\"']?)([^\s,;&\"']+)\3"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_LONG_NUMBER = re.compile(r"(?<!\d)\d{12,19}(?!\d)")


class RecentLogHandler(logging.Handler):
    """Keep only recent formatted records and never retain known secrets."""

    def __init__(self, capacity=500):
        super().__init__(logging.INFO)
        self._entries = deque(maxlen=capacity)
        self._secrets = set()
        self._lock = threading.RLock()

    def add_secret(self, value):
        if isinstance(value, str) and len(value) >= 4:
            with self._lock:
                self._secrets.add(value)

    def redact(self, value):
        message = str(value)
        message = _BEARER.sub("Bearer <redacted>", message)
        message = _SECRET_FIELD.sub(
            lambda match: (
                f"{match.group(1)}{match.group(2)}{match.group(3)}"
                f"<redacted>{match.group(3)}"
            ),
            message,
        )
        message = message.replace("<redacted> <redacted>", "<redacted>")
        message = _LONG_NUMBER.sub("<redacted-number>", message)
        with self._lock:
            secrets = tuple(self._secrets)
        for secret in secrets:
            message = message.replace(secret, "<redacted>")
        return message

    def emit(self, record):
        try:
            entry = {
                "timestamp": datetime.fromtimestamp(
                    record.created, timezone.utc
                ).isoformat(timespec="seconds"),
                "level": record.levelname,
                "logger": record.name,
                "message": self.redact(record.getMessage()),
            }
            with self._lock:
                self._entries.append(entry)
        except Exception:
            self.handleError(record)

    def recent(self, *, limit=300, query=""):
        query = query.strip().casefold()
        with self._lock:
            entries = list(self._entries)
        if query:
            entries = [
                entry
                for entry in entries
                if query
                in " ".join(
                    (
                        entry["timestamp"],
                        entry["level"],
                        entry["logger"],
                        entry["message"],
                    )
                ).casefold()
            ]
        return entries[-limit:]


RECENT_LOGS = RecentLogHandler()


def install_recent_log_handler(*, secrets=()):
    """Install one process-wide handler without changing normal log output."""
    root = logging.getLogger()
    if RECENT_LOGS not in root.handlers:
        root.addHandler(RECENT_LOGS)
    for value in secrets:
        RECENT_LOGS.add_secret(value)
    for name, value in os.environ.items():
        if any(
            part in name.casefold()
            for part in ("password", "token", "secret", "credential", "api_key")
        ):
            RECENT_LOGS.add_secret(value)
    return RECENT_LOGS
