"""Local dashboard access without the retired Admin login flow."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import DEVELOPER_MODE_KEY_ENV, create_app
from automation.screen.ocr_settings import OCRSettingsStorage
from automation.workflows.storage import WorkflowStorage
from test_control import FakeCamera


class DashboardAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.developer_key = "ocean"
        environment = patch.dict(
            os.environ, {DEVELOPER_MODE_KEY_ENV: self.developer_key}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.app = self.create_dashboard()
        self.client = self.app.test_client()

    def create_dashboard(self):
        root = Path(self.temporary.name)
        return create_app(
            FakeCamera(),
            workflow_storage=WorkflowStorage(root / "workflows"),
            ocr_settings_storage=OCRSettingsStorage(root / "ocr-settings.json"),
            ocr_resource_provider=lambda: {
                "physical_cpu_cores": 4,
                "logical_cpu_threads": 8,
                "physical_cpu_fallback": False,
                "total_ram_bytes": 16 * 1024**3,
                "available_ram_bytes": 8 * 1024**3,
                "current_system_cpu_percent": 0.0,
                "current_system_ram_percent": 50.0,
            },
            manual_backend="appium",
        )

    @property
    def headers(self):
        return {
            "X-Control-Token": self.app.config["CONTROL_TOKEN"],
            "X-Control-Client": "dashboard-access-test",
        }

    def test_dashboard_loads_without_admin_account_or_session(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="dashboard-operate"', response.data)
        self.assertIn(b'id="dashboard-admin"', response.data)
        self.assertNotIn(b"Admin login", response.data)
        self.assertNotIn(b"Logout", response.data)
        self.assertNotIn("auth_user_store", self.app.extensions)

        for path in (
            "/setup",
            "/setup/verify",
            "/login",
            "/login/2fa",
            "/login/recovery",
            "/api/auth/session",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_admin_configuration_uses_control_token_not_login_session(self):
        rejected = self.client.put("/api/ocr/settings", json={"worker_pool_size": 1})
        self.assertEqual(rejected.status_code, 403)
        self.assertIn("Invalid control token", rejected.json["error"])
        self.assertEqual(rejected.json["error_code"], "invalid_control_token")
        self.assertEqual(rejected.json["error_params"], {})

        accepted = self.client.put(
            "/api/ocr/settings",
            json={"worker_pool_size": 1},
            headers=self.headers,
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json["worker_pool_size"], 1)

    def test_developer_panels_and_logs_remain_gated_by_hidden_key(self):
        normal = self.client.get("/")
        self.assertIn(b'data-developer-tools="disabled"', normal.data)
        self.assertIn(b'class="card jobs-card admin-only developer-only"', normal.data)
        self.assertEqual(self.client.get("/api/diagnostics/logs").status_code, 403)

        enabled = self.client.get("/?key=" + self.developer_key)
        self.assertEqual(enabled.status_code, 302)
        self.assertEqual(enabled.location, "/")
        developer = self.client.get("/")
        self.assertIn(b'data-developer-tools="enabled"', developer.data)
        self.assertEqual(self.client.get("/api/diagnostics/logs").status_code, 200)

    def test_missing_developer_key_keeps_feature_disabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(DEVELOPER_MODE_KEY_ENV, None)
            app = self.create_dashboard()
        client = app.test_client()

        self.assertFalse(app.config["DEVELOPER_MODE_CONFIGURED"])
        self.assertEqual(client.get("/?key=anything").status_code, 200)
        self.assertIn(b'data-developer-tools="disabled"', client.get("/").data)
        self.assertEqual(client.get("/api/diagnostics/logs").status_code, 403)

    def test_incorrect_developer_key_is_rejected(self):
        self.assertTrue(self.app.config["DEVELOPER_MODE_CONFIGURED"])
        self.assertEqual(self.client.get("/?key=incorrect-key").status_code, 200)
        self.assertIn(b'data-developer-tools="disabled"', self.client.get("/").data)
        self.assertEqual(self.client.get("/api/diagnostics/logs").status_code, 403)

    def test_original_developer_key_works_when_configured(self):
        with patch.dict(os.environ, {DEVELOPER_MODE_KEY_ENV: "ocean"}):
            app = self.create_dashboard()
        client = app.test_client()

        self.assertTrue(app.config["DEVELOPER_MODE_CONFIGURED"])
        self.assertEqual(client.get("/?key=ocean").status_code, 302)
        self.assertIn(b'data-developer-tools="enabled"', client.get("/").data)
        self.assertEqual(client.get("/api/diagnostics/logs").status_code, 200)

    def test_existing_developer_session_remains_enabled(self):
        with self.client.session_transaction() as developer_session:
            developer_session["developer_mode"] = True

        self.assertIn(b'data-developer-tools="enabled"', self.client.get("/").data)
        self.assertEqual(self.client.get("/api/diagnostics/logs").status_code, 200)


if __name__ == "__main__":
    unittest.main()
