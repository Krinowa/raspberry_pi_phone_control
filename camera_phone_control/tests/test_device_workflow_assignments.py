"""Persistent per-device workflow authorization and routing tests."""

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from android_control import AndroidControl
from app import create_app
from automation.jobs.storage import (
    AccountBindingStore,
    DeviceWorkflowAssignmentStore,
    JobStore,
)
from automation.workflows.models import new_workflow
from automation.workflows.storage import WorkflowStorage
from device_lock import DeviceLocks
from hardware.pi_api_client import PiApiClient
from test_control import authenticate_admin


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
        return b"jpeg", 1, 1280, 720

    def observation(self):
        return b"jpeg", 1, 1280, 720, time.monotonic()


class DeviceWorkflowAssignmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assignments = DeviceWorkflowAssignmentStore(
            self.root / "device_workflows.json"
        )
        self.accounts = AccountBindingStore(self.root / "accounts.json")
        self.jobs = JobStore(self.root / "jobs.json")
        self.workflows = WorkflowStorage(self.root / "workflows")
        self.testing = self._workflow("Testing")
        self.maybank = self._workflow("Maybank")
        self.uob = self._workflow("UOB")
        self.balance = self._balance_workflow("Check Balance")
        self.remote_text = self._remote_text_workflow("Remote Account Input")

    def _workflow(self, name):
        workflow = new_workflow(name)
        workflow["steps"] = [
            {"id": "wait", "type": "wait", "seconds": 0.01}
        ]
        return self.workflows.save(workflow, create=True)

    def _balance_workflow(self, name, *, critical=False):
        workflow = new_workflow(name, "balance_check")
        workflow["steps"] = [
            {"id": "tap", "type": "tap", "x": 0.5, "y": 0.5},
            {
                "id": "read",
                "type": "ocr_value",
                "parser": "currency",
                "output": "payer_balance",
                "roi": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.2},
            },
            {
                "id": "shot",
                "type": "capture_screen",
                "output_key": "receipt_path",
                **({"critical": True} if critical else {}),
            },
        ]
        return self.workflows.save(workflow, create=True)

    def _remote_text_workflow(self, name):
        workflow = new_workflow(name, "remote_text")
        workflow["variables"] = [
            {
                "name": "account_number",
                "label": "Account Number",
                "type": "text",
                "required": True,
                "default": None,
            }
        ]
        workflow["keyboards"] = {
            "account_keys": {
                "id": "account_keys",
                "name": "Account entry keypad",
                "kind": "numeric",
                "keys": {
                    str(value): {"x": value / 10, "y": 0.7}
                    for value in range(10)
                },
            }
        }
        workflow["steps"] = [
            {
                "id": "account_input",
                "type": "input",
                "source": "variable",
                "variable": "account_number",
                "input_method": "screen_keyboard",
                "keyboard_id": "account_keys",
            }
        ]
        return self.workflows.save(workflow, create=True)

    def _app(self):
        pi = Mock(spec=PiApiClient)
        pi.base_url = "http://pi.test:8766"
        pi.device_id = "phone_A"
        pi.devices.return_value = {
            "ok": True,
            "default_device": "phone_A",
            "devices": [
                {"id": "phone_A", "name": "Phone A", "connected": True},
                {"id": "phone_B", "name": "Phone B", "connected": True},
            ],
            "enrollment": {"active": False},
        }
        pi.status.return_value = {"ok": True, "connected": True}
        app = create_app(
            FakeCamera(),
            Mock(spec=AndroidControl),
            DeviceLocks(self.root / "locks"),
            self.workflows,
            manual_backend="raspberry_pi",
            pi_client=pi,
            account_store=self.accounts,
            job_store=self.jobs,
            device_workflow_store=self.assignments,
        )
        self.addCleanup(app.extensions["job_executor"].close)
        return app

    @staticmethod
    def _binding(account_id, device_id):
        return {
            "account_id": account_id,
            "display_name": account_id,
            "provider": "Test",
            "device_id": device_id,
            "enabled": True,
        }

    @staticmethod
    def _headers(app):
        return {
            "X-Control-Token": app.config["CONTROL_TOKEN"],
            "X-Control-Client": "workflow-assignment-test",
        }

    def test_one_multiple_reload_and_legacy_unrestricted(self):
        self.assertIsNone(self.assignments.get("phone_A"))
        self.assertTrue(self.assignments.allows("phone_A", self.maybank["id"]))

        self.assignments.set("phone_A", [self.testing["id"]])
        self.assignments.set(
            "phone_B", [self.testing["id"], self.uob["id"]]
        )
        restarted = DeviceWorkflowAssignmentStore(
            self.root / "device_workflows.json"
        )

        self.assertEqual(restarted.get("phone_A"), (self.testing["id"],))
        self.assertEqual(
            restarted.get("phone_B"), (self.testing["id"], self.uob["id"])
        )
        self.assertIsNone(restarted.get("legacy_phone"))

    def test_explicit_empty_blocks_while_two_devices_can_share_workflow(self):
        self.assignments.set("phone_A", [])
        self.assertFalse(self.assignments.allows("phone_A", self.testing["id"]))
        self.assignments.set("phone_A", [self.testing["id"]])
        self.assignments.set("phone_B", [self.testing["id"]])
        self.assertTrue(self.assignments.allows("phone_A", self.testing["id"]))
        self.assertTrue(self.assignments.allows("phone_B", self.testing["id"]))

    def test_work_readiness_defaults_on_for_legacy_and_persists_explicit_value(self):
        self.assertTrue(self.assignments.ready_for_work("legacy_phone"))

        self.assignments.set_ready_for_work("phone_A", False)
        restarted = DeviceWorkflowAssignmentStore(
            self.root / "device_workflows.json"
        )

        self.assertFalse(restarted.ready_for_work("phone_A"))
        self.assertIsNone(restarted.get("phone_A"))

    def test_readiness_only_record_survives_other_workflow_assignment_updates(self):
        self.assignments.set_ready_for_work("phone_B", False)
        self.assignments.set("phone_A", [self.testing["id"]])

        restarted = DeviceWorkflowAssignmentStore(
            self.root / "device_workflows.json"
        )
        self.assertFalse(restarted.ready_for_work("phone_B"))
        self.assertIsNone(restarted.get("phone_B"))

    def test_main_and_balance_routes_persist_together_across_restart(self):
        self.assignments.set_routes(
            "phone_A",
            [self.testing["id"]],
            self.uob["id"],
            self.remote_text["id"],
        )
        self.assignments.set_routes(
            "phone_B", [self.maybank["id"]], self.testing["id"]
        )

        restarted = DeviceWorkflowAssignmentStore(
            self.root / "device_workflows.json"
        )

        self.assertEqual(restarted.get("phone_A"), (self.testing["id"],))
        self.assertEqual(
            restarted.integration("phone_A")["balance_workflow_id"],
            self.uob["id"],
        )
        self.assertEqual(
            restarted.integration("phone_B")["balance_workflow_id"],
            self.testing["id"],
        )
        self.assertEqual(
            restarted.remote_text_workflow("phone_A"), self.remote_text["id"]
        )

    def test_api_assigns_only_a_single_captured_keyboard_input_for_remote_paste(self):
        app = self._app()
        client = app.test_client()
        authenticate_admin(client)
        headers = self._headers(app)

        saved = client.put(
            "/api/devices/phone_A/workflows",
            json={
                "workflow_ids": [self.testing["id"]],
                "balance_workflow_id": self.balance["id"],
                "remote_text_workflow_id": self.remote_text["id"],
            },
            headers=headers,
        )
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertEqual(
            saved.json["remote_text_workflow_id"], self.remote_text["id"]
        )
        self.assertTrue(saved.json["remote_text_workflow"]["remote_text_eligible"])

        unsafe_edit = self.workflows.read(self.remote_text["id"])
        unsafe_edit["steps"].append(
            {"id": "unsafe_tap", "type": "tap", "x": 0.5, "y": 0.5}
        )
        edit_response = client.put(
            f"/api/workflows/{self.remote_text['id']}",
            json=unsafe_edit,
            headers=headers,
        )
        self.assertEqual(edit_response.status_code, 400, edit_response.json)
        self.assertIn("assigned as the Remote Paste Workflow", edit_response.json["error"])

        rejected = client.put(
            "/api/devices/phone_A/workflows",
            json={
                "workflow_ids": [self.testing["id"]],
                "remote_text_workflow_id": self.testing["id"],
            },
            headers=headers,
        )
        self.assertEqual(rejected.status_code, 400, rejected.json)
        self.assertIn("Remote Paste purpose", rejected.json["error"])

    def test_legacy_json_without_device_entry_loads_safely(self):
        path = self.root / "device_workflows.json"
        path.write_text(
            json.dumps(
                {
                    "phone_B": {
                        "allowed_workflow_ids": [self.testing["id"]]
                    }
                }
            )
        )
        store = DeviceWorkflowAssignmentStore(path)
        self.assertIsNone(store.get("phone_A"))

    def test_api_rejects_unknown_assignment_and_exposes_deleted_stale_id(self):
        app = self._app()
        client = app.test_client()
        authenticate_admin(client)
        headers = self._headers(app)

        unknown = client.put(
            "/api/devices/phone_A/workflows",
            json={"workflow_ids": ["flow_missing"]},
            headers=headers,
        )
        self.assertEqual(unknown.status_code, 400, unknown.json)

        saved = client.put(
            "/api/devices/phone_A/workflows",
            json={
                "workflow_ids": [self.testing["id"]],
                "balance_workflow_id": self.balance["id"],
            },
            headers=headers,
        )
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertEqual(saved.json["balance_workflow_id"], self.balance["id"])
        self.assertEqual(
            DeviceWorkflowAssignmentStore(
                self.root / "device_workflows.json"
            ).integration("phone_A")["balance_workflow_id"],
            self.balance["id"],
        )
        self.workflows.delete(self.testing["id"])
        stale = client.get("/api/devices/phone_A/workflows")
        self.assertEqual(stale.status_code, 200, stale.json)
        self.assertEqual(stale.json["missing_workflow_ids"], [self.testing["id"]])
        self.assertTrue(stale.json["assigned_workflows"][0]["missing"])

        cleared = client.put(
            "/api/devices/phone_A/workflows",
            json={"workflow_ids": [], "balance_workflow_id": None},
            headers=headers,
        )
        self.assertEqual(cleared.status_code, 200, cleared.json)
        self.assertEqual(cleared.json["allowed_workflow_ids"], [])

    def test_balance_check_assignment_fails_closed_for_missing_read_or_critical(self):
        critical = self._balance_workflow("Unsafe Balance", critical=True)
        missing_read_workflow = new_workflow("Missing Read", "balance_check")
        missing_read_workflow["steps"] = [
            {"id": "tap", "type": "tap", "x": 0.5, "y": 0.5}
        ]
        missing_read_workflow = self.workflows.save(
            missing_read_workflow, create=True
        )
        app = self._app()
        client = app.test_client()
        authenticate_admin(client)
        headers = self._headers(app)

        payment_as_balance = client.put(
            "/api/devices/phone_A/workflows",
            json={
                "workflow_ids": [self.testing["id"]],
                "balance_workflow_id": self.testing["id"],
            },
            headers=headers,
        )
        self.assertEqual(payment_as_balance.status_code, 400)
        self.assertIn("Balance Check purpose", payment_as_balance.json["error"])

        balance_as_payment = client.put(
            "/api/devices/phone_A/workflows",
            json={"workflow_ids": [self.balance["id"]]},
            headers=headers,
        )
        self.assertEqual(balance_as_payment.status_code, 400)
        self.assertIn("Payment purpose", balance_as_payment.json["error"])

        missing_read = client.put(
            "/api/devices/phone_A/workflows",
            json={
                "workflow_ids": [self.testing["id"]],
                "balance_workflow_id": missing_read_workflow["id"],
            },
            headers=headers,
        )
        self.assertEqual(missing_read.status_code, 400, missing_read.json)
        self.assertIn("does not contain Read Balance", missing_read.json["error"])

        irreversible = client.put(
            "/api/devices/phone_A/workflows",
            json={
                "workflow_ids": [self.testing["id"]],
                "balance_workflow_id": critical["id"],
            },
            headers=headers,
        )
        self.assertEqual(irreversible.status_code, 400, irreversible.json)
        self.assertIn("irreversible payment step", irreversible.json["error"])

        valid = client.put(
            "/api/devices/phone_A/workflows",
            json={
                "workflow_ids": [self.testing["id"]],
                "balance_workflow_id": self.balance["id"],
            },
            headers=headers,
        )
        self.assertEqual(valid.status_code, 200, valid.json)
        eligibility = {
            item["id"]: item["balance_check_eligible"]
            for item in valid.json["available_workflows"]
        }
        self.assertTrue(eligibility[self.balance["id"]])
        self.assertFalse(eligibility[self.testing["id"]])
        self.assertFalse(eligibility[critical["id"]])

    def test_assigned_workflow_purpose_change_is_rejected(self):
        self.assignments.set_routes(
            "phone_A", [self.testing["id"]], self.balance["id"]
        )
        app = self._app()
        client = app.test_client()
        headers = self._headers(app)

        payment_change = client.put(
            f"/api/workflows/{self.testing['id']}",
            json={**self.workflows.read(self.testing["id"]), "purpose": "balance_check"},
            headers=headers,
        )
        self.assertEqual(payment_change.status_code, 400, payment_change.json)
        self.assertIn("assigned as the Payment Workflow", payment_change.json["error"])

        balance_change = client.put(
            f"/api/workflows/{self.balance['id']}",
            json={**self.workflows.read(self.balance["id"]), "purpose": "payment"},
            headers=headers,
        )
        self.assertEqual(balance_change.status_code, 400, balance_change.json)
        self.assertIn(
            "assigned as the Balance Check Workflow", balance_change.json["error"]
        )

    def test_startup_clears_balance_purpose_from_legacy_payment_route(self):
        self.assignments.set_routes(
            "phone_A", [self.balance["id"]], self.balance["id"]
        )

        self._app()

        self.assertEqual(self.assignments.get("phone_A"), ())
        self.assertEqual(
            self.assignments.integration("phone_A")["balance_workflow_id"],
            self.balance["id"],
        )

    def test_job_acceptance_rejection_and_no_cross_device_reroute(self):
        self.accounts.create(self._binding("account_A", "phone_A"))
        self.assignments.set("phone_A", [self.testing["id"]])
        self.assignments.set("phone_B", [self.maybank["id"]])
        app = self._app()
        client = app.test_client()
        headers = self._headers(app)

        accepted = client.post(
            "/api/jobs",
            json={
                "account_id": "account_A",
                "workflow": self.testing["id"],
                "payload": {},
            },
            headers=headers,
        )
        self.assertEqual(accepted.status_code, 202, accepted.json)
        self.assertEqual(accepted.json["device_id"], "phone_A")
        persisted_count = len(self.jobs.list())

        rejected = client.post(
            "/api/jobs",
            json={
                "account_id": "account_A",
                "workflow": self.maybank["id"],
                "payload": {},
            },
            headers=headers,
        )
        self.assertEqual(rejected.status_code, 409, rejected.json)
        self.assertEqual(
            rejected.json["error"],
            "Workflow is not assigned to device phone_A.",
        )
        self.assertEqual(len(self.jobs.list()), persisted_count)
        self.assertTrue(all(job.device_id == "phone_A" for job in self.jobs.list()))

    def test_assignment_updates_do_not_mutate_queued_or_running_jobs(self):
        binding = self.accounts.create(self._binding("account_A", "phone_A"))
        app = self._app()
        scheduler = app.extensions["job_scheduler"]
        queued = scheduler.enqueue(binding, self.testing["id"], {})
        running = replace(
            queued,
            status="RUNNING",
            started_at="already-running",
        )
        self.jobs.replace(running)

        self.assignments.set("phone_A", [self.maybank["id"]])

        unchanged = self.jobs.get(queued.job_id)
        self.assertEqual(unchanged, running)
        self.assertEqual(unchanged.device_id, "phone_A")
        self.assertEqual(unchanged.workflow, self.testing["id"])

    def test_dashboard_run_rejects_workflow_not_assigned_to_selected_phone(self):
        self.assignments.set("phone_A", [self.uob["id"]])
        app = self._app()
        client = app.test_client()
        response = client.post(
            f"/api/workflows/{self.testing['id']}/run",
            json={"variables": {}},
            headers=self._headers(app),
        )
        self.assertEqual(response.status_code, 400, response.json)
        self.assertEqual(
            response.json["error"],
            "Workflow is not assigned to device phone_A.",
        )


if __name__ == "__main__":
    unittest.main()
