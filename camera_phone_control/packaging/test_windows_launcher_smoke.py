"""Regression coverage for hardware-free frozen application validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from windows_launcher import (
    _PackageSmokeCamera,
    _close_application,
    _create_application,
)
from windows_runtime import RuntimePaths


class FrozenLauncherSmokeTests(unittest.TestCase):
    def test_smoke_application_uses_no_capture_thread_and_closes_cleanly(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = RuntimePaths.create(Path(temporary) / "data")
            app, manager = _create_application(paths, package_smoke=True)
            camera = manager.cameras["phone_A"]
            remote = app.extensions["remote_control"]
            try:
                self.assertIsInstance(camera, _PackageSmokeCamera)
                self.assertFalse(hasattr(camera, "thread"))
                self.assertIsNone(app.extensions["job_executor"].thread)
                self.assertEqual(
                    remote.history.path,
                    paths.jobs / "remote_control_history.jsonl",
                )

                registered = {
                    (rule.rule, method)
                    for rule in app.url_map.iter_rules()
                    for method in rule.methods - {"HEAD", "OPTIONS"}
                }
                self.assertIn(
                    ("/agent/v1/devices/<device_id>/frames", "GET"), registered
                )
                self.assertIn(
                    ("/agent/v1/devices/<device_id>/takeover", "POST"), registered
                )
                self.assertIn(
                    ("/agent/v1/devices/<device_id>/screen", "PUT"), registered
                )

                client = app.test_client()
                responses = (
                    client.get("/", follow_redirects=True),
                    client.get(
                        "/api/status",
                        headers={"X-Control-Client": "package-smoke"},
                    ),
                    client.get("/static/app.js"),
                    client.get("/static/i18n.js"),
                    client.get("/static/style.css"),
                )
                try:
                    self.assertEqual(
                        [response.status_code for response in responses],
                        [200, 200, 200, 200, 200],
                    )
                finally:
                    for response in responses:
                        response.close()
            finally:
                with patch.object(remote, "close", wraps=remote.close) as close_remote:
                    _close_application(app, manager)
                    close_remote.assert_called_once_with()

            self.assertTrue(camera.closed)
            self.assertEqual(manager.cameras, {})


if __name__ == "__main__":
    unittest.main()
