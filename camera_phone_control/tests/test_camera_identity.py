"""Hardware-free stable camera identity discovery and resolution tests."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2

from camera import Camera
from hardware.camera_identity import (
    CameraIdentityError,
    _decode_command_output,
    _windows_directshow_devices,
    _windows_pnp_devices,
    discover_linux_devices,
    discover_windows_devices,
    resolve_camera_source,
)
from hardware.camera_diagnostics import collect_windows_camera_diagnostics


class CameraIdentityTests(unittest.TestCase):
    def test_capture_backend_distinguishes_v4l2_path_dshow_and_url(self):
        with patch("camera.cv2.VideoCapture") as capture:
            linux = Camera("/dev/video2")
            linux.capture_backend = "v4l2"
            linux._open_capture()
            capture.assert_called_with("/dev/video2", cv2.CAP_V4L2)

            windows = Camera(1)
            windows.capture_backend = "dshow"
            windows._open_capture()
            capture.assert_called_with(1, cv2.CAP_DSHOW)

            network = Camera("rtsp://camera.example.test/live")
            network._open_capture()
            capture.assert_called_with(
                "rtsp://camera.example.test/live", cv2.CAP_FFMPEG
            )

    def test_macos_identity_resolves_new_index_after_reorder(self):
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
        self.assertEqual(resolve_camera_source("avfoundation:rog", devices=devices), 1)
        devices.reverse()
        devices[0]["runtime_source"] = 0
        devices[1]["runtime_source"] = 1
        self.assertEqual(resolve_camera_source("avfoundation:rog", devices=devices), 0)

    def test_linux_by_id_follows_reconnected_video_node(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            by_id = root / "by-id"
            sys_class = root / "sys"
            by_id.mkdir()
            video2 = root / "video2"
            video0 = root / "video0"
            video2.touch()
            video0.touch()
            for name in ("video2", "video0"):
                directory = sys_class / name
                directory.mkdir(parents=True)
                (directory / "name").write_text("ROG EYE S\n", encoding="utf-8")
            link = by_id / "usb-ASUS_ROG_EYE_S_123-video-index0"
            link.symlink_to(video2)
            with patch("hardware.camera_identity.sys.platform", "linux"):
                first = discover_linux_devices(by_id, sys_class)
                self.assertEqual(first[0]["runtime_source"], str(video2.resolve()))
                link.unlink()
                link.symlink_to(video0)
                second = discover_linux_devices(by_id, sys_class)
            self.assertEqual(first[0]["stable_id"], second[0]["stable_id"])
            self.assertEqual(second[0]["runtime_source"], str(video0.resolve()))

    def test_missing_identity_never_falls_back(self):
        devices = [
            {
                "stable_id": "avfoundation:macbook",
                "name": "MacBook Air Camera",
                "backend": "avfoundation",
                "runtime_source": 0,
            }
        ]
        with self.assertRaisesRegex(CameraIdentityError, "unavailable"):
            resolve_camera_source("avfoundation:rog", devices=devices)

    def test_duplicate_identity_is_rejected(self):
        duplicate = {
            "stable_id": "dshow:usb-camera",
            "name": "USB Camera",
            "backend": "dshow",
            "runtime_source": 0,
        }
        with self.assertRaisesRegex(CameraIdentityError, "ambiguous"):
            resolve_camera_source("dshow:usb-camera", devices=[duplicate, duplicate])

    def test_unresolved_windows_identity_is_not_guessed(self):
        devices = [
            {
                "stable_id": "dshow:usb-camera",
                "name": "Duplicate Camera",
                "backend": "dshow",
                "runtime_source": None,
            }
        ]
        with self.assertRaisesRegex(CameraIdentityError, "cannot be mapped safely"):
            resolve_camera_source("dshow:usb-camera", devices=devices)

    def test_windows_unique_name_maps_pnp_identity_to_directshow_index(self):
        with (
            patch("hardware.camera_identity.sys.platform", "win32"),
            patch(
                "hardware.camera_identity._windows_pnp_devices",
                return_value=[
                    {"FriendlyName": "ROG EYE S", "InstanceId": "USB\\VID_ROG"}
                ],
            ),
            patch(
                "hardware.camera_identity._windows_directshow_devices",
                return_value=(
                    [
                        {"index": 0, "name": "Integrated Camera", "alternative_name": None},
                        {"index": 1, "name": "ROG EYE S", "alternative_name": None},
                    ],
                    None,
                    "ffmpeg.exe",
                ),
            ),
        ):
            devices = discover_windows_devices()
        self.assertEqual(devices[0]["stable_id"], "dshow:USB\\VID_ROG")
        self.assertEqual(devices[0]["runtime_source"], 1)

    def test_windows_duplicate_names_remain_unresolved(self):
        with (
            patch("hardware.camera_identity.sys.platform", "win32"),
            patch(
                "hardware.camera_identity._windows_pnp_devices",
                return_value=[
                    {"FriendlyName": "USB Camera", "InstanceId": "USB\\ONE"},
                    {"FriendlyName": "USB Camera", "InstanceId": "USB\\TWO"},
                ],
            ),
            patch(
                "hardware.camera_identity._windows_directshow_devices",
                return_value=(
                    [
                        {"index": 0, "name": "USB Camera", "alternative_name": None},
                        {"index": 1, "name": "USB Camera", "alternative_name": None},
                    ],
                    None,
                    "ffmpeg.exe",
                ),
            ),
        ):
            devices = discover_windows_devices()
        self.assertEqual([item["runtime_source"] for item in devices], [None, None])

    def test_windows_directshow_parser_keeps_video_moniker_and_ignores_audio(self):
        completed = Mock(
            stderr=(
                '[dshow] "USB Camera" (video)\n'
                '[dshow]   Alternative name "@device_pnp_\\\\?\\usb#vid_1234#one#{guid}\\global"\n'
                '[dshow] "Microphone" (audio)\n'
                '[dshow]   Alternative name "@device_cm_{audio}"\n'
                '[dshow] "Integrated Camera" (video)\n'
            )
        )
        with patch("hardware.camera_identity.subprocess.run", return_value=completed):
            devices = _windows_directshow_devices()

        self.assertEqual(
            devices,
            [
                {
                    "index": 0,
                    "name": "USB Camera",
                    "alternative_name": r"@device_pnp_\\?\usb#vid_1234#one#{guid}\global",
                },
                {
                    "index": 1,
                    "name": "Integrated Camera",
                    "alternative_name": None,
                },
            ],
        )

    def test_windows_directshow_decodes_code_page_950(self):
        completed = Mock(
            stderr=(
                '[dshow] "測試攝影機" (video)\n'
                '[dshow]   Alternative name "@device_pnp_\\\\?\\usb#vid_1234#one"\n'
            ).encode("cp950")
        )
        with (
            patch("hardware.camera_identity.subprocess.run", return_value=completed),
            patch("hardware.camera_identity._windows_ansi_encoding", return_value="cp950"),
        ):
            devices = _windows_directshow_devices()

        self.assertEqual(devices[0]["name"], "測試攝影機")

    def test_windows_directshow_decodes_utf8_mixed_camera_names(self):
        completed = Mock(
            stderr=(
                '[dshow] "Integrated Camera" (video)\n'
                '[dshow] "測試攝影機 USB" (video)\n'
            ).encode("utf-8")
        )
        with patch("hardware.camera_identity.subprocess.run", return_value=completed):
            devices = _windows_directshow_devices()

        self.assertEqual(
            [item["name"] for item in devices],
            ["Integrated Camera", "測試攝影機 USB"],
        )

    def test_windows_directshow_truncated_utf8_and_cp950_fails_closed(self):
        completed = Mock(stderr="測試攝影機".encode("utf-8")[:-1])
        with (
            patch("hardware.camera_identity.subprocess.run", return_value=completed),
            patch("hardware.camera_identity._windows_ansi_encoding", return_value="cp950"),
        ):
            devices, error, _ = _windows_directshow_devices(include_status=True)

        self.assertEqual(devices, [])
        self.assertEqual(error, "directshow_output_decode_failed")

    def test_windows_directshow_empty_output_is_controlled(self):
        with patch(
            "hardware.camera_identity.subprocess.run",
            return_value=Mock(stderr=b""),
        ):
            devices, error, _ = _windows_directshow_devices(include_status=True)

        self.assertEqual(devices, [])
        self.assertEqual(error, "directshow_no_video_devices")

    def test_windows_directshow_malformed_output_fails_closed(self):
        completed = Mock(stderr=b"\x81")
        with (
            patch("hardware.camera_identity.subprocess.run", return_value=completed),
            patch("hardware.camera_identity._windows_ansi_encoding", return_value="cp950"),
        ):
            devices, error, _ = _windows_directshow_devices(include_status=True)

        self.assertEqual(devices, [])
        self.assertEqual(error, "directshow_output_decode_failed")

    def test_windows_pnp_uses_forced_utf8_and_rejects_malformed_output(self):
        completed = Mock(returncode=0, stdout=b"\xff")
        with patch("hardware.camera_identity.subprocess.run", return_value=completed) as run:
            with self.assertRaises(CameraIdentityError) as raised:
                _windows_pnp_devices()

        self.assertEqual(raised.exception.error_code, "camera_discovery_decode_failed")
        self.assertFalse(run.call_args.kwargs["text"])
        self.assertIn("UTF8Encoding", run.call_args.args[0][-1])

    def test_windows_pnp_accepts_forced_utf8_chinese_names(self):
        payload = (
            '[{"FriendlyName":"測試攝影機 USB",'
            '"InstanceId":"USB\\\\VID_1234\\\\ONE"}]'
        ).encode("utf-8")
        completed = Mock(returncode=0, stdout=payload)
        with patch("hardware.camera_identity.subprocess.run", return_value=completed):
            devices = _windows_pnp_devices()

        self.assertEqual(devices[0]["FriendlyName"], "測試攝影機 USB")

    def test_windows_pnp_incomplete_process_or_json_is_controlled(self):
        success_without_stdout = Mock(spec=["returncode"])
        success_without_stdout.returncode = 0
        for completed in (
            Mock(spec=[]),
            success_without_stdout,
            Mock(returncode=0, stdout=b'[{"FriendlyName":"Camera"}]'),
        ):
            with self.subTest(completed=completed), patch(
                "hardware.camera_identity.subprocess.run", return_value=completed
            ):
                with self.assertRaises(CameraIdentityError) as raised:
                    _windows_pnp_devices()
                self.assertIn(
                    raised.exception.error_code,
                    {"camera_discovery_failed", "camera_discovery_output_invalid"},
                )

    def test_decoder_supports_big5_alias_and_rejects_truncated_bytes(self):
        value = "測試攝影機"
        self.assertEqual(
            _decode_command_output(value.encode("big5"), encodings=("utf-8", "big5")),
            value,
        )
        self.assertIsNone(
            _decode_command_output(value.encode("big5")[:-1], encodings=("utf-8", "big5"))
        )

    def test_windows_directshow_reports_missing_ffmpeg_without_guessing(self):
        with (
            patch(
                "hardware.camera_identity.resolve_ffmpeg_executable",
                return_value="ffmpeg.exe",
            ),
            patch(
                "hardware.camera_identity.subprocess.run",
                side_effect=FileNotFoundError,
            ),
        ):
            devices, error, executable = _windows_directshow_devices(
                include_status=True
            )

        self.assertEqual(devices, [])
        self.assertEqual(error, "ffmpeg_unavailable")
        self.assertEqual(executable, "ffmpeg.exe")

    def test_windows_pnp_monikers_disambiguate_duplicate_friendly_names(self):
        pnp = [
            {"FriendlyName": "USB Camera", "InstanceId": r"USB\VID_1234\ONE"},
            {"FriendlyName": "USB Camera", "InstanceId": r"USB\VID_1234\TWO"},
        ]
        directshow = [
            {
                "index": 0,
                "name": "USB Camera",
                "alternative_name": r"@device_pnp_\\?\usb#vid_1234#two#{guid}\global",
            },
            {
                "index": 1,
                "name": "USB Camera",
                "alternative_name": r"@device_pnp_\\?\usb#vid_1234#one#{guid}\global",
            },
        ]
        with (
            patch("hardware.camera_identity.sys.platform", "win32"),
            patch("hardware.camera_identity._windows_pnp_devices", return_value=pnp),
            patch(
                "hardware.camera_identity._windows_directshow_devices",
                return_value=(directshow, None, "ffmpeg.exe"),
            ),
        ):
            devices = discover_windows_devices()

        self.assertEqual([item["runtime_source"] for item in devices], [1, 0])
        self.assertEqual(
            [item["mapping_method"] for item in devices],
            ["pnp_moniker", "pnp_moniker"],
        )

    def test_windows_pnp_identity_follows_directshow_reorder_after_replug(self):
        pnp = [{"FriendlyName": "USB Camera", "InstanceId": r"USB\VID_1234\ONE"}]
        directshow = [
            {
                "index": 1,
                "name": "USB Camera",
                "alternative_name": r"@device_pnp_\\?\usb#vid_1234#one#{guid}\global",
            }
        ]
        with (
            patch("hardware.camera_identity.sys.platform", "win32"),
            patch("hardware.camera_identity._windows_pnp_devices", return_value=pnp),
            patch(
                "hardware.camera_identity._windows_directshow_devices",
                return_value=(directshow, None, "ffmpeg.exe"),
            ),
        ):
            first = discover_windows_devices()
            directshow[0]["index"] = 0
            second = discover_windows_devices()

        self.assertEqual(first[0]["stable_id"], second[0]["stable_id"])
        self.assertEqual(first[0]["runtime_source"], 1)
        self.assertEqual(second[0]["runtime_source"], 0)

    def test_windows_delayed_directshow_initialization_remains_unresolved_and_logged(self):
        pnp = [{"FriendlyName": "Late Camera", "InstanceId": r"USB\VID_LATE\ONE"}]
        with (
            patch("hardware.camera_identity.sys.platform", "win32"),
            patch("hardware.camera_identity._windows_pnp_devices", return_value=pnp),
            patch(
                "hardware.camera_identity._windows_directshow_devices",
                return_value=([], "directshow_no_video_devices", "ffmpeg.exe"),
            ),
            patch("hardware.camera_identity.log_event") as logged,
        ):
            devices = discover_windows_devices()

        self.assertIsNone(devices[0]["runtime_source"])
        self.assertEqual(
            devices[0]["mapping_failure_reason"],
            "directshow_no_video_devices",
        )
        fields = logged.call_args.kwargs
        self.assertEqual(fields["camera_id"], r"dshow:USB\VID_LATE\ONE")
        self.assertEqual(fields["runtime_index_candidates"], [])
        self.assertFalse(fields["msmf_runtime_mapping_used"])

    def test_windows_diagnostics_reports_cp950_names_and_mapping(self):
        pnp = [
            {
                "FriendlyName": "測試攝影機 USB",
                "InstanceId": r"USB\VID_1234\ONE",
            }
        ]
        directshow = [
            {
                "index": 2,
                "name": "測試攝影機 USB",
                "alternative_name": r"@device_pnp_\\?\usb#vid_1234#one",
            }
        ]
        with (
            patch("hardware.camera_diagnostics.os.name", "nt"),
            patch("hardware.camera_diagnostics.sys.platform", "win32"),
            patch("hardware.camera_identity.sys.platform", "win32"),
            patch(
                "hardware.camera_diagnostics._windows_code_page",
                side_effect=[950, 950, None, None],
            ),
            patch(
                "hardware.camera_diagnostics._windows_ansi_encoding",
                return_value="cp950",
            ),
            patch(
                "hardware.camera_diagnostics._windows_pnp_devices",
                return_value=pnp,
            ),
            patch(
                "hardware.camera_diagnostics._windows_directshow_devices",
                return_value=(directshow, None, "C:/PhoneController/tools/ffmpeg.exe"),
            ),
        ):
            report = collect_windows_camera_diagnostics()

        self.assertEqual(report["code_pages"]["ansi"], 950)
        self.assertEqual(report["code_pages"]["ansi_encoding"], "cp950")
        self.assertEqual(report["directshow_devices"][0]["name"], "測試攝影機 USB")
        self.assertEqual(report["mapped_identities"][0]["runtime_source"], 2)
        self.assertEqual(report["errors"], [])


if __name__ == "__main__":
    unittest.main()
