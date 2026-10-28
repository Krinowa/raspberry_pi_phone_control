"""Bounded, redacted, structured diagnostic logging for production support."""

from __future__ import annotations

import contextlib
import contextvars
import copy
import importlib.metadata
import logging
import logging.handlers
import multiprocessing
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

PRIMARY_LOG_NAME = "phone-controller.log"
STARTUP_LOG_NAME = "startup-error.log"
PRIMARY_MAX_BYTES = 10 * 1024 * 1024
PRIMARY_BACKUP_COUNT = 10
STARTUP_MAX_BYTES = 1024 * 1024
STARTUP_BACKUP_COUNT = 2
ROTATED_RETENTION_DAYS = 14

_CONTEXT = contextvars.ContextVar("phone_diagnostic_context", default={})
_SESSION_ID = None
_APP_VERSION = None
_KNOWN_SECRETS = set()
_KNOWN_SECRETS_LOCK = threading.RLock()
_URL = re.compile(r"\b(?:https?|wss?)://[^\s\"'<>]+", re.IGNORECASE)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_FIELD = re.compile(
    r"(?i)\b(password|passwd|otp|totp|recovery[_ -]?code|access[_ -]?token|"
    r"refresh[_ -]?token|token|secret|authorization|cookie|session(?:[_ -]?id|"
    r"[_ -]?secret)?|credential(?:[_ -]?(?:id|ref|reference|secret))?|api[_ -]?key)"
    r"([\"']?\s*[:=]\s*)([\"']?)([^\s,;&\"']+)\3"
)
_AUTH_HEADER = re.compile(r"(?i)\bauthorization\s*:\s*(?:bearer\s+)?[^\s,;]+")
_COOKIE_HEADER = re.compile(r"(?i)\b(?:set-)?cookie\s*:\s*[^\s,;]+")
_ACCOUNT_NUMBER = re.compile(r"(?<![A-Za-z0-9])(\d{7,19})(?![A-Za-z0-9])")
_USER_PATH = re.compile(r"(?i)(?:[A-Z]:[\\/]Users[\\/]|/(?:Users|home)/)[^\\/\s]+")
_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "otp",
    "totp",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "session_cookie",
    "session_secret",
    "credential",
    "credential_ref",
    "amount",
    "balance",
    "available_balance",
    "payer_balance",
    "final_amount",
    "payment_payload",
    "request_body",
    "response_body",
    "ocr_text",
    "expected_text",
    "typed_text",
}
_TECHNICAL_ID_KEYS = {
    "component",
    "event",
    "device_id",
    "camera_id",
    "job_id",
    "workflow_id",
    "workflow_run_id",
    "step_id",
    "request_id",
    "operation_id",
    "ocr_request_id",
    "pi_request_id",
    "error_code",
    "endpoint",
    "operation",
    "route",
}
_FIELD_ORDER = (
    "component",
    "event",
    "app_version",
    "session_id",
    "request_id",
    "operation_id",
    "device_id",
    "camera_id",
    "job_id",
    "workflow_id",
    "workflow_run_id",
    "step_id",
    "ocr_request_id",
    "pi_request_id",
    "error_code",
    "exception_type",
    "duration_ms",
    "retry_count",
    "outcome",
)


def application_version():
    configured = os.environ.get("PHONE_CONTROLLER_APP_VERSION", "").strip()
    if configured:
        return configured[:80]
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        try:
            bundled = (
                (Path(sys._MEIPASS) / "phone-controller-version.txt")
                .read_text(encoding="utf-8")
                .strip()
            )
            if bundled and len(bundled) <= 80 and "\n" not in bundled:
                return bundled
        except OSError:
            pass
    for distribution in ("phone-controller", "camera-phone-control"):
        try:
            return importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            continue
    return "development"


def sanitize_url(value):
    """Return a URL without userinfo, query data, or fragments."""
    try:
        parsed = urlsplit(str(value))
        if parsed.scheme.casefold() not in {"http", "https", "ws", "wss"}:
            return "<redacted-url>"
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        try:
            if parsed.port is not None:
                netloc += f":{parsed.port}"
        except ValueError:
            return "<redacted-url>"
        return urlunsplit((parsed.scheme, netloc, parsed.path or "", "", ""))
    except (TypeError, ValueError):
        return "<redacted-url>"


def _mask_account(match):
    value = match.group(1)
    return "*" * max(3, len(value) - 4) + value[-4:]


def sanitize_text(value, *, secrets=(), mask_account_numbers=True):
    message = str(value)
    message = _URL.sub(lambda match: sanitize_url(match.group(0)), message)
    message = _BEARER.sub("Bearer <redacted>", message)
    message = _AUTH_HEADER.sub("Authorization: <redacted>", message)
    message = _COOKIE_HEADER.sub("Cookie: <redacted>", message)
    message = _USER_PATH.sub("<user-home>", message)
    message = _SECRET_FIELD.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}{match.group(3)}"
            f"<redacted>{match.group(3)}"
        ),
        message,
    )
    if mask_account_numbers:
        message = _ACCOUNT_NUMBER.sub(_mask_account, message)
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 4:
            message = message.replace(secret, "<redacted>")
    return message.replace("<redacted> <redacted>", "<redacted>")


