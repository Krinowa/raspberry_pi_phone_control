"""Static contracts for calibrated dashboard preview mode selection."""

import unittest
from pathlib import Path


class CalibratedPreviewUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1] / "static"
        cls.app_source = (root / "app.js").read_text(encoding="utf-8")
        cls.workflow_source = (root / "workflows.js").read_text(encoding="utf-8")

    def test_cards_use_calibrated_preview_endpoint(self):
        self.assertIn(
            "/camera/preview?t=${Date.now()}",
            self.app_source,
        )

    def test_selected_preview_switches_raw_only_for_calibration(self):
        self.assertIn(
            'requestedCoordinateSpace === "camera_normalized" ? "frame" : "preview"',
            self.app_source,
        )
        self.assertIn('bitmapCoordinateSpace = requestedCoordinateSpace', self.app_source)

    def test_interaction_and_recorder_send_explicit_coordinate_space(self):
        self.assertIn('data.coordinate_space = coordinateSpace', self.app_source)
        self.assertIn('coordinate_space: previewCoordinateSpace()', self.workflow_source)


if __name__ == "__main__":
    unittest.main()
