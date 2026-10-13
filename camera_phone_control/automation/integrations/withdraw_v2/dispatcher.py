"""Dashboard-wide pending selection and phone reservation coordinator."""

import logging
import threading
import time
import uuid

from .balance import final_amount
from .client import APIError
from .journal import TERMINAL

LOG = logging.getLogger("withdraw_v2")


class WithdrawDispatcher:
    """Poll logical sessions once and assign locked withdrawals to safe phones."""

    def __init__(
        self,
        workers,
        scheduler,
        journal,
        *,
        poll_seconds=60,
        ready_for_work=None,
    ):
        self.workers = workers
        self.scheduler = scheduler
        self.journal = journal
        self.poll_seconds = poll_seconds
        self.stop = threading.Event()
        self.event = threading.Event()
        self.state_lock = threading.RLock()
        self.cycle_running = False
        self.thread = None
        self.queue_due = None
        self.cursor = 0
        self.balance_relevant = set()
        self.ready_for_work = ready_for_work or (lambda _device_id: True)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.thread = threading.Thread(
            target=self._run, name="withdraw-dispatcher", daemon=True
        )
        self.thread.start()

    def wake(self):
        with self.state_lock:
            self.queue_due = time.monotonic() + 1
        self.event.set()

    def request_stop(self):
        self.stop.set()
        self.event.set()

    def close(self):
        self.request_stop()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=15)
        if self.thread and not self.thread.is_alive():
            self.thread = None

    def _run(self):
        next_poll = 0
        while not self.stop.is_set():
            stamp = time.monotonic()
            with self.state_lock:
                queue_due = self.queue_due
            due = next_poll <= stamp or (queue_due is not None and queue_due <= stamp)
            if due:
                with self.state_lock:
                    if self.queue_due is not None and self.queue_due <= stamp:
                        self.queue_due = None
                try:
                    self.dispatch_once()
                except Exception:
                    LOG.exception("Withdraw dispatcher cycle failed")
                next_poll = time.monotonic() + self.poll_seconds
                continue
            deadline = min(
                next_poll,
                queue_due if queue_due is not None else next_poll,
            )
            self.event.wait(max(0.05, deadline - stamp))
            self.event.clear()

    def _all_sessions_reconciled(self, workers):
        return bool(workers) and all(
            worker.reconciliation_complete
            or worker.state in {"NEEDS_ATTENTION", "CONFIGURATION_ERROR", "AUTH_FAILED"}
            for worker in workers
        )

    def _pending_snapshot(self, workers):
        merged = []
        by_id = {}
        inconsistent = set()
        for worker in workers:
            if not worker.session_available():
                LOG.info(
                    "[withdraw-dispatch] skip device=%s reason=%s",
                    worker.device_id,
                    self._session_skip_reason(worker) or "device_busy",
                )
                continue

            try:
                pending = worker.request_pending()
            except APIError as exc:
                if exc.code == 401:
                    worker.client.token = worker.client.profile = None
                    worker.reconciliation_complete = False
                    worker.wake.set()
                LOG.info(
                    "[withdraw-dispatch] skip device=%s reason=%s",
                    worker.device_id,
                    (
                        "session_not_connected"
                        if exc.code == 401
                        else "pending_request_failed"
                    ),
                )
                continue
            except Exception:
                LOG.info(
                    "[withdraw-dispatch] skip device=%s reason=pending_request_failed",
                    worker.device_id,
                )
                continue

            for item in pending:
                withdraw_id = item.get("id")
                tx_id = item.get("tx_id")
                if (
                    type(withdraw_id) is not int
                    or not isinstance(tx_id, str)
                    or not tx_id
                ):
                    continue

                found = by_id.get(withdraw_id)

                if found:
                    if found["item"].get("tx_id") != tx_id:
                        inconsistent.add(withdraw_id)
                    found["visible"].add(worker.device_id)
                    continue

                found = {
                    "item": item,
                    "visible": {worker.device_id},
                }
                by_id[withdraw_id] = found
                merged.append(found)
        return [entry for entry in merged if entry["item"]["id"] not in inconsistent]

    @staticmethod
    def _payment_record_compatible(worker, item):
        payment_record = item.get("payment_record")
        allowed = worker.api.get("payment_records") or []
        if payment_record is None or not allowed:
            return True
        if type(payment_record) not in {str, int}:
            return False
        return str(payment_record) in {str(value) for value in allowed}

    @staticmethod
    def _log_skip(worker, item, reason, *, detail=None):
        suffix = f" detail={detail}" if detail else ""
        LOG.info(
            "[withdraw-dispatch] skip device=%s reason=%s withdrawal=%s%s",
            worker.device_id,
            reason,
            item.get("id"),
            suffix,
        )
        return False

    @staticmethod
    def _session_skip_reason(worker):
        if not getattr(worker, "reconciliation_complete", False):
            return "session_not_reconciled"
        if hasattr(worker, "socket"):
            socket = worker.socket
            if socket is None:
                return "session_not_connected"
            if not socket.physical_connected:
                return "realtime_not_connected"
            if not socket.connected:
                return "session_not_connected"
        return None

    @staticmethod
    def _readiness_skip_reason(error):
        detail = str(error or "")
        lowered = detail.casefold()
        if "balance" in lowered:
            return "balance_workflow_not_ready", detail
        if "account" in lowered and "receipt" not in lowered:
            return "no_assignment", detail
        return "payment_workflow_not_ready", detail

    def _prepare_worker(self, worker, item, visible, unavailable):
        LOG.info(
            "[withdraw-dispatch] candidate device=%s withdrawal=%s",
            worker.device_id,
            item.get("id"),
        )
        if worker.device_id in unavailable:
            return self._log_skip(worker, item, unavailable[worker.device_id])
        if worker.api.get("enabled") is False:
            unavailable[worker.device_id] = "device_disabled"
            return self._log_skip(worker, item, "device_disabled")
        if not self.ready_for_work(worker.device_id):
            unavailable[worker.device_id] = "not_ready_for_work"
            return self._log_skip(worker, item, "not_ready_for_work")
        if worker.device_id not in visible:
            return self._log_skip(worker, item, "payment_record_not_allowed")
        if (
            type(worker.api.get("operator_id")) is not int
            or not worker.api.get("username")
            or not worker.api.get("credential_reference")
        ):
            unavailable[worker.device_id] = "no_assignment"
            return self._log_skip(worker, item, "no_assignment")
        if not self._payment_record_compatible(worker, item):
            return self._log_skip(worker, item, "payment_record_not_allowed")
        record = self.journal.current(worker.device_id)

        if record and record.get("phase") not in TERMINAL:
            unavailable[worker.device_id] = "unresolved_journal"
            return self._log_skip(worker, item, "unresolved_journal")
        if worker.device_id in self.scheduler.reservations:
            unavailable[worker.device_id] = "active_reservation"
            return self._log_skip(worker, item, "active_reservation")
        session_reason = self._session_skip_reason(worker)
        if session_reason:
            unavailable[worker.device_id] = session_reason
            return self._log_skip(worker, item, session_reason)
        if getattr(worker, "assigned", None) is not None or getattr(
            worker, "state", None
        ) in {"DISPATCH_RESERVED", "PAYING", "CONFIRMING"}:
            unavailable[worker.device_id] = "device_busy"
            return self._log_skip(worker, item, "device_busy")
        if not worker.dispatch_available():
            unavailable[worker.device_id] = "device_busy"
            return self._log_skip(worker, item, "device_busy")
        payment_ready, payment_error = worker.payment_readiness()
        if not payment_ready:
            reason, detail = self._readiness_skip_reason(payment_error)
            unavailable[worker.device_id] = reason
            return self._log_skip(worker, item, reason, detail=detail)
        balance_valid = getattr(worker, "balance_valid", None)
        fresh = balance_valid() if callable(balance_valid) else worker.cache.valid()
        if not fresh:
            reason = (
                "stale" if worker.cache.available_balance is not None else "unknown"
            )
            LOG.info(
                "dispatcher_balance device=%s value=unknown reason=%s",
                worker.device_id,
                reason,
            )
            self.balance_relevant.add(worker.device_id)
            note_pending = getattr(worker, "note_pending_balance", None)
            if callable(note_pending):
                note_pending(reason)
            unavailable[worker.device_id] = "balance_unknown"
            return self._log_skip(worker, item, "balance_unknown")
        if not self.scheduler.is_device_ready(worker.device_id)["ready"]:
            unavailable[worker.device_id] = "scheduler_not_ready"
            return self._log_skip(worker, item, "scheduler_not_ready")
        LOG.info(
            "dispatcher_balance device=%s value=%s",
            worker.device_id,
            format(worker.cache.available_balance, ".2f"),
        )
        if self.stop.is_set():
            return False

        amount = final_amount(item)

        if worker.cache.available_balance < amount:
            self.balance_relevant.add(worker.device_id)
            note_pending = getattr(worker, "note_pending_balance", None)
            if callable(note_pending):
                note_pending("insufficient")

            return self._log_skip(
                worker,
                item,
                "insufficient_balance",
                detail=(
                    f"available={worker.cache.available_balance} required={amount}"
                ),
            )
        return True

    def _ordered_candidates(self, item, visible, unavailable):
        workers = self.workers()
        device_ids = sorted(workers)

        if not device_ids:
            return []

        start = self.cursor % len(device_ids)
        ordered = device_ids[start:] + device_ids[:start]
        return [
            workers[device_id]
            for device_id in ordered
            if self._prepare_worker(workers[device_id], item, visible, unavailable)
        ]

    def _advance_cursor(self, device_id):
        device_ids = sorted(self.workers())
        if device_id in device_ids:
            self.cursor = (device_ids.index(device_id) + 1) % len(device_ids)

    def _reserve_and_claim(self, worker, item):
        if self.stop.is_set():
            return None

        token = "withdraw-dispatch-" + uuid.uuid4().hex
        amount = final_amount(item)

        with self.scheduler.lock:
            if not self.ready_for_work(worker.device_id):
                self._log_skip(worker, item, "not_ready_for_work")
                return None
            if not worker.dispatch_available():
                self._log_skip(worker, item, "device_busy")
                return None
            if not self.scheduler.is_device_ready(worker.device_id)["ready"]:
                self._log_skip(worker, item, "scheduler_not_ready")
                return None

            balance_valid = getattr(worker, "balance_valid", None)
            fresh = balance_valid() if callable(balance_valid) else worker.cache.valid()

            if not fresh:
                self.balance_relevant.add(worker.device_id)
                note_pending = getattr(worker, "note_pending_balance", None)
                if callable(note_pending):
                    note_pending(
                        "stale"
                        if worker.cache.available_balance is not None
                        else "unknown"
                    )
                self._log_skip(worker, item, "balance_unknown")
                return None
            if worker.cache.available_balance < amount:
                self.balance_relevant.add(worker.device_id)
                note_pending = getattr(worker, "note_pending_balance", None)
                if callable(note_pending):
                    note_pending("insufficient")
                self._log_skip(worker, item, "insufficient_balance")
                return None
            LOG.info(
                "[withdraw-dispatch] creating reservation device=%s withdrawal=%s",
                worker.device_id,
                item["id"],
            )
            self.scheduler.reservations[worker.device_id] = token
            worker.mark_dispatch_reserved()
        try:
            claimed, _ = self.journal.claim(
                worker.device_id,
                withdraw_id=item["id"],
                tx_id=item["tx_id"],
                operator_id=worker.api["operator_id"],
                final_amount=format(amount, ".2f"),
            )
        except Exception:
            self._clear_reservation(worker, token)
            worker.set_state("IDLE")
            raise

        if not claimed:
            self._clear_reservation(worker, token)
            worker.set_state("IDLE")
            self._log_skip(worker, item, "duplicate_withdrawal")
            return None
        return token

    def _clear_reservation(self, worker, token):
        with self.scheduler.lock:
            if self.scheduler.reservations.get(worker.device_id) == token:
                del self.scheduler.reservations[worker.device_id]

    def _reject(self, worker, token, message):
        self.journal.update(worker.device_id, phase="LOCK_REJECTED", error=message)
        self._clear_reservation(worker, token)
        worker.set_state("IDLE")

    def _ambiguous(self, worker, token):
        self._clear_reservation(worker, token)
        worker.stop_renewal()
        worker.attention(
            "External lock result is uncertain. Reconcile this withdrawal "
            "before retrying."
        )

    def _try_assignment(self, worker, item):
        LOG.info(
            "[withdraw-dispatch] selected device=%s withdrawal=%s",
            worker.device_id,
            item["id"],
        )
        token = self._reserve_and_claim(worker, item)
        if token is None:
            return "unavailable"
        self._advance_cursor(worker.device_id)

        try:
            LOG.info(
                "[withdraw-dispatch] sending client-lock device=%s withdrawal=%s",
                worker.device_id,
                item["id"],
            )
            raw = worker.lock_withdrawal(item, claimed=True)
        except APIError as exc:
            if exc.code in {404, 423}:
                self._reject(worker, token, f"External lock rejected ({exc.code}).")
                return "stop"
            if exc.code == 409 and "payment record" in str(exc).casefold():
                self._reject(
                    worker, token, "Session cannot access this payment record."
                )
                return "try_next"
            if exc.code == 409:
                self._reject(worker, token, "Withdrawal is no longer payable.")
                return "stop"

            self._ambiguous(worker, token)
            return "stop"
        except Exception:
            self._ambiguous(worker, token)
            return "stop"
        try:
            LOG.info(
                "[withdraw-dispatch] lock accepted device=%s withdrawal=%s",
                worker.device_id,
                item["id"],
            )
            worker.accept_locked(raw, token)
        except Exception:
            self._ambiguous(worker, token)
            return "stop"
        return "assigned"

    def _dispatch_item(self, item, visible, unavailable):
        try:
            final_amount(item)
        except (ValueError, TypeError):
            LOG.info(
                "[withdraw-dispatch] skip withdrawal=%s reason=invalid_amount",
                item.get("id"),
            )
            return False
        if self.journal.identity_conflict(item["id"], item["tx_id"]):
            LOG.info(
                "[withdraw-dispatch] skip withdrawal=%s reason=duplicate_withdrawal",
                item["id"],
            )
            return False
        for worker in self._ordered_candidates(item, visible, unavailable):
            result = self._try_assignment(worker, item)
            if result == "try_next":
                continue
            return result == "assigned"
        return False

    def dispatch_once(self):
        """Run one deduplicated dashboard selection cycle."""
        with self.state_lock:
            if self.cycle_running:
                return False
            self.cycle_running = True
        try:
            workers = [self.workers()[key] for key in sorted(self.workers())]
            self.balance_relevant = set()
            if not self._all_sessions_reconciled(workers):
                for worker in workers:
                    if not worker.reconciliation_complete:
                        LOG.info(
                            "[withdraw-dispatch] skip device=%s reason=session_not_reconciled",
                            worker.device_id,
                        )
                return False
            unavailable = {}

            for entry in self._pending_snapshot(workers):
                if self.stop.is_set():
                    break
                item = entry["item"]
                LOG.info(
                    "[withdraw-dispatch] pending id=%s tx=%s record=%s amount=%s priority=%s",
                    item.get("id"),
                    item.get("tx_id"),
                    item.get("payment_record"),
                    item.get("final_amount"),
                    item.get("priority"),
                )
                self._dispatch_item(entry["item"], entry["visible"], unavailable)
            return True
        finally:
            relevant = self.balance_relevant
            for worker in self.workers().values():
                if worker.device_id not in relevant:
                    clear_pending = getattr(
                        worker, "clear_pending_balance_requirement", None
                    )
                    if callable(clear_pending):
                        clear_pending()
            with self.state_lock:
                self.cycle_running = False
