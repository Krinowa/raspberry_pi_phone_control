"""Lifecycle and scheduler gate for independent, device-pinned workers."""

import logging

import os

import threading

from pathlib import Path


from automation.jobs.storage import RecordConflict, RecordNotFound

from .balance import (
    CameraOCRBalanceProvider,
    balance_check_workflow,
    final_amount as withdrawal_final_amount,
)

from .client import APIError, WithdrawClient

from .config import WithdrawCredentialStore

from .dispatcher import WithdrawDispatcher

from .journal import TERMINAL, WithdrawJournal

from .socket import SharedPusherTransport

from .worker import (
    WithdrawWorker,
    expiry,
    payment_workflow,
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

        self.account_binding_errors = {}

        self.lock = threading.RLock()

        self.dispatcher = WithdrawDispatcher(
            lambda: self.workers,
            scheduler,
            self.journal,
            ready_for_work=getattr(assignments, "ready_for_work", None),
        )

        self.workflow_completion_listener = self.workflow_completed

        add_completion_listener = getattr(service, "add_completion_listener", None)

        if callable(add_completion_listener):

            add_completion_listener(self.workflow_completion_listener)

        self.balance_provider = CameraOCRBalanceProvider(
            scheduler,
            workflows,
            service,
            lambda device: assignments.integration(device)["balance_workflow_id"],
        )

        scheduler.integration_gate = self.withdraw_job_blocked

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

                self._recover_interrupted_job(job)

        for device_id, config in assignments.integrations().items():

            if config["withdraw_api"]["enabled"]:

                self._create(device_id, config)

        if self.enabled:

            self.dispatcher.start()

    @staticmethod
    def _startup_recovery_fields(job):
        """Rebuild only identity that is durably and consistently stored on job."""

        metadata = (
            job.protected_metadata if isinstance(job.protected_metadata, dict) else {}
        )

        anchored = (
            metadata.get("version") == 1
            and metadata.get("assigned_device_id") == job.device_id
            and metadata.get("account_id") == job.account_id
            and metadata.get("tx_id") == job.external_ref
            and metadata.get("workflow_id") == job.workflow
        )

        raw = metadata.get("raw_withdrawal") if anchored else None

        if not isinstance(raw, dict):

            raw = None

        withdraw_id = metadata.get("withdraw_id") if anchored else None

        if type(withdraw_id) is not int or withdraw_id <= 0:

            withdraw_id = None

        if raw is not None and (
            raw.get("id") != withdraw_id or raw.get("tx_id") != job.external_ref
        ):

            raw = None

        operator_id = metadata.get("operator_id") if anchored else None

        lock_expires_at = metadata.get("lock_expires_at") if anchored else None

        final_amount = metadata.get("final_amount") if anchored else None

        if raw is not None and raw.get("lock_expires_at") != lock_expires_at:

            raw = None

            lock_expires_at = None

        if raw is not None:

            try:

                amount_matches = (
                    format(withdrawal_final_amount(raw), ".2f") == final_amount
                )

            except (TypeError, ValueError):

                amount_matches = False

            if not amount_matches:

                final_amount = None

        if final_amount is None and isinstance(job.payload, dict):

            amount = job.payload.get("amount")

            final_amount = amount if isinstance(amount, str) and amount else None

        missing = [
            name
            for name, value in (
                ("withdraw_id", withdraw_id),
                ("tx_id", job.external_ref),
                ("operator_id", operator_id),
                ("raw_withdrawal", raw),
                ("lock_expires_at", lock_expires_at),
                ("final_amount", final_amount),
            )
            if value is None
        ]

        return {
            "phase": "NEEDS_ATTENTION",
            "withdraw_id": withdraw_id,
            "tx_id": job.external_ref,
            "assigned_device_id": job.device_id,
            "operator_id": operator_id,
            "account_id": job.account_id,
            "workflow_id": job.workflow,
            "job_id": job.job_id,
            "retry_of_job_id": job.retry_of_job_id,
            "final_amount": final_amount,
            "raw_withdrawal": raw,
            "lock_expires_at": lock_expires_at,
            "recovery_identity_complete": not missing,
            "missing_recovery_fields": missing,
            "error": "Queued withdrawal was interrupted by restart and was not replayed.",
        }

    def _recover_interrupted_job(self, job):

        fields = self._startup_recovery_fields(job)

        current = self.journal.current(job.device_id)

        if current:

            identity_fields = (
                "withdraw_id",
                "tx_id",
                "assigned_device_id",
                "operator_id",
                "account_id",
                "workflow_id",
                "job_id",
            )

            conflicts = [
                name
                for name in identity_fields
                if current.get(name) is not None
                and fields.get(name) is not None
                and current[name] != fields[name]
            ]

            if conflicts:

                LOG.error(
                    "Startup recovery preserved conflicting journal for job %s; fields=%s",
                    job.job_id,
                    ",".join(conflicts),
                )

                return False

            self.journal.update(
                job.device_id,
                phase="NEEDS_ATTENTION",
                job_id=job.job_id,
                workflow_id=current.get("workflow_id") or job.workflow,
                account_id=current.get("account_id") or job.account_id,
                error=fields["error"],
            )

            return True

        try:

            self.journal.recover_missing(job.device_id, **fields)

        except RecordConflict as error:

            LOG.error(
                "Startup recovery did not overwrite journal for job %s: %s",
                job.job_id,
                error,
            )

            return False

        return True

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

        if worker is None or workflow_id != assigned_workflow_id:

            return

        outputs = snapshot.get("outputs")

        if not isinstance(outputs, dict) or "payer_balance" not in outputs:

            LOG.info(
                "balance_cache_rejected device=%s reason=missing_output",
                device_id,
            )

            return

        try:

            amount = parse_currency(outputs["payer_balance"])

        except ValueError:

            LOG.info(
                "balance_cache_rejected device=%s reason=invalid_output",
                device_id,
            )

            return

        LOG.info(
            "balance_read device=%s value=%s",
            device_id,
            format(amount, ".2f"),
        )

        worker.observe_balance(amount)

        LOG.info(
            "balance_cache_updated device=%s value=%s",
            device_id,
            format(amount, ".2f"),
        )

        self.dispatcher.wake()

    def set_work_readiness(self, device_id, ready):
        """Persist the operator gate without changing any active recovery state."""

        with self.scheduler.lock:

            previous = self.assignments.ready_for_work(device_id)

            current = self.assignments.set_ready_for_work(device_id, ready)

        if current and not previous:

            self.dispatcher.wake()

        return current

    def withdraw_job_blocked(self, device_id):

        record = self.journal.current(device_id)

        if not record or record.get("phase") in TERMINAL:

            return False

        worker = self.workers.get(device_id)

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
            account_binding_provisioner=lambda: self._provision_withdraw_account_binding(
                device_id,
                config["withdraw_api"]["operator_id"],
                config["withdraw_api"]["username"],
            ),
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

                invalidate = getattr(
                    worker,
                    "invalidate_balance_configuration",
                    None,
                )

                if callable(invalidate):

                    invalidate()

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

            has_account = any(
                account.device_id == device_id and account.enabled
                for account in self.accounts.list()
            )

            if has_account:

                with self.lock:

                    self.account_binding_errors.pop(device_id, None)

            else:

                with self.lock:

                    provisioning_error = self.account_binding_errors.get(device_id)

                if provisioning_error:

                    raise ValueError(provisioning_error)

                raise ValueError("Assign an enabled local account to this device.")

            payment_workflow(self.workflows.read(allowed[0]))

            balance_ready, balance_error = self.balance_readiness(device_id)

            if not balance_ready:

                raise ValueError(balance_error)

        except (KeyError, RecordNotFound, ValueError) as error:

            return False, str(error)

        return True, None

    def _ensure_withdraw_account_binding(
        self,
        device_id,
        operator_id,
        username,
    ):
        """Keep the existing job router usable for a validated Withdraw operator."""

        account_id = f"withdraw_operator_{operator_id}"

        bindings = self.accounts.list()

        assigned = next(
            (account for account in bindings if account.device_id == device_id),
            None,
        )

        if assigned is not None:

            if assigned.account_id == account_id and assigned.enabled:

                return assigned

            if assigned.account_id.startswith("withdraw_operator_"):

                current = self.journal.current(device_id)

                if (
                    current
                    and current.get("phase") not in TERMINAL
                    and (
                        current.get("operator_id") not in (None, operator_id)
                        or current.get("account_id") not in (None, account_id)
                    )
                ):

                    raise RecordConflict(
                        "The unresolved withdrawal is tied to a different local "
                        "account identity; reconcile it before replacing the binding."
                    )

                return self.accounts.replace_device_binding(
                    device_id,
                    {
                        "account_id": account_id,
                        "display_name": username,
                        "provider": None,
                        "device_id": device_id,
                        "enabled": True,
                    },
                )

            if assigned.enabled:

                return assigned

            raise RecordConflict(
                f"Local account {assigned.account_id!r} for {device_id} is disabled; "
                "enable it before using Withdraw Bot."
            )

        conflict = next(
            (
                account
                for account in bindings
                if account.account_id == account_id and account.device_id != device_id
            ),
            None,
        )

        if conflict is not None:

            raise RecordConflict(
                f"Withdraw compatibility account {account_id!r} is already assigned "
                f"to {conflict.device_id}; refusing to overwrite it."
            )

        try:

            return self.accounts.create(
                {
                    "account_id": account_id,
                    "display_name": username,
                    "provider": None,
                    "device_id": device_id,
                    "enabled": True,
                }
            )

        except RecordConflict:

            bindings = self.accounts.list()

            assigned = next(
                (
                    account
                    for account in bindings
                    if account.device_id == device_id and account.enabled
                ),
                None,
            )

            if assigned is not None:

                return assigned

            conflict = next(
                (account for account in bindings if account.account_id == account_id),
                None,
            )

            if conflict is not None and conflict.device_id != device_id:

                raise RecordConflict(
                    f"Withdraw compatibility account {account_id!r} is already "
                    f"assigned to {conflict.device_id}; refusing to overwrite it."
                ) from None

            raise

    def _provision_withdraw_account_binding(
        self,
        device_id,
        operator_id,
        username,
    ):
        """Attempt local routing setup without affecting the external session."""

        try:

            self._ensure_withdraw_account_binding(
                device_id,
                operator_id,
                username,
            )

        except Exception as error:

            if isinstance(
                error,
                (
                    OSError,
                    TypeError,
                    ValueError,
                ),
            ):

                detail = str(error) or type(error).__name__

            else:

                LOG.exception(
                    "Unexpected Withdraw account binding failure device_id=%s",
                    device_id,
                )

                detail = "local account binding persistence failed"

            message = f"Local payment routing could not be initialized: {detail}"

            with self.lock:

                self.account_binding_errors[device_id] = message

            LOG.error("%s", message)

        else:

            message = None

            with self.lock:

                self.account_binding_errors.pop(device_id, None)

            self.dispatcher.wake()

        worker = self.workers.get(device_id)

        set_error = getattr(worker, "set_account_binding_error", None)

        if callable(set_error):

            set_error(message)

        return message

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

    def configure(
        self,
        device_id,
        config,
        *,
        authenticated_client=None,
    ):

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

            normalized = self.assignments.set_integration(
                device_id,
                normalized,
            )

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
                "This phone has unresolved withdrawal activity. "
                "Resolve it before changing the assigned account."
            )

        if (
            self.service.has_active_run(device_id)
            or device_id in self.scheduler.reservations
        ):
            raise RecordConflict(
                "This phone has active financial work. "
                "Wait for it to finish before changing the assigned account."
            )

        if any(
            getattr(job, "device_id", None) == device_id
            and getattr(job, "action", None) == "withdraw_v2"
            and getattr(job, "status", None)
            in {
                "QUEUED",
                "WAITING_FOR_DEVICE",
                "RUNNING",
            }
            for job in self.scheduler.list_jobs()
        ):
            raise RecordConflict(
                "This phone has active withdrawal work. "
                "Wait for it to finish before changing the assigned account."
            )

    @staticmethod
    def _same_withdraw_identity(
        api,
        origin,
        operator_id,
    ):
        """Compare the stable external identity, not its editable display name."""

        return bool(
            api.get("operator_id") == operator_id and api.get("base_url") == origin
        )

    def _reauthenticate_same_account(
        self,
        device_id,
        config,
        client,
    ):
        """Refresh one assigned operator without invoking account-change guards."""

        with self.lock:

            normalized = self.assignments.set_integration(
                device_id,
                config,
            )

            worker = self.workers.get(device_id)

            if (
                worker is not None
                and worker.thread is not None
                and worker.thread.is_alive()
            ):

                worker.adopt_authenticated_client(
                    client,
                    normalized,
                )

                self.dispatcher.wake()

                return normalized

            stale = worker

            if stale is not None:

                stale.stop.set()

                stale.wake.set()

        if stale is not None:

            stale.close()

        with self.lock:

            current = self.workers.get(device_id)

            if current is stale or current is None:

                self.workers.pop(device_id, None)

                self._create(
                    device_id,
                    normalized,
                    client=client,
                )

            else:

                if current.thread and current.thread.is_alive():

                    current.adopt_authenticated_client(
                        client,
                        normalized,
                    )

                else:

                    raise RecordConflict(
                        "Withdraw session is still stopping; "
                        "reconnect again shortly."
                    )

            self.dispatcher.wake()

            return normalized

    @staticmethod
    def _normalize_g2f_code(value):
        """Validate one current six-digit Google Authenticator code."""

        if not isinstance(value, str):

            raise ValueError("Google 2FA code must be exactly 6 digits.")

        code = value.strip()

        if len(code) != 6 or not code.isdigit():

            raise ValueError("Google 2FA code must be exactly 6 digits.")

        return code

    @staticmethod
    def _login_error(error):

        if isinstance(error, ValueError):

            return ValueError("Google 2FA code must be exactly 6 digits.")

        if error.code == 401:

            return ValueError(
                "Invalid username or password, or Google 2FA code was rejected."
            )

        if error.code == 403:

            return ValueError("This account is not authorized for Withdraw Bot.")

        if error.code == 429:

            return ValueError(
                "Too many login attempts. Please wait before trying again."
            )

        return ValueError("Unable to reach Withdraw API.")

    def login_and_assign(
        self,
        device_id,
        credentials,
    ):

        if not isinstance(credentials, dict) or set(credentials) != {
            "username",
            "password",
            "g2f_code",
        }:

            raise ValueError(
                "Username, password and current Google 2FA code are required."
            )

        username = credentials.get("username")

        password = credentials.get("password")

        g2f_code = self._normalize_g2f_code(credentials.get("g2f_code"))

        if (
            not isinstance(username, str)
            or not username.strip()
            or len(username) > 200
            or any(ord(character) < 32 for character in username)
        ):

            raise ValueError("Username is required.")

        if not isinstance(password, str) or not password or len(password) > 4096:

            raise ValueError("Password is required.")

        current = self.assignments.integration(device_id)

        api = current["withdraw_api"]

        base_url = os.environ.get("WITHDRAW_API_BASE_URL") or api.get("base_url")

        if not base_url:

            raise ValueError("Withdraw API host is not configured on this server.")

        client = self.client_factory(base_url)

        client_transferred = False

        try:

            try:

                profile = client.login(
                    username.strip(),
                    password,
                    g2f_code,
                    None,
                )

            except (APIError, ValueError) as error:

                raise self._login_error(error) from None

            returned_username = profile.get("username")

            operator_id = profile.get("id")

            # Validate the handed-off session before enabling the integration.

            # The six-digit OTP is intentionally never persisted.

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

            same_identity = self._same_withdraw_identity(
                api,
                client.origin,
                operator_id,
            )

            if not same_identity:

                self._ensure_account_change_safe(device_id)

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

            # A rotating Google Authenticator code must never be written to the

            # credential store. Fresh HTTP authentication requires a fresh OTP.

            old_reference = api.get("credential_reference")

            replacement = {
                "balance_workflow_id": current["balance_workflow_id"],
                "withdraw_api": {
                    **api,
                    "enabled": True,
                    "base_url": client.origin,
                    "operator_id": operator_id,
                    "username": returned_username,
                    "credential_reference": None,
                    "payment_records": payment_records,
                },
            }

            if same_identity:

                self._reauthenticate_same_account(
                    device_id,
                    replacement,
                    client,
                )

            else:

                self.configure(
                    device_id,
                    replacement,
                    authenticated_client=client,
                )

            self._provision_withdraw_account_binding(
                device_id,
                operator_id,
                returned_username,
            )

            client_transferred = True

            if old_reference:

                self.credential_store.delete(old_reference)

            return {
                "assigned": True,
                "operator_id": operator_id,
                "username": returned_username,
                "payment_records": payment_records,
                "credentials_saved": False,
            }

        finally:

            if not client_transferred:

                client.close()

    def workflow_assignment_changed(
        self,
        device_id,
        workflow_ids,
    ):
        """Stop an enabled worker before its main local route changes."""

        with self.lock, self.scheduler.lock:

            worker = self.workers.get(device_id)

            current = self.journal.current(device_id)

            if current and current.get("phase") not in TERMINAL:

                raise RecordConflict(
                    "Reconcile the active withdrawal before changing "
                    "its main workflow."
                )

            if worker and (
                self.service.has_active_run(device_id)
                or device_id in self.scheduler.reservations
            ):

                raise RecordConflict(
                    "Wait for this device’s workflow to finish "
                    "before changing its main workflow."
                )

            if worker:

                worker.stop.set()

                worker.wake.set()

        if worker:

            worker.close()

            if worker.thread and worker.thread.is_alive():

                raise RecordConflict(
                    "Previous worker is still stopping. "
                    "Main workflow was not changed."
                )

    def reload_after_workflow_assignment(
        self,
        device_id,
    ):
        """Start a fresh worker after the new route was persisted."""

        with self.lock:

            old = self.workers.pop(
                device_id,
                None,
            )

            if old is None:

                return

            config = self.assignments.integration(device_id)

            if config["withdraw_api"]["enabled"]:

                self._create(
                    device_id,
                    config,
                )

            self.dispatcher.wake()

    def reconcile(self, device_id):

        config = self.assignments.integration(device_id)

        api = config["withdraw_api"]

        if not api.get("enabled") or not all(
            api.get(key)
            for key in (
                "base_url",
                "operator_id",
                "username",
            )
        ):

            raise RecordConflict(
                "Enable and assign a valid Withdraw account before reconnecting."
            )

        with self.lock:

            worker = self.workers.get(device_id)

            if worker is None or worker.thread is None or not worker.thread.is_alive():

                raise RecordConflict(
                    "Withdraw authentication is not active. Enter your password and a fresh "
                    "6-digit Google 2FA code to reconnect."
                )

            if not worker.authentication_available():
                raise RecordConflict(
                    "Withdraw authentication expired. Enter your password and a fresh "
                    "6-digit Google 2FA code to reconnect."
                )

            worker.request_reconnect(force_reauthentication=False)

        self.dispatcher.wake()

        return self.status(device_id)

    def refresh_balance(
        self,
        device_id,
    ):

        config = self.assignments.integration(device_id)

        api = config["withdraw_api"]

        if not api.get("enabled") or not api.get("operator_id"):

            raise RecordConflict(
                "Assign and enable a Withdraw account " "before refreshing balance."
            )

        balance_ready, balance_error = self.balance_readiness(device_id)

        if not balance_ready:

            raise RecordConflict(balance_error)

        worker = self.workers.get(device_id)

        if worker is None or worker.thread is None or not worker.thread.is_alive():

            raise RecordConflict(
                "Reconnect the Withdraw account " "before refreshing balance."
            )

        if not worker.session_available():

            raise RecordConflict(
                "Reconnect the Withdraw account " "before refreshing balance."
            )

        worker.request_balance_refresh("manual")

        return self.status(device_id)

    def retry_job(
        self,
        job_id,
        mode,
    ):
        """Create a durable local retry and hand it to a session when available."""

        with self.lock, self.scheduler.lock:

            original = self.scheduler.get_job(job_id)

            if original.action != "withdraw_v2":

                return self.scheduler.create_retry(
                    job_id,
                    mode,
                )

            if original.status not in {
                "FAILED",
                "INTERRUPTED",
            }:

                raise RecordConflict("Only failed or interrupted jobs can be retried.")

            if mode == "restart" and original.recovery_status != "READY":

                raise RecordConflict(
                    "Phone is not verified ready. "
                    "Use Manual Assistance and Verify Phone Ready "
                    "before restarting the workflow."
                )

            if mode == "failed_step" and original.recovery_status not in {
                "READY",
                "NEEDS_ATTENTION",
            }:

                raise RecordConflict(
                    "The phone must be restored and marked ready " "before retrying."
                )

            current = self.journal.current(original.device_id)

            phase = current.get("phase")

            if phase in {
                "CONFIRMED",
                "CANCELLED",
            }:

                raise RecordConflict(
                    "This withdrawal cannot be retried because it "
                    "is already confirmed/cancelled."
                )

            if phase in {
                "RELEASED",
                "LOCK_REJECTED",
            }:

                raise RecordConflict(
                    "This withdrawal no longer has an active external "
                    "lock and cannot be retried."
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
                    "The persisted withdrawal state does not match " "this failed job."
                )

            workflow = self.workflows.read(original.workflow)

            steps = workflow.get(
                "steps",
                [],
            )

            if current.get("workflow_id") != original.workflow:

                raise RecordConflict(
                    "The current withdrawal does not reference "
                    "the failed job's workflow."
                )

            if mode == "failed_step":

                failed = original.failed_step_index

                if failed is None or not (0 <= failed < len(steps)):

                    raise RecordConflict(
                        "The failed workflow step is unavailable; "
                        "restart the workflow instead."
                    )

                if steps[failed].get("id") != original.failed_step_id:

                    raise RecordConflict(
                        "The failed workflow step no longer exists "
                        "at its original index; restart the workflow instead."
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
                    "Retry queued. Reconnect the Withdraw account "
                    "if execution does not resume automatically."
                ),
            )

            worker = self.workers.get(original.device_id)

            if worker is not None:

                try:

                    worker.authorize_retry(
                        original.job_id,
                        retry.job_id,
                    )

                except Exception:

                    LOG.info(
                        "withdraw_retry_handoff_deferred device=%s job=%s",
                        original.device_id,
                        retry.job_id,
                    )

                    wake = getattr(
                        worker,
                        "wake",
                        None,
                    )

                    if wake is not None:

                        wake.set()

            return retry

    def cancel_withdrawal(
        self,
        device_id,
        reason_code,
    ):

        reason = (
            CANCEL_REASONS.get(reason_code)
            if isinstance(
                reason_code,
                str,
            )
            else None
        )

        if reason is None:

            raise ValueError("Select a valid cancellation reason.")

        worker = self.workers.get(device_id)

        if worker is None:

            raise RecordConflict(
                "Reconnect the Withdraw account before " "cancelling this withdrawal."
            )

        worker.cancel_withdrawal(reason)

        return self.status(device_id)

    def close(self):

        remove_completion_listener = getattr(
            self.service,
            "remove_completion_listener",
            None,
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
    app,
    payload,
    *,
    admin_required,
    ensure_registered_device,
    start_workers=False,
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

    @app.route(
        "/api/devices/<device_id>/withdraw",
        methods=[
            "GET",
            "PUT",
        ],
    )
    @admin_required
    def config(device_id):

        ensure_registered_device(device_id)

        if request.method == "PUT":

            manager.configure(
                device_id,
                payload(),
            )

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

        return jsonify(
            manager.login_and_assign(
                device_id,
                payload(),
            )
        )

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

        return jsonify(
            manager.cancel_withdrawal(
                device_id,
                data["reason_code"],
            )
        )

    return manager
