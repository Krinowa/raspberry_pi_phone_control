"""Focused contracts for Withdraw retry recovery and dispatch contention."""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from automation.integrations.withdraw_v2.journal import WithdrawJournal
from automation.integrations.withdraw_v2.manager import WithdrawManager
from automation.integrations.withdraw_v2.worker import WithdrawWorker
from automation.jobs.service import JobScheduler
from automation.jobs.storage import JobStore, RecordConflict


class ReadyDevice:
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


class RetryRecoverySafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.scheduler = JobScheduler(JobStore(root / "jobs.json"), ReadyDevice())
        self.account = SimpleNamespace(account_id="account-A", device_id="phone_A")

    def failed_job(self):
        original = self.scheduler.enqueue(
            self.account,
            "payment-flow",
            {},
            external_ref="TX-RETRY",
            action="withdraw_v2",
        )
        self.scheduler.reserve_device("phone_A", original.job_id)
        return self.scheduler.fail_job(
            original.job_id,
            "workflow failed",
            failed_step_index=3,
            failed_step_id="failed-step",
        )

    def test_failed_step_retry_runs_without_mutating_attention(self):
        original = self.failed_job()
        self.assertEqual(original.recovery_status, "NEEDS_ATTENTION")

        retry = self.scheduler.create_retry(
            original.job_id, "failed_step", withdraw_authorized=True
        )

        self.assertEqual(
            self.scheduler.get_job(original.job_id).recovery_status,
            "NEEDS_ATTENTION",
        )
        self.assertEqual(retry.status, "QUEUED")
        claimed = self.scheduler.claim_next_job_for_device("phone_A")

        self.assertEqual(claimed.job_id, retry.job_id)
        self.assertEqual(claimed.status, "RUNNING")
        self.assertEqual(
            self.scheduler.get_job(original.job_id).recovery_status,
            "NEEDS_ATTENTION",
        )

    def test_manager_authorizes_attention_retry_without_clearing_payment_state(self):
        original = self.failed_job()
        journal = WithdrawJournal(Path(self.temporary.name) / "journal.json")
        journal.begin(
            "phone_A",
            phase="NEEDS_ATTENTION",
            withdraw_id=101,
            tx_id=original.external_ref,
            assigned_device_id="phone_A",
            workflow_id=original.workflow,
            job_id=original.job_id,
            confirmation_marker="unchanged",
        )
        worker = SimpleNamespace(
            stop=threading.Event(),
            allowed_job=None,
            socket=SimpleNamespace(connected=True),
            lock_lost=threading.Event(),
        )

        def authorize_retry(original_job_id, retry_job_id):
            self.assertEqual(original_job_id, original.job_id)
            worker.allowed_job = retry_job_id

        worker.authorize_retry = authorize_retry
        manager = WithdrawManager.__new__(WithdrawManager)
        manager.lock = threading.RLock()
        manager.scheduler = self.scheduler
        manager.journal = journal
        manager.workflows = SimpleNamespace(
            read=lambda workflow_id: {
                "id": workflow_id,
                "steps": [
                    {"id": "step-0", "type": "tap"},
                    {"id": "step-1", "type": "tap"},
                    {"id": "step-2", "type": "tap"},
                    {"id": "failed-step", "type": "wait_for_text"},
                ],
            }
        )
        manager.workers = {"phone_A": worker}
        self.scheduler.integration_gate = manager.withdraw_job_blocked

        retry = manager.retry_job(original.job_id, "failed_step")
        claimed = self.scheduler.claim_next_job_for_device("phone_A")

        self.assertEqual(claimed.job_id, retry.job_id)
        self.assertEqual(claimed.start_step_index, original.failed_step_index)
        self.assertEqual(
            self.scheduler.get_job(original.job_id).recovery_status,
            "NEEDS_ATTENTION",
        )
        self.assertEqual(
            journal.current("phone_A")["confirmation_marker"], "unchanged"
        )

    def test_restart_requires_ready_without_creating_a_job_or_changing_journal(self):
        original = self.failed_job()
        journal = WithdrawJournal(Path(self.temporary.name) / "journal.json")
        journal.begin(
            "phone_A",
            phase="NEEDS_ATTENTION",
            withdraw_id=101,
            tx_id=original.external_ref,
            assigned_device_id="phone_A",
            workflow_id=original.workflow,
            job_id=original.job_id,
            confirmation_marker="unchanged",
        )
        manager = WithdrawManager.__new__(WithdrawManager)
        manager.lock = threading.RLock()
        manager.scheduler = self.scheduler
        manager.journal = journal
        manager.workflows = SimpleNamespace(
            read=lambda workflow_id: {
                "id": workflow_id,
                "steps": [
                    {"id": "step-0", "type": "tap"},
                    {"id": "step-1", "type": "tap"},
                    {"id": "step-2", "type": "tap"},
                    {"id": "failed-step", "type": "wait_for_text"},
                ],
            }
        )
        manager.workers = {}
        journal_before = journal.current("phone_A")
        jobs_before = [job.job_id for job in self.scheduler.list_jobs()]

        with self.assertRaisesRegex(
            RecordConflict,
            "Phone is not verified ready.*Verify Phone Ready",
        ):
            manager.retry_job(original.job_id, "restart")

        self.assertEqual(
            [job.job_id for job in self.scheduler.list_jobs()], jobs_before
        )
        self.assertEqual(journal.current("phone_A"), journal_before)
        self.assertEqual(
            self.scheduler.get_job(original.job_id).recovery_status,
            "NEEDS_ATTENTION",
        )

        self.scheduler.mark_ready(original.job_id)
        restart = manager.retry_job(original.job_id, "restart")

        self.assertEqual(restart.start_step_index, 0)
        self.assertEqual(restart.status, "QUEUED")
        self.assertEqual(
            self.scheduler.get_job(original.job_id).recovery_status,
            "READY",
        )


class DispatchContentionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.scheduler = JobScheduler(JobStore(root / "jobs.json"), ReadyDevice())
        self.journal = WithdrawJournal(root / "journal.json")
        self.account = SimpleNamespace(account_id="account-A", device_id="phone_A")
        self.worker = WithdrawWorker.__new__(WithdrawWorker)
        self.worker.device_id = "phone_A"
        self.worker.scheduler = self.scheduler
        self.worker.journal = self.journal
        self.worker.executor = SimpleNamespace(wake=Mock())
        self.worker.allowed_job = None
        self.worker._finish_payment_job = Mock()
        self.worker.socket = SimpleNamespace(connected=True)
        self.worker.renew_thread = SimpleNamespace(is_alive=lambda: True)
        self.worker.attention = Mock()
        self.worker.disconnect_socket = Mock()
        self.raw = {
            "id": 101,
            "tx_id": "TX-CONTENTION",
            "order_id": "ORDER-CONTENTION",
            "type": "bank",
            "transfer_method": "bank_account",
            "account_number": "6222000000000000",
            "account_name": "Demo Recipient",
            "bank_name": "Demo Bank",
            "final_amount": "10.00",
        }
        self.workflow = {"id": "payment-flow", "variables": [], "steps": []}

    def test_queued_contention_defers_without_attention_or_duplicate_payment(self):
        earlier = self.scheduler.enqueue(self.account, "maintenance-flow", {})
        dispatch_token = "withdraw-dispatch:phone_A"
        self.scheduler.reservations["phone_A"] = dispatch_token
        self.journal.begin(
            "phone_A",
            phase="LOCKED",
            withdraw_id=self.raw["id"],
            tx_id=self.raw["tx_id"],
            assigned_device_id="phone_A",
        )

        self.worker._pay(
            self.raw,
            self.account,
            self.workflow,
            dispatch_reservation=dispatch_token,
        )

        current = self.journal.current("phone_A")
        payment_jobs = [
            job
            for job in self.scheduler.list_jobs()
            if job.action == "withdraw_v2"
        ]
        self.assertEqual(current["phase"], "QUEUED")
        self.assertNotEqual(current["phase"], "NEEDS_ATTENTION")
        self.assertTrue(self.worker.socket.connected)
        self.assertTrue(self.worker.renew_thread.is_alive())
        self.worker.attention.assert_not_called()
        self.worker.disconnect_socket.assert_not_called()
        self.assertEqual(len(payment_jobs), 1)
        self.assertEqual(
            self.scheduler.next_job_for_device("phone_A").job_id, earlier.job_id
        )

        first = self.scheduler.claim_next_job_for_device("phone_A")
        self.assertEqual(first.job_id, earlier.job_id)
        self.scheduler.complete_job(first.job_id, "maintenance complete")
        payment = self.scheduler.claim_next_job_for_device("phone_A")

        self.assertEqual(payment.job_id, payment_jobs[0].job_id)
        self.assertEqual(payment.status, "RUNNING")
        self.worker._finish_payment_job.assert_called_once_with(
            self.raw, payment_jobs[0]
        )


if __name__ == "__main__":
    unittest.main()
