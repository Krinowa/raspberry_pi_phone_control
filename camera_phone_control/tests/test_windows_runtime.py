"""Packaging path behavior without requiring a Windows host."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from windows_runtime import RuntimePaths, default_data_root


class WindowsRuntimePathsTests(unittest.TestCase):
    def _resources(self, root):
        resources = root / "resources"
        workflows = resources / "automation" / "workflow_data"
        defaults = resources / "packaging" / "defaults"
        workflows.mkdir(parents=True)
        defaults.mkdir(parents=True)
        (workflows / "flow_one.json").write_text(
            json.dumps({"id": "flow_one"}), encoding="utf-8"
        )
        (defaults / "cameras.json").write_text(
            json.dumps({"phone_A": {"camera_source": 0}}), encoding="utf-8"
        )
        return resources

    def test_explicit_data_root_contains_all_writable_runtime_categories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = self._resources(root)
            with patch("windows_runtime.resource_root", return_value=resources):
                paths = RuntimePaths.create(root / "client-data")
            for directory in (
                paths.workflows,
                paths.camera,
                paths.jobs,
                paths.receipts,
                paths.logs,
                paths.config,
                paths.locks,
                paths.cache,
            ):
                self.assertTrue(directory.is_dir())
                self.assertTrue(directory.is_relative_to(paths.data))
            self.assertEqual(paths.ocr_models, resources / "paddle_models")

    def test_bundled_defaults_seed_only_missing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = self._resources(root)
            data = root / "client-data"
            with patch("windows_runtime.resource_root", return_value=resources):
                first = RuntimePaths.create(data)
                (first.workflows / "flow_one.json").write_text(
                    '{"client":"custom"}', encoding="utf-8"
                )
                (first.camera / "cameras.json").write_text(
                    '{"client":"calibrated"}', encoding="utf-8"
                )
                RuntimePaths.create(data)
            self.assertEqual(
                (data / "workflows" / "flow_one.json").read_text(encoding="utf-8"),
                '{"client":"custom"}',
            )
            self.assertEqual(
                (data / "camera" / "cameras.json").read_text(encoding="utf-8"),
                '{"client":"calibrated"}',
            )

    def test_environment_override_selects_client_data_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            expected = Path(temporary).resolve()
            self.assertEqual(
                default_data_root({"PHONE_CONTROLLER_DATA_DIR": str(expected)}),
                expected,
            )


if __name__ == "__main__":
    unittest.main()
