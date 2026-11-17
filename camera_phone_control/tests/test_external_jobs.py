"""Account routing and passive persistent device-queue contracts."""

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

from android_control import AndroidControl
from app import create_app
from automation.jobs.models import Job
from automation.jobs.integration import (
    ExternalIntegrationService,
    JobRoute,
    NormalizedJobRequest,
)
from automation.jobs.service import JobScheduler
from automation.jobs.storage import (
    AccountBindingStore,
    DeviceWorkflowAssignmentStore,
    JobStore,
    RecordConflict,
    RecordNotFound,
)
from automation.workflows.models import new_workflow
from automation.workflows.service import WorkflowService
from automation.workflows.storage import WorkflowStorage
from device_lock import DeviceLocks
from test_control import authenticate_admin
from hardware.pi_api_client import PiApiClient


class ReadyDevices:
    def __init__(self, ready=None):
        self.ready = set(ready or ())

    def inspect(self, device_id, *, reserved=False):
        ready = device_id in self.ready and not reserved
        return {
            "device_id": device_id,
            "registered": True,
            "connected": True,
            "camera_configured": True,
            "camera_available": True,
            "runtime_healthy": True,
            "busy": reserved,
            "ready": ready,
            "reasons": ["job_reserved"] if reserved else [],
        }


class FakeCamera:
    source = 0
    rotate = 0

    def start(self):
        return None

    def close(self):
        return None

    def status(self):
        return {
            "ready": True,
            "index": 0,
            "source": "0",
            "width": 1280,
            "height": 720,
            "error": "",
        }

    def frame(self):
        return b"jpeg", 123, 1280, 720

    def observation(self):
        return b"jpeg", 123, 1280, 720, time.monotonic()


class AccountAndJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.accounts = AccountBindingStore(self.root / "accounts.json")
        self.jobs = JobStore(self.root / "jobs.json")

    @staticmethod
    def binding(account_id="uob_account_1", device_id="phone_A", enabled=True):
        return {
            "account_id": account_id,
            "display_name": "UOB Account 1",
            "provider": "UOB",
            "device_id": device_id,
            "enabled": enabled,
        }

    def test_account_create_update_delete_uniqueness_and_resolution(self):
        created = self.accounts.create(self.binding())
        self.assertEqual(created.device_id, "phone_A")
        self.assertEqual(self.accounts.get("uob_account_1").provider, "UOB")
        with self.assertRaises(RecordConflict):
            self.accounts.create(self.binding())
        with self.assertRaises(RecordConflict):
            self.accounts.create(self.binding("other_account", "phone_A"))

        updated = self.accounts.update(
            "uob_account_1", {"display_name": "Primary UOB", "device_id": "phone_B"}
        )
        self.assertEqual(updated.display_name, "Primary UOB")
        self.assertEqual(updated.device_id, "phone_B")
        restarted = AccountBindingStore(self.root / "accounts.json")
        self.assertEqual(restarted.get("uob_account_1").device_id, "phone_B")
        self.accounts.delete("uob_account_1")
        with self.assertRaises(RecordNotFound):
            self.accounts.get("uob_account_1")

    def test_per_device_order_reservation_and_no_reassignment(self):
        account_a = self.accounts.create(self.binding())
        account_b = self.accounts.create(self.binding("uob_account_2", "phone_B"))
        scheduler = JobScheduler(self.jobs, ReadyDevices({"phone_A", "phone_B"}))
        first = scheduler.enqueue(account_a, "demo_flow", {})
        second = scheduler.enqueue(account_a, "demo_flow", {})
        independent = scheduler.enqueue(account_b, "demo_flow", {})

        self.assertEqual(scheduler.queue_position(first), 1)
        self.assertEqual(scheduler.queue_position(second), 2)
        running_a = scheduler.reserve_device("phone_A", first.job_id)
        self.assertEqual(running_a.status, "RUNNING")
        cancelling = scheduler.cancel_job(first.job_id)
        self.assertEqual(cancelling.status, "RUNNING")
        self.assertTrue(cancelling.cancel_requested)
        with self.assertRaises(RecordConflict):
            scheduler.reserve_device("phone_A", second.job_id)
        running_b = scheduler.reserve_device("phone_B", independent.job_id)
        self.assertEqual(running_b.status, "RUNNING")
        self.assertEqual(second.device_id, "phone_A")
        with self.assertRaises(ValueError):
            scheduler.reserve_device("phone_B", second.job_id)

    def test_waiting_cancel_and_restart_recovery_never_replay(self):
        account = self.accounts.create(self.binding())
        scheduler = JobScheduler(self.jobs, ReadyDevices())
        waiting = scheduler.enqueue(account, "demo_flow", {})
        self.assertEqual(waiting.status, "WAITING_FOR_DEVICE")
        cancelled = scheduler.cancel_job(waiting.job_id)
        self.assertEqual(cancelled.status, "CANCELLED")

        running = Job.create(
            account.account_id, account.device_id, "demo_flow", {}, "QUEUED"
        )
        self.jobs.create(replace(running, status="RUNNING", started_at="earlier"))
        JobScheduler(self.jobs, ReadyDevices({"phone_A"}))
        recovered = self.jobs.get(running.job_id)
        self.assertEqual(recovered.status, "INTERRUPTED")
        self.assertIn("not replayed", recovered.error)

    def test_concurrent_reservations_allow_only_one_running_job_per_device(self):
        account = self.accounts.create(self.binding())
        scheduler = JobScheduler(self.jobs, ReadyDevices({"phone_A"}))
        first = scheduler.enqueue(account, "demo_flow", {})
        second = scheduler.enqueue(account, "demo_flow", {})
        barrier = threading.Barrier(3)
        outcomes = []

        def reserve(job_id):
            barrier.wait()
            try:
                scheduler.reserve_device("phone_A", job_id)
                outcomes.append("running")
            except RecordConflict:
                outcomes.append("waiting")

        threads = [
            threading.Thread(target=reserve, args=(first.job_id,)),
            threading.Thread(target=reserve, args=(second.job_id,)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(1)

        self.assertEqual(sorted(outcomes), ["running", "waiting"])
        self.assertEqual(
            len([job for job in self.jobs.list() if job.status == "RUNNING"]), 1
        )

    def test_workflow_job_entry_point_uses_captured_device_not_dashboard_selection(self):
        service = WorkflowService(Mock(), Mock(), device_id="phone_B")
        workflow = {"id": "demo_flow"}
        job = SimpleNamespace(
            workflow="demo_flow",
            payload={"value": "safe-test"},
            device_id="phone_A",
            start_step_index=0,
        )
        service.start = Mock(return_value={"run_id": "run_1"})

        service.start_for_job(job, workflow)

        service.start.assert_called_once_with(
            workflow, job.payload, device_id="phone_A", start_step_index=0
        )

    def test_swappable_integration_adapters_leave_routing_and_execution_unchanged(self):
        account = self.accounts.create(self.binding())
        workflow_store = WorkflowStorage(self.root / "integration-workflows")
        workflow = new_workflow("External adapter demo")
        workflow["variables"] = [
            {
                "name": "value",
                "label": "Value",
                "type": "text",
                "required": True,
                "default": "safe",
                "ask_when_running": False,
            }
        ]
        workflow["steps"] = [
            {"id": "wait", "type": "wait", "seconds": 0.01}
        ]
        workflow_store.save(workflow, create=True)
        scheduler = JobScheduler(self.jobs, ReadyDevices({"phone_A"}))

        class FirstAdapter:
            def normalize(self, raw):
                return NormalizedJobRequest.create(
                    external_ref=raw.get("reference"),
                    bank="Example Bank",
                    action="balance_check",
                    payload={"value": raw["value"]},
                )

        class SecondAdapter:
            def normalize(self, raw):
                return NormalizedJobRequest.create(
                    external_ref=raw[0],
                    bank="Example Bank",
                    action="balance_check",
                    payload={"value": raw[1]},
                )

        class RouteResolver:
            def __init__(self):
                self.requests = []

            def resolve(self, request):
                self.requests.append(request)
                return JobRoute.create(
                    account_id=account.account_id,
                    workflow=workflow["id"],
                )

        resolver = RouteResolver()

        def integration(adapter):
            return ExternalIntegrationService(
                adapter,
                resolver,
                self.accounts,
                workflow_store,
                scheduler,
            )

        first = integration(FirstAdapter()).submit(
            {"reference": "external-one", "value": "safe"}
        )
        second = integration(SecondAdapter()).submit(("external-two", "safe"))

        for job, reference in (
            (first, "external-one"),
            (second, "external-two"),
        ):
            self.assertEqual(job.external_ref, reference)
            self.assertEqual(job.bank, "Example Bank")
            self.assertEqual(job.action, "balance_check")
            self.assertEqual(job.device_id, "phone_A")
            self.assertEqual(job.workflow, workflow["id"])
            self.assertEqual(job.payload, {"value": "safe"})

        workflow_service = WorkflowService(Mock(), Mock(), device_id="phone_B")
        workflow_service.start = Mock(return_value={"run_id": "run_1"})
        workflow_service.start_for_job(first, workflow)
        workflow_service.start_for_job(second, workflow)
        self.assertEqual(
            workflow_service.start.call_args_list,
            [
                call(
                    workflow,
                    {"value": "safe"},
                    device_id="phone_A",
                    start_step_index=0,
                ),
                call(
                    workflow,
                    {"value": "safe"},
                    device_id="phone_A",
                    start_step_index=0,
                ),
            ],
        )
        self.assertEqual(len(resolver.requests), 2)

    def test_legacy_job_records_load_with_nullable_external_metadata(self):
        job = Job.create(
            "uob_account_1", "phone_A", "demo_flow", {}, "QUEUED"
        )
        stored = job.stored()
        for field in (
            "external_ref",
            "bank",
            "action",
            "recovery_workflow_id",
            "recovery_status",
            "recovery_run_id",
            "recovery_error",
            "recovered_at",
            "failed_step_index",
            "failed_step_id",
            "retry_of_job_id",
            "start_step_index",
        ):
            stored.pop(field)
        self.jobs._write({job.job_id: stored})

        loaded = self.jobs.get(job.job_id)

        self.assertIsNone(loaded.external_ref)
        self.assertIsNone(loaded.bank)
        self.assertIsNone(loaded.action)
        self.assertIsNone(loaded.failed_step_index)
        self.assertIsNone(loaded.retry_of_job_id)
        self.assertEqual(loaded.start_step_index, 0)

    def test_api_resolves_account_once_accepts_202_and_cancels(self):
        workflow_store = WorkflowStorage(self.root / "workflows")
        workflow = new_workflow("External demo")
        workflow["steps"] = [
            {"id": "step_wait", "type": "wait", "seconds": 0.1}
        ]
        workflow_store.save(workflow, create=True)
        pi = Mock(spec=PiApiClient)
        pi.base_url = "http://pi.test:8766"
        pi.device_id = "phone_B"  # Dashboard selection must not route the job.
        pi.devices.return_value = {
            "ok": True,
            "default_device": "phone_A",
            "devices": [
                {"id": "phone_A", "connected": True},
                {"id": "phone_B", "connected": True},
            ],
            "enrollment": {"active": False},
        }
        pi.status.return_value = {"ok": True, "connected": True}
        app = create_app(
            FakeCamera(),
            Mock(spec=AndroidControl),
            DeviceLocks(self.root / "locks"),
            workflow_store,
            manual_backend="raspberry_pi",
            pi_client=pi,
            account_store=self.accounts,
            job_store=self.jobs,
            device_workflow_store=DeviceWorkflowAssignmentStore(
                self.root / "device_workflows.json"
            ),
        )
        self.addCleanup(app.extensions["job_executor"].close)
        client = app.test_client()
        authenticate_admin(client)
        headers = {
            "X-Control-Token": app.config["CONTROL_TOKEN"],
            "X-Control-Client": "external-test-client",
        }
        created_account = client.post(
            "/api/accounts", json=self.binding(), headers=headers
        )
        self.assertEqual(created_account.status_code, 201, created_account.json)
        resolved = client.get("/api/accounts/uob_account_1/device")
        self.assertEqual(resolved.status_code, 200, resolved.json)
        self.assertEqual(resolved.json["device_id"], "phone_A")

        started = time.monotonic()
        response = client.post(
            "/api/jobs",
            json={"account_id": "uob_account_1", "workflow": workflow["id"], "payload": {}},
            headers=headers,
        )
        self.assertEqual(response.status_code, 202, response.json)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(response.json["device_id"], "phone_A")
        self.assertNotIn("payload", response.json)
        job_id = response.json["job_id"]
        self.assertEqual(self.jobs.get(job_id).device_id, "phone_A")
        self.assertEqual(client.get(f"/api/jobs/{job_id}").status_code, 200)
        cancelled = client.post(
            f"/api/jobs/{job_id}/cancel", json={}, headers=headers
        )
        self.assertEqual(cancelled.json["status"], "CANCELLED")

        original = self.jobs.replace(
            replace(
                self.jobs.get(job_id),
                status="FAILED",
                recovery_status="READY",
                failed_step_index=0,
                failed_step_id="step_wait",
            )
        )
        retried = client.post(
            f"/api/jobs/{job_id}/retry",
            json={"mode": "failed_step"},
            headers=headers,
        )
        self.assertEqual(retried.status_code, 202, retried.json)
        self.assertNotEqual(retried.json["job_id"], original.job_id)
        self.assertEqual(retried.json["retry_of_job_id"], original.job_id)
        self.assertEqual(retried.json["start_step_index"], 0)
        self.assertEqual(retried.json["device_id"], original.device_id)
        self.assertEqual(self.jobs.get(original.job_id).status, "FAILED")

        disabled = client.patch(
            "/api/accounts/uob_account_1", json={"enabled": False}, headers=headers
        )
        self.assertEqual(disabled.status_code, 200, disabled.json)
        self.assertEqual(
            client.post(
                "/api/jobs",
                json={"account_id": "uob_account_1", "workflow": workflow["id"], "payload": {}},
                headers=headers,
            ).status_code,
            409,
        )

        self.assertEqual(
            client.post(
                "/api/jobs",
                json={"account_id": "missing", "workflow": workflow["id"], "payload": {}},
                headers=headers,
            ).status_code,
            404,
        )
        client.patch(
            "/api/accounts/uob_account_1", json={"enabled": True}, headers=headers
        )
        self.assertEqual(
            client.post(
                "/api/jobs",
                json={"account_id": "uob_account_1", "workflow": "missing_flow", "payload": {}},
                headers=headers,
            ).status_code,
            404,
        )

if __name__ == "__main__":
    unittest.main()
