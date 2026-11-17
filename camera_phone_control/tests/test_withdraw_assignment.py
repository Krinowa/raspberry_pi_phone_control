"""External operator Login & Assign without realtime or payment activity."""

import json
import os
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask, request

from automation.integrations.withdraw_v2.client import APIError
from automation.integrations.withdraw_v2.config import WithdrawCredentialStore
from automation.integrations.withdraw_v2.manager import (
    CANCEL_REASONS,
    WithdrawManager,
    register_withdraw,
)
from automation.jobs.storage import (
    AccountBindingStore,
    DeviceWorkflowAssignmentStore,
    RecordConflict,
)


class FakeClient:
    def __init__(self, origin, result):
        self.origin = origin
        self.result = result
        self.closed = False
        self.login_args = None

    def login(self, username, password, secret, operator_id=None):
        self.login_args = (username, password, secret, operator_id)
        if isinstance(self.result, Exception):
            raise self.result
        self.profile = dict(self.result)
        self.token = self.profile.get("token")
        return dict(self.profile)

    def close(self):
        self.closed = True


class ClientFactory:
    def __init__(self, result):
        self.result = result
        self.clients = []

    def __call__(self, origin):
        client = FakeClient(origin, self.result)
        self.clients.append(client)
        return client


class StartingWorker:
    def __init__(
        self,
        device_id,
        config,
        *,
        client,
        realtime_transport,
        credentials,
        **_kwargs,
    ):
        self.device_id = device_id
        self.config = config
        self.api = config["withdraw_api"]
        self.client = client
        self.realtime_transport = realtime_transport
        self.credentials = credentials
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.started = False
        self.state = "CONNECTING"
        self.reconciliation_complete = False

    def start(self):
        if getattr(self.client, "profile", None) is None:
            password, secret = self.credentials(self.api["credential_reference"])
            self.client.login(
                self.api["username"], password, secret, self.api["operator_id"]
            )
        self.started = True

    def session_available(self):
        return False

    def close(self):
        self.client.close()

    def status(self):
        return {"device_id": self.device_id, "state": "CONNECTING"}


