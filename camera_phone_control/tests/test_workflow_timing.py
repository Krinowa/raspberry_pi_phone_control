"""Focused workflow timing instrumentation tests."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from android_control import AndroidControl, Device
from automation.workflows.models import new_workflow
from automation.workflows.service import WorkflowService
from automation.workflows.timing import WorkflowTiming
from automation.screen.camera_ocr import CameraOCRScreenStateProvider
from device_lock import DeviceLocks


DEVICE = Device("timing-test", 1080, 1920, 0)


def workflow():
    result = new_workflow("Timing")
    result["id"] = "timing"
    result["steps"] = [
        {"id": "tap", "type": "tap", "x": 0.5, "y": 0.5},
        {"id": "wait", "type": "wait", "seconds": 0.01},
    ]
    return result


class WorkflowTimingTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            PHONE_INPUT_BACKEND="appium",
            PHONE_SCREEN_BACKEND="appium",
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.controller = Mock(spec=AndroidControl)
        self.controller.device.return_value = DEVICE
        self.service = WorkflowService(
            self.controller,
            DeviceLocks(Path(self.directory.name) / "locks"),
        )

    def test_result_contains_step_and_summary_timings_without_changing_actions(self):
        result = self.service.start(workflow())
        item = self.service.runs[result["run_id"]]
        item["thread"].join(2)
        result = self.service.snapshot(result["run_id"])

        self.assertEqual(result["status"], "passed")
        self.controller.send.assert_called_once_with(DEVICE, "tap", (540, 960))
        timing = result["timing"]
        self.assertEqual([step["name"] for step in timing["steps"]], ["tap", "wait"])
        self.assertEqual([step["status"] for step in timing["steps"]], ["completed"] * 2)
        self.assertGreaterEqual(timing["steps"][0]["hid_api_seconds"], 0)
        self.assertGreaterEqual(timing["steps"][1]["fixed_sleep_seconds"], 0.009)
        self.assertGreaterEqual(timing["summary"]["total_seconds"], 0.009)
        self.assertEqual(timing["summary"]["retry_count"], 0)
        self.assertIn("Workflow timing total=", "\n".join(result["logs"]))

    def test_ocr_metadata_reports_cache_and_repeated_frames_without_text(self):
        timing = WorkflowTiming()
        timing.start()
        timing.begin_step(1, "wait_for_text")
        timing.record(
            "ocr_call",
            0,
            event={"frame_id": 42, "cache_hit": False, "worker_pid": 100},
        )
        timing.record(
            "ocr_call",
            0,
            event={"frame_id": 42, "cache_hit": True, "worker_pid": 100},
        )
        timing.end_step()
        timing.finish()

        result = timing.snapshot()
        self.assertEqual(result["summary"]["ocr_call_count"], 2)
        self.assertEqual(result["summary"]["ocr_inference_count"], 1)
        self.assertEqual(result["summary"]["ocr_cache_hit_count"], 1)
        self.assertEqual(result["summary"]["repeated_ocr_frame_count"], 1)
        self.assertEqual(result["summary"]["ocr_skipped_count"], 0)
        self.assertFalse(result["steps"][0]["ocr_calls"][0]["repeated_frame"])
        self.assertTrue(result["steps"][0]["ocr_calls"][1]["repeated_frame"])
        self.assertNotIn("text", result["steps"][0]["ocr_calls"][0])

    def test_skipped_ocr_poll_is_counted_separately(self):
        timing = WorkflowTiming()
        timing.start()
        timing.begin_step(1, "wait_for_text")
        timing.record("ocr_skip", 0, count=1, event={"reason": "unchanged"})
        timing.end_step()
        timing.finish()
        self.assertEqual(timing.snapshot()["summary"]["ocr_skipped_count"], 1)

    def test_ocr_trigger_reasons_are_counted(self):
        timing = WorkflowTiming()
        timing.start()
        timing.begin_step(1, "wait_for_text")
        for reason in ("forced", "forced", "changed_and_settled", "no_baseline"):
            timing.record("ocr_trigger", 0, count=1, event={"reason": reason})
        timing.end_step()
        timing.finish()
        summary = timing.snapshot()["summary"]
        self.assertEqual(summary["ocr_forced_count"], 2)
        self.assertEqual(summary["ocr_change_count"], 1)
        self.assertEqual(summary["ocr_initial_count"], 1)

    def test_camera_ocr_contributes_worker_components_and_cache_metadata(self):
        worker = Mock(pid=321)
        worker.request.return_value = (
            [{"res": {"rec_texts": ["Ready"], "rec_scores": [0.95],
                       "rec_boxes": [[0, 0, 10, 10]]}}],
            {"worker_start": 0.01, "ipc_send": 0.002, "ipc_wait": 0.03,
             "inference": 0.02, "worker_pid": 321},
        )
        image = __import__("numpy").zeros((300, 180), dtype="uint8")
        session = Mock()
        session.capture_for_ocr.return_value = (image, 77, 0.0)
        screen = Mock()
        screen.session.return_value = session
        with patch(
            "automation.screen.camera_ocr._PaddleWorkerClient",
            return_value=worker,
        ):
            provider = CameraOCRScreenStateProvider(screen)

        timing = WorkflowTiming()
        timing.start()
        timing.begin_step(1, "wait_for_text")
        self.assertTrue(provider.contains_text("Ready"))
        self.assertTrue(provider.contains_text("Ready"))
        timing.end_step()
        timing.finish()

        step = timing.snapshot()["steps"][0]
        self.assertEqual(step["ocr_call_count"], 2)
        self.assertEqual(step["ocr_inference_count"], 1)
        self.assertEqual(step["ocr_cache_hit_count"], 1)
        self.assertEqual(step["repeated_ocr_frame_count"], 1)
        self.assertGreaterEqual(step["ocr_inference_seconds"], 0.02)
        self.assertAlmostEqual(step["ocr_ipc_seconds"], 0.042)
        self.assertGreaterEqual(step["ocr_queue_seconds"], 0)
        self.assertGreater(step["ocr_matching_seconds"], 0)
        self.assertEqual(step["ocr_calls"][0]["ocr_input_width"], 180)
        self.assertEqual(step["ocr_calls"][0]["ocr_input_height"], 300)
        self.assertEqual(step["ocr_calls"][0]["region_count"], 1)
        self.assertAlmostEqual(step["ocr_calls"][0]["inference_seconds"], 0.02)
        self.assertAlmostEqual(step["ocr_calls"][0]["ipc_wait_seconds"], 0.03)
        self.assertGreaterEqual(step["ocr_calls"][0]["queue_wait_seconds"], 0)
        worker.request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
