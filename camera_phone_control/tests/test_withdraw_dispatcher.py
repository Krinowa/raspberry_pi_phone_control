"""Mocked Phase 2 dashboard dispatcher safety tests."""

import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from automation.integrations.withdraw_v2.balance import BalanceCache
from automation.integrations.withdraw_v2.client import APIError, UncertainResult
from automation.integrations.withdraw_v2.dispatcher import WithdrawDispatcher
from automation.integrations.withdraw_v2.journal import WithdrawJournal
from automation.integrations.withdraw_v2.manager import WithdrawManager
from automation.integrations.withdraw_v2.worker import payment_workflow


def future():
    return (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()


def pending(ident, amount="10.00", *, tx_id=None, payment_record="2b"):
    return {
        "id": ident,
        "tx_id": tx_id or f"TX-{ident}",
        "final_amount": amount,
        "payment_record": payment_record,
        "transfer_method": "bank_account",
        "type": "bank",
        "account_number": "0012345678",
    }


class FakeScheduler:
    def __init__(self):
        self.lock = threading.RLock()
        self.reservations = {}
        self.ready = {}

    def is_device_ready(self, device_id):
        ready = self.ready.get(device_id, True) and device_id not in self.reservations
        return {"ready": ready}


class FakeBalanceProvider:
    def __init__(self, values=None):
        self.values = values or {}
        self.calls = []

    def get_available_balance(self, device_id):
        self.calls.append(device_id)
        value = self.values.get(device_id)
        if isinstance(value, Exception):
            raise value
        return value


class FakeWorker:
    def __init__(
        self,
        device_id,
        scheduler,
        journal,
        *,
        balance=None,
        provider=None,
        payment_records=("2b",),
    ):
        self.device_id = device_id
        self.scheduler = scheduler
        self.journal = journal
        self.api = {
            "base_url": "https://withdraw.example",
            "operator_id": 7 if device_id == "phone_A" else 12,
            "username": "operator-" + device_id,
            "credential_reference": "credential-" + device_id,
            "payment_records": list(payment_records),
        }
        self.config = {"balance_workflow_id": "balance-" + device_id}
        self.cache = BalanceCache(device_id)
        if balance is not None:
            self.cache.observe(Decimal(balance), context=self.balance_context())
        self.balance_provider = provider or FakeBalanceProvider()
        self.state = "IDLE"
        self.assigned = None
        self.reconciliation_complete = True
        self.session_max = time.time() + 600
        self.pending = []
        self.pending_calls = 0
        self.lock_calls = []
        self.lock_error = None
        self.lock_started = threading.Event()
        self.lock_release = None
        self.attention_calls = []
        self.renewal_stopped = False
        self.client = SimpleNamespace(token="token-" + device_id, profile={})
        self.wake = threading.Event()
        self.lock = threading.RLock()
        self.pending_balance_reasons = []
        self.pending_balance_clears = 0

    def balance_context(self):
        return (
            self.device_id,
            self.api.get("operator_id"),
            tuple(self.api.get("payment_records", [])),
            self.config.get("balance_workflow_id"),
        )

    def balance_valid(self):
        return self.cache.valid(context=self.balance_context())

    def observe_balance(self, amount):
        self.cache.observe(amount, context=self.balance_context())

    def note_pending_balance(self, reason):
        self.pending_balance_reasons.append(reason)

    def clear_pending_balance_requirement(self):
        self.pending_balance_clears += 1

    def set_state(self, state, error=None):
        self.state = state

    def mark_dispatch_reserved(self):
        self.state = "DISPATCH_RESERVED"

    def session_available(self):
        return self.reconciliation_complete and self.state not in {
            "NEEDS_ATTENTION",
            "CONFIGURATION_ERROR",
            "AUTH_FAILED",
        }

    def dispatch_available(self):
        return (
            self.session_available()
            and self.assigned is None
            and self.state
            not in {
                "DISPATCH_RESERVED",
                "PAYING",
                "CONFIRMING",
            }
        )

    def request_pending(self):
        self.pending_calls += 1
        return list(self.pending)

    def _route(self):
        return object(), {"id": "payment"}

    def payment_readiness(self):
        try:
            self._route()
        except (KeyError, ValueError) as error:
            return False, str(error)
        return True, None

    def lock_withdrawal(self, item, *, claimed=False):
        self.lock_calls.append((item["id"], self.client.token, claimed))
        self.lock_started.set()
        self.assertions(item)
        if self.lock_release:
            self.lock_release.wait(2)
        if self.lock_error:
            raise self.lock_error
        return {
            **item,
            "lock_expires_at": future(),
        }

    def assertions(self, item):
        assert self.device_id in self.scheduler.reservations
        record = self.journal.current(self.device_id)
        assert record["phase"] == "LOCK_INTENT"
        assert record["withdraw_id"] == item["id"]
        assert self.assigned is None

    def accept_locked(self, raw, reservation):
        self.assigned = (raw, reservation)

    def attention(self, message, **details):
        self.attention_calls.append(message)
        self.state = "NEEDS_ATTENTION"
        self.journal.update(
            self.device_id,
            phase="NEEDS_ATTENTION",
            error=message,
            **details,
        )

    def stop_renewal(self):
        self.renewal_stopped = True


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "journal.json"
        self.journal = WithdrawJournal(self.path)
        self.scheduler = FakeScheduler()
        self.workers = {}
        self.work_ready = {}
        self.dispatcher = WithdrawDispatcher(
            lambda: self.workers,
            self.scheduler,
            self.journal,
            ready_for_work=lambda device_id: self.work_ready.get(device_id, True),
        )

    def worker(self, device, **kwargs):
        worker = FakeWorker(
            device, self.scheduler, self.journal, **kwargs
        )
        self.workers[device] = worker
        return worker

    def expose(self, item, *workers):
        for worker in workers:
            worker.pending = [item]

    def balance_observer(self):
        manager = WithdrawManager.__new__(WithdrawManager)
        manager.lock = threading.RLock()
        manager.workers = self.workers
        manager.dispatcher = self.dispatcher
        return manager

    def finish(self, worker, phase="CONFIRMED"):
        if worker.assigned:
            _, reservation = worker.assigned
            if self.scheduler.reservations.get(worker.device_id) == reservation:
                del self.scheduler.reservations[worker.device_id]
        worker.assigned = None
        worker.state = "IDLE"
        self.journal.update(worker.device_id, phase=phase)

    def test_ready_phone_is_selected_and_lock_precedes_permanent_pin(self):
        worker = self.worker("phone_A", balance="100.00")
        item = pending(1)
        self.expose(item, worker)
        with self.assertLogs("withdraw_v2", level="INFO") as captured:
            self.assertTrue(self.dispatcher.dispatch_once())
        self.assertEqual(worker.lock_calls, [(1, "token-phone_A", True)])
        self.assertEqual(worker.assigned[0]["id"], 1)
        output = "\n".join(captured.output)
        self.assertIn("selected device=phone_A withdrawal=1", output)
        self.assertIn("sending client-lock device=phone_A withdrawal=1", output)
        self.assertIn("lock accepted device=phone_A withdrawal=1", output)
        record = self.journal.current("phone_A")
        self.assertEqual(record["assigned_device_id"], "phone_A")
        self.assertEqual(record["operator_id"], 7)

    def test_not_ready_phone_is_excluded_before_external_lock(self):
        worker = self.worker("phone_A", balance="100.00")
        self.work_ready["phone_A"] = False
        self.expose(pending(1), worker)

        with self.assertLogs("withdraw_v2", level="INFO") as captured:
            self.dispatcher.dispatch_once()

        self.assertEqual(worker.lock_calls, [])
        self.assertIsNone(worker.assigned)
        self.assertEqual(self.scheduler.reservations, {})
        self.assertFalse(self.journal.current("phone_A"))
        self.assertEqual(worker.pending, [pending(1)])
        self.assertIn("reason=not_ready_for_work", "\n".join(captured.output))

    def test_invalid_transfer_methods_are_rejected_before_external_lock(self):
        worker = self.worker("phone_A", balance="100.00")
        invalid_items = []
        for method in (None, "unsupported"):
            item = pending(1)
            item["transfer_method"] = method
            invalid_items.append(item)
        missing_identifier = pending(2)
        missing_identifier.pop("account_number")
        invalid_items.append(missing_identifier)

        for item in invalid_items:
            worker.pending = [item]
            self.dispatcher.dispatch_once()

        self.assertEqual(worker.lock_calls, [])

    def test_missing_transfer_method_branch_is_rejected_before_external_lock(self):
        worker = self.worker("phone_A", balance="100.00")
        worker.payment_method_readiness = Mock(
            return_value=(False, "Payment workflow has no explicit branch")
        )
        self.expose(pending(1), worker)

        self.dispatcher.dispatch_once()

        self.assertEqual(worker.lock_calls, [])
        self.assertEqual(self.scheduler.reservations, {})

    def test_pending_diagnostics_log_only_sanitized_approved_fields(self):
        worker = self.worker("phone_A", balance="100.00")
        self.work_ready["phone_A"] = False
        item = pending(17, "12.34", tx_id="TX-17\nforged")
        item.update(
            {
                "payee_name": "PRIVATE-PAYEE",
                "account_number": "PRIVATE-ACCOUNT",
                "priority": "PRIVATE-PRIORITY",
            }
        )
        self.expose(item, worker)

        with self.assertLogs("withdraw_v2", level="INFO") as captured:
            self.dispatcher.dispatch_once()

        output = "\n".join(captured.output)
        self.assertIn("client-pending device=phone_A result_count=1", output)
        self.assertIn(
            "id=17 tx_id=TX-17 forged final_amount_present=True", output
        )
        self.assertNotIn("12.34", output)
        self.assertIn("reason=not_ready_for_work", output)
        self.assertNotIn("PRIVATE-PAYEE", output)
        self.assertNotIn("PRIVATE-ACCOUNT", output)
        self.assertNotIn("PRIVATE-PRIORITY", output)

    def test_marking_ready_allows_the_same_pending_item_to_dispatch(self):
        worker = self.worker("phone_A", balance="100.00")
        item = pending(1)
        self.work_ready["phone_A"] = False
        self.expose(item, worker)
        self.dispatcher.dispatch_once()
        self.assertEqual(worker.lock_calls, [])

        self.work_ready["phone_A"] = True
        self.assertTrue(self.dispatcher.dispatch_once())

        self.assertEqual([call[0] for call in worker.lock_calls], [1])

    def test_fresh_otp_assignment_does_not_require_credential_reference(self):
        worker = self.worker("phone_A", balance="100.00")
        worker.api["credential_reference"] = None
        item = pending(1)

        self.assertTrue(
            self.dispatcher._prepare_worker(worker, item, {"phone_A"}, {})
        )

    def test_missing_external_operator_identity_is_not_assigned(self):
        for missing_key in ("base_url", "operator_id", "username"):
            with self.subTest(missing_key=missing_key):
                worker = self.worker("phone_A", balance="100.00")
                worker.api["credential_reference"] = None
                worker.api[missing_key] = None
                item = pending(1)

                with self.assertLogs("withdraw_v2", level="INFO") as captured:
                    eligible = self.dispatcher._prepare_worker(
                        worker, item, {"phone_A"}, {}
                    )

                self.assertFalse(eligible)
                self.assertIn("reason=no_assignment", "\n".join(captured.output))
                self.workers.clear()

    def test_session_and_reconciliation_checks_remain_independent_of_assignment(self):
        worker = self.worker("phone_A", balance="100.00")
        worker.api["credential_reference"] = None
        item = pending(1)

        worker.reconciliation_complete = False
        with self.assertLogs("withdraw_v2", level="INFO") as captured:
            eligible = self.dispatcher._prepare_worker(
                worker, item, {"phone_A"}, {}
            )
        self.assertFalse(eligible)
        self.assertIn(
            "reason=session_not_reconciled", "\n".join(captured.output)
        )

        worker.reconciliation_complete = True
        worker.socket = SimpleNamespace(physical_connected=False, connected=False)
        with self.assertLogs("withdraw_v2", level="INFO") as captured:
            eligible = self.dispatcher._prepare_worker(
                worker, item, {"phone_A"}, {}
            )
        self.assertFalse(eligible)
        self.assertIn(
            "reason=realtime_not_connected", "\n".join(captured.output)
        )

    def test_fresh_otp_assignment_with_ready_realtime_session_can_dispatch(self):
        worker = self.worker("phone_A", balance="100.00")
        worker.api["credential_reference"] = None
        worker.socket = SimpleNamespace(physical_connected=True, connected=True)
        self.expose(pending(1), worker)

        self.assertTrue(self.dispatcher.dispatch_once())

        self.assertEqual(worker.lock_calls, [(1, "token-phone_A", True)])

    def test_readiness_is_rechecked_atomically_before_reservation_and_lock(self):
        worker = self.worker("phone_A", balance="100.00")
        item = pending(1)
        self.work_ready["phone_A"] = True
        self.assertTrue(
            self.dispatcher._prepare_worker(
                worker, item, {"phone_A"}, {}
            )
        )

        self.work_ready["phone_A"] = False
        self.assertIsNone(self.dispatcher._reserve_and_claim(worker, item))

        self.assertEqual(worker.lock_calls, [])
        self.assertEqual(self.scheduler.reservations, {})

    def test_ready_phone_is_selected_when_another_phone_is_not_ready(self):
        blocked = self.worker("phone_A", balance="100.00")
        ready = self.worker("phone_B", balance="100.00")
        self.work_ready["phone_A"] = False
        self.expose(pending(1), blocked, ready)

        self.assertTrue(self.dispatcher.dispatch_once())

        self.assertEqual(blocked.lock_calls, [])
        self.assertEqual([call[0] for call in ready.lock_calls], [1])

    def test_becoming_not_ready_does_not_interrupt_existing_assignment(self):
        worker = self.worker("phone_A", balance="100.00")
        self.expose(pending(1), worker)
        self.assertTrue(self.dispatcher.dispatch_once())
        assigned = worker.assigned
        reservation = self.scheduler.reservations["phone_A"]
        journal_before = dict(self.journal.current("phone_A"))

        self.work_ready["phone_A"] = False
        self.expose(pending(2), worker)
        self.dispatcher.dispatch_once()

        self.assertIs(worker.assigned, assigned)
        self.assertIs(self.scheduler.reservations["phone_A"], reservation)
        self.assertEqual([call[0] for call in worker.lock_calls], [1])
        self.assertEqual(self.journal.current("phone_A"), journal_before)
        self.assertFalse(worker.renewal_stopped)

    def test_false_to_true_manager_transition_wakes_dispatcher(self):
        manager = WithdrawManager.__new__(WithdrawManager)
        manager.scheduler = self.scheduler
        manager.assignments = Mock()
        manager.assignments.ready_for_work.return_value = False
        manager.assignments.set_ready_for_work.return_value = True
        manager.dispatcher = Mock()

        self.assertTrue(manager.set_work_readiness("phone_A", True))

        manager.assignments.set_ready_for_work.assert_called_once_with(
            "phone_A", True
        )
        manager.dispatcher.wake.assert_called_once_with()

    def test_one_eligible_pending_withdrawal_selects_phone_b(self):
        worker = self.worker(
            "phone_B", balance="100.00", payment_records=("5", "2b")
        )
        item = pending(936000, "25.00", payment_record="5")
        self.expose(item, worker)

        with self.assertLogs("withdraw_v2", level="INFO") as captured:
            self.assertTrue(self.dispatcher.dispatch_once())

        self.assertEqual(worker.lock_calls, [(936000, "token-phone_B", True)])
        self.assertIn(
            "selected device=phone_B withdrawal=936000",
            "\n".join(captured.output),
        )

    def test_busy_or_insufficient_phone_is_skipped_for_sufficient_phone(self):
        a = self.worker("phone_A", balance="5.00")
        b = self.worker("phone_B", balance="100.00")
        item = pending(1, "10.00")
        self.expose(item, a, b)
        self.dispatcher.dispatch_once()
        self.assertFalse(a.lock_calls)
        self.assertEqual(b.lock_calls[0][0], 1)

        self.finish(b)
        a.cache.observe(Decimal("100.00"), context=a.balance_context())
        self.scheduler.ready["phone_A"] = False
        self.expose(pending(2), a, b)
        self.dispatcher.dispatch_once()
        self.assertEqual([call[0] for call in b.lock_calls], [1, 2])

    def test_unknown_or_all_insufficient_balance_never_locks(self):
        provider = FakeBalanceProvider({"phone_A": ValueError("unknown")})
        a = self.worker("phone_A", provider=provider)
        b = self.worker("phone_B", balance="2.00")
        self.expose(pending(1, "10.00"), a, b)
        with self.assertLogs("withdraw_v2", level="INFO") as captured:
            self.dispatcher.dispatch_once()
        self.assertEqual(provider.calls, [])
        self.assertFalse(a.lock_calls)
        self.assertFalse(b.lock_calls)
        self.assertEqual(a.pending_balance_reasons, ["unknown"])
        self.assertEqual(b.pending_balance_reasons, ["insufficient"])
        self.assertEqual(a.state, "IDLE")
        self.assertEqual(self.scheduler.reservations, {})
        output = "\n".join(captured.output)
        self.assertIn("skip device=phone_A reason=balance_unknown", output)
        self.assertIn("skip device=phone_B reason=insufficient_balance", output)

    def test_explicit_balance_completion_updates_only_its_device_for_dispatch(self):
        a = self.worker("phone_A")
        b = self.worker("phone_B")
        item = pending(1, "10.00")
        self.expose(item, b)

        with self.assertLogs("withdraw_v2", level="INFO") as captured:
            self.balance_observer().workflow_completed(
                {
                    "status": "passed",
                    "device_id": "phone_B",
                    "workflow_id": "balance-phone_B",
                    "outputs": {"payer_balance": "RM 1,234.56"},
                }
            )
            self.assertTrue(self.dispatcher.dispatch_once())

        self.assertFalse(a.cache.valid())
        self.assertTrue(b.cache.valid())
        self.assertEqual(b.cache.available_balance, Decimal("1234.56"))
        self.assertEqual(b.lock_calls[0][0], 1)
        output = "\n".join(captured.output)
        self.assertNotIn("1234.56", output)
        self.assertNotIn("balance_cache_updated device=phone_B value=1234.56", output)
        self.assertNotIn("dispatcher_balance device=phone_B value=1234.56", output)
        self.assertNotIn("device=phone_B reason=balance_unknown", output)

    def test_successful_balance_output_survives_a_later_workflow_failure(self):
        worker = self.worker("phone_A")

        self.balance_observer().workflow_completed(
            {
                "status": "failed",
                "device_id": "phone_A",
                "workflow_id": "balance-phone_A",
                "outputs": {"payer_balance": "RM 321.09"},
            }
        )

        self.assertTrue(worker.cache.valid())
        self.assertEqual(worker.cache.available_balance, Decimal("321.09"))

    def test_missing_or_invalid_balance_output_does_not_update_cache(self):
        worker = self.worker("phone_A")
        observer = self.balance_observer()
        snapshots = (
            {
                "status": "failed",
                "outputs": {},
            },
            {
                "status": "passed",
                "outputs": {},
            },
            {
                "status": "passed",
                "outputs": {"payer_balance": "RM 10.00 RM 20.00"},
            },
        )
        with self.assertLogs("withdraw_v2", level="INFO") as captured:
            for snapshot in snapshots:
                observer.workflow_completed(
                    {
                        "device_id": "phone_A",
                        "workflow_id": "balance-phone_A",
                        **snapshot,
                    }
                )

        self.assertFalse(worker.cache.valid())
        output = "\n".join(captured.output)
        self.assertIn("reason=missing_output", output)
        self.assertIn("reason=invalid_output", output)

    def test_incomplete_payment_workflow_never_locks_and_other_phone_can_dispatch(self):
        incomplete = self.worker("phone_A", balance="100.00")
        ready = self.worker("phone_B", balance="100.00")
        incomplete._route = lambda: (_ for _ in ()).throw(
            ValueError("Add a receipt screenshot after the success check.")
        )
        item = pending(1)
        self.expose(item, incomplete, ready)

        self.assertTrue(self.dispatcher.dispatch_once())

        self.assertFalse(incomplete.lock_calls)
        self.assertEqual(ready.lock_calls[0][0], 1)

    def test_missing_balance_workflow_prerequisite_never_locks(self):
        worker = self.worker("phone_A", balance="100.00")
        worker._route = lambda: (_ for _ in ()).throw(
            ValueError("Assign a balance workflow to this device.")
        )
        self.expose(pending(1), worker)

        self.assertTrue(self.dispatcher.dispatch_once())

        self.assertFalse(worker.lock_calls)

    def test_post_receipt_action_does_not_block_dispatch(self):
        worker = self.worker("phone_A", balance="100.00")
        worker._route = lambda: (
            object(),
            payment_workflow(
                {
                    "steps": [
                        {"type": "wait_for_text"},
                        {"type": "capture_screen", "output_key": "receipt_path"},
                        {"type": "tap", "label": "Confirm Payment"},
                    ]
                }
            ),
        )
        self.expose(pending(1), worker)

        self.assertTrue(self.dispatcher.dispatch_once())

        self.assertEqual(worker.lock_calls[0][0], 1)

    def test_phone_becomes_dispatch_eligible_after_payment_workflow_is_fixed(self):
        worker = self.worker("phone_A", balance="100.00")
        valid_route = worker._route
        worker._route = lambda: (_ for _ in ()).throw(
            ValueError("Payment workflow is incomplete.")
        )
        item = pending(1)
        self.expose(item, worker)

        self.assertTrue(self.dispatcher.dispatch_once())
        self.assertFalse(worker.lock_calls)

        worker._route = valid_route
        self.assertTrue(self.dispatcher.dispatch_once())
        self.assertEqual(worker.lock_calls[0][0], 1)

    def test_unknown_balance_requests_refresh_before_locking(self):
        provider = FakeBalanceProvider({"phone_A": Decimal("100.00")})
        worker = self.worker("phone_A", provider=provider)
        self.expose(pending(1), worker)
        with self.assertLogs("withdraw_v2", level="INFO") as captured:
            self.dispatcher.dispatch_once()

        self.assertEqual(provider.calls, [])
        self.assertEqual(worker.pending_balance_reasons, ["unknown"])
        self.assertEqual(worker.state, "IDLE")
        self.assertFalse(worker.lock_calls)
        self.assertIn(
            "skip device=phone_A reason=balance_unknown",
            "\n".join(captured.output),
        )

    def test_stale_balance_refreshes_before_claim_and_top_up_becomes_eligible(self):
        worker = self.worker("phone_A", balance="20.00")
        worker.cancel_withdrawal = Mock()
        item = pending(1, "100.00")
        self.expose(item, worker)

        self.dispatcher.dispatch_once()
        self.assertFalse(worker.lock_calls)
        worker.cancel_withdrawal.assert_not_called()
        self.assertEqual(worker.pending_balance_reasons, ["insufficient"])
        self.assertEqual(worker.pending, [item])

        worker.cache.invalidate()
        self.dispatcher.dispatch_once()
        self.assertFalse(worker.lock_calls)
        self.assertEqual(worker.pending_balance_reasons[-1], "stale")

        worker.observe_balance(Decimal("220.00"))
        self.assertTrue(self.dispatcher.dispatch_once())
        self.assertEqual(worker.lock_calls[0][0], 1)

    def test_phone_that_cannot_see_pending_item_is_not_selected(self):
        a = self.worker("phone_A", balance="2.00")
        b = self.worker("phone_B", balance="100.00")
        a.pending = [pending(1, "10.00")]
        b.pending = []
        self.dispatcher.dispatch_once()
        self.assertFalse(a.lock_calls)
        self.assertFalse(b.lock_calls)

    def test_attention_and_unresolved_phone_do_not_block_other_phone(self):
        a = self.worker("phone_A", balance="100.00")
        b = self.worker("phone_B", balance="100.00")
        a.state = "NEEDS_ATTENTION"
        self.journal.begin(
            "phone_A", phase="NEEDS_ATTENTION", withdraw_id=99, tx_id="OLD"
        )
        item = pending(1)
        self.expose(item, a, b)
        self.dispatcher.dispatch_once()
        self.assertFalse(a.lock_calls)
        self.assertEqual(b.lock_calls[0][0], 1)

    def test_round_robin_assigns_two_items_to_two_eligible_phones(self):
        a = self.worker("phone_A", balance="100.00")
        b = self.worker("phone_B", balance="100.00")
        items = [pending(1), pending(2)]
        a.pending = items
        b.pending = items
        self.dispatcher.dispatch_once()
        self.assertEqual([call[0] for call in a.lock_calls], [1])
        self.assertEqual([call[0] for call in b.lock_calls], [2])

    def test_server_order_is_preserved_but_later_affordable_item_can_run(self):
        worker = self.worker("phone_A", balance="5.00")
        worker.pending = [pending(1, "10.00"), pending(2, "4.00")]
        self.dispatcher.dispatch_once()
        self.assertEqual([call[0] for call in worker.lock_calls], [2])

    def test_duplicate_snapshots_and_concurrent_triggers_lock_once(self):
        a = self.worker("phone_A", balance="100.00")
        b = self.worker("phone_B", balance="100.00")
        item = pending(1)
        self.expose(item, a, b)
        a.lock_release = threading.Event()
        first = threading.Thread(target=self.dispatcher.dispatch_once)
        second = threading.Thread(target=self.dispatcher.dispatch_once)
        first.start()
        self.assertTrue(a.lock_started.wait(1))
        second.start()
        second.join(1)
        a.lock_release.set()
        first.join(1)
        self.assertEqual(len(a.lock_calls) + len(b.lock_calls), 1)
        self.assertEqual(a.pending_calls, 1)
        self.assertEqual(b.pending_calls, 1)

    def test_two_withdrawals_cannot_reserve_the_same_phone(self):
        a = self.worker("phone_A", balance="100.00")
        a.pending = [pending(1), pending(2)]
        self.dispatcher.dispatch_once()
        self.assertEqual([call[0] for call in a.lock_calls], [1])
        self.assertEqual(a.assigned[0]["id"], 1)

    def test_global_unresolved_and_confirmed_identities_block_other_phone(self):
        a = self.worker("phone_A", balance="100.00")
        b = self.worker("phone_B", balance="100.00")
        self.journal.begin(
            "phone_A", phase="NEEDS_ATTENTION", withdraw_id=1, tx_id="TX-1"
        )
        a.state = "NEEDS_ATTENTION"
        self.expose(pending(1), b)
        self.dispatcher.dispatch_once()
        self.assertFalse(b.lock_calls)

        self.journal.update("phone_A", phase="CONFIRMED")
        self.expose(pending(2, tx_id="TX-1"), b)
        self.dispatcher.dispatch_once()
        self.assertFalse(b.lock_calls)

    def test_durable_identity_guard_survives_journal_restart(self):
        self.journal.begin(
            "phone_A", phase="NEEDS_ATTENTION", withdraw_id=1, tx_id="TX-1"
        )
        restarted = WithdrawJournal(self.path)
        self.assertEqual(
            restarted.identity_conflict(1, "TX-1"),
            ("unresolved", "phone_A"),
        )
        restarted.update("phone_A", phase="CONFIRMED")
        self.assertEqual(
            WithdrawJournal(self.path).identity_conflict(2, "TX-1"),
            ("confirmed", "phone_A"),
        )

    def test_released_withdrawal_can_be_dispatched_again(self):
        claimed, _ = self.journal.claim(
            "phone_A",
            withdraw_id=1,
            tx_id="TX-1",
            operator_id=7,
            final_amount="10.00",
        )
        self.assertTrue(claimed)
        self.journal.update("phone_A", phase="RELEASED")
        worker = self.worker("phone_B", balance="100.00")
        self.expose(pending(1, tx_id="TX-1"), worker)
        self.assertTrue(self.dispatcher.dispatch_once())
        self.assertEqual(worker.lock_calls, [(1, "token-phone_B", True)])

    def test_released_tx_id_can_be_claimed_with_new_withdraw_id(self):
        claimed, _ = self.journal.claim(
            "phone_A",
            withdraw_id=1,
            tx_id="TX-1",
            operator_id=7,
            final_amount="10.00",
        )
        self.assertTrue(claimed)
        self.journal.update("phone_A", phase="RELEASED")
        claimed, conflict = self.journal.claim(
            "phone_B",
            withdraw_id=2,
            tx_id="TX-1",
            operator_id=12,
            final_amount="10.00",
        )
        self.assertTrue(claimed)
        self.assertIsNone(conflict)

    def test_archived_released_identity_is_retryable_after_restart(self):
        claimed, _ = self.journal.claim(
            "phone_A",
            withdraw_id=935402,
            tx_id="TX9F3K2",
            operator_id=7,
            final_amount="10.00",
        )
        self.assertTrue(claimed)
        self.journal.update("phone_A", phase="RELEASED")
        self.journal.begin(
            "phone_A", phase="LOCK_REJECTED", withdraw_id=2, tx_id="TX-2"
        )

        restarted = WithdrawJournal(self.path)
        self.assertIsNone(restarted.identity_conflict(935402, "TX9F3K2"))
        claimed, conflict = restarted.claim(
            "phone_B",
            withdraw_id=935402,
            tx_id="TX9F3K2",
            operator_id=12,
            final_amount="10.00",
        )
        self.assertTrue(claimed)
        self.assertIsNone(conflict)

    def test_cancelled_and_confirmed_identities_remain_blocked(self):
        for phase, expected in (
            ("CANCELLED", "previously_locked"),
            ("CONFIRMED", "confirmed"),
        ):
            with self.subTest(phase=phase):
                path = Path(self.temp.name) / f"{phase}.json"
                journal = WithdrawJournal(path)
                claimed, _ = journal.claim(
                    "phone_A",
                    withdraw_id=1,
                    tx_id="TX-1",
                    operator_id=7,
                    final_amount="10.00",
                )
                self.assertTrue(claimed)
                journal.update("phone_A", phase=phase)
                self.assertEqual(
                    journal.identity_conflict(2, "TX-1"),
                    (expected, "phone_A"),
                )
                claimed, conflict = journal.claim(
                    "phone_B",
                    withdraw_id=1,
                    tx_id="TX-NEW",
                    operator_id=12,
                    final_amount="10.00",
                )
                self.assertFalse(claimed)
                self.assertEqual(conflict, (expected, "phone_A"))

    def test_needs_attention_blocks_but_lock_rejected_is_retryable(self):
        claimed, _ = self.journal.claim(
            "phone_A",
            withdraw_id=1,
            tx_id="TX-1",
            operator_id=7,
            final_amount="10.00",
        )
        self.assertTrue(claimed)
        self.journal.update("phone_A", phase="NEEDS_ATTENTION")
        claimed, conflict = self.journal.claim(
            "phone_B",
            withdraw_id=1,
            tx_id="TX-1",
            operator_id=12,
            final_amount="10.00",
        )
        self.assertFalse(claimed)
        self.assertEqual(conflict, ("unresolved", "phone_A"))

        self.journal.update("phone_A", phase="LOCK_REJECTED")
        claimed, conflict = self.journal.claim(
            "phone_B",
            withdraw_id=1,
            tx_id="TX-1",
            operator_id=12,
            final_amount="10.00",
        )
        self.assertTrue(claimed)
        self.assertIsNone(conflict)

    def test_definite_lock_failures_clear_reservation_without_assignment(self):
        for code in (404, 423):
            with self.subTest(code=code):
                worker = self.worker("phone_A", balance="100.00")
                worker.lock_error = APIError(code, "rejected")
                self.expose(pending(code), worker)
                self.dispatcher.dispatch_once()
                self.assertIsNone(worker.assigned)
                self.assertNotIn("phone_A", self.scheduler.reservations)
                self.assertEqual(
                    self.journal.current("phone_A")["phase"], "LOCK_REJECTED"
                )
                self.workers.clear()

    def test_payment_record_rejection_tries_next_owning_session(self):
        a = self.worker("phone_A", balance="100.00")
        b = self.worker("phone_B", balance="100.00")
        a.lock_error = APIError(409, "Payment record is not accessible")
        item = pending(1)
        self.expose(item, a, b)
        self.dispatcher.dispatch_once()
        self.assertEqual(a.lock_calls[0][1], "token-phone_A")
        self.assertEqual(b.lock_calls[0][1], "token-phone_B")
        self.assertEqual(b.assigned[0]["id"], 1)

    def test_ambiguous_lock_stays_unresolved_and_cannot_redispatch(self):
        a = self.worker("phone_A", balance="100.00")
        b = self.worker("phone_B", balance="100.00")
        a.lock_error = UncertainResult()
        item = pending(1)
        self.expose(item, a, b)
        self.dispatcher.dispatch_once()
        self.assertEqual(a.state, "NEEDS_ATTENTION")
        self.assertEqual(
            self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION"
        )
        self.dispatcher.dispatch_once()
        self.assertFalse(b.lock_calls)

    def test_ambiguous_lock_persists_safe_stage_and_identity_diagnostics(self):
        worker = self.worker("phone_A", balance="100.00")
        error = UncertainResult()
        error.withdraw_stage = "client-lock-action"
        worker.lock_error = error
        item = pending(1)
        self.expose(item, worker)

        with self.assertLogs("withdraw_v2", level="WARNING") as captured:
            self.dispatcher.dispatch_once()

        record = self.journal.current("phone_A")
        self.assertEqual(record["phase"], "NEEDS_ATTENTION")
        self.assertEqual(record["withdraw_id"], item["id"])
        self.assertEqual(record["tx_id"], item["tx_id"])
        self.assertEqual(record["ambiguity_operation"], "client-lock")
        self.assertEqual(record["ambiguity_stage"], "client-lock-action")
        self.assertEqual(record["ambiguity_error_type"], "UncertainResult")
        self.assertIn("outcome is uncertain", record["ambiguity_reason"])
        self.assertIn("withdraw_id=1", "\n".join(captured.output))
        self.assertEqual(len(worker.lock_calls), 1)

        self.dispatcher.dispatch_once()

        self.assertEqual(len(worker.lock_calls), 1)

    def test_payment_record_metadata_filters_incompatible_session(self):
        a = self.worker(
            "phone_A", balance="100.00", payment_records=("another",)
        )
        b = self.worker("phone_B", balance="100.00", payment_records=("2b",))
        item = pending(1, payment_record="2b")
        self.expose(item, a, b)
        self.dispatcher.dispatch_once()
        self.assertFalse(a.lock_calls)
        self.assertEqual(b.lock_calls[0][0], 1)

    def test_dispatch_waits_until_every_session_reconciled(self):
        a = self.worker("phone_A", balance="100.00")
        b = self.worker("phone_B", balance="100.00")
        b.reconciliation_complete = False
        self.expose(pending(1), a, b)
        self.assertFalse(self.dispatcher.dispatch_once())
        self.assertEqual(a.pending_calls + b.pending_calls, 0)
        b.reconciliation_complete = True
        self.assertTrue(self.dispatcher.dispatch_once())
        self.assertEqual(a.pending_calls + b.pending_calls, 2)

    def test_auth_failed_phone_does_not_block_healthy_phone(self):
        a = self.worker("phone_A", balance="100.00")
        b = self.worker("phone_B", balance="100.00")
        a.reconciliation_complete = False
        a.state = "AUTH_FAILED"
        a.connected = False
        self.expose(pending(1), b)

        self.assertTrue(self.dispatcher.dispatch_once())
        self.assertEqual(a.pending_calls, 0)
        self.assertEqual(b.pending_calls, 1)


if __name__ == "__main__":
    unittest.main()
