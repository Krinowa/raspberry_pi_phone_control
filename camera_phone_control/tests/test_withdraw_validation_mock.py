"""Real localhost mock proof for session lifecycle vs payment readiness."""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pyotp

from werkzeug.serving import make_server

MOCK_ROOT = Path(__file__).resolve().parents[2] / "mock_withdraw_api"
sys.path.insert(0, str(MOCK_ROOT))

from app import create_app  # noqa: E402
from config import PASSWORD, TOTP_SECRET  # noqa: E402
from realtime import RealtimeServer  # noqa: E402
from state import MockState  # noqa: E402

from automation.integrations.withdraw_v2.config import WithdrawCredentialStore
from automation.integrations.withdraw_v2.manager import WithdrawManager
from automation.jobs.storage import AccountBindingStore, DeviceWorkflowAssignmentStore


class Scheduler:
    def __init__(self):
        self.lock = threading.RLock()
        self.reservations = {}
        self.integration_gate = None

    def list_jobs(self):
        return []

    def is_device_ready(self, _device_id):
        return {"ready": True}

    def next_job_for_device(self, _device_id):
        return None


class Workflows:
    def __init__(self):
        self.valid_payment = False

    def read(self, workflow_id):
        if workflow_id == "flow_payment":
            if self.valid_payment:
                return {
                    "id": workflow_id,
                    "purpose": "payment",
                    "variables": [],
                    "steps": [
                        {"type": "wait_for_text", "text": "Successful"},
                        {
                            "type": "capture_screen",
                            "output_key": "receipt_path",
                        },
                    ],
                }
            return {"id": workflow_id, "steps": [{"type": "tap"}]}
        return {
            "id": workflow_id,
            "purpose": "balance_check",
            "steps": [
                {
                    "type": "ocr_value",
                    "parser": "currency",
                    "output": "payer_balance",
                }
            ],
        }


class WithdrawValidationMockTests(unittest.TestCase):
    def test_invalid_payment_workflow_keeps_real_session_but_never_locks(self):
        environment = patch.dict(
            os.environ,
            {
                "WITHDRAW_API_BASE_URL": "http://127.0.0.1:19210",
                "WITHDRAW_ALLOW_INSECURE_LOCALHOST": "1",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        state = MockState(ws_port=19211)
        realtime = RealtimeServer(state, port=19211)
        app = create_app(state, realtime)
        server = make_server("127.0.0.1", 19210, app, threaded=True)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        realtime.start()
        server_thread.start()

        def stop_mock():
            server.shutdown()
            server_thread.join(3)
            realtime.close()

        self.addCleanup(stop_mock)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignments = DeviceWorkflowAssignmentStore(root / "devices.json")
            assignments.set("phone_A", ["flow_payment"])
            current = assignments.integration("phone_A")
            current["balance_workflow_id"] = "flow_balance"
            current["withdraw_api"]["base_url"] = "http://127.0.0.1:19210"
            assignments.set_integration("phone_A", current)
            scheduler = Scheduler()
            workflows = Workflows()
            manager = WithdrawManager(
                assignments=assignments,
                accounts=AccountBindingStore(root / "accounts.json"),
                workflows=workflows,
                service=SimpleNamespace(
                    has_active_run=lambda device_id=None: False,
                    screenshot_store=SimpleNamespace(root=root / "screens"),
                ),
                scheduler=scheduler,
                executor=SimpleNamespace(wake=lambda: None),
                journal_path=root / "journal.json",
                credential_store=WithdrawCredentialStore(root / "credentials.json"),
                start_workers=True,
            )
            self.addCleanup(manager.close)

            assigned = manager.login_and_assign(
                "phone_A",
                {
                    "username": "operator_a",
                    "password": PASSWORD,
                    "g2f_code": pyotp.TOTP(TOTP_SECRET).now(),
                },
            )
            self.assertTrue(assigned["assigned"])

            deadline = time.monotonic() + 5
            status = manager.status("phone_A")
            while time.monotonic() < deadline and not (
                status["session_state"] == "READY"
                and status["reconciliation_complete"]
                and any(
                    action["event"] == "client-pending"
                    for action in state.snapshot()["recent_client_actions"]
                )
            ):
                time.sleep(0.02)
                status = manager.status("phone_A")

            snapshot = state.snapshot()
            actions = [item["event"] for item in snapshot["recent_client_actions"]]
            self.assertEqual(status["session_state"], "READY")
            self.assertTrue(status["reconciliation_complete"])
            self.assertFalse(status["payment_ready"])
            self.assertIn("Screenshot step", status["payment_readiness_error"])
            self.assertIn("client-my-locked", actions, snapshot)
            self.assertIn("client-pending", actions)
            self.assertNotIn("client-lock", actions)
            self.assertEqual(snapshot["physical_websocket_count"], 1)

            workflows.valid_payment = True
            ready_status = manager.status("phone_A")
            self.assertTrue(ready_status["payment_ready"])
            self.assertIsNone(ready_status["payment_readiness_error"])


if __name__ == "__main__":
    unittest.main()
