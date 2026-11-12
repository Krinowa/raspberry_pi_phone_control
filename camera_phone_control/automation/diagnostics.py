"""Bounded, redacted application logs for the authenticated diagnostics UI."""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone

from diagnostic_logging import sanitize_diagnostic_fields, sanitize_text


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
        with self._lock:
            secrets = tuple(self._secrets)
        return sanitize_text(value, secrets=secrets)

    def emit(self, record):
        try:
            message = record.getMessage()
            fields = getattr(record, "diagnostic_fields", {})
            if fields:
                with self._lock:
                    secrets = tuple(self._secrets)
                fields = sanitize_diagnostic_fields(fields, secrets=secrets)
                context = " ".join(
                    f"{key}={value}"
                    for key, value in fields.items()
                    if value is not None
                )
                message = f"{context} {message}".strip()
            entry = {
                "timestamp": datetime.fromtimestamp(
                    record.created, timezone.utc
                ).isoformat(timespec="seconds"),
                "level": record.levelname,
                "logger": record.name,
                "message": self.redact(message),
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
