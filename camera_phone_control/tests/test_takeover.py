"""Explicit human takeover and cooperative handoff; no physical Android device involved."""

import logging
import select
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from app import create_app
from automation.context import AutomationContext, Cancelled
from automation.runner import Runner, RunState
from automation.scripts import SCRIPTS
from device_lock import DeviceBusy
from test_automation import LockTestCase, DEVICE, PROJECT, controller
from test_control import FakeCamera, CORNERS, authenticate_admin


class TakeoverTests(LockTestCase):
    def setUp(self):
        super().setUp()
        self.controller = controller()
        self.camera = FakeCamera()
        self.app = create_app(self.camera, self.controller, self.locks)
        self.app.testing = True
        self.state = self.app.extensions["control_state"]
        self.addCleanup(self.state.disable)
        self.client = self.app.test_client()
        authenticate_admin(self.client)
        self.headers = {"X-Control-Client": "browser-test-123",
                        "X-Control-Token": self.app.config["CONTROL_TOKEN"]}
        self.assertEqual(self.post("/api/calibration", {"corners": CORNERS, "frame_id": 123}).status_code, 200)

    def post(self, path, body):
        return self.client.post(path, json=body, headers=self.headers)

    def enable(self):
        return self.post("/api/control", {"enabled": True, "frame_id": 123})

    def takeover(self):
        return self.post("/api/takeover", {})

    def heartbeat(self):
        return self.post("/api/heartbeat", {"frame_id": 123})

    def status(self):
        return self.client.get("/api/status", headers=self.headers).json

    def action(self):
        return self.post("/api/action", {"action": "home", "frame_id": 123})

    def assert_available(self):
        self.assertIsNone(self.locks.owner(DEVICE.serial))
        with self.locks.hold(DEVICE.serial, "automation"):
            pass

    def test_takeover_waits_for_inflight_command_then_manual_owns_without_overlap(self):
        entered, finish = threading.Event(), threading.Event()
        auto = controller()
        runner = Runner(auto, self.locks)
        order = []

        def in_flight(*args):
            order.append("automation started")
            entered.set()
            if not finish.wait(3): raise RuntimeError("Test did not release command")
            order.append("automation finished")

        auto.send.side_effect = in_flight
        self.controller.send.side_effect = lambda *args: order.append("manual input")
        result = []
        thread = threading.Thread(target=lambda: result.append(runner.run("settings_demo")))
        thread.start()
        self.addCleanup(lambda: thread.join(3))
        self.addCleanup(finish.set)
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.status()["control_status"], "automation")
        requested = self.takeover()
        self.assertEqual(requested.status_code, 202)
        self.assertEqual(requested.json, {"enabled": False, "pending": True, "control_status": "takeover_requested"})
        self.assertEqual(self.status()["control_status"], "takeover_requested")
        self.assertTrue(self.status()["pending_owned"])
        self.assertEqual(self.locks.owner(DEVICE.serial), "automation")
        self.assertEqual(self.action().status_code, 409)
        self.controller.send.assert_not_called()
        self.assertTrue(self.heartbeat().json["pending"])
        finish.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0].status, "cancelled")
        self.assertTrue(result[0].takeover_requested)
        self.assertIn("due to takeover", result[0].message)
        self.assertEqual(result[0].exit_code, 130)
        auto.send.assert_called_once()
        auto.launch_app.assert_not_called()
        self.assertIsNone(self.locks.owner(DEVICE.serial))
        # No new automation can steal the handoff before the next fresh heartbeat.
        with self.assertRaises(DeviceBusy):
            with self.locks.hold(DEVICE.serial, "automation"): pass
        self.assertEqual(self.heartbeat().json, {"enabled": True, "pending": False})
        self.assertEqual(self.status()["control_status"], "manual")
        self.assertTrue(self.status()["owned"])
        self.assertIsNone(self.locks.owner(DEVICE.serial))
        self.assertEqual(self.action().status_code, 200)
        self.assertEqual(order, ["automation started", "automation finished", "manual input"])
        self.post("/api/control", {"enabled": False})
        self.assert_available()
        self.assertEqual(result[0].status, "cancelled")
        auto.send.assert_called_once()  # No resumption after manual releases ownership.

    def test_cross_process_runner_wait_wakes_promptly_and_does_not_resume(self):
        code = """
import sys
from unittest.mock import Mock
from android_control import AndroidControl, Device
from automation.runner import Runner
from automation.scripts import SCRIPTS
from device_lock import DeviceLocks
controller = Mock(spec=AndroidControl)
controller.device.return_value = Device('usb-test', 1080, 2400, 0)
controller.send.side_effect = lambda *args: print('unexpected input', flush=True)
def script(context):
    print('waiting', flush=True)
    context.wait(30)
    context.send('home')
SCRIPTS['takeover_test'] = script
result = Runner(controller, DeviceLocks(sys.argv[1])).run('takeover_test')
print(result.status, flush=True)
raise SystemExit(result.exit_code)
"""
        process = subprocess.Popen([sys.executable, "-u", "-c", code, self.temp.name],
                                   cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.close_child, process)
        ready, _, _ = select.select([process.stdout], [], [], 5)
        self.assertTrue(ready)
        self.assertEqual(process.stdout.readline().strip(), "waiting")
        began = time.monotonic()
        self.assertIn(self.takeover().status_code, (202,))
        output, errors = process.communicate(timeout=2)
        self.assertLess(time.monotonic() - began, 2)
        self.assertEqual(process.returncode, 130, errors)
        self.assertEqual(output.strip(), "cancelled")
        self.assertTrue(self.heartbeat().json["enabled"])
        self.assertTrue(self.status()["owned"])
        self.assertEqual(self.action().status_code, 200)

    def test_withdrawn_signal_is_latched_and_saved_context_cannot_send(self):
        auto = controller()
        event = threading.Event()
        with self.locks.hold(DEVICE.serial, "automation") as ownership:
            context = AutomationContext(DEVICE, auto, event, logging.getLogger("test"),
                                        RunState("test"), ownership.takeover_requested)
            self.assertEqual(self.takeover().status_code, 202)
            self.post("/api/control", {"enabled": False})
            self.assertFalse(self.locks.takeover_signal(DEVICE.serial)[0])
            with self.assertRaises(Cancelled): context.send("home")
            self.assertTrue(event.is_set())
            with self.assertRaises(Cancelled): context.send_key("text", "a")
        auto.send.assert_not_called()
        auto.send_key.assert_not_called()
        self.assert_available()

    def test_other_device_signal_and_ownership_are_independent(self):
        with self.locks.hold("second-device", "automation") as second:
            with self.locks.hold(DEVICE.serial, "automation") as first:
                self.assertEqual(self.takeover().status_code, 202)
                self.assertTrue(first.takeover_requested())
                self.assertFalse(second.takeover_requested())
            self.assertTrue(self.heartbeat().json["enabled"])
            self.assertEqual(self.action().status_code, 200)
            self.assertEqual(self.locks.owner("second-device"), "automation")
        self.assertIsNone(self.locks.owner(DEVICE.serial))

    def test_lease_expiry_disables_manual_without_requests(self):
        with patch("app.LEASE_SECONDS", 0.05):
            self.assertTrue(self.enable().json["enabled"])
        deadline = time.monotonic() + 2
        while self.state.owner and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertIsNone(self.state.owner)
        self.assert_available()

    def test_pending_expiry_does_not_rearm_when_automation_finishes(self):
        with self.locks.hold(DEVICE.serial, "automation") as ownership:
            with patch("app.LEASE_SECONDS", 0.05):
                self.assertEqual(self.takeover().status_code, 202)
            deadline = time.monotonic() + 2
            while self.state.owner and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            self.assertIsNone(self.state.owner)
            self.assertTrue(ownership.takeover_requested())
        self.assertFalse(self.heartbeat().json["enabled"])
        self.assert_available()

    def test_health_loss_or_stale_frame_cancels_pending_claim(self):
        for failure in ("camera", "geometry", "frame"):
            with self.subTest(failure=failure):
                self.setUp()
                with self.locks.hold(DEVICE.serial, "automation"):
                    self.assertEqual(self.takeover().status_code, 202)
                    if failure == "camera": self.camera.ready = False
                    if failure == "geometry": self.camera.width = 640
                    result = self.post("/api/heartbeat", {"frame_id": 0 if failure == "frame" else 123})
                    self.assertFalse(result.json.get("enabled", False))
                    self.assertIsNone(self.state.manual_claim)
                self.assert_available()

    def test_other_tab_cannot_take_reserved_manual_lease_or_renew_it(self):
        with self.locks.hold(DEVICE.serial, "automation"):
            self.assertEqual(self.takeover().status_code, 202)
            generation = self.locks.takeover_signal(DEVICE.serial)[1]
            self.assertEqual(self.takeover().status_code, 202)  # Idempotent request.
            self.assertEqual(self.locks.takeover_signal(DEVICE.serial)[1], generation)
            expires = self.state.expires
            self.headers["X-Control-Client"] = "other-browser-123"
            self.assertEqual(self.enable().status_code, 400)
            self.assertEqual(self.heartbeat().json, {"enabled": False, "pending": False})
            self.assertEqual(self.state.expires, expires)
            self.assertFalse(self.status()["pending_owned"])

    def test_automation_can_start_while_manual_lease_is_enabled(self):
        self.assertEqual(self.enable().status_code, 200)
        auto = controller()
        def sending(*args):
            self.assertEqual(self.action().status_code, 409)
            self.assertEqual(self.status()["control_status"], "automation")
        auto.send.side_effect = sending
        with patch("automation.context.AutomationContext.wait"):
            self.assertEqual(Runner(auto, self.locks).run("settings_demo").status, "completed")
        self.controller.send.assert_not_called()
        self.assert_available()
        self.assertEqual(self.enable().status_code, 200)
        self.assertEqual(self.action().status_code, 200)

    def test_acquisition_failure_releases_takeover_reservation(self):
        with self.locks.hold(DEVICE.serial, "automation"):
            self.assertEqual(self.takeover().status_code, 202)
            with patch.object(self.locks, "_acquire", side_effect=OSError("synthetic failure")):
                self.assertEqual(self.heartbeat().status_code, 409)
        self.assertIsNone(self.state.manual_claim)
        self.assertIsNone(self.state.owner)
        self.assert_available()

    def test_normal_manual_requests_never_signal_takeover(self):
        self.assertEqual(self.enable().status_code, 200)
        with self.locks.hold(DEVICE.serial, "automation") as ownership:
            before = self.locks.takeover_signal(DEVICE.serial)
            for action in ("tap", "home", "back", "recents", "screen_on", "screen_off",
                           "swipe_up", "swipe_down", "swipe_left", "swipe_right"):
                result = self.post("/api/action", {"action": action, "x": .5, "y": .5, "frame_id": 123})
                self.assertEqual(result.status_code, 409)
                self.assertEqual(result.json, {"ok": False, "error": "device_busy", "owner": "automation"})
            for kind, value in (("text", "a"), ("key", "enter"), ("key", "backspace"), ("key", "tab")):
                self.assertEqual(self.post("/api/key", {"type": kind, "value": value, "frame_id": 123}).status_code, 409)
            self.assertEqual(self.enable().status_code, 409)
            self.assertFalse(self.heartbeat().json["enabled"])
            self.assertEqual(self.status()["control_status"], "automation")
            self.assertEqual(self.client.get("/api/frame").status_code, 200)
            self.assertEqual(self.locks.takeover_signal(DEVICE.serial), before)
            self.assertFalse(ownership.takeover_requested())
            self.assertIsNone(self.state.manual_claim)
        self.controller.send.assert_not_called()
        self.controller.send_key.assert_not_called()

    def test_takeover_requires_automation_and_local_authorization(self):
        before = self.locks.takeover_signal(DEVICE.serial)
        self.assertEqual(self.takeover().json["error"], "no_automation")
        with self.locks.hold(DEVICE.serial, "manual"):
            self.assertEqual(self.takeover().json["owner"], "manual")
        self.assertEqual(self.locks.takeover_signal(DEVICE.serial), before)
        with self.locks.hold(DEVICE.serial, "automation") as ownership:
            for headers, expected in (({}, 403),
                (dict(self.headers, **{"Sec-Fetch-Site": "cross-site"}), 403),
                (dict(self.headers, **{"X-Control-Client": ""}), 400),
                (dict(self.headers, **{"Host": "attacker.example"}), 400)):
                self.assertEqual(self.client.post("/api/takeover", json={}, headers=headers).status_code, expected)
            for payload in ({"serial": "other"}, {"value": "PRIVATE-SENTINEL"}, [], "text"):
                self.assertEqual(self.post("/api/takeover", payload).status_code, 400)
            self.assertFalse(ownership.takeover_requested())
        self.assertIsNone(self.state.manual_claim)

    def test_takeover_can_cancel_without_calibration_but_cannot_enable_input(self):
        self.state.calibration = None
        with self.locks.hold(DEVICE.serial, "automation") as ownership:
            self.assertEqual(self.takeover().status_code, 202)
            self.assertTrue(ownership.takeover_requested())
        self.assertEqual(self.heartbeat().json, {"enabled": False, "pending": False})
        self.assertEqual(self.status()["control_status"], "free")
        self.assertEqual(self.action().status_code, 400)
        self.assert_available()

    def test_takeover_can_cancel_when_camera_is_unavailable(self):
        self.camera.ready = False
        with self.locks.hold(DEVICE.serial, "automation") as ownership:
            self.assertEqual(self.takeover().status_code, 202)
            self.assertTrue(ownership.takeover_requested())
            self.assertEqual(self.heartbeat().status_code, 409)
        self.assertIsNone(self.state.manual_claim)
        self.assert_available()

    def test_takeover_lifecycle_is_logged_without_input_details(self):
        auto = controller()
        auto.send.side_effect = lambda *args: self.assertEqual(self.takeover().status_code, 202)
        with self.assertLogs(level="INFO") as logs:
            result = Runner(auto, self.locks).run("settings_demo")
            self.assertTrue(self.heartbeat().json["enabled"])
        messages = "\n".join(logs.output)
        for phrase in ("Automation ownership acquired", "Script started", "Takeover requested",
                       "Cancellation detected", "cancelled due to takeover",
                       "Automation ownership released", "Manual control available again"):
            self.assertIn(phrase, messages)
        self.assertEqual(result.status, "cancelled")
        self.assertTrue(result.takeover_requested)
        auto.send.assert_called_once()
