"""Local device configuration and separately referenced external credentials."""

import json
import os
import re
import tempfile
import threading
import uuid
from copy import deepcopy
from pathlib import Path

from automation.workflows.validation import identifier
from .client import base_origin

DEFAULT_CREDENTIAL_PATH = (
    Path(__file__).resolve().parents[2] / "job_data" / "withdraw_credentials.json"
)
_REFERENCE = re.compile(r"(?:env:[A-Z][A-Z0-9_]{0,79}|store:[a-f0-9]{32})")


class WithdrawCredentialStore:
    """Private, atomically written secrets referenced by safe device config."""

    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_CREDENTIAL_PATH)
        self.lock = threading.RLock()

    def _read(self):
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or self.path.stat().st_size > 1024 * 1024:
            raise OSError(
                "Withdraw credential storage is unsafe or unexpectedly large."
            )
        with self.path.open(encoding="utf-8") as stream:
            values = json.load(stream)
        if not isinstance(values, dict):
            raise ValueError("Withdraw credential storage is invalid.")
        return values

    def _write(self, values):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise OSError("Withdraw credential storage cannot be a symlink.")
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent,
            prefix="." + self.path.stem + "-",
            suffix=".tmp",
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(values, stream, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def save(self, password, totp_secret):
        if not isinstance(password, str) or not password or len(password) > 4096:
            raise ValueError("Password is required.")
        if (
            not isinstance(totp_secret, str)
            or not totp_secret.strip()
            or len(totp_secret) > 256
        ):
            raise ValueError("Google 2FA secret is required.")
        reference = "store:" + uuid.uuid4().hex
        with self.lock:
            values = self._read()
            values[reference[6:]] = {
                "password": password,
                "totp_secret": totp_secret.strip().replace(" ", ""),
            }
            self._write(values)
        return reference

    def load(self, reference):
        if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
            raise ValueError("Invalid external credential reference.")
        if reference.startswith("env:"):
            prefix = reference[4:]
            password = os.environ.get(prefix + "_PASSWORD")
            secret = os.environ.get(prefix + "_TOTP_SECRET")
        else:
            with self.lock:
                value = self._read().get(reference[6:])
            password = value.get("password") if isinstance(value, dict) else None
            secret = value.get("totp_secret") if isinstance(value, dict) else None
        if not password or not secret:
            raise ValueError("Referenced external password or TOTP secret is missing.")
        return password, secret

    def delete(self, reference):
        if not isinstance(reference, str) or not reference.startswith("store:"):
            return False
        with self.lock:
            values = self._read()
            removed = values.pop(reference[6:], None) is not None
            if removed:
                self._write(values)
            return removed


_CREDENTIALS = WithdrawCredentialStore()


def validate_configuration(data):
    if not isinstance(data, dict) or set(data) - {
        "balance_workflow_id",
        "withdraw_api",
    }:
        raise ValueError("Expected balance_workflow_id and withdraw_api configuration.")
    balance = data.get("balance_workflow_id") or None
    if balance:
        identifier(balance)
    api = deepcopy(data.get("withdraw_api", {"enabled": False}))
    if not isinstance(api, dict) or set(api) - {
        "enabled",
        "base_url",
        "operator_id",
        "username",
        "credential_reference",
        "payment_records",
        "balance_refresh_seconds",
    }:
        raise ValueError(
            "Unknown Withdraw API configuration field (secrets must use a credential reference)."
        )
    if type(api.get("enabled", False)) is not bool:
        raise ValueError("enabled must be boolean.")
    api.setdefault("enabled", False)
    if api.get("base_url"):
        api["base_url"] = base_origin(api["base_url"])
    if api.get("operator_id") is not None and (
        type(api["operator_id"]) is not int or api["operator_id"] <= 0
    ):
        raise ValueError("operator_id must be a positive integer.")
    for key in ("username", "credential_reference"):
        value = api.get(key)
        if value is not None and (
            not isinstance(value, str)
            or not value
            or len(value) > 200
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError(f"{key} must be nonempty text.")
    if api.get("credential_reference") and not _REFERENCE.fullmatch(
        api["credential_reference"]
    ):
        raise ValueError(
            "credential_reference must identify environment or private stored credentials."
        )
    records = api.get("payment_records", [])
    if (
        not isinstance(records, list)
        or len(records) > 100
        or not all(
            isinstance(value, str)
            and value
            and len(value) <= 80
            and all(ord(character) >= 32 for character in value)
            for value in records
        )
    ):
        raise ValueError("payment_records must contain safe text identifiers.")
    api["payment_records"] = records
    # Backward-compatible dual cadence: observations older than this become
    # stale (their value is retained), and insufficient pending work is read
    # again no sooner than this interval.
    refresh = api.get("balance_refresh_seconds", 60)
    if type(refresh) not in (int, float) or not 1 <= refresh <= 300:
        raise ValueError(
            "Balance freshness and pending-retry interval must be 1–300 seconds."
        )
    if api["enabled"] and not all(
        api.get(key)
        for key in ("base_url", "operator_id", "username", "credential_reference")
    ):
        raise ValueError(
            "Enabled integration requires API host, operator ID, username and credential reference."
        )
    return {"balance_workflow_id": balance, "withdraw_api": api}


def load_credentials(reference):
    return _CREDENTIALS.load(reference)
