"""Captured screen-keyboard workflow tests; no hardware is required."""

import logging
import os
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from android_control import AndroidControl, Device
from app import create_app
from automation.workflows.models import new_workflow
from automation.input.base import UnsupportedOperation
from automation.workflows.runner import SCREEN_KEYBOARD_KEY_DELAY, WorkflowRunner
from automation.workflows.service import WorkflowService
from automation.workflows.storage import WorkflowStorage
from automation.workflows.validation import validate_workflow
from device_lock import DeviceLocks
from hardware.pi_api_client import PiApiClient
from hardware.raspberry_pi_input import RaspberryPiInputBridge
from test_control import CORNERS, FakeCamera, authenticate_admin

DEVICE = Device("usb-test", 1080, 1920, 0)


def keyboard():
    return {
        "id": "keyboard_numeric",
        "name": "Account number keypad",
        "kind": "numeric",
        "keys": {
            "0": {"x": 0.5, "y": 0.9},
            "1": {"x": 0.25, "y": 0.7},
            "2": {"x": 0.5, "y": 0.7},
            ".": {"x": 0.75, "y": 0.9},
        },
    }


def screen_keyboard_flow(value=None, variable=False):
    result = new_workflow("Numeric input")
    result["id"] = "numeric_flow"
    result["keyboards"] = {"keyboard_numeric": keyboard()}
    step = {
        "id": "input_one",
        "type": "input",
        "source": "variable" if variable else "fixed",
        "input_method": "screen_keyboard",
        "keyboard_id": "keyboard_numeric",
    }
    if variable:
        result["variables"] = [
            {
                "name": "account_no",
                "label": "Account number",
                "type": "text",
                "required": True,
                "default": None,
            }
        ]
        step["variable"] = "account_no"
    else:
        step["value"] = value
    result["steps"] = [step]
    return result


def mapped_flow(kind, keys, value):
    result = new_workflow("Mapped input")
    result["id"] = "mapped_flow"
    result["keyboards"] = {
        "keyboard_mapped": {
            "id": "keyboard_mapped",
            "name": "Mapped keyboard",
            "kind": kind,
            "keys": keys,
        }
    }
    result["steps"] = [
        {
            "id": "mapped_input",
            "type": "input",
            "source": "fixed",
            "value": value,
            "input_method": "screen_keyboard",
            "keyboard_id": "keyboard_mapped",
        }
    ]
    return result


class FakeContext:
    def __init__(self):
        self.state = SimpleNamespace()
        self.logger = logging.getLogger("screen-keyboard-test")
        self.input = Mock()
        self.wait = Mock()
        self.check_cancelled = Mock()


