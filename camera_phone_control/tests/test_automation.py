"""Automation, cross-process exclusion and dashboard regression tests; no hardware."""

import io
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from android_control import AndroidControl, Device, DeviceError
from app import create_app
from automation.runner import Runner, main
from automation.scripts import SCRIPTS, available_scripts
from device_lock import DeviceBusy, DeviceLocks
from test_control import FakeCamera, CORNERS, authenticate_admin


DEVICE = Device("usb-test", 1080, 2400, 0)
PROJECT = Path(__file__).resolve().parents[1]


def controller():
    fake = Mock(spec=AndroidControl)
    fake.device.return_value = DEVICE
    return fake


class DeviceSelectionTests(unittest.TestCase):
    def select(self, rows, serial=None):
        adb = AndroidControl()
        with patch.object(adb, "_adb", side_effect=[
            "List of devices attached\n" + rows, "0", "Physical size: 1080x2400",
            "SurfaceOrientation: 0",
        ]) as command:
            device = adb.device(serial=serial, authorized_only=True)
            self.assertEqual(command.call_args.args[:2], ("-s", device.serial))
            return device

    def test_auto_select_one_authorized(self):
        self.assertEqual(self.select("usb-test device usb:1"), DEVICE)
        self.assertEqual(self.select("bad offline usb:2\nusb-test device usb:1"), DEVICE)
        self.assertEqual(self.select("bad unauthorized usb:2\nusb-test device usb:1"), DEVICE)

    def test_empty_multiple_unauthorized_offline_wireless_emulator_rejected(self):
        for rows in ("", "a device usb:1\nb device usb:2", "a unauthorized usb:1",
                     "a offline usb:1", "host:5555 device", "emulator-5554 device",
                     "a unauthorized usb:1\nb offline usb:2"):
            with self.subTest(rows=rows), self.assertRaises(DeviceError):
                self.select(rows)

    def test_explicit_selection(self):
        self.assertEqual(self.select("a device usb:1\nusb-test device usb:2", "usb-test"), DEVICE)
        for rows, serial in (("a unauthorized usb:1\nb device usb:2", "a"),
                             ("a offline usb:1", "a"), ("a device usb:1", "missing"),
                             ("host:5555 device", "host:5555")):
            with self.subTest(rows=rows), self.assertRaises(DeviceError):
                self.select(rows, serial)

    def test_app_launch_resolves_component_and_rejects_unsafe_arguments(self):
        adb = AndroidControl()
        component = "com.android.settings/.Settings"
        with patch.object(adb, "_adb", side_effect=[component, "Status: ok"]) as command:
            adb.launch_app(DEVICE, "com.android.settings")
            self.assertEqual(command.call_args_list[0].args,
                ("-s", DEVICE.serial, "shell", "cmd", "package", "resolve-activity", "--brief", "com.android.settings"))
            self.assertEqual(command.call_args_list[1].args,
                ("-s", DEVICE.serial, "shell", "am", "start", "-W", "-n", component))
        with patch.object(adb, "_adb") as command, self.assertRaises(ValueError):
            adb.launch_app(DEVICE, "arbitrary;command")
        command.assert_not_called()
        for output in ("Error: Activity not found", "Status: timeout"):
            with patch.object(adb, "_adb", side_effect=[component, output]) as command:
                with self.assertRaises(DeviceError): adb.launch_app(DEVICE, "com.android.settings")
                self.assertEqual(command.call_count, 2)
        for output in ("No activity found", "com.android.settings/.Settings;bad", "another.app/.Home"):
            with patch.object(adb, "_adb", return_value=output) as command:
                with self.assertRaises(DeviceError): adb.launch_app(DEVICE, "com.android.settings")
                command.assert_called_once()

    def test_adb_runs_outside_terminal_signal_group_without_retry(self):
        with patch("android_control.shutil.which", return_value="adb"), patch(
            "android_control.subprocess.run", side_effect=subprocess.TimeoutExpired("adb", 8)
        ) as command:
            with self.assertRaises(DeviceError): AndroidControl().send(DEVICE, "home")
            command.assert_called_once()
            self.assertTrue(command.call_args.kwargs["start_new_session"])


class LockTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.locks = DeviceLocks(self.temp.name)

    def child_lock(self):
        code = (
            "import sys\nfrom device_lock import DeviceLocks\n"
            "with DeviceLocks(sys.argv[1]).hold('usb-test', 'automation'):\n"
            " print('ready', flush=True)\n sys.stdin.readline()\n"
        )
        process = subprocess.Popen([sys.executable, "-u", "-c", code, self.temp.name],
                                   cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.close_child, process)
        # select gives a bound if the child fails before publishing readiness.
        import select
        ready, _, _ = select.select([process.stdout], [], [], 5)
        self.assertTrue(ready, "Child failed to acquire the device lock")
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return process

    @staticmethod
    def close_child(process):
        if process.poll() is None: process.kill()
        process.communicate(timeout=5)


