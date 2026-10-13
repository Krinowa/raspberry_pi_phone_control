"""Account-based job routing, readiness checks, and per-device reservations."""

from __future__ import annotations


import threading


import time


from copy import deepcopy


from dataclasses import replace


from automation.workflows.models import now


from .models import (
    JOB_TRANSITIONS,
    Job,
    PENDING_JOB_STATES,
    TERMINAL_JOB_STATES,
)


from .storage import RecordConflict


class DeviceEligibility:

    def __init__(self, state, camera_manager, workflow_service):

        self.state = state

        self.camera_manager = camera_manager

        self.workflow_service = workflow_service

        self.catalog_lock = threading.Lock()

        self.catalog_checked_at = 0.0

    def _refresh_pi_catalog(self):

        if self.state.pi_bridge is None:

            return

        current = time.monotonic()

        if current - self.catalog_checked_at < 1.0:

            return

        if not self.catalog_lock.acquire(blocking=False):

            return

        try:

            current = time.monotonic()

            if current - self.catalog_checked_at < 1.0:

                return

            try:

                catalog = self.state.pi_bridge.devices()

            except Exception:

                with self.state.lock:

                    self.state.pi_connection = {
                        "api": "offline",
                        "phone": "unknown",
                        "message": "Pi device status is unavailable.",
                    }

            else:

                with self.state.lock:

                    self.state.update_pi_devices(catalog)

                    self.state.pi_catalog_initialized = True

            self.catalog_checked_at = current

        finally:

            self.catalog_lock.release()

    def inspect(self, device_id, *, reserved=False):

        # The worker cannot depend on an open dashboard to refresh Pi state.

        self._refresh_pi_catalog()

        with self.state.lock:

            if self.state.pi_bridge is not None:

                details = self.state.pi_devices.get(device_id)

                registered = isinstance(details, dict)

                connected = registered and details.get("connected") is True

                runtime_healthy = self.state.pi_connection.get("api") == "online"

                lock_identity = "pi-api:" + self.state.pi_bridge.client.base_url

                if device_id != "phone_A":

                    lock_identity += "#" + device_id

                manual_busy = self.state.pi_bridge.device_id == device_id and (
                    self.state.owner is not None or self.state.pending
                )

            else:

                selected = getattr(self.workflow_service, "device_id", None)

                registered = device_id in {selected, "phone_A"}

                connected = registered and self.state.device is not None

                runtime_healthy = connected

                lock_identity = (
                    self.state.device.serial if self.state.device is not None else None
                )

                manual_busy = self.state.owner is not None or self.state.pending

        lock_busy = (
            lock_identity is not None
            and self.state.device_locks.owner(lock_identity) is not None
        )

        # Opening the assigned camera is an eligibility check, not discovery;

        # CameraManager still centrally owns and reuses the capture.

        camera = self.camera_manager.status(device_id, open_camera=True)

        configured = bool(
            camera["config"]["enabled"]
            and camera["config"]["camera_source"] is not None
        )

        available = camera["status"].get("ready") is True

        workflow_busy = self.workflow_service.has_active_run(device_id)

        reasons = []

        if not registered:

            reasons.append("not_registered")

        if not connected:

            reasons.append("not_connected")

        if not configured:

            reasons.append("camera_not_configured")

        elif not available:

            reasons.append("camera_not_available")

        if not runtime_healthy:

            reasons.append("runtime_unhealthy")

        if workflow_busy:

            reasons.append("workflow_busy")

        if manual_busy or lock_busy:

            reasons.append("device_owned")

        if reserved:

            reasons.append("job_reserved")

        return {
            "device_id": device_id,
            "registered": registered,
            "connected": connected,
            "camera_configured": configured,
            "camera_available": available,
            "runtime_healthy": runtime_healthy,
            "busy": workflow_busy or manual_busy or lock_busy or reserved,
            "ready": not reasons,
            "reasons": reasons,
        }


