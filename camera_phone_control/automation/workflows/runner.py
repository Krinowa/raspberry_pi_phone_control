"""Transport-independent execution. Ownership is supplied by automation.Runner."""

import re
import time

from .validation import validate_workflow, resolve_variables
from ..context import Cancelled
from ..input.base import UnsupportedOperation
from ..screen.state import (
    ScreenStateError,
    UnavailableScreenStateProvider,
    wait_for_text,
)
from ..screen.stability import wait_until_stable
from .timing import WorkflowTiming
from .values import read_currency

SCREEN_KEYBOARD_KEY_DELAY = 0.08
AMOUNT_FORMATS = {"raw", "implied_cents", "whole_number"}
_PLAIN_AMOUNT = re.compile(r"(\d+)(?:\.(\d{1,2}))?")


def format_input_value(step, value):
    """Resolve only this input step's physical representation of an amount."""
    if step.get("source") != "variable" or step.get("variable") != "amount":
        return value
    mode = step.get("amount_format", "raw")

    if mode == "raw":
        return value
    if mode not in AMOUNT_FORMATS:
        raise ValueError("Amount input format is invalid.")
    if not isinstance(value, str):
        raise ValueError("Amount input must be a plain nonnegative decimal string.")

    match = _PLAIN_AMOUNT.fullmatch(value)
    if match is None:
        raise ValueError(
            "Amount input must be a plain nonnegative decimal with at most 2 decimal places."
        )

    whole, fraction = match.groups()
    cents = (fraction or "").ljust(2, "0")
    if mode == "whole_number":
        if cents != "00":
            raise ValueError(
                f"This bank workflow accepts whole amounts only; received {value}."
            )
        return whole.lstrip("0") or "0"
    return (whole + cents).lstrip("0") or "0"


def screen_keyboard_points(keyboard, value):
    """Validate a complete value before returning any physical tap targets."""
    if keyboard["kind"] == "alphabetic":
        uppercase = sorted({character for character in value if character.isupper()})
        if uppercase:
            labels = ", ".join(repr(character) for character in uppercase)
            raise UnsupportedOperation(
                "Alphabetic captured keyboard has no Shift/caps support for "
                f"uppercase: {labels}. No input sent."
            )
    missing = sorted(set(value) - set(keyboard["keys"]))
    if missing:
        labels = ", ".join(repr(character) for character in missing)
        raise UnsupportedOperation(
            f"Captured keyboard does not support: {labels}. No input sent."
        )
    return [keyboard["keys"][character] for character in value]


def type_with_screen_keyboard(input_bridge, wait, keyboard, value):
    points = screen_keyboard_points(keyboard, value)
    for index, point in enumerate(points):
        input_bridge.tap(point["x"], point["y"])
        if index + 1 < len(points):
            wait(SCREEN_KEYBOARD_KEY_DELAY)


