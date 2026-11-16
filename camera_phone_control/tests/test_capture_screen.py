"""Hardware-free coverage for calibrated workflow screenshot artifacts."""

import logging
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from android_control import AndroidControl, Device
from automation.context import AutomationContext
from automation.jobs.executor import BackgroundJobExecutor
from automation.jobs.service import JobScheduler
from automation.jobs.storage import JobStore
from automation.screen.state import ScreenStateError
from automation.workflows.models import new_workflow
from automation.workflows.recorder import Recorder
from automation.workflows.runner import WorkflowRunner
from automation.workflows.screenshots import WorkflowScreenshotStore
from automation.workflows.service import WorkflowService
from automation.workflows.storage import WorkflowStorage
from automation.workflows.validation import validate_recovery_workflow, validate_workflow
from device_lock import DeviceLocks


DEVICE = Device("screenshot-test", 1080, 1920, 0)


def workflow(*steps):
    result = new_workflow("Screenshot workflow")
    result["id"] = "screenshot_flow"
    result["steps"] = list(steps)
    return result


class CalibratedScreen:
    def __init__(self, image=None, error=None):
        if image is None:
            image = np.zeros((40, 20, 3), np.uint8)
            image[:, :, 0] = 25
            image[:, :, 1] = 110
            image[:, :, 2] = 220
        self.image = image
        self.raw_frame = np.full((200, 300, 3), 240, np.uint8)
        self.error = error
        self.capture_calls = 0
        self.ocr_calls = 0

    def session(self):
        parent = self

        class Session:
            def capture_calibrated_color(self):
                parent.capture_calls += 1
                if parent.error:
                    raise parent.error
                return parent.image.copy(), 7, 1.0

            def capture_for_ocr(self):
                parent.ocr_calls += 1
                return cv2.cvtColor(parent.image, cv2.COLOR_BGR2GRAY), 7, 1.0

        return Session()


class CaptureScreenTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            PHONE_INPUT_BACKEND="appium",
            PHONE_SCREEN_BACKEND="appium",
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.controller = Mock(spec=AndroidControl)
        self.controller.device.return_value = DEVICE
        self.locks = DeviceLocks(self.root / "locks")

    def test_validation_recorder_storage_edit_delete_and_reorder(self):
        capture = {"id": "shot", "type": "capture_screen", "label": "Receipt"}
        validated = validate_workflow(workflow(capture))
        self.assertEqual(validated["steps"][0], capture)

        recovery = workflow(
            capture,
            {
                "id": "ready",
                "type": "wait_for_text",
                "text": "Ready",
                "timeout_seconds": 5,
            },
        )
        validate_recovery_workflow(recovery)

        recorder = Recorder()
        recorder.start("owner-123", "Recorded", execute=False)
        recorder.stop("owner-123")
        inserted = recorder.review(
            "owner-123",
            "insert",
            {"index": 0, "step": {"type": "capture_screen", "label": "Screenshot"}},
        )
        step_id = inserted["workflow"]["steps"][0]["id"]
        recorder.review(
            "owner-123", "insert", {"index": 1, "step": {"type": "wait", "seconds": 0}}
        )
        renamed = recorder.review(
            "owner-123", "rename_step", {"step_id": step_id, "label": "Receipt"}
        )
        self.assertEqual(renamed["workflow"]["steps"][0]["label"], "Receipt")
        moved = recorder.review(
            "owner-123", "move", {"step_id": step_id, "index": 1}
        )
        self.assertEqual(moved["workflow"]["steps"][1]["type"], "capture_screen")
        deleted = recorder.review("owner-123", "delete", {"step_id": step_id})
        self.assertNotIn(
            "capture_screen", [s["type"] for s in deleted["workflow"]["steps"]]
        )

        store = WorkflowStorage(self.root / "flows")
        saved = store.save(validated, create=True)
        self.assertEqual(store.read(saved["id"])["steps"][0], capture)

        legacy = workflow(
            {
                "id": "marked",
                "type": "capture_screen",
                "label": "Screenshot",
            },
            {
                "id": "receipt",
                "type": "capture_screen",
                "label": "Receipt Screenshot",
                "output_key": "receipt_path",
            },
        )
        legacy["id"] = "legacy_screenshots"
        saved_legacy = store.save(legacy, create=True)
        self.assertEqual(store.read(saved_legacy["id"])["steps"], legacy["steps"])

    def test_store_uses_calibrated_frame_daily_relative_paths_and_no_secrets(self):
        instant = datetime(2026, 9, 29, 15, 45, 30, 482000, tzinfo=timezone.utc)
        store = WorkflowScreenshotStore(self.root / "shots", clock=lambda: instant)
        screen = CalibratedScreen()
        first = store.capture(
            screen,
            run_id="run_safe",
            step_id="step_7",
            label="Account 123 amount 999",
        )
        second = store.capture(
            screen,
            run_id="run_safe",
            step_id="step_7",
            label="Account 123 amount 999",
        )

        self.assertEqual(screen.capture_calls, 2)
        self.assertEqual(screen.ocr_calls, 0)
        self.assertRegex(first["relative_path"], r"^2026-09-29/run_safe_step_7_154530_482_[0-9a-f]{8}\.jpg$")
        self.assertNotEqual(first["relative_path"], second["relative_path"])
        self.assertNotIn("Account", first["relative_path"])
        self.assertFalse(Path(first["relative_path"]).is_absolute())
        paths = [self.root / "shots" / item["relative_path"] for item in (first, second)]
        self.assertTrue(all(path.is_file() for path in paths))
        written = cv2.imread(str(paths[0]), cv2.IMREAD_COLOR)
        self.assertEqual(written.shape, screen.image.shape)
        channel_means = written.mean(axis=(0, 1))
        self.assertGreater(float(channel_means.max() - channel_means.min()), 100)
        self.assertLess(np.max(np.abs(channel_means - [25, 110, 220])), 5)
        self.assertNotEqual(written.shape[:2], screen.raw_frame.shape[:2])

    def test_manual_service_run_retains_metadata_and_logs(self):
        screen = CalibratedScreen()
        store = WorkflowScreenshotStore(self.root / "shots")
        service = WorkflowService(
            self.controller,
            self.locks,
            external_screen=screen,
            screenshot_store=store,
        )
        started = service.start(
            workflow({"id": "shot", "type": "capture_screen", "label": "Receipt", "output_key": "receipt_path"})
        )
        result = service.wait(started["run_id"], 3)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["screenshots"][0]["step_id"], "shot")
        self.assertEqual(result["screenshots"][0]["label"], "Receipt")
        self.assertTrue((store.root / result["screenshots"][0]["relative_path"]).is_file())
        self.assertIn("Screenshot saved: Receipt", result["logs"])
        self.assertEqual(
            result["outputs"]["receipt_path"],
            str((store.root / result["screenshots"][0]["relative_path"]).resolve()),
        )

    def test_multiple_unified_screenshots_keep_all_artifacts_and_latest_proof_path(self):
        screen = CalibratedScreen()
        store = WorkflowScreenshotStore(self.root / "shots")
        service = WorkflowService(
            self.controller,
            self.locks,
            external_screen=screen,
            screenshot_store=store,
        )
        started = service.start(
            workflow(
                {
                    "id": "shot_one",
                    "type": "capture_screen",
                    "label": "Screenshot",
                    "output_key": "receipt_path",
                },
                {
                    "id": "shot_two",
                    "type": "capture_screen",
                    "label": "Screenshot",
                    "output_key": "receipt_path",
                },
            )
        )
        result = service.wait(started["run_id"], 3)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["screenshots"]), 2)
        latest = store.root / result["screenshots"][-1]["relative_path"]
        self.assertEqual(result["outputs"]["receipt_path"], str(latest.resolve()))

    def test_capture_failure_fails_step_with_clear_message(self):
        screen = CalibratedScreen(error=ScreenStateError("Calibration unavailable."))
        service = WorkflowService(
            self.controller,
            self.locks,
            external_screen=screen,
            screenshot_store=WorkflowScreenshotStore(self.root / "shots"),
        )
        result = service.wait(
            service.start(workflow({"id": "shot", "type": "capture_screen"}))[
                "run_id"
            ],
            3,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["message"], "Calibration unavailable.")
        self.assertEqual(result["screenshots"], [])

    def test_runner_uses_capture_callback_and_existing_steps_still_work(self):
        logger = logging.Logger("capture-test")
        context = AutomationContext(
            DEVICE,
            self.controller,
            threading.Event(),
            logger,
            SimpleNamespace(step_index=0, step_count=0, current_step=None),
        )
        captured = []
        WorkflowRunner(
            context, capture_screen=lambda step, index: captured.append((step["id"], index))
        ).run(
            workflow(
                {"id": "wait", "type": "wait", "seconds": 0},
                {"id": "shot", "type": "capture_screen"},
            )
        )
        self.assertEqual(captured, [("shot", 2)])

    def test_queued_job_preserves_workflow_run_screenshot_metadata(self):
        artifact = {
            "step_id": "shot",
            "label": "Receipt",
            "captured_at": "2026-09-29T15:45:30.482000+08:00",
            "relative_path": "2026-09-29/run_safe_shot_154530_482_deadbeef.jpg",
        }

        class Ready:
            @staticmethod
            def inspect(device_id, *, reserved=False):
                return {"ready": not reserved}

        class CompletedService:
            @staticmethod
            def has_active_run(device_id=None):
                return False

            @staticmethod
            def start_for_job(job, saved_workflow, *, cancel_event=None):
                return {"run_id": "run_safe"}

            @staticmethod
            def wait(run_id):
                return {
                    "status": "passed",
                    "message": "Script completed.",
                    "screenshots": [artifact],
                    "outputs": {"receipt_path": "/test/receipt.jpg"},
                }

        workflow_store = WorkflowStorage(self.root / "flows")
        saved = workflow_store.save(
            workflow({"id": "shot", "type": "capture_screen", "label": "Receipt"}),
            create=True,
        )
        scheduler = JobScheduler(JobStore(self.root / "jobs.json"), Ready())
        job = scheduler.enqueue(
            SimpleNamespace(account_id="account_a", device_id="phone_A"),
            saved["id"],
            {},
        )
        executor = BackgroundJobExecutor(
            scheduler, workflow_store, CompletedService(), poll_interval=0.01
        )
        executor._dispatch()
        deadline = threading.Event()
        for _ in range(100):
            current = scheduler.get_job(job.job_id)
            if current.status == "COMPLETED":
                break
            deadline.wait(0.01)
        self.assertEqual(current.status, "COMPLETED")
        self.assertEqual(current.screenshots, [artifact])
        self.assertEqual(current.public()["screenshots"], [artifact])
        self.assertEqual(current.outputs, {"receipt_path": "/test/receipt.jpg"})
        self.assertEqual(JobStore(self.root / "jobs.json").get(job.job_id).outputs, current.outputs)


if __name__ == "__main__":
    unittest.main()
