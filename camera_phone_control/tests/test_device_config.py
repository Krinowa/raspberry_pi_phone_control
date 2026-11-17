"""Logical device routing stays compatible with the single current phone."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from automation.backends import select_device
from calibration import Calibration, CalibrationStorage
from hardware.device_config import (
    DEFAULT_DEVICE_ID,
    DEFAULT_PI_BASE_URL,
    DeviceConfig,
    get_device_config,
)
from hardware.pi_api_client import PiApiClient
from hardware.raspberry_pi_input import RaspberryPiInputBridge


class DeviceConfigTests(unittest.TestCase):
    def test_default_device_is_phone_a_at_existing_pi_url(self):
        with patch.dict(os.environ, {"PI_API_BASE_URL": DEFAULT_PI_BASE_URL}):
            config = get_device_config()
            client = PiApiClient()
        self.assertEqual(
            config,
            DeviceConfig("phone_A", "http://192.168.68.61:8766"),
        )
        self.assertEqual(client.device_id, DEFAULT_DEVICE_ID)
        self.assertEqual(client.base_url, DEFAULT_PI_BASE_URL)

    def test_explicit_phone_a_routes_to_existing_pi_endpoint(self):
        with patch("hardware.pi_api_client.http.client.HTTPConnection") as factory:
            connection = factory.return_value
            response = connection.getresponse.return_value
            response.status = 202
            response.read.return_value = (
                b'{"ok":true,"accepted":true,"status":"queued",'
                b'"command_id":"phone-a-command"}'
            )
            client = PiApiClient(device_id="phone_A")
            result = client.home()

        factory.assert_called_once_with("192.168.68.61", 8766, timeout=2.0)
        self.assertEqual(result["command_id"], "phone-a-command")
        self.assertEqual(client.last_command_id, "phone-a-command")

    def test_bridge_and_backend_retain_device_id_without_changing_lock_identity(self):
        client = PiApiClient("http://pi.test:8766")
        bridge = RaspberryPiInputBridge(client)
        default_target = select_device(Mock(), input_backend="raspberry_pi")
        explicit_target = select_device(
            Mock(), input_backend="raspberry_pi", device_id="phone_A"
        )

        self.assertEqual(bridge.device_id, "phone_A")
        self.assertEqual(bridge.target.device_id, "phone_A")
        self.assertEqual(bridge.target.serial, "pi-api:http://pi.test:8766")
        self.assertEqual(default_target.device_id, "phone_A")
        self.assertEqual(explicit_target.device_id, "phone_A")

    def test_existing_positional_client_api_remains_compatible(self):
        client = PiApiClient("http://pi.test:8766", 1.5, 4)
        self.assertEqual(client.device_id, "phone_A")
        self.assertEqual(client.base_url, "http://pi.test:8766")
        self.assertEqual((client.connect_timeout, client.read_timeout), (1.5, 4))

    def test_new_logical_device_uses_same_pi_with_distinct_lock_identity(self):
        client = PiApiClient("http://pi.test:8766", device_id="phone_B")
        bridge = RaspberryPiInputBridge(client)
        self.assertEqual(client.device_id, "phone_B")
        self.assertEqual(bridge.target.device_id, "phone_B")
        self.assertEqual(bridge.target.serial, "pi-api:http://pi.test:8766#phone_B")

    def test_calibration_file_shape_remains_backward_compatible(self):
        target = RaspberryPiInputBridge(
            PiApiClient("http://pi.test:8766")
        ).target
        camera = Mock(source=0, rotate=0)
        camera_info = {"width": 1280, "height": 720}
        calibration = Calibration([[.1, .1], [.9, .1], [.9, .9], [.1, .9]])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.json"
            storage = CalibrationStorage(path)
            storage.save(calibration, camera, camera_info, target)
            stored = json.loads(path.read_text())
            self.assertNotIn("device_id", stored["device"])
            restored = storage.load(camera, camera_info, target)
        self.assertIsNotNone(restored)


if __name__ == "__main__":
    unittest.main()
