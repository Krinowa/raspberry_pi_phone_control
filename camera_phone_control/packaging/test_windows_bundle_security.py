"""Fail-closed checks for sensitive files in the Windows distribution."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PACKAGING = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGING))

from validate_windows_bundle import _forbidden_bundle_inputs  # noqa: E402


class WindowsBundleSecurityTests(unittest.TestCase):
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
