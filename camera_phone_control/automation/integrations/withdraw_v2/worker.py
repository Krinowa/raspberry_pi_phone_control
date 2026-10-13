"""One external session per logical phone; physical execution stays in jobs."""

import logging

import threading

import time

from datetime import datetime, timezone

from pathlib import Path

from automation.jobs.storage import RecordConflict, RecordNotFound

from automation.workflows.validation import resolve_variables

from automation.workflows.values import parse_currency

from .balance import BalanceCache, balance_check_workflow, final_amount

from .client import APIError

from .config import load_credentials

from .journal import TERMINAL

from .normalization import normalize_locked_withdrawal, retained_withdrawal

from .socket import PusherSocket

LOG = logging.getLogger("withdraw_v2")


class AuthenticationFailed(ValueError):
    """Stored credentials or assigned identity require operator intervention."""


def expiry(value):

    try:

        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if parsed.tzinfo is None:

            raise ValueError()

        return parsed.timestamp()

    except (AttributeError, TypeError, ValueError):

        raise ValueError("External lock/session expiry is invalid.") from None


def receipt_contract(workflow):
    """Require top-level success detection followed by a receipt screenshot."""

    steps = workflow.get("steps", [])

    screenshots = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("type") == "capture_screen"
    ]

    receipt_indexes = [
        index for index, step in screenshots if step.get("output_key") == "receipt_path"
    ]

    result = {
        "wait_for_text_found": any(
            step.get("type") == "wait_for_text" for step in steps
        ),
        "screenshot_found": bool(screenshots),
        "screenshot_output": (
            screenshots[-1][1].get("output_key") if screenshots else None
        ),
        "valid": False,
        "reason": None,
    }

    if not receipt_indexes:

        result["reason"] = "Add a Capture Screenshot step after the success check."

        return result

    has_ordered_pair = any(
        step.get("type") == "wait_for_text"
        for receipt_index in receipt_indexes
        for step in steps[:receipt_index]
    )

    if not has_ordered_pair:

        result["reason"] = "Add a success check before Capture Screenshot."

        return result

    result.update(
        wait_for_text_found=True,
        screenshot_found=True,
        screenshot_output="receipt_path",
        valid=True,
    )

    return result


def payment_workflow(workflow):

    if workflow.get("purpose", "payment") != "payment":

        raise ValueError("Only a Payment purpose workflow can be assigned for payment.")

    contract = receipt_contract(workflow)

    if not contract["valid"]:

        raise ValueError(contract["reason"])

    return workflow


