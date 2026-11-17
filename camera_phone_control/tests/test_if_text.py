"""Focused validation, execution, and review tests for text conditions."""

import base64
import logging
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from automation.context import Cancelled
from automation.screen.state import ScreenStateError
from automation.workflows.models import new_workflow
from automation.workflows.recorder import Recorder
from automation.workflows.runner import WorkflowRunner
from automation.workflows.validation import (
    validate_recovery_workflow,
    validate_workflow,
)


def workflow(*steps):
    result = new_workflow("Conditional")
    result["steps"] = list(steps)
    return result


def condition(then_steps=None, else_steps=None, **changes):
    result = {
        "id": "condition",
        "type": "if_text",
        "text": "Optional screen",
        "timeout_seconds": 0.1,
        "then_steps": then_steps or [],
        "else_steps": else_steps or [],
    }
    result.update(changes)
    return result


class Provider:
    poll_interval = 0.001

    def __init__(self, results, cancel=None):
        self.results = iter(results)
        self.last = False
        self.cancel = cancel
        self.rois = []

    def contains_text(self, _text):
        self.rois.append(None)
        if self.cancel is not None:
            self.cancel.set()
        try:
            self.last = next(self.results)
        except StopIteration:
            pass
        return self.last


class Context:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.state = SimpleNamespace()
        self.logger = logging.getLogger("if-text-test")
        self.input = Mock()

    def check_cancelled(self):
        if self.cancel_event.is_set():
            raise Cancelled("cancelled")

    def wait(self, seconds):
        self.cancel_event.wait(seconds)
        self.check_cancelled()

    def manual_checkpoint(self, _message):
        self.check_cancelled()


