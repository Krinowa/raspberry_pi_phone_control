"""Regression coverage for hardware-free frozen application validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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
            try:
                self.assertIsInstance(camera, _PackageSmokeCamera)
                self.assertFalse(hasattr(camera, "thread"))
                self.assertIsNone(app.extensions["job_executor"].thread)

                client = app.test_client()
                responses = (
                    client.get("/", follow_redirects=True),
                    client.get(
                        "/api/status",
                        headers={"X-Control-Client": "package-smoke"},
                    ),
                    client.get("/static/app.js"),
                )
                try:
                    self.assertEqual(
                        [response.status_code for response in responses],
                        [200, 200, 200],
                    )
                finally:
                    for response in responses:
                        response.close()
            finally:
                _close_application(app, manager)

            self.assertTrue(camera.closed)
            self.assertEqual(manager.cameras, {})


if __name__ == "__main__":
    unittest.main()