class WorkflowRunner:
    def __init__(
        self,
        context,
        external_screen=None,
        screen_state_provider=None,
        timing=None,
        capture_screen=None,
        output_callback=None,
    ):
        self.context = context
        self.external_screen = external_screen
        self.screen_state_provider = (
            UnavailableScreenStateProvider()
            if screen_state_provider is None
            else screen_state_provider
        )
        self.timing = timing or WorkflowTiming()
        self.capture_screen = capture_screen
        self.output_callback = output_callback

    def run(self, workflow, variables=None, *, start_step_index=0):
        workflow = validate_workflow(workflow)
        if (
            type(start_step_index) is not int
            or start_step_index < 0
            or start_step_index > len(workflow["steps"])
        ):
            raise ValueError("start_step_index is outside the workflow steps.")
        values = resolve_variables(workflow, variables)
        ctx = self.context
        ctx.state.step_count = len(workflow["steps"])
        ctx.logger.info("Workflow started.")
        self.timing.start()
        try:
            for index, step in enumerate(
                workflow["steps"][start_step_index:], start_step_index + 1
            ):
                ctx.check_cancelled()
                ctx.state.step_index = index
                ctx.state.step_id = step["id"]
                ctx.state.current_step = step["type"]
                ctx.logger.info(
                    "Workflow step %s/%s started: %s",
                    index,
                    len(workflow["steps"]),
                    step["type"],
                )
                self.timing.begin_step(index, step["type"])
                try:
                    following = (
                        workflow["steps"][index]
                        if index < len(workflow["steps"])
                        else None
                    )
                    self._prepare_text_wait_baseline(step, following)
                    self.execute_step(step, values, workflow.get("keyboards", {}))
                    ctx.check_cancelled()
                except Cancelled:
                    self.timing.end_step("cancelled")
                    self._log_step_timing(ctx, index)
                    ctx.logger.info("Workflow step %s cancelled.", index)
                    raise
                except Exception:
                    self.timing.end_step("failed")
                    self._log_step_timing(ctx, index)
                    ctx.logger.info(
                        "Workflow step %s failed; no retry attempted.", index
                    )
                    raise
                self.timing.end_step("completed")
                self._log_step_timing(ctx, index)
                ctx.logger.info("Workflow step %s completed.", index)
        finally:
            values.clear()
            self.timing.finish()
            summary = self.timing.snapshot()["summary"]
            ctx.logger.info(
                "Workflow timing total=%.3fs hid_api=%.3fs fixed_sleep=%.3fs "
                "visual_app_wait=%.3fs camera=%.3fs ocr=%.3fs other=%.3fs "
                "ocr_checks=%d paddle_calls=%d ocr_skipped=%d forced_ocr=%d "
                "change_ocr=%d polls=%d retries=%d",
                summary["total_seconds"],
                summary["hid_api_seconds"],
                summary["fixed_sleep_seconds"],
                summary["visual_app_wait_seconds"],
                summary["camera_frame_seconds"],
                summary["ocr_wall_seconds"],
                summary["other_seconds"],
                summary["ocr_call_count"],
                summary["ocr_inference_count"],
                summary["ocr_skipped_count"],
                summary["ocr_forced_count"],
                summary["ocr_change_count"],
                summary["poll_count"],
                summary["retry_count"],
            )
            ctx.logger.info("Workflow execution finished.")

    def _prepare_text_wait_baseline(self, step, following):
        """Capture a cheap pre-action frame for a following camera text wait."""
        if not following or following["type"] not in {"wait_for_text", "if_text"}:
            return
        physical_action = step["type"] in {"tap", "swipe", "key"} or (
            step["type"] == "input" and step.get("source") != "manual"
        )
        capture = getattr(self.screen_state_provider, "capture_change_baseline", None)
        if physical_action and callable(capture):
            try:
                capture()
            except ScreenStateError as exc:
                # Baseline capture is an optimization. Preserve the prior order:
                # send the action, then let the text wait report camera failure.
                self.context.logger.info(
                    "Text-wait baseline unavailable; continuing: %s", exc
                )

    def _log_step_timing(self, ctx, index):
        step = self.timing.snapshot()["steps"][-1]
        ctx.logger.info(
            "Workflow step timing index=%d name=%s total=%.3fs hid_api=%.3fs "
            "fixed_sleep=%.3fs condition_wait=%.3fs polling_sleep=%.3fs "
            "camera=%.3fs ocr=%.3fs matching=%.3fs polls=%d retries=%d",
            index,
            step["name"],
            step["total_seconds"],
            step["hid_api_seconds"],
            step["fixed_sleep_seconds"],
            step["wait_for_condition_seconds"],
            step["polling_wait_seconds"],
            step["camera_frame_seconds"],
            step["ocr_preprocessing_seconds"]
            + step["ocr_queue_seconds"]
            + step["ocr_ipc_seconds"]
            + step["ocr_postprocessing_seconds"],
            step["ocr_matching_seconds"],
            step["poll_count"],
            step["retry_count"],
        )

    def execute_step(self, step, values=None, keyboards=None):
        ctx = self.context
        ctx.check_cancelled()
        kind = step["type"]

        if kind == "tap":
            ctx.input.tap(step["x"], step["y"])
        elif kind == "swipe":
            ctx.input.swipe(*step["start"], *step["end"], step.get("duration_ms", 300))
        elif kind == "key":
            ctx.input.key(step["key"])
        elif kind == "wait":
            ctx.wait(step["seconds"])
        elif kind == "wait_until_stable":
            ctx.logger.info("Waiting for screen to settle…")
            wait_until_stable(
                self.external_screen,
                ctx,
                step["stable_ms"],
                step["timeout_seconds"],
            )
            ctx.logger.info("Screen stable for %s ms.", step["stable_ms"])
        elif kind == "wait_for_text":
            ctx.logger.info("Waiting for text: %s", step["text"])
            wait_for_text(
                self.screen_state_provider,
                ctx,
                step["text"],
                step["timeout_seconds"],
                roi=step.get("roi"),
            )
            ctx.logger.info("Text detected: %s", step["text"])
        elif kind == "if_text":
            ctx.logger.info("Evaluating text condition: %s", step["text"])
            matched = wait_for_text(
                self.screen_state_provider,
                ctx,
                step["text"],
                step["timeout_seconds"],
                roi=step.get("roi"),
                required=False,
            )
            branch_name = "then_steps" if matched else "else_steps"
            self.timing.annotate_current(
                condition_result=bool(matched),
                selected_branch="then" if matched else "else",
            )
            ctx.logger.info(
                "Text condition result=%s; selected branch=%s.",
                "true" if matched else "false",
                "THEN" if matched else "ELSE",
            )
            self._execute_branch(
                step[branch_name],
                values or {},
                keyboards or {},
                "THEN" if matched else "ELSE",
            )
        elif kind == "ocr_value":
            value = read_currency(self.screen_state_provider, step["roi"], ctx)
            if self.output_callback is not None:
                self.output_callback(step["output"], format(value, ".2f"))
            ctx.logger.info("OCR value extracted.")
        elif kind == "capture_screen":
            if self.capture_screen is None:
                raise ScreenStateError(
                    "Screenshot capture is unavailable for this run."
                )
            self.capture_screen(step, ctx.state.step_index)
            ctx.logger.info("Screenshot saved: %s", step.get("label") or "Screenshot")
        elif kind == "manual_checkpoint":
            ctx.manual_checkpoint(step["message"])
        elif kind == "input":
            if step["source"] == "manual":
                ctx.manual_checkpoint(
                    step.get("message", "Complete this step manually.")
                )
            else:
                value = (
                    step["value"]
                    if step["source"] == "fixed"
                    else values[step["variable"]]
                )
                value = format_input_value(step, value)
                if step.get("input_method", "hid") == "screen_keyboard":
                    keyboard = (keyboards or {})[step["keyboard_id"]]
                    type_with_screen_keyboard(ctx.input, ctx.wait, keyboard, value)
                else:
                    ctx.input.type_text(value)
        else:
            raise ValueError("Unsupported workflow step type.")

    def _execute_branch(self, steps, values, keyboards, branch_name):
        """Run validated nested steps through the normal step executor."""
        for index, step in enumerate(steps, 1):
            started = time.monotonic()
            self.context.check_cancelled()
            self.context.state.current_step = step["type"]
            self.context.logger.info(
                "%s branch step %s/%s started: %s",
                branch_name,
                index,
                len(steps),
                step["type"],
            )
            following = steps[index] if index < len(steps) else None
            self._prepare_text_wait_baseline(step, following)
            try:
                self.execute_step(step, values, keyboards)
                self.context.check_cancelled()
            except Cancelled:
                self.timing.record_nested_step(
                    f"{branch_name}.{index}",
                    step["type"],
                    "cancelled",
                    time.monotonic() - started,
                )
                raise
            except Exception:
                self.timing.record_nested_step(
                    f"{branch_name}.{index}",
                    step["type"],
                    "failed",
                    time.monotonic() - started,
                )
                raise
            self.timing.record_nested_step(
                f"{branch_name}.{index}",
                step["type"],
                "completed",
                time.monotonic() - started,
            )
            self.context.logger.info(
                "%s branch step %s/%s completed: %s",
                branch_name,
                index,
                len(steps),
                step["type"],
            )
