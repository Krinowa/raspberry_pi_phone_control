"""Deterministic camera/OCR state isolation across logical devices."""

import threading
import unittest

import numpy as np

from automation.screen.camera_ocr import CameraOCRScreenStateProvider


class TaggedScreen:
    def __init__(self, device_id, pixel, *, sequence=1):
        self.device_id = device_id
        self.pixel = pixel
        self.sequence = sequence
        self.camera = object()
        self.calibration = object()
        self.lock = threading.Lock()

    def update(self, pixel, sequence):
        with self.lock:
            self.pixel = pixel
            self.sequence = sequence

    def session(self):
        screen = self

        class Session:
            def capture_for_ocr(self, *, timings):
                with screen.lock:
                    pixel = screen.pixel
                    sequence = screen.sequence
                timings.update(capture=0.001, decode=0.001, warp=0.001)
                return np.full((120, 80), pixel, dtype=np.uint8), sequence, 0.0

        return Session()


class TaggedWorkerPool:
    pool_size = 2

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []

    def request(self, image):
        pixel = int(image[0, 0, 0])
        with self.lock:
            self.calls.append(pixel)
        device = "PHONE_1" if pixel < 100 else "PHONE_2"
        frame = pixel if pixel < 100 else pixel - 100
        return (
            [
                {
                    "res": {
                        "rec_texts": [f"{device}_FRAME_{frame}"],
                        "rec_scores": [0.99],
                        "rec_boxes": [[0, 0, 20, 10]],
                    }
                }
            ],
            {
                "queue_wait": 0.0,
                "worker_start": 0.0,
                "ipc_send": 0.0,
                "ipc_wait": 0.0,
                "inference": 0.001,
                "worker_pid": 1234,
                "worker_id": 0,
                "pool_size": self.pool_size,
                "busy_workers": 1,
                "queued_requests": 0,
            },
        )

    def status(self):
        return {
            "pool_size": self.pool_size,
            "busy_workers": 0,
            "queued_requests": 0,
        }


class MultiDeviceScreenStateTests(unittest.TestCase):
    def setUp(self):
        self.worker = TaggedWorkerPool()
        self.phone_1 = TaggedScreen("phone_1", 11, sequence=7)
        self.phone_2 = TaggedScreen("phone_2", 121, sequence=7)
        self.provider_1 = CameraOCRScreenStateProvider(
            self.phone_1, _shared_worker=self.worker
        )
        self.provider_2 = self.provider_1.for_screen(self.phone_2)
        self.provider_1.cache_threshold = 0.001
        self.provider_2.cache_threshold = 0.001

    def test_equal_frame_sequences_keep_camera_ocr_and_cache_state_isolated(self):
        self.assertIs(self.provider_1._worker, self.provider_2._worker)
        self.assertIsNot(self.provider_1._cache_by_scope, self.provider_2._cache_by_scope)
        self.assertIsNot(self.phone_1.camera, self.phone_2.camera)
        self.assertIsNot(self.phone_1.calibration, self.phone_2.calibration)

        self.assertEqual(self.provider_1.visible_texts(), ["PHONE_1_FRAME_11"])
        self.assertEqual(self.provider_2.visible_texts(), ["PHONE_2_FRAME_21"])
        self.assertEqual(self.worker.calls, [11, 121])

        # Equal sequence IDs and unchanged frames hit only each provider's own cache.
        self.assertEqual(self.provider_1.visible_texts(), ["PHONE_1_FRAME_11"])
        self.assertEqual(self.provider_2.visible_texts(), ["PHONE_2_FRAME_21"])
        self.assertEqual(self.worker.calls, [11, 121])

        self.phone_1.update(12, sequence=8)
        self.assertEqual(self.provider_1.visible_texts(), ["PHONE_1_FRAME_12"])
        self.assertEqual(self.provider_2.visible_texts(), ["PHONE_2_FRAME_21"])
        self.assertEqual(self.worker.calls, [11, 121, 12])

    def test_change_baselines_and_roi_wait_state_do_not_leak(self):
        self.provider_1.capture_change_baseline()
        self.provider_2.capture_change_baseline()
        self.assertIsNot(
            self.provider_1._change_baseline, self.provider_2._change_baseline
        )
        self.assertNotEqual(
            float(self.provider_1._change_baseline.mean()),
            float(self.provider_2._change_baseline.mean()),
        )

        roi = {"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0}
        wait_1 = self.provider_1.new_roi_wait()
        wait_2 = self.provider_2.new_roi_wait()
        self.assertEqual(
            self.provider_1.poll_contains_text(
                "missing", roi=roi, force=True, roi_state=wait_1
            ),
            (True, False),
        )
        self.assertEqual(wait_1["misses"], 1)
        self.assertEqual(wait_2["misses"], 0)
        self.assertEqual(self.provider_2._roi_waits, {})

        self.assertEqual(
            self.provider_2.poll_contains_text(
                "PHONE_2_FRAME_21", roi=roi, force=True, roi_state=wait_2
            ),
            (True, True),
        )
        self.assertEqual(wait_1["misses"], 1)
        self.assertEqual(wait_2["misses"], 0)
        self.assertIsNot(
            self.provider_1._change_baseline, self.provider_2._change_baseline
        )


if __name__ == "__main__":
    unittest.main()