class IfTextTests(unittest.TestCase):
    def run_flow(self, provider, *steps):
        context = Context()
        runner = WorkflowRunner(context, screen_state_provider=provider)
        runner.run(workflow(*steps))
        return context, runner

    def test_true_and_false_select_one_branch_then_continue_parent(self):
        for found, expected_x in ((True, 0.1), (False, 0.2)):
            with self.subTest(found=found):
                step = condition(
                    [{"id": "then", "type": "tap", "x": 0.1, "y": 0.1}],
                    [{"id": "else", "type": "tap", "x": 0.2, "y": 0.2}],
                )
                context, runner = self.run_flow(
                    Provider([found]),
                    step,
                    {"id": "after", "type": "tap", "x": 0.9, "y": 0.9},
                )
                self.assertEqual(
                    [call.args for call in context.input.tap.call_args_list],
                    [(expected_x, expected_x), (0.9, 0.9)],
                )
                timing = runner.timing.snapshot()["steps"][0]
                self.assertEqual(timing["condition_result"], found)
                self.assertEqual(
                    timing["selected_branch"], "then" if found else "else"
                )
                self.assertEqual(timing["nested_steps"][0]["status"], "completed")

    def test_timeout_is_false_and_empty_else_succeeds(self):
        context, _ = self.run_flow(
            Provider([False]),
            condition([{"id": "then", "type": "tap", "x": 0, "y": 0}]),
            {"id": "after", "type": "tap", "x": 1, "y": 1},
        )
        context.input.tap.assert_called_once_with(1, 1)

    def test_nested_condition_runs_normal_actions(self):
        nested = condition(
            [{"id": "wait", "type": "wait", "seconds": 0}],
            [{"id": "nested-tap", "type": "tap", "x": 0.3, "y": 0.4}],
            id="nested",
            text="Nested",
        )
        context, _ = self.run_flow(Provider([True, False]), condition([nested]))
        context.input.tap.assert_called_once_with(0.3, 0.4)

    def test_condition_and_branch_cancellation_propagate(self):
        context = Context()
        provider = Provider([False], context.cancel_event)
        with self.assertRaises(Cancelled):
            WorkflowRunner(context, screen_state_provider=provider).run(
                workflow(condition())
            )

        for found, branch in ((True, "then_steps"), (False, "else_steps")):
            with self.subTest(branch=branch):
                context = Context()
                context.input.tap.side_effect = Cancelled("cancelled")
                branches = {
                    "then_steps": [],
                    "else_steps": [],
                    branch: [{"id": branch, "type": "tap", "x": 0, "y": 0}],
                }
                with self.assertRaises(Cancelled):
                    WorkflowRunner(
                        context, screen_state_provider=Provider([found])
                    ).run(workflow(condition(**branches)))

    def test_validation_rejects_bad_fields_and_depth(self):
        invalid = [
            condition(text=" "),
            condition(timeout_seconds=0.01),
            condition(then_steps="bad"),
            condition(else_steps="bad"),
            condition(roi={"x": 0.9, "y": 0, "width": 0.2, "height": 1}),
        ]
        for step in invalid:
            with self.subTest(step=step):
                with self.assertRaises(ValueError):
                    validate_workflow(workflow(step))

        nested = {"id": "leaf", "type": "wait", "seconds": 0}
        for depth in range(6):
            nested = condition([nested], id=f"condition_{depth}")
        with self.assertRaisesRegex(ValueError, "5 levels"):
            validate_workflow(workflow(nested))

    def test_roi_legacy_and_object_formats_validate(self):
        for roi, step in (
            (None, condition()),
            ([0.1, 0.2, 0.3, 0.4], condition(roi=[0.1, 0.2, 0.3, 0.4])),
            (
                {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
                condition(roi={"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}),
            ),
        ):
            validated = validate_workflow(workflow(step))
            if roi is None:
                self.assertNotIn("roi", validated["steps"][0])
            else:
                self.assertEqual(validated["steps"][0]["roi"], roi)

    def test_existing_wait_for_text_still_fails_on_timeout(self):
        wait = {
            "id": "required",
            "type": "wait_for_text",
            "text": "Required",
            "timeout_seconds": 0.5,
        }
        with self.assertRaises(ScreenStateError):
            self.run_flow(Provider([False]), wait)

    def test_recovery_restrictions_are_recursive(self):
        recovery = workflow(
            condition(
                [{
                    "id": "unsafe",
                    "type": "manual_checkpoint",
                    "message": "Manual",
                }]
            ),
            {
                "id": "ready",
                "type": "wait_for_text",
                "text": "Ready",
                "timeout_seconds": 1,
            },
        )
        with self.assertRaisesRegex(ValueError, "manual checkpoint"):
            validate_recovery_workflow(recovery)

    def test_nested_recording_screenshot_and_recorder_branch_edits(self):
        screenshot = {
            "image": base64.b64encode(b"\xff\xd8\xfftest").decode(),
            "width": 10,
            "height": 20,
            "format": "jpeg",
        }
        validate_workflow(
            workflow(
                condition(
                    [{
                        "id": "tap",
                        "type": "tap",
                        "x": 0.5,
                        "y": 0.5,
                        "recording_screenshot": screenshot,
                    }]
                )
            )
        )

        recorder = Recorder()
        recorder.start("owner", "Conditional")
        recorder.stop("owner")
        result = recorder.review(
            "owner",
            "insert",
            {"step": condition()},
        )
        condition_id = result["workflow"]["steps"][0]["id"]
        result = recorder.review(
            "owner",
            "insert",
            {
                "parent_step_id": condition_id,
                "branch": "then_steps",
                "index": 0,
                "step": {"type": "wait", "seconds": 1},
            },
        )
        nested_id = result["workflow"]["steps"][0]["then_steps"][0]["id"]
        result = recorder.review(
            "owner",
            "move",
            {"step_id": nested_id, "index": 0},
        )
        self.assertEqual(
            result["workflow"]["steps"][0]["then_steps"][0]["id"], nested_id
        )

    def test_live_recorder_routes_steps_to_alternate_screen_and_returns(self):
        recorder = Recorder()
        recorder.start("owner", "Alternate screens")
        top = recorder.prepare("owner", {"type": "tap", "x": 0.1, "y": 0.1})
        recorder.commit_recording("owner", top)

        started = recorder.start_alternate_screen(
            "owner",
            "Extra verification",
            0.8,
            {"x": 0.1, "y": 0.2, "width": 0.7, "height": 0.3},
        )
        condition_id = started["alternate_screen"]["condition_id"]
        with self.assertRaisesRegex(ValueError, "Finish the current"):
            recorder.start_alternate_screen("owner", "Another", 0.8)

        screenshot = {
            "image": base64.b64encode(b"\xff\xd8\xffnested").decode(),
            "width": 10,
            "height": 20,
            "format": "jpeg",
        }
        steps = [
            {
                "type": "tap",
                "x": 0.5,
                "y": 0.6,
                "recording_screenshot": screenshot,
            },
            {
                "type": "swipe",
                "start": [0.5, 0.8],
                "end": [0.5, 0.2],
                "duration_ms": 300,
            },
            {"type": "key", "key": "BACK"},
            {"type": "input", "source": "fixed", "value": "hello"},
            {"type": "wait", "seconds": 0},
        ]
        for index, step in enumerate(steps):
            candidate = (
                recorder.prepare_capture("owner", step)
                if index == 0
                else recorder.prepare("owner", step)
            )
            recorder.commit_recording("owner", candidate)

        condition_step = next(
            step
            for step in recorder.workflow["steps"]
            if step["id"] == condition_id
        )
        self.assertEqual(
            [step["type"] for step in condition_step["then_steps"]],
            ["tap", "swipe", "key", "input", "wait"],
        )
        self.assertEqual(condition_step["else_steps"], [])
        self.assertEqual(
            condition_step["then_steps"][0]["recording_screenshot"], screenshot
        )
        self.assertIn("recorded_at", condition_step["then_steps"][0])
        self.assertEqual(len(recorder.workflow["steps"]), 2)

        recorder.undo("owner")
        self.assertEqual(
            [step["type"] for step in condition_step["then_steps"]],
            ["tap", "swipe", "key", "input"],
        )
        recorder.finish_alternate_screen("owner")
        next_step = recorder.prepare("owner", {"type": "wait", "seconds": 1})
        recorder.commit_recording("owner", next_step)
        self.assertEqual(recorder.workflow["steps"][-1]["type"], "wait")

    def test_cancel_and_multiple_sequential_alternate_screens(self):
        recorder = Recorder()
        recorder.start("owner", "Multiple screens")
        first = recorder.start_alternate_screen("owner", "Notice", 0.8)
        first_id = first["alternate_screen"]["condition_id"]
        candidate = recorder.prepare("owner", {"type": "key", "key": "BACK"})
        recorder.commit_recording("owner", candidate)
        recorder.finish_alternate_screen("owner")

        second = recorder.start_alternate_screen("owner", "Verification", 0.8)
        second_id = second["alternate_screen"]["condition_id"]
        recorder.cancel_alternate_screen("owner")
        ids = [step["id"] for step in recorder.workflow["steps"]]
        self.assertIn(first_id, ids)
        self.assertNotIn(second_id, ids)
        self.assertIsNone(recorder.snapshot()["alternate_screen"])

        recorder.start_alternate_screen("owner", "Final notice", 0.8)
        stopped = recorder.stop("owner")
        self.assertFalse(stopped["active"])
        self.assertIsNone(stopped["alternate_screen"])
        self.assertEqual(len(stopped["workflow"]["steps"]), 2)

    def test_review_special_case_returns_to_review_and_preserves_routing(self):
        recorder = Recorder()
        recorder.start("owner", "Review cases")
        for x in (0.1, 0.9):
            candidate = recorder.prepare(
                "owner", {"type": "tap", "x": x, "y": 0.5}
            )
            recorder.commit_recording("owner", candidate)
        recorder.stop("owner")
        original_ids = [step["id"] for step in recorder.workflow["steps"]]

        started = recorder.start_alternate_screen(
            "owner", "Increase limit", 0.8, after_step_id=original_ids[0]
        )
        condition_id = started["alternate_screen"]["condition_id"]
        self.assertTrue(started["active"])
        self.assertEqual(started["alternate_screen"]["return_mode"], "review")
        self.assertEqual(
            [step["id"] for step in started["workflow"]["steps"]],
            [original_ids[0], condition_id, original_ids[1]],
        )

        screenshot = {
            "image": base64.b64encode(b"\xff\xd8\xffreview").decode(),
            "width": 10,
            "height": 20,
            "format": "jpeg",
        }
        candidate = recorder.prepare_capture(
            "owner",
            {
                "type": "tap",
                "x": 0.5,
                "y": 0.5,
                "recording_screenshot": screenshot,
            },
        )
        recorder.commit_recording("owner", candidate)
        candidate = recorder.prepare(
            "owner",
            {
                "type": "swipe",
                "start": [0.2, 0.8],
                "end": [0.8, 0.8],
                "duration_ms": 300,
            },
        )
        recorder.commit_recording("owner", candidate)
        candidate = recorder.prepare("owner", {"type": "key", "key": "BACK"})
        recorder.commit_recording("owner", candidate)
        candidate = recorder.prepare(
            "owner", {"type": "input", "source": "fixed", "value": "hello"}
        )
        recorder.commit_recording("owner", candidate)
        recorder.undo("owner")

        refreshed = recorder.snapshot()
        self.assertEqual(refreshed["alternate_screen"]["return_mode"], "review")
        condition_step = refreshed["workflow"]["steps"][1]
        self.assertEqual(
            [step["type"] for step in condition_step["then_steps"]],
            ["tap", "swipe", "key"],
        )
        self.assertEqual(
            condition_step["then_steps"][0]["recording_screenshot"], screenshot
        )

        finished = recorder.finish_alternate_screen("owner")
        self.assertFalse(finished["active"])
        with self.assertRaisesRegex(ValueError, "Recording is stopped"):
            recorder.prepare("owner", {"type": "wait", "seconds": 1})

        nested_id = condition_step["then_steps"][0]["id"]
        with self.assertRaisesRegex(ValueError, "current Special Case"):
            recorder.start_alternate_screen(
                "owner", "Nested", 0.8, after_step_id=nested_id
            )

        second = recorder.start_alternate_screen(
            "owner", "Temporary warning", 0.8, after_step_id=original_ids[1]
        )
        second_id = second["alternate_screen"]["condition_id"]
        candidate = recorder.prepare("owner", {"type": "key", "key": "BACK"})
        recorder.commit_recording("owner", candidate)
        recorder.finish_alternate_screen("owner")

        third = recorder.start_alternate_screen(
            "owner", "Cancel this", 0.8, after_step_id=original_ids[1]
        )
        third_id = third["alternate_screen"]["condition_id"]
        candidate = recorder.prepare("owner", {"type": "key", "key": "HOME"})
        recorder.commit_recording("owner", candidate)
        cancelled = recorder.cancel_alternate_screen("owner")
        self.assertFalse(cancelled["active"])
        remaining_ids = [step["id"] for step in cancelled["workflow"]["steps"]]
        self.assertNotIn(third_id, remaining_ids)
        self.assertEqual(
            remaining_ids,
            [original_ids[0], condition_id, original_ids[1], second_id],
        )


if __name__ == "__main__":
    unittest.main()
