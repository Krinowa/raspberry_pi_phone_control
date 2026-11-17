"""Fail-closed checks for sensitive files in the Windows distribution."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PACKAGING = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGING))

from validate_windows_bundle import (  # noqa: E402
    _forbidden_bundle_inputs,
    _validate_cython_runtime_data,
)


class WindowsBundleSecurityTests(unittest.TestCase):
    def test_cython_runtime_utility_data_is_required_and_nonempty(self):
        with tempfile.TemporaryDirectory() as temporary:
            resources = Path(temporary)
            utility = resources / "Cython" / "Utility" / "CppSupport.cpp"

            with self.assertRaisesRegex(RuntimeError, "runtime utility data"):
                _validate_cython_runtime_data(resources)

            utility.parent.mkdir(parents=True)
            utility.touch()
            with self.assertRaisesRegex(RuntimeError, "runtime utility data"):
                _validate_cython_runtime_data(resources)

            utility.write_text("// Cython runtime template", encoding="utf-8")
            self.assertEqual(_validate_cython_runtime_data(resources), utility)

    def test_remoteviewer_secret_file_is_rejected_from_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            distribution = Path(temporary)
            secret = distribution / "static" / "remoteviewer.secret"
            secret.parent.mkdir()
            secret.write_text("test-only-secret", encoding="utf-8")
            (distribution / "static" / "app.js").write_text(
                "// safe asset", encoding="utf-8"
            )

            self.assertEqual(_forbidden_bundle_inputs(distribution), (secret,))


if __name__ == "__main__":
    unittest.main()
