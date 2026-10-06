"""Lifecycle and scheduler gate for independent, device-pinned workers."""

import os
import threading
from pathlib import Path

from automation.jobs.storage import RecordConflict, RecordNotFound
from .balance import CameraOCRBalanceProvider
from .client import APIError, WithdrawClient
from .config import WithdrawCredentialStore
from .journal import TERMINAL, WithdrawJournal
from .worker import WithdrawWorker, payment_workflow
from flask import jsonify, request


class WithdrawManager:
    def __init__(
        self,
        *,
        assignments,
        accounts,
        workflows,
        service,
        scheduler,
        executor,
        journal_path,
        start_workers=False,
        worker_factory=WithdrawWorker,
        client_factory=WithdrawClient,
        credential_store=None,
    ):
        self.assignments, self.accounts, self.workflows, self.service = (
            assignments,
            accounts,
            workflows,
            service,
        )
        self.scheduler, self.executor = scheduler, executor
        self.journal = WithdrawJournal(journal_path)
        self.worker_factory = worker_factory
        self.client_factory = client_factory
        self.credential_store = credential_store or WithdrawCredentialStore()
        self.enabled = start_workers
        self.workers = {}
        self.lock = threading.RLock()
        self.balance_provider = CameraOCRBalanceProvider(
            scheduler,
            workflows,
            service,
            lambda device: assignments.integration(device)["balance_workflow_id"],
        )

        scheduler.integration_gate = self.blocked
        # No queued payment created by a previous process is authorized to run.
        # A journal write can fail after enqueue; scan jobs as well as the journal.
        for job in scheduler.list_jobs():
            if job.action == "withdraw_v2" and job.status in {
                "QUEUED",
                "WAITING_FOR_DEVICE",
            }:
                scheduler.cancel_job(job.job_id)
                self.journal.update(
                    job.device_id,
                    phase="NEEDS_ATTENTION",
                    job_id=job.job_id,
                    tx_id=job.external_ref,
                    error="Queued withdrawal was interrupted by restart and was not replayed.",
                )

        for device_id, config in assignments.integrations().items():
            if config["withdraw_api"]["enabled"]:
                self._create(device_id, config)

    def blocked(self, device_id):
        record = self.journal.current(device_id)
        if not record or record.get("phase") in TERMINAL:
            return False
        worker = self.workers.get(device_id)
        # Only the live creator can authorize its exact queued job. This lease is
        # lost on restart/disconnect and never persisted or restored.
        if (
            record.get("phase") == "QUEUED"
            and worker
            and not worker.stop.is_set()
            and worker.allowed_job == record.get("job_id")
            and worker.socket
            and worker.socket.connected
            and not worker.lock_lost.is_set()
        ):
            first = self.scheduler.next_job_for_device(device_id)
            return first is None or first.job_id != worker.allowed_job
        return True

    def _create(self, device_id, config):
        worker = self.worker_factory(
            device_id,
            config,
            assignments=self.assignments,
            accounts=self.accounts,
            workflows=self.workflows,
            scheduler=self.scheduler,
            executor=self.executor,
            journal=self.journal,
            balance_provider=self.balance_provider,
            client=self.client_factory(config["withdraw_api"]["base_url"]),
            screenshot_root=self.service.screenshot_store.root,
            credentials=self.credential_store.load,
        )
        self.workers[device_id] = worker
        if self.enabled:
            worker.start()
        return worker

    def status(self, device_id):
        worker = self.workers.get(device_id)
        if worker:
            return worker.status()
        record = self.journal.current(device_id)
        config = self.assignments.integration(device_id)["withdraw_api"]
        blocked = bool(record and record.get("phase") not in TERMINAL)
        return {
            "device_id": device_id,
            "operator_id": config.get("operator_id"),
            "username": config.get("username"),
            "state": "NEEDS_ATTENTION" if blocked else "DISABLED",
            "session_state": "NEEDS_ATTENTION" if blocked else "DISABLED",
            "error": record.get("error") if blocked else None,
            "socket_connected": False,
            "phase": record.get("phase"),
            "job_id": record.get("job_id"),
            "withdraw_id": record.get("withdraw_id"),
            "tx_id": record.get("tx_id"),
            "balance_state": "UNKNOWN",
            "balance_valid": False,
            "last_balance": None,
            "balance_observed_at": None,
        }

    def configure(self, device_id, config):
        with self.lock, self.scheduler.lock:
            current = self.journal.current(device_id)
            if current and current.get("phase") not in TERMINAL:
                raise RecordConflict(
                    "Reconcile the active withdrawal before changing its device configuration."
                )
            if (
                self.service.has_active_run(device_id)
                or device_id in self.scheduler.reservations
            ):
                raise RecordConflict(
                    "Wait for this device’s workflow to finish before changing configuration."
                )
            # Validate everything before stopping the existing worker.
            from .config import validate_configuration

            normalized = validate_configuration(config)
            if normalized["balance_workflow_id"]:
                flow = self.workflows.read(normalized["balance_workflow_id"])
                if not any(
                    step["type"] == "ocr_value"
                    and step.get("output") == "payer_balance"
                    for step in flow["steps"]
                ):
                    raise ValueError(
                        "Balance workflow must contain Read Balance output payer_balance."
                    )

            if normalized["withdraw_api"]["enabled"]:
                allowed = self.assignments.get(device_id)
                if not allowed:
                    raise ValueError("Assign a main payment workflow first.")
                if normalized["balance_workflow_id"] == allowed[0]:
                    raise ValueError(
                        "Balance-check workflow must be separate from the main payment workflow."
                    )
                payment_workflow(self.workflows.read(allowed[0]))
                if not any(
                    a.device_id == device_id and a.enabled for a in self.accounts.list()
                ):
                    raise ValueError("Assign an enabled local account first.")

            previous = self.workers.get(device_id)
            if previous:
                previous.stop.set()
                previous.wake.set()
        # Never join while holding scheduler.lock (the worker may be acquiring it).
        if previous:
            previous.close()
            if previous.thread and previous.thread.is_alive():
                raise RecordConflict(
                    "Previous worker is still stopping. Configuration was not replaced."
                )

        with self.lock, self.scheduler.lock:
            current = self.journal.current(device_id)
            if current and current.get("phase") not in TERMINAL:
                raise RecordConflict(
                    "Worker acquired a withdrawal while stopping; reconciliation is required."
                )

            normalized = self.assignments.set_integration(device_id, normalized)
            self.workers.pop(device_id, None)

            if normalized["withdraw_api"]["enabled"]:
                self._create(device_id, normalized)
            return normalized

    def _ensure_account_change_safe(self, device_id):
        current = self.journal.current(device_id)
        if current and current.get("phase") not in TERMINAL:
            raise RecordConflict(
                "This phone has unresolved withdrawal activity. Resolve it before changing the assigned account."
            )
        if (
            self.service.has_active_run(device_id)
            or device_id in self.scheduler.reservations
        ):
            raise RecordConflict(
                "This phone has active financial work. Wait for it to finish before changing the assigned account."
            )
        if any(
            getattr(job, "device_id", None) == device_id
            and (
                getattr(job, "recovery_status", None)
                in {"NEEDS_ATTENTION", "RECOVERING"}
                or (
                    getattr(job, "action", None) == "withdraw_v2"
                    and getattr(job, "status", None)
                    in {"QUEUED", "WAITING_FOR_DEVICE", "RUNNING"}
                )
            )
            for job in self.scheduler.list_jobs()
        ):
            raise RecordConflict(
                "This phone has unresolved withdrawal activity. Resolve it before changing the assigned account."
            )

    @staticmethod
    def _login_error(error):
        if isinstance(error, ValueError):
            return ValueError(
                "Unable to verify Google 2FA. Check the 2FA secret and system time."
            )
        if error.code == 401:
            return ValueError(
                "Invalid username or password, or Google 2FA was rejected."
            )
        if error.code == 403:
            return ValueError("This account is not authorized for Withdraw Bot.")
        if error.code == 429:
            return ValueError(
                "Too many login attempts. Please wait before trying again."
            )
        return ValueError("Unable to reach Withdraw API.")

    def login_and_assign(self, device_id, credentials):
        if not isinstance(credentials, dict) or set(credentials) != {
            "username",
            "password",
            "totp_secret",
        }:
            raise ValueError("Username, password and Google 2FA secret are required.")
        username = credentials.get("username")
        password = credentials.get("password")
        secret = credentials.get("totp_secret")
        if (
            not isinstance(username, str)
            or not username.strip()
            or len(username) > 200
            or any(ord(character) < 32 for character in username)
        ):
            raise ValueError("Username is required.")
        if not isinstance(password, str) or not password or len(password) > 4096:
            raise ValueError("Password is required.")
        if not isinstance(secret, str) or not secret.strip() or len(secret) > 256:
            raise ValueError("Google 2FA secret is required.")

        self._ensure_account_change_safe(device_id)
        current = self.assignments.integration(device_id)
        api = current["withdraw_api"]
        base_url = api.get("base_url") or os.environ.get("WITHDRAW_API_BASE_URL")
        if not base_url:
            raise ValueError("Withdraw API host is not configured on this server.")

        client = self.client_factory(base_url)
        try:
            try:
                profile = client.login(username.strip(), password, secret, None)
            except (APIError, ValueError) as error:
                raise self._login_error(error) from None

            returned_username = profile.get("username")
            operator_id = profile.get("id")
            if (
                profile.get("role") != "operator"
                or type(operator_id) is not int
                or operator_id <= 0
            ):
                raise ValueError("This account is not authorized for Withdraw Bot.")
            if (
                not isinstance(returned_username, str)
                or not returned_username
                or len(returned_username) > 200
                or any(ord(character) < 32 for character in returned_username)
            ):
                raise ValueError("Withdraw API login response is missing the username.")
            raw_records = profile.get("payment_records", [])
            if not isinstance(raw_records, list) or len(raw_records) > 100:
                raise ValueError(
                    "Withdraw API returned invalid payment record metadata."
                )
            payment_records = []
            for value in raw_records:
                if type(value) not in {str, int}:
                    raise ValueError(
                        "Withdraw API returned invalid payment record metadata."
                    )
                value = str(value)
                if (
                    not value
                    or len(value) > 80
                    or any(ord(char) < 32 for char in value)
                ):
                    raise ValueError(
                        "Withdraw API returned invalid payment record metadata."
                    )
                payment_records.append(value)

            for other_device, other in self.assignments.integrations().items():
                other_api = other["withdraw_api"]
                if (
                    other_device != device_id
                    and other_api.get("base_url") == client.origin
                    and other_api.get("operator_id") == operator_id
                ):
                    raise RecordConflict(
                        "This Withdraw account is already assigned to another phone."
                    )

            new_reference = self.credential_store.save(password, secret)
            replacement = {
                "balance_workflow_id": current["balance_workflow_id"],
                "withdraw_api": {
                    **api,
                    "enabled": api.get("enabled", False),
                    "base_url": client.origin,
                    "operator_id": operator_id,
                    "username": returned_username,
                    "credential_reference": new_reference,
                    "payment_records": payment_records,
                },
            }
            old_reference = api.get("credential_reference")
            try:
                self.configure(device_id, replacement)
            except Exception:
                self.credential_store.delete(new_reference)
                raise
            self.credential_store.delete(old_reference)
            return {
                "assigned": True,
                "operator_id": operator_id,
                "username": returned_username,
                "payment_records": payment_records,
                "credentials_saved": True,
            }
        finally:
            client.close()

    def workflow_assignment_changed(self, device_id, workflow_ids):
        """Stop an enabled worker before its main local route changes."""
        with self.lock, self.scheduler.lock:
            worker = self.workers.get(device_id)
            integration = self.assignments.integration(device_id)

            if integration["withdraw_api"]["enabled"]:
                if len(workflow_ids) != 1:
                    raise RecordConflict(
                        "An enabled Withdraw Bot requires one main payment workflow."
                    )
                if workflow_ids[0] == integration["balance_workflow_id"]:
                    raise RecordConflict(
                        "Balance-check workflow must be separate from the main payment workflow."
                    )
                payment_workflow(self.workflows.read(workflow_ids[0]))

            current = self.journal.current(device_id)
            if current and current.get("phase") not in TERMINAL:
                raise RecordConflict(
                    "Reconcile the active withdrawal before changing its main workflow."
                )
            if worker and (
                self.service.has_active_run(device_id)
                or device_id in self.scheduler.reservations
            ):
                raise RecordConflict(
                    "Wait for this device’s workflow to finish before changing its main workflow."
                )
            if worker:
                worker.stop.set()
                worker.wake.set()

        if worker:
            worker.close()
            if worker.thread and worker.thread.is_alive():
                raise RecordConflict(
                    "Previous worker is still stopping. Main workflow was not changed."
                )

    def reload_after_workflow_assignment(self, device_id):
        """Start a fresh worker after the new route was persisted."""
        with self.lock:
            old = self.workers.pop(device_id, None)
            if old is None:
                return
            config = self.assignments.integration(device_id)
            if config["withdraw_api"]["enabled"]:
                self._create(device_id, config)

    def reconcile(self, device_id):
        worker = self.workers.get(device_id)
        if worker is None or worker.thread is None or not worker.thread.is_alive():
            raise RecordConflict(
                "Enable a valid worker session before requesting external reconciliation."
            )
        worker.wake.set()
        return self.status(device_id)

    def close(self):
        for worker in self.workers.values():
            worker.stop.set()
            worker.wake.set()
        for worker in self.workers.values():
            worker.close()