class JobScheduler:
    """Persistent per-device queue and its validated state transitions.







    The process-local lock covers queue inspection, readiness checks, durable



    state changes, and reservations. The JSON store writes each resulting



    snapshot atomically. Physical ownership remains guarded by DeviceLocks in



    WorkflowService/Runner.



    """

    def __init__(self, store, readiness):

        self.store = store

        self.readiness = readiness

        self.lock = threading.RLock()

        self.reservations = {}

        self.integration_gate = None

        self.recover_pending_jobs()

    def recover_pending_jobs(self):

        # HID commands cannot be safely replayed after a process interruption.

        timestamp = now()

        jobs = self.store.recover_running(
            timestamp,
            "Interrupted by backend restart; physical actions were not replayed.",
        )

        recoveries = self.store.recover_incomplete_recoveries(
            timestamp,
            "Recovery was interrupted by backend restart; it was not replayed.",
        )

        return jobs + recoveries

    def enqueue(
        self,
        account,
        workflow,
        payload,
        *,
        external_ref=None,
        bank=None,
        action=None,
        recovery_workflow_id=None,
        protected_metadata=None,
    ):

        with self.lock:

            ready = self.is_device_ready(account.device_id)["ready"]

            job = Job.create(
                account.account_id,
                account.device_id,
                workflow,
                payload,
                "QUEUED" if ready else "WAITING_FOR_DEVICE",
                external_ref=external_ref,
                bank=bank,
                action=action,
                recovery_workflow_id=recovery_workflow_id,
                protected_metadata=protected_metadata,
            )

            return self.store.create(job)

    def create_retry(self, job_id, mode, *, withdraw_authorized=False):
        """Create a device-pinned retry under its mode-specific recovery rule."""

        with self.lock:

            original = self.get_job(job_id)

            if original.action == "withdraw_v2" and not withdraw_authorized:

                raise RecordConflict(
                    "Withdraw Bot retry requires integration safety validation."
                )

            if original.status not in {"FAILED", "INTERRUPTED"}:

                raise RecordConflict("Only failed or interrupted jobs can be retried.")

            if mode == "failed_step":

                allowed_recovery_states = {"READY"}

                if original.action == "withdraw_v2" and withdraw_authorized:

                    allowed_recovery_states.add("NEEDS_ATTENTION")

                if original.recovery_status not in allowed_recovery_states:

                    raise RecordConflict(
                        "The phone must be restored and marked ready before retrying."
                    )

                if original.failed_step_index is None:

                    raise RecordConflict(
                        "The failed workflow step is unavailable; restart the workflow instead."
                    )

                start_step_index = original.failed_step_index

            elif mode == "restart":

                if original.recovery_status != "READY":

                    raise RecordConflict(
                        "Phone is not verified ready. Use Manual Assistance and "
                        "Verify Phone Ready before restarting the workflow."
                    )

                start_step_index = 0

            else:

                raise ValueError("Retry mode must be failed_step or restart.")

            if any(
                candidate.status in PENDING_JOB_STATES | {"RUNNING"}
                and (
                    candidate.retry_of_job_id == original.job_id
                    or (
                        original.action == "withdraw_v2"
                        and candidate.action == "withdraw_v2"
                        and candidate.device_id == original.device_id
                        and candidate.external_ref == original.external_ref
                    )
                )
                for candidate in self.list_jobs()
            ):

                raise RecordConflict("A retry for this job is already active.")

            ready = self.is_device_ready(original.device_id)["ready"]

            retry = Job.create(
                original.account_id,
                original.device_id,
                original.workflow,
                original.payload,
                "QUEUED" if ready else "WAITING_FOR_DEVICE",
                external_ref=original.external_ref,
                bank=original.bank,
                action=original.action,
                recovery_workflow_id=original.recovery_workflow_id,
                retry_of_job_id=original.job_id,
                start_step_index=start_step_index,
            )

            return self.store.create(retry)

    def get_job(self, job_id):

        return self.store.get(job_id)

    def list_jobs(self):

        return self.store.list()

    def is_device_ready(self, device_id):

        return self.readiness.inspect(
            device_id,
            reserved=device_id in self.reservations,
        )

    def integration_blocks_job(self, job):

        return bool(
            job.action == "withdraw_v2"
            and self.integration_gate is not None
            and self.integration_gate(job.device_id)
        )

    def next_job_for_device(self, device_id):

        with self.lock:

            return next(
                (
                    job
                    for job in self.list_jobs()
                    if job.device_id == device_id and job.status in PENDING_JOB_STATES
                ),
                None,
            )

    def reserve_balance_workflow(
        self, device_id, reservation, *, manual_recovery=False
    ):
        """Atomically reserve one balance run without weakening payment gates.







        An explicit operator balance check is recovery work: it still requires



        the physical phone/camera/runtime to be idle and healthy, but it may run



        ahead of payment work which is waiting solely on Withdraw recovery.



        Ordinary/background balance checks retain the normal queue and



        integration-gate priority rules.



        """

        with self.lock:

            if device_id in self.reservations:

                raise RecordConflict("Device already has a running job.")

            readiness = self.is_device_ready(device_id)

            if not readiness["ready"]:

                raise RecordConflict("Device is not ready for a balance check.")

            if any(
                job.device_id == device_id and job.status == "RUNNING"
                for job in self.list_jobs()
            ):

                raise RecordConflict("Device already has a running job.")

            if not manual_recovery and self.next_job_for_device(device_id) is not None:

                raise RecordConflict(
                    "Queued device work takes priority over a balance check."
                )

            self.reservations[device_id] = reservation

    @staticmethod
    def _summary(value):

        if value is not None and (
            not isinstance(value, str)
            or len(value) > 500
            or any(ord(character) < 32 for character in value)
        ):

            raise ValueError("Job result summaries must be at most 500 characters.")

        return value

    def _transition(self, job, status, **changes):

        if status not in JOB_TRANSITIONS[job.status]:

            raise RecordConflict(
                f"Job cannot transition from {job.status} to {status}."
            )

        return self.store.replace(replace(job, status=status, **changes))

    def claim_next_job_for_device(self, device_id):
        """Atomically claim the oldest eligible job fixed to ``device_id``."""

        with self.lock:

            job = self.next_job_for_device(device_id)

            if job is None:

                return None

            if device_id in self.reservations or any(
                item.device_id == device_id and item.status == "RUNNING"
                for item in self.list_jobs()
            ):

                return None

            readiness = self.is_device_ready(device_id)

            if not readiness["ready"]:

                if job.status != "WAITING_FOR_DEVICE":

                    self._transition(job, "WAITING_FOR_DEVICE")

                return None

            if self.integration_blocks_job(job):

                return None

            running = self._transition(
                job,
                "RUNNING",
                started_at=now(),
                finished_at=None,
                error=None,
                result=None,
            )

            self.reservations[device_id] = job.job_id

            return running

    def reserve_device(self, device_id, job_id):
        """Backward-compatible explicit claim used by existing callers/tests."""

        with self.lock:

            if device_id in self.reservations or any(
                job.device_id == device_id and job.status == "RUNNING"
                for job in self.list_jobs()
            ):

                raise RecordConflict("Device already has a running job.")

            job = self.get_job(job_id)

            if job.device_id != device_id:

                raise ValueError("A job cannot be moved to another device.")

            if job.status not in PENDING_JOB_STATES:

                raise RecordConflict("Job is not waiting in the device queue.")

            next_job = self.next_job_for_device(device_id)

            if next_job is None or next_job.job_id != job_id:

                raise RecordConflict("An earlier job is waiting for this device.")

            if not self.is_device_ready(device_id)["ready"]:

                if job.status != "WAITING_FOR_DEVICE":

                    self._transition(job, "WAITING_FOR_DEVICE")

                raise RecordConflict("Device is not ready for this job.")

            if self.integration_blocks_job(job):

                raise RecordConflict(
                    "Withdraw job is waiting for external withdrawal recovery."
                )

            running = self._transition(job, "RUNNING", started_at=now())

            self.reservations[device_id] = job_id

            return running

    def _finish(
        self,
        job_id,
        status,
        *,
        error=None,
        result=None,
        screenshots=None,
        failed_step_index=None,
        failed_step_id=None,
        failed_step_type=None,
        outputs=None,
    ):

        with self.lock:

            job = self.get_job(job_id)

            if self.reservations.get(job.device_id) != job_id:

                raise RecordConflict("Job does not own this device reservation.")

            error = self._summary(error)

            result = self._summary(result)

            finished = self._transition(
                job,
                status,
                finished_at=now(),
                error=error,
                result=result,
                outputs=deepcopy(outputs) if outputs is not None else job.outputs,
                cancel_requested=job.cancel_requested or status == "CANCELLED",
                recovery_status=(
                    "NEEDS_ATTENTION"
                    if status in {"FAILED", "INTERRUPTED"}
                    else job.recovery_status
                ),
                screenshots=(
                    deepcopy(screenshots)
                    if screenshots is not None
                    else job.screenshots
                ),
                failed_step_index=(
                    failed_step_index if status == "FAILED" else job.failed_step_index
                ),
                failed_step_id=(
                    failed_step_id if status == "FAILED" else job.failed_step_id
                ),
                failed_step_type=(
                    failed_step_type if status == "FAILED" else job.failed_step_type
                ),
            )

            del self.reservations[job.device_id]

            return finished

    def complete_job(self, job_id, result=None, *, screenshots=None, outputs=None):

        return self._finish(
            job_id, "COMPLETED", result=result, screenshots=screenshots, outputs=outputs
        )

    def fail_job(
        self,
        job_id,
        error,
        *,
        screenshots=None,
        failed_step_index=None,
        failed_step_id=None,
        failed_step_type=None,
    ):

        return self._finish(
            job_id,
            "FAILED",
            error=error,
            screenshots=screenshots,
            failed_step_index=failed_step_index,
            failed_step_id=failed_step_id,
            failed_step_type=failed_step_type,
        )

    def finish_cancelled(self, job_id, result=None, *, screenshots=None):

        return self._finish(job_id, "CANCELLED", result=result, screenshots=screenshots)

    def interrupt_job(self, job_id, error):

        return self._finish(job_id, "INTERRUPTED", error=error)

    def release_device(self, device_id, job_id, *, error=None, result=None):

        job = self.get_job(job_id)

        if job.device_id != device_id:

            raise ValueError("A job cannot be released from another device.")

        return (
            self.fail_job(job_id, error)
            if error is not None
            else self.complete_job(job_id, result)
        )

    def cancel_job(self, job_id):

        with self.lock:

            job = self.get_job(job_id)

            if job.status in PENDING_JOB_STATES:

                return self._transition(
                    job,
                    "CANCELLED",
                    finished_at=now(),
                    cancel_requested=True,
                )

            if job.status == "RUNNING":

                return self.store.replace(replace(job, cancel_requested=True))

            if job.status in TERMINAL_JOB_STATES:

                return job

            raise RecordConflict("Job cannot be cancelled in its current state.")

    def begin_recovery(self, job_id, recovery_workflow_id):

        with self.lock:

            job = self.get_job(job_id)

            if job.status not in {"FAILED", "INTERRUPTED"}:

                raise RecordConflict(
                    "Only failed or interrupted jobs can be recovered."
                )

            if job.recovery_status == "RECOVERING":

                raise RecordConflict("Phone recovery is already running.")

            if job.device_id in self.reservations or any(
                item.device_id == job.device_id and item.status == "RUNNING"
                for item in self.list_jobs()
            ):

                raise RecordConflict("Device already has running work.")

            readiness = self.readiness.inspect(job.device_id, reserved=False)

            if not readiness["ready"]:

                raise RecordConflict("Device is not ready to start recovery.")

            reservation = "recovery:" + job.job_id

            self.reservations[job.device_id] = reservation

            try:

                return self.store.replace(
                    replace(
                        job,
                        recovery_workflow_id=recovery_workflow_id,
                        recovery_status="RECOVERING",
                        recovery_run_id=None,
                        recovery_error=None,
                        recovered_at=None,
                    )
                )

            except Exception:

                del self.reservations[job.device_id]

                raise

    def attach_recovery_run(self, job_id, run_id):

        with self.lock:

            job = self.get_job(job_id)

            if (
                job.recovery_status != "RECOVERING"
                or self.reservations.get(job.device_id) != "recovery:" + job_id
            ):

                raise RecordConflict("Phone recovery is no longer active.")

            return self.store.replace(replace(job, recovery_run_id=run_id))

    def finish_recovery(self, job_id, *, successful, error=None, screenshots=None):

        with self.lock:

            job = self.get_job(job_id)

            reservation = "recovery:" + job_id

            if self.reservations.get(job.device_id) != reservation:

                raise RecordConflict("Recovery does not own this device reservation.")

            finished = self.store.replace(
                replace(
                    job,
                    recovery_status="READY" if successful else "NEEDS_ATTENTION",
                    recovery_error=None if successful else self._summary(error),
                    recovered_at=now(),
                    screenshots=(
                        job.screenshots + deepcopy(screenshots)
                        if screenshots
                        else job.screenshots
                    ),
                )
            )

            del self.reservations[job.device_id]

            return finished

    def queue_position(self, target):

        if target.status not in PENDING_JOB_STATES:

            return None

        pending = [
            job
            for job in self.list_jobs()
            if job.device_id == target.device_id and job.status in PENDING_JOB_STATES
        ]

        for index, job in enumerate(pending, 1):

            if job.job_id == target.job_id:

                return index

        return None

    def public(self, job):

        return job.public(self.queue_position(job))

    def mark_ready(self, job_id):

        with self.lock:

            job = self.store.get(job_id)

            if job.status not in {"FAILED", "INTERRUPTED"}:

                raise RecordConflict(
                    "Only failed or interrupted jobs can be manually marked ready."
                )

            if job.recovery_status == "READY":

                return job

            updated = replace(
                job,
                recovery_status="READY",
                recovered_at=now(),
                recovery_error=None,
            )

            return self.store.replace(updated)
