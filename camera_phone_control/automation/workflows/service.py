"""Local asynchronous workflow runs, reusing the script runner and device locks."""

import logging
import threading
import uuid
from collections import deque
from copy import deepcopy
from dataclasses import asdict

from ..backends import backend_names
from ..runner import Runner
from .runner import WorkflowRunner
from .validation import validate_workflow, resolve_variables


class RunLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.entries = deque(maxlen=500)

    def emit(self, record):
        # Only fixed lifecycle labels and redacted mock commands are emitted.
        self.entries.append(record.getMessage())


class WorkflowService:
    def __init__(
        self, controller, locks, external_screen=None, screen_state_provider=None
    ):
        self.controller, self.locks = controller, locks
        self.external_screen = external_screen
        self.screen_state_provider = screen_state_provider
        self.lock = threading.RLock()
        self.runs = {}

    def start(self, workflow, variables=None):
        workflow = validate_workflow(workflow)
        variables = deepcopy(variables)
        resolve_variables(workflow, variables)
        backend_names()
        with self.lock:
            if any(item["thread"].is_alive() for item in self.runs.values()):
                raise ValueError(
                    "A workflow is already running. Stop it or wait for completion."
                )
            # Bounded in-memory history, never variable payloads.
            while len(self.runs) >= 100:
                del self.runs[next(iter(self.runs))]

            run_id = uuid.uuid4().hex
            logger = logging.Logger("workflow." + run_id, logging.INFO)
            handler = RunLog()
            logger.addHandler(handler)
            runner = Runner(self.controller, self.locks, logger)
            cancel, resume = threading.Event(), threading.Event()
            item = {
                "runner": runner,
                "cancel": cancel,
                "resume": resume,
                "workflow_id": workflow["id"],
                "logs": handler,
                "backend": backend_names()[0],
                "steps": self.preview_steps(workflow),
            }

            def execute():
                try:
                    runner.run_task(
                        workflow["id"],
                        lambda ctx: WorkflowRunner(
                            ctx, self.external_screen, self.screen_state_provider
                        ).run(workflow, variables),
                        cancel_event=cancel,
                        resume_event=resume,
                    )
                finally:
                    if isinstance(variables, dict):
                        variables.clear()

            thread = threading.Thread(
                target=execute, name="workflow-" + run_id, daemon=True
            )
            item["thread"] = thread
            self.runs[run_id] = item
            thread.start()
            return self.snapshot(run_id)

    @staticmethod
    def preview_steps(workflow):
        result = []
        for step in workflow["steps"]:
            safe = {
                k: v
                for k, v in step.items()
                if k
                in {
                    "type",
                    "x",
                    "y",
                    "start",
                    "end",
                    "duration_ms",
                    "key",
                    "seconds",
                    "stable_ms",
                    "timeout_seconds",
                    "text",
                    "label",
                }
            }
            if step["type"] == "input":
                definition = next(
                    (
                        v
                        for v in workflow["variables"]
                        if v["name"] == step.get("variable")
                    ),
                    None,
                )
                safe["label"] = (
                    definition["label"]
                    if definition
                    else step.get("label", "Recorded input")
                )
            result.append(safe)
        return result

    def get(self, run_id):
        if run_id not in self.runs:
            raise KeyError(run_id)
        return self.runs[run_id]

    def snapshot(self, run_id):
        with self.lock:
            item = self.get(run_id)
            state = item["runner"].state
            result = (
                asdict(state)
                if state
                else {
                    "status": "pending",
                    "step_index": 0,
                    "step_count": len(item["steps"]),
                }
            )
            result["status"] = {
                "completed": "passed",
                "takeover_requested": "running",
            }.get(result["status"], result["status"])
            with item["logs"].lock:
                logs = list(item["logs"].entries)
            result.update(
                run_id=run_id,
                workflow_id=item["workflow_id"],
                input_backend=item["backend"],
                logs=logs,
            )
            index = result.get("step_index", 0)
            result["preview"] = deepcopy(item["steps"][index - 1]) if index else None
            return result

    def cancel(self, run_id):
        with self.lock:
            item = self.get(run_id)
            item["runner"].logger.info("Cancel requested.")
            item["cancel"].set()
            return self.snapshot(run_id)

    def resume(self, run_id, step_index=None):
        with self.lock:
            item = self.get(run_id)
            if (
                item["runner"].state is None
                or item["runner"].state.status != "waiting_manual"
            ):
                raise ValueError("Run is not waiting for a manual checkpoint.")
            if step_index is not None and step_index != item["runner"].state.step_index:
                raise ValueError(
                    "The checkpoint changed. Review the current step before resuming."
                )
            item["resume"].set()
            return self.snapshot(run_id)