def register_withdraw(
    app, payload, *, admin_required, ensure_registered_device, start_workers=False
):

    extensions = app.extensions
    manager = WithdrawManager(
        assignments=extensions["device_workflow_assignment_store"],
        accounts=extensions["account_binding_store"],
        workflows=extensions["workflow_storage"],
        service=extensions["workflow_service"],
        scheduler=extensions["job_scheduler"],
        executor=extensions["job_executor"],
        journal_path=Path(extensions["job_store"].path).with_name(
            "withdraw_journal.json"
        ),
        credential_store=WithdrawCredentialStore(
            Path(extensions["job_store"].path).with_name("withdraw_credentials.json")
        ),
        start_workers=start_workers,
    )
    extensions["withdraw_manager"] = manager

    @app.route("/api/devices/<device_id>/withdraw", methods=["GET", "PUT"])
    @admin_required
    def config(device_id):
        ensure_registered_device(device_id)
        if request.method == "PUT":
            manager.configure(device_id, payload())
        saved = manager.assignments.integration(device_id)
        api = saved["withdraw_api"]
        return jsonify(
            balance_workflow_id=saved["balance_workflow_id"],
            withdraw_api={
                key: api.get(key)
                for key in (
                    "enabled",
                    "base_url",
                    "operator_id",
                    "username",
                    "payment_records",
                    "balance_refresh_seconds",
                )
            },
            assigned=bool(api.get("operator_id") and api.get("username")),
            credentials_saved=bool(api.get("credential_reference")),
            status=manager.status(device_id),
        )

    @app.post("/api/devices/<device_id>/withdraw/login-assign")
    @admin_required
    def login_assign(device_id):
        ensure_registered_device(device_id)
        return jsonify(manager.login_and_assign(device_id, payload()))

    @app.post("/api/devices/<device_id>/withdraw/reconcile")
    @admin_required
    def reconcile(device_id):
        ensure_registered_device(device_id)
        if payload():
            raise ValueError("Reconciliation expects an empty JSON object.")
        return jsonify(manager.reconcile(device_id)), 202

    return manager