class ScreenKeyboardPlaybackTests(unittest.TestCase):
    def test_two_input_steps_use_their_exact_referenced_keyboard_maps(self):
        workflow = new_workflow("Two numeric keypads")
        workflow["id"] = "two_keypads"
        workflow["keyboards"] = {
            "keyboard_account": {
                "id": "keyboard_account",
                "name": "Account Number Keypad",
                "kind": "numeric",
                "keys": {
                    "1": {"x": 0.11, "y": 0.71},
                    "0": {"x": 0.51, "y": 0.91},
                },
            },
            "keyboard_amount": {
                "id": "keyboard_amount",
                "name": "Amount Keypad",
                "kind": "numeric",
                "keys": {
                    "1": {"x": 0.18, "y": 0.69},
                    "0": {"x": 0.52, "y": 0.88},
                    ".": {"x": 0.22, "y": 0.91},
                },
            },
        }
        workflow["variables"] = [
            {
                "name": "account_no",
                "label": "Account number",
                "type": "text",
                "required": True,
                "default": None,
            },
            {
                "name": "amount",
                "label": "Amount",
                "type": "text",
                "required": True,
                "default": None,
            },
        ]
        workflow["steps"] = [
            {
                "id": "account_input",
                "type": "input",
                "source": "variable",
                "variable": "account_no",
                "input_method": "screen_keyboard",
                "keyboard_id": "keyboard_account",
            },
            {
                "id": "amount_input",
                "type": "input",
                "source": "variable",
                "variable": "amount",
                "input_method": "screen_keyboard",
                "keyboard_id": "keyboard_amount",
            },
        ]
        ctx = FakeContext()
        WorkflowRunner(ctx).run(workflow, {"account_no": "10", "amount": "1.0"})
        self.assertEqual(
            ctx.input.tap.call_args_list,
            [
                call(0.11, 0.71),
                call(0.51, 0.91),
                call(0.18, 0.69),
                call(0.22, 0.91),
                call(0.52, 0.88),
            ],
        )

    def test_referenced_map_never_falls_back_to_another_map(self):
        workflow = mapped_flow(
            "numeric",
            {"1": {"x": 0.2, "y": 0.2}, "0": {"x": 0.5, "y": 0.9}},
            "1.0",
        )
        workflow["keyboards"]["keyboard_other"] = {
            "id": "keyboard_other",
            "name": "Other keypad",
            "kind": "numeric",
            "keys": {".": {"x": 0.9, "y": 0.9}},
        }
        ctx = FakeContext()
        with self.assertRaisesRegex(UnsupportedOperation, r"'\.'.*No input sent"):
            WorkflowRunner(ctx).run(workflow)
        ctx.input.tap.assert_not_called()

    def test_missing_referenced_keyboard_fails_validation_before_input(self):
        workflow = screen_keyboard_flow("1")
        workflow["steps"][0]["keyboard_id"] = "keyboard_missing"
        ctx = FakeContext()
        with self.assertRaisesRegex(ValueError, "undefined captured keyboard"):
            WorkflowRunner(ctx).run(workflow)
        ctx.input.tap.assert_not_called()

    def test_alphabetic_lowercase_repeated_letters_and_spaces_tap_in_order(self):
        keys = {
            "h": {"x": 0.1, "y": 0.7},
            "e": {"x": 0.2, "y": 0.7},
            "l": {"x": 0.3, "y": 0.7},
            "o": {"x": 0.4, "y": 0.7},
            " ": {"x": 0.5, "y": 0.9},
        }
        ctx = FakeContext()
        WorkflowRunner(ctx).run(mapped_flow("alphabetic", keys, "hello hello"))
        expected = [
            call(0.1, 0.7),
            call(0.2, 0.7),
            call(0.3, 0.7),
            call(0.3, 0.7),
            call(0.4, 0.7),
            call(0.5, 0.9),
        ]
        self.assertEqual(ctx.input.tap.call_args_list, expected + expected[:-1])

    def test_alphabetic_uppercase_fails_before_first_tap_without_shift(self):
        ctx = FakeContext()
        with self.assertRaisesRegex(
            UnsupportedOperation, "no captured Shift/case-toggle key.*'H'.*No input sent"
        ):
            WorkflowRunner(ctx).run(
                mapped_flow("alphabetic", {"h": {"x": 0.1, "y": 0.2}}, "Hello")
            )
        ctx.input.tap.assert_not_called()

    def test_alphabetic_case_toggle_taps_preserve_mixed_case_order(self):
        keys = {
            "a": {"x": 0.1, "y": 0.7},
            "b": {"x": 0.2, "y": 0.7},
            "c": {"x": 0.3, "y": 0.7},
            "d": {"x": 0.4, "y": 0.7},
            "shift": {"x": 0.05, "y": 0.8},
        }
        for value in ("abc", "Abc", "abC", "AbC", "ABC", "aBcD"):
            with self.subTest(value=value):
                labels = []
                for character in value:
                    if "A" <= character <= "Z":
                        labels.extend(("shift", character.lower()))
                    else:
                        labels.append(character)
                ctx = FakeContext()
                WorkflowRunner(ctx).run(mapped_flow("alphabetic", keys, value))
                self.assertEqual(
                    ctx.input.tap.call_args_list,
                    [call(keys[label]["x"], keys[label]["y"]) for label in labels],
                )

    def test_alphabetic_case_toggle_replay_uses_pi_tap_api(self):
        pi = Mock(spec=PiApiClient)
        pi.base_url = "http://pi.test:8766"
        ctx = FakeContext()
        ctx.input = RaspberryPiInputBridge(pi)
        keys = {
            "a": {"x": 0.1, "y": 0.7},
            "b": {"x": 0.2, "y": 0.7},
            "shift": {"x": 0.05, "y": 0.8},
        }

        WorkflowRunner(ctx).run(mapped_flow("alphabetic", keys, "Ab"))

        self.assertEqual(
            pi.tap.call_args_list,
            [call(0.05, 0.8), call(0.1, 0.7), call(0.2, 0.7)],
        )
        pi.type_text.assert_not_called()

    def test_alphabetic_mapping_with_digits_types_mixed_values(self):
        labels = set("abcuser120236")
        keys = {
            label: {"x": (index + 1) / 20, "y": 0.7}
            for index, label in enumerate(sorted(labels))
        }
        for value in ("abc123", "user2026", "a1b2c3"):
            with self.subTest(value=value):
                ctx = FakeContext()
                WorkflowRunner(ctx).run(mapped_flow("alphabetic", keys, value))
                self.assertEqual(
                    ctx.input.tap.call_args_list,
                    [call(keys[label]["x"], keys[label]["y"]) for label in value],
                )

    def test_custom_labels_play_back_exact_case_and_punctuation(self):
        labels = "AB-12"
        keys = {
            label: {"x": index / 10, "y": 0.8} for index, label in enumerate(labels, 1)
        }
        ctx = FakeContext()
        WorkflowRunner(ctx).run(mapped_flow("custom", keys, labels))
        self.assertEqual(
            ctx.input.tap.call_args_list,
            [call(index / 10, 0.8) for index in range(1, 6)],
        )

    def test_dynamic_numeric_value_taps_each_coordinate_once_in_order(self):
        workflow = screen_keyboard_flow(variable=True)
        ctx = FakeContext()
        WorkflowRunner(ctx).run(workflow, {"account_no": "1201"})
        self.assertEqual(
            ctx.input.tap.call_args_list,
            [call(0.25, 0.7), call(0.5, 0.7), call(0.5, 0.9), call(0.25, 0.7)],
        )
        self.assertEqual(ctx.wait.call_args_list, [call(SCREEN_KEYBOARD_KEY_DELAY)] * 3)
        ctx.input.type_text.assert_not_called()

    def test_missing_runtime_variable_fails_before_input(self):
        ctx = FakeContext()
        with self.assertRaisesRegex(ValueError, "account_no"):
            WorkflowRunner(ctx).run(screen_keyboard_flow(variable=True), {})
        ctx.input.tap.assert_not_called()
        ctx.input.type_text.assert_not_called()

    def test_decimal_is_preserved_exactly_when_mapped(self):
        ctx = FakeContext()
        WorkflowRunner(ctx).run(screen_keyboard_flow("12.0"))
        self.assertEqual(
            ctx.input.tap.call_args_list,
            [call(0.25, 0.7), call(0.5, 0.7), call(0.75, 0.9), call(0.5, 0.9)],
        )

    def test_failed_tap_is_not_retried_and_stops_remaining_characters(self):
        ctx = FakeContext()
        ctx.input.tap.side_effect = [None, RuntimeError("uncertain tap")]
        with self.assertRaisesRegex(RuntimeError, "uncertain tap"):
            WorkflowRunner(ctx).run(screen_keyboard_flow("111"))
        self.assertEqual(ctx.input.tap.call_count, 2)

    def test_missing_character_fails_before_first_tap(self):
        ctx = FakeContext()
        with self.assertRaisesRegex(UnsupportedOperation, "'5'.*No input sent"):
            WorkflowRunner(ctx).run(screen_keyboard_flow("15"))
        ctx.input.tap.assert_not_called()
        ctx.wait.assert_not_called()

    def test_run_status_reports_missing_characters_without_hardware_input(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, PHONE_INPUT_BACKEND="mock", PHONE_SCREEN_BACKEND="mock"
        ):
            controller = Mock(spec=AndroidControl)
            service = WorkflowService(
                controller, DeviceLocks(Path(directory) / "locks")
            )
            snapshot = service.start(screen_keyboard_flow("15"))
            item = service.runs[snapshot["run_id"]]
            item["thread"].join(3)
            result = service.snapshot(snapshot["run_id"])
            self.assertEqual(result["status"], "failed")
            self.assertIn("'5'", result["message"])
            self.assertIn("No input sent", result["message"])
            controller.assert_not_called()

    def test_legacy_and_explicit_hid_input_remain_unchanged(self):
        for method in (None, "hid"):
            workflow = new_workflow("HID")
            step = {"id": "input", "type": "input", "source": "fixed", "value": "12"}
            if method:
                step["input_method"] = method
            workflow["steps"] = [step]
            ctx = FakeContext()
            WorkflowRunner(ctx).run(workflow)
            ctx.input.type_text.assert_called_once_with("12")
            ctx.input.tap.assert_not_called()

    def test_sensitive_metadata_uses_normal_hid_workflow_execution(self):
        workflow = new_workflow("Credential input")
        workflow["variables"] = [
            {
                "name": "password",
                "label": "Password",
                "type": "text",
                "required": True,
                "default": None,
                "sensitive": True,
            }
        ]
        workflow["steps"] = [
            {
                "id": "credential_input",
                "type": "input",
                "source": "variable",
                "variable": "password",
                "sensitive": True,
            }
        ]
        ctx = FakeContext()

        WorkflowRunner(ctx).run(workflow, {"password": "AbC123"})

        ctx.input.type_text.assert_called_once_with("AbC123")
        ctx.input.tap.assert_not_called()

    def test_malformed_and_protected_keyboard_data_is_rejected(self):
        for mutate in (
            lambda flow: flow["keyboards"]["keyboard_numeric"]["keys"]["1"].update(
                x=1.1
            ),
            lambda flow: flow["keyboards"]["keyboard_numeric"].update(keys={}),
            lambda flow: flow["keyboards"]["keyboard_numeric"].update(
                kind="randomized"
            ),
            lambda flow: flow["steps"][0].update(label="Final payment authorization"),
        ):
            workflow = screen_keyboard_flow("1")
            mutate(workflow)
            with self.subTest(workflow=workflow), self.assertRaises(ValueError):
                validate_workflow(workflow)

    def test_sensitive_keyboard_names_and_input_labels_are_supported(self):
        workflow = screen_keyboard_flow("1")
        workflow["keyboards"]["keyboard_numeric"]["name"] = "PIN keypad"
        workflow["steps"][0]["label"] = "Enter OTP"
        validated = validate_workflow(workflow)
        self.assertEqual(
            validated["keyboards"]["keyboard_numeric"]["name"], "PIN keypad"
        )

    def test_custom_map_rejects_empty_control_and_oversized_labels(self):
        for label in ("", "   ", "line\nfeed", "control\x01", "x" * 33):
            with self.subTest(label=repr(label)), self.assertRaises(ValueError):
                validate_workflow(
                    mapped_flow("custom", {label: {"x": 0.2, "y": 0.3}}, "")
                )

    def test_alphabetic_stored_labels_must_be_canonical_lowercase(self):
        with self.assertRaisesRegex(ValueError, "lowercase a-z"):
            validate_workflow(
                mapped_flow("alphabetic", {"A": {"x": 0.2, "y": 0.3}}, "a")
            )

    def test_old_numeric_map_and_workflow_without_keyboards_remain_valid(self):
        numeric = screen_keyboard_flow("12.0")
        numeric["keyboards"]["keyboard_numeric"]["keys"]["BACKSPACE"] = {
            "x": 0.9,
            "y": 0.9,
        }
        validate_workflow(numeric)
        old = {
            "id": "old_flow",
            "name": "Old HID flow",
            "version": 1,
            "variables": [],
            "steps": [
                {
                    "id": "old_input",
                    "type": "input",
                    "source": "fixed",
                    "value": "unchanged",
                }
            ],
            "financial": False,
        }
        self.assertNotIn("keyboards", validate_workflow(old))

    def test_workflow_rejects_duplicate_keyboard_names(self):
        workflow = screen_keyboard_flow("1")
        duplicate = deepcopy(workflow["keyboards"]["keyboard_numeric"])
        duplicate["id"] = "keyboard_duplicate"
        duplicate["name"] = " account NUMBER keypad "
        workflow["keyboards"]["keyboard_duplicate"] = duplicate
        with self.assertRaisesRegex(ValueError, "names must be unique"):
            validate_workflow(workflow)


class ScreenKeyboardCaptureTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ, PHONE_INPUT_BACKEND="appium", PHONE_SCREEN_BACKEND="appium"
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.controller = Mock(spec=AndroidControl)
        self.controller.device.return_value = DEVICE
        self.camera = FakeCamera()
        self.store = WorkflowStorage(Path(self.temp.name) / "flows")
        self.app = create_app(
            self.camera,
            self.controller,
            DeviceLocks(Path(self.temp.name) / "locks"),
            self.store,
            manual_backend="appium",
        )
        self.client = self.app.test_client()
        authenticate_admin(self.client)
        self.headers = {
            "X-Control-Token": self.app.config["CONTROL_TOKEN"],
            "X-Control-Client": "keyboard-test-client",
        }
        self.post("/api/calibration", {"corners": CORNERS, "frame_id": 123})
        self.post("/api/control", {"enabled": True, "frame_id": 123})
        self.post("/api/recorder/start", {"name": "Captured", "execute": False})

    def post(self, path, data, status=200):
        response = self.client.post(path, json=data, headers=self.headers)
        self.assertEqual(response.status_code, status, response.json)
        return response.json

    def capture_numeric(self, name="Account keypad", labels=("1", "2")):
        self.post(
            "/api/recorder/keyboard-capture/start",
            {"name": name, "kind": "numeric", "keys": list(labels)},
        )
        for index, label in enumerate(labels, 1):
            self.post(
                "/api/recorder/keyboard-capture/key",
                {"key": label, "point": [index / 10, 0.7]},
            )
        result = self.post("/api/recorder/keyboard-capture/save", {})
        return next(
            key
            for key, item in result["workflow"]["keyboards"].items()
            if item["name"] == name
        )

    def variable_request(self, keyboard_id, *, sample="12", type_sample=False):
        return {
            "variable": {
                "name": "account_no",
                "label": "Account number",
                "type": "text",
                "required": True,
                "default": sample or None,
                "ask_when_running": True,
            },
            "input_method": "screen_keyboard",
            "keyboard_id": keyboard_id,
            "sample": sample,
            "type_sample": type_sample,
            "frame_id": 123,
        }

    def test_recording_variable_step_only_persists_default_without_phone_input(self):
        keyboard_id = self.capture_numeric()
        self.controller.reset_mock()
        result = self.post(
            "/api/recorder/variable-input",
            self.variable_request(keyboard_id, sample="1212"),
        )
        self.assertTrue(result["active"])
        step = result["workflow"]["steps"][0]
        self.assertEqual(
            step,
            {
                "id": step["id"],
                "type": "input",
                "source": "variable",
                "variable": "account_no",
                "input_method": "screen_keyboard",
                "keyboard_id": keyboard_id,
            },
        )
        self.assertEqual(result["workflow"]["variables"][0]["default"], "1212")
        self.controller.send.assert_not_called()
        self.controller.send_key.assert_not_called()

    def test_recording_variable_default_types_once_then_saves_semantic_step(self):
        keyboard_id = self.capture_numeric()
        self.controller.reset_mock()
        result = self.post(
            "/api/recorder/variable-input",
            self.variable_request(keyboard_id, sample="121", type_sample=True),
        )
        self.assertTrue(result["active"])
        self.assertEqual(
            self.controller.send.call_args_list,
            [
                call(DEVICE, "tap", (108, 1343)),
                call(DEVICE, "tap", (216, 1343)),
                call(DEVICE, "tap", (108, 1343)),
            ],
        )
        self.assertEqual(result["workflow"]["variables"][0]["default"], "121")
        self.assertEqual(result["workflow"]["steps"][0]["variable"], "account_no")
        self.post("/api/recorder/stop", {})
        saved = self.post("/api/recorder/save", {}, 201)
        loaded = self.client.get(f"/api/workflows/{saved['id']}").json
        self.assertEqual(loaded["variables"][0]["default"], "121")
        self.assertEqual(loaded["steps"][0]["variable"], "account_no")

    def test_recording_amount_sample_formats_once_before_screen_keyboard_typing(self):
        keyboard_id = self.capture_numeric(labels=("0", "2", "5"))
        self.controller.reset_mock()
        request = self.variable_request(
            keyboard_id, sample="25.50", type_sample=True
        )
        request["variable"] = {
            **request["variable"],
            "name": "amount",
            "label": "Amount",
        }
        request["amount_format"] = "implied_cents"

        result = self.post("/api/recorder/variable-input", request)

        self.assertEqual(
            self.controller.send.call_args_list,
            [
                call(DEVICE, "tap", (216, 1343)),
                call(DEVICE, "tap", (324, 1343)),
                call(DEVICE, "tap", (324, 1343)),
                call(DEVICE, "tap", (108, 1343)),
            ],
        )
        step = result["workflow"]["steps"][0]
        self.assertEqual(step["variable"], "amount")
        self.assertEqual(step["amount_format"], "implied_cents")
        self.assertEqual(result["workflow"]["variables"][0]["default"], "25.50")

    def test_recording_variable_rejects_typing_an_empty_default(self):
        keyboard_id = self.capture_numeric()
        self.controller.reset_mock()
        response = self.client.post(
            "/api/recorder/variable-input",
            json=self.variable_request(keyboard_id, sample="", type_sample=True),
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400, response.json)
        self.assertIn("Enter a Default value", response.json["error"])
        self.controller.send.assert_not_called()
        snapshot = self.client.get("/api/recorder", headers=self.headers).json
        self.assertEqual(snapshot["workflow"]["steps"], [])

    def test_recording_sample_preflight_failure_sends_nothing_and_keeps_recording(self):
        keyboard_id = self.capture_numeric(labels=("1",))
        self.controller.reset_mock()
        response = self.client.post(
            "/api/recorder/variable-input",
            json=self.variable_request(keyboard_id, sample="12", type_sample=True),
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400, response.json)
        self.assertIn("No input sent", response.json["error"])
        self.controller.send.assert_not_called()
        snapshot = self.client.get("/api/recorder", headers=self.headers).json
        self.assertTrue(snapshot["active"])
        self.assertEqual(snapshot["workflow"]["steps"], [])

    def test_capture_maps_camera_coordinates_without_phone_input_and_stays_live(self):
        started = self.post(
            "/api/recorder/keyboard-capture/start",
            {"name": "Account keypad", "keys": ["1", "."]},
        )
        self.assertTrue(started["active"])
        self.assertIsNotNone(started["keyboard_capture"])
        first = self.post(
            "/api/recorder/keyboard-capture/key",
            {"key": "1", "camera_point": [0.5, 0.5], "frame_id": 123},
        )
        point = first["keyboard_capture"]["keys"]["1"]
        self.assertAlmostEqual(point["x"], 0.5)
        self.assertAlmostEqual(point["y"], 0.5)
        self.post(
            "/api/recorder/keyboard-capture/key",
            {"key": ".", "camera_point": [0.7, 0.7], "frame_id": 123},
        )
        saved_capture = self.post("/api/recorder/keyboard-capture/save", {})
        self.assertTrue(saved_capture["active"])
        self.assertIsNone(saved_capture["keyboard_capture"])
        captured = next(iter(saved_capture["workflow"]["keyboards"].values()))
        self.assertEqual(set(captured["keys"]), {"1", "."})
        self.controller.send.assert_not_called()
        self.controller.send_key.assert_not_called()
        self.assertEqual(
            self.client.post(
                "/api/heartbeat", json={"frame_id": 123}, headers=self.headers
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get("/api/frame").status_code, 200)

    def test_alphabetic_capture_saves_lowercase_digits_and_normalized_points(self):
        started = self.post(
            "/api/recorder/keyboard-capture/start",
            {
                "name": "App keyboard",
                "kind": "alphabetic",
                "keys": ["a", "b", "1", " ", "BACKSPACE", "SHIFT"],
            },
        )
        self.assertEqual(
            started["keyboard_capture"]["requested_keys"],
            ["a", "b", "1", " ", "backspace", "shift"],
        )
        points = {
            "b": [0.2, 0.3],
            "1": [0.3, 0.3],
            " ": [0.5, 0.9],
            "backspace": [0.9, 0.8],
            "shift": [0.05, 0.8],
        }
        self.post(
            "/api/recorder/keyboard-capture/key",
            {"key": "a", "camera_point": [0.18, 0.26], "frame_id": 123},
        )
        for label, point in points.items():
            self.post(
                "/api/recorder/keyboard-capture/key",
                {"key": label, "point": point},
            )
        result = self.post("/api/recorder/keyboard-capture/save", {})
        captured = next(iter(result["workflow"]["keyboards"].values()))
        self.assertEqual(captured["kind"], "alphabetic")
        self.assertAlmostEqual(captured["keys"]["a"]["x"], 0.1)
        self.assertAlmostEqual(captured["keys"]["a"]["y"], 0.2)
        self.assertEqual(captured["keys"][" "], {"x": 0.5, "y": 0.9})
        self.assertEqual(captured["keys"]["backspace"], {"x": 0.9, "y": 0.8})
        self.assertEqual(captured["keys"]["shift"], {"x": 0.05, "y": 0.8})
        self.assertTrue(result["active"])
        self.controller.send.assert_not_called()
        self.controller.send_key.assert_not_called()

    def test_alphabetic_capture_rejects_uppercase_instead_of_lowercasing(self):
        result = self.post(
            "/api/recorder/keyboard-capture/start",
            {
                "name": "Uppercase keyboard",
                "kind": "alphabetic",
                "keys": ["A", "b"],
            },
            status=400,
        )
        self.assertIn("lowercase a-z", result["error"])

    def test_custom_capture_saves_exact_labels(self):
        labels = ["A", "B", "-", "1", "2"]
        started = self.post(
            "/api/recorder/keyboard-capture/start",
            {"name": "Special keyboard", "kind": "custom", "keys": labels},
        )
        self.assertEqual(started["keyboard_capture"]["requested_keys"], labels)
        for index, label in enumerate(labels, 1):
            self.post(
                "/api/recorder/keyboard-capture/key",
                {"key": label, "point": [index / 10, 0.8]},
            )
        result = self.post("/api/recorder/keyboard-capture/save", {})
        captured = next(iter(result["workflow"]["keyboards"].values()))
        self.assertEqual(captured["kind"], "custom")
        self.assertEqual(set(captured["keys"]), set(labels))
        self.assertEqual(captured["keys"]["A"], {"x": 0.1, "y": 0.8})

    def test_second_keyboard_has_unique_id_and_does_not_overwrite_first(self):
        saved_ids = []
        for index, (name, key, point) in enumerate(
            (
                ("Account Number Keypad", "1", [0.2, 0.7]),
                ("Amount Keypad", ".", [0.22, 0.91]),
            )
        ):
            started = self.post(
                "/api/recorder/keyboard-capture/start",
                {"name": name, "kind": "numeric", "keys": [key]},
            )
            self.assertTrue(started["active"])
            self.assertEqual(len(started["workflow"]["keyboards"]), index)
            if index == 1:
                heartbeat = self.client.post(
                    "/api/heartbeat",
                    json={"frame_id": 123},
                    headers=self.headers,
                )
                self.assertEqual(heartbeat.status_code, 200)
                self.assertEqual(self.client.get("/api/frame").status_code, 200)
            self.post(
                "/api/recorder/keyboard-capture/key",
                {"key": key, "point": point},
            )
            result = self.post("/api/recorder/keyboard-capture/save", {})
            self.assertTrue(result["active"])
            saved_ids = list(result["workflow"]["keyboards"])
        self.assertEqual(len(saved_ids), 2)
        self.assertEqual(len(set(saved_ids)), 2)
        maps = result["workflow"]["keyboards"]
        self.assertEqual(
            {item["name"] for item in maps.values()},
            {"Account Number Keypad", "Amount Keypad"},
        )
        account = next(
            item for item in maps.values() if item["name"].startswith("Account")
        )
        amount = next(
            item for item in maps.values() if item["name"].startswith("Amount")
        )
        self.assertEqual(account["keys"]["1"], {"x": 0.2, "y": 0.7})
        self.assertEqual(amount["keys"]["."], {"x": 0.22, "y": 0.91})

    def test_duplicate_keyboard_names_are_rejected_case_insensitively(self):
        self.post(
            "/api/recorder/keyboard-capture/start",
            {"name": "Amount Keypad", "kind": "numeric", "keys": ["1"]},
        )
        self.post(
            "/api/recorder/keyboard-capture/key",
            {"key": "1", "point": [0.2, 0.7]},
        )
        self.post("/api/recorder/keyboard-capture/save", {})
        self.post(
            "/api/recorder/keyboard-capture/start",
            {"name": " amount keypad ", "kind": "numeric", "keys": ["2"]},
            400,
        )

    def test_delete_unused_keyboard_and_block_referenced_keyboard(self):
        keyboard_ids = []
        for name, key in (("Account keypad", "1"), ("Unused keypad", "2")):
            self.post(
                "/api/recorder/keyboard-capture/start",
                {"name": name, "kind": "numeric", "keys": [key]},
            )
            self.post(
                "/api/recorder/keyboard-capture/key",
                {"key": key, "point": [0.2, 0.7]},
            )
            result = self.post("/api/recorder/keyboard-capture/save", {})
            keyboard_ids = list(result["workflow"]["keyboards"])
        account_id = next(
            key
            for key, item in result["workflow"]["keyboards"].items()
            if item["name"] == "Account keypad"
        )
        unused_id = next(key for key in keyboard_ids if key != account_id)
        self.post(
            "/api/recorder/step",
            {
                "step": {
                    "type": "input",
                    "source": "fixed",
                    "value": "1",
                    "input_method": "screen_keyboard",
                    "keyboard_id": account_id,
                }
            },
        )
        blocked = self.client.post(
            "/api/recorder/keyboard-delete",
            json={"keyboard_id": account_id},
            headers=self.headers,
        )
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("used by an input step", blocked.json["error"])
        deleted = self.post("/api/recorder/keyboard-delete", {"keyboard_id": unused_id})
        self.assertIn(account_id, deleted["workflow"]["keyboards"])
        self.assertNotIn(unused_id, deleted["workflow"]["keyboards"])

    def test_map_survives_save_load_and_edit(self):
        self.post(
            "/api/recorder/keyboard-capture/start",
            {"name": "Amount keypad", "keys": ["1"]},
        )
        self.post(
            "/api/recorder/keyboard-capture/key",
            {"key": "1", "point": [0.25, 0.75]},
        )
        captured = self.post("/api/recorder/keyboard-capture/save", {})
        keyboard_id = next(iter(captured["workflow"]["keyboards"]))
        input_step = self.post(
            "/api/recorder/step",
            {"step": {"type": "input", "source": "fixed", "value": "1"}},
        )["workflow"]["steps"][0]
        self.post("/api/recorder/stop", {})
        reviewed = self.post(
            "/api/recorder/review",
            {
                "operation": "input",
                "step_id": input_step["id"],
                "source": "variable",
                "variable": {
                    "name": "account_no",
                    "label": "Account number",
                    "type": "text",
                    "required": True,
                    "default": None,
                },
                "input_method": "screen_keyboard",
                "keyboard_id": keyboard_id,
            },
        )
        self.assertEqual(reviewed["workflow"]["steps"][0]["keyboard_id"], keyboard_id)
        saved = self.post("/api/recorder/save", {}, 201)
        loaded = self.client.get(f"/api/workflows/{saved['id']}").json
        self.assertEqual(
            loaded["keyboards"][keyboard_id]["keys"]["1"],
            {"x": 0.25, "y": 0.75},
        )
        self.assertEqual(loaded["steps"][0]["input_method"], "screen_keyboard")
        reopened = WorkflowStorage(self.store.directory).read(saved["id"])
        self.assertEqual(reopened["steps"][0]["keyboard_id"], keyboard_id)
        self.assertIn(keyboard_id, reopened["keyboards"])
        edited = self.post("/api/recorder/edit", {"workflow_id": saved["id"]})
        self.assertIn(keyboard_id, edited["workflow"]["keyboards"])
        self.post(
            "/api/recorder/keyboard-capture/start",
            {"name": "Second keypad", "keys": ["2"]},
        )
        self.post(
            "/api/recorder/keyboard-capture/key",
            {"key": "2", "point": [0.4, 0.8]},
        )
        updated = self.post("/api/recorder/keyboard-capture/save", {})
        self.assertEqual(len(updated["workflow"]["keyboards"]), 2)
        self.assertEqual(updated["workflow"]["steps"][0]["keyboard_id"], keyboard_id)

    def test_cancel_and_incomplete_save_leave_workflow_unchanged(self):
        before = deepcopy(self.app.extensions["workflow_recorder"].workflow)
        self.post(
            "/api/recorder/keyboard-capture/start",
            {"name": "Temporary keypad", "keys": ["1", "2"]},
        )
        self.post(
            "/api/recorder/keyboard-capture/key", {"key": "1", "point": [0.1, 0.2]}
        )
        self.post("/api/recorder/keyboard-capture/save", {}, 400)
        cancelled = self.post("/api/recorder/keyboard-capture/cancel", {})
        self.assertEqual(cancelled["workflow"], before)
        self.assertTrue(cancelled["active"])

    def test_duplicate_labels_and_malformed_points_are_rejected(self):
        self.post(
            "/api/recorder/keyboard-capture/start",
            {"name": "Duplicate", "keys": ["1", "1"]},
            400,
        )
        self.post(
            "/api/recorder/keyboard-capture/start",
            {"name": "Numeric", "keys": ["1"]},
        )
        self.post(
            "/api/recorder/keyboard-capture/key",
            {"key": "1", "point": [2, 0.5]},
            400,
        )

    def test_duplicate_and_malformed_custom_capture_labels_are_rejected(self):
        for keys in (["A", "A"], [""], ["line\nfeed"], ["x" * 33]):
            with self.subTest(keys=keys):
                self.post(
                    "/api/recorder/keyboard-capture/start",
                    {"name": "Custom", "kind": "custom", "keys": keys},
                    400,
                )

    def test_capture_is_blocked_after_manual_security_checkpoint(self):
        self.post(
            "/api/recorder/step",
            {
                "step": {
                    "type": "manual_checkpoint",
                    "message": "Complete secure authentication manually.",
                }
            },
        )
        self.post(
            "/api/recorder/keyboard-capture/start",
            {"name": "Numeric", "keys": ["1"]},
            400,
        )

    def test_pi_manual_capture_keeps_heartbeat_and_sends_no_teaching_tap(self):
        pi = Mock(spec=PiApiClient)
        pi.base_url = "http://pi.test:8766"
        pi.status.return_value = {"ok": True, "connected": True}
        app = create_app(
            self.camera,
            self.controller,
            DeviceLocks(Path(self.temp.name) / "pi-locks"),
            WorkflowStorage(Path(self.temp.name) / "pi-flows"),
            manual_backend="raspberry_pi",
            pi_client=pi,
        )
        client = app.test_client()
        authenticate_admin(client)
        headers = {
            "X-Control-Token": app.config["CONTROL_TOKEN"],
            "X-Control-Client": "pi-keyboard-client",
        }

        def post(path, data):
            response = client.post(path, json=data, headers=headers)
            self.assertEqual(response.status_code, 200, response.json)
            return response.json

        client.get("/api/pi/status")
        post("/api/calibration", {"corners": CORNERS, "frame_id": 123})
        post("/api/control", {"enabled": True, "frame_id": 123})
        post("/api/recorder/start", {"name": "Pi capture", "execute": True})
        pi.reset_mock()
        post(
            "/api/recorder/keyboard-capture/start",
            {"name": "Stable numeric", "keys": ["1"]},
        )
        post(
            "/api/recorder/keyboard-capture/key",
            {"key": "1", "camera_point": [0.5, 0.5], "frame_id": 123},
        )
        self.assertTrue(post("/api/heartbeat", {"frame_id": 123})["enabled"])
        pi.tap.assert_not_called()
        self.assertEqual(pi.mock_calls, [])

    def test_pi_manual_variable_sample_uses_saved_map_once_and_keeps_semantic_step(
        self,
    ):
        pi = Mock(spec=PiApiClient)
        pi.base_url = "http://pi.test:8766"
        pi.status.return_value = {"ok": True, "connected": True}
        app = create_app(
            self.camera,
            self.controller,
            DeviceLocks(Path(self.temp.name) / "pi-variable-locks"),
            WorkflowStorage(Path(self.temp.name) / "pi-variable-flows"),
            manual_backend="raspberry_pi",
            pi_client=pi,
        )
        client = app.test_client()
        authenticate_admin(client)
        headers = {
            "X-Control-Token": app.config["CONTROL_TOKEN"],
            "X-Control-Client": "pi-variable-client",
        }

        def post(path, data, status=200):
            response = client.post(path, json=data, headers=headers)
            self.assertEqual(response.status_code, status, response.json)
            return response.json

        client.get("/api/pi/status")
        post("/api/calibration", {"corners": CORNERS, "frame_id": 123})
        post("/api/control", {"enabled": True, "frame_id": 123})
        post("/api/recorder/start", {"name": "Pi variable", "execute": True})
        post(
            "/api/recorder/keyboard-capture/start",
            {"name": "Account keypad", "keys": ["1", "2"]},
        )
        post(
            "/api/recorder/keyboard-capture/key",
            {"key": "1", "point": [0.1, 0.7]},
        )
        post(
            "/api/recorder/keyboard-capture/key",
            {"key": "2", "point": [0.2, 0.7]},
        )
        captured = post("/api/recorder/keyboard-capture/save", {})
        keyboard_id = next(iter(captured["workflow"]["keyboards"]))
        pi.reset_mock()
        first_tap = threading.Event()
        release_tap = threading.Event()

        def delayed_tap(*_point):
            if not first_tap.is_set():
                first_tap.set()
                release_tap.wait(2)

        pi.tap.side_effect = delayed_tap
        result_holder = {}

        def type_sample():
            worker = app.test_client()
            authenticate_admin(worker)
            response = worker.post(
                "/api/recorder/variable-input",
                json=self.variable_request(keyboard_id, sample="121", type_sample=True),
                headers=headers,
            )
            result_holder["status"] = response.status_code
            result_holder["body"] = response.json

        thread = threading.Thread(target=type_sample)
        thread.start()
        self.assertTrue(first_tap.wait(1))
        busy_action = client.post(
            "/api/action",
            json={"action": "back", "frame_id": 123},
            headers=headers,
        )
        self.assertEqual(busy_action.status_code, 409)
        self.assertIn("Controller busy", busy_action.json["error"])
        busy_sample = client.post(
            "/api/recorder/variable-input",
            json={
                **self.variable_request(keyboard_id, sample="12", type_sample=True),
                "variable": {
                    **self.variable_request(keyboard_id)["variable"],
                    "name": "other_value",
                    "label": "Other value",
                },
            },
            headers=headers,
        )
        self.assertEqual(busy_sample.status_code, 409)
        self.assertIn("sample was not sent", busy_sample.json["error"])
        release_tap.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_holder["status"], 200, result_holder["body"])
        result = result_holder["body"]
        self.assertEqual(
            pi.tap.call_args_list,
            [call(0.1, 0.7), call(0.2, 0.7), call(0.1, 0.7)],
        )
        pi.back.assert_not_called()
        variable = result["workflow"]["variables"][0]
        step = result["workflow"]["steps"][0]
        self.assertEqual(variable["default"], "121")
        self.assertEqual(step["variable"], "account_no")
        self.assertEqual(step["source"], "variable")
        self.assertNotIn("value", step)
        self.assertNotIn("121", repr(step))
        self.assertTrue(result["active"])


if __name__ == "__main__":
    unittest.main()
