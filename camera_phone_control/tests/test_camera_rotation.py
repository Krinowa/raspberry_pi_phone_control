"""Frame-level tests for per-device camera rotation."""

import time
import threading
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from camera import Camera, rotate_frame


class CameraRotationTests(unittest.TestCase):
    def setUp(self):
        in_process = patch.object(
            Camera, "_should_use_capture_subprocess", return_value=False
        )
        in_process.start()
        self.addCleanup(in_process.stop)

    @staticmethod
    def _capture(read):
        capture = Mock()
        capture.isOpened.return_value = True
        capture.read.side_effect = read
        capture.getBackendName.return_value = "AVFoundation"
        properties = {
            cv2.CAP_PROP_FRAME_WIDTH: 1280.0,
            cv2.CAP_PROP_FRAME_HEIGHT: 720.0,
            cv2.CAP_PROP_FPS: 30.0,
            getattr(cv2, "CAP_PROP_FOURCC", 6): float(
                ord("M") | ord("J") << 8 | ord("P") << 16 | ord("G") << 24
            ),
        }

        def set_property(prop, value):
            properties[prop] = float(value)
            return True

        capture.set.side_effect = set_property
        capture.get.side_effect = properties.get
        return capture

    @staticmethod
    def _wait_for(predicate, timeout=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    def test_rotate_frame_orientation_for_all_supported_rotations(self):
        raw = np.arange(2 * 3, dtype=np.uint8).reshape(2, 3)
        expected = {
            0: raw,
            90: np.array([[3, 0], [4, 1], [5, 2]], dtype=np.uint8),
            180: np.array([[5, 4, 3], [2, 1, 0]], dtype=np.uint8),
            270: np.array([[2, 5], [1, 4], [0, 3]], dtype=np.uint8),
        }

        for rotation, result in expected.items():
            with self.subTest(rotation=rotation):
                np.testing.assert_array_equal(rotate_frame(raw, rotation), result)

    def _capture_one_rotated_frame(self, raw, rotation):
        camera = Camera(
            source=7,
            width=raw.shape[1],
            height=raw.shape[0],
            fps=60,
            rotate=rotation,
        )
        capture = Mock()
        capture.isOpened.return_value = True
        capture.read.side_effect = lambda: (True, raw.copy())

        with patch.object(camera, "_open_capture", return_value=capture):
            camera.start()
            deadline = time.monotonic() + 2
            latest = None
            while latest is None and time.monotonic() < deadline:
                latest = camera.frame()
                if latest is None:
                    time.sleep(0.005)
            self.assertIsNotNone(latest)
            observation = camera.observation()
            camera.close()

        capture.release.assert_called_once_with()
        self.assertIsNotNone(observation)
        # The observer used by calibration/OCR receives the same rotated JPEG
        # tuple produced by capture (plus its timestamp).
        return observation[:4]

    def test_capture_pipeline_stores_rotated_jpeg_and_geometry(self):
        raw = np.zeros((60, 100, 3), dtype=np.uint8)
        raw[:30, :50] = (255, 0, 0)
        raw[:30, 50:] = (0, 255, 0)
        raw[30:, :50] = (0, 0, 255)
        raw[30:, 50:] = (255, 255, 255)
        expected_sizes = {
            0: (100, 60),
            90: (60, 100),
            180: (100, 60),
            270: (60, 100),
        }

        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                jpeg, _, width, height = self._capture_one_rotated_frame(
                    raw, rotation
                )
                decoded = cv2.imdecode(
                    np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR
                )
                expected = rotate_frame(raw, rotation)
                self.assertEqual(decoded.shape[:2], expected.shape[:2])
                self.assertEqual((width, height), expected_sizes[rotation])

    def test_landscape_90_degree_capture_becomes_portrait(self):
        raw = np.zeros((1080, 1920, 3), dtype=np.uint8)
        _, _, width, height = self._capture_one_rotated_frame(raw, 90)
        self.assertEqual((width, height), (1080, 1920))

    def test_invalid_rotation_is_rejected_before_encoding(self):
        with self.assertRaisesRegex(ValueError, "0, 90, 180, or 270"):
            rotate_frame(np.zeros((2, 3), dtype=np.uint8), 45)

    def test_close_waits_for_reader_before_capture_thread_releases(self):
        camera = Camera(source=0)
        released = threading.Event()
        reading = threading.Event()
        finish_read = threading.Event()
        capture = Mock()
        capture.isOpened.return_value = True

        def read():
            reading.set()
            finish_read.wait(1)
            return False, None

        capture.read.side_effect = read
        capture.release.side_effect = released.set
        with patch.object(camera, "_open_capture", return_value=capture):
            camera.start()
            self.assertTrue(reading.wait(1))
            closed = threading.Event()
            closer = threading.Thread(
                target=lambda: (camera.close(), closed.set())
            )
            closer.start()
            self.assertFalse(released.wait(0.05))
            self.assertFalse(closed.is_set())
            finish_read.set()
            closer.join(1)

        capture.release.assert_called_once_with()
        self.assertTrue(closed.is_set())
        self.assertFalse(camera.thread.is_alive())

    def test_close_timeout_never_releases_while_read_is_active(self):
        camera = Camera(source=0)
        reading = threading.Event()
        finish_read = threading.Event()
        capture = Mock()
        capture.isOpened.return_value = True

        def read():
            reading.set()
            finish_read.wait(1)
            return False, None

        capture.read.side_effect = read
        with patch.object(camera, "_open_capture", return_value=capture):
            camera.start()
            self.assertTrue(reading.wait(1))
            with self.assertRaisesRegex(RuntimeError, "did not stop"):
                camera.close(timeout=0.01)
            capture.release.assert_not_called()
            finish_read.set()
            camera.thread.join(1)
            camera.close()

        capture.release.assert_called_once_with()
        self.assertFalse(camera.thread.is_alive())

    def test_open_handle_is_not_healthy_until_first_valid_frame(self):
        allow_frame = threading.Event()
        raw = np.zeros((20, 30, 3), dtype=np.uint8)

        def read():
            allow_frame.wait(1)
            return True, raw.copy()

        capture = self._capture(read)
        camera = Camera(source=0, avfoundation_modes=())
        with patch.object(camera, "_open_capture", return_value=capture):
            camera.start()
            self.assertIsNone(camera.wait_until_opened(timeout=0.02))
            self.assertFalse(camera.status()["ready"])
            allow_frame.set()
            self.assertTrue(camera.wait_until_opened(timeout=1))
            self.assertTrue(self._wait_for(lambda: camera.status()["ready"]))
            camera.close()

    def test_startup_read_failures_are_retried_until_first_frame(self):
        raw = np.zeros((20, 30, 3), dtype=np.uint8)
        attempts = 0

        def read():
            nonlocal attempts
            attempts += 1
            if attempts <= 3:
                return False, None
            return True, raw.copy()

        capture = self._capture(read)
        camera = Camera(
            source=0,
            read_failure_grace_seconds=0.2,
            read_retry_delay_seconds=0.005,
            avfoundation_modes=(),
        )
        with patch.object(camera, "_open_capture", return_value=capture):
            camera.start()
            self.assertTrue(camera.wait_until_opened(timeout=1))
            self.assertGreaterEqual(capture.read.call_count, 4)
            camera.close()

    def test_temporary_frame_gap_recovers_without_reopen(self):
        raw = np.zeros((20, 30, 3), dtype=np.uint8)
        results = [(True, raw), (False, None), (False, None), (True, raw)]

        def read():
            ok, frame = results.pop(0) if results else (True, raw)
            return ok, None if frame is None else frame.copy()

        capture = self._capture(read)
        camera = Camera(
            source=0,
            fps=120,
            read_failure_grace_seconds=0.2,
            read_retry_delay_seconds=0.005,
            avfoundation_modes=(),
        )
        with patch.object(camera, "_open_capture", return_value=capture) as opened:
            camera.start()
            self.assertTrue(camera.wait_until_opened(timeout=1))
            self.assertTrue(self._wait_for(lambda: camera.sequence >= 2))
            self.assertTrue(camera.thread.is_alive())
            self.assertTrue(camera.status()["ready"])
            opened.assert_called_once_with()
            camera.close()

    def test_repeated_failed_reads_report_diagnostics_and_stop(self):
        capture = self._capture(lambda: (False, None))
        camera = Camera(
            source=0,
            read_failure_grace_seconds=0.03,
            read_retry_delay_seconds=0.005,
            avfoundation_modes=(),
        )
        camera.configured_source = "avfoundation:rog"
        camera.camera_name = "ROG EYE S"
        with patch.object(camera, "_open_capture", return_value=capture), patch(
            "camera.sys.platform", "linux"
        ):
            with self.assertLogs("camera", level="WARNING") as logs:
                camera.start()
                camera.thread.join(1)

        self.assertFalse(camera.thread.is_alive())
        self.assertFalse(camera.wait_until_opened(timeout=0))
        self.assertGreater(capture.read.call_count, 1)
        status = camera.status()
        self.assertFalse(status["ready"])
        self.assertIn("read_ok=False", status["error"])
        self.assertIn("frame_is_none=True", status["error"])
        self.assertIn("capture_opened=True", status["error"])
        self.assertIn("stable_identity='avfoundation:rog'", status["error"])
        self.assertTrue(
            any("Camera frame delivery failed" in line for line in logs.output)
        )

    def test_avfoundation_mode_falls_back_and_logs_negotiated_values(self):
        capture = self._capture(lambda: (False, None))
        properties = {
            cv2.CAP_PROP_FRAME_WIDTH: 1280.0,
            cv2.CAP_PROP_FRAME_HEIGHT: 720.0,
            cv2.CAP_PROP_FPS: 30.0,
            getattr(cv2, "CAP_PROP_FOURCC", 6): float(
                ord("M") | ord("J") << 8 | ord("P") << 16 | ord("G") << 24
            ),
        }

        def set_property(prop, value):
            # Simulate hardware rejecting 1920x1080 and accepting 1280x720.
            if prop == cv2.CAP_PROP_FRAME_WIDTH and value == 1920:
                return False
            properties[prop] = float(value)
            return True

        capture.set.side_effect = set_property
        capture.get.side_effect = properties.get
        camera = Camera(
            source=1,
            avfoundation_modes=((1920, 1080, 30), (1280, 720, 30)),
        )
        camera.configured_source = "avfoundation:rog"
        with patch("camera.sys.platform", "darwin"):
            with self.assertLogs("camera", level="INFO") as logs:
                camera._configure_capture(capture)

        self.assertEqual(camera.negotiated_capture["width"], 1280.0)
        self.assertEqual(camera.negotiated_capture["height"], 720.0)
        self.assertEqual(camera.negotiated_capture["fps"], 30.0)
        combined = "\n".join(logs.output)
        self.assertIn("requested=1920x1080@30", combined)
        self.assertIn("requested=1280x720@30", combined)
        self.assertIn("fourcc=MJPG", combined)
        self.assertIn("backend=AVFoundation", combined)


if __name__ == "__main__":
    unittest.main()
