"""Private atomic JSON persistence for account bindings and job state."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from automation.workflows.validation import identifier
from hardware.device_config import validate_device_id

from .models import AccountBinding, Job

DEFAULT_DATA_DIRECTORY = Path(__file__).resolve().parents[1] / "job_data"


class RecordNotFound(KeyError):
    pass


class RecordConflict(ValueError):
    pass


class _JsonStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def _read(self):
        if not self.path.exists():
            return {}
        if self.path.is_symlink():
            raise OSError("Persistent state path cannot be a symlink.")
        if self.path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Persistent state file is unexpectedly large.")
        with self.path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("Persistent state must be a JSON object.")
        return data

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise OSError("Persistent state path cannot be a symlink.")
        fd, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix="." + self.path.stem + "-", suffix=".tmp"
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class AccountBindingStore(_JsonStore):
    def __init__(self, path=None):
        super().__init__(path or DEFAULT_DATA_DIRECTORY / "accounts.json")

    def _bindings(self):
        bindings = {}
        for account_id, value in self._read().items():
            binding = AccountBinding.from_dict(value)
            if binding.account_id != account_id:
                raise ValueError("Stored account ID does not match its key.")
            bindings[account_id] = binding
        return bindings

    def list(self):
        with self.lock:
            return sorted(self._bindings().values(), key=lambda item: item.account_id)

    def get(self, account_id):
        with self.lock:
            try:
                return self._bindings()[account_id]
            except KeyError:
                raise RecordNotFound(account_id) from None

    def create(self, data):
        binding = AccountBinding.create(data)
        with self.lock:
            bindings = self._bindings()
            if binding.account_id in bindings:
                raise RecordConflict("Account binding already exists.")
            self._check_device(bindings, binding)
            bindings[binding.account_id] = binding
            self._save_bindings(bindings)
            return binding

    def update(self, account_id, changes):
        with self.lock:
            bindings = self._bindings()
            if account_id not in bindings:
                raise RecordNotFound(account_id)
            if "account_id" in changes and changes["account_id"] != account_id:
                raise ValueError("account_id must match the URL.")
            binding = AccountBinding.create(changes, bindings[account_id])
            self._check_device(bindings, binding, excluding=account_id)
            bindings[account_id] = binding
            self._save_bindings(bindings)
            return binding

    def delete(self, account_id):
        with self.lock:
            bindings = self._bindings()
            if account_id not in bindings:
                raise RecordNotFound(account_id)
            binding = bindings.pop(account_id)
            self._save_bindings(bindings)
            return binding

    @staticmethod
    def _check_device(bindings, binding, excluding=None):
        duplicate = next(
            (
                item.account_id
                for item in bindings.values()
                if item.account_id != excluding and item.device_id == binding.device_id
            ),
            None,
        )
        if duplicate:
            raise RecordConflict(
                f"Device {binding.device_id} is already assigned to account {duplicate}."
            )

    def _save_bindings(self, bindings):
        self._write({key: value.public() for key, value in sorted(bindings.items())})


class DeviceWorkflowAssignmentStore(_JsonStore):
    """Persistent workflow allowlists keyed by stable logical device ID.

    A missing key intentionally means unrestricted legacy behavior. An existing
    key with an empty list means that no workflow is allowed on that device.
    """

    def __init__(self, path=None):
        super().__init__(path or DEFAULT_DATA_DIRECTORY / "device_workflows.json")

    def _assignments(self):
        assignments = {}
        for device_id, value in self._read().items():
            validate_device_id(device_id)
            if (
                not isinstance(value, dict)
                or "allowed_workflow_ids" not in value
                or set(value)
                - {"allowed_workflow_ids", "balance_workflow_id", "withdraw_api"}
            ):
                raise ValueError("Stored device workflow assignment is invalid.")
            workflow_ids = value["allowed_workflow_ids"]
            if not isinstance(workflow_ids, list) or len(workflow_ids) > 1:
                raise ValueError("Only one workflow can be assigned to a phone.")
            normalized = []
            for workflow_id in workflow_ids:
                workflow_id = identifier(workflow_id)
                if workflow_id in normalized:
                    raise ValueError("Stored workflow assignments contain duplicates.")
                normalized.append(workflow_id)
            assignments[device_id] = tuple(normalized)
        return assignments

    def get(self, device_id):
        """Return a tuple allowlist, or None when the legacy device is unrestricted."""
        validate_device_id(device_id)
        with self.lock:
            return self._assignments().get(device_id)

    def set(self, device_id, workflow_ids):
        validate_device_id(device_id)
        if not isinstance(workflow_ids, list) or len(workflow_ids) > 1:
            raise ValueError("Only one workflow can be assigned to a phone.")

        normalized = []
        for workflow_id in workflow_ids:
            workflow_id = identifier(workflow_id)
            if workflow_id in normalized:
                raise ValueError("workflow_ids must not contain duplicates.")
            normalized.append(workflow_id)
        with self.lock:
            assignments = self._assignments()
            assignments[device_id] = tuple(normalized)
            self._save(assignments)
        return tuple(normalized)

    def delete(self, device_id):
        validate_device_id(device_id)
        with self.lock:
            assignments = self._assignments()
            removed = assignments.pop(device_id, None)
            if removed is not None:
                self._save(assignments)
            return removed is not None

    def allows(self, device_id, workflow_id):
        allowed = self.get(device_id)
        return allowed is None or identifier(workflow_id) in allowed

    def integration(self, device_id):
        from automation.integrations.withdraw_v2.config import validate_configuration

        validate_device_id(device_id)
        with self.lock:
            value = self._read().get(device_id, {})
            return validate_configuration(
                {
                    key: value[key]
                    for key in ("balance_workflow_id", "withdraw_api")
                    if key in value
                }
            )

    def integrations(self):
        with self.lock:
            return {
                device_id: self.integration(device_id) for device_id in self._read()
            }

    def set_integration(self, device_id, data):
        from automation.integrations.withdraw_v2.config import validate_configuration

        validate_device_id(device_id)
        value = validate_configuration(data)
        with self.lock:
            records = self._read()
            for other, record in records.items():
                api = record.get("withdraw_api", {})
                current = value["withdraw_api"]
                if (
                    other != device_id
                    and current.get("enabled")
                    and api.get("enabled")
                    and api.get("base_url") == current.get("base_url")
                    and api.get("operator_id") == current.get("operator_id")
                ):
                    raise RecordConflict(
                        "External operator is already assigned to another device."
                    )
            records.setdefault(device_id, {"allowed_workflow_ids": []}).update(value)
            self._write(records)
        return value

    def _save(self, assignments):
        previous = self._read()
        self._write(
            {
                device_id: {
                    **previous.get(device_id, {}),
                    "allowed_workflow_ids": list(workflow_ids),
                }
                for device_id, workflow_ids in sorted(assignments.items())
            }
        )


class JobStore(_JsonStore):
    def __init__(self, path=None):
        super().__init__(path or DEFAULT_DATA_DIRECTORY / "jobs.json")

    def _jobs(self):
        jobs = {}
        for job_id, value in self._read().items():
            job = Job.from_dict(value)
            if job.job_id != job_id:
                raise ValueError("Stored job ID does not match its key.")
            jobs[job_id] = job
        return jobs

    def list(self):
        with self.lock:
            return sorted(
                self._jobs().values(),
                key=lambda item: (item.created_at, item.job_id),
            )

    def get(self, job_id):
        with self.lock:
            try:
                return self._jobs()[job_id]
            except KeyError:
                raise RecordNotFound(job_id) from None

    def create(self, job):
        with self.lock:
            jobs = self._jobs()
            if job.job_id in jobs:
                raise RecordConflict("Job already exists.")
            jobs[job.job_id] = job
            self._save_jobs(jobs)
            return job

    def replace(self, job):
        with self.lock:
            jobs = self._jobs()
            if job.job_id not in jobs:
                raise RecordNotFound(job.job_id)
            jobs[job.job_id] = job
            self._save_jobs(jobs)
            return job

    def recover_running(self, timestamp, message):
        with self.lock:
            jobs = self._jobs()
            changed = []
            for job_id, job in jobs.items():
                if job.status == "RUNNING":
                    jobs[job_id] = replace(
                        job,
                        status="INTERRUPTED",
                        finished_at=timestamp,
                        error=message,
                        recovery_status="NEEDS_ATTENTION",
                    )
                    changed.append(job_id)
            if changed:
                self._save_jobs(jobs)
            return changed

    def recover_incomplete_recoveries(self, timestamp, message):
        with self.lock:
            jobs = self._jobs()
            changed = []
            for job_id, job in jobs.items():
                if job.recovery_status == "RECOVERING":
                    jobs[job_id] = replace(
                        job,
                        recovery_status="NEEDS_ATTENTION",
                        recovery_error=message,
                        recovered_at=timestamp,
                    )
                    changed.append(job_id)
            if changed:
                self._save_jobs(jobs)
            return changed

    def _save_jobs(self, jobs):
        self._write({key: value.stored() for key, value in sorted(jobs.items())})