class LockTests(LockTestCase):
    def test_cross_process_exclusion_and_per_device_scope(self):
        process = self.child_lock()
        with self.assertRaises(DeviceBusy) as raised:
            with self.locks.hold(DEVICE.serial, "manual"): self.fail("Lock was not exclusive")
        self.assertEqual(raised.exception.owner, "automation")
        with self.locks.hold("another-device", "manual"):
            pass
        process.communicate("done\n", timeout=5)
        self.assertEqual(process.returncode, 0)
        with self.locks.hold(DEVICE.serial, "manual"):
            pass

    def test_process_death_releases_lock_and_stale_marker_does_not_block(self):
        process = self.child_lock()
        process.kill()
        process.communicate(timeout=5)
        with self.locks.hold(DEVICE.serial, "manual"):
            pass

    def test_exceptions_release_and_files_hold_only_owner_type(self):
        with self.assertRaises(RuntimeError):
            with self.locks.hold(DEVICE.serial, "automation"):
                raise RuntimeError("synthetic failure")
        self.assertEqual([p.read_text() for p in Path(self.temp.name).glob("*.lock")], ["automation"])
        with self.locks.hold(DEVICE.serial, "manual"):
            pass


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.locks = DeviceLocks(self.temp.name)
        self.controller = controller()
        self.runner = Runner(self.controller, self.locks)

    def assert_released(self):
        with self.locks.hold(DEVICE.serial, "manual"):
            pass

    def test_discovery_unknown_and_list_do_not_touch_device(self):
        self.assertIn("settings_demo", available_scripts())
        result = self.runner.run("unknown")
        self.assertEqual(result.status, "failed")
        self.assertNotEqual(result.exit_code, 0)
        self.controller.device.assert_not_called()
        output = io.StringIO()
        with redirect_stdout(output): self.assertEqual(main(["--list"]), 0)
        self.assertIn("settings_demo", output.getvalue())

    def test_demo_uses_shared_controller_and_tracks_state(self):
        event = threading.Event()
        with patch.object(event, "wait", wraps=event.wait) as wait, self.assertLogs("automation", "INFO") as logs:
            result = self.runner.run("settings_demo", DEVICE.serial, cancel_event=event)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.device_serial, DEVICE.serial)
        self.assertEqual(result.script_name, "settings_demo")
        self.assertIsNotNone(result.start_time)
        self.assertGreaterEqual(result.end_time, result.start_time)
        self.assertEqual(result.current_step, "wait")
        self.assertEqual([call.args for call in self.controller.send.call_args_list],
                         [(DEVICE, "screen_on", None), (DEVICE, "home", None)])
        self.controller.launch_app.assert_called_once_with(DEVICE, "com.android.settings")
        self.controller.device.assert_called_with(serial=DEVICE.serial, authorized_only=True)
        self.assertTrue(wait.called)
        self.assertTrue(all(0 < call.args[0] <= 0.05 for call in wait.call_args_list))
        for phrase in ("Script started", "Step started", "Step completed", "Lock acquired", "Lock released", "Script completed"):
            self.assertIn(phrase, "\n".join(logs.output))
        self.assert_released()

    def test_lock_held_during_script_and_wait(self):
        def script(context):
            self.assertIs(context.controller, self.controller)
            self.assertEqual(context.device, DEVICE)
            self.assertEqual(self.runner.state.status, "running")
            with self.assertRaises(DeviceBusy):
                with self.locks.hold(DEVICE.serial, "manual"): pass
            context.wait(0)
            with self.assertRaises(DeviceBusy):
                with self.locks.hold(DEVICE.serial, "automation"): pass
        with patch.dict(SCRIPTS, {"test": script}):
            self.assertEqual(self.runner.run("test").status, "completed")
        self.assert_released()

    def test_busy_rejects_script_without_inputs(self):
        with self.locks.hold(DEVICE.serial, "manual"):
            result = self.runner.run("settings_demo")
        self.assertEqual(result.status, "failed")
        self.assertIn("Device busy", result.message)
        self.controller.send.assert_not_called()

    def test_exception_failure_privacy_and_cli_exit_code(self):
        for error in (RuntimeError("PRIVATE-SENTINEL"), DeviceError("PRIVATE-SENTINEL"), SystemExit(0)):
            def broken(context):
                context.send_key("text", "a")
                raise error
            with patch.dict(SCRIPTS, {"broken": broken}), self.assertLogs(level="INFO") as logs:
                result = self.runner.run("broken")
            self.assertEqual(result.status, "failed")
            self.assertNotIn("PRIVATE-SENTINEL", str(result))
            self.assertNotIn("PRIVATE-SENTINEL", "\n".join(logs.output))
            self.assertIn("Lock released", "\n".join(logs.output))
            self.assert_released()
            with patch.dict(SCRIPTS, {"broken": broken}), patch(
                "automation.runner.Runner", return_value=self.runner
            ), patch("automation.runner.logging.basicConfig"):
                self.assertEqual(main(["broken"]), 1)

    def test_cancellation_before_run_between_steps_during_wait_and_interrupt(self):
        event = threading.Event()
        event.set()
        self.assertEqual(self.runner.run("settings_demo", cancel_event=event).status, "cancelled")
        self.controller.device.assert_not_called()
        event.clear()
        self.controller.send.side_effect = lambda *args: event.set()
        result = self.runner.run("settings_demo", cancel_event=event)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.exit_code, 130)
        self.controller.send.assert_called_once()
        self.controller.launch_app.assert_not_called()
        self.assert_released()
        self.controller.send.side_effect = None
        event.clear()
        with patch.object(event, "wait", side_effect=lambda seconds: event.set() or True):
            self.assertEqual(self.runner.run("settings_demo", cancel_event=event).status, "cancelled")
        self.assert_released()
        def interrupted(context): raise KeyboardInterrupt()
        with patch.dict(SCRIPTS, {"interrupt": interrupted}):
            self.assertEqual(self.runner.run("interrupt").status, "cancelled")
        self.assert_released()

    def test_cli_sigint_sets_event_and_restores_handler(self):
        previous = signal.getsignal(signal.SIGINT)
        def trigger(name, serial, *, cancel_event):
            signal.raise_signal(signal.SIGINT)
            self.assertTrue(cancel_event.is_set())
            return self.runner.run(name, serial, cancel_event=cancel_event)
        with patch("automation.runner.run", side_effect=trigger), patch("automation.runner.logging.basicConfig"):
            self.assertEqual(main(["settings_demo"]), 130)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)
        self.assert_released()

    def test_cli_sigint_during_command_allows_command_to_finish_then_stops(self):
        finished = []
        def send(*args):
            signal.raise_signal(signal.SIGINT)
            finished.append(True)
        self.controller.send.side_effect = send
        with patch("automation.runner.Runner", return_value=self.runner), patch("automation.runner.logging.basicConfig"):
            self.assertEqual(main(["settings_demo"]), 130)
        self.assertEqual(finished, [True])
        self.controller.send.assert_called_once()
        self.controller.launch_app.assert_not_called()
        self.assertEqual(self.runner.state.status, "cancelled")
        self.assert_released()

    def test_cli_list_and_unknown_script_in_separate_process(self):
        listing = subprocess.run([sys.executable, "-m", "automation.runner", "--list"],
                                 cwd=PROJECT, capture_output=True, text=True, timeout=5)
        self.assertEqual(listing.returncode, 0)
        self.assertEqual(listing.stdout.splitlines(), list(available_scripts()))
        unknown = subprocess.run([sys.executable, "-m", "automation.runner", "unknown"],
                                 cwd=PROJECT, capture_output=True, text=True, timeout=5)
        self.assertEqual(unknown.returncode, 1)
        self.assertIn("Unknown script", unknown.stderr)
        self.assertNotIn("Traceback", unknown.stderr)

    def test_phone_geometry_change_and_command_failure_stop_without_retry(self):
        self.controller.device.side_effect = [DEVICE, Device(DEVICE.serial, 2400, 1080, 1)]
        self.assertEqual(self.runner.run("settings_demo").status, "failed")
        self.controller.send.assert_not_called()
        self.assert_released()
        self.controller.device.side_effect = None
        self.controller.send.side_effect = DeviceError("USB lost")
        self.assertEqual(self.runner.run("settings_demo").status, "failed")
        self.controller.send.assert_called_once()
        self.assert_released()

    def test_context_wait_validates_and_single_character_input_is_not_logged(self):
        adb = AndroidControl()
        def keys(context):
            for value in "aB9% '$;": context.send_key("text", value)
            for value in ("enter", "backspace", "tab"): context.send_key("key", value)
            for seconds in (-1, True, "1", float("nan"), float("inf")):
                with self.assertRaises(ValueError): context.wait(seconds)
        with patch.object(adb, "device", return_value=DEVICE), patch.object(adb, "_adb") as command:
            with patch.dict(SCRIPTS, {"keys": keys}), self.assertLogs(level="INFO") as logs:
                result = Runner(adb, self.locks).run("keys")
        self.assertEqual(result.status, "completed")
        self.assertEqual(command.call_count, 11)
        self.assertTrue(all(record.name == "automation" for record in logs.records))
        self.assertTrue(all("keyboard input" in line for line in logs.output if "Step" in line))
        self.assertNotIn("aB9", "\n".join(logs.output))


