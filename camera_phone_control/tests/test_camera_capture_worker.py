"""Tests for isolated macOS AVFoundation camera capture."""

import multiprocessing
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from camera import Camera
from camera_manager import (
    CameraConfigStorage,
    CameraManager,
    UnavailableCamera,
    avfoundation_camera_source,
)
from hardware.camera_capture_worker import (
    STABLE_IDENTITY_MISMATCH,
    CameraCaptureWorker,
    capture_worker_main,
)


def idle_worker(output_queue, stop_event, config):
    while not stop_event.wait(0.01):
        pass


class FakeCapture:
    def __init__(self, frames=(), *, opened=True):
        self.frames = list(frames)
        self.opened = opened
        self.released = False
        self.properties = {
            cv2.CAP_PROP_FRAME_WIDTH: 1920.0,
            cv2.CAP_PROP_FRAME_HEIGHT: 1080.0,
            cv2.CAP_PROP_FPS: 30.0,
            getattr(cv2, "CAP_PROP_FOURCC", 6): 0.0,
        }

    def isOpened(self):
        return self.opened

    def read(self):
        if self.frames:
            return self.frames.pop(0)
        return False, None

    def set(self, prop, value):
        self.properties[prop] = float(value)
        return True

    def get(self, prop):
        return self.properties.get(prop)

    def getBackendName(self):
        return "AVFOUNDATION"

    def release(self):
        self.released = True


class StopAfterFrame:
    def __init__(self):
        self.event = threading.Event()
        self.waits = 0

    def is_set(self):
        return self.event.is_set()

    def set(self):
        self.event.set()

    def wait(self, timeout):
        self.waits += 1
        if self.waits >= 1:
            self.event.set()
        return self.event.wait(timeout)


class FakeWorker:
    next_pid = 9000

    def __init__(self, config, messages):
        self.config = config
        self.messages = [dict(message) for message in messages]
        for message in self.messages:
            if message.get("type") == "frame" and message.get("device_id") is None:
                message["device_id"] = config.get("device_id")
        self.pid = self.__class__.next_pid
        self.__class__.next_pid += 1
        self.exitcode = None
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.started = True

    def receive(self, timeout=0.1):
        if self.messages:
            return self.messages.pop(0)
        time.sleep(min(timeout, 0.002))
        return None

    def is_alive(self):
        return self.started and not self.stopped

    def request_stop(self):
        self.stopped = True

    def update_fps(self, fps):
        self.config["fps"] = fps

    def close(self, timeout=1.0):
        self.stopped = True
        self.exitcode = 0
        self.closed = True


def frame_message(color=(0, 0, 255), width=12, height=8):
    image = np.full((height, width, 3), color, dtype=np.uint8)
    encoded_ok, encoded = cv2.imencode(".jpg", image)
    assert encoded_ok
    return {
        "type": "frame",
        "device_id": None,
        "jpeg": encoded.tobytes(),
        "sequence": 1,
        "width": width,
        "height": height,
        "raw_width": width,
        "raw_height": height,
        "negotiated_capture": {
            "width": 1920.0,
            "height": 1080.0,
            "fps": 30.0,
            "backend": "AVFOUNDATION",
            "fourcc": "0",
        },
    }


