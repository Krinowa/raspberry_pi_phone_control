"""Mocked Withdraw Bot v2 transport and per-device safety tests."""

import json
import queue
import ssl
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import websocket
import certifi

from automation.integrations.withdraw_v2.balance import select_pending
from automation.integrations.withdraw_v2.client import (
    APIError,
    UncertainResult,
    WithdrawClient,
)
from automation.integrations.withdraw_v2.journal import WithdrawJournal
from automation.integrations.withdraw_v2.manager import WithdrawManager
from automation.integrations.withdraw_v2.normalization import (
    normalize_locked_withdrawal,
)
from automation.integrations.withdraw_v2.socket import (
    PusherSocket,
    SharedPusherTransport,
    _settings,
)
from automation.integrations.withdraw_v2.worker import (
    AuthenticationFailed,
    WithdrawWorker,
    payment_workflow,
    receipt_contract,
)
from automation.transfer_methods import transfer_method_fingerprint
from automation.jobs.service import JobScheduler
from automation.jobs.storage import AccountBindingStore, JobStore
from automation.jobs.storage import RecordConflict, RecordNotFound
from automation.workflows.validation import resolve_variables


def future(seconds=600):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def withdrawal(*, ident=935402, tx_id="TX9F3K2", final="4950.00", expires=None):
    return {
        "id": ident,
        "order_id": "WT20261005A1",
        "tx_id": tx_id,
        "merchant_name": "Demo Merchant",
        "type": "bank",
        "transfer_method": "bank_account",
        "withdraw_amount": "5000.00",
        "service_charges": "1.00",
        "service_charges_amount": "50.00",
        "final_amount": final,
        "bank_name": "Bank Name",
        "account_name": "Account Name",
        "account_number": "6222000000000000",
        "holder_name": "Holder Name",
        "mobile_no": "13800000000",
        "remark": None,
        "proofs": [],
        "created_at": "2026-10-05 10:00:00",
        "lock_expires_at": expires or future(),
    }


class DummySocket:
    connected = True
    physical_connected = True
    session_state = "READY"
    error = None
    generation = 1

    def wait_ready(self, _timeout):
        return self.connected

    def close(self):
        self.connected = False
        self.physical_connected = False
        self.session_state = "DISCONNECTED"


class DummyClient:
    def __init__(self, profile=None):
        self.profile = profile
        self.token = profile.get("token") if profile else None
        self.login = Mock(return_value=profile) if profile else Mock()
        self.reconcile_confirmation = Mock(return_value=False)
        self.confirm_with_receipt = Mock(return_value={})
        self.logout = Mock()
        self.close = Mock()


def realtime(session, queues):
    return {
        "host": "realtime.example",
        "port": 443,
        "key": "app-key",
        "scheme": "https",
        "auth_endpoint": "/api/broadcasting/auth",
        "channels": {"session": session, "queue": queues},
    }


class FakeAuthClient:
    def __init__(self, token, profile=None):
        self.token = token
        self.profile = profile
        self.calls = []
        self.fail = set()

    def authorize(self, endpoint, socket_id, channel):
        self.calls.append((endpoint, socket_id, channel, self.token))
        if channel in self.fail:
            raise APIError(401, "Expired logical session")
        return {"auth": f"{self.token}:{channel}"}


class FakeWebSocket:
    def __init__(self, socket_id):
        self.incoming = queue.Queue()
        self.sent = []
        self.closed = False
        self.incoming.put(
            json.dumps(
                {
                    "event": "pusher:connection_established",
                    "data": json.dumps(
                        {"socket_id": socket_id, "activity_timeout": 60}
                    ),
                }
            )
        )

    def settimeout(self, _timeout):
        pass

    def recv(self):
        try:
            value = self.incoming.get(timeout=0.02)
        except queue.Empty:
            raise websocket.WebSocketTimeoutException() from None
        if isinstance(value, Exception):
            raise value
        return value

    def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message["event"] == "pusher:subscribe":
            self.emit(
                "pusher_internal:subscription_succeeded",
                channel=message["data"]["channel"],
            )

    def emit(self, event, *, channel=None, data=None):
        message = {"event": event, "data": data or {}}
        if channel is not None:
            message["channel"] = channel
        self.incoming.put(json.dumps(message))

    def fail_connection(self):
        self.incoming.put(ConnectionError("connection lost"))

    def close(self):
        self.closed = True
        self.incoming.put(ConnectionError("closed"))


class FakeConnections:
    def __init__(self):
        self.sockets = []

    def __call__(self, _url, **_kwargs):
        socket = FakeWebSocket(f"socket-{len(self.sockets) + 1}")
        self.sockets.append(socket)
        return socket


class FailOnceConnections(FakeConnections):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def __call__(self, url, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("first connection failed")
        return super().__call__(url, **kwargs)


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class PaymentWorkflowValidationTests(unittest.TestCase):
    @staticmethod
    def workflow(*steps):
        return {"id": "payment", "steps": list(steps)}

    def test_canonical_wait_and_receipt_screenshot_are_ready(self):
        workflow = self.workflow(
            {"type": "wait_for_text", "text": "Successful"},
            {"type": "capture_screen", "output_key": "receipt_path"},
        )
        self.assertFalse(any(step.get("critical") for step in workflow["steps"]))
        self.assertIs(payment_workflow(workflow), workflow)
        self.assertEqual(
            receipt_contract(workflow),
            {
                "wait_for_text_found": True,
                "screenshot_found": True,
                "screenshot_output": "receipt_path",
                "valid": True,
                "reason": None,
            },
        )

    def test_wrong_or_missing_receipt_output_is_not_ready(self):
        for screenshot in (
            {"type": "capture_screen"},
            {"type": "capture_screen", "output_key": "another_path"},
        ):
            with self.subTest(screenshot=screenshot), self.assertRaisesRegex(
                ValueError, "Add a Capture Screenshot step after the success check"
            ):
                payment_workflow(self.workflow({"type": "wait_for_text"}, screenshot))

    def test_wait_and_receipt_are_both_required(self):
        with self.assertRaisesRegex(
            ValueError, "Add a success check before Capture Screenshot"
        ):
            payment_workflow(
                self.workflow({"type": "capture_screen", "output_key": "receipt_path"})
            )
        with self.assertRaisesRegex(ValueError, "Capture Screenshot"):
            payment_workflow(self.workflow({"type": "wait_for_text"}))

    def test_post_receipt_steps_do_not_affect_readiness(self):
        trailing_steps = [
            {"type": "wait", "seconds": 1},
            {"type": "wait_until_stable"},
            {"type": "ocr_value"},
            {"type": "capture_screen"},
            {"type": "key", "key": "HOME"},
            {"type": "key", "key": "back"},
            {"type": "tap"},
            {"type": "tap", "label": "Confirm Payment"},
            {"type": "swipe"},
            {"type": "input", "source": "variable", "variable": "amount"},
            {"type": "manual_checkpoint"},
        ]
        for trailing in trailing_steps:
            with self.subTest(trailing=trailing):
                workflow = self.workflow(
                    {"type": "wait_for_text"},
                    {"type": "capture_screen", "output_key": "receipt_path"},
                    trailing,
                )
                self.assertIs(payment_workflow(workflow), workflow)

    def test_post_receipt_conditional_is_not_inspected(self):
        workflow = self.workflow(
            {"type": "wait_for_text"},
            {"type": "capture_screen", "output_key": "receipt_path"},
            {
                "type": "if_text",
                "then_steps": [
                    {"type": "input", "source": "variable", "variable": "amount"},
                    {"type": "tap", "label": "Confirm Payment"},
                ],
                "else_steps": [{"type": "swipe"}],
            },
        )
        self.assertIs(payment_workflow(workflow), workflow)

    def test_only_ordering_matters_between_success_wait_and_receipt(self):
        workflow = self.workflow(
            {"type": "wait_for_text"},
            {"type": "tap"},
            {"type": "capture_screen", "output_key": "receipt_path"},
        )
        self.assertIs(payment_workflow(workflow), workflow)

    def test_current_assigned_workflow_satisfies_receipt_contract(self):
        workflow = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "automation/workflow_data/flow_4a1246b891b2403780b9596d8f45eb21.json"
            ).read_text()
        )
        self.assertEqual(workflow["purpose"], "payment")
        self.assertFalse(any(step.get("critical") for step in workflow["steps"]))
        success_index = next(
            index
            for index, step in enumerate(workflow["steps"])
            if step.get("type") == "wait_for_text" and step.get("text") == "Successful"
        )
        receipt_index = next(
            index
            for index, step in enumerate(workflow["steps"])
            if step.get("type") == "capture_screen"
            and step.get("output_key") == "receipt_path"
        )
        self.assertLess(success_index, receipt_index)
        self.assertIs(payment_workflow(workflow), workflow)


class WithdrawWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "screens"
        self.root.mkdir()
        self.journal = WithdrawJournal(Path(self.temp.name) / "journal.json")
        self.scheduler = SimpleNamespace(
            lock=threading.RLock(),
            reservations={},
            is_device_ready=lambda _: {"ready": True},
            next_job_for_device=lambda _: None,
        )
        self.executor = SimpleNamespace(wake=Mock(), cancel=Mock())
        self.config = {
            "balance_workflow_id": "balance",
            "withdraw_api": {
                "enabled": True,
                "base_url": "https://withdraw.example",
                "operator_id": 42,
                "username": "operator_A",
                "credential_reference": "env:WITHDRAW_A",
                "balance_refresh_seconds": 60,
            },
        }

    def worker(
        self,
        device="phone_A",
        client=None,
        renew_seconds=0.01,
        account_binding_provisioner=None,
    ):
        return WithdrawWorker(
            device,
            self.config,
            assignments=Mock(),
            accounts=Mock(),
            workflows=Mock(),
            scheduler=self.scheduler,
            executor=self.executor,
            journal=self.journal,
            balance_provider=Mock(),
            client=client or DummyClient(),
            screenshot_root=self.root,
            credentials=lambda _: ("password", "JBSWY3DPEHPK3PXP"),
            account_binding_provisioner=account_binding_provisioner,
            renew_seconds=renew_seconds,
        )

    def balance_ready_worker(self, value="616.50"):
        worker = self.worker()
        worker.state = "IDLE"
        worker.session_available = Mock(return_value=True)
        worker.payment_readiness = Mock(return_value=(True, None))
        worker.balance_readiness = Mock(return_value=(True, None))
        worker.balance_provider.get_available_balance.return_value = Decimal(value)
        return worker

    def test_pending_and_locked_transfer_methods_must_match(self):
        worker = self.worker()
        pending = withdrawal()
        locked = {
            **pending,
            "transfer_method": "phone_no",
            "type": "kuaizhuan",
            "mobile_no": "01234567",
        }
        worker.action = Mock(return_value={"withdrawal": locked})

        with self.assertRaisesRegex(
            ValueError, "Locked withdrawal does not match"
        ):
            worker.lock_withdrawal(pending)

        record = self.journal.current("phone_A")
        self.assertEqual(record["phase"], "LOCKED")
        self.assertEqual(record["transfer_method"], "phone_no")

    def waiting_cancel_worker(
        self, status="FAILED", *, action=None, continue_retry=None
    ):
        worker = self.worker()
        worker.socket = DummySocket()
        raw = withdrawal(final="10.00")
        job = SimpleNamespace(
            job_id=f"job-{status.lower()}",
            action="withdraw_v2",
            device_id="phone_A",
            external_ref=raw["tx_id"],
            workflow="payment-workflow",
            account_id="account-A",
            retry_of_job_id=None,
            status=status,
            failed_step_index=33,
        )
        self.scheduler.get_job = Mock(return_value=job)
        self.journal.begin(
            "phone_A",
            phase="QUEUED",
            withdraw_id=raw["id"],
            tx_id=raw["tx_id"],
            assigned_device_id="phone_A",
            operator_id=42,
            account_id=job.account_id,
            workflow_id=job.workflow,
            job_id=job.job_id,
            lock_expires_at=raw["lock_expires_at"],
            raw_withdrawal=raw,
        )
        worker.assigned = (raw, "dispatch-token")
        self.scheduler.reservations["phone_A"] = "dispatch-token"
        worker.cache.valid = Mock(return_value=True)
        worker.cache.available_balance = Decimal("1000.00")
        worker._route = Mock(
            return_value=(
                SimpleNamespace(account_id=job.account_id, device_id="phone_A"),
                {"id": job.workflow, "name": "Payment", "steps": []},
            )
        )
        def wait_for_operator_action(*_args, **_kwargs):
            retry = worker._wait_for_retry(job)
            if continue_retry is not None:
                continue_retry.wait(1)
            return retry

        worker._pay = Mock(side_effect=wait_for_operator_action)
        worker.action = action or Mock(return_value={"ok": True})
        worker.stop_renewal = Mock()

        thread = threading.Thread(
            target=lambda: self._run_assigned_with_operation(worker),
        )
        thread.start()
        self.assertTrue(wait_for(lambda: worker.retry_waiting_job_id == job.job_id))
        self.assertTrue(worker.withdrawal_operation.locked())
        return worker, job, raw, thread

    @staticmethod
    def _run_assigned_with_operation(worker):
        with worker.withdrawal_operation:
            worker._execute_assigned()

    def persisted_retry_worker(
        self,
        *,
        client=None,
        raw=None,
        start_step_index=36,
        journal_overrides=None,
    ):
        raw = raw or withdrawal()
        parent = SimpleNamespace(
            job_id="job-original",
            retry_of_job_id=None,
            action="withdraw_v2",
            device_id="phone_A",
            account_id="account-A",
            workflow="payment-workflow",
            external_ref=raw["tx_id"],
            status="FAILED",
            failed_step_index=start_step_index,
        )
        retry = SimpleNamespace(
            job_id="job-retry",
            retry_of_job_id=parent.job_id,
            action="withdraw_v2",
            device_id="phone_A",
            account_id=parent.account_id,
            workflow=parent.workflow,
            external_ref=raw["tx_id"],
            status="WAITING_FOR_DEVICE",
            start_step_index=start_step_index,
        )
        self.scheduler.get_job = Mock(
            side_effect=lambda job_id: retry if job_id == retry.job_id else parent
        )
        journal = {
            "phase": "QUEUED",
            "withdraw_id": raw["id"],
            "tx_id": raw["tx_id"],
            "assigned_device_id": "phone_A",
            "operator_id": 42,
            "account_id": retry.account_id,
            "job_id": retry.job_id,
            "retry_of_job_id": retry.retry_of_job_id,
            "workflow_id": retry.workflow,
            "final_amount": raw["final_amount"],
            "raw_withdrawal": raw,
            "lock_expires_at": raw["lock_expires_at"],
        }
        journal.update(journal_overrides or {})
        self.journal.begin("phone_A", **journal)
        return self.worker(client=client), raw, retry

    def install_live_renewal_stub(self, worker):
        thread = Mock()
        thread.is_alive.return_value = True

        def start():
            worker.renew_thread = thread

        worker._start_renewal = Mock(side_effect=start)
        return thread

    def test_initial_balance_refresh_runs_once_when_worker_becomes_usable(self):
        worker = self.balance_ready_worker()

        self.assertTrue(worker._maybe_refresh_balance())
        self.assertTrue(worker.balance_valid())
        self.assertEqual(worker.cache.available_balance, Decimal("616.50"))
        self.assertFalse(worker._maybe_refresh_balance())
        worker.balance_provider.get_available_balance.assert_called_once_with("phone_A")

    def test_worker_status_retains_stale_value_and_clears_changed_account_context(self):
        worker = self.balance_ready_worker()
        worker.observe_balance(Decimal("616.50"))
        worker.cache.observed_monotonic = (
            time.monotonic() - worker.cache.refresh_seconds - 1
        )

        stale = worker.status()
        self.assertEqual(stale["balance_state"], "STALE")
        self.assertFalse(stale["balance_valid"])
        self.assertEqual(stale["last_balance"], "616.50")
        self.assertIsNotNone(stale["balance_observed_at"])

        worker.api["operator_id"] = 43
        changed = worker.status()
        self.assertEqual(changed["balance_state"], "UNKNOWN")
        self.assertIsNone(changed["last_balance"])
        self.assertIsNone(changed["balance_observed_at"])

    def test_changed_balance_workflow_clears_observation_and_triggers_initial_read(
        self,
    ):
        worker = self.balance_ready_worker("710.25")
        worker.observe_balance(Decimal("616.50"))

        worker.config["balance_workflow_id"] = "replacement-balance"
        changed = worker.status()
        self.assertEqual(changed["balance_state"], "UNKNOWN")
        self.assertIsNone(changed["last_balance"])
        self.assertFalse(worker.balance_initial_attempted)

        self.assertTrue(worker._maybe_refresh_balance())
        worker.balance_provider.get_available_balance.assert_called_once_with("phone_A")
        self.assertEqual(worker.cache.available_balance, Decimal("710.25"))

    def test_invalid_balance_purpose_discards_trusted_observation(self):
        worker = self.worker()
        worker.observe_balance(Decimal("616.50"))
        worker.assignments.integration.return_value = {
            "balance_workflow_id": "changed-purpose"
        }
        worker.workflows.read.return_value = {
            "purpose": "payment",
            "steps": [
                {
                    "type": "ocr_value",
                    "parser": "currency",
                    "output": "payer_balance",
                }
            ],
        }

        ready, error = worker.balance_readiness()

        self.assertFalse(ready)
        self.assertIn("Balance Check purpose", error)
        self.assertIsNone(worker.cache.available_balance)
        self.assertFalse(worker.balance_initial_attempted)

    def test_manual_balance_conflict_terminates_request_instead_of_staying_checking(
        self,
    ):
        worker = self.balance_ready_worker()
        worker.balance_initial_attempted = True
        worker.balance_provider.get_available_balance.side_effect = RecordConflict(
            "Device is not ready for a balance check."
        )
        worker.request_balance_refresh("manual")
        worker.request_balance_refresh("manual")

        self.assertFalse(worker._maybe_refresh_balance())
        self.assertEqual(worker.status()["balance_state"], "ERROR")
        self.assertIsNone(worker.balance_refresh_request_reason)
        self.assertFalse(worker._maybe_refresh_balance())
        worker.balance_provider.get_available_balance.assert_called_once_with(
            "phone_A", manual_recovery=True
        )

    def test_needs_attention_allows_only_manual_balance_recovery(self):
        worker = self.balance_ready_worker()
        worker.balance_initial_attempted = True
        worker.state = "NEEDS_ATTENTION"
        worker.request_balance_refresh("pending")

        self.assertFalse(worker._maybe_refresh_balance())
        worker.balance_provider.get_available_balance.assert_not_called()

        worker.request_balance_refresh("manual")
        self.assertTrue(worker._maybe_refresh_balance())
        worker.balance_provider.get_available_balance.assert_called_once_with(
            "phone_A", manual_recovery=True
        )
        self.assertEqual(worker.status()["balance_state"], "VALID")

    def test_manual_balance_requires_healthy_realtime_and_clears_failed_request(self):
        worker = self.balance_ready_worker()
        worker.balance_initial_attempted = True
        worker.state = "NEEDS_ATTENTION"
        worker.session_available.return_value = False
        worker.request_balance_refresh("manual")

        self.assertFalse(worker._maybe_refresh_balance())
        self.assertEqual(worker.status()["balance_state"], "ERROR")
        self.assertIn("realtime", worker.status()["balance_error"])
        self.assertIsNone(worker.balance_refresh_request_reason)
        worker.balance_provider.get_available_balance.assert_not_called()

    def test_manual_balance_provider_failure_clears_refreshing_state(self):
        worker = self.balance_ready_worker()
        worker.observe_balance(Decimal("10.00"))
        worker.state = "NEEDS_ATTENTION"
        worker.balance_provider.get_available_balance.side_effect = RuntimeError(
            "OCR failed"
        )
        worker.request_balance_refresh("manual")

        self.assertTrue(worker._maybe_refresh_balance())
        status = worker.status()
        self.assertEqual(status["balance_state"], "ERROR")
        self.assertEqual(status["balance_error"], "OCR failed")
        self.assertFalse(worker.balance_refreshing)
        self.assertIsNone(worker.balance_refresh_request_reason)

    def test_insufficient_pending_refreshes_on_cadence_and_stops_when_cleared(self):
        worker = self.balance_ready_worker("220.00")
        worker.observe_balance(Decimal("20.00"))
        worker.note_pending_balance("insufficient")

        self.assertGreater(worker.balance_retry_at, time.monotonic())
        self.assertAlmostEqual(
            worker.balance_retry_at - time.monotonic(),
            worker.cache.refresh_seconds,
            delta=1,
        )
        self.assertFalse(worker._maybe_refresh_balance())
        worker.balance_retry_at = 0
        self.assertTrue(worker._maybe_refresh_balance())
        self.assertEqual(worker.cache.available_balance, Decimal("220.00"))
        worker.balance_provider.get_available_balance.assert_called_once_with("phone_A")

        worker.clear_pending_balance_requirement()
        worker.cache.invalidate()
        self.assertFalse(worker._maybe_refresh_balance())
        self.assertEqual(worker.balance_provider.get_available_balance.call_count, 1)

    def test_active_payment_assignment_is_never_interrupted_for_balance(self):
        worker = self.balance_ready_worker()
        worker.balance_initial_attempted = True
        worker.request_balance_refresh("manual")
        worker.assigned = (withdrawal(), "reservation")
        worker.state = "PAYING"

        self.assertFalse(worker._maybe_refresh_balance())
        worker.balance_provider.get_available_balance.assert_not_called()
        self.assertEqual(worker.status()["balance_state"], "ERROR")
        self.assertIsNone(worker.balance_refresh_request_reason)

    def test_login_binds_exact_operator_and_refuses_parent_or_other_operator(self):
        profile = {
            "id": 42,
            "role": "operator",
            "token": "a",
            "realtime": {},
            "session_max_expires_at": future(),
        }
        worker = self.worker(client=DummyClient(profile))
        self.assertEqual(worker.login()["id"], 42)
        for bad in ({**profile, "id": 77}, {**profile, "role": "parent"}):
            client = WithdrawClient("https://withdraw.example")
            client.request = Mock(return_value=bad)
            with self.assertRaises(APIError):
                client.login("operator_A", "password", "JBSWY3DPEHPK3PXP", 42)
            self.assertIsNone(client.profile)

    def test_payment_readiness_requires_distinct_workflow_purposes(self):
        worker = self.worker()
        worker.assignments.get.return_value = ("combined",)
        worker.accounts.list.return_value = [
            SimpleNamespace(device_id="phone_A", enabled=True)
        ]
        combined = {
            "purpose": "payment",
            "steps": [
                {"type": "wait_for_text"},
                {"type": "capture_screen", "output_key": "receipt_path"},
                {
                    "type": "ocr_value",
                    "parser": "currency",
                    "output": "payer_balance",
                },
            ],
        }
        worker.workflows.read.return_value = combined
        worker.assignments.integration.return_value = {"balance_workflow_id": None}
        self.assertEqual(
            worker.payment_readiness(),
            (False, "Assign a Balance Check Workflow to this device."),
        )

        worker.assignments.integration.return_value = {
            "balance_workflow_id": "combined"
        }
        ready, error = worker.payment_readiness()
        self.assertFalse(ready)
        self.assertIn("Balance Check purpose", error)

    def test_rejected_stored_credentials_wait_for_deliberate_reconnect(self):
        client = DummyClient()
        client.login.side_effect = APIError(401, "rejected")
        worker = self.worker(client=client)
        worker.start()
        self.addCleanup(worker.close)

        self.assertTrue(wait_for(lambda: worker.state == "AUTH_FAILED"))
        self.assertEqual(client.login.call_count, 1)
        time.sleep(0.05)
        self.assertEqual(client.login.call_count, 1)

        worker.wake.set()
        self.assertTrue(wait_for(lambda: client.login.call_count == 2))

    def test_login_wraps_identity_or_credential_rejection_safely(self):
        client = DummyClient()
        client.login.side_effect = APIError(403, "different operator")
        worker = self.worker(client=client)
        with self.assertRaises(AuthenticationFailed):
            worker.login()

    def test_pending_preserves_order_and_uses_final_amount(self):
        pending = [
            withdrawal(ident=1, final="5000.00"),
            withdrawal(ident=2, final="1000.00"),
            withdrawal(ident=3, final="700.00"),
        ]
        self.assertIs(select_pending(pending, Decimal("3280.50")), pending[1])

    def test_documented_lock_normalization_uses_final_amount_and_bank_fields(self):
        raw = withdrawal()
        values = normalize_locked_withdrawal(raw)
        self.assertEqual(
            {
                key: values[key]
                for key in (
                    "account_number",
                    "account_name",
                    "bank_name",
                )
            },
            {
                "account_number": "6222000000000000",
                "account_name": "Account Name",
                "bank_name": "Bank Name",
            },
        )
        self.assertEqual(values["amount"], "4950.00")
        self.assertEqual(values["final_amount"], "4950.00")
        self.assertNotEqual(values["amount"], raw["withdraw_amount"])
        self.assertEqual(values["withdraw_id"], "935402")
        self.assertNotIn("holder_name", values)
        self.assertNotIn("mobile_no", values)

    def test_withdraw_amount_variables_and_generic_variables_still_resolve(self):
        values = normalize_locked_withdrawal(withdrawal())
        payment = {
            "variables": [
                {
                    "name": "account_number",
                    "label": "Account Number",
                    "type": "text",
                    "required": True,
                    "default": None,
                },
                {
                    "name": "amount",
                    "label": "Amount",
                    "type": "text",
                    "required": True,
                    "default": None,
                },
            ],
            "steps": [],
        }
        self.assertEqual(
            resolve_variables(
                payment,
                {
                    "account_number": values["account_number"],
                    "amount": values["amount"],
                },
            )["amount"],
            "4950.00",
        )
        generic = {
            "variables": [
                {
                    "name": "customer_name",
                    "label": "Customer name",
                    "type": "text",
                    "required": True,
                    "default": None,
                }
            ],
            "steps": [],
        }
        self.assertEqual(
            resolve_variables(generic, {"customer_name": "Alicia"}),
            {"customer_name": "Alicia"},
        )

    def test_lock_mismatch_or_expiry_never_starts_a_payment(self):
        item = withdrawal(ident=1, tx_id="ONE", final="10.00")
        for locked in (
            withdrawal(ident=2, tx_id="ONE", final="10.00"),
            withdrawal(ident=1, tx_id="TWO", final="10.00"),
            withdrawal(ident=1, tx_id="ONE", final="11.00"),
            withdrawal(ident=1, tx_id="ONE", final="10.00", expires=future(1)),
        ):
            worker = self.worker()
            worker.action = Mock(return_value={"withdrawal": locked})
            with self.assertRaises(ValueError):
                worker.lock_withdrawal(item)
            self.assertIsNone(worker.allowed_job)
            worker.stop_renewal()

    def test_renewal_is_per_worker_and_stops_after_release_or_cancel(self):
        worker = self.worker(renew_seconds=0.01)
        raw = withdrawal(ident=1, tx_id="ONE", final="10.00")
        raw2 = withdrawal(ident=2, tx_id="TWO", final="10.00")
        renewed = future(900)
        calls = []

        def action(name, **fields):
            calls.append((name, fields))
            if name == "lock":
                return {"withdrawal": raw if fields["withdraw_id"] == 1 else raw2}
            if name == "renew-lock":
                return {"lock_expires_at": renewed}
            return {}

        worker.action = action
        worker.lock_withdrawal(raw)
        time.sleep(0.04)
        self.assertTrue(any(name == "renew-lock" for name, _ in calls))
        self.assertEqual(self.journal.current("phone_A")["lock_expires_at"], renewed)
        worker.release_withdrawal()
        count = len(calls)
        time.sleep(0.03)
        self.assertEqual(len(calls), count)
        raw2 = worker.lock_withdrawal(withdrawal(ident=2, tx_id="TWO", final="10.00"))
        worker.assigned = (raw2, "cancel-token")
        self.scheduler.reservations["phone_A"] = "cancel-token"
        worker.cancel_withdrawal("Bank rejected the transfer.")
        self.assertEqual(self.journal.current("phone_A")["phase"], "CANCELLED")
        count = len(calls)
        time.sleep(0.03)
        self.assertEqual(len(calls), count)

    def test_selected_phone_owns_lock_renewal_and_release_session_actions(self):
        worker = self.worker("phone_B", renew_seconds=0.01)
        raw = withdrawal(ident=2, tx_id="PHONE-B", final="10.00")
        calls = []

        def action(name, **fields):
            calls.append((name, fields))
            if name == "lock":
                return {"withdrawal": raw}
            if name == "renew-lock":
                return {"lock_expires_at": future(900)}
            return {}

        worker.action = action
        worker.lock_withdrawal(raw)
        time.sleep(0.03)
        worker.release_withdrawal()
        self.assertEqual(calls[0], ("lock", {"withdraw_id": 2}))
        self.assertTrue(any(name == "renew-lock" for name, _ in calls))
        self.assertIn(("release", {"withdraw_id": 2}), calls)
        self.assertEqual(
            self.journal.current("phone_B")["assigned_device_id"], "phone_B"
        )
        self.journal.begin(
            "phone_B",
            phase="LOCKED",
            withdraw_id=3,
            tx_id="PHONE-B-CANCEL",
            assigned_device_id="phone_B",
        )
        worker.assigned = (
            withdrawal(ident=3, tx_id="PHONE-B-CANCEL", final="10.00"),
            "cancel-token",
        )
        worker.cancel_withdrawal("External bank rejected the transfer.")
        self.assertEqual(calls[-1][0], "cancel")

    def test_manual_cancel_confirms_before_terminal_and_clears_ownership(self):
        worker = self.worker()
        raw = withdrawal(ident=935402, tx_id="TX9F3K2", final="10.00")
        self.journal.begin(
            "phone_A",
            phase="LOCKED",
            withdraw_id=935402,
            tx_id="TX9F3K2",
            assigned_device_id="phone_A",
        )
        worker.assigned = (raw, "dispatch-token")
        self.scheduler.reservations["phone_A"] = "dispatch-token"
        worker.action = Mock(return_value={"ok": True})

        worker.cancel_withdrawal("invalid account number")

        worker.action.assert_called_once_with(
            "cancel",
            withdraw_id=935402,
            reason="invalid account number",
        )
        self.assertEqual(self.journal.current("phone_A")["phase"], "CANCELLED")
        self.assertIsNone(worker.assigned)
        self.assertNotIn("phone_A", self.scheduler.reservations)
        self.assertEqual(worker.state, "IDLE")

    def test_ambiguous_manual_cancel_stays_owned_and_needs_attention(self):
        worker = self.worker()
        raw = withdrawal(ident=935402, tx_id="TX9F3K2", final="10.00")
        failed = SimpleNamespace(
            job_id="job-failed",
            action="withdraw_v2",
            device_id="phone_A",
            external_ref=raw["tx_id"],
            workflow="payment-workflow",
            account_id="account-A",
            retry_of_job_id=None,
            status="FAILED",
        )
        self.scheduler.get_job = Mock(return_value=failed)
        self.journal.begin(
            "phone_A",
            phase="NEEDS_ATTENTION",
            withdraw_id=935402,
            tx_id="TX9F3K2",
            assigned_device_id="phone_A",
            operator_id=42,
            account_id=failed.account_id,
            workflow_id=failed.workflow,
            job_id=failed.job_id,
            raw_withdrawal=raw,
        )
        worker.assigned = (raw, "dispatch-token")
        self.scheduler.reservations["phone_A"] = "dispatch-token"
        worker.action = Mock(side_effect=APIError(409, "external lock was lost"))

        with self.assertRaisesRegex(
            RecordConflict,
            "APIError code=409: external lock was lost",
        ):
            worker.cancel_withdrawal(
                "bank rejected payment",
            )

        current = self.journal.current("phone_A")
        self.assertEqual(current["phase"], "NEEDS_ATTENTION")
        self.assertEqual(current["withdraw_id"], 935402)
        self.assertEqual(current["tx_id"], "TX9F3K2")
        self.assertEqual(current["assigned_device_id"], "phone_A")
        self.assertEqual(current["ambiguity_error_code"], 409)
        self.assertEqual(
            current["ambiguity_error_reason"], "external lock was lost"
        )
        self.assertEqual(worker.assigned, (raw, "dispatch-token"))
        self.assertEqual(self.scheduler.reservations["phone_A"], "dispatch-token")
        worker.action.assert_called_once()

    def test_failed_job_can_be_cancelled_without_backend_attestation(self):
        worker = self.worker()
        failed = SimpleNamespace(
            job_id="job-failed",
            action="withdraw_v2",
            device_id="phone_A",
            external_ref="TX9F3K2",
            workflow="payment-workflow",
            account_id="account-A",
            retry_of_job_id=None,
            status="FAILED",
            failed_step_index=2,
        )
        self.scheduler.get_job = Mock(return_value=failed)
        self.journal.begin(
            "phone_A",
            phase="NEEDS_ATTENTION",
            withdraw_id=935402,
            tx_id="TX9F3K2",
            assigned_device_id="phone_A",
            job_id=failed.job_id,
            workflow_id=failed.workflow,
            account_id=failed.account_id,
            operator_id=42,
            raw_withdrawal=withdrawal(final="10.00"),
        )
        worker.assigned = (withdrawal(final="10.00"), "dispatch-token")
        self.scheduler.reservations["phone_A"] = "dispatch-token"
        worker.action = Mock(return_value={"ok": True})

        self.assertTrue(worker.can_cancel_withdrawal())
        worker.cancel_withdrawal("invalid account number")

        worker.action.assert_called_once_with(
            "cancel", withdraw_id=935402, reason="invalid account number"
        )
        self.assertEqual(self.journal.current("phone_A")["phase"], "CANCELLED")
        self.assertIsNone(worker.assigned)
        self.assertNotIn("phone_A", self.scheduler.reservations)
        self.assertEqual(worker.state, "IDLE")

    def test_failed_or_interrupted_retry_wait_hands_operation_to_cancel(self):
        for status in ("FAILED", "INTERRUPTED"):
            with self.subTest(status=status):
                worker, _job, _raw, thread = self.waiting_cancel_worker(status)

                worker.cancel_withdrawal("operator unable to complete payment")
                thread.join(1)

                self.assertFalse(thread.is_alive())
                worker.action.assert_called_once_with(
                    "cancel",
                    withdraw_id=935402,
                    reason="operator unable to complete payment",
                )
                self.assertEqual(
                    self.journal.current("phone_A")["phase"], "CANCELLED"
                )
                self.assertIsNone(worker.assigned)
                self.assertFalse(worker.manual_cancel_requested.is_set())
                worker.stop_renewal.assert_called_once_with()

    def test_failed_retry_wait_cancel_failure_keeps_renewal_and_attention(self):
        worker, _job, raw, thread = self.waiting_cancel_worker(
            action=Mock(side_effect=APIError(0, "outcome uncertain"))
        )

        with self.assertRaisesRegex(RecordConflict, "outcome uncertain"):
            worker.cancel_withdrawal("operator unable to complete payment")
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(
            self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION"
        )
        self.assertEqual(worker.assigned, (raw, "dispatch-token"))
        self.assertFalse(worker.manual_cancel_requested.is_set())
        worker.stop_renewal.assert_not_called()

        # The next worker pass must resume waiting on the same failed job. It
        # must not call _pay() again and create a duplicate payment job.
        errors = []
        resumed = threading.Thread(
            target=lambda: self._capture_error(
                errors, lambda: self._run_assigned_with_operation(worker)
            )
        )
        resumed.start()
        self.assertTrue(
            wait_for(lambda: worker.retry_waiting_job_id == "job-failed")
        )
        time.sleep(0.02)
        self.assertEqual(worker._pay.call_count, 1)
        worker.stop.set()
        worker.wake.set()
        resumed.join(1)
        self.assertFalse(resumed.is_alive())
        self.assertEqual(len(errors), 1)

    def test_running_operation_cannot_be_interrupted_for_manual_cancel(self):
        worker = self.worker()
        job = SimpleNamespace(
            job_id="job-running",
            action="withdraw_v2",
            device_id="phone_A",
            external_ref="TX9F3K2",
            workflow="payment-workflow",
            account_id="account-A",
            retry_of_job_id=None,
            status="RUNNING",
        )
        self.scheduler.get_job = Mock(return_value=job)
        self.journal.begin(
            "phone_A",
            phase="QUEUED",
            withdraw_id=935402,
            tx_id="TX9F3K2",
            assigned_device_id="phone_A",
            operator_id=42,
            account_id=job.account_id,
            workflow_id=job.workflow,
            job_id=job.job_id,
        )
        worker.action = Mock()
        worker.withdrawal_operation.acquire()
        try:
            with self.assertRaisesRegex(RecordConflict, "already in progress"):
                worker.cancel_withdrawal("operator unable to complete payment")
        finally:
            worker.withdrawal_operation.release()

        worker.action.assert_not_called()
        self.assertFalse(worker.manual_cancel_requested.is_set())

    def test_retry_and_manual_cancel_race_allows_exactly_one_action(self):
        continue_retry = threading.Event()
        worker, original, _raw, owner = self.waiting_cancel_worker(
            continue_retry=continue_retry
        )
        retry = SimpleNamespace(
            job_id="job-retry",
            action="withdraw_v2",
            device_id="phone_A",
            external_ref=original.external_ref,
            workflow=original.workflow,
            account_id=original.account_id,
            retry_of_job_id=original.job_id,
            status="QUEUED",
        )
        self.scheduler.get_job = Mock(
            side_effect=lambda job_id: retry if job_id == retry.job_id else original
        )
        start = threading.Barrier(3)
        outcomes = []

        def cancel():
            start.wait()
            try:
                worker.cancel_withdrawal("operator unable to complete payment")
                outcomes.append("cancel")
            except RecordConflict:
                outcomes.append("cancel-rejected")

        def authorize_retry():
            start.wait()
            try:
                worker.authorize_retry(original.job_id, retry.job_id)
                outcomes.append("retry")
            except RecordConflict:
                outcomes.append("retry-rejected")

        cancel_thread = threading.Thread(target=cancel)
        retry_thread = threading.Thread(target=authorize_retry)
        cancel_thread.start()
        retry_thread.start()
        start.wait()
        self.assertTrue(wait_for(lambda: len(outcomes) == 2))
        continue_retry.set()
        cancel_thread.join(1)
        retry_thread.join(1)
        owner.join(1)

        winners = [value for value in outcomes if "rejected" not in value]
        self.assertEqual(len(winners), 1, outcomes)
        self.assertIn(winners[0], {"cancel", "retry"})
        self.assertFalse(cancel_thread.is_alive())
        self.assertFalse(retry_thread.is_alive())
        self.assertFalse(owner.is_alive())

    def test_interrupted_job_can_be_cancelled_without_backend_attestation(self):
        worker = self.worker()
        interrupted = SimpleNamespace(
            job_id="job-interrupted",
            action="withdraw_v2",
            device_id="phone_A",
            external_ref="TX9F3K2",
            workflow="payment-workflow",
            account_id="account-A",
            retry_of_job_id=None,
            status="INTERRUPTED",
        )
        self.scheduler.get_job = Mock(return_value=interrupted)
        self.journal.begin(
            "phone_A",
            phase="NEEDS_ATTENTION",
            withdraw_id=935402,
            tx_id="TX9F3K2",
            assigned_device_id="phone_A",
            job_id=interrupted.job_id,
            workflow_id=interrupted.workflow,
            account_id=interrupted.account_id,
        )
        worker.action = Mock(return_value={"ok": True})

        worker.cancel_withdrawal("operator unable to complete payment")
        worker.action.assert_called_once()
        self.assertEqual(self.journal.current("phone_A")["phase"], "CANCELLED")

    def test_queued_and_waiting_jobs_cancel_without_backend_attestation(self):
        for status in ("QUEUED", "WAITING_FOR_DEVICE"):
            with self.subTest(status=status):
                worker = self.worker()
                job = SimpleNamespace(
                    job_id=f"job-{status.lower()}",
                    action="withdraw_v2",
                    device_id="phone_A",
                    external_ref="TX9F3K2",
                    workflow="payment-workflow",
                    account_id="account-A",
                    retry_of_job_id=(
                        "job-original" if status == "WAITING_FOR_DEVICE" else None
                    ),
                    status=status,
                )
                self.scheduler.get_job = Mock(return_value=job)
                self.executor.cancel = Mock(
                    return_value=SimpleNamespace(status="CANCELLED")
                )
                self.journal.update(
                    "phone_A",
                    phase="QUEUED",
                    withdraw_id=935402,
                    tx_id="TX9F3K2",
                    assigned_device_id="phone_A",
                    job_id=job.job_id,
                    workflow_id=job.workflow,
                    account_id=job.account_id,
                    retry_of_job_id=job.retry_of_job_id,
                )
                worker.action = Mock(return_value={"ok": True})

                self.assertTrue(worker.can_cancel_withdrawal())
                worker.cancel_withdrawal("invalid account number")

                self.executor.cancel.assert_called_once_with(job.job_id)
                worker.action.assert_called_once()

    def test_running_or_resolved_withdrawal_cannot_use_manual_override(self):
        worker = self.worker()
        job = SimpleNamespace(
            job_id="job-running",
            action="withdraw_v2",
            device_id="phone_A",
            external_ref="TX9F3K2",
            workflow="payment-workflow",
            account_id="account-A",
            retry_of_job_id=None,
            status="RUNNING",
        )
        self.scheduler.get_job = Mock(return_value=job)
        worker.action = Mock()
        for phase in (
            "LOCKED",
            "CONFIRMING",
            "CONFIRMED",
            "CANCELLED",
            "RELEASED",
            "LOCK_REJECTED",
        ):
            with self.subTest(phase=phase):
                self.journal.update(
                    "phone_A",
                    phase=phase,
                    withdraw_id=935402,
                    tx_id="TX9F3K2",
                    assigned_device_id="phone_A",
                    job_id=job.job_id,
                    workflow_id=job.workflow,
                    account_id=job.account_id,
                )
                self.assertFalse(worker.can_cancel_withdrawal())
                with self.assertRaises(RecordConflict):
                    worker.cancel_withdrawal(
                        "invalid account number",
                    )
        worker.action.assert_not_called()

    def test_failed_cancel_rejects_mismatched_job_identity(self):
        worker = self.worker()
        failed = SimpleNamespace(
            job_id="job-failed",
            action="withdraw_v2",
            device_id="phone_A",
            external_ref="DIFFERENT-TX",
            workflow="payment-workflow",
            account_id="account-A",
            retry_of_job_id=None,
            status="FAILED",
        )
        self.scheduler.get_job = Mock(return_value=failed)
        self.journal.begin(
            "phone_A",
            phase="NEEDS_ATTENTION",
            withdraw_id=935402,
            tx_id="TX9F3K2",
            assigned_device_id="phone_A",
            job_id=failed.job_id,
            workflow_id=failed.workflow,
            account_id=failed.account_id,
        )
        worker.action = Mock()

        self.assertFalse(worker.can_cancel_withdrawal())
        with self.assertRaises(RecordConflict):
            worker.cancel_withdrawal(
                "invalid account number",
            )
        worker.action.assert_not_called()

    def test_duplicate_manual_cancel_executes_external_action_once(self):
        worker = self.worker()
        self.journal.begin(
            "phone_A",
            phase="LOCKED",
            withdraw_id=935402,
            tx_id="TX9F3K2",
            assigned_device_id="phone_A",
        )
        worker.assigned = (
            withdrawal(ident=935402, tx_id="TX9F3K2", final="10.00"),
            "dispatch-token",
        )
        entered = threading.Event()
        finish = threading.Event()
        calls = []

        def action(name, **fields):
            calls.append((name, fields))
            entered.set()
            finish.wait(1)
            return {"ok": True}

        worker.action = action
        first = threading.Thread(
            target=worker.cancel_withdrawal,
            args=("operator unable to complete payment",),
        )
        first.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaisesRegex(RecordConflict, "already in progress"):
            worker.cancel_withdrawal("operator unable to complete payment")
        finish.set()
        first.join(1)
        self.assertFalse(first.is_alive())
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.journal.current("phone_A")["phase"], "CANCELLED")

    def test_manual_cancel_rejects_confirmed_or_unowned_withdrawal(self):
        worker = self.worker()
        self.scheduler.get_job = Mock(
            side_effect=RecordNotFound("payment-job")
        )
        for phase, owner, job_id in (
            ("CONFIRMED", "phone_A", None),
            ("LOCKED", "phone_B", None),
            ("LOCKED", "phone_A", "payment-job"),
        ):
            with self.subTest(phase=phase, owner=owner, job_id=job_id):
                self.journal.update(
                    "phone_A",
                    phase=phase,
                    withdraw_id=935402,
                    tx_id="TX9F3K2",
                    assigned_device_id=owner,
                    job_id=job_id,
                )
                worker.action = Mock()
                with self.assertRaises(RecordConflict):
                    worker.cancel_withdrawal("invalid account number")
                worker.action.assert_not_called()

    def test_restart_reconciles_my_locked_before_new_work_and_gates_unresolved(self):
        worker = self.worker()
        calls = []
        worker.get_my_locked = lambda: calls.append("my-locked") or None
        self.assertTrue(worker.reconcile())
        self.assertEqual(calls, ["my-locked"])
        self.journal.begin("phone_A", phase="CONFIRMING", withdraw_id=1, tx_id="ONE")
        worker.client.reconcile_confirmation.return_value = False
        self.assertFalse(worker.reconcile())
        self.assertEqual(self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION")

    def test_each_session_reconciles_before_pending_after_reconnect(self):
        for device in ("phone_A", "phone_B"):
            worker = self.worker(device, client=DummyClient({"token": device}))
            worker.session_max = time.time() + 600
            worker.socket = DummySocket()
            calls = []
            worker.reconcile = lambda: calls.append("my-locked") or True
            worker.cycle = lambda: calls.append("pending")

            self.assertTrue(worker.ensure_realtime())
            worker.cycle()
            worker.socket.generation = 2
            self.assertTrue(worker.ensure_realtime())
            worker.cycle()
            self.assertEqual(calls, ["my-locked", "pending", "my-locked", "pending"])

    def test_same_operator_adopts_authenticated_session_then_reconciles(self):
        previous = DummyClient({"token": "expired-session"})
        worker = self.worker(client=previous)
        worker.socket = DummySocket()
        replacement = DummyClient(
            {
                "token": "fresh-session",
                "session_max_expires_at": future(),
                "realtime": {"channels": {}},
            }
        )
        config = {
            **self.config,
            "withdraw_api": {
                **self.config["withdraw_api"],
                "credential_reference": "env:WITHDRAW_A_UPDATED",
            },
        }
        worker.reconcile = Mock(return_value=True)

        def connect():
            worker.socket = DummySocket()
            worker.reconciled_generation = 0
            worker.reconciliation_complete = False

        worker.connect_socket = connect

        worker.adopt_authenticated_client(replacement, config)

        self.assertIs(worker.client, previous)
        self.assertEqual(worker.state, "CONNECTING")
        self.assertTrue(worker.ensure_realtime())
        self.assertIs(worker.client, replacement)
        self.assertEqual(worker.api["operator_id"], 42)
        self.assertTrue(worker.session_available())
        worker.reconcile.assert_called_once_with()
        previous.close.assert_called_once_with()
        replacement.login.assert_not_called()

    def test_local_binding_failure_does_not_break_or_retry_healthy_session(self):
        error = (
            "Local payment routing could not be initialized: account binding "
            "conflicts with another device."
        )
        provision = Mock(return_value=error)
        client = DummyClient({"token": "session-token"})
        worker = self.worker(
            client=client,
            account_binding_provisioner=provision,
        )
        worker.session_max = time.time() + 600
        worker.socket = DummySocket()
        worker.reconcile = Mock(return_value=True)
        worker.assignments.get.return_value = ("payment",)
        worker.accounts.list.return_value = []

        self.assertTrue(worker.ensure_realtime())
        self.assertEqual(worker.state, "IDLE")
        self.assertTrue(worker.socket.connected)
        client.login.assert_not_called()
        self.assertEqual(worker.payment_readiness(), (False, error))

        self.assertTrue(worker.ensure_realtime())
        provision.assert_called_once_with()
        client.login.assert_not_called()

    def test_completed_job_confirms_its_own_receipt_then_updates_journal_and_balance(
        self,
    ):
        receipt = self.root / "receipt.png"
        receipt.write_bytes(b"png")
        job = SimpleNamespace(
            job_id="job-A",
            status="COMPLETED",
            outputs={"receipt_path": str(receipt)},
            screenshots=[{"relative_path": "receipt.png"}],
        )
        self.scheduler.enqueue = Mock(return_value=job)
        self.scheduler.get_job = Mock(return_value=job)
        client = DummyClient()
        worker = self.worker(client=client)
        worker.socket = DummySocket()
        raw = withdrawal(ident=1, tx_id="ONE", final="10.00")
        self.journal.begin(
            "phone_A",
            phase="LOCKED",
            withdraw_id=1,
            tx_id="ONE",
            lock_expires_at=raw["lock_expires_at"],
        )
        worker.cache.observe(Decimal("100.00"))
        self.scheduler.reservations["phone_A"] = "dispatch-token"
        account = SimpleNamespace(account_id="account-A", device_id="phone_A")
        workflow = {
            "id": "pay-A",
            "variables": [],
            "steps": [
                {"type": "wait_for_text"},
                {"type": "capture_screen", "output_key": "receipt_path"},
            ],
        }
        worker._pay(
            raw,
            account,
            workflow,
            dispatch_reservation="dispatch-token",
        )
        self.scheduler.enqueue.assert_called_once()
        protected = self.scheduler.enqueue.call_args.kwargs["protected_metadata"]
        self.assertEqual(
            protected,
            {
                "version": 3,
                "withdraw_id": raw["id"],
                "tx_id": raw["tx_id"],
                "assigned_device_id": "phone_A",
                "operator_id": 42,
                "account_id": "account-A",
                "workflow_id": "pay-A",
                "final_amount": "10.00",
                "transfer_method": "bank_account",
                "transfer_method_profile": "account_number",
                "transfer_method_fingerprint": transfer_method_fingerprint(
                    "bank_account"
                ),
                "raw_withdrawal": raw,
                "lock_expires_at": raw["lock_expires_at"],
            },
        )
        worker.executor.wake.assert_called()
        self.assertNotIn("phone_A", self.scheduler.reservations)
        client.confirm_with_receipt.assert_called_once_with(1, receipt.resolve())
        self.assertEqual(self.journal.current("phone_A")["phase"], "CONFIRMED")
        self.assertEqual(worker.cache.available_balance, Decimal("90.00"))

    def test_missing_or_outside_receipt_cannot_confirm_or_replay(self):
        worker = self.worker()
        worker.socket = DummySocket()
        raw = withdrawal(ident=1, tx_id="ONE", final="10.00")
        self.journal.begin(
            "phone_A",
            phase="LOCKED",
            withdraw_id=1,
            tx_id="ONE",
            lock_expires_at=raw["lock_expires_at"],
        )
        job = SimpleNamespace(
            job_id="job-A", status="COMPLETED", outputs={}, screenshots=[]
        )
        self.scheduler.enqueue = Mock(return_value=job)
        self.scheduler.get_job = Mock(return_value=job)
        workflow = {
            "id": "pay-A",
            "variables": [],
            "steps": [
                {"type": "wait_for_text"},
                {"type": "capture_screen", "output_key": "receipt_path"},
            ],
        }
        with self.assertRaises(ValueError):
            worker._pay(
                raw, SimpleNamespace(account_id="a", device_id="phone_A"), workflow
            )
        worker.client.confirm_with_receipt.assert_not_called()

    def test_two_workers_have_isolated_journals_caches_and_sockets(self):
        a, b = self.worker("phone_A"), self.worker("phone_B")
        a.socket, b.socket = DummySocket(), DummySocket()
        a.cache.observe(Decimal("6000.00"))
        b.cache.observe(Decimal("3000.00"))
        self.journal.begin("phone_A", phase="LOCKED", withdraw_id=1, tx_id="A")
        self.journal.begin("phone_B", phase="LOCKED", withdraw_id=2, tx_id="B")
        self.assertNotEqual(a.socket, b.socket)
        self.assertEqual(a.cache.available_balance, Decimal("6000.00"))
        self.assertEqual(b.cache.available_balance, Decimal("3000.00"))
        self.assertEqual(self.journal.current("phone_A")["tx_id"], "A")
        self.assertEqual(self.journal.current("phone_B")["tx_id"], "B")

    def test_fresh_session_adopts_persisted_retry_for_the_same_withdrawal(self):
        worker, raw, retry = self.persisted_retry_worker(client=DummyClient())
        worker.socket = DummySocket()
        self.install_live_renewal_stub(worker)

        self.assertTrue(
            worker._adopt_pending_retry(self.journal.current("phone_A"), raw)
        )
        self.assertEqual(worker.allowed_job, retry.job_id)
        self.assertEqual(worker.retry_job_id, retry.job_id)
        self.assertEqual(worker.assigned, (raw, None))
        worker._start_renewal.assert_called_once_with()

    def test_reconcile_resolves_exact_approved_before_retry_adoption(self):
        client = DummyClient()
        calls = []
        client.reconcile_confirmation.side_effect = (
            lambda tx_id, withdraw_id: calls.append(("approved", tx_id, withdraw_id))
            or True
        )
        worker, raw, retry = self.persisted_retry_worker(client=client)
        worker.get_my_locked = Mock(
            side_effect=AssertionError("my-locked must not precede exact approval")
        )
        worker._adopt_pending_retry = Mock()
        worker._execute_assigned = Mock()
        worker.action = Mock()
        self.scheduler.enqueue = Mock()
        worker.allowed_job = retry.job_id
        worker.retry_job_id = retry.job_id

        def cancel(job_id):
            calls.append(("cancel", job_id))
            retry.status = "CANCELLED"
            return retry

        self.executor.cancel.side_effect = cancel

        self.assertTrue(worker.reconcile())

        self.assertEqual(
            calls,
            [
                ("approved", raw["tx_id"], raw["id"]),
                ("cancel", retry.job_id),
            ],
        )
        current = self.journal.current("phone_A")
        self.assertEqual(current["phase"], "CONFIRMED")
        self.assertEqual(current["withdraw_id"], raw["id"])
        self.assertEqual(current["tx_id"], raw["tx_id"])
        self.assertEqual(current["assigned_device_id"], "phone_A")
        self.assertEqual(current["job_id"], retry.job_id)
        self.assertEqual(retry.status, "CANCELLED")
        self.assertIsNone(worker.allowed_job)
        self.assertIsNone(worker.retry_job_id)
        worker._adopt_pending_retry.assert_not_called()
        worker._execute_assigned.assert_not_called()
        worker.action.assert_not_called()
        worker.get_my_locked.assert_not_called()
        self.scheduler.enqueue.assert_not_called()
        self.executor.wake.assert_not_called()

    def test_reconcile_no_approval_adopts_retry_with_valid_existing_lock(self):
        client = DummyClient()
        calls = []
        client.reconcile_confirmation.side_effect = (
            lambda _tx_id, _withdraw_id: calls.append("approved") or False
        )
        worker, raw, retry = self.persisted_retry_worker(client=client)
        worker.get_my_locked = Mock(
            side_effect=lambda: calls.append("my-locked") or raw
        )
        self.install_live_renewal_stub(worker)

        self.assertTrue(worker.reconcile())

        self.assertEqual(calls, ["approved", "my-locked"])
        self.assertEqual(worker.allowed_job, retry.job_id)
        self.assertEqual(worker.retry_job_id, retry.job_id)
        self.assertEqual(worker.assigned, (raw, None))
        self.assertEqual(self.journal.current("phone_A")["phase"], "QUEUED")
        worker._start_renewal.assert_called_once_with()
        self.executor.cancel.assert_not_called()

    def test_reconcile_no_approval_and_missing_lock_remains_unresolved(self):
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, raw, retry = self.persisted_retry_worker(client=client)
        worker.get_my_locked = Mock(return_value=None)
        worker.request_pending = Mock(return_value=[])
        worker.action = Mock()

        self.assertFalse(worker.reconcile())

        client.reconcile_confirmation.assert_called_once_with(raw["tx_id"], raw["id"])
        worker.request_pending.assert_called_once_with()
        worker.action.assert_not_called()
        self.assertEqual(self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION")
        self.assertIsNone(worker.allowed_job)
        self.assertNotEqual(worker.retry_job_id, retry.job_id)
        self.executor.cancel.assert_not_called()

    def test_reconcile_no_approval_and_expired_lock_remains_unresolved(self):
        raw = withdrawal(expires=future(-60))
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, raw, _retry = self.persisted_retry_worker(client=client, raw=raw)
        worker.get_my_locked = Mock(return_value=raw)
        self.install_live_renewal_stub(worker)

        self.assertFalse(worker.reconcile())

        client.reconcile_confirmation.assert_called_once_with(raw["tx_id"], raw["id"])
        self.assertEqual(self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION")
        worker._start_renewal.assert_not_called()
        self.executor.cancel.assert_not_called()

    def test_reconcile_mismatched_approved_tx_does_not_resolve_retry(self):
        raw = withdrawal()
        client = WithdrawClient("https://withdraw.example")
        client.request = Mock(
            return_value={
                "withdrawals": [{"id": raw["id"], "tx_id": "DIFFERENT", "status": 1}],
                "pagination": {"last_page": 1},
            }
        )
        worker, raw, _retry = self.persisted_retry_worker(client=client, raw=raw)
        worker.get_my_locked = Mock(return_value=None)
        worker.request_pending = Mock(return_value=[])

        self.assertFalse(worker.reconcile())

        client.request.assert_called_once_with(
            "GET",
            "/api/admin-withdraw/v2/withdrawals/my-approved",
            params={"tx_id": raw["tx_id"], "page": 1},
        )
        self.assertEqual(self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION")
        self.executor.cancel.assert_not_called()

    def test_expired_lock_reacquires_exact_pending_withdrawal_for_same_retry(self):
        expired = withdrawal(expires=future(-60))
        locked = {**expired, "lock_expires_at": future(600)}
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, raw, retry = self.persisted_retry_worker(client=client, raw=expired)
        worker.get_my_locked = Mock(return_value=None)
        worker.request_pending = Mock(return_value=[raw])
        worker.action = Mock(return_value={"withdrawal": locked})
        self.install_live_renewal_stub(worker)
        self.scheduler.enqueue = Mock()
        original_start = retry.start_step_index

        self.assertTrue(worker.reconcile())

        worker.action.assert_called_once_with("lock", withdraw_id=raw["id"])
        self.assertEqual(worker.allowed_job, retry.job_id)
        self.assertEqual(worker.retry_job_id, retry.job_id)
        self.assertEqual(worker.assigned, (locked, None))
        self.assertEqual(retry.start_step_index, original_start)
        self.assertEqual(retry.retry_of_job_id, "job-original")
        worker._start_renewal.assert_called_once_with()
        self.scheduler.enqueue.assert_not_called()
        self.assertEqual(self.journal.current("phone_A")["phase"], "QUEUED")

    def test_expired_lock_does_not_relock_same_amount_with_different_identity(self):
        expired = withdrawal(expires=future(-60))
        different = withdrawal(
            ident=expired["id"] + 1,
            tx_id="OTHER-TX",
            final=expired["final_amount"],
            expires=future(600),
        )
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, _raw, _retry = self.persisted_retry_worker(client=client, raw=expired)
        worker.get_my_locked = Mock(return_value=None)
        worker.request_pending = Mock(return_value=[different])
        worker.action = Mock()

        self.assertFalse(worker.reconcile())

        worker.action.assert_not_called()
        self.assertEqual(self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION")

    def test_expired_lock_rejects_changed_payment_record_before_relock(self):
        expired = withdrawal(expires=future(-60))
        expired["payment_record"] = 5
        pending = {**expired, "payment_record": 6, "lock_expires_at": None}
        client = DummyClient({"payment_records": [5]})
        client.reconcile_confirmation.return_value = False
        worker, _raw, _retry = self.persisted_retry_worker(client=client, raw=expired)
        worker.get_my_locked = Mock(return_value=None)
        worker.request_pending = Mock(return_value=[pending])
        worker.action = Mock()

        self.assertFalse(worker.reconcile())

        worker.action.assert_not_called()
        self.assertEqual(self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION")

    def test_expired_lock_definite_conflict_attempts_client_lock_only_once(self):
        expired = withdrawal(expires=future(-60))
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, raw, _retry = self.persisted_retry_worker(client=client, raw=expired)
        worker.get_my_locked = Mock(return_value=None)
        worker.request_pending = Mock(return_value=[raw])
        worker.action = Mock(side_effect=APIError(423, "locked elsewhere"))

        self.assertFalse(worker.reconcile())

        worker.action.assert_called_once_with("lock", withdraw_id=raw["id"])
        self.assertEqual(self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION")

    def test_uncertain_relock_after_apply_adopts_exact_my_locked_without_second_lock(
        self,
    ):
        expired = withdrawal(expires=future(-60))
        recovered = {**expired, "lock_expires_at": future(600)}
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, raw, retry = self.persisted_retry_worker(client=client, raw=expired)
        worker.get_my_locked = Mock(side_effect=[None, recovered])
        worker.request_pending = Mock(return_value=[raw])
        worker.action = Mock(side_effect=UncertainResult())
        self.install_live_renewal_stub(worker)

        self.assertTrue(worker.reconcile())

        worker.action.assert_called_once_with("lock", withdraw_id=raw["id"])
        self.assertEqual(worker.get_my_locked.call_count, 2)
        self.assertEqual(worker.allowed_job, retry.job_id)
        self.assertEqual(worker.assigned, (recovered, None))
        worker._start_renewal.assert_called_once_with()

    def test_uncertain_relock_never_blindly_retries_when_ownership_is_unproven(self):
        expired = withdrawal(expires=future(-60))
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, raw, _retry = self.persisted_retry_worker(client=client, raw=expired)
        worker.get_my_locked = Mock(side_effect=[None, None])
        worker.request_pending = Mock(side_effect=[[raw], [raw]])
        worker.action = Mock(side_effect=TimeoutError("uncertain"))

        self.assertFalse(worker.reconcile())

        worker.action.assert_called_once_with("lock", withdraw_id=raw["id"])
        self.assertEqual(worker.request_pending.call_count, 2)
        self.assertIsNone(worker.allowed_job)
        self.assertEqual(self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION")

    def test_relock_response_identity_mismatch_keeps_retry_blocked(self):
        expired = withdrawal(expires=future(-60))
        wrong = withdrawal(
            ident=expired["id"],
            tx_id="WRONG-TX",
            final=expired["final_amount"],
            expires=future(600),
        )
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, raw, _retry = self.persisted_retry_worker(client=client, raw=expired)
        worker.get_my_locked = Mock(return_value=None)
        worker.request_pending = Mock(return_value=[raw])
        worker.action = Mock(return_value={"withdrawal": wrong})
        worker._start_renewal = Mock()

        self.assertFalse(worker.reconcile())

        worker.action.assert_called_once_with("lock", withdraw_id=raw["id"])
        worker._start_renewal.assert_not_called()
        self.assertIsNone(worker.allowed_job)
        self.assertEqual(self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION")

    def test_relock_response_amount_mismatch_keeps_retry_blocked(self):
        expired = withdrawal(expires=future(-60))
        wrong = {**expired, "final_amount": "1.00", "lock_expires_at": future(600)}
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, raw, _retry = self.persisted_retry_worker(client=client, raw=expired)
        worker.get_my_locked = Mock(return_value=None)
        worker.request_pending = Mock(return_value=[raw])
        worker.action = Mock(return_value={"withdrawal": wrong})

        self.assertFalse(worker.reconcile())

        worker.action.assert_called_once_with("lock", withdraw_id=raw["id"])
        self.assertIsNone(worker.allowed_job)
        self.executor.wake.assert_not_called()

    def test_relock_response_payment_record_mismatch_keeps_retry_blocked(self):
        expired = {
            **withdrawal(expires=future(-60)),
            "payment_record": 5,
        }
        wrong = {**expired, "payment_record": 6, "lock_expires_at": future(600)}
        client = DummyClient({"payment_records": [5]})
        client.reconcile_confirmation.return_value = False
        worker, raw, _retry = self.persisted_retry_worker(client=client, raw=expired)
        worker.get_my_locked = Mock(return_value=None)
        worker.request_pending = Mock(return_value=[raw])
        worker.action = Mock(return_value={"withdrawal": wrong})

        self.assertFalse(worker.reconcile())

        worker.action.assert_called_once_with("lock", withdraw_id=raw["id"])
        self.assertIsNone(worker.allowed_job)
        self.executor.wake.assert_not_called()

    def test_successful_relock_starts_real_renewal_for_same_retry(self):
        expired = withdrawal(expires=future(-60))
        locked = {**expired, "lock_expires_at": future(600)}
        renewed = future(900)
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, raw, retry = self.persisted_retry_worker(client=client, raw=expired)
        worker.socket = DummySocket()
        worker.get_my_locked = Mock(return_value=None)
        worker.request_pending = Mock(return_value=[raw])
        calls = []

        def action(name, **fields):
            calls.append((name, fields))
            if name == "lock":
                return {"withdrawal": locked}
            if name == "renew-lock":
                return {"lock_expires_at": renewed}
            raise AssertionError(name)

        worker.action = action
        self.addCleanup(worker.stop_renewal)

        self.assertTrue(worker.reconcile())
        self.assertTrue(
            wait_for(lambda: any(name == "renew-lock" for name, _fields in calls))
        )

        self.assertEqual(calls[0], ("lock", {"withdraw_id": raw["id"]}))
        self.assertEqual(worker.allowed_job, retry.job_id)
        self.assertEqual(worker.retry_job_id, retry.job_id)
        self.assertEqual(retry.start_step_index, 36)

    def test_restart_retry_never_uses_expired_lock_reacquisition(self):
        expired = withdrawal(expires=future(-60))
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, raw, retry = self.persisted_retry_worker(
            client=client, raw=expired, start_step_index=0
        )
        # A real restart may have a parent that failed later, which proves it is
        # not the failed-step continuation even though the restart index is zero.
        parent = self.scheduler.get_job(retry.retry_of_job_id)
        parent.failed_step_index = 33
        worker.get_my_locked = Mock(return_value=None)
        worker.request_pending = Mock(return_value=[raw])
        worker.action = Mock()

        self.assertFalse(worker.reconcile())

        worker.action.assert_not_called()
        self.assertIsNone(worker.allowed_job)
        self.assertEqual(self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION")

    def test_restart_retry_cannot_adopt_an_existing_live_lock(self):
        live = withdrawal(expires=future(600))
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, _raw, retry = self.persisted_retry_worker(
            client=client, raw=live, start_step_index=0
        )
        parent = self.scheduler.get_job(retry.retry_of_job_id)
        parent.failed_step_index = 33
        worker.get_my_locked = Mock(return_value=live)
        worker._start_renewal = Mock()

        self.assertFalse(worker.reconcile())

        self.assertIsNone(worker.allowed_job)
        self.assertIsNone(worker.retry_job_id)
        self.assertIsNone(worker.assigned)
        worker._start_renewal.assert_not_called()
        self.executor.wake.assert_not_called()

    def test_incomplete_legacy_retry_identity_does_not_reacquire_lock(self):
        expired = withdrawal(expires=future(-60))
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, raw, _retry = self.persisted_retry_worker(
            client=client,
            raw=expired,
            journal_overrides={"workflow_id": None, "raw_withdrawal": None},
        )
        worker.get_my_locked = Mock(return_value=None)
        worker.request_pending = Mock(return_value=[raw])
        worker.action = Mock()

        self.assertFalse(worker.reconcile())

        worker.action.assert_not_called()
        self.assertEqual(self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION")

    def test_recovered_lock_journal_failure_never_authorizes_retry(self):
        worker, raw, retry = self.persisted_retry_worker(client=DummyClient())
        worker._start_renewal = Mock()
        real_update = self.journal.update

        def fail_recovery_write(device_id, **changes):
            if (
                changes.get("phase") == "QUEUED"
                and changes.get("job_id") == retry.job_id
            ):
                raise OSError("disk write failed")
            return real_update(device_id, **changes)

        self.journal.update = Mock(side_effect=fail_recovery_write)

        self.assertFalse(
            worker._adopt_pending_retry(self.journal.current("phone_A"), raw)
        )

        self.assertIsNone(worker.allowed_job)
        self.assertIsNone(worker.retry_job_id)
        self.assertIsNone(worker.assigned)
        worker._start_renewal.assert_not_called()
        self.executor.wake.assert_not_called()

    def test_recovered_lock_renewal_start_failure_fails_closed(self):
        worker, raw, _retry = self.persisted_retry_worker(client=DummyClient())
        worker._start_renewal = Mock(side_effect=RuntimeError("thread failed"))
        worker.stop_renewal = Mock()

        self.assertFalse(
            worker._adopt_pending_retry(self.journal.current("phone_A"), raw)
        )

        self.assertIsNone(worker.allowed_job)
        self.assertIsNone(worker.retry_job_id)
        self.assertIsNone(worker.assigned)
        self.assertEqual(self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION")
        self.executor.wake.assert_not_called()
        worker.stop_renewal.assert_called_once_with()

    def test_recovered_lock_renewal_must_install_a_live_thread(self):
        worker, raw, _retry = self.persisted_retry_worker(client=DummyClient())
        worker._start_renewal = Mock(return_value=None)

        self.assertFalse(
            worker._adopt_pending_retry(self.journal.current("phone_A"), raw)
        )

        self.assertIsNone(worker.allowed_job)
        self.assertIsNone(worker.retry_job_id)
        self.assertIsNone(worker.assigned)
        self.assertEqual(self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION")
        self.executor.wake.assert_not_called()

    def test_recovered_lock_renewal_dead_thread_fails_closed(self):
        worker, raw, _retry = self.persisted_retry_worker(client=DummyClient())
        dead_thread = Mock()
        dead_thread.is_alive.return_value = False

        def start_dead_thread():
            worker.renew_thread = dead_thread

        worker._start_renewal = Mock(side_effect=start_dead_thread)

        self.assertFalse(
            worker._adopt_pending_retry(self.journal.current("phone_A"), raw)
        )

        self.assertIsNone(worker.allowed_job)
        self.assertIsNone(worker.retry_job_id)
        self.assertIsNone(worker.assigned)
        self.assertEqual(self.journal.current("phone_A")["phase"], "NEEDS_ATTENTION")
        self.executor.wake.assert_not_called()

    def test_uncertain_relock_is_not_repeated_by_later_reconciliation(self):
        expired = withdrawal(expires=future(-60))
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, raw, _retry = self.persisted_retry_worker(client=client, raw=expired)
        worker.get_my_locked = Mock(return_value=None)
        worker.request_pending = Mock(return_value=[raw])
        worker.action = Mock(side_effect=UncertainResult())

        self.assertFalse(worker.reconcile())
        self.assertFalse(worker.reconcile())

        worker.action.assert_called_once_with("lock", withdraw_id=raw["id"])

    def test_uncertain_relock_guard_survives_worker_restart(self):
        expired = withdrawal(expires=future(-60))
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, raw, retry = self.persisted_retry_worker(client=client, raw=expired)
        worker.get_my_locked = Mock(return_value=None)
        worker.request_pending = Mock(return_value=[raw])
        worker.action = Mock(side_effect=UncertainResult())

        self.assertFalse(worker.reconcile())

        restarted = self.worker(client=client)
        restarted.get_my_locked = Mock(return_value=None)
        restarted.request_pending = Mock(return_value=[raw])
        restarted.action = Mock()

        self.assertFalse(restarted.reconcile())

        worker.action.assert_called_once_with("lock", withdraw_id=raw["id"])
        restarted.action.assert_not_called()
        current = self.journal.current("phone_A")
        self.assertEqual(current["relock_attempted_for_job_id"], retry.job_id)
        self.assertEqual(current["relock_attempted_withdraw_id"], raw["id"])
        self.assertEqual(current["relock_attempted_tx_id"], raw["tx_id"])
        self.assertEqual(current["relock_outcome"], "uncertain")

    def test_relock_attempt_guard_is_scoped_to_retry_job(self):
        expired = withdrawal(expires=future(-60))
        locked = {**expired, "lock_expires_at": future(600)}
        client = DummyClient()
        client.reconcile_confirmation.return_value = False
        worker, raw, retry = self.persisted_retry_worker(client=client, raw=expired)
        self.journal.update(
            "phone_A",
            relock_attempted_for_job_id="an-older-retry",
            relock_attempted_withdraw_id=raw["id"],
            relock_attempted_tx_id=raw["tx_id"],
            relock_outcome="uncertain",
        )
        worker.get_my_locked = Mock(return_value=None)
        worker.request_pending = Mock(return_value=[raw])
        worker.action = Mock(return_value={"withdrawal": locked})
        self.install_live_renewal_stub(worker)

        self.assertTrue(worker.reconcile())

        worker.action.assert_called_once_with("lock", withdraw_id=raw["id"])
        self.assertEqual(worker.allowed_job, retry.job_id)
        current = self.journal.current("phone_A")
        self.assertEqual(current["relock_attempted_for_job_id"], retry.job_id)
        self.assertEqual(current["relock_outcome"], "locked")

    def test_fresh_session_completes_adopted_retry_through_confirmation(self):
        receipt = self.root / "adopted-retry.png"
        receipt.write_bytes(b"png")
        raw = withdrawal()
        retry = SimpleNamespace(
            job_id="job-retry",
            retry_of_job_id="job-original",
            action="withdraw_v2",
            device_id="phone_A",
            external_ref=raw["tx_id"],
            status="COMPLETED",
            outputs={"receipt_path": str(receipt)},
            screenshots=[{"relative_path": receipt.name}],
        )
        self.scheduler.get_job = Mock(return_value=retry)
        self.journal.begin(
            "phone_A",
            phase="QUEUED",
            withdraw_id=raw["id"],
            tx_id=raw["tx_id"],
            assigned_device_id="phone_A",
            job_id=retry.job_id,
            lock_expires_at=raw["lock_expires_at"],
        )
        worker = self.worker(client=DummyClient())
        worker.socket = DummySocket()
        worker.assigned = (raw, None)
        worker.retry_job_id = retry.job_id
        worker.allowed_job = retry.job_id

        worker._execute_assigned()

        worker.client.confirm_with_receipt.assert_called_once_with(
            raw["id"], receipt.resolve()
        )
        self.assertEqual(self.journal.current("phone_A")["phase"], "CONFIRMED")

    def test_failed_payment_waits_for_and_authorizes_exact_retry_job(self):
        worker = self.worker()
        worker.socket = DummySocket()
        worker.reconciliation_complete = True
        worker.reconciled_generation = worker.socket.generation
        raw = withdrawal()
        worker.assigned = (raw, "dispatch-token")
        self.journal.begin(
            "phone_A",
            phase="QUEUED",
            withdraw_id=raw["id"],
            tx_id=raw["tx_id"],
            assigned_device_id="phone_A",
            job_id="job-original",
            lock_expires_at=raw["lock_expires_at"],
        )
        renewed = future(900)
        worker.action = Mock(return_value={"lock_expires_at": renewed})
        worker._start_renewal()
        self.addCleanup(worker.stop_renewal)
        failed = SimpleNamespace(
            job_id="job-original",
            status="FAILED",
            failed_step_index=33,
        )
        retry = SimpleNamespace(job_id="job-retry")
        self.scheduler.get_job = Mock(
            side_effect=lambda job_id: retry if job_id == retry.job_id else failed
        )
        result = []
        thread = threading.Thread(
            target=lambda: result.append(worker._wait_for_retry(failed))
        )
        thread.start()
        self.assertTrue(wait_for(lambda: worker.retry_waiting_job_id == "job-original"))
        status = worker.status()
        self.assertEqual(status["state"], "NEEDS_ATTENTION")
        self.assertEqual(status["session_state"], "READY")
        self.assertTrue(status["physical_socket_connected"])
        self.assertTrue(worker.session_available())
        self.assertFalse(worker.dispatch_available())
        self.assertTrue(worker.socket.connected)
        self.assertEqual(worker.assigned, (raw, "dispatch-token"))
        self.assertTrue(
            wait_for(
                lambda: any(
                    call.args and call.args[0] == "renew-lock"
                    for call in worker.action.call_args_list
                )
            )
        )
        self.assertEqual(self.journal.current("phone_A")["lock_expires_at"], renewed)

        worker.authorize_retry("job-original", "job-retry")
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [retry])
        self.assertEqual(worker.allowed_job, "job-retry")
        self.assertEqual(self.journal.current("phone_A")["phase"], "QUEUED")

    def test_actual_socket_disconnect_reports_reconnect_state(self):
        worker = self.worker(client=DummyClient({"token": "session-token"}))
        worker.session_max = time.time() + 600
        worker.socket = DummySocket()
        worker.socket.close()

        self.assertFalse(worker.ensure_realtime())

        status = worker.status()
        self.assertEqual(status["state"], "WAITING_FOR_CONNECTION")
        self.assertEqual(status["session_state"], "DISCONNECTED")
        self.assertFalse(status["physical_socket_connected"])

    def test_socket_handshake_error_is_exposed_in_worker_status(self):
        worker = self.worker(client=DummyClient({"token": "session-token"}))
        worker.session_max = time.time() + 600
        worker.socket = DummySocket()
        worker.socket.close()
        worker.socket.error = (
            "Realtime connection failed: ConnectionRefusedError: connection refused"
        )

        self.assertFalse(worker.ensure_realtime())

        status = worker.status()
        self.assertEqual(status["state"], "WAITING_FOR_CONNECTION")
        self.assertEqual(status["error"], worker.socket.error)
        self.assertFalse(status["physical_socket_connected"])

    def test_safe_retry_completion_returns_to_existing_confirmation_path(self):
        receipt = self.root / "retry-receipt.png"
        receipt.write_bytes(b"png")
        original = SimpleNamespace(
            job_id="job-original",
            status="FAILED",
            outputs={},
            screenshots=[],
        )
        retry = SimpleNamespace(
            job_id="job-retry",
            status="COMPLETED",
            outputs={"receipt_path": str(receipt)},
            screenshots=[{"relative_path": receipt.name}],
        )
        self.scheduler.enqueue = Mock(return_value=original)
        self.scheduler.get_job = Mock(
            side_effect=lambda job_id: retry if job_id == retry.job_id else original
        )
        client = DummyClient()
        worker = self.worker(client=client)
        worker.socket = DummySocket()
        raw = withdrawal()
        worker.assigned = (raw, "dispatch-token")
        self.journal.begin(
            "phone_A",
            phase="LOCKED",
            withdraw_id=raw["id"],
            tx_id=raw["tx_id"],
            assigned_device_id="phone_A",
            lock_expires_at=raw["lock_expires_at"],
        )
        workflow = {
            "id": "payment",
            "variables": [],
            "steps": [
                {"id": "submit", "type": "tap", "critical": True},
                {"id": "receipt", "type": "capture_screen"},
            ],
        }
        errors = []
        thread = threading.Thread(
            target=lambda: self._capture_error(
                errors,
                lambda: worker._pay(
                    raw,
                    SimpleNamespace(account_id="account-A", device_id="phone_A"),
                    workflow,
                ),
            )
        )
        thread.start()
        self.assertTrue(
            wait_for(lambda: worker.retry_waiting_job_id == original.job_id)
        )
        worker.authorize_retry(original.job_id, retry.job_id)
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.scheduler.enqueue.assert_called_once()
        client.confirm_with_receipt.assert_called_once_with(
            raw["id"], receipt.resolve()
        )
        self.assertEqual(self.journal.current("phone_A")["phase"], "CONFIRMED")

    @staticmethod
    def _capture_error(errors, operation):
        try:
            operation()
        except Exception as error:  # pragma: no cover - assertion reports detail
            errors.append(error)


class WithdrawRetryDecisionTests(unittest.TestCase):
    class Ready:
        @staticmethod
        def inspect(device_id, *, reserved=False):
            return {
                "device_id": device_id,
                "registered": True,
                "connected": True,
                "camera_configured": True,
                "camera_available": True,
                "runtime_healthy": True,
                "busy": reserved,
                "ready": not reserved,
                "reasons": ["job_reserved"] if reserved else [],
            }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.scheduler = JobScheduler(JobStore(root / "jobs.json"), self.Ready())
        self.journal = WithdrawJournal(root / "journal.json")
        self.workflow = {
            "id": "payment-flow",
            "steps": [
                {"id": "open", "type": "tap"},
                {"id": "balance", "type": "ocr_value"},
                {"id": "account", "type": "input"},
                {"id": "amount", "type": "input"},
                {"id": "submit", "type": "tap"},
                *[{"id": f"step-{index}", "type": "wait"} for index in range(5, 33)],
                {"id": "success", "type": "wait_for_text"},
                {"id": "receipt", "type": "capture_screen"},
            ],
        }
        self.manager = WithdrawManager.__new__(WithdrawManager)
        self.manager.lock = threading.RLock()
        self.manager.scheduler = self.scheduler
        self.manager.journal = self.journal
        self.manager.workflows = SimpleNamespace(read=lambda _ident: self.workflow)
        self.worker = SimpleNamespace(
            validate_retry_lock=Mock(),
            authorize_retry=Mock(),
            cancel_withdrawal=Mock(),
        )
        self.manager.workers = {"phone_A": self.worker}

    def failed(self, index, *, tx_id=None, ready=True):
        tx_id = tx_id or f"TX-{index}-{time.monotonic_ns()}"
        account = SimpleNamespace(account_id="account-A", device_id="phone_A")
        original = self.scheduler.enqueue(
            account,
            self.workflow["id"],
            {
                "account_number": "6222000000000000",
                "bank_name": "Demo Bank",
                "amount": "49.50",
                "final_amount": "49.50",
                "withdraw_id": "935402",
            },
            external_ref=tx_id,
            action="withdraw_v2",
        )
        self.scheduler.reserve_device("phone_A", original.job_id)
        original = self.scheduler.fail_job(
            original.job_id,
            "failed",
            failed_step_index=index,
            failed_step_id=self.workflow["steps"][index]["id"],
        )
        if ready:
            original = self.scheduler.mark_ready(original.job_id)
        self.journal.update(
            "phone_A",
            phase="NEEDS_ATTENTION",
            job_id=original.job_id,
            withdraw_id=935402,
            tx_id=tx_id,
            assigned_device_id="phone_A",
            workflow_id=self.workflow["id"],
            workflow_step_ids=[step["id"] for step in self.workflow["steps"]],
            irreversible_step_indexes=[],
        )
        return original

    def test_manual_cancel_serializes_with_retry_creation(self):
        original = self.failed(33)
        entered = threading.Event()
        release = threading.Event()
        outcomes = []

        def cancel_withdrawal(_reason):
            entered.set()
            release.wait(1)
            self.journal.update("phone_A", phase="CANCELLED", error=None)

        self.worker.cancel_withdrawal.side_effect = cancel_withdrawal
        self.manager.status = Mock(return_value={"phase": "CANCELLED"})

        cancel_thread = threading.Thread(
            target=lambda: outcomes.append(
                ("cancel", self.manager.cancel_withdrawal("phone_A", "bank_rejected"))
            )
        )

        def retry():
            try:
                self.manager.retry_job(original.job_id, "failed_step")
                outcomes.append(("retry", "created"))
            except RecordConflict:
                outcomes.append(("retry", "rejected"))

        cancel_thread.start()
        self.assertTrue(entered.wait(1))
        retry_thread = threading.Thread(target=retry)
        retry_thread.start()
        time.sleep(0.02)
        self.assertEqual(outcomes, [])
        release.set()
        cancel_thread.join(1)
        retry_thread.join(1)

        self.assertEqual(
            sorted(outcomes, key=lambda item: item[0]),
            [("cancel", {"phase": "CANCELLED"}), ("retry", "rejected")],
        )
        retries = [
            job
            for job in self.scheduler.list_jobs()
            if job.retry_of_job_id == original.job_id
        ]
        self.assertEqual(retries, [])

    def test_pre_payment_failures_allow_selected_failed_step(self):
        for index in (0, 1, 2, 3):
            with self.subTest(step=self.workflow["steps"][index]["id"]):
                original = self.failed(index)
                retry = self.manager.retry_job(original.job_id, "failed_step")
                self.assertEqual(retry.start_step_index, index)
                self.assertEqual(retry.retry_of_job_id, original.job_id)
                self.assertEqual(retry.payload, original.payload)
                self.assertEqual(retry.external_ref, original.external_ref)
                self.assertEqual(retry.device_id, original.device_id)
                self.scheduler.cancel_job(retry.job_id)

    def test_failure_at_another_step_resumes_exactly_there(self):
        original = self.failed(2)
        retry = self.manager.retry_job(original.job_id, "failed_step")
        self.assertEqual(retry.start_step_index, 2)

    def test_failure_at_step_33_resumes_exactly_there(self):
        original = self.failed(33)
        retry = self.manager.retry_job(original.job_id, "failed_step")
        self.assertEqual(retry.start_step_index, 33)

    def test_success_wait_or_receipt_resumes_exactly_there(self):
        for index in (33, 34):
            with self.subTest(step=self.workflow["steps"][index]["id"]):
                original = self.failed(index)
                retry = self.manager.retry_job(original.job_id, "failed_step")
                self.assertEqual(retry.start_step_index, index)
                self.scheduler.cancel_job(retry.job_id)

    def test_submission_step_uses_ordinary_failed_step_retry(self):
        original = self.failed(4)
        retry = self.manager.retry_job(original.job_id, "failed_step")
        self.assertEqual(retry.start_step_index, 4)

    def test_explicit_restart_still_starts_at_zero(self):
        original = self.failed(33)
        retry = self.manager.retry_job(original.job_id, "restart")
        self.assertEqual(retry.start_step_index, 0)

    def test_disconnected_or_missing_worker_does_not_block_retry_creation(self):
        original = self.failed(1)
        self.worker.authorize_retry.side_effect = RecordConflict("disconnected")
        self.worker.wake = threading.Event()
        self.journal.update("phone_A", lock_expires_at="2000-01-01T00:00:00+00:00")

        retry = self.manager.retry_job(original.job_id, "failed_step")

        self.assertEqual(retry.start_step_index, 1)
        self.assertTrue(self.worker.wake.is_set())
        self.scheduler.cancel_job(retry.job_id)

        original = self.failed(2)
        self.manager.workers = {}
        restart = self.manager.retry_job(original.job_id, "restart")
        self.assertEqual(restart.start_step_index, 0)

    def test_needs_attention_job_can_run_failed_step_retry(self):
        original = self.failed(2, ready=False)
        self.manager.workers = {}

        retry = self.manager.retry_job(original.job_id, "failed_step")

        self.assertEqual(retry.start_step_index, 2)
        self.assertEqual(
            self.scheduler.get_job(original.job_id).recovery_status,
            "NEEDS_ATTENTION",
        )
        self.assertEqual(retry.status, "QUEUED")

    def test_old_signature_and_unrelated_workflow_edit_do_not_block_retry(self):
        original = self.failed(1)
        self.workflow["steps"][0]["label"] = "Edited after failure"
        self.journal.update("phone_A", workflow_signature="old-signature")

        retry = self.manager.retry_job(original.job_id, "failed_step")

        self.assertEqual(retry.start_step_index, 1)

    def test_changed_failed_step_id_is_blocked_with_clear_error(self):
        original = self.failed(1)
        self.workflow["steps"][1]["id"] = "replacement-step"
        with self.assertRaisesRegex(RecordConflict, "no longer exists"):
            self.manager.retry_job(original.job_id, "failed_step")

    def test_removed_failed_step_is_blocked_with_clear_error(self):
        original = self.failed(34)
        self.workflow["steps"].pop()
        with self.assertRaisesRegex(
            RecordConflict, "failed workflow step is unavailable"
        ):
            self.manager.retry_job(original.job_id, "failed_step")

    def test_terminal_withdrawal_states_are_blocked(self):
        for phase in ("CONFIRMED", "CANCELLED", "RELEASED", "LOCK_REJECTED"):
            with self.subTest(phase=phase):
                original = self.failed(1)
                self.journal.update("phone_A", phase=phase)
                with self.assertRaises(RecordConflict):
                    self.manager.retry_job(original.job_id, "failed_step")

    def test_terminal_withdrawal_state_is_derived_from_current_and_history(self):
        for phase in ("CANCELLED", "CONFIRMED"):
            with self.subTest(phase=phase):
                original = self.failed(1)
                self.journal.update("phone_A", phase=phase)

                state = self.manager.job_withdrawal_state(original)

                self.assertEqual(state["withdrawal_phase"], phase)
                self.assertTrue(state["withdrawal_terminal"])
                self.assertFalse(state["withdrawal_retryable"])

        original = self.failed(1)
        self.journal.update("phone_A", phase="CANCELLED")
        self.journal.begin(
            "phone_A",
            phase="NEEDS_ATTENTION",
            job_id="job_unrelated",
            withdraw_id=999999,
            tx_id="UNRELATED-TX",
            assigned_device_id="phone_A",
            workflow_id=self.workflow["id"],
        )

        state = self.manager.job_withdrawal_state(original)

        self.assertEqual(state["withdrawal_phase"], "CANCELLED")
        self.assertTrue(state["withdrawal_terminal"])
        with self.assertRaisesRegex(RecordConflict, "terminal"):
            self.manager.retry_job(original.job_id, "restart")

    def test_exact_terminal_identity_does_not_suppress_unresolved_job(self):
        terminal = self.failed(1, tx_id="TERMINAL-TX")
        self.journal.update("phone_A", phase="CANCELLED")
        account = SimpleNamespace(account_id="account-A", device_id="phone_A")
        unresolved = self.scheduler.enqueue(
            account,
            self.workflow["id"],
            {"withdraw_id": "935403"},
            external_ref="UNRESOLVED-TX",
            action="withdraw_v2",
        )
        self.scheduler.reserve_device("phone_A", unresolved.job_id)
        unresolved = self.scheduler.fail_job(
            unresolved.job_id,
            "failed",
            failed_step_index=1,
            failed_step_id=self.workflow["steps"][1]["id"],
        )
        unresolved = self.scheduler.mark_ready(unresolved.job_id)
        self.journal.begin(
            "phone_A",
            phase="NEEDS_ATTENTION",
            job_id=unresolved.job_id,
            withdraw_id=935403,
            tx_id="UNRESOLVED-TX",
            assigned_device_id="phone_A",
            workflow_id=self.workflow["id"],
        )

        terminal_state = self.manager.job_withdrawal_state(terminal)
        unresolved_state = self.manager.job_withdrawal_state(unresolved)

        self.assertTrue(terminal_state["withdrawal_terminal"])
        self.assertEqual(unresolved_state["withdrawal_phase"], "NEEDS_ATTENTION")
        self.assertFalse(unresolved_state["withdrawal_terminal"])
        self.assertTrue(unresolved_state["withdrawal_retryable"])

    def test_non_withdraw_job_has_no_withdrawal_business_state(self):
        ordinary = SimpleNamespace(action=None)
        self.assertEqual(self.manager.job_withdrawal_state(ordinary), {})

    def test_duplicate_active_retry_is_blocked(self):
        original = self.failed(1)
        self.manager.retry_job(original.job_id, "failed_step")
        with self.assertRaisesRegex(RecordConflict, "already active"):
            self.manager.retry_job(original.job_id, "failed_step")

    def test_empty_irreversible_metadata_does_not_block_retry(self):
        original = self.failed(1)
        self.journal.update(
            "phone_A",
            irreversible_step_indexes=[],
        )

        retry = self.manager.retry_job(original.job_id, "failed_step")

        self.assertEqual(retry.start_step_index, 1)


class WithdrawStartupJournalRecoveryTests(unittest.TestCase):
    class Ready:
        @staticmethod
        def inspect(device_id, *, reserved=False):
            return {
                "device_id": device_id,
                "registered": True,
                "connected": True,
                "camera_configured": True,
                "camera_available": True,
                "runtime_healthy": True,
                "busy": reserved,
                "ready": not reserved,
                "reasons": [],
            }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scheduler = JobScheduler(JobStore(self.root / "jobs.json"), self.Ready())
        self.journal_path = self.root / "journal.json"
        self.raw = withdrawal()
        self.account = SimpleNamespace(
            account_id="withdraw_operator_101", device_id="phone_A"
        )
        self.executor = Mock()
        self.service = SimpleNamespace()
        self.assignments = Mock()
        self.assignments.integrations.return_value = {}

    def enqueue(self, *, metadata=True):
        protected_metadata = None
        if metadata:
            protected_metadata = {
                "version": 1,
                "withdraw_id": self.raw["id"],
                "tx_id": self.raw["tx_id"],
                "assigned_device_id": "phone_A",
                "operator_id": 101,
                "account_id": self.account.account_id,
                "workflow_id": "payment-flow",
                "final_amount": "4950.00",
                "raw_withdrawal": self.raw,
                "lock_expires_at": self.raw["lock_expires_at"],
            }
        return self.scheduler.enqueue(
            self.account,
            "payment-flow",
            {"amount": "4950.00"},
            external_ref=self.raw["tx_id"],
            action="withdraw_v2",
            protected_metadata=protected_metadata,
        )

    def start_manager(self):
        return WithdrawManager(
            assignments=self.assignments,
            accounts=Mock(),
            workflows=Mock(),
            service=self.service,
            scheduler=self.scheduler,
            executor=self.executor,
            journal_path=self.journal_path,
            start_workers=False,
        )

    def test_missing_journal_is_reconstructed_with_complete_exact_identity(self):
        job = self.enqueue()

        manager = self.start_manager()
        current = manager.journal.current("phone_A")

        self.assertEqual(current["phase"], "NEEDS_ATTENTION")
        self.assertEqual(current["withdraw_id"], self.raw["id"])
        self.assertEqual(current["tx_id"], self.raw["tx_id"])
        self.assertEqual(current["assigned_device_id"], "phone_A")
        self.assertEqual(current["operator_id"], 101)
        self.assertEqual(current["account_id"], self.account.account_id)
        self.assertEqual(current["workflow_id"], "payment-flow")
        self.assertEqual(current["job_id"], job.job_id)
        self.assertIsNone(current["retry_of_job_id"])
        self.assertEqual(current["final_amount"], "4950.00")
        self.assertEqual(current["transfer_method"], "bank_account")
        self.assertEqual(current["raw_withdrawal"], self.raw)
        self.assertEqual(current["lock_expires_at"], self.raw["lock_expires_at"])
        self.assertTrue(current["recovery_identity_complete"])
        self.assertEqual(current["missing_recovery_fields"], [])

    def test_reconstruction_does_not_execute_or_confirm_anything(self):
        job = self.enqueue()

        manager = self.start_manager()

        self.assertEqual(self.scheduler.get_job(job.job_id).status, "CANCELLED")
        self.assertEqual(manager.workers, {})
        self.assertEqual(self.executor.mock_calls, [])

    def test_existing_complete_journal_is_updated_without_replacement(self):
        job = self.enqueue()
        journal = WithdrawJournal(self.journal_path)
        journal.begin(
            "phone_A",
            phase="LOCKED",
            withdraw_id=self.raw["id"],
            tx_id=self.raw["tx_id"],
            assigned_device_id="phone_A",
            operator_id=101,
            final_amount="4950.00",
            raw_withdrawal=self.raw,
            lock_expires_at=self.raw["lock_expires_at"],
        )

        manager = self.start_manager()
        current = manager.journal.current("phone_A")

        self.assertEqual(current["phase"], "NEEDS_ATTENTION")
        self.assertEqual(current["job_id"], job.job_id)
        self.assertEqual(current["raw_withdrawal"], self.raw)
        self.assertEqual(current["lock_expires_at"], self.raw["lock_expires_at"])
        self.assertEqual(current["operator_id"], 101)

    def test_conflicting_journal_is_preserved_instead_of_overwritten(self):
        job = self.enqueue()
        journal = WithdrawJournal(self.journal_path)
        journal.begin(
            "phone_A",
            phase="NEEDS_ATTENTION",
            withdraw_id=123456,
            tx_id="OTHER-TX",
            assigned_device_id="phone_A",
            operator_id=101,
            job_id="job_other",
            workflow_id="other-flow",
            raw_withdrawal={"id": 123456, "tx_id": "OTHER-TX"},
        )
        before = journal.current("phone_A")

        with self.assertLogs("withdraw_v2", level="ERROR"):
            manager = self.start_manager()

        self.assertEqual(manager.journal.current("phone_A"), before)
        self.assertEqual(self.scheduler.get_job(job.job_id).status, "CANCELLED")

    def test_second_startup_is_idempotent(self):
        self.enqueue()
        first = self.start_manager()
        recovered = first.journal.current("phone_A")

        second = self.start_manager()

        self.assertEqual(second.journal.current("phone_A"), recovered)
        with self.journal_path.open(encoding="utf-8") as stream:
            persisted = json.load(stream)
        self.assertEqual(len(persisted["phone_A"].get("history", [])), 0)

    def test_legacy_job_records_unknown_identity_fields_explicitly(self):
        job = self.enqueue(metadata=False)

        manager = self.start_manager()
        current = manager.journal.current("phone_A")

        self.assertEqual(current["job_id"], job.job_id)
        self.assertEqual(current["tx_id"], self.raw["tx_id"])
        self.assertEqual(current["assigned_device_id"], "phone_A")
        self.assertEqual(current["account_id"], self.account.account_id)
        self.assertEqual(current["workflow_id"], "payment-flow")
        self.assertFalse(current["recovery_identity_complete"])
        self.assertEqual(
            current["missing_recovery_fields"],
            [
                "withdraw_id",
                "operator_id",
                "raw_withdrawal",
                "lock_expires_at",
                "final_amount",
                "transfer_method",
            ],
        )

    def test_protected_metadata_is_not_exposed_by_public_job_status(self):
        job = self.enqueue()

        public = self.scheduler.public(job)

        self.assertNotIn("protected_metadata", public)
        self.assertNotIn("raw_withdrawal", public)


class WithdrawTransportTests(unittest.TestCase):
    def test_channel_authorization_posts_bearer_json_and_accepts_raw_pusher_data(self):
        response = SimpleNamespace(
            status_code=200,
            headers={},
            json=lambda: {
                "auth": "public-key:signature",
                "channel_data": '{"user_id":"1072"}',
            },
        )
        http = Mock()
        http.request.return_value = response
        client = WithdrawClient("https://staging.example", http=http)
        client.token = "private-session-token"

        result = client.authorize(
            "/api/admin-withdraw/v2/broadcasting/auth",
            "123.456",
            "private-withdraw-session-1072",
        )

        self.assertEqual(result["auth"], "public-key:signature")
        self.assertIn("channel_data", result)
        request = http.request.call_args
        self.assertEqual(request.args[:2], ("POST", "https://staging.example/api/admin-withdraw/v2/broadcasting/auth"))
        self.assertEqual(
            request.kwargs["headers"]["Authorization"],
            "Bearer private-session-token",
        )
        self.assertEqual(request.kwargs["headers"]["Accept"], "application/json")
        self.assertEqual(
            request.kwargs["json"],
            {
                "socket_id": "123.456",
                "channel_name": "private-withdraw-session-1072",
            },
        )

    def test_failed_handshake_preserves_safe_error_for_worker_status(self):
        def fail_connection(_url, **_kwargs):
            raise ConnectionRefusedError(
                "connection refused /app/private-key?token=private-token"
            )

        transport = SharedPusherTransport(connect=fail_connection)
        socket = PusherSocket(
            FakeAuthClient("token-A"),
            device_id="phone_A",
            transport=transport,
        )
        self.addCleanup(transport.close)

        with self.assertLogs("withdraw_v2", level="WARNING") as captured:
            transport.register(
                "phone_A",
                socket.client,
                realtime("private-session-A", ["private-queue-1"]),
                lambda: None,
            )
            socket.registered = True
            self.assertTrue(
                wait_for(lambda: socket.error is not None),
                "handshake error was not retained",
            )

        self.assertFalse(socket.physical_connected)
        self.assertEqual(socket.session_state, "WAITING_FOR_CONNECTION")
        self.assertIn("Realtime connection failed", socket.error)
        self.assertIn("connection refused", socket.error)
        self.assertNotIn("token-A", socket.error)
        self.assertNotIn("private-key", socket.error)
        self.assertNotIn("private-token", socket.error)
        warning = "\n".join(captured.output)
        self.assertIn("Withdraw realtime connection failed", warning)
        self.assertIn("ConnectionRefusedError", warning)
        self.assertIn("wss://realtime.example:443/app/<redacted>", warning)
        self.assertNotIn("private-key", warning)
        self.assertNotIn("private-token", warning)

    def test_pusher_url_uses_returned_tls_endpoint_and_key(self):
        urls = []
        connection_options = []

        def connect(url, **kwargs):
            urls.append(url)
            connection_options.append(kwargs)
            return FakeWebSocket("socket-1")

        transport = SharedPusherTransport(connect=connect)
        socket = PusherSocket(
            FakeAuthClient("token-A"),
            device_id="phone_A",
            transport=transport,
        )
        self.addCleanup(transport.close)
        settings = realtime("private-session-A", ["private-queue-1"])
        settings.update(
            host="realtime.staging.example",
            port=7443,
            scheme="https",
            key="public/key value",
        )

        with self.assertLogs("withdraw_v2", level="INFO") as captured:
            socket.connect(settings)

        self.assertEqual(
            urls[0],
            "wss://realtime.staging.example:7443/app/public%2Fkey%20value"
            "?protocol=7&client=phone-controller&version=1.0",
        )
        sslopt = connection_options[0]["sslopt"]
        self.assertEqual(sslopt["ca_certs"], certifi.where())
        self.assertEqual(sslopt["cert_reqs"], ssl.CERT_REQUIRED)
        self.assertNotIn("check_hostname", sslopt)
        stages = "\n".join(captured.output)
        self.assertIn("websocket URL structure", stages)
        self.assertIn("TCP/TLS/WebSocket open attempt", stages)
        self.assertIn("WebSocket open succeeded", stages)
        self.assertIn("waiting for pusher:connection_established", stages)
        self.assertIn("received pusher:connection_established", stages)
        self.assertNotIn("public/key value", stages)
        self.assertNotIn("public%2Fkey%20value", stages)

    def test_plaintext_localhost_socket_does_not_receive_tls_options(self):
        connection_options = []

        def connect(_url, **kwargs):
            connection_options.append(kwargs)
            return FakeWebSocket("socket-local")

        transport = SharedPusherTransport(connect=connect)
        socket = PusherSocket(
            FakeAuthClient("token-A"),
            device_id="phone_A",
            transport=transport,
        )
        self.addCleanup(transport.close)
        settings = realtime("private-session-A", ["private-queue-1"])
        settings.update(host="127.0.0.1", port=9101, scheme="http")

        with patch.dict(
            "os.environ", {"WITHDRAW_ALLOW_INSECURE_LOCALHOST": "1"}, clear=False
        ):
            socket.connect(settings)

        self.assertNotIn("sslopt", connection_options[0])

    def test_concurrent_first_registrations_start_one_physical_socket(self):
        connections = FakeConnections()
        transport = SharedPusherTransport(connect=connections)
        sockets = [
            PusherSocket(
                FakeAuthClient("token-A"),
                device_id="phone_A",
                transport=transport,
            ),
            PusherSocket(
                FakeAuthClient("token-B"),
                device_id="phone_B",
                transport=transport,
            ),
        ]
        self.addCleanup(transport.close)
        threads = [
            threading.Thread(
                target=socket.connect,
                args=(realtime(f"private-session-{index}", ["private-common"]),),
            )
            for index, socket in enumerate(sockets)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertTrue(all(socket.connected for socket in sockets))
        self.assertEqual(len(connections.sockets), 1)

    def test_failed_first_connection_retries_without_duplicate_transport(self):
        connections = FailOnceConnections()
        transport = SharedPusherTransport(connect=connections)
        socket = PusherSocket(
            FakeAuthClient("token-A"),
            device_id="phone_A",
            transport=transport,
        )
        self.addCleanup(transport.close)
        socket.connect(realtime("private-session-A", ["private-common"]))

        self.assertTrue(socket.connected)
        self.assertEqual(connections.calls, 2)
        self.assertEqual(len(connections.sockets), 1)
        self.assertIsNotNone(transport.thread)

    def test_plaintext_realtime_opt_in_is_localhost_only(self):
        settings = realtime("private-session-A", ["private-common"])
        settings.update(host="127.0.0.1", port=9101, scheme="http")
        with patch.dict(
            "os.environ", {"WITHDRAW_ALLOW_INSECURE_LOCALHOST": "1"}, clear=False
        ):
            self.assertEqual(_settings(settings)[0][:2], ("127.0.0.1", 9101))
            with self.assertRaises(APIError):
                _settings({**settings, "host": "192.168.1.20"})
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(APIError):
                _settings(settings)

    def test_two_enabled_manager_workers_reuse_one_physical_connection(self):
        connections = FakeConnections()
        transport = SharedPusherTransport(connect=connections)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = {
                device: {
                    "balance_workflow_id": "balance-" + device,
                    "withdraw_api": {
                        "enabled": True,
                        "base_url": "https://withdraw.example",
                        "operator_id": number,
                        "username": "operator-" + device,
                        "credential_reference": "credential-" + device,
                        "balance_refresh_seconds": 60,
                    },
                }
                for device, number in (("phone_A", 7), ("phone_B", 12))
            }
            assignments = Mock()
            assignments.integrations.return_value = configs
            scheduler = SimpleNamespace(list_jobs=lambda: [], integration_gate=None)
            service = SimpleNamespace(
                screenshot_store=SimpleNamespace(root=root / "screens")
            )
            manager = WithdrawManager(
                assignments=assignments,
                accounts=AccountBindingStore(root / "accounts.json"),
                workflows=Mock(),
                service=service,
                scheduler=scheduler,
                executor=Mock(),
                journal_path=root / "journal.json",
                client_factory=lambda _origin: DummyClient(),
                credential_store=Mock(),
                realtime_transport=transport,
            )
            self.addCleanup(manager.close)
            for device, token in (("phone_A", "token-A"), ("phone_B", "token-B")):
                profile = {
                    "token": token,
                    "realtime": realtime(
                        "private-session-" + device[-1], ["private-common"]
                    ),
                }
                manager.workers[device].client = FakeAuthClient(token, profile)
                manager.workers[device].connect_socket()

            self.assertIs(
                manager.workers["phone_A"].realtime_transport,
                manager.workers["phone_B"].realtime_transport,
            )
            self.assertEqual(len(connections.sockets), 1)

    def test_my_approved_requires_exact_tx_id_and_withdraw_id(self):
        client = WithdrawClient("https://withdraw.example")
        client.request = Mock(
            return_value={
                "withdrawals": [{"id": 935402, "tx_id": "TX9F3K299", "status": 1}],
                "pagination": {"last_page": 1},
            }
        )
        self.assertFalse(client.reconcile_confirmation("TX9F3K2", 935402))
        client.request.return_value = {
            "withdrawals": [{"id": 935402, "tx_id": "TX9F3K2", "status": 1}],
            "pagination": {"last_page": 1},
        }
        self.assertTrue(client.reconcile_confirmation("TX9F3K2", 935402))

    def test_receipt_confirmation_only_uses_regular_supported_file_and_correct_path(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.png"
            path.write_bytes(b"png")
            client = WithdrawClient("https://withdraw.example")
            client.request = Mock(return_value={})
            client.confirm_with_receipt(935402, path)
            method, endpoint = client.request.call_args.args[:2]
            self.assertEqual(
                (method, endpoint),
                ("POST", "/api/admin-withdraw/v2/withdrawals/935402/confirm"),
            )
            with self.assertRaises(ValueError):
                client.confirm_with_receipt(935402, Path(directory) / "missing.png")

    def test_two_sessions_share_one_socket_deduplicate_queues_and_correlate_actions(
        self,
    ):
        connections = FakeConnections()
        transport = SharedPusherTransport(connect=connections)
        client_a, client_b = FakeAuthClient("token-A"), FakeAuthClient("token-B")
        changed = []
        socket_a = PusherSocket(
            client_a,
            device_id="phone_A",
            on_queue_changed=lambda: changed.append("A"),
            transport=transport,
        )
        socket_b = PusherSocket(
            client_b,
            device_id="phone_B",
            on_queue_changed=lambda: changed.append("B"),
            transport=transport,
        )
        self.addCleanup(transport.close)
        socket_a.connect(
            realtime("private-session-A", ["private-common", "private-queue-A"])
        )
        socket_b.connect(
            realtime("private-session-B", ["private-common", "private-queue-B"])
        )

        self.assertEqual(len(connections.sockets), 1)
        physical = connections.sockets[0]
        subscriptions = [
            message
            for message in physical.sent
            if message["event"] == "pusher:subscribe"
        ]
        channels = [message["data"]["channel"] for message in subscriptions]
        self.assertEqual(channels.count("private-common"), 1)
        self.assertEqual(channels.count("private-queue-A"), 1)
        self.assertEqual(channels.count("private-queue-B"), 1)
        auth_by_channel = {
            message["data"]["channel"]: message["data"]["auth"]
            for message in subscriptions
        }
        self.assertEqual(
            auth_by_channel["private-session-A"], "token-A:private-session-A"
        )
        self.assertEqual(
            auth_by_channel["private-session-B"], "token-B:private-session-B"
        )
        self.assertIn(
            (
                "/api/broadcasting/auth",
                "socket-1",
                "private-session-A",
                "token-A",
            ),
            client_a.calls,
        )
        self.assertIn(
            (
                "/api/broadcasting/auth",
                "socket-1",
                "private-queue-A",
                "token-A",
            ),
            client_a.calls,
        )

        answers = []

        def call(sock, label):
            answers.append((label, sock.action("pending", timeout=1)))

        first = threading.Thread(target=call, args=(socket_a, "A"))
        second = threading.Thread(target=call, args=(socket_b, "B"))
        first.start()
        second.start()
        self.assertTrue(
            wait_for(
                lambda: len(
                    [m for m in physical.sent if m["event"] == "client-pending"]
                )
                == 2
            )
        )
        sent_actions = [
            message for message in physical.sent if message["event"] == "client-pending"
        ]
        for message in reversed(sent_actions):
            physical.emit(
                "action.result",
                channel=message["channel"],
                data={
                    "request_id": message["data"]["request_id"],
                    "code": 200,
                    "status": 1,
                    "data": {"pending": [message["channel"]]},
                },
            )
        first.join(1)
        second.join(1)
        self.assertCountEqual(
            answers,
            [
                ("A", {"pending": ["private-session-A"]}),
                ("B", {"pending": ["private-session-B"]}),
            ],
        )
        physical.emit("queue.changed", channel="private-common")
        self.assertTrue(wait_for(lambda: sorted(changed) == ["A", "B"]))

    def test_reconnect_resubscribes_all_sessions_once_and_keeps_one_reconnect_loop(
        self,
    ):
        connections = FakeConnections()
        transport = SharedPusherTransport(connect=connections)
        socket_a = PusherSocket(
            FakeAuthClient("token-A"), device_id="phone_A", transport=transport
        )
        socket_b = PusherSocket(
            FakeAuthClient("token-B"), device_id="phone_B", transport=transport
        )
        self.addCleanup(transport.close)
        socket_a.connect(
            realtime("private-session-A", ["private-common", "private-queue-A"])
        )
        socket_b.connect(
            realtime("private-session-B", ["private-common", "private-queue-B"])
        )
        generation_a, generation_b = socket_a.generation, socket_b.generation

        connections.sockets[0].fail_connection()
        self.assertTrue(wait_for(lambda: len(connections.sockets) == 2))
        self.assertTrue(wait_for(lambda: socket_a.connected and socket_b.connected))
        self.assertGreater(socket_a.generation, generation_a)
        self.assertGreater(socket_b.generation, generation_b)
        resubscribed = [
            message["data"]["channel"]
            for message in connections.sockets[1].sent
            if message["event"] == "pusher:subscribe"
        ]
        self.assertCountEqual(
            resubscribed,
            [
                "private-common",
                "private-queue-A",
                "private-queue-B",
                "private-session-A",
                "private-session-B",
            ],
        )

    def test_one_session_can_reauthenticate_or_stop_without_disrupting_the_other(self):
        connections = FakeConnections()
        transport = SharedPusherTransport(connect=connections)
        client_a, client_b = FakeAuthClient("token-A"), FakeAuthClient("token-B")
        socket_a = PusherSocket(client_a, device_id="phone_A", transport=transport)
        socket_b = PusherSocket(client_b, device_id="phone_B", transport=transport)
        self.addCleanup(transport.close)
        settings_a = realtime("private-session-A", ["private-common"])
        socket_a.connect(settings_a)
        socket_b.connect(realtime("private-session-B", ["private-common"]))

        client_a.fail.add("private-session-A")
        with self.assertRaises(APIError) as raised:
            socket_a.connect(settings_a)
        self.assertEqual(raised.exception.code, 401)
        self.assertTrue(socket_b.connected)
        self.assertTrue(transport.physical_connected)
        self.assertEqual(len(connections.sockets), 1)

        client_a.fail.clear()
        client_a.token = "token-A-refreshed"
        socket_a.connect(settings_a)
        self.assertTrue(socket_a.connected)
        self.assertTrue(socket_b.connected)
        self.assertEqual(len(connections.sockets), 1)

        socket_a.close()
        self.assertTrue(wait_for(lambda: socket_b.connected))
        self.assertTrue(transport.physical_connected)
        socket_b.close()
        self.assertTrue(wait_for(lambda: not transport.physical_connected))
        self.assertTrue(wait_for(lambda: transport.thread is None))
