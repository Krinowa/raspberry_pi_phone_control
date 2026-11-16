"""Record-first capture, privacy, grouped text and post-recording review."""

import base64
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from android_control import AndroidControl, Device
from app import create_app
from automation.workflows.capture import tap_preview
from automation.workflows.recorder import Recorder
from automation.workflows.storage import WorkflowStorage
from automation.workflows.validation import validate_workflow, resolve_variables
from device_lock import DeviceLocks
from test_control import FakeCamera, CORNERS, authenticate_admin

DEVICE = Device("usb-test", 1080, 1920, 0)
NORMAL = '<hierarchy><node class="android.widget.EditText" focused="true" password="false" resource-id="app:id/customer_name" hint="Customer Name" bounds="[10,10][300,90]" /></hierarchy>'
SECURE = '<hierarchy><node class="android.widget.EditText" focused="true" password="true" resource-id="app:id/password" text="existing-secret" bounds="[10,10][300,90]" /></hierarchy>'


class RecordFirstTests(unittest.TestCase):
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
        self.controller.dump_ui.return_value = NORMAL
        self.controller.screenshot.return_value = b""
        self.store = WorkflowStorage(Path(self.temp.name) / "data")
        self.app = create_app(
            FakeCamera(),
            self.controller,
            DeviceLocks(Path(self.temp.name) / "locks"),
            self.store,
        )
        self.client = self.app.test_client()
        authenticate_admin(self.client)
        self.headers = {
            "X-Control-Client": "test-record-first",
            "X-Control-Token": self.app.config["CONTROL_TOKEN"],
        }
        self.post("/api/calibration", {"corners": CORNERS, "frame_id": 123})
        self.post("/api/control", {"enabled": True, "frame_id": 123})
        self.post("/api/recorder/start", {})
        self.addCleanup(self.app.extensions["control_state"].disable)

    def post(self, path, data, status=200):
        response = self.client.post(path, json=data, headers=self.headers)
        self.assertEqual(response.status_code, status, response.json)
        return response.json

    def capture(self, step):
        return self.post(
            "/api/recorder/step", {"capture": True, "step": step, "frame_id": 123}
        )

    def review(self, **data):
        return self.post("/api/recorder/review", data)

    def test_live_and_review_screenshot_markers_use_the_same_semantic_step(self):
        self.controller.reset_mock()
        live = self.post(
            "/api/recorder/step",
            {
                "capture": False,
                "step": {"type": "capture_screen", "label": "Screenshot"},
            },
        )
        self.assertTrue(live["active"])
        self.assertEqual(
            {
                key: live["workflow"]["steps"][0][key]
                for key in ("type", "label")
            },
            {"type": "capture_screen", "label": "Screenshot"},
        )
        self.assertEqual(self.controller.mock_calls, [])

        self.post("/api/recorder/stop", {})
        reviewed = self.review(
            operation="insert",
            index=1,
            step={"type": "capture_screen", "label": "Screenshot"},
        )
        self.assertEqual(
            [
                {key: step[key] for key in ("type", "label")}
                for step in reviewed["workflow"]["steps"]
            ],
            [
                {"type": "capture_screen", "label": "Screenshot"},
                {"type": "capture_screen", "label": "Screenshot"},
            ],
        )

    def test_typing_is_sent_once_and_coalesced_then_converted_after_stop(self):
        self.capture({"type": "input", "source": "fixed", "value": "John "})
        result = self.capture({"type": "input", "source": "fixed", "value": "Tan"})
        self.assertEqual(len(result["workflow"]["steps"]), 1)
        step = result["workflow"]["steps"][0]
        self.assertEqual(step["value"], "John Tan")
        self.assertEqual(self.controller.send_key.call_count, 8)
        self.post(
            "/api/recorder/review", {"operation": "delete", "step_id": step["id"]}, 400
        )
        self.post("/api/recorder/stop", {})
        definition = {
            "name": "customer_name",
            "label": "Customer Name",
            "type": "text",
            "required": True,
            "default": None,
            "ask_when_running": True,
        }
        result = self.review(
            operation="input",
            step_id=step["id"],
            source="variable",
            variable=definition,
        )
        self.assertNotIn("John Tan", json.dumps(result))
        self.assertEqual(
            resolve_variables(result["workflow"], {"customer_name": "Alice"}),
            {"customer_name": "Alice"},
        )
        result = self.review(
            operation="input", step_id=step["id"], source="fixed", value="Alice"
        )
        self.assertEqual(result["workflow"]["variables"], [])
        self.assertEqual(result["workflow"]["steps"][0]["value"], "Alice")
        result = self.review(operation="input", step_id=step["id"], source="manual")
        self.assertNotIn("Alice", json.dumps(result))
        saved = self.post("/api/recorder/save", {}, 201)
        self.assertEqual(saved["steps"][0]["type"], "manual_checkpoint")
        self.assertNotIn("John Tan", self.store.path(saved["id"]).read_text())

    def test_backspace_folds_into_last_input_without_replaying_deleted_characters(self):
        self.capture({"type": "input", "source": "fixed", "value": "Johnx"})
        result = self.capture({"type": "key", "key": "BACKSPACE"})
        self.assertEqual(result["workflow"]["steps"][0]["value"], "John")
        self.assertEqual(len(result["workflow"]["steps"]), 1)
        self.controller.send_key.assert_called_with(DEVICE, "key", "backspace")

    def test_secure_unknown_and_unlabelled_fields_become_manual_without_input_or_values(
        self,
    ):
        for xml in [
            SECURE,
            "<hierarchy/>",
            "bad xml",
            NORMAL.replace("customer_name", "field").replace("Customer Name", "Field"),
        ]:
            self.controller.dump_ui.return_value = xml
            result = self.capture(
                {"type": "input", "source": "fixed", "value": "never-save-this"}
            )
            self.assertNotIn("never-save-this", json.dumps(result))
            self.assertEqual(
                result["workflow"]["steps"][-1]["type"], "manual_checkpoint"
            )
        self.assertEqual(len(result["workflow"]["steps"]), 1)
        self.controller.send_key.assert_not_called()
        self.controller.screenshot.assert_not_called()

    def test_secure_keypad_taps_not_recorded_as_replayable_coordinates(self):
        self.controller.dump_ui.return_value = SECURE
        result = self.capture({"type": "tap", "x": 0.5, "y": 0.8})
        self.assertEqual(result["workflow"]["steps"][0]["type"], "manual_checkpoint")
        self.controller.send.assert_not_called()
        self.controller.screenshot.assert_not_called()

    def test_input_target_change_prevents_coalescing(self):
        self.capture({"type": "input", "source": "fixed", "value": "John"})
        self.controller.dump_ui.return_value = NORMAL.replace(
            "customer_name", "reference"
        )
        result = self.capture({"type": "input", "source": "fixed", "value": "ABC123"})
        self.assertEqual(len(result["workflow"]["steps"]), 2)

    def test_review_rename_delete_reorder_insert_and_ask_default(self):
        first = self.capture({"type": "input", "source": "fixed", "value": "John"})[
            "workflow"
        ]["steps"][0]
        self.capture({"type": "key", "key": "ENTER"})
        self.post("/api/recorder/stop", {})
        self.review(operation="rename_flow", name="Customer Flow")
        self.review(operation="rename_step", step_id=first["id"], label="Customer Name")
        result = self.review(operation="move", step_id=first["id"], index=1)
        self.assertEqual(result["workflow"]["steps"][1]["id"], first["id"])
        self.review(operation="insert", index=0, step={"type": "wait", "seconds": 1})
        result = self.review(
            operation="insert",
            step={"type": "manual_checkpoint", "message": "Review manually."},
        )
        self.assertEqual(result["workflow"]["steps"][-1]["type"], "manual_checkpoint")
        result = self.review(operation="delete", step_id=first["id"])
        self.assertFalse(
            any(s["id"] == first["id"] for s in result["workflow"]["steps"])
        )
        validate_workflow(result["workflow"])

    def test_sensitive_rename_purges_recorded_value(self):
        step = self.capture(
            {"type": "input", "source": "fixed", "value": "hidden-value"}
        )["workflow"]["steps"][0]
        self.post("/api/recorder/stop", {})
        result = self.review(
            operation="rename_step", step_id=step["id"], label="Banking password"
        )
        self.assertNotIn("hidden-value", json.dumps(result))
        self.assertEqual(result["workflow"]["steps"][0]["type"], "manual_checkpoint")

    def test_thumbnail_masks_editable_field_and_skips_secure_screen(self):
        import cv2
        import numpy as np
        import xml.etree.ElementTree as ET

        image = np.full((1920, 1080, 3), 255, dtype=np.uint8)
        _, png = cv2.imencode(".png", image)
        context = SimpleNamespace(input_backend="appium", device=DEVICE, screen=Mock())
        context.screen.capture.return_value = png.tobytes()
        target = {
            "known": True,
            "secure": False,
            "nodes": [
                ET.fromstring(
                    '<node class="android.widget.EditText" bounds="[0,0][1080,1920]"/>'
                )
            ],
        }
        encoded = tap_preview(context, {"type": "tap", "x": 0.5, "y": 0.5}, target)
        crop = cv2.imdecode(
            np.frombuffer(base64.b64decode(encoded), dtype=np.uint8), cv2.IMREAD_COLOR
        )
        self.assertEqual(crop.shape, (96, 96, 3))
        self.assertFalse(crop.any())
        context.screen.capture.reset_mock()
        self.assertIsNone(
            tap_preview(
                context, {"type": "tap", "x": 0.5, "y": 0.5}, {**target, "secure": True}
            )
        )
        context.screen.capture.assert_not_called()

    def test_ask_when_running_false_requires_a_default_for_required_values(self):
        step = self.capture({"type": "input", "source": "fixed", "value": "hello"})[
            "workflow"
        ]["steps"][0]
        self.post("/api/recorder/stop", {})
        variable = {
            "name": "search_text",
            "label": "Search Text",
            "type": "text",
            "required": True,
            "default": None,
            "ask_when_running": False,
        }
        self.post(
            "/api/recorder/review",
            {
                "operation": "input",
                "step_id": step["id"],
                "source": "variable",
                "variable": variable,
            },
            400,
        )
        variable["default"] = "default search"
        result = self.review(
            operation="input", step_id=step["id"], source="variable", variable=variable
        )
        self.assertEqual(
            resolve_variables(result["workflow"]), {"search_text": "default search"}
        )

    def test_hidden_authoring_values_survive_partial_review_updates(self):
        step = self.capture({"type": "input", "source": "fixed", "value": "hello"})[
            "workflow"
        ]["steps"][0]
        self.post("/api/recorder/stop", {})
        result = self.review(
            operation="input",
            step_id=step["id"],
            source="variable",
            variable={
                "name": "search_text",
                "label": "Search Text",
                "type": "text",
                "required": True,
                "default": "saved default",
                "ask_when_running": False,
            },
        )
        result = self.review(
            operation="input",
            step_id=step["id"],
            source="variable",
            variable={
                "name": "renamed_search",
            },
        )
        self.assertEqual(
            result["workflow"]["variables"],
            [
                {
                    "name": "renamed_search",
                    "label": "Search Text",
                    "type": "text",
                    "required": True,
                    "default": "saved default",
                    "ask_when_running": False,
                }
            ],
        )

        result = self.review(
            operation="rename_flow",
            name="Recovery-aware flow",
            financial=True,
            recovery_workflow_id="flow_recovery",
        )
        result = self.review(operation="rename_flow", name="Renamed flow")
        self.assertTrue(result["workflow"]["financial"])
        self.assertEqual(
            result["workflow"]["recovery_workflow_id"], "flow_recovery"
        )
