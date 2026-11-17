import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from automation.context import Cancelled
from automation.screen.state import (
    ScreenStateError,
    TextDetectionUnavailable,
    UnavailableScreenStateProvider,
    wait_for_text,
)
from automation.workflows.models import new_workflow
from automation.workflows.runner import WorkflowRunner
from automation.workflows.validation import validate_workflow


class Clock:
    def __init__(self):
        self.now = 0.0
        self.cancel_event = SimpleNamespace(wait=self.advance)
        self.check_cancelled = Mock()

    def advance(self, seconds):
        self.now += seconds


class Provider:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def contains_text(self, text):
        self.calls.append(text)
        return next(self.results, False)


class OptimizedProvider:
    forced_ocr_interval = 0.4
    poll_interval = 0.1

    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def poll_contains_text(self, text, *, roi, force):
        self.calls.append((text, roi, force))
        return next(self.results)


def workflow(**fields):
    result = new_workflow("Text wait")
    result["steps"] = [
        {"id": "text", "type": "wait_for_text", "text": "Transfer",
         "timeout_seconds": 10} | fields
    ]
    return result


class ScreenStateTests(unittest.TestCase):
    def test_validation_trims_text_and_rejects_invalid_values(self):
        candidate = workflow(text="  Transfer  ", timeout_seconds=0.5)
        self.assertEqual(validate_workflow(candidate)["steps"][0]["text"], "Transfer")
        for value in ("", "   ", "x" * 501, None):
            with self.subTest(text=value), self.assertRaises(ValueError):
                validate_workflow(workflow(text=value))
        for value in (None, True, "10", 0.49, 301, float("inf")):
            with self.subTest(timeout=value), self.assertRaisesRegex(
                ValueError, "timeout_seconds"
            ):
                validate_workflow(workflow(timeout_seconds=value))

    def test_validation_accepts_normalized_roi_and_rejects_invalid_roi(self):
        valid = validate_workflow(workflow(roi=[0.1, 0.2, 0.8, 0.3]))
        self.assertEqual(valid["steps"][0]["roi"], [0.1, 0.2, 0.8, 0.3])
        invalid = (
            None,
            [0, 0, 1],
            [0, 0, 0, 1],
            [0.8, 0, 0.3, 1],
            [0, -0.1, 1, 1],
            [0, 0, "1", 1],
        )
        for roi in invalid:
            with self.subTest(roi=roi), self.assertRaisesRegex(ValueError, "ROI"):
                validate_workflow(workflow(roi=roi))

    def test_editor_roi_object_round_trip_and_disable(self):
        from automation.workflows.recorder import Recorder
        roi = dict(x=.1, y=.2, width=.8, height=.3)
        validated = validate_workflow(workflow(roi=roi))
        self.assertEqual(validated["steps"][0]["roi"], roi)
        recorder = Recorder()
        recorder.edit("owner", validated)
        step_id = validated["steps"][0]["id"]
        recorder.review("owner", "fields", dict(step_id=step_id, fields=dict(roi=None)))
        self.assertNotIn("roi", recorder.workflow["steps"][0])

    def test_roi_object_validation_and_roundoff(self):
        from automation.coordinates import normalized_roi
        self.assertEqual(normalized_roi(dict(x=.1, y=.2, width=.8, height=.3)), [.1, .2, .8, .3])
        for roi in ([True, 0, .5, 1], [0, 0, float("nan"), 1],
                    [0, 0, float("inf"), 1], dict(x=0, y=0, width=1), [1, 0, 1e-13, 1]):
            with self.subTest(roi=roi), self.assertRaises(ValueError):
                normalized_roi(roi)
        self.assertEqual(normalized_roi([.8, 0, .20000000000000004, 1])[2], 1 - .8)

    def test_text_already_present_returns_immediately(self):
        clock = Clock()
        provider = Provider([True])
        wait_for_text(provider, clock, "Transfer", 10, clock=lambda: clock.now)
        self.assertEqual(clock.now, 0)
        self.assertEqual(provider.calls, ["Transfer"])

    def test_text_appears_after_several_polls(self):
        clock = Clock()
        provider = Provider([False, False, True])
        wait_for_text(provider, clock, "Transfer", 10, clock=lambda: clock.now)
        self.assertAlmostEqual(clock.now, 0.4)
        self.assertEqual(len(provider.calls), 3)

    def test_optimized_provider_skips_ocr_until_change_or_forced_interval(self):
        clock = Clock()
        provider = OptimizedProvider(
            [(False, False)] * 4 + [(True, False), (True, True)]
        )
        roi = [0.1, 0.2, 0.8, 0.3]
        wait_for_text(
            provider, clock, "Transfer", 3, roi=roi, clock=lambda: clock.now
        )
        self.assertEqual(len(provider.calls), 6)
        self.assertTrue(provider.calls[4][2])
        self.assertAlmostEqual(clock.now, 0.5)
        self.assertEqual({tuple(call[1]) for call in provider.calls}, {tuple(roi)})

    def test_timeout(self):
        clock = Clock()
        with self.assertRaisesRegex(
            ScreenStateError,
            'Text "Transfer" was not detected within 0.5 seconds',
        ):
            wait_for_text(
                Provider([False]), clock, "Transfer", 0.5,
                clock=lambda: clock.now,
            )

    def test_cancellation_interrupts_polling(self):
        clock = Clock()
        clock.check_cancelled.side_effect = [None, Cancelled()]
        with self.assertRaises(Cancelled):
            wait_for_text(
                Provider([False]), clock, "Transfer", 10,
                clock=lambda: clock.now,
            )
        self.assertAlmostEqual(clock.now, 0.2)

    def test_unavailable_provider_fails_clearly(self):
        clock = Clock()
        with self.assertRaisesRegex(TextDetectionUnavailable, "not available"):
            wait_for_text(
                UnavailableScreenStateProvider(), clock, "Transfer", 10,
                clock=lambda: clock.now,
            )

    def test_runner_captures_baseline_before_action_followed_by_text_wait(self):
        events = []
        provider = Provider([True])
        provider.capture_change_baseline = lambda: events.append("baseline")
        context = Clock()
        context.state = SimpleNamespace()
        context.logger = Mock()
        context.input = Mock()
        context.input.tap.side_effect = lambda *_: events.append("tap")
        candidate = new_workflow("Baseline")
        candidate["steps"] = [
            {"id": "tap", "type": "tap", "x": 0.5, "y": 0.5},
            {"id": "text", "type": "wait_for_text", "text": "Transfer",
             "timeout_seconds": 10},
        ]
        WorkflowRunner(context, screen_state_provider=provider).run(candidate)
        self.assertEqual(events, ["baseline", "tap"])


if __name__ == "__main__":
    unittest.main()
