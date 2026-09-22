"""Transport-independent execution. Ownership is supplied by automation.Runner."""

from .validation import validate_workflow, resolve_variables
from ..context import Cancelled
from ..screen.state import UnavailableScreenStateProvider, wait_for_text
from ..screen.stability import wait_until_stable


class WorkflowRunner:
    def __init__(self, context, external_screen=None, screen_state_provider=None):
        self.context = context
        self.external_screen = external_screen
        self.screen_state_provider = (
            UnavailableScreenStateProvider()
            if screen_state_provider is None
            else screen_state_provider
        )

    def run(self, workflow, variables=None):
        workflow = validate_workflow(workflow)
        values = resolve_variables(workflow, variables)
        ctx = self.context
        ctx.state.step_count = len(workflow["steps"])
        ctx.logger.info("Workflow started.")
        try:
            for index, step in enumerate(workflow["steps"], 1):
                ctx.check_cancelled()
                ctx.state.step_index = index
                ctx.state.current_step = step["type"]
                ctx.logger.info(
                    "Workflow step %s/%s started: %s",
                    index,
                    len(workflow["steps"]),
                    step["type"],
                )
                try:
                    self.execute_step(step, values)
                    ctx.check_cancelled()
                except Cancelled:
                    ctx.logger.info("Workflow step %s cancelled.", index)
                    raise
                except Exception:
                    ctx.logger.info(
                        "Workflow step %s failed; no retry attempted.", index
                    )
                    raise
                ctx.logger.info("Workflow step %s completed.", index)
        finally:
            values.clear()
            ctx.logger.info("Workflow execution finished.")

    def execute_step(self, step, values=None):
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
            )
            ctx.logger.info("Text detected: %s", step["text"])
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
                ctx.input.type_text(value)
        else:
            raise ValueError("Unsupported workflow step type.")