class ManualLockTests(LockTestCase):
    def setUp(self):
        super().setUp()
        self.controller = controller()
        self.app = create_app(FakeCamera(), self.controller, self.locks)
        self.addCleanup(self.app.extensions["control_state"].disable)
        self.app.testing = True
        self.client = self.app.test_client()
        authenticate_admin(self.client)
        self.headers = {"X-Control-Client": "browser-test-123",
                        "X-Control-Token": self.app.config["CONTROL_TOKEN"]}
        self.assertEqual(self.post("/api/calibration", {"corners": CORNERS, "frame_id": 123}).status_code, 200)
        self.enable()

    def post(self, path, body):
        return self.client.post(path, json=body, headers=self.headers)

    def enable(self):
        self.assertEqual(self.post("/api/control", {"enabled": True, "frame_id": 123}).status_code, 200)

    def test_separate_runner_process_blocks_all_manual_inputs_and_allows_camera(self):
        self.post("/api/control", {"enabled": False})
        process = self.child_lock()
        result = self.post("/api/takeover", {})
        self.assertEqual(result.status_code, 202)
        self.assertTrue(result.json["pending"])
        for name in ("tap", "back", "home", "recents", "swipe_up", "swipe_down",
                     "swipe_left", "swipe_right", "screen_on", "screen_off"):
            result = self.post("/api/action", {"action": name, "x": .45, "y": .5, "frame_id": 123})
            self.assertEqual(result.status_code, 409)
            self.assertEqual(result.json, {"ok": False, "error": "takeover_requested", "owner": "automation", "pending": True})
        for kind, value in (("text", "a"), ("key", "enter"), ("key", "backspace"), ("key", "tab")):
            result = self.post("/api/key", {"type": kind, "value": value, "frame_id": 123})
            self.assertEqual(result.status_code, 409)
            self.assertEqual(result.json["owner"], "automation")
        self.assertEqual(self.post("/api/control", {"enabled": True, "frame_id": 123}).status_code, 409)
        self.assertEqual(self.client.get("/api/frame").status_code, 200)
        self.assertEqual(self.client.get("/api/status", headers=self.headers).status_code, 200)
        self.controller.send.assert_not_called()
        self.controller.send_key.assert_not_called()
        process.communicate("done\n", timeout=5)
        self.assertTrue(self.post("/api/heartbeat", {"frame_id": 123}).json["enabled"])
        self.assertEqual(self.post("/api/action", {"action": "home", "frame_id": 123}).status_code, 200)
        self.assertEqual(self.post("/api/key", {"type": "text", "value": "a", "frame_id": 123}).status_code, 200)

    def test_manual_command_holds_lock_and_releases_on_adb_failure(self):
        def send(*args):
            with self.assertRaises(DeviceBusy):
                with self.locks.hold(DEVICE.serial, "automation"): pass
            raise DeviceError("Synthetic USB failure")
        self.controller.send.side_effect = send
        self.assertEqual(self.post("/api/action", {"action": "home", "frame_id": 123}).status_code, 409)
        with self.locks.hold(DEVICE.serial, "automation"): pass

    def test_other_device_automation_does_not_block_manual_phone(self):
        with self.locks.hold("another-device", "automation"):
            self.assertEqual(self.post("/api/action", {"action": "home", "frame_id": 123}).status_code, 200)

    def test_manual_does_not_reroute_to_a_different_phone_under_old_lock(self):
        self.controller.device.return_value = Device("replacement", 1080, 2400, 0)
        self.assertEqual(self.post("/api/action", {"action": "home", "frame_id": 123}).status_code, 409)
        self.controller.send.assert_not_called()

    def test_keyboard_validation_and_lease_checks_still_apply(self):
        def key(**changes):
            return self.post("/api/key", {"type": "text", "value": "a", "frame_id": 123} | changes)
        for change in ({"value": "ab"}, {"value": "é"}, {"value": "\n"}, {"value": []},
                       {"type": "shell"}, {"frame_id": True}, {"serial": "another-phone"}):
            self.assertEqual(key(**change).status_code, 400)
        self.headers["X-Control-Client"] = "other-browser-123"
        self.assertEqual(key().status_code, 400)
        self.headers["X-Control-Client"] = "browser-test-123"
        self.app.extensions["control_state"].expires = 0
        self.assertEqual(key().status_code, 400)
        self.controller.send_key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
