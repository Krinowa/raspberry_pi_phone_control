"""Private atomic JSON persistence for account bindings and job state."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

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
            return sorted(self._jobs().values(), key=lambda item: item.created_at)

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
                        status="FAILED",
                        finished_at=timestamp,
                        error=message,
                    )
                    changed.append(job_id)
            if changed:
                self._save_jobs(jobs)
            return changed

    def _save_jobs(self, jobs):
        self._write({key: value.stored() for key, value in sorted(jobs.items())})
