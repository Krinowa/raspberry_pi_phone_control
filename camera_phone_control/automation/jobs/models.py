"""Validated records for account routing and persistent workflow jobs."""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath

from hardware.device_config import validate_device_id
from automation.workflows.models import now
from automation.workflows.validation import identifier

JOB_STATES = {
    "QUEUED",
    "WAITING_FOR_DEVICE",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "INTERRUPTED",
}
PENDING_JOB_STATES = {"QUEUED", "WAITING_FOR_DEVICE"}
TERMINAL_JOB_STATES = {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}

JOB_TRANSITIONS = {
    "QUEUED": {"WAITING_FOR_DEVICE", "RUNNING", "CANCELLED"},
    "WAITING_FOR_DEVICE": {"QUEUED", "RUNNING", "CANCELLED"},
    "RUNNING": {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"},
    "COMPLETED": set(),
    "FAILED": set(),
    "CANCELLED": set(),
    "INTERRUPTED": set(),
}


def _plain_text(value, label, maximum, *, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    value = value.strip()
    if (
        (required and not value)
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label} must be valid text of at most {maximum} characters.")
    return value or None


def _screenshot_metadata(values):
    if not isinstance(values, list) or len(values) > 1000:
        raise ValueError("Job screenshots must be a list of at most 1000 items.")
    result = []
    for item in values:
        if not isinstance(item, dict) or set(item) != {
            "step_id",
            "label",
            "captured_at",
            "relative_path",
        }:
            raise ValueError("Stored screenshot metadata is invalid.")
        step_id = identifier(item.get("step_id"))
        label = _plain_text(item.get("label"), "screenshot label", 160, required=False)
        captured_at = _plain_text(item.get("captured_at"), "captured_at", 80)
        relative_path = _plain_text(item.get("relative_path"), "relative_path", 240)
        path = PurePosixPath(relative_path)
        if (
            path.is_absolute()
            or len(path.parts) != 2
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.suffix.casefold() != ".jpg"
        ):
            raise ValueError(
                "Stored screenshot path must be a relative daily JPEG path."
            )
        result.append(
            {
                "step_id": step_id,
                "label": label,
                "captured_at": captured_at,
                "relative_path": relative_path,
            }
        )
    return result


@dataclass(frozen=True)
class AccountBinding:
    account_id: str
    display_name: str
    device_id: str
    enabled: bool
    created_at: str
    updated_at: str
    provider: str | None = None

    @classmethod
    def create(cls, data, existing=None):
        if not isinstance(data, dict):
            raise ValueError("Account binding must be a JSON object.")

        allowed = {"account_id", "display_name", "provider", "device_id", "enabled"}

        if set(data) - allowed:
            raise ValueError("Unsupported account binding field.")
        account_id = identifier(
            data.get("account_id", existing.account_id if existing else None)
        )

        if existing is not None and account_id != existing.account_id:
            raise ValueError("account_id is stable and cannot be changed.")
        display_name = _plain_text(
            data.get("display_name", existing.display_name if existing else None),
            "display_name",
            160,
        )
        provider = _plain_text(
            data.get("provider", existing.provider if existing else None),
            "provider",
            80,
            required=False,
        )

        device_id = data.get("device_id", existing.device_id if existing else None)
        validate_device_id(device_id)
        enabled = data.get("enabled", existing.enabled if existing else True)
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false.")
        timestamp = now()
        return cls(
            account_id=account_id,
            display_name=display_name,
            provider=provider,
            device_id=device_id,
            enabled=enabled,
            created_at=existing.created_at if existing else timestamp,
            updated_at=timestamp,
        )

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise ValueError("Stored account binding is invalid.")

        binding = cls.create(
            {
                key: data.get(key)
                for key in (
                    "account_id",
                    "display_name",
                    "provider",
                    "device_id",
                    "enabled",
                )
            }
        )
        return cls(
            **{
                **asdict(binding),
                "created_at": _plain_text(data.get("created_at"), "created_at", 80),
                "updated_at": _plain_text(data.get("updated_at"), "updated_at", 80),
            }
        )

    def public(self):
        return asdict(self)


@dataclass(frozen=True)
class Job:
    job_id: str
    account_id: str
    device_id: str
    workflow: str
    payload: dict
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    result: str | None = None
    cancel_requested: bool = False
    external_ref: str | None = None
    bank: str | None = None
    action: str | None = None
    recovery_workflow_id: str | None = None
    recovery_status: str | None = None
    recovery_run_id: str | None = None
    recovery_error: str | None = None
    recovered_at: str | None = None
    failed_step_index: int | None = None
    failed_step_id: str | None = None
    retry_of_job_id: str | None = None
    start_step_index: int = 0
    screenshots: list = field(default_factory=list)

    @classmethod
    def create(
        cls,
        account_id,
        device_id,
        workflow,
        payload,
        status,
        *,
        external_ref=None,
        bank=None,
        action=None,
        recovery_workflow_id=None,
        retry_of_job_id=None,
        start_step_index=0,
    ):
        account_id = identifier(account_id)
        validate_device_id(device_id)
        workflow = identifier(workflow)

        if status not in PENDING_JOB_STATES:
            raise ValueError("New jobs must start queued or waiting for their device.")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object.")

        try:
            encoded = json.dumps(payload, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must contain valid JSON values.") from exc

        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise ValueError("payload is too large.")

        external_ref = _plain_text(external_ref, "external_ref", 256, required=False)
        bank = _plain_text(bank, "bank", 80, required=False)
        action = _plain_text(action, "action", 80, required=False)
        if recovery_workflow_id is not None:
            recovery_workflow_id = identifier(recovery_workflow_id)
        if retry_of_job_id is not None:
            retry_of_job_id = identifier(retry_of_job_id)
        if type(start_step_index) is not int or start_step_index < 0:
            raise ValueError("start_step_index must be a non-negative integer.")

        return cls(
            job_id="job_" + uuid.uuid4().hex,
            account_id=account_id,
            device_id=device_id,
            workflow=workflow,
            payload=deepcopy(payload),
            status=status,
            created_at=now(),
            external_ref=external_ref,
            bank=bank,
            action=action,
            recovery_workflow_id=recovery_workflow_id,
            retry_of_job_id=retry_of_job_id,
            start_step_index=start_step_index,
        )

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("Stored job record is invalid.")

        fields = set(cls.__dataclass_fields__)
        optional_fields = {
            "external_ref",
            "bank",
            "action",
            "recovery_workflow_id",
            "recovery_status",
            "recovery_run_id",
            "recovery_error",
            "recovered_at",
            "failed_step_index",
            "failed_step_id",
            "retry_of_job_id",
            "start_step_index",
            "screenshots",
        }
        required_fields = fields - optional_fields
        if set(data) - fields or not required_fields.issubset(data):
            raise ValueError("Stored job record is invalid.")
        data = {
            **{
                field: None
                for field in optional_fields - {"screenshots", "start_step_index"}
            },
            "start_step_index": 0,
            "screenshots": [],
            **data,
        }

        identifier(data.get("job_id"))
        identifier(data.get("account_id"))
        validate_device_id(data.get("device_id"))
        identifier(data.get("workflow"))

        if data.get("status") not in JOB_STATES:
            raise ValueError("Stored job status is invalid.")
        if not isinstance(data.get("payload"), dict):
            raise ValueError("Stored job payload is invalid.")
        try:
            encoded = json.dumps(data["payload"], allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Stored job payload is invalid.") from exc

        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise ValueError("Stored job payload is too large.")
        if type(data.get("cancel_requested")) is not bool:
            raise ValueError("Stored cancellation state is invalid.")
        _plain_text(data.get("created_at"), "created_at", 80)

        for field in ("started_at", "finished_at"):
            _plain_text(data.get(field), field, 80, required=False)
        for field in ("error", "result"):
            _plain_text(data.get(field), field, 500, required=False)

        _plain_text(data.get("external_ref"), "external_ref", 256, required=False)
        _plain_text(data.get("bank"), "bank", 80, required=False)
        _plain_text(data.get("action"), "action", 80, required=False)
        recovery_workflow_id = data.get("recovery_workflow_id")
        if recovery_workflow_id is not None:
            identifier(recovery_workflow_id)
        if data.get("recovery_status") not in {
            None,
            "RECOVERING",
            "READY",
            "NEEDS_ATTENTION",
        }:
            raise ValueError("Stored recovery status is invalid.")
        recovery_run_id = data.get("recovery_run_id")
        if recovery_run_id is not None:
            identifier(recovery_run_id)
        _plain_text(data.get("recovery_error"), "recovery_error", 500, required=False)
        _plain_text(data.get("recovered_at"), "recovered_at", 80, required=False)
        failed_step_index = data.get("failed_step_index")
        if failed_step_index is not None and (
            type(failed_step_index) is not int or failed_step_index < 0
        ):
            raise ValueError("Stored failed step index is invalid.")
        for field in ("failed_step_id", "retry_of_job_id"):
            if data.get(field) is not None:
                identifier(data[field])
        if (
            type(data.get("start_step_index")) is not int
            or data["start_step_index"] < 0
        ):
            raise ValueError("Stored start step index is invalid.")
        data["screenshots"] = _screenshot_metadata(data.get("screenshots"))
        return cls(**deepcopy(data))

    def stored(self):
        return asdict(self)

    def public(self, queue_position=None):
        # Payload is intentionally excluded: status/debug endpoints must not
        # echo future banking values or credentials.
        return {
            "job_id": self.job_id,
            "account_id": self.account_id,
            "device_id": self.device_id,
            "workflow": self.workflow,
            "external_ref": self.external_ref,
            "bank": self.bank,
            "action": self.action,
            "status": self.status,
            "queue_position": queue_position,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
            "cancel_requested": self.cancel_requested,
            "recovery_workflow_id": self.recovery_workflow_id,
            "recovery_status": self.recovery_status,
            "recovery_run_id": self.recovery_run_id,
            "recovery_error": self.recovery_error,
            "recovered_at": self.recovered_at,
            "failed_step_index": self.failed_step_index,
            "failed_step_id": self.failed_step_id,
            "retry_of_job_id": self.retry_of_job_id,
            "start_step_index": self.start_step_index,
            "screenshots": deepcopy(self.screenshots),
        }
