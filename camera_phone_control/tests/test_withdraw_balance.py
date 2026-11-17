import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from automation.integrations.withdraw_v2.balance import (
    BalanceCache,
    CameraOCRBalanceProvider,
    balance_check_workflow,
    final_amount,
    select_pending,
)
from automation.jobs.service import JobScheduler
from automation.jobs.storage import JobStore, RecordConflict


class WithdrawBalanceTests(unittest.TestCase):
    def test_balance_check_contract_allows_generic_steps_but_rejects_payment(self):
        valid = {
            "purpose": "balance_check",
            "steps": [
                {"type": "tap"},
                {"type": "input"},
                {"type": "capture_screen", "output_key": "receipt_path"},
                {
                    "type": "if_text",
                    "then_steps": [
                        {
                            "type": "ocr_value",
                            "parser": "currency",
                            "output": "payer_balance",
                        }
                    ],
                    "else_steps": [{"type": "swipe"}],
                },
            ]
        }
        self.assertIs(balance_check_workflow(valid), valid)
        with self.assertRaisesRegex(ValueError, "does not contain Read Balance"):
            balance_check_workflow(
                {"purpose": "balance_check", "steps": [{"type": "capture_screen"}]}
            )
        with self.assertRaisesRegex(ValueError, "irreversible payment step"):
            balance_check_workflow(
                {
                    "purpose": "balance_check",
                    "steps": [
                        valid["steps"][-1],
                        {"type": "tap", "critical": True},
                    ]
                }
            )

    def test_first_affordable_final_amount_in_api_order(self):
        pending = [
            {"id": 1, "final_amount": "5000.00", "withdraw_amount": "1.00"},
            {"id": 2, "final_amount": "1000.00", "withdraw_amount": "9000.00"},
            {"id": 3, "final_amount": "700.00"},
        ]
        self.assertIs(select_pending(pending, Decimal("3280.50")), pending[1])
        self.assertIsNone(select_pending(pending, Decimal("600.00")))
        for amount in (None, True, 1.2, "NaN", "-12", "1.234", "0.00"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                final_amount({"final_amount": amount})

    def test_cache_is_per_device_expires_and_invalidates(self):
        a, b = BalanceCache("phone_A"), BalanceCache("phone_B")
        self.assertFalse(a.valid())
        a.observe(Decimal("8230.50"))
        b.observe(Decimal("99.00"))
        a.confirmed_payment(Decimal("4950.00"))
        self.assertEqual(a.available_balance, Decimal("3280.50"))
        self.assertEqual(b.available_balance, Decimal("99.00"))
        self.assertFalse(a.valid(now=a.observed_monotonic + 61))
        a.invalidate()
        self.assertFalse(a.valid())
        a.confirmed_payment(Decimal("1.00"))
        self.assertEqual(a.available_balance, Decimal("3280.50"))
        self.assertFalse(BalanceCache("phone_A").valid())

    def test_stale_value_is_retained_but_assignment_context_does_not_carry_over(self):
        cache = BalanceCache("phone_A", refresh_seconds=60)
        context = ("phone_A", 7, ("2b",), "balance-a")
        cache.observe(Decimal("616.50"), now=100, context=context)

        self.assertFalse(cache.valid(now=161, context=context))
        self.assertEqual(cache.available_balance, Decimal("616.50"))
        self.assertIsNotNone(cache.observed_at)
        self.assertFalse(
            cache.valid(now=101, context=("phone_A", 8, ("2b",), "balance-a"))
        )
        cache.clear()
        self.assertIsNone(cache.available_balance)
        self.assertIsNone(cache.observed_at)

    def test_provider_uses_same_device_reservation_and_existing_service(self):
        with tempfile.TemporaryDirectory() as directory:
            readiness = SimpleNamespace(inspect=lambda device_id, reserved=False: {"ready": not reserved})
            scheduler = JobScheduler(JobStore(Path(directory) / "jobs.json"), readiness)
            workflow = {"id": "balance_a", "purpose": "balance_check", "steps": [{"type": "ocr_value", "parser": "currency", "output": "payer_balance"}]}
            storage = Mock()
            storage.read.return_value = workflow
            service = Mock()
            service.start.return_value = {"run_id": "balance_run"}

            def wait(run_id):
                self.assertFalse(scheduler.is_device_ready("phone_A")["ready"])
                self.assertTrue(scheduler.is_device_ready("phone_B")["ready"])
                return {"status": "passed", "outputs": {"payer_balance": "8230.50"}}

            service.wait.side_effect = wait
            provider = CameraOCRBalanceProvider(scheduler, storage, service, lambda device: "balance_a" if device == "phone_A" else None)
            self.assertEqual(provider.get_available_balance("phone_A"), Decimal("8230.50"))
            service.start.assert_called_once_with(workflow, {}, device_id="phone_A")
            self.assertEqual(scheduler.reservations, {})
            with self.assertRaises(ValueError):
                provider.get_available_balance("phone_B")
            service.wait.side_effect = RuntimeError("Read failed")
            with self.assertRaises(RuntimeError):
                provider.get_available_balance("phone_A")
            self.assertEqual(scheduler.reservations, {})

    def test_provider_accepts_balance_output_when_a_later_step_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            readiness = SimpleNamespace(
                inspect=lambda device_id, reserved=False: {"ready": not reserved}
            )
            scheduler = JobScheduler(
                JobStore(Path(directory) / "jobs.json"), readiness
            )
            storage = Mock()
            storage.read.return_value = {
                "purpose": "balance_check",
                "steps": [
                    {
                        "type": "ocr_value",
                        "parser": "currency",
                        "output": "payer_balance",
                    }
                ]
            }
            service = Mock()
            service.start.return_value = {"run_id": "balance_run"}
            service.wait.return_value = {
                "status": "failed",
                "outputs": {"payer_balance": "RM 616.50"},
            }
            provider = CameraOCRBalanceProvider(
                scheduler, storage, service, lambda _: "balance"
            )

            self.assertEqual(
                provider.get_available_balance("phone_A"), Decimal("616.50")
            )

    def test_provider_revalidates_exact_assigned_workflow_before_execution(self):
        scheduler = Mock()
        scheduler.lock = threading.RLock()
        scheduler.reservations = {}
        scheduler.is_device_ready.return_value = {"ready": True}
        scheduler.next_job_for_device.return_value = None
        storage = Mock()
        storage.read.return_value = {
            "id": "unsafe_balance",
            "purpose": "balance_check",
            "steps": [
                {
                    "type": "ocr_value",
                    "parser": "currency",
                    "output": "payer_balance",
                },
                {"type": "tap", "critical": True},
            ],
        }
        service = Mock()
        provider = CameraOCRBalanceProvider(
            scheduler, storage, service, lambda _: "unsafe_balance"
        )

        with self.assertRaisesRegex(ValueError, "irreversible payment step"):
            provider.get_available_balance("phone_A")
        storage.read.assert_called_once_with("unsafe_balance")
        service.start.assert_not_called()

    def test_queued_jobs_prevent_balance_navigation(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = SimpleNamespace(inspect=lambda device_id, reserved=False: {"ready": not reserved})
            scheduler = JobScheduler(JobStore(Path(directory) / "jobs.json"), ready)
            scheduler.enqueue(SimpleNamespace(account_id="account_a", device_id="phone_A"), "main", {})
            storage = Mock()
            storage.read.return_value = {"purpose": "balance_check", "steps": [{"type": "ocr_value", "parser": "currency", "output": "payer_balance"}]}
            service = Mock()
            provider = CameraOCRBalanceProvider(scheduler, storage, service, lambda _: "balance")
            with self.assertRaises(RecordConflict):
                provider.get_available_balance("phone_A")
            service.start.assert_not_called()

    def test_manual_recovery_balance_reserves_ahead_of_blocked_retry_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            readiness = SimpleNamespace(
                inspect=lambda device_id, reserved=False: {
                    "device_id": device_id,
                    "ready": not reserved,
                    "busy": reserved,
                    "reasons": ["job_reserved"] if reserved else [],
                }
            )
            scheduler = JobScheduler(
                JobStore(Path(directory) / "jobs.json"), readiness
            )
            scheduler.integration_gate = lambda _: True
            queued = scheduler.enqueue(
                SimpleNamespace(account_id="account_a", device_id="phone_A"),
                "payment",
                {},
                action="withdraw_v2",
            )
            self.assertEqual(queued.status, "WAITING_FOR_DEVICE")

            storage = Mock()
            workflow = {
                "id": "balance",
                "purpose": "balance_check",
                "steps": [
                    {
                        "type": "ocr_value",
                        "parser": "currency",
                        "output": "payer_balance",
                    }
                ],
            }
            storage.read.return_value = workflow
            service = Mock()
            service.start.return_value = {"run_id": "balance_run"}

            def wait(_run_id):
                self.assertIn("phone_A", scheduler.reservations)
                self.assertIsNone(scheduler.claim_next_job_for_device("phone_A"))
                self.assertEqual(
                    scheduler.get_job(queued.job_id).status,
                    "WAITING_FOR_DEVICE",
                )
                return {"status": "passed", "outputs": {"payer_balance": "25.00"}}

            service.wait.side_effect = wait
            provider = CameraOCRBalanceProvider(
                scheduler, storage, service, lambda _: "balance"
            )

            self.assertEqual(
                provider.get_available_balance(
                    "phone_A", manual_recovery=True
                ),
                Decimal("25.00"),
            )
            self.assertEqual(scheduler.reservations, {})

            scheduler.integration_gate = lambda _: False
            resumed = scheduler.claim_next_job_for_device("phone_A")
            self.assertIsNotNone(resumed)
            self.assertEqual(resumed.job_id, queued.job_id)
            self.assertEqual(resumed.status, "RUNNING")
