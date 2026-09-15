"""Run with python -m unittest discover -s tests -v. No camera or phone is touched."""

import time
import unittest
from unittest.mock import patch

import numpy as np

from android_control import AndroidControl, Device, DeviceError, parse_size
from app import create_app
from calibration import Calibration
from camera import Camera


CORNERS = [[0.2, 0.1], [0.8, 0.2], [0.7, 0.9], [0.1, 0.8]]


class GeometryTests(unittest.TestCase):
    def test_corners_and_perspective_interior(self):
        corners = [[0.25, 0.1], [0.9, 0.2], [0.65, 0.95], [0.1, 0.7]]
        calibration = Calibration(corners)
        for point, expected in zip(corners, [(0, 0), (1079, 0), (1079, 2399), (0, 2399)]):
            pixel, _ = calibration.pixel(point, 1080, 2400)
            self.assertEqual(pixel, expected)
        # Project an interior screen point back into a tilted camera view.
        camera = np.linalg.inv(calibration.transform) @ [0.3, 0.65, 1]
        pixel, normalized = calibration.pixel((camera[:2] / camera[2]).tolist(), 1080, 2400)
        np.testing.assert_allclose(normalized, [0.3, 0.65], atol=1e-9)
        self.assertEqual(pixel, (324, 1559))

    def test_reject_invalid_calibration_and_points(self):
        for corners in ([], [[0, 0]] * 4, [[0, 0], [1, 1], [1, 0], [0, 1]],
                        [[0, 0], [1, 0], [.3, .1], [0, 1]],
                        [[0, 0], [.01, 0], [.01, .01], [0, .01]]):
            with self.subTest(corners=corners), self.assertRaises(ValueError):
                Calibration(corners)
        calibration = Calibration(CORNERS)
        for point in ([0, 0], [float("nan"), .5], [True, .5], [-1, .5], [".3", .5]):
            with self.subTest(point=point), self.assertRaises(ValueError):
                calibration.map(point)


class FakeCamera:
    ready = True
    width, height = 1280, 720

    def status(self):
        return dict(ready=self.ready, index=0, width=self.width, height=self.height,
                    error="" if self.ready else "Camera disconnected")

    def validate_frame(self, number):
        if number != 123 or not self.ready:
            raise ValueError("Displayed camera frame is stale.")

    def frame(self):
        return b"fake-jpeg", 123, self.width, self.height


class FakeAndroid:
    def __init__(self):
        self.current = Device("usb-test", 1080, 2400, 0)
        self.actions = []
        self.failure = None

    def device(self):
        if self.failure:
            raise DeviceError(self.failure)
        return self.current

    def send(self, device, action, pixel=None):
        self.actions.append((device.serial, action, pixel))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.camera, self.android = FakeCamera(), FakeAndroid()
        self.app = create_app(self.camera, self.android)
        self.app.testing = True
        self.client = self.app.test_client()
        self.headers = {"X-Control-Token": self.app.config["CONTROL_TOKEN"],
                        "X-Control-Client": "browser-test-123"}

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    def calibrate(self):
        return self.post("/api/calibration", dict(corners=CORNERS, frame_id=123))

    def enable(self):
        self.assertEqual(self.calibrate().status_code, 200)
        self.assertEqual(self.post("/api/control", dict(enabled=True, frame_id=123)).status_code, 200)

    def action(self, **extra):
        return self.post("/api/action", dict(action="tap", x=.45, y=.5, frame_id=123, **extra))

    def test_disabled_by_default_and_after_calibration(self):
        self.assertEqual(self.action().status_code, 400)
        self.assertEqual(self.calibrate().status_code, 200)
        self.assertEqual(self.action().status_code, 400)
        self.assertEqual(self.android.actions, [])

    def test_enable_requires_calibration(self):
        result = self.post("/api/control", dict(enabled=True, frame_id=123))
        self.assertEqual(result.status_code, 400)

    def test_manual_tap_and_buttons(self):
        self.enable()
        result = self.action()
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json["pixel"], [540, 1200])
        for name in ("back", "home", "swipe_up", "swipe_down"):
            result = self.post("/api/action", dict(action=name, frame_id=123))
            self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.android.actions), 5)
        self.assertEqual(self.android.actions[0], ("usb-test", "tap", (540, 1200)))

    def test_outside_click_rejected(self):
        self.enable()
        result = self.post("/api/action", dict(action="tap", x=0, y=0, frame_id=123))
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.android.actions, [])

    def test_disabled_switch_and_expired_lease(self):
        self.enable()
        self.post("/api/control", dict(enabled=False))
        self.assertEqual(self.action().status_code, 400)
        self.enable()
        self.app.extensions["control_state"].expires = time.monotonic() - 1
        self.assertEqual(self.action().status_code, 400)
        self.assertEqual(self.android.actions, [])

    def test_stale_frame_disarms(self):
        self.enable()
        self.assertEqual(self.post("/api/action", dict(action="back", frame_id=122)).status_code, 400)
        self.assertIsNone(self.app.extensions["control_state"].owner)
        self.assertEqual(self.android.actions, [])

    def test_device_change_disconnect_and_camera_change_disarm(self):
        for kind in ("serial", "rotation", "disconnect", "camera", "camera_size"):
            with self.subTest(kind=kind):
                self.setUp()
                self.enable()
                if kind == "serial": self.android.current = Device("new-phone", 1080, 2400, 0)
                if kind == "rotation": self.android.current = Device("usb-test", 2400, 1080, 1)
                if kind == "disconnect": self.android.failure = "Phone disconnected"
                if kind == "camera": self.camera.ready = False
                if kind == "camera_size": self.camera.width = 640
                self.assertIn(self.action().status_code, (400, 409))
                self.assertIsNone(self.app.extensions["control_state"].owner)
                self.assertIsNone(self.app.extensions["control_state"].calibration)
                self.assertEqual(self.android.actions, [])

    def test_other_browser_cannot_send_or_take_control(self):
        self.enable()
        self.headers["X-Control-Client"] = "other-browser-123"
        self.assertEqual(self.action().status_code, 400)
        self.assertEqual(self.post("/api/control", dict(enabled=True, frame_id=123)).status_code, 400)
        self.assertEqual(self.android.actions, [])

    def test_no_shell_or_text_input_endpoint(self):
        self.enable()
        for name in ("shell", "text", "login", "proceed", "payment"):
            result = self.post("/api/action", dict(action=name, frame_id=123))
            self.assertEqual(result.status_code, 400)
        self.assertEqual(self.android.actions, [])

    def test_local_request_protection(self):
        self.assertEqual(self.client.post("/api/control", json={"enabled": True}).status_code, 403)
        headers = dict(self.headers, **{"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(self.client.get("/api/frame", headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/", headers={"Host": "attacker.example"}).status_code, 400)

    def test_adb_failure_disables_no_retry(self):
        self.enable()
        with patch.object(self.android, "send", side_effect=DeviceError("USB lost")) as send:
            self.assertEqual(self.action().status_code, 409)
            send.assert_called_once()
        self.assertIsNone(self.app.extensions["control_state"].owner)

    def test_overlapping_commands_are_rejected(self):
        self.enable()
        gate = self.app.extensions["control_state"].action_lock
        gate.acquire()
        try:
            self.assertEqual(self.action().status_code, 409)
            self.assertEqual(self.android.actions, [])
        finally:
            gate.release()

    def test_frame_and_html(self):
        self.assertEqual(self.client.get("/api/frame").headers["X-Frame-Id"], "123")
        page = self.client.get("/")
        self.assertIn(b"Local control", page.data)
        self.assertEqual(page.headers["X-Frame-Options"], "DENY")