class WithdrawWorker:

    def __init__(
        self,
        device_id,
        config,
        *,
        assignments,
        accounts,
        workflows,
        scheduler,
        executor,
        journal,
        balance_provider,
        client,
        screenshot_root,
        socket_factory=PusherSocket,
        realtime_transport=None,
        dispatcher=None,
        credentials=load_credentials,
        account_binding_provisioner=None,
        renew_seconds=300,
    ):

        self.device_id, self.config = device_id, config

        self.api = config["withdraw_api"]

        self.assignments, self.accounts, self.workflows = (
            assignments,
            accounts,
            workflows,
        )

        self.scheduler, self.executor, self.journal = scheduler, executor, journal

        self.balance_provider, self.client = balance_provider, client

        self.screenshot_root = Path(screenshot_root).resolve()

        self.socket_factory, self.credentials = socket_factory, credentials

        self.realtime_transport = realtime_transport

        self.dispatcher = dispatcher

        self.account_binding_provisioner = account_binding_provisioner

        self.account_binding_error = None

        self.cache = BalanceCache(
            device_id, refresh_seconds=self.api.get("balance_refresh_seconds", 60)
        )

        self.stop = threading.Event()

        self.wake = threading.Event()

        self.reauthenticate = threading.Event()

        self.lock = threading.RLock()

        self.withdrawal_operation = threading.Lock()

        self.thread = None

        self.socket = None

        self.pending_authenticated_client = None

        self.state = "STOPPED"

        self.error = None

        self.allowed_job = (
            None  # Runtime-only authorization; never restored after restart.
        )

        self.renew_seconds = renew_seconds

        self.renew_stop = threading.Event()

        self.renew_thread = None

        self.lock_lost = threading.Event()

        profile = getattr(client, "profile", None)

        self.session_max = (
            expiry(profile.get("session_max_expires_at"))
            if isinstance(profile, dict) and profile.get("session_max_expires_at")
            else 0
        )

        self.reconciled_generation = 0

        self.reconciliation_complete = False

        self.assigned = None

        self.retry_waiting_job_id = None

        self.retry_job_id = None

        self.balance_refreshing = False

        self.balance_refresh_request_reason = None

        self.balance_error = None

        self.balance_initial_attempted = False

        self.balance_pending_required = False

        self.balance_retry_at = 0.0

    def set_state(self, state, error=None):

        with self.lock:

            self.state, self.error = state, error

        current = self.journal.current(self.device_id)

        LOG.info(
            "Withdraw worker device_id=%s operator_id=%s withdraw_id=%s state=%s",
            self.device_id,
            self.api["operator_id"],
            current.get("withdraw_id"),
            state,
        )

    def status(self):

        current = self.journal.current(self.device_id)

        cancel_allowed = self.can_cancel_withdrawal(current)

        with self.lock:

            context = self.balance_context()

            if self.cache.available_balance is not None and not self.cache.belongs_to(
                context
            ):

                self.cache.clear()

                self.balance_initial_attempted = False

            valid = self.cache.valid(context=context)

            # Only an active run or an explicit operator request is surfaced as

            # Checking. Background demand may remain deferred without making

            # the dashboard look like a request is running forever.

            refreshing = bool(
                self.balance_refreshing
                or self.balance_refresh_request_reason == "manual"
            )

            balance_state = (
                "REFRESHING"
                if refreshing
                else (
                    "ERROR"
                    if self.balance_error
                    else (
                        "VALID"
                        if valid
                        else (
                            "STALE"
                            if self.cache.available_balance is not None
                            else "UNKNOWN"
                        )
                    )
                )
            )

            return {
                "device_id": self.device_id,
                "operator_id": self.api["operator_id"],
                "username": self.api["username"],
                "state": self.state,
                "session_state": (
                    self.socket.session_state if self.socket else "DISCONNECTED"
                ),
                "reconciliation_complete": self.reconciliation_complete,
                "error": self.error,
                "physical_socket_connected": bool(
                    self.socket and self.socket.physical_connected
                ),
                "withdraw_id": current.get("withdraw_id"),
                "tx_id": current.get("tx_id"),
                "phase": current.get("phase"),
                "job_id": current.get("job_id"),
                "balance_state": balance_state,
                "balance_valid": valid,
                "last_balance": (
                    format(self.cache.available_balance, ".2f")
                    if self.cache.available_balance is not None
                    else None
                ),
                "balance_observed_at": self.cache.observed_at,
                "balance_error": self.balance_error,
                "cancel_allowed": cancel_allowed,
            }

    def balance_context(self):
        """Identity of the payer whose balance the assigned workflow reads."""

        records = tuple(
            sorted(str(value) for value in self.api.get("payment_records", []))
        )

        return (
            self.device_id,
            self.api.get("base_url"),
            self.api.get("operator_id"),
            self.api.get("username"),
            records,
            self.config.get("balance_workflow_id"),
        )

    def balance_valid(self):

        with self.lock:

            return self.cache.valid(context=self.balance_context())

    def observe_balance(self, amount):
        """Publish one valid observation for the worker's current payer identity."""

        with self.lock:

            self.cache.observe(amount, context=self.balance_context())

            self.balance_error = None

            self.balance_initial_attempted = True

            self.balance_refresh_request_reason = None

            if self.balance_pending_required:

                self.balance_retry_at = time.monotonic() + self.cache.refresh_seconds

    def invalidate_balance_configuration(self):
        """Discard observations when the assigned reader is no longer trusted."""

        with self.lock:

            self.cache.clear()

            self.balance_error = None

            self.balance_initial_attempted = False

            self.balance_refresh_request_reason = None

    def request_balance_refresh(self, reason="manual"):
        """Queue one refresh; the worker executes it only when the phone is safe."""

        if reason not in {"manual", "pending"}:

            raise ValueError("Balance refresh reason is invalid.")

        with self.lock:

            if reason == "manual":

                self.balance_error = None

            if reason == "pending":

                self.balance_pending_required = True

                self.balance_retry_at = 0.0

            if self.balance_refresh_request_reason != "manual":

                self.balance_refresh_request_reason = reason

            already_active = self.balance_refreshing

        self.wake.set()

        return not already_active

    def note_pending_balance(self, reason):
        """Retain pending demand and schedule immediate or cadence-based OCR."""

        now = time.monotonic()

        with self.lock:

            was_required = self.balance_pending_required

            self.balance_pending_required = True

            if reason in {"unknown", "stale"}:

                self.balance_retry_at = 0.0

                if self.balance_refresh_request_reason != "manual":

                    self.balance_refresh_request_reason = "pending"

            elif reason == "insufficient" and not was_required:

                self.balance_retry_at = now + self.cache.refresh_seconds

        self.wake.set()

    def clear_pending_balance_requirement(self):

        with self.lock:

            self.balance_pending_required = False

            self.balance_retry_at = 0.0

            if self.balance_refresh_request_reason == "pending":

                self.balance_refresh_request_reason = None

    def _maybe_refresh_balance(self):
        """Run a deferred single-flight balance workflow when the device is idle."""

        context = self.balance_context()

        now = time.monotonic()

        with self.lock:

            if self.cache.available_balance is not None and not self.cache.belongs_to(
                context
            ):

                self.cache.clear()

                self.balance_initial_attempted = False

            reason = self.balance_refresh_request_reason

            if reason is None and not self.balance_initial_attempted:

                reason = "initial"

            elif (
                reason is None
                and self.balance_pending_required
                and now >= self.balance_retry_at
            ):

                reason = "pending"

            if reason is None or self.balance_refreshing:

                return False

            manual_recovery = reason == "manual"

            blocked = self.assigned is not None or self.withdrawal_operation.locked()

            blocked = blocked or self.state in {
                "DISPATCH_RESERVED",
                "PAYING",
                "CONFIRMING",
                "CONFIGURATION_ERROR",
                "AUTH_FAILED",
            }

            if not manual_recovery:

                blocked = blocked or self.state == "NEEDS_ATTENTION"

            if blocked:

                if manual_recovery:

                    self.balance_refresh_request_reason = None

                    self.balance_error = (
                        "The phone is busy with an active withdrawal operation."
                    )

                return False

        if not self.session_available():

            if manual_recovery:

                with self.lock:

                    if self.balance_refresh_request_reason == reason:

                        self.balance_refresh_request_reason = None

                    self.balance_error = (
                        "The Withdraw session and realtime connection must be "
                        "healthy before refreshing balance."
                    )

            return False

        ready, _ = self.balance_readiness()

        if not ready:

            if manual_recovery:

                with self.lock:

                    if self.balance_refresh_request_reason == reason:

                        self.balance_refresh_request_reason = None

                    self.balance_error = (
                        "The assigned Balance Check Workflow is not ready."
                    )

            return False

        with self.lock:

            if self.balance_refreshing:

                return False

            self.balance_refreshing = True

            if reason == "initial":

                self.balance_initial_attempted = True

        LOG.info("balance_refresh_started device=%s reason=%s", self.device_id, reason)

        deferred = False

        try:

            if manual_recovery:

                amount = self.balance_provider.get_available_balance(
                    self.device_id, manual_recovery=True
                )

            else:

                amount = self.balance_provider.get_available_balance(self.device_id)

            self.observe_balance(amount)

            LOG.info(
                "balance_refresh_completed device=%s value=%s",
                self.device_id,
                format(amount, ".2f"),
            )

        except RecordConflict as exc:

            # Work appeared after the eligibility check. Leave the request

            # queued only for automatic refreshes. An explicit operator request

            # must terminate instead of leaving the UI at Checking forever.

            deferred = not manual_recovery

            with self.lock:

                if reason == "initial":

                    self.balance_initial_attempted = False

                if manual_recovery:

                    self.balance_error = str(exc)

            return False

        except Exception as exc:

            LOG.warning(
                "balance_refresh_failed device=%s reason=%s error=%s",
                self.device_id,
                reason,
                exc,
            )

            with self.lock:

                self.balance_error = str(exc)

        finally:

            with self.lock:

                self.balance_refreshing = False

                if not deferred and self.balance_refresh_request_reason == reason:

                    self.balance_refresh_request_reason = None

                if not deferred and self.balance_pending_required:

                    self.balance_retry_at = (
                        time.monotonic() + self.cache.refresh_seconds
                    )

            if self.dispatcher:

                self.dispatcher.wake()

            self.executor.wake()

        return True

    def start(self):

        self.thread = threading.Thread(
            target=self.run, name="withdraw-" + self.device_id, daemon=True
        )

        self.thread.start()

    def queue_changed(self):

        if self.dispatcher:

            self.dispatcher.wake()

    def login(self):

        try:

            password, secret = self.credentials(self.api["credential_reference"])

            profile = self.client.login(
                self.api["username"], password, secret, self.api["operator_id"]
            )

            self.session_max = expiry(profile["session_max_expires_at"])

            return profile

        except APIError as exc:

            if exc.code in {401, 403, 422}:

                raise AuthenticationFailed(
                    "Stored Withdraw credentials were rejected. Login again or reconnect after correcting the account."
                ) from None

            raise

        except ValueError:

            raise AuthenticationFailed(
                "Stored Withdraw credentials or session metadata are invalid. Login again to update the account."
            ) from None

    def connect_socket(self):

        if self.socket is None:

            kwargs = {
                "device_id": self.device_id,
                "on_queue_changed": self.queue_changed,
            }

            if self.realtime_transport is not None:

                kwargs["transport"] = self.realtime_transport

            self.socket = self.socket_factory(self.client, **kwargs)

        self.socket.connect(self.client.profile["realtime"])

        self.cache.invalidate()

        self.balance_initial_attempted = False

        self.reconciled_generation = 0

        self.reconciliation_complete = False

    def disconnect_socket(self):

        if self.socket:

            self.socket.close()

            self.socket = None

        self.cache.invalidate()

        self.reconciled_generation = 0

        self.reconciliation_complete = False

    def request_reconnect(self, *, force_reauthentication=False):
        """Wake this worker for reconciliation or a fresh credential login."""

        with self.lock:

            self.reconciliation_complete = False

            self.reconciled_generation = 0

            if force_reauthentication:

                self.reauthenticate.set()

        self.wake.set()

    def adopt_authenticated_client(self, client, config):
        """Queue a verified same-operator session for this worker to adopt."""

        profile = getattr(client, "profile", None)

        if not isinstance(profile, dict):

            raise ValueError("Authenticated Withdraw session is missing its profile.")

        session_max = expiry(profile.get("session_max_expires_at"))

        with self.lock:

            previous_pending = self.pending_authenticated_client

            self.pending_authenticated_client = (client, session_max)

            self.config = config

            self.api = config["withdraw_api"]

            self.reconciliation_complete = False

            self.reconciled_generation = 0

            self.state = "CONNECTING"

            self.error = None

            self.reauthenticate.clear()

            self.cache.clear()

            self.balance_initial_attempted = False

        if previous_pending is not None:

            previous_pending[0].close()

        self.wake.set()

    def action(self, name, **fields):

        if self.socket is None:

            raise APIError(0, "Withdrawal socket is disconnected.")

        return self.socket.action(name, **fields)

    def request_pending(self):

        data = self.action("pending")

        items = data.get("pending")

        if (
            not isinstance(items, list)
            or len(items) > 200
            or not all(isinstance(item, dict) for item in items)
        ):

            raise ValueError("Invalid pending-withdrawal response.")

        return items

    def get_my_locked(self):

        data = self.action("my-locked")

        if "withdrawal" not in data or (
            data["withdrawal"] is not None and not isinstance(data["withdrawal"], dict)
        ):

            raise ValueError("Invalid my-locked response.")

        return data["withdrawal"]

    def attention(self, message):

        self.allowed_job = None

        self.cache.invalidate()

        self.journal.update(self.device_id, phase="NEEDS_ATTENTION", error=message)

        self.set_state("NEEDS_ATTENTION", message)

    def authorize_retry(self, original_job_id, retry_job_id):
        """Authorize one validated retry while retaining this worker's lock lease."""

        with self.lock:

            if self.retry_waiting_job_id != original_job_id:

                raise RecordConflict(
                    "The live Withdraw worker is not waiting for this failed job."
                )

            if self.retry_job_id is not None:

                raise RecordConflict("A retry for this withdrawal is already active.")

            self.allowed_job = retry_job_id

            self.retry_job_id = retry_job_id

            self.error = None

        try:

            self.journal.update(
                self.device_id,
                phase="QUEUED",
                job_id=retry_job_id,
                error=None,
            )

        except Exception:

            with self.lock:

                if self.retry_job_id == retry_job_id:

                    self.retry_job_id = None

                    self.allowed_job = None

            raise

        self.set_state("PAYING")

        self.wake.set()

    def _pending_retry(self, current):
        job_id = current.get("job_id")
        if not job_id or current.get("phase") not in {"QUEUED", "NEEDS_ATTENTION"}:
            return None
        try:
            job = self.scheduler.get_job(job_id)
        except RecordNotFound:
            return None
        if (
            job.action != "withdraw_v2"
            or not job.retry_of_job_id
            or job.device_id != self.device_id
            or job.external_ref != current.get("tx_id")
            or job.status not in {"QUEUED", "WAITING_FOR_DEVICE", "RUNNING"}
        ):
            return None
        return job

    def _retry_identity_error(
        self, current, withdrawal, *, require_stored_fields=False
    ):
        """Return why an external item cannot represent this persisted retry."""
        if not isinstance(withdrawal, dict):
            return "The external withdrawal response is invalid."
        if withdrawal.get("id") != current.get("withdraw_id") or withdrawal.get(
            "tx_id"
        ) != current.get("tx_id"):
            return "The external withdrawal identity changed."
        if current.get("assigned_device_id") not in (None, self.device_id):
            return "The retry belongs to another phone."
        if current.get("operator_id") not in (None, self.api.get("operator_id")):
            return "The retry belongs to another Withdraw operator."

        expected = current.get("raw_withdrawal")
        expected = expected if isinstance(expected, dict) else {}
        expected_amount = current.get("final_amount")
        if expected_amount in (None, "") and expected:
            try:
                expected_amount = format(final_amount(expected), ".2f")
            except (TypeError, ValueError):
                return "The persisted withdrawal amount is invalid."
        if expected_amount not in (None, ""):
            try:
                actual_amount = format(final_amount(withdrawal), ".2f")
                persisted_amount = format(
                    final_amount({"final_amount": expected_amount}), ".2f"
                )
            except (TypeError, ValueError):
                return "The external withdrawal amount is invalid."
            if actual_amount != persisted_amount:
                return "The external withdrawal amount changed."

        # Pending payloads may be summaries. If a stable field is present there,
        # it must match. A successful lock response must retain every stable field
        # that existed on the original locked withdrawal.
        for field in (
            "payment_record",
            "order_id",
            "type",
            "account_number",
            "account_name",
            "holder_name",
            "bank_name",
            "mobile_no",
        ):
            expected_value = expected.get(field)
            if expected_value in (None, ""):
                continue
            actual_value = withdrawal.get(field)
            if actual_value in (None, ""):
                if require_stored_fields:
                    return f"The recovered withdrawal is missing {field}."
                continue
            if str(actual_value) != str(expected_value):
                return f"The external withdrawal {field} changed."

        profile = getattr(self.client, "profile", None)
        records = (
            profile.get("payment_records", []) if isinstance(profile, dict) else []
        )
        if not records:
            records = self.api.get("payment_records", [])
        payment_record = withdrawal.get("payment_record")
        if records and payment_record not in (None, ""):
            allowed = {str(value) for value in records}
            if str(payment_record) not in allowed:
                return "The withdrawal no longer belongs to this operator's payment records."
        return None

    def _retry_relock_context_error(self, current, job):
        """Require exact local failed-step identity before reacquiring a lease."""
        if type(current.get("withdraw_id")) is not int or current["withdraw_id"] <= 0:
            return "The persisted withdrawal ID is unavailable."
        if not isinstance(current.get("tx_id"), str) or not current["tx_id"]:
            return "The persisted transaction ID is unavailable."
        if current.get("assigned_device_id") != self.device_id:
            return "The recovery record belongs to another phone."
        if current.get("operator_id") != self.api.get("operator_id"):
            return "The recovery record belongs to another Withdraw operator."
        if not isinstance(current.get("raw_withdrawal"), dict):
            return "The original locked withdrawal details are unavailable."
        if current.get("final_amount") in (None, ""):
            try:
                format(final_amount(current["raw_withdrawal"]), ".2f")
            except (TypeError, ValueError):
                return "The original withdrawal amount is unavailable."

        if job.action != "withdraw_v2" or job.device_id != self.device_id:
            return "The queued job is not the original phone's Withdraw retry."
        if job.external_ref != current.get("tx_id"):
            return "The retry transaction identity cannot be proven."
        if not getattr(job, "retry_of_job_id", None):
            return "The retry parent identity cannot be proven."
        if job.status not in {"QUEUED", "WAITING_FOR_DEVICE"}:
            return "The retry is no longer safely waiting for external-lock recovery."

        start_index = getattr(job, "start_step_index", None)
        if type(start_index) is not int or start_index <= 0:
            return (
                "Expired-lock reacquisition is allowed only for a non-zero "
                "Retry Failed Step, not Restart Workflow."
            )

        workflow_id = current.get("workflow_id")
        if not workflow_id or workflow_id != getattr(job, "workflow", None):
            return "The retry workflow identity cannot be proven."
        account_id = getattr(job, "account_id", None)
        if not account_id:
            return "The retry payer-account identity cannot be proven."
        if current.get("account_id") not in (None, account_id):
            return "The retry payer-account identity changed."
        if current.get("retry_of_job_id") not in (None, job.retry_of_job_id):
            return "The retry parent identity changed."

        try:
            parent = self.scheduler.get_job(job.retry_of_job_id)
        except RecordNotFound:
            return "The retry parent job is unavailable."
        if (
            parent.action != "withdraw_v2"
            or parent.device_id != job.device_id
            or parent.external_ref != job.external_ref
            or getattr(parent, "account_id", None) != account_id
            or getattr(parent, "workflow", None) != getattr(job, "workflow", None)
        ):
            return "The retry no longer matches its original Withdraw job."
        if getattr(parent, "failed_step_index", None) != start_index:
            return "The retry is not the original failed-step continuation."
        return None

    def _retry_attention(self, message):
        """Fail recovery closed even if the attention journal write itself fails."""
        with self.lock:
            self.allowed_job = None
        try:
            self.attention(message)
        except Exception as exc:
            self.cache.invalidate()
            try:
                self.set_state("NEEDS_ATTENTION", message)
            except Exception:
                with self.lock:
                    self.state = "NEEDS_ATTENTION"
                    self.error = message
            LOG.error(
                "Withdraw retry attention persistence failed device=%s error=%s",
                self.device_id,
                exc,
            )
        return False

    def _relock_attempt_state(self, current, job):
        """Return the durable relock outcome for this exact retry, if any."""
        attempted_job_id = current.get("relock_attempted_for_job_id")
        if attempted_job_id is None:
            return None
        if attempted_job_id != job.job_id:
            return None
        if current.get("relock_attempted_withdraw_id") != current.get(
            "withdraw_id"
        ) or current.get("relock_attempted_tx_id") != current.get("tx_id"):
            return "identity-conflict"
        return current.get("relock_outcome") or "attempting"

    def _record_relock_outcome(self, current, job, outcome):
        """Persist an outcome without weakening the pre-request attempt guard."""
        if self._relock_attempt_state(current, job) in (None, "identity-conflict"):
            return current
        try:
            self.journal.update(
                self.device_id,
                relock_outcome=outcome,
            )
        except Exception as exc:
            # The already-durable ``attempting`` marker is deliberately retained.
            LOG.error(
                "Withdraw relock outcome persistence failed device=%s job=%s outcome=%s error=%s",
                self.device_id,
                job.job_id,
                outcome,
                exc,
            )
        return self.journal.current(self.device_id)

    def _confirm_recovered_retry(self, current, job):
        """Resolve an already-approved withdrawal before any lock/adoption work."""
        if not current.get("tx_id") or not current.get("withdraw_id"):
            return False
        if not self.client.reconcile_confirmation(
            current["tx_id"], current["withdraw_id"]
        ):
            return False
        try:
            self.executor.cancel(job.job_id)
        except (RecordConflict, RecordNotFound):
            return self._retry_attention(
                "The withdrawal is externally confirmed, but its queued retry could not be cancelled safely."
            )
        self.stop_renewal()
        with self.lock:
            self.assigned = None
            self.allowed_job = None
            self.retry_job_id = None
            self.retry_waiting_job_id = None
        self.journal.update(self.device_id, phase="CONFIRMED", error=None)
        self.set_state("IDLE")
        return True

    def _adopt_pending_retry(self, current, locked=None):
        """Attach a persisted retry only after durable lease recovery succeeds."""
        job = self._pending_retry(current)
        if job is None:
            return False
        context_error = self._retry_relock_context_error(current, job)
        if context_error:
            return self._retry_attention(context_error)
        if locked is None:
            locked = self.get_my_locked()
        identity_error = self._retry_identity_error(
            current, locked, require_stored_fields=True
        )
        if identity_error:
            return self._retry_attention(
                "Retry is queued, but this session does not own the exact persisted external withdrawal. "
                + identity_error
            )
        raw = retained_withdrawal(locked)
        if expiry(raw.get("lock_expires_at")) <= time.time():
            return self._retry_attention(
                "Retry is queued, but the external withdrawal lock has expired. Reconciliation is required before continuing."
            )

        # Durable state first. Until this succeeds allowed_job remains None, so
        # the scheduler integration gate must continue blocking the retry.
        try:
            changes = {
                "phase": "QUEUED",
                "job_id": job.job_id,
                "retry_of_job_id": job.retry_of_job_id,
                "account_id": getattr(job, "account_id", current.get("account_id")),
                "workflow_id": getattr(job, "workflow", current.get("workflow_id")),
                "raw_withdrawal": raw,
                "lock_expires_at": raw.get("lock_expires_at"),
                "assigned_device_id": self.device_id,
                "operator_id": self.api["operator_id"],
                "error": None,
            }
            if self._relock_attempt_state(current, job) not in (
                None,
                "identity-conflict",
            ):
                changes["relock_outcome"] = "locked"
            self.journal.update(self.device_id, **changes)
        except Exception as exc:
            with self.lock:
                self.assigned = None
                self.allowed_job = None
                self.retry_job_id = None
            self.cache.invalidate()
            message = (
                "Recovered external lock could not be persisted safely. "
                "The retry remains blocked."
            )
            try:
                self.set_state("NEEDS_ATTENTION", message)
            except Exception:
                with self.lock:
                    self.state = "NEEDS_ATTENTION"
                    self.error = message
            LOG.error(
                "Withdraw retry adoption persistence failed device=%s job=%s error=%s",
                self.device_id,
                job.job_id,
                exc,
            )
            return False

        # Install renewal before runtime authorization. If startup fails, return
        # the durable journal to attention and never wake the executor.
        try:
            self._start_renewal()
            if self.renew_thread is None or not self.renew_thread.is_alive():
                raise RuntimeError("Withdraw lock renewal thread did not start.")
        except Exception as exc:
            self.stop_renewal()
            with self.lock:
                self.assigned = None
                self.allowed_job = None
                self.retry_job_id = None
            message = (
                "Recovered external lock could not start renewal safely. "
                "The retry remains blocked."
            )
            try:
                self.journal.update(
                    self.device_id, phase="NEEDS_ATTENTION", error=message
                )
            except Exception as persist_exc:
                LOG.error(
                    "Withdraw retry renewal-failure persistence failed device=%s job=%s error=%s",
                    self.device_id,
                    job.job_id,
                    persist_exc,
                )
            self.cache.invalidate()
            try:
                self.set_state("NEEDS_ATTENTION", message)
            except Exception:
                with self.lock:
                    self.state = "NEEDS_ATTENTION"
                    self.error = message
            LOG.error(
                "Withdraw retry adoption renewal startup failed device=%s job=%s error=%s",
                self.device_id,
                job.job_id,
                exc,
            )
            return False

        # Runtime authorization is last and is installed while the scheduler lock
        # is held so no claimant can observe a half-installed handoff.
        with self.scheduler.lock, self.lock:
            self.assigned = (raw, None)
            self.allowed_job = job.job_id
            self.retry_job_id = job.job_id
            self.error = None
        self.set_state("PAYING")
        self.executor.wake()
        self.wake.set()
        return True

    def _reconcile_uncertain_relock(self, current, job, error):
        """Reconcile a possibly-applied client-lock without issuing another lock."""
        LOG.warning(
            "Withdraw retry relock outcome is uncertain device=%s job=%s error=%s",
            self.device_id,
            job.job_id,
            error,
        )
        current = self._record_relock_outcome(current, job, "uncertain")
        try:
            if self._confirm_recovered_retry(current, job):
                return True
        except Exception as exc:
            return self._retry_attention(
                "The recovery lock result was uncertain and approval status could not be verified: "
                + str(exc)
            )
        try:
            locked = self.get_my_locked()
        except Exception as exc:
            return self._retry_attention(
                "The recovery lock result was uncertain and current lock ownership could not be verified: "
                + str(exc)
            )
        if locked is not None:
            identity_error = self._retry_identity_error(
                current, locked, require_stored_fields=True
            )
            if identity_error:
                return self._retry_attention(
                    "The recovery lock result was uncertain and this session now owns a different withdrawal. "
                    + identity_error
                )
            return self._adopt_pending_retry(current, locked)

        # Pending is diagnostic only after an uncertain lock outcome. Even if the
        # exact item is visible, never issue a second lock in this recovery call.
        try:
            pending = self.request_pending()
        except Exception as exc:
            return self._retry_attention(
                "The recovery lock result was uncertain and pending state could not be verified: "
                + str(exc)
            )
        exact_pending = [
            item
            for item in pending
            if item.get("id") == current.get("withdraw_id")
            and item.get("tx_id") == current.get("tx_id")
            and self._retry_identity_error(current, item) is None
        ]
        if exact_pending:
            return self._retry_attention(
                "The recovery lock result was uncertain. The withdrawal is pending again, "
                "but automatic relock is blocked for this retry. Manual recovery is required."
            )
        return self._retry_attention(
            "The recovery lock result was uncertain and external ownership could not be proven. The retry remains blocked."
        )

    def _recover_pending_retry(self, current, locked=None):
        """Recover one exact persisted failed-step retry after its lock is lost."""
        job = self._pending_retry(current)
        if job is None:
            return False
        if not self.withdrawal_operation.acquire(blocking=False):
            return False
        try:
            with self.lock:
                if self.assigned is not None or self.balance_refreshing:
                    return False

            # Prove that this is the exact non-zero failed-step continuation
            # before any approved, owned-lock, pending, or relock recovery path.
            context_error = self._retry_relock_context_error(current, job)
            if context_error:
                return self._retry_attention(context_error)

            # Approved-first ordering is mandatory: an already-paid withdrawal
            # must never be reacquired because a local retry still exists.
            if self._confirm_recovered_retry(current, job):
                return True

            if locked is None:
                locked = self.get_my_locked()
            if locked is not None:
                identity_error = self._retry_identity_error(
                    current, locked, require_stored_fields=True
                )
                if identity_error:
                    return self._retry_attention(
                        "This session owns a different external withdrawal lock. "
                        + identity_error
                    )
                return self._adopt_pending_retry(current, locked)

            pending = self.request_pending()
            matching_identity = [
                item
                for item in pending
                if item.get("id") == current.get("withdraw_id")
                and item.get("tx_id") == current.get("tx_id")
            ]
            exact = [
                item
                for item in matching_identity
                if self._retry_identity_error(current, item) is None
            ]
            if not exact:
                if matching_identity:
                    reason = self._retry_identity_error(current, matching_identity[0])
                    return self._retry_attention(
                        "The pending withdrawal no longer matches the persisted retry. "
                        + str(reason)
                    )
                return self._retry_attention(
                    "The withdrawal is no longer pending or could not be found for recovery."
                )
            if len(exact) != 1:
                return self._retry_attention(
                    "The pending withdrawal identity is ambiguous; recovery was not attempted."
                )

            attempt_state = self._relock_attempt_state(current, job)
            if attempt_state == "identity-conflict":
                return self._retry_attention(
                    "The persisted relock-attempt identity is inconsistent with this retry."
                )
            if attempt_state is not None:
                return self._retry_attention(
                    "An automatic relock was already attempted for this exact retry "
                    f"(outcome: {attempt_state}). External ownership must be reconciled manually."
                )

            # Persist the exact attempt identity before the external side effect.
            # A crash or timeout after this point must never cause another
            # automatic client-lock for this same retry/withdrawal identity.
            try:
                self.journal.update(
                    self.device_id,
                    relock_attempted_for_job_id=job.job_id,
                    relock_attempted_withdraw_id=current["withdraw_id"],
                    relock_attempted_tx_id=current["tx_id"],
                    relock_outcome="attempting",
                )
                current = self.journal.current(self.device_id)
            except Exception as exc:
                return self._retry_attention(
                    "The external-lock recovery attempt could not be recorded safely: "
                    + str(exc)
                )

            # Exactly one lock request in this recovery call. Any uncertain result
            # is reconciled through approved/my-locked/pending without a retry.
            try:
                response = self.action("lock", withdraw_id=current["withdraw_id"])
            except APIError as exc:
                if exc.code == 423:
                    current = self._record_relock_outcome(
                        current, job, "definite-conflict"
                    )
                    return self._retry_attention(
                        "The withdrawal was locked by another session before recovery could claim it."
                    )
                if exc.code == 404:
                    current = self._record_relock_outcome(
                        current, job, "definite-not-found"
                    )
                    return self._retry_attention(
                        "The withdrawal could not be found during lock recovery."
                    )
                if exc.code in {409, 422}:
                    current = self._record_relock_outcome(
                        current, job, "definite-unavailable"
                    )
                    return self._retry_attention(
                        "The withdrawal is no longer available for lock recovery."
                    )
                if exc.code in {401, 403}:
                    raise
                return self._reconcile_uncertain_relock(current, job, exc)
            except Exception as exc:
                return self._reconcile_uncertain_relock(current, job, exc)

            if not isinstance(response, dict):
                return self._reconcile_uncertain_relock(
                    current, job, ValueError("Invalid recovery lock response.")
                )
            try:
                raw = retained_withdrawal(response.get("withdrawal"))
            except Exception as exc:
                return self._reconcile_uncertain_relock(current, job, exc)
            identity_error = self._retry_identity_error(
                current, raw, require_stored_fields=True
            )
            if identity_error:
                return self._retry_attention(
                    "The recovered lock does not match the persisted withdrawal. "
                    + identity_error
                )
            try:
                if expiry(raw.get("lock_expires_at")) <= time.time() + 30:
                    return self._retry_attention(
                        "The recovered external lock has insufficient remaining time."
                    )
            except (TypeError, ValueError) as exc:
                # The server may already have applied the lock even if the expiry
                # in its reply is malformed; reconcile ownership, never relock.
                return self._reconcile_uncertain_relock(current, job, exc)
            return self._adopt_pending_retry(current, raw)
        finally:
            self.withdrawal_operation.release()

    def can_cancel_withdrawal(self, current=None):
        """Allow external cancellation only before payment execution has started."""

        current = current or self.journal.current(self.device_id)

        if (
            current.get("phase") not in {"LOCKED", "NEEDS_ATTENTION", "QUEUED"}
            or type(current.get("withdraw_id")) is not int
            or current.get("assigned_device_id") != self.device_id
        ):

            return False

        job_id = current.get("job_id")

        # Withdrawal is locked but no local payment job exists yet.

        if not job_id:

            return True

        # A retry belongs to a withdrawal whose earlier payment execution already

        # failed/interrupted. Do not guess whether money may already have moved.

        if current.get("retry_of_job_id"):

            return False

        try:

            job = self.scheduler.get_job(job_id)

        except RecordNotFound:

            return False

        # The original local payment job has not started physical execution yet.

        return job.status in {"QUEUED", "WAITING_FOR_DEVICE"}

    def _wait_for_retry(self, job):
        """Keep renewing the lock while the operator restores and resumes safely."""

        message = (
            "Payment workflow failed. Restore the phone, then retry from a safe step "
            "while this Withdraw session retains the external lock."
        )

        with self.lock:

            self.retry_waiting_job_id = job.job_id

            self.retry_job_id = None

        self.attention(message)

        try:

            while True:

                current = self.journal.current(self.device_id)

                if (
                    self.stop.is_set()
                    or self.lock_lost.is_set()
                    or not self.socket
                    or not self.socket.connected
                    or expiry(current.get("lock_expires_at")) <= time.time()
                ):

                    raise ValueError(
                        "External lock/session was lost while waiting for retry. Reconciliation is required."
                    )

                with self.lock:

                    retry_job_id = self.retry_job_id

                if retry_job_id is not None:

                    return self.scheduler.get_job(retry_job_id)

                self.wake.wait(0.2)

                self.wake.clear()

        finally:

            with self.lock:

                self.retry_waiting_job_id = None

                self.retry_job_id = None

    def reconcile(self):
        """Called after every subscription, before any pending/physical work."""
        self.cache.invalidate()
        current = self.journal.current(self.device_id)
        if current and current.get("phase") not in TERMINAL:
            pending_retry = self._pending_retry(current)
            if pending_retry is not None:
                return self._recover_pending_retry(current)

        locked = self.get_my_locked()
        if current and current.get("phase") not in TERMINAL:
            if current.get("tx_id") and current.get("withdraw_id"):
                if self.client.reconcile_confirmation(
                    current["tx_id"], current["withdraw_id"]
                ):
                    self.journal.update(self.device_id, phase="CONFIRMED", error=None)
                    if locked is None:
                        return True
            self.attention(
                "Previous withdrawal is unresolved. Review payment and external confirmation; do not repay."
            )
            return False
        if locked is not None:
            self.journal.begin(
                self.device_id,
                phase="NEEDS_ATTENTION",
                withdraw_id=locked.get("id"),
                tx_id=locked.get("tx_id"),
                raw_withdrawal=retained_withdrawal(locked),
            )
            self.attention(
                "An existing external lock has no safe local execution record. Manual reconciliation is required."
            )
            return False
        return True

    def _renew(self):

        while not self.renew_stop.wait(self.renew_seconds):

            try:

                current = self.journal.current(self.device_id)

                response = self.action("renew-lock", withdraw_id=current["withdraw_id"])

                until = expiry(response.get("lock_expires_at"))

                if until <= time.time():

                    raise ValueError("Renewed lock is already expired.")

                self.journal.update(
                    self.device_id, lock_expires_at=response["lock_expires_at"]
                )

            except Exception:

                self.lock_lost.set()

                return

    def stop_renewal(self):

        self.renew_stop.set()

        if self.renew_thread:

            self.renew_thread.join(timeout=12)

            self.renew_thread = None

    def _start_renewal(self):

        if self.renew_thread and self.renew_thread.is_alive():

            return

        self.lock_lost.clear()

        self.renew_stop.clear()

        self.renew_thread = threading.Thread(
            target=self._renew, name="withdraw-renew-" + self.device_id, daemon=True
        )

        self.renew_thread.start()

    def lock_withdrawal(self, item, *, claimed=False):

        if (
            type(item.get("id")) is not int
            or item["id"] <= 0
            or not isinstance(item.get("tx_id"), str)
            or not item["tx_id"]
        ):

            raise ValueError("Pending withdrawal identifiers are invalid.")

        if not claimed and self.journal.was_confirmed(self.device_id, item["tx_id"]):

            raise ValueError(
                "Previously confirmed transaction appeared in pending; manual review is required."
            )

        if not claimed:

            self.journal.begin(
                self.device_id,
                phase="LOCK_INTENT",
                withdraw_id=item["id"],
                tx_id=item["tx_id"],
                assigned_device_id=self.device_id,
                operator_id=self.api["operator_id"],
                final_amount=format(final_amount(item), ".2f"),
            )

        response = self.action("lock", withdraw_id=item["id"])

        raw = retained_withdrawal(response.get("withdrawal"))

        # Keep the entire response before normalization, including unknown fields.

        self.journal.update(
            self.device_id,
            phase="LOCKED",
            raw_withdrawal=raw,
            lock_expires_at=raw.get("lock_expires_at"),
            assigned_device_id=self.device_id,
            operator_id=self.api["operator_id"],
        )

        if (
            raw.get("id") != item["id"]
            or raw.get("tx_id") != item["tx_id"]
            or final_amount(raw) != final_amount(item)
        ):

            raise ValueError(
                "Locked withdrawal does not match the selected pending transaction."
            )

        if expiry(raw.get("lock_expires_at")) <= time.time() + 30:

            raise ValueError("External lock has insufficient remaining time.")

        self._start_renewal()

        return raw

    def release_withdrawal(self):

        current = self.journal.current(self.device_id)

        if current.get("job_id"):

            raise ValueError(
                "Cannot release automatically after physical execution may have started."
            )

        self.action("release", withdraw_id=current["withdraw_id"])

        self.journal.update(self.device_id, phase="RELEASED")

        self.stop_renewal()

    def cancel_withdrawal(self, reason):

        # Explicit caller classification only; local failures never invoke this.

        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:

            raise ValueError(
                "A genuine withdrawal-problem reason (1–200 characters) is required."
            )

        if not self.withdrawal_operation.acquire(blocking=False):

            raise RecordConflict(
                "Withdrawal cancellation or payment is already in progress."
            )

        try:

            current = self.journal.current(self.device_id)

            if (
                type(current.get("withdraw_id")) is not int
                or current["withdraw_id"] <= 0
                or current.get("assigned_device_id") != self.device_id
            ):

                raise RecordConflict(
                    "This phone does not own an active withdrawal that can be cancelled."
                )

            if current.get("phase") not in {"LOCKED", "NEEDS_ATTENTION", "QUEUED"}:

                raise RecordConflict(
                    "Withdrawal cannot be cancelled in its current state."
                )

            if not self.can_cancel_withdrawal(current):

                raise RecordConflict(
                    "Withdrawal cannot be cancelled after physical payment may have started."
                )

            job_id = current.get("job_id")

            if job_id:

                job = self.scheduler.get_job(job_id)

                if job.status in {"QUEUED", "WAITING_FOR_DEVICE"}:

                    cancelled = self.executor.cancel(job_id)

                    if cancelled.status != "CANCELLED":

                        raise RecordConflict(
                            "The local retry already started and can no longer be cancelled safely."
                        )

            with self.lock:

                assignment = self.assigned

            if current.get("phase") == "LOCKED" and assignment is None:

                raise RecordConflict(
                    "The locked withdrawal is not currently owned by this worker."
                )

            try:

                self.action("cancel", withdraw_id=current["withdraw_id"], reason=reason)

                self.journal.update(self.device_id, phase="CANCELLED", error=None)

            except Exception:

                message = (
                    "Unable to confirm cancellation. This withdrawal remains "
                    "under manual attention."
                )

                self.attention(message)

                raise RecordConflict(message) from None

            self.cache.invalidate()

            self.stop_renewal()

            with self.lock:

                if self.assigned == assignment:

                    self.assigned = None

            if assignment is not None:

                _, reservation = assignment

                with self.scheduler.lock:

                    if self.scheduler.reservations.get(self.device_id) == reservation:

                        del self.scheduler.reservations[self.device_id]

            self.set_state("IDLE")

            if self.dispatcher:

                self.dispatcher.wake()

        finally:

            self.withdrawal_operation.release()

    def _route(self):

        allowed = self.assignments.get(self.device_id)

        if not allowed or len(allowed) != 1:

            raise ValueError("Assign one main payment workflow to this device.")

        account = next(
            (
                a
                for a in self.accounts.list()
                if a.device_id == self.device_id and a.enabled
            ),
            None,
        )

        if account is None:

            raise ValueError("Assign an enabled local account to this device.")

        return account, payment_workflow(self.workflows.read(allowed[0]))

    def set_account_binding_error(self, error):

        with self.lock:

            self.account_binding_error = error

    def payment_readiness(self):
        """Validate payment eligibility without changing the logical session."""

        try:

            self._route()

            self.set_account_binding_error(None)

            balance_ready, balance_error = self.balance_readiness()

            if not balance_ready:

                raise ValueError(balance_error)

        except (KeyError, ValueError) as error:

            with self.lock:

                provisioning_error = self.account_binding_error

            if provisioning_error:

                return False, provisioning_error

            return False, str(error)

        return True, None

    def _balance_workflow(self):

        balance_workflow_id = self.assignments.integration(self.device_id).get(
            "balance_workflow_id"
        )

        if not balance_workflow_id:

            raise ValueError("Assign a Balance Check Workflow to this device.")

        try:

            workflow = self.workflows.read(balance_workflow_id)

        except FileNotFoundError:

            raise ValueError("Assigned Balance Check Workflow was not found.") from None

        return balance_check_workflow(workflow)

    def balance_readiness(self):
        """Validate only the dedicated balance route and its safety contract."""

        try:

            self._balance_workflow()

        except (KeyError, ValueError) as error:

            self.invalidate_balance_configuration()

            return False, str(error)

        return True, None

    def _pay(self, raw, account, workflow, *, dispatch_reservation=None):

        variables = normalize_locked_withdrawal(raw)

        operator_id = getattr(self, "api", {}).get("operator_id")

        if operator_id is None:

            operator_id = self.journal.current(self.device_id).get("operator_id")

        # Workflow input declarations own their schema; ignore other API fields.

        values = {
            v["name"]: variables[v["name"]]
            for v in workflow["variables"]
            if v["name"] in variables
        }

        resolve_variables(workflow, values)

        with self.scheduler.lock:

            if dispatch_reservation is not None:

                if (
                    self.scheduler.reservations.get(self.device_id)
                    != dispatch_reservation
                ):

                    raise RecordConflict(
                        "Selected phone no longer owns its dispatch reservation."
                    )

                del self.scheduler.reservations[self.device_id]

            # Reservation release and enqueue are performed under the same

            # scheduler lock. Work that appeared before this handoff remains

            # ahead of the payment job, so the payment safely defers without

            # losing its external lock/session or creating another job.

            job = self.scheduler.enqueue(
                account,
                workflow["id"],
                values,
                external_ref=raw["tx_id"],
                action="withdraw_v2",
                recovery_workflow_id=workflow.get("recovery_workflow_id"),
                protected_metadata={
                    "version": 1,
                    "withdraw_id": raw["id"],
                    "tx_id": raw["tx_id"],
                    "assigned_device_id": self.device_id,
                    "operator_id": operator_id,
                    "account_id": account.account_id,
                    "workflow_id": workflow["id"],
                    "final_amount": format(final_amount(raw), ".2f"),
                    "raw_withdrawal": retained_withdrawal(raw),
                    "lock_expires_at": raw.get("lock_expires_at"),
                },
            )

            self.journal.update(
                self.device_id,
                phase="QUEUED",
                job_id=job.job_id,
                account_id=account.account_id,
                workflow_id=workflow["id"],
                operator_id=operator_id,
                final_amount=format(final_amount(raw), ".2f"),
                raw_withdrawal=retained_withdrawal(raw),
                lock_expires_at=raw.get("lock_expires_at"),
            )

            self.allowed_job = job.job_id

        self.executor.wake()

        self._finish_payment_job(raw, job)

    def _receipt_path_from_job(self, job, *, allowed_step_ids=None):
        """Return a receipt path proven by this job's screenshot metadata."""

        screenshots = job.screenshots if isinstance(job.screenshots, list) else []

        outputs = job.outputs if isinstance(job.outputs, dict) else {}

        output_value = outputs.get("receipt_path")

        if output_value:

            path = Path(output_value).resolve()

            for shot in screenshots:

                if not isinstance(shot, dict) or not shot.get("relative_path"):

                    continue

                if (
                    allowed_step_ids is not None
                    and shot.get("step_id") not in allowed_step_ids
                ):

                    continue

                shot_path = (self.screenshot_root / shot["relative_path"]).resolve()

                if (
                    shot_path == path
                    and path.is_relative_to(self.screenshot_root)
                    and path.is_file()
                ):

                    return path

        # Failed jobs preserve screenshot metadata even when later steps fail.

        # A failed-step retry may reuse the exact earlier receipt only when the

        # receipt capture step is before the retry start index.

        if allowed_step_ids is not None:

            for shot in reversed(screenshots):

                if (
                    not isinstance(shot, dict)
                    or shot.get("step_id") not in allowed_step_ids
                    or not shot.get("relative_path")
                ):

                    continue

                path = (self.screenshot_root / shot["relative_path"]).resolve()

                if path.is_relative_to(self.screenshot_root) and path.is_file():

                    return path

        return None

    def _receipt_for_completed_job(self, job):
        """Resolve a receipt for this attempt or a safe failed-step parent attempt."""

        path = self._receipt_path_from_job(job)

        if path is not None:

            return path

        start_index = job.start_step_index

        parent_id = job.retry_of_job_id

        if not parent_id or type(start_index) is not int or start_index <= 0:

            raise ValueError(
                "Payment receipt output is missing or does not belong to this workflow run."
            )

        try:

            workflow = self.workflows.read(job.workflow)

        except FileNotFoundError:

            raise ValueError(
                "Payment workflow is unavailable while validating the retry receipt."
            ) from None

        steps = workflow.get("steps", [])

        if not isinstance(steps, list):

            raise ValueError(
                "Payment workflow is invalid while validating the retry receipt."
            )

        allowed_step_ids = {
            step.get("id")
            for index, step in enumerate(steps)
            if (
                index < start_index
                and isinstance(step, dict)
                and step.get("type") == "capture_screen"
                and step.get("output_key") == "receipt_path"
                and isinstance(step.get("id"), str)
                and step.get("id")
            )
        }

        if not allowed_step_ids:

            raise ValueError(
                "Payment receipt output is missing or does not belong to this workflow run."
            )

        seen = {job.job_id}

        while parent_id and parent_id not in seen:

            seen.add(parent_id)

            try:

                parent = self.scheduler.get_job(parent_id)

            except RecordNotFound:

                break

            if (
                parent.action != "withdraw_v2"
                or parent.device_id != job.device_id
                or parent.account_id != job.account_id
                or parent.external_ref != job.external_ref
                or parent.workflow != job.workflow
            ):

                break

            path = self._receipt_path_from_job(
                parent, allowed_step_ids=allowed_step_ids
            )

            if path is not None:

                LOG.info(
                    "[withdraw-worker] reusing receipt from previous attempt "
                    "device=%s retry_job=%s source_job=%s",
                    self.device_id,
                    job.job_id,
                    parent.job_id,
                )

                return path

            parent_id = parent.retry_of_job_id

        raise ValueError(
            "Payment receipt output is missing or does not belong to this workflow run."
        )

    def _finish_payment_job(self, raw, job):
        """Monitor an existing local payment job and preserve confirmation."""

        self.set_state("PAYING")

        while True:

            job = self.scheduler.get_job(job.job_id)

            if (
                self.stop.is_set()
                or self.lock_lost.is_set()
                or not self.socket.connected
                or expiry(self.journal.current(self.device_id)["lock_expires_at"])
                <= time.time()
            ):

                self.executor.cancel(job.job_id)

                raise ValueError(
                    "Payment interrupted or lock/session lost. Do not replay; reconciliation is required."
                )

            if job.status in {"FAILED", "INTERRUPTED"}:

                job = self._wait_for_retry(job)

                continue

            if job.status == "CANCELLED":

                raise ValueError(
                    "Payment workflow did not prove success. Restore the phone and reconcile externally."
                )

            if job.status == "COMPLETED":

                break

            self.stop.wait(0.2)

        self.allowed_job = None

        path = self._receipt_for_completed_job(job)

        self.journal.update(self.device_id, phase="CONFIRMING", receipt_path=str(path))

        self.set_state("CONFIRMING")

        try:

            self.client.confirm_with_receipt(raw["id"], path)

            confirmed = True  # HTTP 200 + successful envelope is final per the API.

        except APIError:

            confirmed = self.client.reconcile_confirmation(raw["tx_id"], raw["id"])

        if not confirmed:

            raise ValueError(
                "External confirmation could not be reconciled. Do not pay again."
            )

        self.journal.update(self.device_id, phase="CONFIRMED", error=None)

        payer_balance = job.outputs.get("payer_balance")

        if payer_balance is not None:
            try:
                payer_balance = parse_currency(payer_balance)
            except (AttributeError, TypeError, ValueError):
                LOG.warning(
                    "payment_balance_rejected device=%s job_id=%s value=%r",
                    self.device_id,
                    job.job_id,
                    payer_balance,
                )
                self.cache.confirmed_payment(final_amount(raw))
            else:
                self.observe_balance(payer_balance)
                LOG.info(
                    "payment_balance_updated device=%s job_id=%s value=%s",
                    self.device_id,
                    job.job_id,
                    format(payer_balance, ".2f"),
                )
        else:
            LOG.info(
                "payment_balance_missing device=%s job_id=%s",
                self.device_id,
                job.job_id,
            )
            self.cache.confirmed_payment(final_amount(raw))

        self.executor.wake()
        self.set_state("IDLE")

    def session_available(self):

        with self.lock:

            state = self.state

        return bool(
            self.reconciliation_complete
            and self.socket
            and self.socket.connected
            and self.reconciled_generation == self.socket.generation
            # NEEDS_ATTENTION is local withdrawal recovery state, not an
            # authentication or realtime-session failure. dispatch_available()
            # independently keeps that phone out of new payment dispatch.
            and state not in {"CONFIGURATION_ERROR", "AUTH_FAILED"}
        )

    def dispatch_available(self):

        with self.lock:

            assigned = self.assigned

            state = self.state

            balance_refreshing = self.balance_refreshing

        return bool(
            self.session_available()
            and not self.stop.is_set()
            and not balance_refreshing
            and assigned is None
            and state
            not in {
                "NEEDS_ATTENTION",
                "CONFIGURATION_ERROR",
                "PAYING",
                "CONFIRMING",
                "DISPATCH_RESERVED",
            }
            and time.time() < self.session_max - 60
        )

    def mark_dispatch_reserved(self):

        with self.lock:

            self.state = "DISPATCH_RESERVED"

            self.error = None

    def accept_locked(self, raw, reservation):
        """Hand a successfully locked withdrawal to this phone's worker thread."""

        with self.lock:

            if self.assigned is not None:

                raise RecordConflict("Phone already owns a dispatched withdrawal.")

            self.assigned = (raw, reservation)

        self.wake.set()

    def _execute_assigned(self):

        with self.lock:

            assignment = self.assigned

            retry_job_id = self.retry_job_id

        if assignment is None:

            return

        raw, reservation = assignment

        try:

            if retry_job_id is not None:

                self._finish_payment_job(raw, self.scheduler.get_job(retry_job_id))

                return

            account, workflow = self._route()

            if (
                not self.cache.valid(context=self.balance_context())
                or final_amount(raw) > self.cache.available_balance
            ):

                self.release_withdrawal()

                return

            LOG.info(
                "[withdraw-worker] starting workflow device=%s workflow=%s withdrawal=%s",
                self.device_id,
                workflow.get("name") or workflow.get("id"),
                raw.get("id"),
            )

            self._pay(
                raw,
                account,
                workflow,
                dispatch_reservation=reservation,
            )

        finally:

            self.stop_renewal()

            with self.scheduler.lock:

                if (
                    reservation is not None
                    and self.scheduler.reservations.get(self.device_id) == reservation
                ):

                    del self.scheduler.reservations[self.device_id]

            with self.lock:

                if self.assigned == assignment:

                    self.assigned = None

                if self.retry_job_id == retry_job_id:

                    self.retry_job_id = None

            if self.journal.current(self.device_id).get("phase") in TERMINAL:

                self.set_state("IDLE")

            if self.dispatcher:

                self.dispatcher.wake()

    def ensure_realtime(self):
        """Authenticate this session and reconcile it once per physical generation."""

        with self.lock:

            authenticated = self.pending_authenticated_client

            self.pending_authenticated_client = None

        if authenticated is not None:

            replacement, session_max = authenticated

            self.disconnect_socket()

            previous = self.client

            self.client = replacement

            self.session_max = session_max

            if previous is not replacement:

                previous.close()

        if self.reauthenticate.is_set():

            self.disconnect_socket()

            self.client.token = self.client.profile = None

            self.session_max = 0

            self.reauthenticate.clear()

        refresh = self.client.profile is None or time.time() >= self.session_max - 60

        if self.socket is None or refresh:

            self.set_state("CONNECTING")

            if refresh:

                self.login()

            self.connect_socket()

        elif not self.socket.connected:

            self.cache.invalidate()

            self.reconciliation_complete = False

            self.set_state("WAITING_FOR_CONNECTION")

            if not self.socket.wait_ready(10):

                return False

        generation = self.socket.generation

        session_became_ready = False

        if generation != self.reconciled_generation:

            self.reconciliation_complete = False

            reconciled = self.reconcile()

            self.reconciliation_complete = True

            if not reconciled:

                self.reconciled_generation = generation

                if self.dispatcher:

                    self.dispatcher.wake()

                return False

            self.reconciled_generation = generation

            session_became_ready = True

            if self.dispatcher:

                self.dispatcher.wake()

        if self.state in {
            "STOPPED",
            "CONNECTING",
            "WAITING_FOR_CONNECTION",
        }:

            self.set_state("IDLE")

        if session_became_ready and callable(self.account_binding_provisioner):

            # Provisioning is local readiness only. The callback absorbs and

            # reports failures so a healthy external session stays connected.

            self.set_account_binding_error(self.account_binding_provisioner())

        return True

    def run(self):

        backoff = 5

        while not self.stop.is_set():

            try:

                with self.lock:

                    has_assignment = self.assigned is not None

                if has_assignment:

                    with self.withdrawal_operation:

                        self._execute_assigned()

                else:

                    if not self.ensure_realtime():

                        if self.state == "NEEDS_ATTENTION":

                            self.wake.wait(60)

                            self.wake.clear()

                        else:

                            self.wake.wait(0.5)

                            self.wake.clear()

                        continue

                    current = self.journal.current(self.device_id)

                    if self._pending_retry(current) is not None:

                        if self._adopt_pending_retry(current):

                            continue

                    self._maybe_refresh_balance()

                backoff = 5

                self.wake.wait(0.5)

                self.wake.clear()

            except Exception as exc:

                self.cache.invalidate()

                if self.stop.is_set():

                    break

                current = self.journal.current(self.device_id)

                requires_intervention = isinstance(
                    exc, (AuthenticationFailed, ValueError)
                ) or (isinstance(exc, APIError) and exc.code in {403, 422})

                if current and current.get("phase") not in TERMINAL:

                    self.attention(
                        "Withdrawal state is unresolved. Manual/external reconciliation is required."
                    )

                else:

                    self.set_state(
                        (
                            "AUTH_FAILED"
                            if isinstance(exc, AuthenticationFailed)
                            else (
                                "CONFIGURATION_ERROR"
                                if requires_intervention
                                else "WAITING_FOR_CONNECTION"
                            )
                        ),
                        (
                            str(exc)
                            if isinstance(exc, (ValueError, APIError))
                            else "Integration request failed; review configuration/connectivity."
                        ),
                    )

                if isinstance(exc, APIError) and exc.code == 401:

                    self.client.token = self.client.profile = None

                self.reconciled_generation = 0

                self.reconciliation_complete = False

                if requires_intervention:

                    # Remove only this logical session. Other phones continue on

                    # the shared socket. A deliberate reconnect/status action can

                    # wake this worker after credentials or remote state changes.

                    self.disconnect_socket()

                    self.wake.wait()

                    self.wake.clear()

                    backoff = 5

                    continue

                delay = (
                    exc.retry_after
                    if isinstance(exc, APIError) and exc.code == 429
                    else backoff
                )

                self.wake.wait(delay)

                self.wake.clear()

                backoff = min(60, backoff * 2)

        self.stop_renewal()

        self.disconnect_socket()

        # Logout releases locks. Never logout an unresolved/uncertain withdrawal.

        current = self.journal.current(self.device_id)

        if self.client.token and (not current or current.get("phase") in TERMINAL):

            try:

                self.client.logout()

            except APIError:

                pass

        self.client.close()

    def close(self):

        self.stop.set()

        self.wake.set()

        self.allowed_job = None

        if self.thread:

            self.thread.join(timeout=25)

        with self.lock:

            pending = self.pending_authenticated_client

            self.pending_authenticated_client = None

        if pending is not None:

            pending[0].close()

        self.disconnect_socket()