class ProvisioningWorker(StartingWorker):
    def __init__(self, *args, account_binding_provisioner=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.account_binding_provisioner = account_binding_provisioner
        self.account_binding_error = None
        self.start_count = 0

    def start(self):
        super().start()
        self.start_count += 1
        self.state = "IDLE"
        self.reconciliation_complete = True
        self.account_binding_error = self.account_binding_provisioner()

    def set_account_binding_error(self, error):
        self.account_binding_error = error

    def status(self):
        return {
            "device_id": self.device_id,
            "state": self.state,
            "session_state": "READY",
            "physical_socket_connected": True,
            "reconciliation_complete": self.reconciliation_complete,
        }


class ReauthWorker(ProvisioningWorker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.socket = SimpleNamespace(connected=False, session_state="DISCONNECTED")
        self.adopt_count = 0
        self.balance_refresh_reasons = []

    def start(self):
        super().start()
        self.thread = SimpleNamespace(is_alive=lambda: True)
        self.socket = SimpleNamespace(connected=True, session_state="CONNECTED")

    def disconnect(self):
        self.state = "AUTH_FAILED"
        self.reconciliation_complete = False
        self.socket = SimpleNamespace(connected=False, session_state="DISCONNECTED")

    def adopt_authenticated_client(self, client, config):
        self.client.close()
        self.client = client
        self.config = config
        self.api = config["withdraw_api"]
        self.adopt_count += 1
        self.state = "IDLE"
        self.reconciliation_complete = True
        self.socket = SimpleNamespace(connected=True, session_state="CONNECTED")

    def session_available(self):
        return bool(self.reconciliation_complete and self.socket.connected)

    def request_balance_refresh(self, reason="manual"):
        self.balance_refresh_reasons.append(reason)
        return True

    def status(self):
        return {
            "device_id": self.device_id,
            "state": self.state,
            "session_state": self.socket.session_state,
            "physical_socket_connected": self.socket.connected,
            "reconciliation_complete": self.reconciliation_complete,
        }


def profile(operator_id=7, username="bot-operator01", role="operator"):
    return {
        "id": operator_id,
        "username": username,
        "role": role,
        "payment_records": ["5", "2b"],
        "token": "must-not-leak",
        "session_max_expires_at": (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat(),
        "realtime": {"key": "must-not-leak"},
    }


class WithdrawAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.root = root
        self.assignments = DeviceWorkflowAssignmentStore(root / "devices.json")
        self.assignments.set("phone_A", ["flow_payment"])
        self.assignments.set("phone_B", ["flow_payment"])
        self.credentials = WithdrawCredentialStore(root / "credentials.json")
        self.scheduler = SimpleNamespace(
            lock=threading.RLock(),
            reservations={},
            integration_gate=None,
            list_jobs=lambda: [],
        )
        self.service = SimpleNamespace(
            has_active_run=lambda device_id=None: False,
            screenshot_store=SimpleNamespace(root=root / "screens"),
        )
        self.accounts = Mock()
        self.accounts.list.return_value = [
            SimpleNamespace(device_id="phone_A", enabled=True),
            SimpleNamespace(device_id="phone_B", enabled=True),
        ]
        self.workflows = Mock()
        self.workflows.read.side_effect = lambda workflow_id: (
            self.valid_balance_flow()
            if workflow_id == "flow_balance"
            else self.payment_flow()
        )
        self.factory = ClientFactory(profile())
        self.manager = WithdrawManager(
            assignments=self.assignments,
            accounts=self.accounts,
            workflows=self.workflows,
            service=self.service,
            scheduler=self.scheduler,
            executor=Mock(),
            journal_path=root / "journal.json",
            client_factory=self.factory,
            credential_store=self.credentials,
        )
        self.environment = patch.dict(
            os.environ, WITHDRAW_API_BASE_URL="https://withdraw.example"
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def assign(self, device="phone_A"):
        return self.manager.login_and_assign(
            device,
            {
                "username": "typed-username",
                "password": "private-password",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            },
        )

    def use_persistent_accounts(self):
        self.accounts = AccountBindingStore(self.root / "accounts.json")
        self.manager.accounts = self.accounts
        return self.accounts

    @staticmethod
    def local_binding(account_id, device_id="phone_A", enabled=True):
        return {
            "account_id": account_id,
            "display_name": account_id,
            "provider": None,
            "device_id": device_id,
            "enabled": enabled,
        }

    def test_login_assign_creates_missing_compatibility_binding_once(self):
        accounts = self.use_persistent_accounts()
        self.set_balance_workflow("phone_A", "flow_balance")
        self.manager.worker_factory = ProvisioningWorker
        self.manager.enabled = True
        self.manager.dispatcher = Mock()

        self.assign()
        first = accounts.list()
        self.manager._ensure_withdraw_account_binding(
            "phone_A", 7, "bot-operator01"
        )

        self.assertEqual(len(accounts.list()), 1)
        self.assertEqual(first[0].account_id, "withdraw_operator_7")
        self.assertEqual(first[0].display_name, "bot-operator01")
        self.assertEqual(first[0].device_id, "phone_A")
        self.assertTrue(first[0].enabled)
        self.assertEqual(self.manager.payment_readiness("phone_A"), (True, None))

    def test_existing_enabled_device_binding_is_preserved(self):
        accounts = self.use_persistent_accounts()
        existing = accounts.create(self.local_binding("existing_route"))

        result = self.manager._ensure_withdraw_account_binding(
            "phone_A", 7, "bot-operator01"
        )

        self.assertEqual(result, existing)
        self.assertEqual([item.account_id for item in accounts.list()], ["existing_route"])

    def test_stale_withdraw_operator_binding_is_replaced_for_current_operator(self):
        accounts = self.use_persistent_accounts()
        accounts.create(self.local_binding("withdraw_operator_101"))

        result = self.manager._ensure_withdraw_account_binding(
            "phone_A", 1072, "sdktech"
        )

        self.assertEqual(result.account_id, "withdraw_operator_1072")
        self.assertEqual(result.display_name, "sdktech")
        self.assertEqual(
            [item.account_id for item in accounts.list()],
            ["withdraw_operator_1072"],
        )

    def test_unresolved_different_account_identity_blocks_binding_replacement(self):
        accounts = self.use_persistent_accounts()
        accounts.create(self.local_binding("withdraw_operator_101"))
        self.manager.journal.begin(
            "phone_A",
            phase="QUEUED",
            withdraw_id=1,
            tx_id="TX-1",
            operator_id=101,
            account_id="withdraw_operator_101",
        )

        with self.assertRaisesRegex(RecordConflict, "different local account identity"):
            self.manager._ensure_withdraw_account_binding(
                "phone_A", 1072, "sdktech"
            )

        self.assertEqual(
            [item.account_id for item in accounts.list()],
            ["withdraw_operator_101"],
        )

    def test_compatibility_account_conflict_is_not_overwritten(self):
        accounts = self.use_persistent_accounts()
        accounts.create(self.local_binding("withdraw_operator_7", "phone_B"))

        with self.assertRaisesRegex(RecordConflict, "already assigned to phone_B"):
            self.manager._ensure_withdraw_account_binding(
                "phone_A", 7, "bot-operator01"
            )

        binding = accounts.get("withdraw_operator_7")
        self.assertEqual(binding.device_id, "phone_B")

    def test_binding_conflict_does_not_break_successful_login_session(self):
        accounts = self.use_persistent_accounts()
        accounts.create(self.local_binding("withdraw_operator_7", "phone_B"))
        self.set_balance_workflow("phone_A", "flow_balance")
        self.manager.worker_factory = ProvisioningWorker
        self.manager.enabled = True
        self.manager.dispatcher = Mock()

        result = self.assign()

        worker = self.manager.workers["phone_A"]
        self.assertTrue(result["assigned"])
        self.assertEqual(worker.state, "IDLE")
        self.assertEqual(worker.start_count, 1)
        self.assertFalse(worker.client.closed)
        self.assertEqual(
            worker.client.login_args,
            ("typed-username", "private-password", "JBSWY3DPEHPK3PXP", None),
        )
        ready, error = self.manager.payment_readiness("phone_A")
        self.assertFalse(ready)
        self.assertIn("Local payment routing could not be initialized", error)
        self.assertIn("already assigned to phone_B", error)
        self.assertEqual(worker.account_binding_error, error)

    def test_restart_restores_missing_compatibility_binding(self):
        accounts = self.use_persistent_accounts()
        current = self.assignments.integration("phone_A")
        current["withdraw_api"].update(
            enabled=True,
            base_url="https://withdraw.example",
            operator_id=7,
            username="bot-operator01",
            credential_reference=self.credentials.save("password", "secret"),
            payment_records=["5"],
        )
        self.assignments.set_integration("phone_A", current)

        restored = WithdrawManager(
            assignments=self.assignments,
            accounts=accounts,
            workflows=self.workflows,
            service=self.service,
            scheduler=self.scheduler,
            executor=Mock(),
            journal_path=self.root / "restored-journal.json",
            client_factory=ClientFactory(profile()),
            credential_store=self.credentials,
            worker_factory=ProvisioningWorker,
        )
        self.addCleanup(restored.close)
        restored.workers["phone_A"].start()

        binding = accounts.get("withdraw_operator_7")
        self.assertEqual(binding.device_id, "phone_A")
        self.assertTrue(binding.enabled)

    def test_startup_preserves_operator_created_retry_for_fresh_session_handoff(self):
        original = SimpleNamespace(
            action="withdraw_v2",
            status="WAITING_FOR_DEVICE",
            retry_of_job_id=None,
            job_id="job-original",
            device_id="phone_A",
            external_ref="TX-ORIGINAL",
        )
        retry = SimpleNamespace(
            action="withdraw_v2",
            status="WAITING_FOR_DEVICE",
            retry_of_job_id="job-failed",
            job_id="job-retry",
            device_id="phone_B",
            external_ref="TX-RETRY",
        )
        scheduler = SimpleNamespace(
            lock=threading.RLock(),
            reservations={},
            integration_gate=None,
            list_jobs=lambda: [original, retry],
            cancel_job=Mock(),
        )
        assignments = Mock()
        assignments.integrations.return_value = {}
        manager = WithdrawManager(
            assignments=assignments,
            accounts=self.accounts,
            workflows=self.workflows,
            service=self.service,
            scheduler=scheduler,
            executor=Mock(),
            journal_path=self.root / "startup-journal.json",
            credential_store=self.credentials,
        )
        self.addCleanup(manager.close)

        scheduler.cancel_job.assert_called_once_with("job-original")

    def set_balance_workflow(self, device, workflow_id):
        current = self.assignments.integration(device)
        current["balance_workflow_id"] = workflow_id
        self.assignments.set_integration(device, current)

    @staticmethod
    def payment_flow():
        return {
            "purpose": "payment",
            "steps": [
                {"type": "wait_for_text"},
                {"type": "capture_screen", "output_key": "receipt_path"},
            ]
        }

    @staticmethod
    def valid_balance_flow():
        return {
            "purpose": "balance_check",
            "steps": [
                {
                    "type": "ocr_value",
                    "parser": "currency",
                    "output": "payer_balance",
                }
            ]
        }

    def test_success_uses_returned_identity_and_private_credentials(self):
        self.assignments.set("phone_A", ["flow_payment"])
        self.set_balance_workflow("phone_A", "flow_balance")
        result = self.assign()
        self.assertEqual(
            result,
            {
                "assigned": True,
                "operator_id": 7,
                "username": "bot-operator01",
                "payment_records": ["5", "2b"],
                "credentials_saved": True,
            },
        )
        saved = self.assignments.integration("phone_A")
        self.assertEqual(saved["withdraw_api"]["operator_id"], 7)
        self.assertEqual(saved["withdraw_api"]["username"], "bot-operator01")
        self.assertEqual(saved["withdraw_api"]["payment_records"], ["5", "2b"])
        self.assertEqual(saved["balance_workflow_id"], "flow_balance")
        self.assertEqual(self.assignments.get("phone_A"), ("flow_payment",))
        self.assertEqual(
            self.credentials.load(saved["withdraw_api"]["credential_reference"]),
            ("private-password", "JBSWY3DPEHPK3PXP"),
        )
        self.assertEqual(stat.S_IMODE(self.credentials.path.stat().st_mode), 0o600)
        normal_config = self.assignments.path.read_text()
        self.assertNotIn("private-password", normal_config)
        self.assertNotIn("JBSWY3DPEHPK3PXP", normal_config)
        self.assertNotIn("must-not-leak", json.dumps(result))
        self.assertEqual(
            self.factory.clients[0].login_args,
            ("typed-username", "private-password", "JBSWY3DPEHPK3PXP", None),
        )

    def test_login_assign_enables_worker_and_transfers_authenticated_session(self):
        manager = WithdrawManager(
            assignments=self.assignments,
            accounts=self.accounts,
            workflows=self.workflows,
            service=self.service,
            scheduler=self.scheduler,
            executor=Mock(),
            journal_path=Path(self.temporary.name) / "lifecycle-journal.json",
            worker_factory=StartingWorker,
            client_factory=self.factory,
            credential_store=self.credentials,
        )
        manager.enabled = True
        manager.dispatcher = Mock()
        self.addCleanup(manager.close)

        manager.login_and_assign(
            "phone_A",
            {
                "username": "typed-username",
                "password": "private-password",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            },
        )

        worker = manager.workers["phone_A"]
        self.assertTrue(worker.started)
        self.assertTrue(worker.config["withdraw_api"]["enabled"])
        self.assertIs(worker.client, self.factory.clients[-1])
        self.assertFalse(worker.client.closed)
        self.assertIs(worker.realtime_transport, manager.realtime_transport)

    def test_login_assign_allows_incomplete_payment_workflow(self):
        self.workflows.read.side_effect = lambda workflow_id: {
            "id": workflow_id,
            "steps": (
                self.valid_balance_flow()["steps"]
                if workflow_id == "flow_balance"
                else [{"type": "tap"}]
            ),
        }
        manager = WithdrawManager(
            assignments=self.assignments,
            accounts=self.accounts,
            workflows=self.workflows,
            service=self.service,
            scheduler=self.scheduler,
            executor=Mock(),
            journal_path=Path(self.temporary.name) / "incomplete-journal.json",
            worker_factory=StartingWorker,
            client_factory=self.factory,
            credential_store=self.credentials,
        )
        manager.enabled = True
        manager.dispatcher = Mock()
        self.addCleanup(manager.close)

        result = manager.login_and_assign(
            "phone_A",
            {
                "username": "typed-username",
                "password": "private-password",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            },
        )

        self.assertTrue(result["assigned"])
        self.assertTrue(manager.workers["phone_A"].started)
        status = manager.status("phone_A")
        self.assertFalse(status["payment_ready"])
        self.assertEqual(
            status["payment_readiness_error"],
            "Add a Capture Screenshot step after the success check.",
        )
        self.assertEqual(
            status["payment_readiness_error_code"],
            "payment_readiness_failed",
        )
        self.assertEqual(status["payment_readiness_error_params"], {})

    def test_readiness_reloads_latest_saved_and_assigned_workflow(self):
        current = self.assignments.integration("phone_A")
        current["balance_workflow_id"] = "flow_balance"
        self.assignments.set_integration("phone_A", current)
        workflows = {
            "flow_payment": {
                "purpose": "payment",
                "steps": [
                    {"type": "wait_for_text"},
                    {"type": "capture_screen", "output_key": "receipt_path"},
                ]
            },
            "flow_balance": {
                "purpose": "balance_check",
                "steps": [
                    {
                        "type": "ocr_value",
                        "parser": "currency",
                        "output": "payer_balance",
                    }
                ]
            },
            "flow_other": {"purpose": "payment", "steps": [{"type": "tap"}]},
        }
        self.workflows.read.side_effect = lambda workflow_id: workflows[workflow_id]

        self.assertEqual(self.manager.payment_readiness("phone_A"), (True, None))
        workflows["flow_payment"] = {
            "purpose": "payment",
            "steps": [{"type": "tap"}],
        }
        self.assertFalse(self.manager.payment_readiness("phone_A")[0])
        workflows["flow_payment"] = {
            "purpose": "payment",
            "steps": [
                {"type": "wait_for_text"},
                {"type": "capture_screen", "output_key": "receipt_path"},
            ]
        }
        self.assertEqual(self.manager.payment_readiness("phone_A"), (True, None))

        self.assignments.set("phone_A", ["flow_other"])
        self.assertFalse(self.manager.payment_readiness("phone_A")[0])
        self.assignments.set("phone_A", ["flow_payment"])
        self.assertEqual(self.manager.payment_readiness("phone_A"), (True, None))

    def test_balance_readiness_distinguishes_missing_invalid_and_valid(self):
        self.assertEqual(
            self.manager.payment_readiness("phone_A"),
            (False, "Assign a Balance Check Workflow to this device."),
        )

        self.set_balance_workflow("phone_A", "flow_missing")

        def missing(workflow_id):
            if workflow_id == "flow_missing":
                raise FileNotFoundError(workflow_id)
            return self.payment_flow()

        self.workflows.read.side_effect = missing
        self.assertEqual(
            self.manager.payment_readiness("phone_A"),
            (False, "Assigned Balance Check Workflow was not found."),
        )

        self.set_balance_workflow("phone_A", "flow_invalid_balance")
        self.workflows.read.side_effect = lambda workflow_id: (
            {"purpose": "balance_check", "steps": [{"type": "tap"}]}
            if workflow_id == "flow_invalid_balance"
            else self.payment_flow()
        )
        self.assertEqual(
            self.manager.payment_readiness("phone_A"),
            (
                False,
                "This workflow cannot be used as a Balance Check Workflow because it does not contain Read Balance.",
            ),
        )

        self.set_balance_workflow("phone_A", "flow_balance")
        self.workflows.read.side_effect = lambda workflow_id: (
            self.valid_balance_flow()
            if workflow_id == "flow_balance"
            else self.payment_flow()
        )
        self.assertEqual(self.manager.payment_readiness("phone_A"), (True, None))

    def test_balance_assignment_and_edits_refresh_readiness_immediately(self):
        flows = {
            "flow_payment": self.payment_flow(),
            "flow_invalid": {"steps": [{"type": "tap"}]},
            "flow_balance": self.valid_balance_flow(),
        }
        self.workflows.read.side_effect = lambda workflow_id: flows[workflow_id]

        self.set_balance_workflow("phone_A", "flow_invalid")
        self.assertFalse(self.manager.payment_readiness("phone_A")[0])
        self.set_balance_workflow("phone_A", "flow_balance")
        self.assertEqual(self.manager.payment_readiness("phone_A"), (True, None))

        flows["flow_balance"] = {"steps": [{"type": "tap"}]}
        self.assertFalse(self.manager.payment_readiness("phone_A")[0])
        flows["flow_balance"] = self.valid_balance_flow()
        self.assertEqual(self.manager.payment_readiness("phone_A"), (True, None))

    def test_payment_purpose_cannot_also_be_the_balance_workflow(self):
        self.set_balance_workflow("phone_A", "flow_payment")
        combined = self.payment_flow()
        combined["steps"].append(
            {
                "type": "ocr_value",
                "parser": "currency",
                "output": "payer_balance",
            }
        )
        self.workflows.read.side_effect = lambda _workflow_id: combined

        ready, error = self.manager.payment_readiness("phone_A")
        self.assertFalse(ready)
        self.assertIn("Balance Check purpose", error)

    def test_balance_workflow_readiness_is_independent_per_device(self):
        self.set_balance_workflow("phone_A", "flow_balance")
        self.set_balance_workflow("phone_B", "flow_invalid")
        self.workflows.read.side_effect = lambda workflow_id: (
            self.valid_balance_flow()
            if workflow_id == "flow_balance"
            else {"steps": [{"type": "tap"}]}
            if workflow_id == "flow_invalid"
            else self.payment_flow()
        )

        self.assertEqual(self.manager.payment_readiness("phone_A"), (True, None))
        self.assertFalse(self.manager.payment_readiness("phone_B")[0])

    def test_restart_reloads_persisted_balance_workflow_for_readiness(self):
        self.set_balance_workflow("phone_A", "flow_balance")
        restarted_assignments = DeviceWorkflowAssignmentStore(
            self.root / "devices.json"
        )
        self.workflows.read.side_effect = lambda workflow_id: (
            self.valid_balance_flow()
            if workflow_id == "flow_balance"
            else self.payment_flow()
        )
        restarted = WithdrawManager(
            assignments=restarted_assignments,
            accounts=self.accounts,
            workflows=self.workflows,
            service=self.service,
            scheduler=self.scheduler,
            executor=Mock(),
            journal_path=self.root / "restart-readiness-journal.json",
            credential_store=self.credentials,
        )
        self.addCleanup(restarted.close)

        self.assertEqual(
            restarted_assignments.integration("phone_A")["balance_workflow_id"],
            "flow_balance",
        )
        self.assertEqual(restarted.payment_readiness("phone_A"), (True, None))

    def test_restart_restores_only_enabled_assignment_with_stored_credentials(self):
        self.assign("phone_A")
        self.manager.client_factory = ClientFactory(profile(12, "operator-B"))
        self.assign("phone_B")
        disabled = self.assignments.integration("phone_B")
        disabled["withdraw_api"]["enabled"] = False
        self.assignments.set_integration("phone_B", disabled)

        factory = ClientFactory(profile())
        restored = WithdrawManager(
            assignments=self.assignments,
            accounts=self.accounts,
            workflows=self.workflows,
            service=self.service,
            scheduler=self.scheduler,
            executor=Mock(),
            journal_path=Path(self.temporary.name) / "restart-journal.json",
            worker_factory=StartingWorker,
            client_factory=factory,
            credential_store=self.credentials,
            start_workers=True,
        )
        self.addCleanup(restored.close)

        self.assertEqual(set(restored.workers), {"phone_A"})
        worker = restored.workers["phone_A"]
        self.assertTrue(worker.started)
        self.assertEqual(
            worker.client.login_args,
            (
                "bot-operator01",
                "private-password",
                "JBSWY3DPEHPK3PXP",
                7,
            ),
        )

    def test_reconnect_reauthenticates_unavailable_live_worker(self):
        self.set_balance_workflow("phone_A", "flow_balance")
        self.assign("phone_A")
        worker = Mock()
        worker.thread = Mock()
        worker.thread.is_alive.return_value = True
        worker.session_available.return_value = False
        worker.status.return_value = {
            "device_id": "phone_A",
            "state": "AUTH_FAILED",
        }
        self.manager.workers["phone_A"] = worker

        result = self.manager.reconcile("phone_A")

        worker.request_reconnect.assert_called_once_with(
            force_reauthentication=True
        )
        self.assertEqual(result["state"], "AUTH_FAILED")
        self.assertEqual(
            self.assignments.integration("phone_A")["balance_workflow_id"],
            "flow_balance",
        )

    def test_reconnect_rebuilds_missing_worker_from_persistent_assignment(self):
        self.assign("phone_A")
        previous = self.manager.workers.pop("phone_A")
        previous.close()
        self.manager.enabled = True
        self.manager.worker_factory = StartingWorker
        self.manager.dispatcher = Mock()
        factory = ClientFactory(profile())
        self.manager.client_factory = factory

        result = self.manager.reconcile("phone_A")

        worker = self.manager.workers["phone_A"]
        self.assertTrue(worker.started)
        self.assertEqual(worker.client.login_args[-1], 7)
        self.assertEqual(result["state"], "CONNECTING")

    def test_reconnect_rebuilds_worker_when_payment_workflow_is_incomplete(self):
        self.assign("phone_A")
        previous = self.manager.workers.pop("phone_A")
        previous.close()
        self.workflows.read.side_effect = lambda workflow_id: (
            self.valid_balance_flow()
            if workflow_id == "flow_balance"
            else {"steps": [{"type": "tap"}]}
        )
        self.manager.enabled = True
        self.manager.worker_factory = StartingWorker
        self.manager.dispatcher = Mock()
        self.manager.client_factory = ClientFactory(profile())

        result = self.manager.reconcile("phone_A")

        self.assertTrue(self.manager.workers["phone_A"].started)
        self.assertEqual(result["state"], "CONNECTING")
        self.assertFalse(result["payment_ready"])
        self.assertIn("Capture Screenshot", result["payment_readiness_error"])

    def test_reconnect_uses_existing_healthy_session_for_reconciliation(self):
        self.assign("phone_A")
        worker = Mock()
        worker.thread = Mock()
        worker.thread.is_alive.return_value = True
        worker.session_available.return_value = True
        worker.status.return_value = {"device_id": "phone_A", "state": "IDLE"}
        self.manager.workers["phone_A"] = worker

        self.manager.reconcile("phone_A")

        worker.request_reconnect.assert_called_once_with(
            force_reauthentication=False
        )

    def test_invalid_profiles_and_safe_login_errors_do_not_assign(self):
        cases = [
            (profile(role="parent"), "not authorized"),
            (profile(operator_id=None), "not authorized"),
            (APIError(401), "Invalid username or password"),
            (ValueError("bad secret"), "Unable to verify Google 2FA"),
        ]
        for response, message in cases:
            with self.subTest(response=response):
                self.manager.client_factory = ClientFactory(response)
                with self.assertRaisesRegex(ValueError, message):
                    self.assign()
                self.assertFalse(
                    self.assignments.integration("phone_A")["withdraw_api"].get(
                        "operator_id"
                    )
                )

    def test_failed_change_and_unresolved_state_preserve_old_assignment(self):
        self.assign()
        before = self.assignments.integration("phone_A")
        old_reference = before["withdraw_api"]["credential_reference"]

        self.manager.client_factory = ClientFactory(APIError(401))
        with self.assertRaisesRegex(ValueError, "Invalid username"):
            self.assign()
        self.assertEqual(self.assignments.integration("phone_A"), before)
        self.assertEqual(
            self.credentials.load(old_reference),
            ("private-password", "JBSWY3DPEHPK3PXP"),
        )

        self.manager.journal.update("phone_A", phase="NEEDS_ATTENTION")
        self.manager.client_factory = ClientFactory(profile(12, "replacement"))
        with self.assertRaisesRegex(RecordConflict, "unresolved withdrawal"):
            self.assign()
        self.assertEqual(self.assignments.integration("phone_A"), before)

        self.manager.journal.update("phone_A", phase="RELEASED")
        changed = self.assign()
        self.assertEqual(changed["operator_id"], 12)
        self.assertEqual(
            self.assignments.integration("phone_A")["withdraw_api"]["operator_id"],
            12,
        )

    def test_same_account_reauthentication_bypasses_unresolved_change_guard(self):
        self.set_balance_workflow("phone_A", "flow_balance")
        self.manager.worker_factory = ReauthWorker
        self.manager.enabled = True
        self.manager.dispatcher = Mock()
        self.assign()
        worker = self.manager.workers["phone_A"]
        worker.disconnect()
        self.manager.journal.update(
            "phone_A",
            phase="NEEDS_ATTENTION",
            withdraw_id=935402,
            tx_id="TX-935402",
            job_id="job-failed",
            error="Payment workflow failed.",
        )
        journal_before = dict(self.manager.journal.current("phone_A"))
        assignment_before = self.assignments.integration("phone_A")
        old_reference = assignment_before["withdraw_api"]["credential_reference"]
        self.manager.client_factory = ClientFactory(profile(7, "bot-operator01"))

        result = self.manager.login_and_assign(
            "phone_A",
            {
                "username": "bot-operator01",
                "password": "updated-password",
                "totp_secret": "UPDATEDSECRET",
            },
        )

        assignment_after = self.assignments.integration("phone_A")
        self.assertEqual(result["operator_id"], 7)
        self.assertIs(self.manager.workers["phone_A"], worker)
        self.assertEqual(worker.adopt_count, 1)
        self.assertTrue(worker.session_available())
        self.assertEqual(worker.socket.session_state, "CONNECTED")
        self.assertEqual(assignment_after["withdraw_api"]["operator_id"], 7)
        self.assertEqual(
            assignment_after["withdraw_api"]["base_url"],
            assignment_before["withdraw_api"]["base_url"],
        )
        self.assertNotEqual(
            assignment_after["withdraw_api"]["credential_reference"], old_reference
        )
        self.assertEqual(
            self.credentials.load(
                assignment_after["withdraw_api"]["credential_reference"]
            ),
            ("updated-password", "UPDATEDSECRET"),
        )
        self.assertEqual(self.manager.journal.current("phone_A"), journal_before)
        self.assertEqual(
            [device for device in self.manager.workers if device == "phone_A"],
            ["phone_A"],
        )

        refreshed = self.manager.refresh_balance("phone_A")
        self.assertEqual(worker.balance_refresh_reasons, ["manual"])
        self.assertEqual(refreshed["session_state"], "CONNECTED")

    def test_different_account_remains_blocked_while_unresolved(self):
        self.manager.worker_factory = ReauthWorker
        self.manager.enabled = True
        self.manager.dispatcher = Mock()
        self.assign()
        before = self.assignments.integration("phone_A")
        worker = self.manager.workers["phone_A"]
        self.manager.journal.update(
            "phone_A",
            phase="NEEDS_ATTENTION",
            withdraw_id=935402,
            tx_id="TX-935402",
        )
        self.manager.client_factory = ClientFactory(profile(12, "operator-B"))

        with self.assertRaisesRegex(
            RecordConflict,
            "unresolved withdrawal activity",
        ):
            self.assign()

        self.assertEqual(self.assignments.integration("phone_A"), before)
        self.assertIs(self.manager.workers["phone_A"], worker)
        self.assertEqual(worker.adopt_count, 0)
        self.assertTrue(self.manager.client_factory.clients[0].closed)

    def test_phone_assignments_are_independent(self):
        self.assign("phone_A")
        self.manager.client_factory = ClientFactory(profile(12, "operator-B"))
        self.assign("phone_B")
        self.assertEqual(
            self.assignments.integration("phone_A")["withdraw_api"]["operator_id"],
            7,
        )
        self.assertEqual(
            self.assignments.integration("phone_B")["withdraw_api"]["operator_id"],
            12,
        )

    def test_manual_cancel_reason_codes_are_mapped_server_side(self):
        worker = Mock()
        worker.status.return_value = {"phase": "CANCELLED", "withdraw_id": 935402}
        self.manager.workers["phone_A"] = worker
        for code, reason in CANCEL_REASONS.items():
            with self.subTest(code=code):
                result = self.manager.cancel_withdrawal("phone_A", code)
                self.assertEqual(result["phase"], "CANCELLED")
                worker.cancel_withdrawal.assert_called_with(
                    reason,
                )
        with self.assertRaisesRegex(ValueError, "valid cancellation reason"):
            self.manager.cancel_withdrawal("phone_A", "raw frontend reason")

    def test_manual_balance_refresh_rejects_missing_or_unsafe_assignment(self):
        worker = Mock()
        worker.thread = Mock()
        worker.thread.is_alive.return_value = True
        worker.session_available.return_value = True
        worker.status.return_value = {
            "balance_state": "VALID",
            "balance_valid": True,
            "last_balance": "999.00",
            "balance_observed_at": "2026-10-08T01:02:03+00:00",
        }
        self.manager.workers["phone_A"] = worker
        current = self.assignments.integration("phone_A")
        current["withdraw_api"].update(
            enabled=True,
            operator_id=7,
            username="operator",
            credential_reference="env:WITHDRAW_TEST",
            base_url="https://withdraw.example",
        )
        self.assignments.set_integration("phone_A", current)

        with self.assertRaisesRegex(RecordConflict, "Balance Check Workflow"):
            self.manager.refresh_balance("phone_A")
        worker.request_balance_refresh.assert_not_called()
        missing_status = self.manager.status("phone_A")
        self.assertEqual(missing_status["balance_state"], "UNKNOWN")
        self.assertFalse(missing_status["balance_valid"])
        self.assertIsNone(missing_status["last_balance"])

        self.set_balance_workflow("phone_A", "unsafe_balance")
        self.workflows.read.side_effect = lambda workflow_id: {
            "purpose": "balance_check",
            "steps": [
                {
                    "type": "ocr_value",
                    "parser": "currency",
                    "output": "payer_balance",
                },
                {"type": "tap", "critical": True},
            ]
        }
        with self.assertRaisesRegex(RecordConflict, "irreversible payment step"):
            self.manager.refresh_balance("phone_A")
        worker.request_balance_refresh.assert_not_called()

    def test_assignment_and_config_endpoints_return_safe_metadata_only(self):
        self.set_balance_workflow("phone_A", "flow_balance")
        app = Flask(__name__)
        app.extensions.update(
            device_workflow_assignment_store=self.assignments,
            account_binding_store=self.accounts,
            workflow_storage=self.workflows,
            workflow_service=self.service,
            job_scheduler=self.scheduler,
            job_executor=Mock(),
            job_store=SimpleNamespace(
                path=Path(self.temporary.name) / "endpoint-jobs.json"
            ),
        )
        manager = register_withdraw(
            app,
            lambda: request.get_json(),
            admin_required=lambda view: view,
            ensure_registered_device=lambda device_id: None,
        )
        manager.client_factory = ClientFactory(profile())
        client = app.test_client()
        response = client.post(
            "/api/devices/phone_A/withdraw/login-assign",
            json={
                "username": "typed-username",
                "password": "private-password",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            },
        )
        self.assertEqual(response.status_code, 200)
        status = client.get("/api/devices/phone_A/withdraw")
        self.assertEqual(status.status_code, 200)
        serialized = json.dumps({"assign": response.json, "status": status.json})
        for forbidden in (
            "private-password",
            "JBSWY3DPEHPK3PXP",
            "must-not-leak",
            "credential_reference",
            "realtime",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(status.json["withdraw_api"]["operator_id"], 7)

        worker = Mock()
        worker.status.return_value = {
            "device_id": "phone_A",
            "balance_state": "VALID",
            "balance_valid": True,
            "last_balance": "1234.56",
            "balance_observed_at": "2026-10-08T01:02:03+00:00",
        }
        manager.workers["phone_A"] = worker
        balance_status = client.get("/api/devices/phone_A/withdraw")
        self.assertEqual(balance_status.status_code, 200)
        self.assertEqual(balance_status.json["status"]["balance_state"], "VALID")
        self.assertTrue(balance_status.json["status"]["balance_valid"])
        self.assertEqual(balance_status.json["status"]["last_balance"], "1234.56")
        self.assertEqual(
            balance_status.json["status"]["balance_observed_at"],
            "2026-10-08T01:02:03+00:00",
        )

        worker.thread = Mock()
        worker.thread.is_alive.return_value = True
        worker.session_available.return_value = True
        refreshed = client.post(
            "/api/devices/phone_A/withdraw/balance/refresh", json={}
        )
        self.assertEqual(refreshed.status_code, 202)
        worker.request_balance_refresh.assert_called_once_with("manual")

        worker.status.return_value = {
            "device_id": "phone_A",
            "phase": "CANCELLED",
            "withdraw_id": 935402,
        }
        manager.workers["phone_A"] = worker
        cancelled = client.post(
            "/api/devices/phone_A/withdraw/cancel",
            json={"reason": "bank_rejected"},
        )
        self.assertEqual(cancelled.status_code, 200)
        worker.cancel_withdrawal.assert_called_once_with(
            "bank rejected payment",
        )
        self.assertEqual(cancelled.json["phase"], "CANCELLED")

        worker.cancel_withdrawal.reset_mock()
        compatible = client.post(
            "/api/devices/phone_A/withdraw/cancel",
            json={"reason_code": "invalid_account"},
        )
        self.assertEqual(compatible.status_code, 200)
        worker.cancel_withdrawal.assert_called_once_with(
            "invalid account number",
        )

    def test_status_exposes_runtime_authentication_separately_from_assignment(self):
        configured = self.assignments.integration("phone_A")
        configured["withdraw_api"].update(
            enabled=True,
            base_url="https://withdraw.example",
            operator_id=7,
            username="bot-operator01",
            credential_reference=None,
        )
        self.assignments.set_integration("phone_A", configured)

        self.manager.workers.pop("phone_A", None)
        self.assertFalse(self.manager.status("phone_A")["authentication_available"])

        worker = Mock()
        worker.status.return_value = {
            "device_id": "phone_A",
            "state": "WAITING_FOR_CONNECTION",
        }
        worker.authentication_available.return_value = True
        self.manager.workers["phone_A"] = worker
        self.assertTrue(self.manager.status("phone_A")["authentication_available"])

        worker.authentication_available.return_value = False
        worker.status.return_value["state"] = "AUTH_FAILED"
        self.assertFalse(self.manager.status("phone_A")["authentication_available"])

    def test_login_again_is_pinned_to_existing_operator_and_stores_no_otp(self):
        accounts = self.use_persistent_accounts()
        initial = self.manager.login_and_assign(
            "phone_A",
            {
                "username": "typed-username",
                "password": "private-password",
                "g2f_code": "123456",
            },
        )
        self.assertEqual(initial["operator_id"], 7)
        self.assertEqual(accounts.get("withdraw_operator_7").device_id, "phone_A")
        before = self.assignments.integration("phone_A")
        self.assertIsNone(before["withdraw_api"]["credential_reference"])
        self.assertFalse(self.credentials.path.exists())

        self.manager.client_factory = ClientFactory(profile(12, "other-operator"))
        with self.assertRaisesRegex(RecordConflict, "does not match"):
            self.manager.login_and_assign(
                "phone_A",
                {
                    "username": "bot-operator01",
                    "password": "fresh-password",
                    "g2f_code": "654321",
                    "reauthenticate": True,
                },
            )
        self.assertEqual(self.assignments.integration("phone_A"), before)

        self.manager.client_factory = ClientFactory(profile(7, "bot-operator01"))
        result = self.manager.login_and_assign(
            "phone_A",
            {
                "username": "bot-operator01",
                "password": "fresh-password",
                "g2f_code": "654321",
                "reauthenticate": True,
            },
        )
        after = self.assignments.integration("phone_A")
        self.assertEqual(result["operator_id"], 7)
        self.assertEqual(after["withdraw_api"]["operator_id"], 7)
        self.assertEqual(after["withdraw_api"]["base_url"], before["withdraw_api"]["base_url"])
        self.assertEqual(accounts.get("withdraw_operator_7").device_id, "phone_A")
        self.assertEqual(len(accounts.list()), 1)
        self.assertIsNone(after["withdraw_api"]["credential_reference"])
        self.assertFalse(self.credentials.path.exists())

    def test_reconnect_requires_existing_authentication_and_never_logs_in(self):
        configured = self.assignments.integration("phone_A")
        configured["withdraw_api"].update(
            enabled=True,
            base_url="https://withdraw.example",
            operator_id=7,
            username="bot-operator01",
            credential_reference=None,
        )
        self.assignments.set_integration("phone_A", configured)
        worker = Mock()
        worker.thread = Mock()
        worker.thread.is_alive.return_value = True
        worker.authentication_available.return_value = False
        self.manager.workers["phone_A"] = worker
        factory_calls = len(self.factory.clients)

        with self.assertRaisesRegex(RecordConflict, "fresh 6-digit"):
            self.manager.reconcile("phone_A")

        worker.request_reconnect.assert_not_called()
        self.assertEqual(len(self.factory.clients), factory_calls)


if __name__ == "__main__":
    unittest.main()
