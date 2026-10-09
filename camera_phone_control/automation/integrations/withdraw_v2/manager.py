"""Lifecycle and scheduler gate for independent, device-pinned workers."""

import logging
import os
import threading
from pathlib import Path

from automation.jobs.storage import RecordConflict, RecordNotFound
from .balance import CameraOCRBalanceProvider, balance_check_workflow
from .client import APIError, WithdrawClient
from .config import WithdrawCredentialStore
from .dispatcher import WithdrawDispatcher
from .journal import TERMINAL, WithdrawJournal
from .socket import SharedPusherTransport
from .worker import (
    WithdrawWorker,
    expiry,
    irreversible_step_indexes,
    payment_workflow,
    workflow_execution_signature,
)
from automation.workflows.values import parse_currency
from flask import jsonify, request

LOG = logging.getLogger("withdraw_v2")

CANCEL_REASONS = {
    "invalid_account": "invalid account number",
    "bank_rejected": "bank rejected payment",
    "unable_to_complete": "operator unable to complete payment",
}


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
        realtime_transport=None,
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
        self.realtime_transport = realtime_transport or SharedPusherTransport()
        self.enabled = start_workers
        self.workers = {}
        self.lock = threading.RLock()
        self.dispatcher = WithdrawDispatcher(
            lambda: self.workers, scheduler, self.journal
        )
        self.workflow_completion_listener = self.workflow_completed
        add_completion_listener = getattr(service, "add_completion_listener", None)
        if callable(add_completion_listener):
            add_completion_listener(self.workflow_completion_listener)
        # All automatic/manual reads still run through the ordinary workflow
        # service and its scheduler reservation; no OCR path bypasses it.
        self.balance_provider = CameraOCRBalanceProvider(
            scheduler,
            workflows,
            service,
            lambda device: assignments.integration(device)["balance_workflow_id"],
        )

        scheduler.integration_gate = self.blocked
        # Brand-new queued payments from a previous process are never replayed.
        # Operator-created retries remain gated for fresh-session adoption.
        # A journal write can fail after enqueue; scan jobs as well as the journal.
        for job in scheduler.list_jobs():
            if (
                job.action == "withdraw_v2"
                and job.status
                in {
                    "QUEUED",
                    "WAITING_FOR_DEVICE",
                }
                and not job.retry_of_job_id
            ):
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
        if self.enabled:
            self.dispatcher.start()

    def workflow_completed(self, snapshot):
        """Publish a successful explicit balance read to its own device cache."""
        if not isinstance(snapshot, dict):
            return
        device_id = snapshot.get("device_id")
        workflow_id = snapshot.get("workflow_id")
        if not isinstance(device_id, str) or not device_id:
            return

        with self.lock:
            worker = self.workers.get(device_id)
            assigned_workflow_id = (
                worker.config.get("balance_workflow_id") if worker else None
            )
        # Ordinary payment and maintenance workflows must not affect balance.
        if worker is None or workflow_id != assigned_workflow_id:
            return

        outputs = snapshot.get("outputs")
        if not isinstance(outputs, dict) or "payer_balance" not in outputs:
            LOG.info(
                "balance_cache_rejected device=%s reason=missing_output", device_id
            )
            return

        # The Read Balance step has already succeeded once it publishes this
        # output. A later workflow step may still fail or be cancelled; that
        # must not discard the valid same-device observation.
        try:
            amount = parse_currency(outputs["payer_balance"])
        except ValueError:
            LOG.info(
                "balance_cache_rejected device=%s reason=invalid_output", device_id
            )
            return

        LOG.info("balance_read device=%s value=%s", device_id, format(amount, ".2f"))
        worker.observe_balance(amount)
        LOG.info(
            "balance_cache_updated device=%s value=%s",
            device_id,
            format(amount, ".2f"),
        )
        self.dispatcher.wake()

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

    def _create(self, device_id, config, *, client=None):
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
            client=client or self.client_factory(config["withdraw_api"]["base_url"]),
            screenshot_root=self.service.screenshot_store.root,
            realtime_transport=self.realtime_transport,
            dispatcher=self.dispatcher,
            credentials=self.credential_store.load,
        )
        self.workers[device_id] = worker
        if self.enabled:
            worker.start()
        return worker

    def status(self, device_id):
        payment_ready, payment_readiness_error = self.payment_readiness(device_id)
        balance_ready, balance_readiness_error = self.balance_readiness(device_id)
        worker = self.workers.get(device_id)
        if worker:
            status = worker.status()
            if not balance_ready:
                invalidate = getattr(worker, "invalidate_balance_configuration", None)
                if callable(invalidate):
                    invalidate()
                # A cached observation cannot be presented as usable while its
                # explicitly assigned reader is missing or fails the safety
                # contract. Payment dispatch is already blocked by the same
                # readiness check; keep the client-facing lifecycle fail-closed
                # as UNKNOWN as well.
                status.update(
                    balance_state="UNKNOWN",
                    balance_valid=False,
                    last_balance=None,
                    balance_observed_at=None,
                )
            status.update(
                payment_ready=payment_ready,
                payment_readiness_error=payment_readiness_error,
                balance_workflow_ready=balance_ready,
                balance_workflow_error=balance_readiness_error,
            )
            return status
        record = self.journal.current(device_id)
        config = self.assignments.integration(device_id)["withdraw_api"]
        blocked = bool(record and record.get("phase") not in TERMINAL)
        return {
            "device_id": device_id,
            "operator_id": config.get("operator_id"),
            "username": config.get("username"),
            "state": "NEEDS_ATTENTION" if blocked else "DISABLED",
            "session_state": "NEEDS_ATTENTION" if blocked else "DISABLED",
            "reconciliation_complete": False,
            "error": record.get("error") if blocked else None,
            "physical_socket_connected": self.realtime_transport.physical_connected,
            "phase": record.get("phase"),
            "job_id": record.get("job_id"),
            "withdraw_id": record.get("withdraw_id"),
            "tx_id": record.get("tx_id"),
            "balance_state": "UNKNOWN",
            "balance_valid": False,
            "last_balance": None,
            "balance_observed_at": None,
            "cancel_allowed": False,
            "payment_ready": payment_ready,
            "payment_readiness_error": payment_readiness_error,
            "balance_workflow_ready": balance_ready,
            "balance_workflow_error": balance_readiness_error,
        }

    def payment_readiness(self, device_id):
        """Report local payment eligibility without affecting session lifecycle."""
        try:
            allowed = self.assignments.get(device_id)
            if not allowed or len(allowed) != 1:
                raise ValueError("Assign one main payment workflow to this device.")
            if not any(
                account.device_id == device_id and account.enabled
                for account in self.accounts.list()
            ):
                raise ValueError("Assign an enabled local account to this device.")
            payment_workflow(self.workflows.read(allowed[0]))

            balance_ready, balance_error = self.balance_readiness(device_id)
            if not balance_ready:
                raise ValueError(balance_error)
        except (KeyError, RecordNotFound, ValueError) as error:
            return False, str(error)
        return True, None

    def balance_readiness(self, device_id):
        """Validate the explicitly assigned, non-payment balance route."""
        try:
            balance_workflow_id = self.assignments.integration(device_id).get(
                "balance_workflow_id"
            )
            if not balance_workflow_id:
                raise ValueError("Assign a Balance Check Workflow to this device.")
            try:
                workflow = self.workflows.read(balance_workflow_id)
            except FileNotFoundError:
                raise ValueError(
                    "Assigned Balance Check Workflow was not found."
                ) from None
            balance_check_workflow(workflow)
        except (KeyError, RecordNotFound, ValueError) as error:
            return False, str(error)
        return True, None

    def configure(self, device_id, config, *, authenticated_client=None):
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
            # Account/session configuration is intentionally independent from
            # local payment readiness. The dispatcher validates the complete
            # route before it can lock a withdrawal.
            from .config import validate_configuration

            normalized = validate_configuration(config)
            if normalized.get("balance_workflow_id"):
                try:
                    balance_flow = self.workflows.read(
                        normalized["balance_workflow_id"]
                    )
                except FileNotFoundError:
                    raise ValueError(
                        "Assigned Balance Check Workflow was not found."
                    ) from None
                balance_check_workflow(balance_flow)

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
                self._create(
                    device_id,
                    normalized,
                    client=authenticated_client,
                )
            self.dispatcher.wake()
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
        client_transferred = False
        try:
            try:
                profile = client.login(username.strip(), password, secret, None)
            except (APIError, ValueError) as error:
                raise self._login_error(error) from None

            returned_username = profile.get("username")
            operator_id = profile.get("id")
            # Validate the handed-off session before persisting an enabled
            # integration. The worker must be able to reuse it immediately.
            expiry(profile.get("session_max_expires_at"))
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
                    # Login & Assign creates a usable logical Withdraw session.
                    # The normal server worker then registers it with the one
                    # dashboard-wide realtime transport immediately.
                    "enabled": True,
                    "base_url": client.origin,
                    "operator_id": operator_id,
                    "username": returned_username,
                    "credential_reference": new_reference,
                    "payment_records": payment_records,
                },
            }
            old_reference = api.get("credential_reference")
            try:
                self.configure(
                    device_id,
                    replacement,
                    authenticated_client=client,
                )
                client_transferred = True
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
            if not client_transferred:
                client.close()

    def workflow_assignment_changed(self, device_id, workflow_ids):
        """Stop an enabled worker before its main local route changes."""
        with self.lock, self.scheduler.lock:
            worker = self.workers.get(device_id)
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
            self.dispatcher.wake()

    def reconcile(self, device_id):
        config = self.assignments.integration(device_id)
        api = config["withdraw_api"]
        if not api.get("enabled") or not all(
            api.get(key)
            for key in ("base_url", "operator_id", "username", "credential_reference")
        ):
            raise RecordConflict(
                "Enable and assign a valid Withdraw account before reconnecting."
            )

        with self.lock:
            worker = self.workers.get(device_id)
            if worker is not None and worker.thread and worker.thread.is_alive():
                worker.request_reconnect(
                    force_reauthentication=not worker.session_available()
                )
                return self.status(device_id)
            if not self.enabled:
                raise RecordConflict(
                    "Withdraw workers are not enabled in this server process."
                )
            stale = worker
            if stale is not None:
                stale.stop.set()
                stale.wake.set()

        if stale is not None:
            stale.close()

        with self.lock:
            # A concurrent reconnect may already have replaced the stale worker.
            current = self.workers.get(device_id)
            if current is stale or current is None:
                self.workers.pop(device_id, None)
                worker = self._create(device_id, config)
            else:
                worker = current
                if worker.thread and worker.thread.is_alive():
                    worker.request_reconnect(
                        force_reauthentication=not worker.session_available()
                    )
                else:
                    raise RecordConflict(
                        "Withdraw session is still stopping; reconnect again shortly."
                    )
        self.dispatcher.wake()
        return self.status(device_id)

    def refresh_balance(self, device_id):
        config = self.assignments.integration(device_id)
        api = config["withdraw_api"]
        if not api.get("enabled") or not api.get("operator_id"):
            raise RecordConflict(
                "Assign and enable a Withdraw account before refreshing balance."
            )
        balance_ready, balance_error = self.balance_readiness(device_id)
        if not balance_ready:
            raise RecordConflict(balance_error)
        worker = self.workers.get(device_id)
        if worker is None or worker.thread is None or not worker.thread.is_alive():
            raise RecordConflict(
                "Reconnect the Withdraw account before refreshing balance."
            )
        if not worker.session_available():
            raise RecordConflict(
                "Reconnect the Withdraw account before refreshing balance."
            )
        worker.request_balance_refresh("manual")
        return self.status(device_id)

    def retry_job(self, job_id, mode):
        """Create a durable local retry and hand it to a session when available."""
        with self.lock, self.scheduler.lock:
            original = self.scheduler.get_job(job_id)
            if original.action != "withdraw_v2":
                return self.scheduler.create_retry(job_id, mode)
            if original.status not in {"FAILED", "INTERRUPTED"}:
                raise RecordConflict("Only failed or interrupted jobs can be retried.")
            if original.recovery_status not in {"READY", "NEEDS_ATTENTION"}:
                raise RecordConflict(
                    "The phone must be restored and marked ready before retrying."
                )

            current = self.journal.current(original.device_id)
            phase = current.get("phase")
            if phase in {"CONFIRMED", "CANCELLED"}:
                raise RecordConflict(
                    "This withdrawal cannot be retried because it is already confirmed/cancelled."
                )
            if phase in {"RELEASED", "LOCK_REJECTED"}:
                raise RecordConflict(
                    "This withdrawal no longer has an active external lock and cannot be retried."
                )
            if (
                (
                    current.get("job_id") != original.job_id
                    and current.get("retry_of_job_id") != original.job_id
                )
                or current.get("assigned_device_id") != original.device_id
                or current.get("tx_id") != original.external_ref
            ):
                raise RecordConflict(
                    "The persisted withdrawal state does not match this failed job."
                )

            workflow = self.workflows.read(original.workflow)
            steps = workflow.get("steps", [])
            recorded_step_ids = current.get("workflow_step_ids")
            current_step_ids = [step.get("id") for step in steps]
            recorded_irreversible = current.get("irreversible_step_indexes")
            current_irreversible = irreversible_step_indexes(workflow)
            if (
                current.get("workflow_id") != original.workflow
                or current.get("workflow_signature")
                != workflow_execution_signature(workflow)
                or recorded_step_ids != current_step_ids
                or recorded_irreversible != current_irreversible
            ):
                raise RecordConflict(
                    "Retry unavailable because the payment workflow changed or its irreversible boundary was not recorded for the original run. Reconcile the withdrawal first."
                )
            if len(recorded_irreversible) != 1:
                raise RecordConflict(
                    "Retry unavailable because the original payment workflow did not record exactly one irreversible payment-submission step."
                )
            if mode == "failed_step":
                failed = original.failed_step_index
                if failed is None or not (0 <= failed < len(steps)):
                    raise RecordConflict(
                        "The failed workflow step is unavailable; restart the workflow instead."
                    )
                if steps[failed].get("id") != original.failed_step_id:
                    raise RecordConflict(
                        "Retry unavailable because the payment workflow changed after the failure."
                    )
            elif mode == "restart":
                pass
            else:
                raise ValueError("Retry mode must be failed_step or restart.")

            retry = self.scheduler.create_retry(
                job_id,
                mode,
                withdraw_authorized=True,
            )
            self.journal.update(
                original.device_id,
                phase="QUEUED",
                job_id=retry.job_id,
                retry_of_job_id=original.job_id,
                error=(
                    "Retry queued. Reconnect the Withdraw account if execution "
                    "does not resume automatically."
                ),
            )

            # A still-live worker can continue its existing confirmation path.
            # Failure to hand off here is not a retry-creation failure: a fresh
            # worker can adopt the persisted retry after login/reconciliation.
            worker = self.workers.get(original.device_id)
            if worker is not None:
                try:
                    worker.authorize_retry(original.job_id, retry.job_id)
                except Exception:
                    LOG.info(
                        "withdraw_retry_handoff_deferred device=%s job=%s",
                        original.device_id,
                        retry.job_id,
                    )
                    wake = getattr(worker, "wake", None)
                    if wake is not None:
                        wake.set()
            return retry

    def cancel_withdrawal(self, device_id, reason_code):
        reason = (
            CANCEL_REASONS.get(reason_code) if isinstance(reason_code, str) else None
        )
        if reason is None:
            raise ValueError("Select a valid cancellation reason.")
        worker = self.workers.get(device_id)
        if worker is None:
            raise RecordConflict(
                "Reconnect the Withdraw account before cancelling this withdrawal."
            )
        worker.cancel_withdrawal(reason)
        return self.status(device_id)

    def close(self):
        remove_completion_listener = getattr(
            self.service, "remove_completion_listener", None
        )
        if callable(remove_completion_listener):
            remove_completion_listener(self.workflow_completion_listener)
        self.dispatcher.request_stop()
        for worker in self.workers.values():
            worker.stop.set()
            worker.wake.set()
        for worker in self.workers.values():
            worker.close()
        self.dispatcher.close()
        self.realtime_transport.close()


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

    @app.post("/api/devices/<device_id>/withdraw/balance/refresh")
    @admin_required
    def refresh_balance(device_id):
        ensure_registered_device(device_id)
        if payload():
            raise ValueError("Balance refresh expects an empty JSON object.")
        return jsonify(manager.refresh_balance(device_id)), 202

    @app.post("/api/devices/<device_id>/withdraw/cancel")
    @admin_required
    def cancel_withdrawal(device_id):
        ensure_registered_device(device_id)
        data = payload()
        if set(data) != {"reason_code"}:
            raise ValueError("Cancel Withdrawal expects a reason_code only.")
        return jsonify(manager.cancel_withdrawal(device_id, data["reason_code"]))

    return manager