class AdbTests(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(parse_size("Physical size: 1080x2400\nOverride size: 720x1600"), (720, 1600))
        with self.assertRaises(DeviceError): parse_size("unknown")

    def test_usb_device_and_rotation(self):
        adb = AndroidControl()
        with patch.object(adb, "_adb", side_effect=["List of devices attached\nusb123 device usb:1-1", "0", "Physical size: 1080x2400", "SurfaceOrientation: 1"]):
            self.assertEqual(adb.device(), Device("usb123", 2400, 1080, 1))

    def test_rotation_fallback(self):
        adb = AndroidControl()
        with patch.object(adb, "_adb", side_effect=["List of devices attached\nusb123 device usb:1-1", "0", "Physical size: 1080x2400", "", "mRotation=ROTATION_270"]):
            self.assertEqual(adb.device(), Device("usb123", 2400, 1080, 3))

    def test_reject_missing_multiple_unauthorized_network_and_emulator(self):
        for rows in ("", "a device usb:1\nb device usb:2", "a unauthorized usb:1", "a offline usb:1", "10.0.0.1:5555 device", "emulator-5554 device"):
            with self.subTest(rows=rows), patch.object(AndroidControl, "_adb", return_value="List of devices attached\n" + rows):
                with self.assertRaises(DeviceError): AndroidControl().device()

    def test_commands_use_selected_serial_and_fixed_arguments(self):
        adb = AndroidControl()
        device = Device("usb123", 1080, 2400, 0)
        with patch.object(adb, "_adb") as command:
            adb.send(device, "tap", (50, 100))
            command.assert_called_once_with("-s", "usb123", "shell", "input", "tap", "50", "100")
            command.reset_mock()
            adb.send(device, "swipe_up")
            command.assert_called_once_with("-s", "usb123", "shell", "input", "swipe", "540", "1799", "540", "600", "300")


class CameraFreshnessTests(unittest.TestCase):
    def test_fresh_and_stale_frames_without_opening_camera(self):
        camera = Camera()
        camera.latest = (b"frame", 1, 1280, 720)
        camera.timestamps.append((1, time.monotonic(), 1280, 720))
        self.assertTrue(camera.status()["ready"])
        camera.validate_frame(1)
        with self.assertRaises(ValueError): camera.validate_frame(True)
        camera.timestamps.clear()
        camera.timestamps.append((1, time.monotonic() - 3, 1280, 720))
        self.assertFalse(camera.status()["ready"])
        with self.assertRaises(ValueError): camera.validate_frame(1)

    def test_old_resolution_frame_is_rejected(self):
        camera = Camera()
        camera.latest = (b"frame", 2, 640, 480)
        camera.timestamps.append((1, time.monotonic(), 1280, 720))
        with self.assertRaises(ValueError): camera.validate_frame(1)


if __name__ == "__main__":
    unittest.main()
