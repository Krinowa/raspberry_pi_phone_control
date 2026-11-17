"""Local camera calibration persistence and compatibility checks."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from android_control import AndroidControl, Device
from app import create_app
from automation.workflows.storage import WorkflowStorage
from calibration import Calibration, CalibrationStorage
from device_lock import DeviceLocks
from hardware.pi_api_client import PiApiClient
from hardware.raspberry_pi_input import PiControlTarget
from test_control import CORNERS, FakeCamera, authenticate_admin


DEVICE = Device("usb-calibration", 1080, 2400, 0)


class PersistentCamera(FakeCamera):
    def __init__(self, source=0, rotate=0):
        super().__init__()
        self.source = source
        self.rotate = rotate


class CalibrationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.path = root / "camera_data" / "calibration.json"
        self.storage = CalibrationStorage(self.path)
        self.workflows = WorkflowStorage(root / "workflows")
        self.locks = DeviceLocks(root / "locks")
        self.controller = Mock(spec=AndroidControl)
        self.controller.device.return_value = DEVICE

    def app(self, camera=None):
        return create_app(
            camera or PersistentCamera(),
            self.controller,
            self.locks,
            self.workflows,
            manual_backend="appium",
            calibration_storage=self.storage,
        )

    @staticmethod
    def headers(app):
        return {
            "X-Control-Client": "calibration-browser",
            "X-Control-Token": app.config["CONTROL_TOKEN"],
        }

    def calibrate(self, app, corners=CORNERS):
        client = authenticate_admin(app.test_client())
        return client.post(
            "/api/calibration",
            json={"corners": corners, "frame_id": 123},
            headers=self.headers(app),
        )

    def status(self, app):
        return app.test_client().get(
            "/api/status",
            headers=self.headers(app),
        )

    def test_save_and_restore_after_backend_restart(self):
        first = self.app()
        response = self.calibrate(first)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(self.status(first).json["calibrated"])
        dashboard_client = authenticate_admin(first.test_client())
        self.assertEqual(dashboard_client.get("/").status_code, 200)
        self.assertTrue(self.status(first).json["calibrated"])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["version"], 1)
        self.assertEqual(
            saved["camera"],
            {"source": 0, "rotation": 0, "width": 1280, "height": 720},
        )
        self.assertEqual(saved["device"], {
            "serial": DEVICE.serial,
            "width": DEVICE.width,
            "height": DEVICE.height,
            "rotation": DEVICE.rotation,
        })
        self.assertEqual(saved["corners"], CORNERS)
        self.assertIn("updated_at", saved)

        restarted = self.app()
        restored = self.status(restarted)
        self.assertEqual(restored.status_code, 200)
        self.assertTrue(restored.json["calibrated"])
        self.assertEqual(restored.json["corners"], CORNERS)

    def test_resolution_source_rotation_and_phone_changes_are_rejected(self):
        self.assertEqual(self.calibrate(self.app()).status_code, 200)
        cases = []
        resolution = PersistentCamera()
        resolution.width = 640
        cases.append(resolution)
        cases.append(PersistentCamera(source=1))
        cases.append(PersistentCamera(rotate=90))
        for camera in cases:
            with self.subTest(camera=camera.status() | {"rotate": camera.rotate}):
                self.assertFalse(self.status(self.app(camera)).json["calibrated"])

        other_controller = Mock(spec=AndroidControl)
        other_controller.device.return_value = Device("other", 1080, 2400, 0)
        app = create_app(
            PersistentCamera(),
            other_controller,
            self.locks,
            self.workflows,
            manual_backend="appium",
            calibration_storage=self.storage,
        )
        self.assertFalse(self.status(app).json["calibrated"])

    def test_corrupt_file_is_ignored_and_delete_clears_saved_calibration(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not-json")
        response = self.status(self.app())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["calibrated"])

        app = self.app()
        self.assertEqual(self.calibrate(app).status_code, 200)
        self.assertTrue(self.path.exists())
        cleared = authenticate_admin(app.test_client()).delete(
            "/api/calibration",
            json={},
            headers=self.headers(app),
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.status(self.app()).json["calibrated"])

    def test_recalibration_replaces_saved_corners(self):
        app = self.app()
        self.assertEqual(self.calibrate(app).status_code, 200)
        replacement = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
        self.assertEqual(self.calibrate(app, replacement).status_code, 200)
        self.assertEqual(json.loads(self.path.read_text())["corners"], replacement)

    def test_compatible_load_is_cached_and_success_is_logged_only_once(self):
        camera = PersistentCamera()
        camera_info = camera.status()
        writer = CalibrationStorage(self.path)
        writer.save(Calibration(CORNERS), camera, camera_info, DEVICE)
        storage = CalibrationStorage(self.path)

        with patch("calibration.json.load", wraps=json.load) as parse, self.assertLogs(
            "calibration", level="INFO"
        ) as logs:
            first = storage.load(camera, camera_info, DEVICE)
            second = storage.load(camera, camera_info, DEVICE)

        self.assertIs(first, second)
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(
            sum("Loaded saved camera calibration." in line for line in logs.output),
            1,
        )

    def test_save_updates_cache_immediately_and_clear_invalidates_it(self):
        camera = PersistentCamera()
        camera_info = camera.status()
        first = Calibration(CORNERS)
        replacement_corners = [
            [0.2, 0.2],
            [0.8, 0.2],
            [0.8, 0.8],
            [0.2, 0.8],
        ]
        replacement = Calibration(replacement_corners)

        self.storage.save(first, camera, camera_info, DEVICE)
        self.assertIs(self.storage.load(camera, camera_info, DEVICE), first)
        self.storage.save(replacement, camera, camera_info, DEVICE)
        self.assertIs(
            self.storage.load(camera, camera_info, DEVICE), replacement
        )
        self.assertEqual(
            self.storage.load(camera, camera_info, DEVICE).corners.tolist(),
            replacement_corners,
        )

        self.storage.clear(DEVICE)
        self.assertIsNone(self.storage.load(camera, camera_info, DEVICE))

    def test_cache_rejects_incompatible_camera_and_device_contexts(self):
        camera = PersistentCamera()
        camera_info = camera.status()
        CalibrationStorage(self.path).save(
            Calibration(CORNERS), camera, camera_info, DEVICE
        )

        cases = []
        changed_source = PersistentCamera(source=1)
        cases.append(("source", changed_source, changed_source.status(), DEVICE))
        changed_rotation = PersistentCamera(rotate=90)
        cases.append(
            ("rotation", changed_rotation, changed_rotation.status(), DEVICE)
        )
        changed_resolution = PersistentCamera()
        changed_resolution.width = 640
        cases.append(
            (
                "resolution",
                changed_resolution,
                changed_resolution.status(),
                DEVICE,
            )
        )
        cases.append(
            (
                "device geometry",
                camera,
                camera_info,
                Device(DEVICE.serial, 720, 1600, DEVICE.rotation),
            )
        )

        for label, changed_camera, changed_info, changed_device in cases:
            with self.subTest(label=label):
                storage = CalibrationStorage(self.path)
                compatible = storage.load(camera, camera_info, DEVICE)
                self.assertIsNotNone(compatible)
                self.assertIsNone(
                    storage.load(changed_camera, changed_info, changed_device)
                )

    def test_pi_calibrations_use_legacy_phone_a_path_and_per_device_files(self):
        camera = PersistentCamera()
        camera_info = camera.status()
        phone_a = PiControlTarget(
            "pi-api:http://pi.test:8766",
            device_id="phone_A",
        )
        phone_b = PiControlTarget(
            "pi-api:http://pi.test:8766#phone_B",
            device_id="phone_B",
        )
        corners_a = CORNERS
        corners_b = [[.2, .15], [.8, .15], [.85, .85], [.15, .85]]

        calibration_a = Calibration(corners_a)
        calibration_b = Calibration(corners_b)
        self.storage.save(calibration_a, camera, camera_info, phone_a)
        self.storage.save(calibration_b, camera, camera_info, phone_b)

        phone_b_path = self.path.with_name("calibration.phone_B.json")
        self.assertEqual(self.storage.path_for(phone_a), self.path)
        self.assertEqual(self.storage.path_for(phone_b), phone_b_path)
        stored_a = json.loads(self.path.read_text())
        self.assertEqual(stored_a["version"], 1)
        self.assertNotIn("device_id", stored_a["device"])
        self.assertEqual(stored_a["corners"], corners_a)
        self.assertEqual(json.loads(phone_b_path.read_text())["corners"], corners_b)
        loaded_a = self.storage.load(camera, camera_info, phone_a)
        loaded_b = self.storage.load(camera, camera_info, phone_b)
        self.assertIs(loaded_a, calibration_a)
        self.assertIs(loaded_b, calibration_b)
        self.assertIsNot(loaded_a, loaded_b)
        self.assertEqual(loaded_a.corners.tolist(), corners_a)
        self.assertEqual(loaded_b.corners.tolist(), corners_b)
        self.storage.clear(phone_b)
        self.assertTrue(self.path.exists())
        self.assertFalse(phone_b_path.exists())
        self.assertEqual(
            self.storage.load(camera, camera_info, phone_a).corners.tolist(),
            corners_a,
        )

    def test_switching_pi_devices_loads_only_their_saved_calibration(self):
        camera = PersistentCamera()
        camera_info = camera.status()
        phone_a = PiControlTarget(
            "pi-api:http://pi.test:8766",
            device_id="phone_A",
        )
        phone_b = PiControlTarget(
            "pi-api:http://pi.test:8766#phone_B",
            device_id="phone_B",
        )
        corners_a = CORNERS
        corners_b = [[.2, .15], [.8, .15], [.85, .85], [.15, .85]]
        self.storage.save(Calibration(corners_a), camera, camera_info, phone_a)
        self.storage.save(Calibration(corners_b), camera, camera_info, phone_b)

        pi = Mock(spec=PiApiClient)
        pi.base_url = "http://pi.test:8766"
        pi.device_id = "phone_A"
        pi.status.return_value = {"ok": True, "connected": True}
        pi.devices.return_value = {
            "ok": True,
            "default_device": "phone_A",
            "devices": [{"id": name} for name in ("phone_A", "phone_B", "phone_C")],
            "enrollment": {"active": False},
        }
        app = create_app(
            camera,
            self.controller,
            self.locks,
            self.workflows,
            manual_backend="raspberry_pi",
            pi_client=pi,
            calibration_storage=self.storage,
        )
        client = app.test_client()
        authenticate_admin(client)
        headers = self.headers(app)

        client.get("/api/pi/status", headers=headers)
        status_a = client.get("/api/status", headers=headers).json
        self.assertTrue(status_a["calibrated"])
        self.assertEqual(status_a["corners"], corners_a)

        selected_b = client.post(
            "/api/devices/select",
            json={"device_id": "phone_B"},
            headers=headers,
        )
        self.assertEqual(selected_b.status_code, 200, selected_b.json)
        status_b = client.get("/api/status", headers=headers).json
        self.assertTrue(status_b["calibrated"])
        self.assertEqual(status_b["corners"], corners_b)

        selected_a = client.post(
            "/api/devices/select",
            json={"device_id": "phone_A"},
            headers=headers,
        )
        self.assertEqual(selected_a.status_code, 200, selected_a.json)
        restored_a = client.get("/api/status", headers=headers).json
        self.assertTrue(restored_a["calibrated"])
        self.assertEqual(restored_a["corners"], corners_a)

        selected_c = client.post(
            "/api/devices/select",
            json={"device_id": "phone_C"},
            headers=headers,
        )
        self.assertEqual(selected_c.status_code, 200, selected_c.json)
        status_c = client.get("/api/status", headers=headers).json
        self.assertFalse(status_c["calibrated"])
        self.assertEqual(status_c["corners"], [])


if __name__ == "__main__":
    unittest.main()
