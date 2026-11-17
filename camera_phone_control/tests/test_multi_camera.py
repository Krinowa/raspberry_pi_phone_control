"""Device-scoped camera routing and persistence."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from android_control import AndroidControl
from app import ControlState, create_app
from automation.workflows.models import new_workflow
from automation.workflows.storage import WorkflowStorage
from calibration import Calibration, CalibrationStorage
from camera_manager import (
    CameraConfig,
    CameraConfigStorage,
    CameraManager,
    CameraManagerError,
    avfoundation_camera_source,
)
from device_lock import DeviceLocks
from hardware.pi_api_client import PiApiClient
from hardware.raspberry_pi_input import PiControlTarget
from test_control import authenticate_admin


class ManagedCamera:
    instances = []

    def __init__(self, source, width, height, fps, rotate):
        self.source, self.rotate = source, rotate
        self.width, self.height = width, height
        self.fps = fps
        self.started = False
        self.closed = False
        self.sequence = 123
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def status(self):
        ready = self.started and not self.closed and self.source != 99
        return {
            "ready": ready,
            "source": str(self.source),
            "index": self.source,
            "width": self.width if ready else None,
            "height": self.height if ready else None,
            "error": "Cannot open assigned camera." if not ready else "",
        }

    def frame(self):
        if not self.status()["ready"]:
            return None
        return f"jpeg-{self.source}".encode(), self.sequence, self.width, self.height

    def observation(self):
        frame = self.frame()
        return (*frame, time.monotonic()) if frame else None

    def validate_frame(self, frame_id):
        if frame_id != self.sequence or not self.status()["ready"]:
            raise ValueError("A fresh camera frame is required.")


class LifecycleCamera(ManagedCamera):
    plans = {}
    events = []

    def __init__(self, source, width, height, fps, rotate):
        super().__init__(source, width, height, fps, rotate)
        plans = self.__class__.plans.setdefault(source, [])
        self.plan = plans.pop(0) if plans else {"opened": True}
        self.thread = Mock()
        self.thread.is_alive.return_value = self.plan.get("thread_alive", True)
        self.open_succeeded = None

    def start(self):
        super().start()
        self.__class__.events.append(("start", self.source))

    def wait_until_opened(self, timeout):
        self.__class__.events.append(("wait", self.source, timeout))
        ready = self.plan.get("ready_event")
        if ready is not None:
            ready.wait(timeout)
        opened = self.plan.get("opened", True)
        if opened is not None:
            self.open_succeeded = opened
        return opened

    def status(self):
        status = super().status()
        ready_event = self.plan.get("ready_event")
        opened = self.plan.get("opened", True)
        starting = ready_event is not None and not ready_event.is_set()
        if opened is not True or starting:
            status.update(
                ready=False,
                width=None,
                height=None,
                error=(
                    "Camera is starting..."
                    if opened is None or starting
                    else "Cannot open assigned camera."
                ),
            )
        return status

    def close(self):
        self.__class__.events.append(("close", self.source))
        self.thread.is_alive.return_value = False
        super().close()


class IdentityFrameCamera(ManagedCamera):
    def frame(self):
        if not self.status()["ready"]:
            return None
        identity = getattr(self, "configured_source", "unassigned")
        return f"jpeg-{identity}".encode(), self.sequence, self.width, self.height


class MultiCameraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ManagedCamera.instances.clear()
        LifecycleCamera.plans = {}
        LifecycleCamera.events = []
        self.storage = CameraConfigStorage(self.root / "cameras.json")

    def manager(self, source=0, rotation=0):
        return CameraManager(
            storage=self.storage,
            default_source=source,
            default_rotation=rotation,
            camera_factory=ManagedCamera,
            identity_discovery=False,
        )

    def lifecycle_manager(self, **kwargs):
        return CameraManager(
            storage=self.storage,
            default_source=0,
            camera_factory=LifecycleCamera,
            identity_discovery=False,
            startup_timeout=kwargs.pop("startup_timeout", 0.25),
            recovery_retry_seconds=kwargs.pop("recovery_retry_seconds", 0),
            **kwargs,
        )

    def identity_manager(self, devices):
        return CameraManager(
            storage=self.storage,
            default_source=0,
            camera_factory=ManagedCamera,
            identity_discovery=lambda: [dict(item) for item in devices],
            recovery_retry_seconds=0,
            slow_recovery_retry_seconds=0,
        )

    def test_stable_identity_follows_camera_when_order_changes(self):
        devices = [
            {"index": 0, "unique_id": "macbook", "name": "MacBook Air Camera"},
            {"index": 1, "unique_id": "rog", "name": "ROG EYE S"},
        ]
        rog = avfoundation_camera_source("rog")
        manager = self.identity_manager(devices)
        manager.update(
            "phone_A",
            camera_source=rog,
            rotation=0,
            enabled=True,
            display_name="ROG EYE S",
        )
        self.assertEqual(manager.get_camera("phone_A").source, 1)
        manager.release_device("phone_A")

        devices[:] = [
            {"index": 0, "unique_id": "rog", "name": "ROG EYE S"},
            {"index": 1, "unique_id": "macbook", "name": "MacBook Air Camera"},
        ]

        self.assertEqual(manager.config("phone_A").camera_source, rog)
        self.assertEqual(manager.get_camera("phone_A").source, 0)

    def test_camera_id_is_authoritative_over_saved_runtime_source(self):
        devices = [
            {
                "stable_id": "avfoundation:macbook",
                "name": "MacBook Air Camera",
                "backend": "avfoundation",
                "runtime_source": 0,
            },
            {
                "stable_id": "avfoundation:rog",
                "name": "ROG EYE S",
                "backend": "avfoundation",
                "runtime_source": 1,
            },
        ]
        manager = self.identity_manager(devices)
        manager.update(
            "phone_A",
            camera_source=1,
            camera_id="avfoundation:rog",
            rotation=0,
            enabled=True,
        )
        self.assertEqual(manager.get_camera("phone_A").source, 1)
        manager.release_device("phone_A")
        devices[0]["runtime_source"] = 1
        devices[1]["runtime_source"] = 0

        reopened = manager.get_camera("phone_A")

        self.assertEqual(reopened.source, 0)
        self.assertEqual(manager.config("phone_A").camera_id, "avfoundation:rog")
        self.assertEqual(manager.config("phone_A").camera_source, 1)
        restarted = self.identity_manager(devices)
        self.assertEqual(restarted.config("phone_A").camera_id, "avfoundation:rog")
        self.assertEqual(restarted.get_camera("phone_A").source, 0)

    def test_disconnected_identity_never_falls_back_to_reused_index(self):
        devices = [
            {"index": 1, "unique_id": "rog", "name": "ROG EYE S"},
        ]
        rog = avfoundation_camera_source("rog")
        manager = self.identity_manager(devices)
        manager.update(
            "phone_A",
            camera_source=rog,
            rotation=0,
            enabled=True,
            display_name="ROG EYE S",
        )
        manager.release_device("phone_A")
        before = len(ManagedCamera.instances)
        devices[:] = [
            {"index": 1, "unique_id": "macbook", "name": "MacBook Air Camera"},
        ]

        unavailable = manager.get_camera("phone_A")

        self.assertFalse(unavailable.status()["ready"])
        self.assertIn("disconnected", unavailable.status()["error"])
        self.assertEqual(len(ManagedCamera.instances), before)
        self.assertEqual(manager.config("phone_A").camera_source, rog)

    def test_live_avfoundation_mapping_fails_closed_when_index_is_reused(self):
        devices = [
            {"index": 0, "unique_id": "rog", "name": "ROG EYE S"},
            {"index": 1, "unique_id": "macbook", "name": "MacBook Air Camera"},
        ]
        rog = avfoundation_camera_source("rog")
        manager = self.identity_manager(devices)
        manager.update(
            "phone_A",
            camera_source=rog,
            rotation=0,
            enabled=True,
            display_name="ROG EYE S",
        )
        original = manager.get_camera("phone_A")
        self.assertEqual(original.source, 0)
        before_disconnect = len(ManagedCamera.instances)

        devices[:] = [
            {"index": 0, "unique_id": "macbook", "name": "MacBook Air Camera"}
        ]
        with patch("camera_manager.CAMERA_REOPEN_DELAY_SECONDS", 0):
            unavailable = manager.get_camera("phone_A")

        self.assertTrue(original.closed)
        self.assertFalse(unavailable.status()["ready"])
        self.assertIn("disconnected", unavailable.status()["error"])
        self.assertEqual(len(ManagedCamera.instances), before_disconnect)
        self.assertEqual(manager.config("phone_A").camera_source, rog)

        devices.append({"index": 1, "unique_id": "rog", "name": "ROG EYE S"})
        manager.last_opened["phone_A"] = 0
        restored = manager.get_camera("phone_A")

        self.assertEqual(restored.source, 1)
        self.assertEqual(restored.configured_source, rog)
        self.assertEqual(restored.camera_name, "ROG EYE S")
        self.assertEqual(
            [camera.camera_name for camera in ManagedCamera.instances],
            ["ROG EYE S", "ROG EYE S"],
        )

    def test_avfoundation_identity_revalidation_is_scoped_per_device(self):
        devices = [
            {"index": 0, "unique_id": "rog", "name": "ROG EYE S"},
            {"index": 1, "unique_id": "macbook", "name": "MacBook Air Camera"},
        ]
        rog = avfoundation_camera_source("rog")
        macbook = avfoundation_camera_source("macbook")
        manager = self.identity_manager(devices)
        manager.update("phone_A", camera_source=rog, rotation=0, enabled=True)
        manager.update("phone_B", camera_source=macbook, rotation=0, enabled=True)
        old_rog = manager.get_camera("phone_A")
        old_macbook = manager.get_camera("phone_B")

        devices[:] = [
            {"index": 0, "unique_id": "macbook", "name": "MacBook Air Camera"}
        ]
        with patch("camera_manager.CAMERA_REOPEN_DELAY_SECONDS", 0):
            unavailable_rog = manager.get_camera("phone_A")
            current_macbook = manager.get_camera("phone_B")

        self.assertFalse(unavailable_rog.status()["ready"])
        self.assertTrue(old_rog.closed)
        self.assertTrue(old_macbook.closed)
        self.assertEqual(current_macbook.source, 0)
        self.assertEqual(current_macbook.configured_source, macbook)
        self.assertEqual(current_macbook.camera_name, "MacBook Air Camera")

        devices.append({"index": 1, "unique_id": "rog", "name": "ROG EYE S"})
        manager.last_opened["phone_A"] = 0
        restored_rog = manager.get_camera("phone_A")

        self.assertEqual(restored_rog.source, 1)
        self.assertEqual(restored_rog.configured_source, rog)
        self.assertIs(manager.get_camera("phone_B"), current_macbook)
        self.assertEqual(
            [
                (camera.camera_name, camera.source)
                for camera in ManagedCamera.instances
                if not camera.closed
            ],
            [("MacBook Air Camera", 0), ("ROG EYE S", 1)],
        )

    def test_reconnected_identity_restores_its_current_index(self):
        devices = []
        rog = avfoundation_camera_source("rog")
        manager = self.identity_manager(devices)
        manager.update(
            "phone_A",
            camera_source=rog,
            rotation=0,
            enabled=True,
            display_name="ROG EYE S",
        )
        self.assertFalse(manager.get_camera("phone_A").status()["ready"])
        devices.append({"index": 0, "unique_id": "rog", "name": "ROG EYE S"})
        manager.last_opened["phone_A"] = 0

        restored = manager.get_camera("phone_A")

        self.assertTrue(restored.status()["ready"])
        self.assertEqual(restored.source, 0)
        self.assertEqual(manager.config("phone_A").camera_source, rog)

    def test_unresolved_windows_identity_retries_and_recovers_at_new_index(self):
        stable_id = r"dshow:USB\VID_1234\CAMERA_ONE"
        devices = [
            {
                "stable_id": stable_id,
                "name": "USB Camera",
                "backend": "dshow",
                "runtime_source": None,
                "mapping_failure_reason": "directshow_enumeration_empty",
            }
        ]
        manager = CameraManager(
            storage=self.storage,
            camera_factory=ManagedCamera,
            identity_discovery=lambda: [dict(item) for item in devices],
            recovery_retry_seconds=3,
            slow_recovery_retry_seconds=15,
        )
        manager.update(
            "phone_A",
            camera_source=stable_id,
            rotation=0,
            enabled=True,
            display_name="USB Camera",
        )
        unavailable = manager.get_camera("phone_A")
        self.assertFalse(unavailable.status()["ready"])
        self.assertIn("cannot be mapped safely", unavailable.status()["error"])

        devices[0]["runtime_source"] = 2
        devices[0]["mapping_method"] = "pnp_moniker"
        devices[0]["mapping_failure_reason"] = None
        manager.last_opened["phone_A"] = 0
        restored = manager.get_camera("phone_A")

        self.assertTrue(restored.status()["ready"])
        self.assertEqual(restored.source, 2)
        self.assertEqual(restored.configured_source, stable_id)
        self.assertEqual(restored.capture_backend, "dshow")
        self.assertEqual(
            len([camera for camera in ManagedCamera.instances if not camera.closed]),
            1,
        )

    def test_identity_decode_failure_is_camera_specific_and_preserves_config(self):
        stable_id = r"dshow:USB\VID_1234\CAMERA_ONE"

        def failed_discovery():
            raise UnicodeDecodeError("cp950", b"\x81", 0, 1, "truncated")

        manager = CameraManager(
            storage=self.storage,
            camera_factory=ManagedCamera,
            identity_discovery=failed_discovery,
        )
        manager.configs["phone_A"] = CameraConfig(
            "phone_A", stable_id, 0, True, "測試攝影機", stable_id
        )

        with self.assertRaises(CameraManagerError) as raised:
            manager.discover(force=True)

        self.assertEqual(raised.exception.error_code, "camera_discovery_failed")
        self.assertEqual(manager.config("phone_A").camera_id, stable_id)

    def test_disconnected_identity_waits_for_slow_poll_and_reopens_at_new_index(self):
        devices = [{"index": 0, "unique_id": "rog", "name": "ROG EYE S"}]
        discovery_calls = []

        def discover():
            discovery_calls.append(tuple(item["unique_id"] for item in devices))
            return [dict(item) for item in devices]

        manager = CameraManager(
            storage=self.storage,
            camera_factory=ManagedCamera,
            identity_discovery=discover,
            recovery_retry_seconds=3,
            slow_recovery_retry_seconds=15,
        )
        rog = avfoundation_camera_source("rog")
        manager.update("phone_A", camera_source=rog, rotation=0, enabled=True)
        original = manager.get_camera("phone_A")
        manager.discovery_cache = [{"source": rog, "available": True}]

        devices.clear()
        with patch("camera_manager.CAMERA_REOPEN_DELAY_SECONDS", 0):
            unavailable = manager.get_camera("phone_A")

        self.assertTrue(original.closed)
        self.assertFalse(unavailable.status()["ready"])
        self.assertIsNone(manager.discovery_cache)
        calls_while_disconnected = len(discovery_calls)

        devices.append({"index": 1, "unique_id": "rog", "name": "ROG EYE S"})
        self.assertIs(manager.get_camera("phone_A"), unavailable)
        self.assertEqual(len(discovery_calls), calls_while_disconnected)

        manager.last_opened["phone_A"] = 0
        restored = manager.get_camera("phone_A")

        self.assertTrue(restored.status()["ready"])
        self.assertEqual(restored.source, 1)
        self.assertEqual(restored.configured_source, rog)
        self.assertEqual(
            len([camera for camera in ManagedCamera.instances if not camera.closed]),
            1,
        )

    def test_shutdown_clears_recovery_state_and_closes_shared_capture_once(self):
        manager = self.manager()
        manager.update("phone_B", camera_source=0, rotation=0, enabled=True)
        shared = manager.get_camera("phone_A")
        self.assertIs(manager.get_camera("phone_B"), shared)
        manager.recovery_attempts.update(phone_A=3, phone_B=2)

        manager.release_all()

        self.assertTrue(shared.closed)
        self.assertEqual(manager.cameras, {})
        self.assertEqual(manager.last_opened, {})
        self.assertEqual(manager.recovery_attempts, {})

    def test_running_process_recovers_identity_at_new_index_without_restart(self):
        devices = [
            {"index": 0, "unique_id": "macbook", "name": "MacBook Air Camera"},
            {"index": 1, "unique_id": "rog", "name": "ROG EYE S"},
        ]
        rog = avfoundation_camera_source("rog")
        manager = CameraManager(
            storage=self.storage,
            default_source=0,
            camera_factory=LifecycleCamera,
            identity_discovery=lambda: [dict(item) for item in devices],
            recovery_retry_seconds=0,
        )
        manager.update(
            "phone_A",
            camera_source=rog,
            rotation=0,
            enabled=True,
            display_name="ROG EYE S",
        )
        original = manager.get_camera("phone_A")
        state = ControlState(
            Mock(), original, Mock(), camera_manager=manager
        )
        self.assertEqual(original.source, 1)

        original.started = False
        original.thread.is_alive.return_value = False
        manager.last_opened["phone_A"] = 0
        devices[:] = [
            {"index": 0, "unique_id": "rog", "name": "ROG EYE S"},
            {"index": 1, "unique_id": "macbook", "name": "MacBook Air Camera"},
        ]

        with patch("app.select_device", return_value=None):
            state.refresh()

        recovered = manager.get_camera("phone_A")
        self.assertIs(state.camera_manager, manager)
        self.assertIs(state.camera, recovered)
        self.assertIsNot(recovered, original)
        self.assertTrue(original.closed)
        self.assertEqual(recovered.source, 0)
        self.assertEqual(recovered.configured_source, rog)
        self.assertEqual(recovered.camera_name, "ROG EYE S")
        self.assertEqual(manager.config("phone_A").camera_source, rog)
        self.assertFalse(
            any(
                camera.source == 1 and not camera.closed
                for camera in LifecycleCamera.instances
            )
        )

    def test_exhausted_fast_recovery_uses_slower_retry_without_reopen_loop(self):
        devices = [{"index": 1, "unique_id": "rog", "name": "ROG EYE S"}]
        rog = avfoundation_camera_source("rog")
        manager = CameraManager(
            storage=self.storage,
            default_source=0,
            camera_factory=LifecycleCamera,
            identity_discovery=lambda: [dict(item) for item in devices],
            recovery_retry_seconds=0,
            recovery_max_attempts=1,
            slow_recovery_retry_seconds=15,
            recovery_reset_seconds=30,
        )
        manager.update(
            "phone_A",
            camera_source=rog,
            rotation=0,
            enabled=True,
            display_name="ROG EYE S",
        )
        manager.update(
            "phone_B",
            camera_source=rog,
            rotation=0,
            enabled=True,
            display_name="ROG EYE S",
        )
        original = manager.get_camera("phone_A")
        self.assertIs(manager.get_camera("phone_B"), original)
        manager.recovery_attempts["phone_B"] = 1
        original.thread.is_alive.return_value = False
        original.status = lambda: {"ready": False, "error": "lost"}
        manager.last_opened["phone_A"] = 0

        recovered = manager.get_camera("phone_A")
        recovered.thread.is_alive.return_value = False
        recovered.status = lambda: {"ready": False, "error": "lost again"}
        manager.last_opened["phone_A"] = time.monotonic()

        self.assertIs(manager.get_camera("phone_A"), recovered)
        self.assertEqual(manager.recovery_attempts["phone_A"], 1)
        self.assertEqual(manager.recovery_attempts["phone_B"], 1)
        self.assertEqual(
            len([camera for camera in LifecycleCamera.instances if camera.source == 1]),
            2,
        )

        manager.last_opened["phone_A"] = 0
        slow_recovered = manager.get_camera("phone_A")

        self.assertIsNot(slow_recovered, recovered)
        self.assertTrue(recovered.closed)
        self.assertEqual(manager.recovery_attempts["phone_A"], 1)
        self.assertEqual(
            len([camera for camera in LifecycleCamera.instances if camera.source == 1]),
            3,
        )

    def test_delayed_camera_readiness_recovers_after_fast_attempts_are_exhausted(self):
        devices = [{"index": 1, "unique_id": "rog", "name": "ROG EYE S"}]
        LifecycleCamera.plans[1] = [
            {"opened": False},
            {"opened": False},
            {"opened": False},
            {"opened": False},
            {"opened": True},
        ]
        manager = CameraManager(
            storage=self.storage,
            camera_factory=LifecycleCamera,
            identity_discovery=lambda: [dict(item) for item in devices],
            recovery_retry_seconds=3,
            recovery_max_attempts=3,
            slow_recovery_retry_seconds=15,
        )
        rog = avfoundation_camera_source("rog")
        manager.update("phone_A", camera_source=rog, rotation=0, enabled=True)

        camera = manager.get_camera("phone_A")
        for expected_attempts in (1, 2, 3):
            self.assertFalse(camera.wait_until_opened(0))
            manager.last_opened["phone_A"] = 0
            camera = manager.get_camera("phone_A")
            self.assertEqual(
                manager.recovery_attempts["phone_A"], expected_attempts
            )

        self.assertFalse(camera.wait_until_opened(0))
        manager.last_opened["phone_A"] = time.monotonic()
        self.assertIs(manager.get_camera("phone_A"), camera)
        self.assertEqual(LifecycleCamera.events.count(("start", 1)), 4)

        manager.last_opened["phone_A"] = 0
        restored = manager.get_camera("phone_A")
        self.assertTrue(restored.status()["ready"])
        self.assertTrue(camera.closed)
        self.assertEqual(LifecycleCamera.events.count(("start", 1)), 5)

        self.assertIs(manager.get_camera("phone_A"), restored)
        self.assertNotIn("phone_A", manager.recovery_attempts)

    def test_starting_camera_is_not_recreated_before_first_frame(self):
        devices = [{"index": 1, "unique_id": "rog", "name": "ROG EYE S"}]
        rog = avfoundation_camera_source("rog")
        manager = CameraManager(
            storage=self.storage,
            default_source=0,
            camera_factory=LifecycleCamera,
            identity_discovery=lambda: [dict(item) for item in devices],
            recovery_retry_seconds=0,
        )
        manager.update("phone_A", camera_source=rog, rotation=0, enabled=True)
        starting = manager.get_camera("phone_A")
        starting.open_succeeded = None
        starting.status = lambda: {"ready": False, "error": "Camera feed is stale."}
        manager.last_opened["phone_A"] = 0

        current = manager.get_camera("phone_A")

        self.assertIs(current, starting)
        self.assertFalse(starting.closed)
        self.assertEqual(LifecycleCamera.events.count(("start", 1)), 1)

    def test_previously_healthy_stale_camera_is_recreated(self):
        devices = [{"index": 1, "unique_id": "rog", "name": "ROG EYE S"}]
        rog = avfoundation_camera_source("rog")
        manager = CameraManager(
            storage=self.storage,
            default_source=0,
            camera_factory=LifecycleCamera,
            identity_discovery=lambda: [dict(item) for item in devices],
            recovery_retry_seconds=0,
        )
        manager.update("phone_A", camera_source=rog, rotation=0, enabled=True)
        stale = manager.get_camera("phone_A")
        stale.open_succeeded = True
        stale.status = lambda: {"ready": False, "error": "Camera feed is stale."}
        manager.last_opened["phone_A"] = 0

        with patch("camera_manager.CAMERA_REOPEN_DELAY_SECONDS", 0):
            recovered = manager.get_camera("phone_A")

        self.assertIsNot(recovered, stale)
        self.assertTrue(stale.closed)
        self.assertEqual(recovered.source, 1)
        self.assertEqual(recovered.configured_source, rog)

    def test_failed_camera_startup_is_recreated(self):
        devices = [{"index": 1, "unique_id": "rog", "name": "ROG EYE S"}]
        rog = avfoundation_camera_source("rog")
        manager = CameraManager(
            storage=self.storage,
            default_source=0,
            camera_factory=LifecycleCamera,
            identity_discovery=lambda: [dict(item) for item in devices],
            recovery_retry_seconds=0,
        )
        manager.update("phone_A", camera_source=rog, rotation=0, enabled=True)
        failed = manager.get_camera("phone_A")
        failed.open_succeeded = False
        failed.status = lambda: {"ready": False, "error": "Cannot open camera."}
        manager.last_opened["phone_A"] = 0

        with patch("camera_manager.CAMERA_REOPEN_DELAY_SECONDS", 0):
            recovered = manager.get_camera("phone_A")

        self.assertIsNot(recovered, failed)
        self.assertTrue(failed.closed)
        self.assertEqual(recovered.source, 1)

    def test_dead_capture_thread_is_recreated_while_start_state_is_unknown(self):
        devices = [{"index": 1, "unique_id": "rog", "name": "ROG EYE S"}]
        rog = avfoundation_camera_source("rog")
        manager = CameraManager(
            storage=self.storage,
            default_source=0,
            camera_factory=LifecycleCamera,
            identity_discovery=lambda: [dict(item) for item in devices],
            recovery_retry_seconds=0,
        )
        manager.update("phone_A", camera_source=rog, rotation=0, enabled=True)
        dead = manager.get_camera("phone_A")
        dead.open_succeeded = None
        dead.status = lambda: {"ready": False, "error": "Camera feed is stale."}
        dead.thread.is_alive.return_value = False
        manager.last_opened["phone_A"] = 0

        with patch("camera_manager.CAMERA_REOPEN_DELAY_SECONDS", 0):
            recovered = manager.get_camera("phone_A")

        self.assertIsNot(recovered, dead)
        self.assertTrue(dead.closed)
        self.assertEqual(recovered.source, 1)

    def test_two_stable_camera_assignments_never_cross_route(self):
        devices = [
            {"index": 0, "unique_id": "macbook", "name": "MacBook Air Camera"},
            {"index": 1, "unique_id": "rog", "name": "ROG EYE S"},
        ]
        rog = avfoundation_camera_source("rog")
        macbook = avfoundation_camera_source("macbook")
        manager = self.identity_manager(devices)
        manager.update("phone_A", camera_source=rog, rotation=0, enabled=True)
        manager.update("phone_B", camera_source=macbook, rotation=0, enabled=True)
        manager.update("phone_C", camera_source=rog, rotation=0, enabled=True)
        phone_a = manager.get_camera("phone_A")
        self.assertEqual(phone_a.source, 1)
        self.assertEqual(manager.get_camera("phone_B").source, 0)
        self.assertIs(manager.get_camera("phone_C"), phone_a)
        devices[:] = [
            {"index": 0, "unique_id": "rog", "name": "ROG EYE S"},
            {"index": 1, "unique_id": "macbook", "name": "MacBook Air Camera"},
        ]
        _, rog_frame = manager.test_source(rog)
        _, macbook_frame = manager.test_source(macbook)
        self.assertEqual(rog_frame, b"jpeg-1")
        self.assertEqual(macbook_frame, b"jpeg-0")

        reordered_rog = manager.get_camera("phone_A")
        reordered_macbook = manager.get_camera("phone_B")
        self.assertEqual(reordered_rog.source, 0)
        self.assertEqual(reordered_macbook.source, 1)
        self.assertIs(manager.get_camera("phone_C"), reordered_rog)
        self.assertIsNot(reordered_rog, reordered_macbook)
        self.assertEqual(
            len(
                {
                    id(camera)
                    for camera in manager.cameras.values()
                    if not camera.closed
                }
            ),
            2,
        )

    def test_identity_discovery_exposes_names_and_stable_sources(self):
        devices = [
            {"index": 0, "unique_id": "rog", "name": "ROG EYE S"},
            {"index": 1, "unique_id": "macbook", "name": "MacBook Air Camera"},
        ]
        manager = self.identity_manager(devices)
        manager.update(
            "phone_A",
            camera_source=avfoundation_camera_source("rog"),
            rotation=0,
            enabled=True,
        )
        manager.update(
            "phone_B",
            camera_source=avfoundation_camera_source("macbook"),
            rotation=0,
            enabled=True,
        )
        manager.get_camera("phone_A")
        manager.get_camera("phone_B")

        discovered = manager.discover(force=True)

        self.assertEqual(
            discovered,
            [
                {
                    "source": avfoundation_camera_source("rog"),
                    "stable_id": avfoundation_camera_source("rog"),
                    "runtime_source": 0,
                    "index": 0,
                    "name": "ROG EYE S",
                    "backend": "avfoundation",
                    "available": True,
                },
                {
                    "source": avfoundation_camera_source("macbook"),
                    "stable_id": avfoundation_camera_source("macbook"),
                    "runtime_source": 1,
                    "index": 1,
                    "name": "MacBook Air Camera",
                    "backend": "avfoundation",
                    "available": True,
                },
            ],
        )

    def test_discovery_keeps_unready_managed_identity_in_camera_picker(self):
        devices = [{"index": 0, "unique_id": "rog", "name": "ROG EYE S"}]
        manager = self.identity_manager(devices)
        rog = avfoundation_camera_source("rog")
        manager.update(
            "phone_A",
            camera_source=rog,
            rotation=0,
            enabled=True,
            display_name="ROG EYE S",
        )
        camera = manager.get_camera("phone_A")
        camera.started = False

        with patch("camera_manager.cv2.VideoCapture") as video_capture:
            unavailable = manager.discover(force=True)

        self.assertEqual(len(unavailable), 1)
        self.assertEqual(unavailable[0]["source"], rog)
        self.assertEqual(unavailable[0]["runtime_source"], 0)
        self.assertFalse(unavailable[0]["available"])
        video_capture.assert_not_called()

        camera.started = True
        available = manager.discover(force=True)
        self.assertTrue(available[0]["available"])

    def test_discovery_lists_configured_identity_while_physically_absent(self):
        devices = [{"index": 0, "unique_id": "rog", "name": "ROG EYE S"}]
        manager = self.identity_manager(devices)
        rog = avfoundation_camera_source("rog")
        manager.update(
            "phone_A",
            camera_source=rog,
            rotation=0,
            enabled=True,
            display_name="ROG EYE S",
        )
        devices.clear()

        with patch("camera_manager.cv2.VideoCapture") as video_capture:
            discovered = manager.discover(force=True)

        self.assertEqual(
            discovered,
            [
                {
                    "source": rog,
                    "stable_id": rog,
                    "runtime_source": None,
                    "name": "ROG EYE S",
                    "backend": "avfoundation",
                    "available": False,
                }
            ],
        )
        video_capture.assert_not_called()

    def test_stable_identity_persists_while_integer_configs_still_load(self):
        devices = [{"index": 0, "unique_id": "rog", "name": "ROG EYE S"}]
        stable = avfoundation_camera_source("rog")
        manager = self.identity_manager(devices)
        manager.update(
            "phone_A",
            camera_source=stable,
            rotation=90,
            enabled=True,
            display_name="ROG EYE S",
        )
        manager.update("legacy_phone", camera_source=7, rotation=0, enabled=False)

        restarted = self.identity_manager(devices)

        self.assertEqual(restarted.config("phone_A").camera_source, stable)
        self.assertEqual(restarted.config("phone_A").camera_id, stable)
        self.assertEqual(restarted.config("phone_A").display_name, "ROG EYE S")
        self.assertEqual(restarted.config("legacy_phone").camera_source, 7)

    def test_network_camera_url_remains_a_legacy_source(self):
        manager = self.manager()
        source = "rtsp://camera.example.test/live"

        manager.update("phone_A", camera_source=source, rotation=0, enabled=True)

        self.assertEqual(manager.config("phone_A").camera_source, source)
        self.assertIsNone(manager.config("phone_A").camera_id)
        self.assertEqual(manager.get_camera("phone_A").source, source)

    def test_same_source_assignment_is_a_strict_lifecycle_noop(self):
        manager = self.lifecycle_manager()
        camera = manager.get_camera("phone_A")
        before_events = list(LifecycleCamera.events)

        config, affected = manager.reassign(
            "phone_A", camera_source=0, rotation=0, enabled=True
        )

        self.assertEqual(config.camera_source, 0)
        self.assertEqual(affected, set())
        self.assertIs(manager.get_camera("phone_A"), camera)
        self.assertEqual(LifecycleCamera.events, before_events)
        self.assertEqual(manager.source_users, {0: {"phone_A"}})

    def test_successful_switch_stops_old_camera_then_waits_for_new_frame(self):
        manager = self.lifecycle_manager(startup_timeout=0.4)
        old = manager.get_camera("phone_A")

        manager.update("phone_A", camera_source=1, rotation=0, enabled=True)

        self.assertTrue(old.closed)
        self.assertEqual(
            LifecycleCamera.events[-3:],
            [("close", 0), ("start", 1), ("wait", 1, 0.4)],
        )
        self.assertEqual(manager.config("phone_A").camera_source, 1)
        self.assertEqual(manager.get_camera("phone_A").source, 1)

    def test_slow_startup_waits_for_first_frame_within_timeout(self):
        ready = threading.Event()
        LifecycleCamera.plans[1] = [{"opened": True, "ready_event": ready}]
        manager = self.lifecycle_manager(startup_timeout=0.4)
        manager.get_camera("phone_A")
        timer = threading.Timer(0.05, ready.set)
        timer.start()

        manager.update("phone_A", camera_source=1, rotation=0, enabled=True)
        timer.join(1)

        self.assertTrue(ready.is_set())
        self.assertIn(("wait", 1, 0.4), LifecycleCamera.events)
        self.assertEqual(manager.config("phone_A").camera_source, 1)

    def test_disable_enable_waits_for_delayed_frame_without_rollback(self):
        manager = self.lifecycle_manager(startup_timeout=0.4)
        original = manager.get_camera("phone_A")
        manager.update("phone_A", camera_source=0, rotation=0, enabled=False)
        events_after_disable = len(LifecycleCamera.events)
        instances_after_disable = len(LifecycleCamera.instances)
        ready = threading.Event()
        LifecycleCamera.plans[0] = [{"opened": True, "ready_event": ready}]
        timer = threading.Timer(0.05, ready.set)
        timer.start()

        manager.update("phone_A", camera_source=0, rotation=0, enabled=True)
        timer.join(1)

        enabled = manager.get_camera("phone_A")
        self.assertTrue(original.closed)
        self.assertTrue(ready.is_set())
        self.assertTrue(enabled.status()["ready"])
        self.assertFalse(enabled.closed)
        self.assertTrue(manager.config("phone_A").enabled)
        self.assertEqual(len(LifecycleCamera.instances), instances_after_disable + 1)
        self.assertEqual(
            LifecycleCamera.events[events_after_disable:],
            [("start", 0), ("wait", 0, 0.4)],
        )

    def test_enable_rolls_back_only_after_genuine_startup_timeout(self):
        manager = self.lifecycle_manager(startup_timeout=0.03)
        manager.get_camera("phone_A")
        manager.update("phone_A", camera_source=0, rotation=0, enabled=False)
        LifecycleCamera.plans[0] = [{"opened": None}]
        starts_before = LifecycleCamera.events.count(("start", 0))

        with self.assertRaisesRegex(CameraManagerError, "previous assignments"):
            manager.update("phone_A", camera_source=0, rotation=0, enabled=True)

        self.assertFalse(manager.config("phone_A").enabled)
        self.assertEqual(
            LifecycleCamera.events.count(("start", 0)), starts_before + 1
        )
        self.assertTrue(LifecycleCamera.instances[-1].closed)

    def test_enable_fails_when_capture_thread_dies_before_first_frame(self):
        manager = self.lifecycle_manager(startup_timeout=0.4)
        manager.get_camera("phone_A")
        manager.update("phone_A", camera_source=0, rotation=0, enabled=False)
        LifecycleCamera.plans[0] = [{"opened": None, "thread_alive": False}]
        with patch("camera_manager.CAMERA_REOPEN_DELAY_SECONDS", 0):
            with self.assertRaisesRegex(CameraManagerError, "previous assignments"):
                manager.update("phone_A", camera_source=0, rotation=0, enabled=True)

        self.assertFalse(manager.config("phone_A").enabled)

    def test_failed_switch_restores_previous_source_and_reports_restored(self):
        LifecycleCamera.plans[1] = [{"opened": False}]
        manager = self.lifecycle_manager()
        manager.get_camera("phone_A")

        with self.assertRaisesRegex(
            CameraManagerError, "previous assignments were restored"
        ):
            manager.update("phone_A", camera_source=1, rotation=0, enabled=True)

        self.assertEqual(manager.config("phone_A").camera_source, 0)
        self.assertEqual(manager.source_users, {0: {"phone_A"}})
        self.assertEqual(manager.get_camera("phone_A").source, 0)
        self.assertEqual(LifecycleCamera.events.count(("start", 1)), 1)

    def test_rollback_waits_for_old_source_startup(self):
        rollback_ready = threading.Event()
        LifecycleCamera.plans[0] = [
            {"opened": True},
            {"opened": True, "ready_event": rollback_ready},
        ]
        LifecycleCamera.plans[1] = [{"opened": False}]
        manager = self.lifecycle_manager(startup_timeout=0.4)
        manager.get_camera("phone_A")
        timer = threading.Timer(0.05, rollback_ready.set)
        timer.start()

        with self.assertRaisesRegex(CameraManagerError, "restored"):
            manager.update("phone_A", camera_source=1, rotation=0, enabled=True)
        timer.join(1)

        self.assertTrue(rollback_ready.is_set())
        self.assertEqual(manager.get_camera("phone_A").source, 0)
        self.assertIn(("wait", 0, 0.4), LifecycleCamera.events)

    def test_failed_rollback_has_distinct_error_and_keeps_old_config(self):
        LifecycleCamera.plans[0] = [
            {"opened": True},
            {"opened": False},
        ]
        LifecycleCamera.plans[1] = [{"opened": False}]
        manager = self.lifecycle_manager()
        manager.get_camera("phone_A")

        with self.assertRaisesRegex(
            CameraManagerError, "previous camera assignments could not be reopened"
        ):
            manager.update("phone_A", camera_source=1, rotation=0, enabled=True)

        self.assertEqual(manager.config("phone_A").camera_source, 0)
        self.assertEqual(manager.source_users, {0: {"phone_A"}})

    def test_watchdog_recovery_attempts_are_bounded(self):
        manager = self.lifecycle_manager(recovery_max_attempts=2)
        camera = manager.get_camera("phone_A")
        camera.thread.is_alive.return_value = False
        camera.status = lambda: {"ready": False, "error": "lost"}

        for _ in range(5):
            camera.thread.is_alive.return_value = False
            manager.get_camera("phone_A")

        self.assertEqual(manager.recovery_attempts["phone_A"], 2)
        self.assertEqual(LifecycleCamera.events.count(("start", 0)), 3)

    def test_watchdog_lookup_cannot_overlap_user_reassignment(self):
        manager = self.lifecycle_manager()
        old = manager.get_camera("phone_A")
        close_entered = threading.Event()
        allow_close = threading.Event()
        lookup_finished = threading.Event()
        original_close = old.close

        def blocking_close():
            close_entered.set()
            self.assertTrue(allow_close.wait(1))
            original_close()

        old.close = blocking_close
        switch = threading.Thread(
            target=lambda: manager.update(
                "phone_A", camera_source=1, rotation=0, enabled=True
            )
        )
        switch.start()
        self.assertTrue(close_entered.wait(1))
        lookup = threading.Thread(
            target=lambda: (manager.get_camera("phone_A"), lookup_finished.set())
        )
        lookup.start()
        self.assertFalse(lookup_finished.wait(0.05))
        allow_close.set()
        switch.join(1)
        lookup.join(1)

        self.assertFalse(switch.is_alive())
        self.assertFalse(lookup.is_alive())
        self.assertTrue(lookup_finished.is_set())
        self.assertEqual(manager.get_camera("phone_A").source, 1)

    def test_devices_resolve_different_sources_and_switch_keeps_monitoring(self):
        manager = self.manager()
        manager.update(
            "phone_B", camera_source=1, rotation=90, enabled=True
        )
        phone_a = manager.activate("phone_A")
        phone_b = manager.activate("phone_B")
        self.assertEqual((phone_a.source, phone_b.source), (0, 1))
        self.assertFalse(phone_a.closed)
        self.assertTrue(phone_b.started)
        self.assertEqual((phone_a.fps, phone_b.fps), (3, 15))
        restored_a = manager.activate("phone_A")
        self.assertIs(restored_a, phone_a)
        self.assertEqual(restored_a.source, 0)
        self.assertFalse(phone_b.closed)
        self.assertEqual((phone_a.fps, phone_b.fps), (15, 3))

    def test_equal_frame_sequences_remain_scoped_to_each_device_camera(self):
        manager = self.manager()
        manager.update("phone_B", camera_source=1, rotation=0, enabled=True)
        phone_a = manager.get_camera("phone_A")
        phone_b = manager.get_camera("phone_B")

        frame_a = phone_a.observation()
        frame_b = phone_b.observation()
        self.assertEqual(frame_a[1], frame_b[1])
        self.assertEqual(frame_a[0], b"jpeg-0")
        self.assertEqual(frame_b[0], b"jpeg-1")
        self.assertIs(manager.get_camera("phone_A"), phone_a)
        self.assertIs(manager.get_camera("phone_B"), phone_b)
        self.assertIsNot(phone_a, phone_b)

    def test_assignment_change_reloads_only_that_device(self):
        manager = self.manager()
        manager.update("phone_B", camera_source=1, rotation=0, enabled=True)
        phone_a = manager.get_camera("phone_A")
        phone_b = manager.get_camera("phone_B")
        manager.update("phone_B", camera_source=2, rotation=0, enabled=True)
        self.assertFalse(phone_a.closed)
        self.assertTrue(phone_b.closed)
        self.assertEqual(manager.get_camera("phone_B").source, 2)

    def test_rotation_change_rebuilds_selected_device_camera(self):
        manager = self.manager()
        original = manager.activate("phone_A")
        manager.update("phone_A", camera_source=0, rotation=90, enabled=True)
        rotated = manager.activate("phone_A")
        self.assertTrue(original.closed)
        self.assertIsNot(rotated, original)
        self.assertEqual(rotated.rotate, 90)

    def test_config_persists_and_cli_defaults_only_fill_missing_phone_a(self):
        manager = self.manager(source=4, rotation=90)
        self.assertEqual(manager.config("phone_A").camera_source, 4)
        manager.update("phone_A", camera_source=2, rotation=180, enabled=True)
        manager.update("branch_17", camera_source=7, rotation=270, enabled=False)
        restarted = self.manager(source=9, rotation=0)
        self.assertEqual(restarted.config("phone_A").camera_source, 2)
        self.assertEqual(restarted.config("phone_A").rotation, 180)
        self.assertEqual(restarted.config("branch_17").camera_source, 7)
        self.assertFalse(restarted.config("branch_17").enabled)

    def test_unassigned_and_unavailable_cameras_fail_cleanly(self):
        manager = self.manager()
        unassigned = manager.get_camera("phone_B")
        self.assertFalse(unassigned.status()["ready"])
        self.assertIn("No camera", unassigned.status()["error"])
        manager.update("phone_B", camera_source=99, rotation=0, enabled=True)
        unavailable = manager.get_camera("phone_B")
        self.assertFalse(unavailable.status()["ready"])
        self.assertIn("Cannot open", unavailable.status()["error"])
        self.assertTrue(manager.get_camera("phone_A").status()["ready"])

    def test_shared_physical_source_rejects_incompatible_rotation(self):
        manager = self.manager()
        with self.assertRaisesRegex(ValueError, "must use the same rotation"):
            manager.update("phone_B", camera_source=0, rotation=90, enabled=True)

    def test_joining_compatible_source_reuses_camera_without_moving_user(self):
        manager = self.manager()
        manager.update("phone_B", camera_source=1, rotation=0, enabled=True)
        phone_a = manager.get_camera("phone_A")
        phone_b = manager.get_camera("phone_B")
        instance_count = len(ManagedCamera.instances)

        config, affected = manager.reassign(
            "phone_A", camera_source=1, rotation=0, enabled=True
        )

        self.assertEqual(config.camera_source, 1)
        self.assertEqual(affected, {"phone_A"})
        self.assertEqual(manager.config("phone_B").camera_source, 1)
        self.assertEqual(manager.source_users, {1: {"phone_A", "phone_B"}})
        self.assertTrue(phone_a.closed)
        self.assertFalse(phone_b.closed)
        self.assertIs(manager.get_camera("phone_A"), phone_b)
        self.assertIs(manager.get_camera("phone_B"), phone_b)
        self.assertEqual(len(ManagedCamera.instances), instance_count)

    def test_leaving_shared_source_keeps_capture_until_last_user_releases(self):
        manager = self.manager()
        manager.update("phone_B", camera_source=0, rotation=0, enabled=True)
        shared = manager.get_camera("phone_A")
        self.assertIs(manager.get_camera("phone_B"), shared)

        with patch.object(shared, "close", wraps=shared.close) as close:
            manager.release_device("phone_A")
            close.assert_not_called()
            self.assertIs(manager.get_camera("phone_B"), shared)
            self.assertFalse(shared.closed)

            manager.release_device("phone_B")
            close.assert_called_once_with()
            self.assertTrue(shared.closed)

    def test_shared_join_does_not_touch_third_camera_or_duplicate_capture(self):
        manager = self.manager()
        manager.update("phone_B", camera_source=1, rotation=0, enabled=True)
        manager.update("phone_C", camera_source=2, rotation=0, enabled=True)
        old_a = manager.get_camera("phone_A")
        phone_b = manager.get_camera("phone_B")
        phone_c = manager.get_camera("phone_C")
        instance_count = len(ManagedCamera.instances)

        manager.update("phone_A", camera_source=1, rotation=0, enabled=True)

        self.assertEqual(manager.config("phone_A").camera_source, 1)
        self.assertEqual(manager.config("phone_B").camera_source, 1)
        self.assertEqual(
            manager.source_users,
            {1: {"phone_A", "phone_B"}, 2: {"phone_C"}},
        )
        self.assertTrue(old_a.closed)
        self.assertIs(manager.get_camera("phone_A"), phone_b)
        self.assertIs(manager.get_camera("phone_B"), phone_b)
        self.assertFalse(phone_b.closed)
        self.assertIs(manager.get_camera("phone_C"), phone_c)
        self.assertFalse(phone_c.closed)
        self.assertEqual(len(ManagedCamera.instances), instance_count)
        self.assertEqual(
            len(
                {
                    id(camera)
                    for camera in manager.cameras.values()
                    if not camera.closed
                }
            ),
            2,
        )

    def test_status_poll_waits_for_assignment_transaction(self):
        manager = self.manager()
        manager.update("phone_B", camera_source=1, rotation=0, enabled=True)
        manager.get_camera("phone_A")
        manager.get_camera("phone_B")
        save_entered = threading.Event()
        allow_save = threading.Event()
        status_finished = threading.Event()
        original_save = manager.storage.save

        def blocking_save(configs):
            save_entered.set()
            self.assertTrue(allow_save.wait(1))
            original_save(configs)

        manager.storage.save = blocking_save
        swap = threading.Thread(
            target=lambda: manager.update(
                "phone_A", camera_source=1, rotation=0, enabled=True
            )
        )
        swap.start()
        self.assertTrue(save_entered.wait(1))
        poll = threading.Thread(
            target=lambda: (
                manager.status("phone_B"),
                status_finished.set(),
            )
        )
        poll.start()
        self.assertFalse(status_finished.wait(0.05))
        allow_save.set()
        swap.join(1)
        poll.join(1)
        self.assertFalse(swap.is_alive())
        self.assertFalse(poll.is_alive())
        self.assertTrue(status_finished.is_set())
        self.assertEqual(manager.source_users, {1: {"phone_A", "phone_B"}})

    def test_discovery_returns_only_opened_readable_indexes_and_caches(self):
        manager = self.manager()
        captures = []

        def video_capture(source, backend):
            capture = Mock()
            capture.isOpened.return_value = source in {0, 1}
            capture.read.return_value = (
                (True, np.zeros((2, 3, 3), dtype=np.uint8))
                if source == 0
                else (False, None)
            )
            captures.append(capture)
            return capture

        with patch("camera_manager.cv2.VideoCapture", side_effect=video_capture) as factory:
            first = manager.discover()
            cached = manager.discover()
            refreshed = manager.discover(force=True)

        self.assertEqual(first, [{"source": 0, "available": True}])
        self.assertEqual(cached, first)
        self.assertEqual(refreshed, first)
        self.assertEqual(factory.call_count, 22)
        self.assertTrue(all(capture.release.call_count == 1 for capture in captures))

    def test_preview_api_never_cross_routes_frames_after_avfoundation_reorder(self):
        devices = [
            {"index": 0, "unique_id": "rog", "name": "ROG EYE S"},
            {"index": 1, "unique_id": "macbook", "name": "MacBook Air Camera"},
        ]
        manager = CameraManager(
            storage=self.storage,
            default_source=0,
            camera_factory=IdentityFrameCamera,
            identity_discovery=lambda: [dict(item) for item in devices],
            recovery_retry_seconds=0,
        )
        rog = avfoundation_camera_source("rog")
        macbook = avfoundation_camera_source("macbook")
        manager.update("phone_A", camera_source=rog, rotation=90, enabled=True)
        manager.update("phone_B", camera_source=macbook, rotation=0, enabled=True)
        camera_a = manager.get_camera("phone_A")
        camera_b = manager.get_camera("phone_B")
        self.assertIsNot(camera_a, camera_b)

        pi = Mock(spec=PiApiClient)
        pi.base_url = "http://pi.test:8766"
        pi.device_id = "phone_A"
        pi.status.return_value = {"ok": True, "connected": True}
        pi.devices.return_value = {
            "ok": True,
            "default_device": "phone_A",
            "devices": [
                {"id": "phone_A", "connected": True},
                {"id": "phone_B", "connected": True},
            ],
            "enrollment": {"active": False},
        }
        app = create_app(
            camera_a,
            Mock(spec=AndroidControl),
            DeviceLocks(self.root / "locks"),
            WorkflowStorage(self.root / "workflows"),
            manual_backend="raspberry_pi",
            pi_client=pi,
            calibration_storage=CalibrationStorage(self.root / "calibration.json"),
            camera_manager=manager,
        )
        client = app.test_client()
        authenticate_admin(client)
        headers = {
            "X-Control-Token": app.config["CONTROL_TOKEN"],
            "X-Control-Client": "camera-browser",
        }

        self.assertEqual(
            (
                initial_rog := client.get(
                    "/api/devices/phone_A/camera/frame", headers=headers
                )
            ).data,
            b"jpeg-avfoundation:rog",
        )
        self.assertEqual(initial_rog.headers["X-Camera-Device-Id"], "phone_A")
        self.assertEqual(
            (
                initial_macbook := client.get(
                    "/api/devices/phone_B/camera/frame", headers=headers
                )
            ).data,
            b"jpeg-avfoundation:macbook",
        )
        self.assertEqual(initial_macbook.headers["X-Camera-Device-Id"], "phone_B")

        devices[:] = [
            {"index": 0, "unique_id": "macbook", "name": "MacBook Air Camera"}
        ]
        with patch("camera_manager.CAMERA_REOPEN_DELAY_SECONDS", 0):
            missing_rog = client.get(
                "/api/devices/phone_A/camera/frame", headers=headers
            )
            live_macbook = client.get(
                "/api/devices/phone_B/camera/frame", headers=headers
            )

        self.assertEqual(missing_rog.status_code, 503)
        self.assertNotIn(b"macbook", missing_rog.data.lower())
        self.assertEqual(live_macbook.status_code, 200)
        self.assertEqual(live_macbook.data, b"jpeg-avfoundation:macbook")
        self.assertIsNot(
            manager.cameras["phone_A"], manager.cameras["phone_B"]
        )

        devices.append({"index": 1, "unique_id": "rog", "name": "ROG EYE S"})
        manager.last_opened["phone_A"] = 0
        restored_rog = client.get(
            "/api/devices/phone_A/camera/frame", headers=headers
        )

        self.assertEqual(restored_rog.status_code, 200)
        self.assertEqual(restored_rog.data, b"jpeg-avfoundation:rog")
        self.assertEqual(manager.cameras["phone_A"].source, 1)
        self.assertEqual(manager.cameras["phone_B"].source, 0)
        self.assertIsNot(
            manager.cameras["phone_A"], manager.cameras["phone_B"]
        )

    def test_api_selection_preview_update_and_single_ocr_provider(self):
        manager = self.manager()
        manager.update("phone_B", camera_source=1, rotation=90, enabled=True)
        camera = manager.activate("phone_A")
        pi = Mock(spec=PiApiClient)
        pi.base_url = "http://pi.test:8766"
        pi.device_id = "phone_A"
        pi.status.return_value = {"ok": True, "connected": True}
        pi.devices.return_value = {
            "ok": True,
            "default_device": "phone_A",
            "devices": [
                {"id": "phone_A", "connected": True},
                {"id": "phone_B", "connected": True},
            ],
            "enrollment": {"active": False},
        }
        app = create_app(
            camera,
            Mock(spec=AndroidControl),
            DeviceLocks(self.root / "locks"),
            WorkflowStorage(self.root / "workflows"),
            manual_backend="raspberry_pi",
            pi_client=pi,
            calibration_storage=CalibrationStorage(self.root / "calibration.json"),
            camera_manager=manager,
        )
        client = app.test_client()
        authenticate_admin(client)
        headers = {
            "X-Control-Token": app.config["CONTROL_TOKEN"],
            "X-Control-Client": "camera-browser",
        }
        provider = app.extensions["workflow_screen_state_provider"]
        camera_response = client.get(
            "/api/devices/phone_A/camera", headers=headers
        )
        self.assertEqual(camera_response.status_code, 200, camera_response.json)
        self.assertEqual(camera_response.content_type, "application/json")
        self.assertTrue(camera_response.json["selected"])
        self.assertIn("config", camera_response.json)
        self.assertIn("status", camera_response.json)
        frame_a = client.get("/api/devices/phone_A/camera/frame", headers=headers)
        self.assertEqual(frame_a.data, b"jpeg-0")
        selected = client.post(
            "/api/devices/select",
            json={"device_id": "phone_B"},
            headers=headers,
        )
        self.assertEqual(selected.status_code, 200, selected.json)
        self.assertEqual(app.extensions["control_state"].camera.source, 1)
        self.assertIs(app.extensions["workflow_screen_state_provider"], provider)
        self.assertEqual(
            client.get("/api/devices/phone_B/camera/frame", headers=headers).data,
            b"jpeg-1",
        )
        self.assertEqual(
            client.get("/api/devices/phone_A/camera/frame", headers=headers).data,
            b"jpeg-0",
        )
        tested = client.post(
            "/api/cameras/test",
            json={"camera_source": 1, "rotation": 90},
            headers=headers,
        )
        self.assertEqual(tested.status_code, 200, tested.json)
        self.assertTrue(tested.json["preview"].startswith("data:image/jpeg;base64,"))

        unavailable = Mock()
        unavailable.isOpened.return_value = False
        with patch("camera_manager.cv2.VideoCapture", return_value=unavailable):
            failed_test = client.post(
                "/api/cameras/test",
                json={"camera_source": 8, "rotation": 0},
                headers=headers,
            )
        self.assertEqual(failed_test.status_code, 422)
        self.assertEqual(failed_test.content_type, "application/json")
        self.assertFalse(failed_test.json["ok"])
        unavailable.release.assert_called_once_with()

        with patch(
            "camera_manager.cv2.VideoCapture",
            side_effect=cv2.error("probe failure"),
        ) as video_capture:
            discovery = client.get("/api/cameras", headers=headers)
            cached_discovery = client.get("/api/cameras", headers=headers)
        self.assertEqual(discovery.status_code, 200, discovery.json)
        self.assertEqual(cached_discovery.status_code, 200, cached_discovery.json)
        self.assertEqual(cached_discovery.json["cameras"], discovery.json["cameras"])
        self.assertEqual(video_capture.call_count, 9)

        self.assertEqual(
            discovery.json["cameras"],
            [
                {"source": 0, "available": True},
                {"source": 1, "available": True},
            ],
        )

        swapped = client.put(
            "/api/devices/phone_B/camera",
            json={"camera_source": 0, "rotation": 0, "enabled": True},
            headers=headers,
        )
        self.assertEqual(swapped.status_code, 200, swapped.json)
        self.assertEqual(swapped.json["affected_devices"], ["phone_B"])
        self.assertTrue(swapped.json["selected_camera_changed"])
        self.assertEqual(manager.config("phone_A").camera_source, 0)
        self.assertEqual(manager.config("phone_B").camera_source, 0)
        self.assertIs(
            manager.get_camera("phone_A"), manager.get_camera("phone_B")
        )
        self.assertEqual(app.extensions["control_state"].camera.source, 0)

        with patch.object(manager.storage, "save", side_effect=OSError("disk full")):
            storage_failure = client.put(
                "/api/devices/phone_B/camera",
                json={"camera_source": 3, "rotation": 0, "enabled": True},
                headers=headers,
            )
        self.assertEqual(storage_failure.status_code, 503)
        self.assertEqual(storage_failure.content_type, "application/json")
        self.assertFalse(storage_failure.json["ok"])
        updated = client.put(
            "/api/devices/phone_B/camera",
            json={"camera_source": 3, "rotation": 0, "enabled": True},
            headers=headers,
        )
        self.assertEqual(updated.status_code, 200, updated.json)
        self.assertTrue(updated.json["calibration_revalidation_required"])
        self.assertEqual(app.extensions["control_state"].camera.source, 3)
        self.assertFalse(client.get("/api/status", headers=headers).json["calibrated"])

    def test_shared_camera_has_independent_calibrated_cached_previews(self):
        manager = self.manager()
        manager.update("phone_B", camera_source=0, rotation=0, enabled=True)
        manager.update("phone_C", camera_source=0, rotation=0, enabled=True)
        camera_a = manager.get_camera("phone_A")
        camera_b = manager.get_camera("phone_B")
        self.assertIs(camera_a, camera_b)
        self.assertEqual(len(ManagedCamera.instances), 1)

        image = np.zeros((240, 320, 3), dtype=np.uint8)
        image[:, :160] = (0, 0, 255)
        image[:, 160:] = (0, 255, 0)
        encoded, jpeg = cv2.imencode(".jpg", image)
        self.assertTrue(encoded)
        camera_a.width, camera_a.height = 320, 240
        camera_a.frame = lambda: (jpeg.tobytes(), 501, 320, 240)

        pi = Mock(spec=PiApiClient)
        pi.base_url = "http://pi.test:8766"
        pi.device_id = "phone_A"
        pi.status.return_value = {"ok": True, "connected": True}
        pi.devices.return_value = {
            "ok": True,
            "default_device": "phone_A",
            "devices": [
                {"id": name, "connected": True}
                for name in ("phone_A", "phone_B", "phone_C")
            ],
            "enrollment": {"active": False},
        }
        calibrations = CalibrationStorage(self.root / "calibration.json")
        serial = "pi-api:" + pi.base_url
        calibrations.save(
            Calibration([[0, 0], [0.5, 0], [0.5, 1], [0, 1]]),
            camera_a,
            camera_a.status(),
            PiControlTarget(serial, device_id="phone_A"),
        )
        calibrations.save(
            Calibration([[0.5, 0], [1, 0], [1, 1], [0.5, 1]]),
            camera_b,
            camera_b.status(),
            PiControlTarget(serial + "#phone_B", device_id="phone_B"),
        )
        app = create_app(
            camera_a,
            Mock(spec=AndroidControl),
            DeviceLocks(self.root / "locks"),
            WorkflowStorage(self.root / "workflows"),
            manual_backend="raspberry_pi",
            pi_client=pi,
            calibration_storage=calibrations,
            camera_manager=manager,
        )
        client = app.test_client()
        authenticate_admin(client)

        resolved_calibrations = []
        original_load = calibrations.load

        def track_calibration_load(*args):
            result = original_load(*args)
            resolved_calibrations.append(result)
            return result

        with patch.object(
            calibrations, "load", side_effect=track_calibration_load
        ) as calibration_load:
            started = app.extensions["workflow_service"].start(
                {
                    **new_workflow("Phone B calibration check"),
                    "steps": [
                        {"id": "wait", "type": "wait", "seconds": 0.01}
                    ],
                },
                {},
                device_id="phone_B",
            )
            completed = app.extensions["workflow_service"].wait(
                started["run_id"], timeout=3
            )
        self.assertEqual(completed["status"], "passed")
        calibration_load.assert_called_once()
        resolved_device = calibration_load.call_args.args[2]
        self.assertEqual(resolved_device.device_id, "phone_B")
        self.assertEqual(
            resolved_calibrations[0].corners.tolist(),
            [[0.5, 0], [1, 0], [1, 1], [0.5, 1]],
        )

        calibration_paths = {
            calibrations.path_for(
                PiControlTarget(serial, device_id="phone_A")
            ),
            calibrations.path_for(
                PiControlTarget(serial + "#phone_B", device_id="phone_B")
            ),
        }
        opened_calibrations = []
        original_open = Path.open

        def tracked_open(path, *args, **kwargs):
            if path in calibration_paths:
                opened_calibrations.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", autospec=True, side_effect=tracked_open):
            preview_a = client.get("/api/devices/phone_A/camera/preview")
            preview_b = client.get("/api/devices/phone_B/camera/preview")
            cached_a = client.get("/api/devices/phone_A/camera/preview")

        self.assertEqual(opened_calibrations, [])

        self.assertEqual(preview_a.status_code, 200, preview_a.json)
        self.assertEqual(preview_b.status_code, 200, preview_b.json)
        self.assertEqual(preview_a.headers["X-Frame-Id"], "501")
        self.assertEqual(
            preview_a.headers["X-Preview-Coordinate-Space"],
            "screen_normalized",
        )
        self.assertEqual(preview_a.headers["X-Preview-Cache"], "miss")
        self.assertEqual(preview_b.headers["X-Preview-Cache"], "miss")
        self.assertEqual(cached_a.headers["X-Preview-Cache"], "hit")
        self.assertEqual(cached_a.data, preview_a.data)
        self.assertNotEqual(preview_a.data, preview_b.data)
        decoded_a = cv2.imdecode(
            np.frombuffer(preview_a.data, np.uint8), cv2.IMREAD_COLOR
        )
        decoded_b = cv2.imdecode(
            np.frombuffer(preview_b.data, np.uint8), cv2.IMREAD_COLOR
        )
        self.assertGreater(decoded_a[:, :, 2].mean(), decoded_a[:, :, 1].mean())
        self.assertGreater(decoded_b[:, :, 1].mean(), decoded_b[:, :, 2].mean())
        self.assertEqual(decoded_a.shape, decoded_b.shape)
        self.assertEqual(len(ManagedCamera.instances), 1)

        calibrations.save(
            Calibration([[0.5, 0], [1, 0], [1, 1], [0.5, 1]]),
            camera_a,
            camera_a.status(),
            PiControlTarget(serial, device_id="phone_A"),
        )
        recalibrated_a = client.get("/api/devices/phone_A/camera/preview")
        self.assertEqual(recalibrated_a.headers["X-Preview-Cache"], "miss")
        self.assertNotEqual(recalibrated_a.data, preview_a.data)

        raw = client.get("/api/devices/phone_A/camera/frame")
        self.assertEqual(raw.data, jpeg.tobytes())
        missing = client.get("/api/devices/phone_C/camera/preview")
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.json["error"], "Calibration required for this phone.")


if __name__ == "__main__":
    unittest.main()