def _safe_value(key, value, secrets=()):
    if key.casefold() in _SENSITIVE_KEYS:
        return "<redacted>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = sanitize_text(
        value,
        secrets=secrets,
        mask_account_numbers=key.casefold() not in _TECHNICAL_ID_KEYS,
    )
    return text[:500]


def sanitize_diagnostic_fields(fields, *, secrets=()):
    return {
        str(key): _safe_value(str(key), value, secrets)
        for key, value in dict(fields).items()
    }


class RedactionFilter(logging.Filter):
    """Sanitize the copy of a record consumed by a durable handler."""

    def __init__(self, secrets=()):
        super().__init__()
        self.secrets = tuple(value for value in secrets if isinstance(value, str))

    def filter(self, record):
        with _KNOWN_SECRETS_LOCK:
            secrets = (*self.secrets, *_KNOWN_SECRETS)
        record.msg = sanitize_text(record.getMessage(), secrets=secrets)
        record.args = None
        fields = getattr(record, "diagnostic_fields", {})
        record.diagnostic_fields = sanitize_diagnostic_fields(fields, secrets=secrets)
        return True


class SafeQueueHandler(logging.handlers.QueueHandler):
    """Queue records without persisting arbitrary exception tracebacks."""

    def prepare(self, record):
        safe_record = copy.copy(record)
        if safe_record.exc_info:
            fields = dict(getattr(safe_record, "diagnostic_fields", {}))
            fields.setdefault(
                "exception_type",
                getattr(safe_record.exc_info[0], "__name__", "Exception"),
            )
            safe_record.diagnostic_fields = fields
        safe_record.exc_info = None
        safe_record.exc_text = None
        safe_record.stack_info = None
        return super().prepare(safe_record)


def register_secrets(*values):
    with _KNOWN_SECRETS_LOCK:
        for value in values:
            if isinstance(value, str) and len(value) >= 4:
                _KNOWN_SECRETS.add(value)


def register_sensitive_environment(environment=None):
    environment = os.environ if environment is None else environment
    register_secrets(
        *(
            value
            for name, value in environment.items()
            if any(
                part in name.casefold()
                for part in (
                    "password",
                    "token",
                    "secret",
                    "credential",
                    "api_key",
                    "authorization",
                )
            )
        )
    )