class CameraCaptureWorkerTests(unittest.TestCase):
    def worker_config(self, **updates):
        config = {
            "source": 1,
            "width": 1280,
            "height": 720,
            "fps": 30,
            "rotation": 0,
            "configured_source": "avfoundation:rog",
            "camera_name": "ROG EYE S",
            "stable_id": "avfoundation:rog",
            "avfoundation_modes": ((1920, 1080, 30.0), (1280, 720, 30.0)),
            "startup_grace_seconds": 0.02,
            "read_failure_grace_seconds": 0.02,
            "read_retry_delay_seconds": 0.001,
        }
        config.update(updates)
        return config

    @staticmethod
    def wait_for(predicate, timeout=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    def test_worker_startup_success_publishes_latest_jpeg(self):
        raw = np.full((8, 12, 3), (10, 80, 220), dtype=np.uint8)
        capture = FakeCapture([(True, raw)])
        output = queue.Queue(maxsize=1)
        stop = StopAfterFrame()

        capture_worker_main(
            output,
            stop,
            self.worker_config(),
            capture_factory=lambda source, backend: capture,
            identity_discovery=lambda: [
                {"stable_id": "avfoundation:rog", "runtime_source": 1}
            ],
        )

        message = output.get_nowait()
        self.assertEqual(message["type"], "frame")
        self.assertEqual((message["width"], message["height"]), (12, 8))
        self.assertIsNotNone(
            cv2.imdecode(np.frombuffer(message["jpeg"], np.uint8), cv2.IMREAD_COLOR)
        )
        self.assertTrue(capture.released)

    def test_worker_startup_failure_is_reported_and_releases_handle(self):
        capture = FakeCapture(opened=False)
        output = queue.Queue(maxsize=1)
        stop = threading.Event()

        capture_worker_main(
            output,
            stop,
            self.worker_config(),
            capture_factory=lambda source, backend: capture,
        )

        message = output.get_nowait()
        self.assertEqual(message["type"], "error")
        self.assertFalse(message["opened"])
        self.assertIn("Cannot open camera source", message["error"])
        self.assertTrue(capture.released)

    def test_worker_refuses_frame_after_stable_identity_moves(self):
        raw = np.full((8, 12, 3), (10, 80, 220), dtype=np.uint8)
        capture = FakeCapture([(True, raw)])
        output = queue.Queue(maxsize=1)
        stop = threading.Event()
        config = self.worker_config()

        capture_worker_main(
            output,
            stop,
            config,
            capture_factory=lambda source, backend: capture,
            identity_discovery=lambda: [
                {"stable_id": "avfoundation:macbook", "runtime_source": 1}
            ],
        )

        message = output.get_nowait()
        self.assertEqual(message["type"], "error")
        self.assertEqual(message["error_code"], STABLE_IDENTITY_MISMATCH)
        self.assertIn("identity no longer maps", message["error"])
        self.assertTrue(capture.released)

    def test_worker_failure_after_healthy_marks_camera_unavailable(self):
        messages = [frame_message(), {"type": "error", "error": "unplugged"}]
        workers = []

        def factory(config):
            worker = FakeWorker(config, messages)
            workers.append(worker)
            return worker

        camera = Camera(
            source=1,
            use_capture_subprocess=True,
            capture_worker_factory=factory,
        )
        camera.capture_backend = "avfoundation"
        camera.start()

        self.assertTrue(camera.wait_until_opened(1))
        self.assertTrue(self.wait_for(lambda: not camera.thread.is_alive()))
        self.assertFalse(camera.status()["ready"])
        self.assertEqual(camera.open_succeeded, True)
        self.assertIn("unplugged", camera.status()["error"])
        self.assertTrue(workers[0].closed)

    def test_parent_rejects_frame_when_stable_identity_no_longer_matches(self):
        workers = []

        def factory(config):
            worker = FakeWorker(config, [frame_message()])
            workers.append(worker)
            return worker

        camera = Camera(
            source=0,
            use_capture_subprocess=True,
            capture_worker_factory=factory,
        )
        camera.configured_source = "avfoundation:rog"
        camera.identity_validator = lambda: False
        camera.start()

        self.assertFalse(camera.wait_until_opened(1))
        self.assertTrue(self.wait_for(lambda: not camera.thread.is_alive()))
        self.assertIsNone(camera.frame())
        self.assertFalse(camera.status()["ready"])
        self.assertIn("no longer resolves", camera.status()["error"])
        self.assertTrue(workers[0].closed)

    def test_parent_rejects_frame_tagged_for_another_device(self):
        message = frame_message()
        message["device_id"] = "phone_B"
        workers = []

        def factory(config):
            worker = FakeWorker(config, [message])
            workers.append(worker)
            return worker

        camera = Camera(
            source=0,
            use_capture_subprocess=True,
            capture_worker_factory=factory,
        )
        camera.device_id = "phone_A"
        camera.start()

        self.assertFalse(camera.wait_until_opened(1))
        self.assertTrue(self.wait_for(lambda: not camera.thread.is_alive()))
        self.assertIsNone(camera.frame())
        self.assertIn("wrong logical device", camera.status()["error"])
        self.assertTrue(workers[0].closed)

    def test_camera_can_start_a_brand_new_worker_after_failure(self):
        plans = [
            [frame_message(), {"type": "error", "error": "unplugged"}],
            [frame_message(color=(0, 255, 0))],
        ]
        workers = []

        def factory(config):
            worker = FakeWorker(config, plans.pop(0))
            workers.append(worker)
            return worker

        camera = Camera(
            source=1,
            use_capture_subprocess=True,
            capture_worker_factory=factory,
        )
        camera.start()
        self.assertTrue(camera.wait_until_opened(1))
        self.assertTrue(self.wait_for(lambda: not camera.thread.is_alive()))

        camera.start()

        self.assertTrue(camera.wait_until_opened(1))
        self.assertNotEqual(workers[0].pid, workers[1].pid)
        self.assertIsNotNone(camera.frame())
        camera.close()
        self.assertTrue(workers[1].closed)

    def test_manager_restarts_worker_for_same_identity_at_new_runtime_index(self):
        devices = [
            {"index": 0, "unique_id": "macbook", "name": "MacBook Camera"},
            {"index": 1, "unique_id": "rog", "name": "ROG EYE S"},
        ]
        plans = {
            1: [[frame_message(), {"type": "error", "error": "unplugged"}]],
            0: [[frame_message(color=(255, 0, 0))]],
        }
        workers = []

        def camera_factory(source, width, height, fps, rotation):
            def worker_factory(config):
                worker = FakeWorker(config, plans[source].pop(0))
                workers.append(worker)
                return worker

            return Camera(
                source,
                width,
                height,
                fps,
                rotation,
                use_capture_subprocess=True,
                capture_worker_factory=worker_factory,
            )

        with tempfile.TemporaryDirectory() as temporary:
            manager = CameraManager(
                storage=CameraConfigStorage(Path(temporary) / "cameras.json"),
                camera_factory=camera_factory,
                identity_discovery=lambda: [dict(item) for item in devices],
                recovery_retry_seconds=0,
            )
            rog = avfoundation_camera_source("rog")
            manager.update("phone_A", camera_source=rog, rotation=0, enabled=True)
            original = manager.get_camera("phone_A")
            self.assertTrue(original.wait_until_opened(1))
            self.assertEqual(original.source, 1)
            self.assertTrue(self.wait_for(lambda: not original.thread.is_alive()))

            devices[:] = [
                {"index": 0, "unique_id": "rog", "name": "ROG EYE S"},
                {"index": 1, "unique_id": "macbook", "name": "MacBook Camera"},
            ]
            manager.last_opened["phone_A"] = 0
            with patch("camera_manager.CAMERA_REOPEN_DELAY_SECONDS", 0):
                recovered = manager.get_camera("phone_A")

            self.assertIsNot(recovered, original)
            self.assertTrue(recovered.wait_until_opened(1))
            self.assertEqual(recovered.source, 0)
            self.assertEqual(recovered.configured_source, rog)
            self.assertEqual(manager.config("phone_A").camera_source, rog)
            self.assertNotEqual(workers[0].pid, workers[1].pid)
            manager.release_all()

    def test_enable_waits_for_delayed_worker_frame_without_replacement(self):
        devices = [{"index": 0, "unique_id": "rog", "name": "ROG EYE S"}]
        workers = []
        timers = []

        def camera_factory(source, width, height, fps, rotation):
            def worker_factory(config):
                worker = FakeWorker(config, [])
                original_start = worker.start

                def delayed_start():
                    original_start()

                    def publish_frame():
                        message = frame_message()
                        message["device_id"] = config.get("device_id")
                        worker.messages.append(message)

                    timer = threading.Timer(
                        0.05, publish_frame
                    )
                    timers.append(timer)
                    timer.start()

                worker.start = delayed_start
                workers.append(worker)
                return worker

            return Camera(
                source,
                width,
                height,
                fps,
                rotation,
                use_capture_subprocess=True,
                capture_worker_factory=worker_factory,
            )

        with tempfile.TemporaryDirectory() as temporary:
            manager = CameraManager(
                storage=CameraConfigStorage(Path(temporary) / "cameras.json"),
                camera_factory=camera_factory,
                identity_discovery=lambda: [dict(item) for item in devices],
                startup_timeout=0.4,
            )
            rog = avfoundation_camera_source("rog")
            manager.update("phone_A", camera_source=rog, rotation=0, enabled=False)
            manager.get_camera("phone_A")

            with patch("camera_manager.CAMERA_REOPEN_DELAY_SECONDS", 0):
                manager.update(
                    "phone_A", camera_source=rog, rotation=0, enabled=True
                )

            camera = manager.get_camera("phone_A")
            self.assertEqual(len(workers), 1)
            self.assertTrue(camera.open_succeeded)
            self.assertTrue(camera.status()["ready"])
            self.assertFalse(workers[0].closed)
            manager.release_all()
            for timer in timers:
                timer.join(1)

    def test_manager_keeps_rediscovering_after_worker_identity_mismatch(self):
        devices = [
            {"index": 0, "unique_id": "rog", "name": "ROG EYE S"},
            {"index": 1, "unique_id": "macbook", "name": "MacBook Camera"},
        ]
        plans = {
            0: [
                [
                    frame_message(),
                    {
                        "type": "error",
                        "error_code": STABLE_IDENTITY_MISMATCH,
                        "error": "ROG no longer owns runtime index 0",
                    },
                ]
            ],
            1: [[frame_message(color=(255, 0, 0))]],
        }
        workers = []
        opened_sources = []

        def camera_factory(source, width, height, fps, rotation):
            opened_sources.append(source)

            def worker_factory(config):
                worker = FakeWorker(config, plans[source].pop(0))
                workers.append(worker)
                return worker

            return Camera(
                source,
                width,
                height,
                fps,
                rotation,
                use_capture_subprocess=True,
                capture_worker_factory=worker_factory,
            )

        with tempfile.TemporaryDirectory() as temporary:
            manager = CameraManager(
                storage=CameraConfigStorage(Path(temporary) / "cameras.json"),
                camera_factory=camera_factory,
                identity_discovery=lambda: [dict(item) for item in devices],
                recovery_retry_seconds=0,
                recovery_max_attempts=1,
            )
            rog = avfoundation_camera_source("rog")
            manager.update("phone_A", camera_source=rog, rotation=0, enabled=True)
            original = manager.get_camera("phone_A")
            self.assertTrue(original.wait_until_opened(1))
            self.assertTrue(self.wait_for(lambda: not original.thread.is_alive()))
            self.assertTrue(original.identity_mismatch)
            self.assertTrue(original.open_succeeded)
            self.assertFalse(original.status()["ready"])
            self.assertFalse(original.status()["worker_alive"])

            # The ROG is gone and the MacBook has inherited index 0. The old
            # numeric source must not be reopened for phone_A.
            devices[:] = [
                {"index": 0, "unique_id": "macbook", "name": "MacBook Camera"}
            ]
            unavailable = manager.get_camera("phone_A")
            self.assertIsInstance(unavailable, UnavailableCamera)
            self.assertIsNone(unavailable.frame())
            self.assertEqual(opened_sources, [0])
            self.assertNotIn("phone_A", manager.recovery_attempts)

            manager.last_opened["phone_A"] = 0
            still_unavailable = manager.get_camera("phone_A")
            self.assertIsInstance(still_unavailable, UnavailableCamera)
            self.assertEqual(opened_sources, [0])

            # The same stable ID returns at a different runtime index. A fresh
            # Camera and worker are created without restarting the manager.
            devices[:] = [
                {"index": 0, "unique_id": "macbook", "name": "MacBook Camera"},
                {"index": 1, "unique_id": "rog", "name": "ROG EYE S"},
            ]
            manager.last_opened["phone_A"] = 0
            recovered = manager.get_camera("phone_A")
            self.assertIsNot(recovered, original)
            self.assertTrue(recovered.wait_until_opened(1))
            self.assertTrue(recovered.status()["ready"])
            self.assertEqual(recovered.source, 1)
            self.assertEqual(recovered.configured_source, rog)
            self.assertEqual(opened_sources, [0, 1])
            self.assertNotEqual(workers[0].pid, workers[1].pid)
            manager.release_all()

    def test_two_camera_workers_keep_frames_and_lifecycles_isolated(self):
        workers = []

        def make_camera(source, color):
            def factory(config):
                worker = FakeWorker(config, [frame_message(color=color)])
                workers.append(worker)
                return worker

            camera = Camera(
                source=source,
                use_capture_subprocess=True,
                capture_worker_factory=factory,
            )
            camera.start()
            self.assertTrue(camera.wait_until_opened(1))
            return camera

        phone_a = make_camera(0, (0, 0, 255))
        phone_b = make_camera(1, (0, 255, 0))
        frame_a = cv2.imdecode(
            np.frombuffer(phone_a.frame()[0], np.uint8), cv2.IMREAD_COLOR
        )
        frame_b = cv2.imdecode(
            np.frombuffer(phone_b.frame()[0], np.uint8), cv2.IMREAD_COLOR
        )
        self.assertGreater(frame_a[:, :, 2].mean(), frame_a[:, :, 1].mean())
        self.assertGreater(frame_b[:, :, 1].mean(), frame_b[:, :, 2].mean())

        phone_a.close()

        self.assertTrue(workers[0].closed)
        self.assertFalse(workers[1].closed)
        self.assertIsNotNone(phone_b.frame())
        phone_b.close()

    def test_frame_status_observation_and_validation_remain_compatible(self):
        worker = None

        def factory(config):
            nonlocal worker
            worker = FakeWorker(config, [frame_message(width=15, height=9)])
            return worker

        camera = Camera(
            source=0,
            use_capture_subprocess=True,
            capture_worker_factory=factory,
        )
        camera.start()
        self.assertTrue(camera.wait_until_opened(1))
        jpeg, sequence, width, height = camera.frame()
        self.assertTrue(jpeg)
        self.assertEqual((width, height), (15, 9))
        self.assertEqual(camera.observation()[:4], camera.frame())
        camera.validate_frame(sequence)
        status = camera.status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["worker_pid"], worker.pid)
        camera.fps = 7
        self.assertEqual(worker.config["fps"], 7)
        camera.close()

    def test_close_joins_real_process_without_zombie(self):
        worker = CameraCaptureWorker(self.worker_config(), target=idle_worker)
        worker.start()
        pid = worker.pid

        worker.close(timeout=1)

        self.assertIsNotNone(pid)
        self.assertFalse(worker.is_alive())
        self.assertIsNotNone(worker.exitcode)

    def test_each_camera_worker_owns_distinct_process_and_ipc_state(self):
        worker_a = CameraCaptureWorker(self.worker_config(source=0), target=idle_worker)
        worker_b = CameraCaptureWorker(self.worker_config(source=1), target=idle_worker)
        self.assertIsNot(worker_a.process, worker_b.process)
        self.assertIsNot(worker_a.output_queue, worker_b.output_queue)
        self.assertIsNot(worker_a.stop_event, worker_b.stop_event)
        self.assertIsNot(worker_a.fps_value, worker_b.fps_value)

        worker_a.start()
        worker_b.start()
        try:
            self.assertNotEqual(worker_a.pid, worker_b.pid)
            self.assertTrue(worker_a.is_alive())
            self.assertTrue(worker_b.is_alive())
            worker_a.close(timeout=1)
            self.assertFalse(worker_a.is_alive())
            self.assertTrue(worker_b.is_alive())
        finally:
            worker_a.close(timeout=1)
            worker_b.close(timeout=1)

    def test_only_macos_local_avfoundation_uses_subprocess(self):
        with patch("camera.sys.platform", "darwin"):
            mac = Camera(0)
            mac.capture_backend = "avfoundation"
            self.assertTrue(mac._should_use_capture_subprocess())
            network = Camera("rtsp://camera.test/live")
            self.assertFalse(network._should_use_capture_subprocess())
        with patch("camera.sys.platform", "linux"):
            linux = Camera("/dev/video0")
            linux.capture_backend = "v4l2"
            self.assertFalse(linux._should_use_capture_subprocess())
        with patch("camera.sys.platform", "win32"):
            windows = Camera(0)
            windows.capture_backend = "dshow"
            self.assertFalse(windows._should_use_capture_subprocess())


if __name__ == "__main__":
    multiprocessing.freeze_support()
    unittest.main()
