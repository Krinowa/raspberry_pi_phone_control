"""Local asynchronous workflow runs, reusing the script runner and device locks."""

import logging
import threading
import uuid
import time
from collections import deque
from copy import deepcopy
from dataclasses import asdict

from ..backends import backend_names
from diagnostic_logging import diagnostic_context, log_event
from ..runner import Runner
from .runner import WorkflowRunner
from .screenshots import WorkflowScreenshotStore
from .timing import WorkflowTiming
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
        self,
        controller,
        locks,
        external_screen=None,
        screen_state_provider=None,
        *,
        device_id=None,
        screen_resolver=None,
        screenshot_store=None,
    ):
        self.controller, self.locks = controller, locks
        self.external_screen = external_screen
        self.screen_state_provider = screen_state_provider
        self.device_id = device_id
        self.screen_resolver = screen_resolver
        self.screenshot_store = screenshot_store or WorkflowScreenshotStore()
        self.lock = threading.RLock()
        self.runs = {}
        self.completion_listeners = []

    def add_completion_listener(self, listener):
        """Observe immutable completion snapshots without affecting run status."""
        if not callable(listener):
            raise ValueError("Workflow completion listener must be callable.")
        with self.lock:
            if listener not in self.completion_listeners:
                self.completion_listeners.append(listener)

    def remove_completion_listener(self, listener):
        with self.lock:
            if listener in self.completion_listeners:
                self.completion_listeners.remove(listener)

    def _notify_completion(self, snapshot):
        with self.lock:
            listeners = list(self.completion_listeners)
        for listener in listeners:
            try:
                listener(deepcopy(snapshot))
            except Exception:
                logging.getLogger("workflow_service").exception(
                    "Workflow completion listener failed."
                )

    def has_active_run(self, device_id=None):
        with self.lock:
            return any(
                item.get("active", item["thread"].is_alive())
                and (device_id is None or item.get("device_id") == device_id)
                for item in self.runs.values()
            )

    def start(
        self,
        workflow,
        variables=None,
        *,
        device_id=None,
        cancel_event=None,
        start_step_index=0,
        job_id=None,
    ):
        workflow = validate_workflow(workflow)
        variables = deepcopy(variables)
        resolve_variables(workflow, variables)
        backend_names()
        target_device_id = self.device_id if device_id is None else device_id
        with self.lock:
            if self.has_active_run(target_device_id):
                raise ValueError(
                    "A workflow is already running on this device. Stop it or wait for completion."
                )
            # Bounded in-memory history, never variable payloads.
            while len(self.runs) >= 100:
                del self.runs[next(iter(self.runs))]

            run_id = uuid.uuid4().hex
            logger = logging.Logger("workflow." + run_id, logging.INFO)
            handler = RunLog()
            logger.addHandler(handler)
            runner = Runner(self.controller, self.locks, logger)
            cancel = cancel_event or threading.Event()
            resume = threading.Event()
            completed = threading.Event()
            item = {
                "runner": runner,
                "cancel": cancel,
                "resume": resume,
                "workflow_id": workflow["id"],
                "device_id": target_device_id,
                "job_id": job_id,
                "logs": handler,
                "backend": backend_names()[0],
                "steps": self.preview_steps(workflow),
                "timing": WorkflowTiming(),
                "completed": completed,
                "active": True,
                "screenshots": [],
                "outputs": {},
                "diagnostic_failure": None,
            }

            external_screen = self.external_screen
            screen_state_provider = self.screen_state_provider
            if self.screen_resolver is not None and target_device_id is not None:
                external_screen, screen_state_provider = self.screen_resolver(
                    target_device_id
                )

            def capture_screen(step, step_index):
                artifact = self.screenshot_store.capture(
                    external_screen,
                    run_id=run_id,
                    step_id=step.get("id") or f"step_{step_index}",
                    label=step.get("label"),
                )
                with self.lock:
                    item["screenshots"].append(artifact)
                    if step.get("output_key"):
                        item["outputs"][step["output_key"]] = str(
                            (
                                self.screenshot_store.root / artifact["relative_path"]
                            ).resolve()
                        )
                return artifact

            def store_output(name, value):
                with self.lock:
                    item["outputs"][name] = value

            def store_diagnostic_failure(failure):
                with self.lock:
                    if item["diagnostic_failure"] is None:
                        item["diagnostic_failure"] = dict(failure)

            def execute():
                started_at = time.monotonic()
                try:
                    with diagnostic_context(
                        device_id=target_device_id,
                        job_id=job_id,
                        workflow_id=workflow["id"],
                        workflow_run_id=run_id,
                    ):
                        log_event(
                            logging.getLogger("workflow"),
                            logging.INFO,
                            "workflow",
                            "workflow_started",
                            retry_count=1 if start_step_index else 0,
                        )
                        runner.run_task(
                            workflow["id"],
                            lambda ctx: WorkflowRunner(
                                ctx,
                                external_screen,
                                screen_state_provider,
                                timing=item["timing"],
                                capture_screen=capture_screen,
                                output_callback=store_output,
                                diagnostic_failure_callback=store_diagnostic_failure,
                            ).run(
                                workflow,
                                variables,
                                start_step_index=start_step_index,
                            ),
                            cancel_event=cancel,
                            resume_event=resume,
                            device_id=target_device_id,
                        )
                finally:
                    if isinstance(variables, dict):
                        variables.clear()
                    # Physical workflow execution has ended. Mark it inactive
                    # before publishing outputs so downstream device readiness
                    # cannot invalidate a newly observed balance as still busy.
                    with self.lock:
                        item["active"] = False
                    try:
                        snapshot = self.snapshot(run_id)
                        diagnostic_failure = item.get("diagnostic_failure") or {}
                        status = snapshot.get("status")
                        event = {
                            "passed": "workflow_completed",
                            "cancelled": "workflow_cancelled",
                        }.get(status, "workflow_failed")
                        level = (
                            logging.INFO
                            if status in {"passed", "cancelled"}
                            else logging.ERROR
                        )
                        with diagnostic_context(
                            device_id=target_device_id,
                            job_id=job_id,
                            workflow_id=workflow["id"],
                            workflow_run_id=run_id,
                        ):
                            log_event(
                                logging.getLogger("workflow"),
                                level,
                                "workflow",
                                event,
                                step_id=diagnostic_failure.get("step_id")
                                or snapshot.get("step_id"),
                                error_code=diagnostic_failure.get("error_code")
                                or snapshot.get("error_code")
                                or (
                                    "workflow_failed"
                                    if status not in {"passed", "cancelled"}
                                    else None
                                ),
                                exception_type=diagnostic_failure.get("exception_type"),
                                duration_ms=round(
                                    (time.monotonic() - started_at) * 1000
                                ),
                                outcome=status,
                                retry_count=1 if start_step_index else 0,
                            )
                        self._notify_completion(snapshot)
                    finally:
                        completed.set()

            thread = threading.Thread(
                target=execute, name="workflow-" + run_id, daemon=True
            )
            item["thread"] = thread
            self.runs[run_id] = item
            thread.start()
            return self.snapshot(run_id)

    def start_for_job(self, job, workflow, *, cancel_event=None):
        """Worker entry point with a device fixed at job creation."""
        if workflow.get("id") != job.workflow:
            raise ValueError("Job workflow does not match the loaded workflow.")
        options = {"device_id": job.device_id, "job_id": job.job_id}
        options["start_step_index"] = job.start_step_index
        if cancel_event is not None:
            options["cancel_event"] = cancel_event
        return self.start(workflow, job.payload, **options)

    def wait(self, run_id, timeout=None):
        """Wait for a run without tying its lifetime to an HTTP request."""
        with self.lock:
            completed = self.get(run_id)["completed"]
        if not completed.wait(timeout):
            return None
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
                    "roi",
                    "label",
                    "critical",
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
                device_id=item["device_id"],
                input_backend=item["backend"],
                logs=logs,
                timing=item["timing"].snapshot(),
                screenshots=deepcopy(item["screenshots"]),
                outputs=deepcopy(item["outputs"]),
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
