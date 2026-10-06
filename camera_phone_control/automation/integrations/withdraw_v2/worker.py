"""One external session per logical phone; physical execution stays in jobs."""

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from automation.jobs.storage import RecordConflict
from automation.workflows.validation import resolve_variables
from .balance import BalanceCache, final_amount, select_pending
from .client import APIError
from .config import load_credentials
from .journal import TERMINAL
from .normalization import normalize_locked_withdrawal, retained_withdrawal
from .socket import PusherSocket

LOG = logging.getLogger("withdraw_v2")


def expiry(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.timestamp()
    except (AttributeError, TypeError, ValueError):
        raise ValueError("External lock/session expiry is invalid.") from None


def payment_workflow(workflow):
    # Conservative V1 proof contract: the final two top-level steps must verify
    # the configured successful bank screen, then capture the receipt. A mere
    # screenshot or a branch that skips verification is not payment evidence.
    steps = workflow["steps"]
    if (
        len(steps) < 2
        or steps[-2]["type"] != "wait_for_text"
        or steps[-1]["type"] != "capture_screen"
        or steps[-1].get("output_key") != "receipt_path"
    ):
        raise ValueError(
            "Payment workflow must end with success-screen Wait for Text then Screenshot output receipt_path."
        )
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
        credentials=load_credentials,
        renew_seconds=300
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
        self.cache = BalanceCache(
            device_id, refresh_seconds=self.api.get("balance_refresh_seconds", 60)
        )
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.RLock()
        self.thread = None
        self.socket = None
        self.state = "STOPPED"
        self.error = None
        self.allowed_job = (
            None  # Runtime-only authorization; never restored after restart.
        )
        self.renew_seconds = renew_seconds
        self.queue_due = None
        self.next_pending = 0
        self.renew_stop = threading.Event()
        self.renew_thread = None
        self.lock_lost = threading.Event()
        self.session_max = 0

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
        with self.lock:
            valid = self.cache.valid()
            balance_state = (
                "VALID"
                if valid
                else (
                    "STALE" if self.cache.available_balance is not None else "UNKNOWN"
                )
            )
            return {
                "device_id": self.device_id,
                "operator_id": self.api["operator_id"],
                "username": self.api["username"],
                "state": self.state,
                "session_state": self.state,
                "error": self.error,
                "socket_connected": bool(self.socket and self.socket.connected),
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
            }

    def start(self):
        self.thread = threading.Thread(
            target=self.run, name="withdraw-" + self.device_id, daemon=True
        )
        self.thread.start()

    def queue_changed(self):
        with self.lock:
            self.queue_due = time.monotonic() + 1
        self.wake.set()

    def login(self):
        password, secret = self.credentials(self.api["credential_reference"])
        profile = self.client.login(
            self.api["username"], password, secret, self.api["operator_id"]
        )
        self.session_max = expiry(profile["session_max_expires_at"])
        return profile

    def connect_socket(self):
        self.disconnect_socket()
        self.socket = self.socket_factory(
            self.client, device_id=self.device_id, on_queue_changed=self.queue_changed
        )
        self.socket.connect(self.client.profile["realtime"])

    def disconnect_socket(self):
        if self.socket:
            self.socket.close()
            self.socket = None
        self.cache.invalidate()

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

    def reconcile(self):
        """Called after every subscription, before any pending/physical work."""
        self.cache.invalidate()
        locked = self.get_my_locked()
        current = self.journal.current(self.device_id)
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

    def lock_withdrawal(self, item):
        if (
            type(item.get("id")) is not int
            or item["id"] <= 0
            or not isinstance(item.get("tx_id"), str)
            or not item["tx_id"]
        ):
            raise ValueError("Pending withdrawal identifiers are invalid.")
        if self.journal.was_confirmed(self.device_id, item["tx_id"]):
            raise ValueError(
                "Previously confirmed transaction appeared in pending; manual review is required."
            )
        self.journal.begin(
            self.device_id,
            phase="LOCK_INTENT",
            withdraw_id=item["id"],
            tx_id=item["tx_id"],
        )
        response = self.action("lock", withdraw_id=item["id"])
        raw = retained_withdrawal(response.get("withdrawal"))
        # Keep the entire response before normalization, including unknown fields.
        self.journal.update(
            self.device_id,
            phase="LOCKED",
            raw_withdrawal=raw,
            lock_expires_at=raw.get("lock_expires_at"),
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
        self.lock_lost.clear()
        self.renew_stop.clear()
        self.renew_thread = threading.Thread(
            target=self._renew, name="withdraw-renew-" + self.device_id, daemon=True
        )
        self.renew_thread.start()
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
        current = self.journal.current(self.device_id)
        self.action("cancel", withdraw_id=current["withdraw_id"], reason=reason)
        self.journal.update(self.device_id, phase="CANCELLED")
        self.cache.invalidate()
        self.stop_renewal()

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

    def _pay(self, raw, account, workflow):
        variables = normalize_locked_withdrawal(raw)
        # Workflow input declarations own their schema; ignore other API fields.
        values = {
            v["name"]: variables[v["name"]]
            for v in workflow["variables"]
            if v["name"] in variables
        }
        resolve_variables(workflow, values)
        with self.scheduler.lock:
            if self.scheduler.next_job_for_device(self.device_id) is not None:
                raise RecordConflict(
                    "Queued device work appeared before payment; review the held withdrawal."
                )
            job = self.scheduler.enqueue(
                account,
                workflow["id"],
                values,
                external_ref=raw["tx_id"],
                action="withdraw_v2",
                recovery_workflow_id=workflow.get("recovery_workflow_id"),
            )
            self.journal.update(self.device_id, phase="QUEUED", job_id=job.job_id)
            self.allowed_job = job.job_id
        self.executor.wake()
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
            if job.status in {"FAILED", "INTERRUPTED", "CANCELLED"}:
                raise ValueError(
                    "Payment workflow did not prove success. Restore the phone and reconcile externally."
                )
            if job.status == "COMPLETED":
                break
            self.stop.wait(0.2)
        self.allowed_job = None
        path = Path(job.outputs.get("receipt_path", "")).resolve()
        metadata_paths = {
            (self.screenshot_root / shot["relative_path"]).resolve()
            for shot in job.screenshots
        }
        if (
            path not in metadata_paths
            or not path.is_relative_to(self.screenshot_root)
            or not path.is_file()
        ):
            raise ValueError(
                "Payment receipt output is missing or does not belong to this workflow run."
            )
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
        self.cache.confirmed_payment(final_amount(raw))
        self.executor.wake()
        self.set_state("IDLE")

    def cycle(self):
        if not self.scheduler.is_device_ready(self.device_id)["ready"]:
            self.cache.invalidate()
            self.set_state("WAITING_FOR_DEVICE")
            return
        if self.scheduler.next_job_for_device(self.device_id) is not None:
            self.cache.invalidate()
            return
        account, workflow = self._route()
        if not self.config.get("balance_workflow_id"):
            self.set_state(
                "BALANCE_UNAVAILABLE",
                "Assign a balance-check workflow before taking withdrawals.",
            )
            return
        if not self.cache.valid():
            self.set_state("READING_BALANCE")
            balance = self.balance_provider.get_available_balance(self.device_id)
            self.cache.observe(balance)

        pending = self.request_pending()
        item = select_pending(pending, self.cache.available_balance)

        if item is None:
            self.set_state("IDLE")
            return
        try:
            raw = self.lock_withdrawal(item)
            if (
                not self.cache.valid()
                or final_amount(raw) > self.cache.available_balance
            ):
                self.release_withdrawal()  # No payment job has been created.
                return
            self._pay(raw, account, workflow)
        finally:
            self.stop_renewal()

    def run(self):
        backoff = 5
        while not self.stop.is_set():
            try:
                if (
                    self.socket is None
                    or not self.socket.connected
                    or time.time() >= self.session_max - 60
                ):
                    self.set_state("CONNECTING")
                    if (
                        self.client.profile is None
                        or time.time() >= self.session_max - 60
                    ):
                        self.login()
                    self.connect_socket()
                    if not self.reconcile():
                        self.wake.wait(60)
                        self.wake.clear()
                        self.disconnect_socket()
                        continue
                    self.next_pending = 0

                stamp = time.monotonic()
                # Invalidate during observable manual/recovery/disconnect states,
                # including between periodic queue fetches.
                if not self.scheduler.is_device_ready(self.device_id)["ready"]:
                    self.cache.invalidate()
                with self.lock:
                    due = self.queue_due is not None and stamp >= self.queue_due
                    if due:
                        self.queue_due = None
                if due or stamp >= self.next_pending:
                    self.cycle()
                    self.next_pending = time.monotonic() + 60

                backoff = 5
                self.wake.wait(0.5)
                self.wake.clear()
            except Exception as exc:
                self.cache.invalidate()
                current = self.journal.current(self.device_id)
                if current and current.get("phase") not in TERMINAL:
                    self.attention(
                        "Withdrawal state is unresolved. Manual/external reconciliation is required."
                    )
                else:
                    self.set_state(
                        (
                            "CONFIGURATION_ERROR"
                            if isinstance(exc, ValueError)
                            else "WAITING_FOR_CONNECTION"
                        ),
                        (
                            str(exc)
                            if isinstance(exc, (ValueError, APIError))
                            else "Integration request failed; review configuration/connectivity."
                        ),
                    )
                if isinstance(exc, APIError) and exc.code == 401:
                    self.client.token = self.client.profile = None
                self.disconnect_socket()

                if isinstance(exc, APIError) and exc.code in (403, 422):
                    self.set_state("CONFIGURATION_ERROR", str(exc))
                    break
                delay = (
                    exc.retry_after
                    if isinstance(exc, APIError) and exc.code == 429
                    else backoff
                )
                self.stop.wait(delay)
                backoff = min(60, backoff * 2)

        self.stop_renewal()
        self.disconnect_socket()
        # Logout releases locks. Never logout an unresolved/uncertain withdrawal.
        if (
            self.client.token
            and self.journal.current(self.device_id).get("phase") in TERMINAL
        ):
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
        self.disconnect_socket()