def _format_value(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if re.fullmatch(r"[A-Za-z0-9_.:/@+\-]+", text):
        return text
    return (
        '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    )


class StructuredFormatter(logging.Formatter):
    def format(self, record):
        timestamp = (
            datetime.fromtimestamp(record.created)
            .astimezone()
            .isoformat(sep=" ", timespec="milliseconds")
        )
        fields = dict(getattr(record, "diagnostic_fields", {}))
        fields.setdefault("component", record.name)
        if _APP_VERSION:
            fields.setdefault("app_version", _APP_VERSION)
        if _SESSION_ID:
            fields.setdefault("session_id", _SESSION_ID)
        ordered = []
        for key in _FIELD_ORDER:
            if key in fields:
                value = _format_value(fields.pop(key))
                if value is not None:
                    ordered.append(f"{key}={value}")
        for key in sorted(fields):
            value = _format_value(fields[key])
            if value is not None:
                ordered.append(f"{key}={value}")
        message = record.getMessage()
        if message:
            ordered.append(f"message={_format_value(message)}")
        return f"{timestamp} {record.levelname} " + " ".join(ordered)


def log_event(logger, level, component, event, message=None, **fields):
    merged = dict(_CONTEXT.get())
    merged.update(fields)
    merged["component"] = component
    merged["event"] = event
    logger.log(
        level,
        message or "",
        extra={"diagnostic_fields": merged},
    )


@contextlib.contextmanager
def diagnostic_context(**fields):
    merged = dict(_CONTEXT.get())
    merged.update({key: value for key, value in fields.items() if value is not None})
    token = _CONTEXT.set(merged)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


class EventDeduplicator:
    def __init__(self, interval_seconds=45.0):
        self.interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._states = {}

    def should_log(self, component, event, identity=()):
        key = (component, event, *tuple(identity))
        current = time.monotonic()
        with self._lock:
            previous = self._states.get(key)
            if previous is not None and current - previous < self.interval_seconds:
                return False
            self._states[key] = current
            return True

    def clear(self, component, event, identity=()):
        key = (component, event, *tuple(identity))
        with self._lock:
            return self._states.pop(key, None)


EVENT_DEDUPLICATOR = EventDeduplicator()
_PROCESS_LOG_QUEUE = None


def cleanup_old_rotated_logs(log_directory, *, now=None):
    cutoff = (time.time() if now is None else now) - ROTATED_RETENTION_DAYS * 86400
    directory = Path(log_directory)
    try:
        candidates = list(directory.glob(PRIMARY_LOG_NAME + ".*"))
    except OSError:
        return
    for path in candidates:
        try:
            suffix = path.name.removeprefix(PRIMARY_LOG_NAME + ".")
            if (
                suffix.isdigit()
                and path.is_file()
                and not path.is_symlink()
                and path.stat().st_mtime < cutoff
            ):
                path.unlink()
        except OSError:
            continue


@dataclass
class LoggingRuntime:
    queue: object
    listener: logging.handlers.QueueListener
    session_id: str
    app_version: str
    previous_thread_hook: object

    def stop(self):
        global _APP_VERSION, _PROCESS_LOG_QUEUE, _SESSION_ID
        log_event(
            logging.getLogger("app"),
            logging.INFO,
            "app",
            "app_stopped",
            app_version=self.app_version,
            session_id=self.session_id,
        )
        root = logging.getLogger()
        for handler in tuple(root.handlers):
            if (
                isinstance(handler, logging.handlers.QueueHandler)
                and getattr(handler, "queue", None) is self.queue
            ):
                root.removeHandler(handler)
                handler.close()
        self.listener.stop()
        for handler in self.listener.handlers:
            handler.close()
        threading.excepthook = self.previous_thread_hook
        close = getattr(self.queue, "close", None)
        if close is not None:
            close()
        if _PROCESS_LOG_QUEUE is self.queue:
            _PROCESS_LOG_QUEUE = None
        if _SESSION_ID == self.session_id:
            _SESSION_ID = None
            _APP_VERSION = None


def _thread_hook(args):
    log_event(
        logging.getLogger("app"),
        logging.ERROR,
        "app",
        "background_thread_failed",
        thread_name=getattr(args.thread, "name", "unknown"),
        exception_type=getattr(args.exc_type, "__name__", "Exception"),
        error_code="background_thread_failed",
    )


def configure_production_logging(log_directory, *, version=None, packaged=None):
    global _APP_VERSION, _PROCESS_LOG_QUEUE, _SESSION_ID
    directory = Path(log_directory)
    directory.mkdir(parents=True, exist_ok=True)
    cleanup_old_rotated_logs(directory)
    log_queue = multiprocessing.get_context("spawn").Queue(-1)
    _PROCESS_LOG_QUEUE = log_queue
    handler = logging.handlers.RotatingFileHandler(
        directory / PRIMARY_LOG_NAME,
        maxBytes=PRIMARY_MAX_BYTES,
        backupCount=PRIMARY_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.addFilter(RedactionFilter())
    handler.setFormatter(StructuredFormatter())
    listener = logging.handlers.QueueListener(
        log_queue, handler, respect_handler_level=True
    )
    listener.start()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(SafeQueueHandler(log_queue))
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    session_id = uuid.uuid4().hex
    app_version = version or application_version()
    _SESSION_ID = session_id
    _APP_VERSION = app_version
    previous_hook = threading.excepthook
    threading.excepthook = _thread_hook
    runtime = LoggingRuntime(
        log_queue, listener, session_id, app_version, previous_hook
    )
    log_event(
        logging.getLogger("app"),
        logging.INFO,
        "app",
        "app_started",
        app_version=app_version,
        session_id=session_id,
        python=sys.version.split()[0],
        packaged=bool(getattr(sys, "frozen", False) if packaged is None else packaged),
    )
    return runtime


def configure_child_logging(log_queue):
    if log_queue is None:
        return
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(SafeQueueHandler(log_queue))


def current_logging_queue():
    return _PROCESS_LOG_QUEUE


def configure_development_logging():
    global _APP_VERSION, _SESSION_ID
    _APP_VERSION = application_version()
    _SESSION_ID = _SESSION_ID or uuid.uuid4().hex
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(RedactionFilter())
        handler.setFormatter(StructuredFormatter())
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def write_startup_error(log_directory, message, *, exception_type=None):
    """Best-effort bounded fallback used before normal logging is available."""
    try:
        directory = Path(log_directory)
        directory.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            directory / STARTUP_LOG_NAME,
            maxBytes=STARTUP_MAX_BYTES,
            backupCount=STARTUP_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.addFilter(RedactionFilter())
        handler.setFormatter(StructuredFormatter())
        record = logging.LogRecord(
            "app",
            logging.CRITICAL,
            __file__,
            0,
            message,
            (),
            None,
        )
        record.diagnostic_fields = {
            "component": "app",
            "event": "app_start_failed",
            "error_code": "app_start_failed",
            "exception_type": exception_type,
        }
        handler.handle(record)
        handler.close()
    except Exception:
        pass
