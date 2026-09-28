"""Account-based job routing, readiness checks, and per-device reservations."""

from __future__ import annotations

import threading
from dataclasses import replace

from automation.workflows.models import now

from .models import Job, PENDING_JOB_STATES, TERMINAL_JOB_STATES
from .storage import RecordConflict


class DeviceEligibility:
    def __init__(self, state, camera_manager, workflow_service):
        self.state = state
        self.camera_manager = camera_manager
        self.workflow_service = workflow_service

    def inspect(self, device_id, *, reserved=False):
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
        camera = self.camera_manager.status(device_id, open_camera=False)
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
    """Persistent queue contract; it deliberately starts no worker thread."""

    def __init__(self, store, readiness):
        self.store = store
        self.readiness = readiness
        self.lock = threading.RLock()
        self.reservations = {}
        self.recover_pending_jobs()

    def recover_pending_jobs(self):
        # HID commands cannot be safely replayed after a process interruption.
        return self.store.recover_running(
            now(), "Interrupted by backend restart; physical actions were not replayed."
        )

    def enqueue(self, account, workflow, payload):
        with self.lock:
            ready = self.is_device_ready(account.device_id)["ready"]
            job = Job.create(
                account.account_id,
                account.device_id,
                workflow,
                payload,
                "QUEUED" if ready else "WAITING_FOR_DEVICE",
            )
            return self.store.create(job)

    def is_device_ready(self, device_id):
        return self.readiness.inspect(
            device_id, reserved=device_id in self.reservations
        )

    def next_job_for_device(self, device_id):
        with self.lock:
            return next(
                (
                    job
                    for job in self.store.list()
                    if job.device_id == device_id and job.status in PENDING_JOB_STATES
                ),
                None,
            )

    def reserve_device(self, device_id, job_id):
        with self.lock:
            if device_id in self.reservations or any(
                job.device_id == device_id and job.status == "RUNNING"
                for job in self.store.list()
            ):
                raise RecordConflict("Device already has a running job.")
            job = self.store.get(job_id)
            if job.device_id != device_id:
                raise ValueError("A job cannot be moved to another device.")
            if job.status not in PENDING_JOB_STATES:
                raise RecordConflict("Job is not waiting in the device queue.")
            next_job = self.next_job_for_device(device_id)
            if next_job is None or next_job.job_id != job_id:
                raise RecordConflict("An earlier job is waiting for this device.")
            if not self.is_device_ready(device_id)["ready"]:
                if job.status != "WAITING_FOR_DEVICE":
                    self.store.replace(replace(job, status="WAITING_FOR_DEVICE"))
                raise RecordConflict("Device is not ready for this job.")
            running = replace(job, status="RUNNING", started_at=now())
            self.store.replace(running)
            self.reservations[device_id] = job_id
            return running

    def release_device(self, device_id, job_id, *, error=None, result=None):
        with self.lock:
            if self.reservations.get(device_id) != job_id:
                raise RecordConflict("Job does not own this device reservation.")
            job = self.store.get(job_id)
            summary = error if error is not None else result
            if summary is not None and (
                not isinstance(summary, str)
                or len(summary) > 500
                or any(ord(character) < 32 for character in summary)
            ):
                raise ValueError("Job result summaries must be at most 500 characters.")
            finished = replace(
                job,
                status="FAILED" if error is not None else "COMPLETED",
                finished_at=now(),
                error=error,
                result=result,
            )
            self.store.replace(finished)
            del self.reservations[device_id]
            return finished

    def cancel_job(self, job_id):
        with self.lock:
            job = self.store.get(job_id)
            if job.status in PENDING_JOB_STATES:
                return self.store.replace(
                    replace(
                        job,
                        status="CANCELLED",
                        finished_at=now(),
                        cancel_requested=True,
                    )
                )
            if job.status == "RUNNING":
                return self.store.replace(replace(job, cancel_requested=True))
            if job.status in TERMINAL_JOB_STATES:
                return job
            raise RecordConflict("Job cannot be cancelled in its current state.")

    def queue_position(self, target):
        if target.status not in PENDING_JOB_STATES:
            return None
        pending = [
            job
            for job in self.store.list()
            if job.device_id == target.device_id and job.status in PENDING_JOB_STATES
        ]
        for index, job in enumerate(pending, 1):
            if job.job_id == target.job_id:
                return index
        return None

    def public(self, job):
        return job.public(self.queue_position(job))
