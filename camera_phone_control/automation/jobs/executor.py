"""Background execution for the persistent per-device job queues."""

from __future__ import annotations


import logging

import threading

import time

from copy import deepcopy


from automation.workflows.validation import validate_recovery_workflow


from .models import PENDING_JOB_STATES

from .storage import RecordConflict, RecordNotFound

LOG = logging.getLogger("job_executor")


class BackgroundJobExecutor:
    """Claim and execute at most one persisted job per logical device.



    One coordinator chooses devices with pending work. Each claimed device has

    one execution thread, so independent devices can progress concurrently.

    The scheduler performs the authoritative claim while holding its queue

    lock; this class's lock only protects live run handles used for cancellation.

    """

    def __init__(
        self,
        scheduler,
        workflow_storage,
        workflow_service,
        *,
        poll_interval=0.25,
    ):

        self.scheduler = scheduler

        self.workflow_storage = workflow_storage

        self.workflow_service = workflow_service

        self.poll_interval = poll_interval

        self.lock = threading.RLock()

        self.active = {}

        self.recoveries = {}

        self.stop_event = threading.Event()

        self.wake_event = threading.Event()

        self.thread = None

    def start(self):

        with self.lock:

            if self.thread is not None and self.thread.is_alive():

                return self

            self.stop_event.clear()

            self.thread = threading.Thread(
                target=self._run,
                name="job-scheduler",
                daemon=True,
            )

            self.thread.start()

        return self

    def wake(self):

        self.wake_event.set()

    def _run(self):

        while not self.stop_event.is_set():

            self.wake_event.wait(self.poll_interval)

            self.wake_event.clear()

            if self.stop_event.is_set():

                break

            try:

                self._dispatch()

            except Exception:

                LOG.exception("Persistent job dispatch failed; it will be retried.")

    def _dispatch(self):

        pending = self.scheduler.store.list()

        device_ids = []

        for job in pending:

            if job.status in PENDING_JOB_STATES and job.device_id not in device_ids:

                device_ids.append(job.device_id)

        for device_id in device_ids:

            with self.lock:

                if device_id in self.active or device_id in self.recoveries:

                    continue

            claimed = self.scheduler.claim_next_job_for_device(device_id)

            if claimed is None:

                continue

            cancel_event = threading.Event()

            execution = {
                "job_id": claimed.job_id,
                "run_id": None,
                "cancel": cancel_event,
                "thread": None,
            }

            thread = threading.Thread(
                target=self._execute,
                args=(claimed, execution),
                name=f"job-{claimed.device_id}-{claimed.job_id}",
                daemon=True,
            )

            execution["thread"] = thread

            with self.lock:

                self.active[claimed.device_id] = execution

            thread.start()

    def _execute(self, job, execution):

        try:

            # Cancellation can land after the durable claim and before this

            # thread is published/running. Carry that persisted request into

            # Runner before it can select a device or send physical input.

            if self.scheduler.get_job(job.job_id).cancel_requested:

                execution["cancel"].set()

            workflow = self.workflow_storage.read(job.workflow)

            started = self.workflow_service.start_for_job(
                job,
                workflow,
                cancel_event=execution["cancel"],
            )

            with self.lock:

                execution["run_id"] = started["run_id"]

            snapshot = self.workflow_service.wait(started["run_id"])

            if snapshot is None:

                raise RuntimeError("Workflow completion could not be observed.")

            status = snapshot.get("status")

            screenshots = snapshot.get("screenshots", [])

            if status == "passed":

                self.scheduler.complete_job(
                    job.job_id,
                    "Workflow completed.",
                    screenshots=screenshots,
                    outputs=snapshot.get("outputs", {}),
                )

            elif status == "cancelled":

                self.scheduler.finish_cancelled(
                    job.job_id, "Workflow cancelled.", screenshots=screenshots
                )

            else:

                message = snapshot.get("message")

                if not isinstance(message, str) or not message:

                    message = "Workflow failed; physical actions were not retried."

                failed_step_index = (
                    snapshot["step_index"] - 1
                    if type(snapshot.get("step_index")) is int
                    and snapshot["step_index"] > 0
                    else None
                )

                failed_step_id = snapshot.get("step_id")

                failed_step_type = None

                steps = workflow.get("steps", []) if isinstance(workflow, dict) else []

                if (
                    failed_step_index is not None
                    and 0 <= failed_step_index < len(steps)
                    and isinstance(steps[failed_step_index], dict)
                ):

                    failed_step = steps[failed_step_index]

                    if (
                        failed_step_id is None
                        or failed_step.get("id") == failed_step_id
                    ):

                        step_type = failed_step.get("type")

                        if isinstance(step_type, str) and step_type:

                            failed_step_type = step_type

                self.scheduler.fail_job(
                    job.job_id,
                    message[:500],
                    screenshots=screenshots,
                    failed_step_index=failed_step_index,
                    failed_step_id=failed_step_id,
                    failed_step_type=failed_step_type,
                )

        except (RecordConflict, RecordNotFound):

            LOG.exception(
                "Persistent job state changed unexpectedly for %s", job.job_id
            )

        except Exception:

            LOG.exception("Background workflow failed to start for %s", job.job_id)

            try:

                self.scheduler.fail_job(
                    job.job_id,
                    "Workflow could not be executed; physical actions were not retried.",
                )

            except (RecordConflict, RecordNotFound):

                pass

        finally:

            with self.lock:

                current = self.active.get(job.device_id)

                if current is execution:

                    del self.active[job.device_id]

            self.wake()

    def cancel(self, job_id):

        job = self.scheduler.cancel_job(job_id)

        if job.status != "RUNNING":

            self.wake()

            return job

        execution = None

        with self.lock:

            candidate = self.active.get(job.device_id)

            if candidate is not None and candidate["job_id"] == job_id:

                execution = candidate

                execution["cancel"].set()

                run_id = execution["run_id"]

            else:

                run_id = None

        if run_id is not None:

            self.workflow_service.cancel(run_id)

        self.wake()

        return self.scheduler.store.get(job_id)

    def _recovery_workflow(self, job, *, verify_only=False):

        recovery_workflow_id = job.recovery_workflow_id

        if recovery_workflow_id is None:

            source = self.workflow_storage.read(job.workflow)

            recovery_workflow_id = source.get("recovery_workflow_id")

        if recovery_workflow_id is None:

            raise RecordConflict("No recovery workflow is configured for this job.")

        try:

            workflow = self.workflow_storage.read(recovery_workflow_id)

        except (FileNotFoundError, KeyError):

            raise RecordConflict(
                "The configured recovery workflow was not found."
            ) from None

        try:

            workflow = validate_recovery_workflow(workflow)

        except ValueError as exc:

            raise RecordConflict(str(exc)) from None

        if verify_only:

            workflow = deepcopy(workflow)

            workflow["name"] += " — ready-state verification"

            workflow["steps"] = [workflow["steps"][-1]]

        return recovery_workflow_id, workflow

    def recover(self, job_id, *, verify_only=False):

        job = self.scheduler.get_job(job_id)

        recovery_workflow_id, workflow = self._recovery_workflow(
            job, verify_only=verify_only
        )

        with self.lock:

            if job.device_id in self.active or job.device_id in self.recoveries:

                raise RecordConflict("Device already has running work.")

            recovering = self.scheduler.begin_recovery(job_id, recovery_workflow_id)

            execution = {
                "job_id": job_id,
                "run_id": None,
                "cancel": threading.Event(),
                "thread": None,
            }

            thread = threading.Thread(
                target=self._execute_recovery,
                args=(recovering, workflow, execution),
                name=f"recovery-{recovering.device_id}-{recovering.job_id}",
                daemon=True,
            )

            execution["thread"] = thread

            self.recoveries[recovering.device_id] = execution

            thread.start()

        return recovering

    def _execute_recovery(self, job, workflow, execution):

        try:

            started = self.workflow_service.start(
                workflow,
                {},
                device_id=job.device_id,
                cancel_event=execution["cancel"],
            )

            with self.lock:

                execution["run_id"] = started["run_id"]

            self.scheduler.attach_recovery_run(job.job_id, started["run_id"])

            snapshot = self.workflow_service.wait(started["run_id"])

            if snapshot is None:

                raise RuntimeError("Recovery completion could not be observed.")

            if snapshot.get("status") == "passed":

                self.scheduler.finish_recovery(
                    job.job_id,
                    successful=True,
                    screenshots=snapshot.get("screenshots", []),
                )

            else:

                message = snapshot.get("message")

                if not isinstance(message, str) or not message:

                    message = "Automatic recovery failed. Manual assistance required."

                self.scheduler.finish_recovery(
                    job.job_id,
                    successful=False,
                    error=message[:500],
                    screenshots=snapshot.get("screenshots", []),
                )

        except (RecordConflict, RecordNotFound):

            LOG.exception("Recovery state changed unexpectedly for %s", job.job_id)

        except Exception:

            LOG.exception("Recovery workflow failed for %s", job.job_id)

            try:

                self.scheduler.finish_recovery(
                    job.job_id,
                    successful=False,
                    error="Automatic recovery failed. Manual assistance required.",
                )

            except (RecordConflict, RecordNotFound):

                pass

        finally:

            with self.lock:

                current = self.recoveries.get(job.device_id)

                if current is execution:

                    del self.recoveries[job.device_id]

            self.wake()

    def close(self, timeout=2.0):
        """Stop dispatching and safely cancel live workflows before shutdown."""

        self.stop_event.set()

        self.wake()

        with self.lock:

            active = list(self.active.values())

            recoveries = list(self.recoveries.values())

        for execution in active:

            try:

                self.cancel(execution["job_id"])

            except (RecordConflict, RecordNotFound, KeyError):

                pass

        for execution in recoveries:

            execution["cancel"].set()

            if execution["run_id"] is not None:

                try:

                    self.workflow_service.cancel(execution["run_id"])

                except KeyError:

                    pass

        deadline = time.monotonic() + max(0.0, timeout)

        for execution in active + recoveries:

            remaining = max(0.0, deadline - time.monotonic())

            execution["thread"].join(remaining)

        if self.thread is not None:

            self.thread.join(max(0.0, deadline - time.monotonic()))

    def wait_until_idle(self, timeout=2.0):
        """Test/maintenance helper; normal callers consume persisted status."""

        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:

            with self.lock:

                active = bool(self.active or self.recoveries)

            pending = any(
                job.status in PENDING_JOB_STATES for job in self.scheduler.store.list()
            )

            if not active and not pending:

                return True

            time.sleep(0.01)

        return False
