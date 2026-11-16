"""Persistent background job execution and per-device concurrency tests."""

import tempfile
import threading
import time
import unittest
import random
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch

from automation.jobs.executor import BackgroundJobExecutor
from automation.jobs.models import Job
from automation.jobs.service import JobScheduler
from automation.jobs.storage import AccountBindingStore, JobStore
from automation.workflows.models import new_workflow
from automation.workflows.service import WorkflowService
from automation.workflows.storage import WorkflowStorage
from device_lock import DeviceLocks


class ReadyDevices:
    def __init__(self, ready=("phone_A", "phone_B")):
        self.ready = set(ready)

    def inspect(self, device_id, *, reserved=False):
        ready = device_id in self.ready and not reserved
        return {
            "device_id": device_id,
            "registered": device_id in self.ready,
            "connected": device_id in self.ready,
            "camera_configured": True,
            "camera_available": True,
            "runtime_healthy": True,
            "busy": reserved,
            "ready": ready,
            "reasons": ["job_reserved"] if reserved else [],
        }


class ControlledWorkflowService:
    """A controllable stand-in for the existing asynchronous WorkflowService."""

    def __init__(self):
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.runs = {}
        self.started = []
        self.started_offsets = []
        self.maximum_by_device = {}
        self.maximum_global = 0
        self.physical_attempts = Counter()
        self.recovery_started = []

    def has_active_run(self, device_id=None):
        with self.lock:
            return any(
                not run["done"].is_set()
                and (device_id is None or run["device_id"] == device_id)
                for run in self.runs.values()
            )

    def start_for_job(self, job, workflow, *, cancel_event=None):
        self.assert_workflow(job, workflow)
        run_id = "run_" + job.job_id
        run = {
            "device_id": job.device_id,
            "job_id": job.job_id,
            "done": threading.Event(),
            "status": "running",
            "cancel": cancel_event,
        }
        with self.lock:
            self.runs[run_id] = run
            self.started.append((job.job_id, job.device_id))
            self.started_offsets.append((job.job_id, job.start_step_index))
            active = sum(
                not item["done"].is_set()
                for item in self.runs.values()
                if item["device_id"] == job.device_id
            )
            self.maximum_by_device[job.device_id] = max(
                active, self.maximum_by_device.get(job.device_id, 0)
            )
            global_active = sum(
                not item["done"].is_set() for item in self.runs.values()
            )
            self.maximum_global = max(self.maximum_global, global_active)
            self.changed.notify_all()
        if cancel_event is not None and cancel_event.is_set():
            self.cancel(run_id)
        return {"run_id": run_id}

    def start(self, workflow, variables, *, device_id=None, cancel_event=None):
        run_id = "run_recovery_" + workflow["id"]
        run = {
            "device_id": device_id,
            "job_id": None,
            "done": threading.Event(),
            "status": "running",
            "cancel": cancel_event,
        }
        with self.lock:
            self.runs[run_id] = run
            self.recovery_started.append((workflow, device_id, variables))
        return {"run_id": run_id}

    @staticmethod
    def assert_workflow(job, workflow):
        if workflow["id"] != job.workflow:
            raise AssertionError("wrong workflow")

    def wait(self, run_id, timeout=None):
        run = self.runs[run_id]
        if not run["done"].wait(timeout):
            return None
        return {
            "status": run["status"],
            "message": "Controlled workflow failure.",
            "step_index": 2,
            "step_id": "failed_step",
        }

    def cancel(self, run_id):
        run = self.runs[run_id]
        run["status"] = "cancelled"
        run["done"].set()
        return {"run_id": run_id, "status": "cancelled"}

    def complete_job(self, job_id, status="passed"):
        with self.changed:
            run = next(item for item in self.runs.values() if item["job_id"] == job_id)
            run["status"] = status
            run["done"].set()
            self.changed.notify_all()

    def attempt_physical_action(self, job_id, *, fail=False):
        with self.changed:
            run = next(item for item in self.runs.values() if item["job_id"] == job_id)
            self.physical_attempts[(run["device_id"], job_id)] += 1
        if fail:
            self.complete_job(job_id, "failed")

    def wait_for_started(self, count, timeout=2.0):
        with self.changed:
            return self.changed.wait_for(lambda: len(self.started) >= count, timeout)

    def active_job_ids(self):
        with self.lock:
            return {
                run["job_id"]
                for run in self.runs.values()
                if run["job_id"] is not None and not run["done"].is_set()
            }

    def complete_recovery(self, status="passed"):
        with self.lock:
            run = next(
                item
                for item in self.runs.values()
                if item["job_id"] is None and not item["done"].is_set()
            )
            run["status"] = status
            run["done"].set()


class BackgroundJobExecutorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.accounts = AccountBindingStore(root / "accounts.json")
        self.jobs = JobStore(root / "jobs.json")
        self.workflows = WorkflowStorage(root / "workflows")
        workflow = new_workflow("Queued job")
        workflow["steps"] = [{"id": "wait", "type": "wait", "seconds": 0.01}]
        self.workflows.save(workflow, create=True)
        self.workflow_id = workflow["id"]
        self.service = ControlledWorkflowService()

    def binding(self, account_id, device_id):
        return self.accounts.create(
            {
                "account_id": account_id,
                "display_name": account_id,
                "device_id": device_id,
                "enabled": True,
            }
        )

    @staticmethod
    def eventually(predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("condition was not reached")

    def executor(self, scheduler):
        executor = BackgroundJobExecutor(
            scheduler,
            self.workflows,
            self.service,
            poll_interval=0.01,
        ).start()
        self.addCleanup(executor.close)
        return executor

    def test_same_device_jobs_execute_sequentially_and_wait_when_busy(self):
        account = self.binding("account_a", "phone_A")
        scheduler = JobScheduler(self.jobs, ReadyDevices())
        first = scheduler.enqueue(account, self.workflow_id, {"sequence": 1})
        executor = self.executor(scheduler)
        self.eventually(lambda: len(self.service.started) == 1)

        second = scheduler.enqueue(account, self.workflow_id, {"sequence": 2})
        self.assertEqual(self.jobs.get(second.job_id).status, "WAITING_FOR_DEVICE")
        time.sleep(0.04)
        self.assertEqual(len(self.service.started), 1)

        self.service.complete_job(first.job_id)
        self.eventually(lambda: len(self.service.started) == 2)
        self.assertEqual(
            self.service.started,
            [(first.job_id, "phone_A"), (second.job_id, "phone_A")],
        )
        self.assertEqual(self.service.maximum_by_device["phone_A"], 1)
        self.service.complete_job(second.job_id)
        self.assertTrue(executor.wait_until_idle())

    def test_different_devices_execute_concurrently_without_rerouting(self):
        account_a = self.binding("account_a", "phone_A")
        account_b = self.binding("account_b", "phone_B")
        scheduler = JobScheduler(self.jobs, ReadyDevices())
        job_a = scheduler.enqueue(account_a, self.workflow_id, {})
        job_b = scheduler.enqueue(account_b, self.workflow_id, {})
        self.executor(scheduler)

        self.eventually(lambda: len(self.service.started) == 2)
        self.assertEqual({device for _, device in self.service.started}, {"phone_A", "phone_B"})
        self.assertEqual(self.jobs.get(job_a.job_id).device_id, "phone_A")
        self.assertEqual(self.jobs.get(job_b.job_id).device_id, "phone_B")
        self.service.complete_job(job_a.job_id)
        self.service.complete_job(job_b.job_id)

    def test_four_jobs_interleave_concurrently_and_preserve_per_device_fifo(self):
        phone_1 = self.binding("account_1", "phone_1")
        phone_2 = self.binding("account_2", "phone_2")
        scheduler = JobScheduler(
            self.jobs, ReadyDevices(("phone_1", "phone_2"))
        )
        job_a = scheduler.enqueue(phone_1, self.workflow_id, {"job": "A"})
        job_b = scheduler.enqueue(phone_1, self.workflow_id, {"job": "B"})
        job_c = scheduler.enqueue(phone_2, self.workflow_id, {"job": "C"})
        job_d = scheduler.enqueue(phone_2, self.workflow_id, {"job": "D"})
        executor = self.executor(scheduler)

        self.assertTrue(self.service.wait_for_started(2))
        self.assertEqual(
            self.service.active_job_ids(), {job_a.job_id, job_c.job_id}
        )
        self.assertNotIn(job_b.job_id, dict(self.service.started))
        self.assertNotIn(job_d.job_id, dict(self.service.started))

        self.service.complete_job(job_a.job_id)
        self.assertTrue(self.service.wait_for_started(3))
        self.assertEqual(
            self.service.active_job_ids(), {job_b.job_id, job_c.job_id}
        )

        self.service.complete_job(job_c.job_id)
        self.assertTrue(self.service.wait_for_started(4))
        self.assertEqual(
            self.service.active_job_ids(), {job_b.job_id, job_d.job_id}
        )
        self.assertEqual(
            [job_id for job_id, device in self.service.started if device == "phone_1"],
            [job_a.job_id, job_b.job_id],
        )
        self.assertEqual(
            [job_id for job_id, device in self.service.started if device == "phone_2"],
            [job_c.job_id, job_d.job_id],
        )
        self.assertEqual(self.service.maximum_by_device, {"phone_1": 1, "phone_2": 1})

        self.service.complete_job(job_b.job_id)
        self.service.complete_job(job_d.job_id)
        self.assertTrue(executor.wait_until_idle())

    def test_failure_needs_attention_blocks_only_the_affected_device_queue(self):
        phone_1 = self.binding("account_1", "phone_1")
        phone_2 = self.binding("account_2", "phone_2")
        scheduler = JobScheduler(
            self.jobs, ReadyDevices(("phone_1", "phone_2"))
        )
        job_a = scheduler.enqueue(phone_1, self.workflow_id, {"job": "A"})
        job_b = scheduler.enqueue(phone_1, self.workflow_id, {"job": "B"})
        job_c = scheduler.enqueue(phone_2, self.workflow_id, {"job": "C"})
        executor = self.executor(scheduler)

        self.assertTrue(self.service.wait_for_started(2))
        self.assertEqual(
            self.service.active_job_ids(), {job_a.job_id, job_c.job_id}
        )
        self.service.complete_job(job_a.job_id, "failed")
        self.eventually(lambda: self.jobs.get(job_a.job_id).status == "FAILED")
        failed = self.jobs.get(job_a.job_id)
        self.assertEqual(failed.recovery_status, "NEEDS_ATTENTION")
        self.assertEqual(self.jobs.get(job_c.job_id).status, "RUNNING")
        self.assertIn(job_c.job_id, self.service.active_job_ids())

        executor._dispatch()
        self.assertEqual(self.jobs.get(job_b.job_id).status, "WAITING_FOR_DEVICE")
        self.assertNotIn(job_b.job_id, dict(self.service.started))
        readiness = scheduler.is_device_ready("phone_1")
        self.assertFalse(readiness["ready"])
        self.assertIn("needs_attention", readiness["reasons"])
        self.assertIn(job_c.job_id, self.service.active_job_ids())

        self.service.complete_job(job_c.job_id)
        self.eventually(lambda: self.jobs.get(job_c.job_id).status == "COMPLETED")
        self.assertEqual(self.jobs.get(job_c.job_id).status, "COMPLETED")
        executor.cancel(job_b.job_id)
        self.assertTrue(executor.wait_until_idle())

    def test_one_device_physical_failure_is_sent_once_and_does_not_affect_other(self):
        phone_1 = self.binding("account_1", "phone_1")
        phone_2 = self.binding("account_2", "phone_2")
        scheduler = JobScheduler(
            self.jobs, ReadyDevices(("phone_1", "phone_2"))
        )
        job_a = scheduler.enqueue(phone_1, self.workflow_id, {})
        job_c = scheduler.enqueue(phone_2, self.workflow_id, {})
        executor = self.executor(scheduler)

        self.assertTrue(self.service.wait_for_started(2))
        self.service.attempt_physical_action(job_a.job_id, fail=True)
        self.eventually(lambda: self.jobs.get(job_a.job_id).status == "FAILED")
        self.assertEqual(
            self.service.physical_attempts[("phone_1", job_a.job_id)], 1
        )
        self.assertEqual(self.jobs.get(job_c.job_id).status, "RUNNING")
        self.assertIn(job_c.job_id, self.service.active_job_ids())
        self.assertIn("phone_2", scheduler.reservations)

        self.service.complete_job(job_c.job_id)
        self.assertTrue(executor.wait_until_idle())
        self.assertEqual(self.jobs.get(job_c.job_id).status, "COMPLETED")
        self.assertEqual(
            self.service.physical_attempts[("phone_1", job_a.job_id)], 1
        )

    def test_failure_persists_zero_based_failed_step_index_and_step_id(self):
        account = self.binding("account_a", "phone_A")
        scheduler = JobScheduler(self.jobs, ReadyDevices())
        job = scheduler.enqueue(account, self.workflow_id, {})
        executor = self.executor(scheduler)
        self.assertTrue(self.service.wait_for_started(1))

        self.service.complete_job(job.job_id, "failed")
        self.eventually(lambda: self.jobs.get(job.job_id).status == "FAILED")

        failed = self.jobs.get(job.job_id)
        self.assertEqual(failed.failed_step_index, 1)
        self.assertEqual(failed.failed_step_id, "failed_step")
        self.assertEqual(failed.start_step_index, 0)

    def test_retry_creates_new_same_device_job_and_preserves_original_failure(self):
        account = self.binding("account_a", "phone_A")
        scheduler = JobScheduler(self.jobs, ReadyDevices())
        original = scheduler.enqueue(account, self.workflow_id, {"value": "same"})
        scheduler.reserve_device("phone_A", original.job_id)
        original = scheduler.fail_job(
            original.job_id,
            "failed",
            failed_step_index=1,
            failed_step_id="failed_step",
        )

        with self.assertRaisesRegex(Exception, "marked ready"):
            scheduler.create_retry(original.job_id, "failed_step")
        original = scheduler.mark_ready(original.job_id)
        retry = scheduler.create_retry(original.job_id, "failed_step")

        self.assertNotEqual(retry.job_id, original.job_id)
        self.assertEqual(self.jobs.get(original.job_id).status, "FAILED")
        self.assertEqual(retry.retry_of_job_id, original.job_id)
        self.assertEqual(retry.start_step_index, 1)
        self.assertEqual(retry.device_id, "phone_A")
        self.assertEqual(retry.account_id, original.account_id)
        self.assertEqual(retry.workflow, original.workflow)
        self.assertEqual(retry.payload, original.payload)

    def test_withdraw_retry_requires_integration_safety_validation(self):
        account = self.binding("account_a", "phone_A")
        scheduler = JobScheduler(self.jobs, ReadyDevices())
        original = scheduler.enqueue(
            account,
            self.workflow_id,
            {"amount": "10.00"},
            external_ref="TX-RETRY",
            action="withdraw_v2",
            protected_metadata={"transfer_method": "mobile_number"},
        )
        scheduler.reserve_device("phone_A", original.job_id)
        scheduler.fail_job(
            original.job_id,
            "failed",
            failed_step_index=1,
            failed_step_id="failed_step",
        )
        scheduler.mark_ready(original.job_id)

        with self.assertRaisesRegex(Exception, "integration safety validation"):
            scheduler.create_retry(original.job_id, "failed_step")

        retry = scheduler.create_retry(
            original.job_id, "failed_step", withdraw_authorized=True
        )
        self.assertEqual(retry.action, "withdraw_v2")
        self.assertEqual(retry.external_ref, "TX-RETRY")
        self.assertEqual(retry.payload, {"amount": "10.00"})
        self.assertEqual(
            retry.protected_metadata, {"transfer_method": "mobile_number"}
        )

    def test_restart_retry_starts_at_zero_and_normal_jobs_default_to_zero(self):
        account = self.binding("account_a", "phone_A")
        scheduler = JobScheduler(self.jobs, ReadyDevices())
        normal = scheduler.enqueue(account, self.workflow_id, {})
        scheduler.reserve_device("phone_A", normal.job_id)
        failed = scheduler.fail_job(
            normal.job_id,
            "failed",
            failed_step_index=1,
            failed_step_id="failed_step",
        )
        scheduler.mark_ready(failed.job_id)

        retry = scheduler.create_retry(failed.job_id, "restart")

        self.assertEqual(normal.start_step_index, 0)
        self.assertEqual(retry.start_step_index, 0)
        self.assertEqual(retry.retry_of_job_id, failed.job_id)

    def test_failed_step_retry_requires_a_captured_failure_index(self):
        account = self.binding("account_a", "phone_A")
        scheduler = JobScheduler(self.jobs, ReadyDevices())
        original = scheduler.enqueue(account, self.workflow_id, {})
        scheduler.reserve_device("phone_A", original.job_id)
        scheduler.fail_job(original.job_id, "failed before a step started")
        scheduler.mark_ready(original.job_id)

        with self.assertRaisesRegex(Exception, "failed workflow step is unavailable"):
            scheduler.create_retry(original.job_id, "failed_step")

        restart = scheduler.create_retry(original.job_id, "restart")
        self.assertEqual(restart.start_step_index, 0)

    def test_failed_step_retry_executes_with_persisted_start_offset(self):
        account = self.binding("account_a", "phone_A")
        scheduler = JobScheduler(self.jobs, ReadyDevices())
        original = scheduler.enqueue(account, self.workflow_id, {})
        scheduler.reserve_device("phone_A", original.job_id)
        scheduler.fail_job(
            original.job_id,
            "failed",
            failed_step_index=1,
            failed_step_id="failed_step",
        )
        scheduler.mark_ready(original.job_id)
        retry = scheduler.create_retry(original.job_id, "failed_step")
        executor = self.executor(scheduler)

        self.assertTrue(self.service.wait_for_started(1))
        self.assertEqual(self.service.started_offsets, [(retry.job_id, 1)])
        self.service.complete_job(retry.job_id)
        self.assertTrue(executor.wait_until_idle())

    def test_disconnected_device_waits_while_other_device_runs(self):
        readiness = ReadyDevices(("phone_2",))
        phone_1 = self.binding("account_1", "phone_1")
        phone_2 = self.binding("account_2", "phone_2")
        scheduler = JobScheduler(self.jobs, readiness)
        job_a = scheduler.enqueue(phone_1, self.workflow_id, {})
        job_c = scheduler.enqueue(phone_2, self.workflow_id, {})
        executor = self.executor(scheduler)

        self.assertTrue(self.service.wait_for_started(1))
        self.assertEqual(self.service.active_job_ids(), {job_c.job_id})
        self.assertEqual(self.jobs.get(job_a.job_id).status, "WAITING_FOR_DEVICE")
        self.assertNotIn(job_a.job_id, dict(self.service.started))

        readiness.ready.add("phone_1")
        executor.wake()
        self.assertTrue(self.service.wait_for_started(2))
        self.assertEqual(
            self.service.active_job_ids(), {job_a.job_id, job_c.job_id}
        )
        self.service.complete_job(job_a.job_id)
        self.service.complete_job(job_c.job_id)
        self.assertTrue(executor.wait_until_idle())

    def test_stress_many_devices_preserves_fifo_and_cleans_all_reservations(self):
        device_ids = [f"phone_{index}" for index in range(1, 7)]
        scheduler = JobScheduler(self.jobs, ReadyDevices(device_ids))
        jobs_by_device = {}
        for index, device_id in enumerate(device_ids, 1):
            account = self.binding(f"account_{index}", device_id)
            jobs_by_device[device_id] = [
                scheduler.enqueue(
                    account,
                    self.workflow_id,
                    {"device": device_id, "sequence": sequence},
                )
                for sequence in range(3)
            ]
        executor = self.executor(scheduler)

        self.assertTrue(self.service.wait_for_started(len(device_ids)))
        self.assertEqual(self.service.maximum_global, len(device_ids))
        rng = random.Random(20261002)
        expected_starts = len(device_ids)
        for round_index in range(2):
            completion_order = list(device_ids)
            rng.shuffle(completion_order)
            for device_id in completion_order:
                current = jobs_by_device[device_id][round_index]
                following = jobs_by_device[device_id][round_index + 1]
                self.service.complete_job(current.job_id)
                expected_starts += 1
                self.assertTrue(self.service.wait_for_started(expected_starts))
                self.assertIn(following.job_id, self.service.active_job_ids())

        final_order = list(device_ids)
        rng.shuffle(final_order)
        for device_id in final_order:
            self.service.complete_job(jobs_by_device[device_id][-1].job_id)
        self.assertTrue(executor.wait_until_idle(4.0))

        self.assertEqual(
            self.service.maximum_by_device,
            {device_id: 1 for device_id in device_ids},
        )
        self.assertGreater(self.service.maximum_global, 1)
        for device_id in device_ids:
            self.assertEqual(
                [
                    job_id
                    for job_id, started_device in self.service.started
                    if started_device == device_id
                ],
                [job.job_id for job in jobs_by_device[device_id]],
            )
        self.assertTrue(
            all(job.status == "COMPLETED" for job in self.jobs.list())
        )
        self.assertEqual(scheduler.reservations, {})
        self.assertEqual(executor.active, {})

    def test_existing_workflow_service_accepts_parallel_distinct_devices(self):
        workflow = new_workflow("Parallel devices")
        workflow["steps"] = [
            {"id": "wait", "type": "wait", "seconds": 0.1}
        ]
        service = WorkflowService(Mock(), DeviceLocks(self.jobs.path.parent / "locks"))
        with patch.dict(
            "os.environ",
            {"PHONE_INPUT_BACKEND": "raspberry_pi", "PHONE_SCREEN_BACKEND": "mock"},
        ):
            run_a = service.start(workflow, {}, device_id="phone_A")
            run_b = service.start(workflow, {}, device_id="phone_B")
            result_a = service.wait(run_a["run_id"], 1.0)
            result_b = service.wait(run_b["run_id"], 1.0)

        self.assertEqual(result_a["status"], "passed")
        self.assertEqual(result_b["status"], "passed")

    def test_queued_and_running_cancellation_are_persisted(self):
        account = self.binding("account_a", "phone_A")
        scheduler = JobScheduler(self.jobs, ReadyDevices())
        queued = scheduler.enqueue(account, self.workflow_id, {})
        self.assertEqual(scheduler.cancel_job(queued.job_id).status, "CANCELLED")

        running = scheduler.enqueue(account, self.workflow_id, {})
        executor = self.executor(scheduler)
        self.eventually(lambda: self.jobs.get(running.job_id).status == "RUNNING")
        cancelled = executor.cancel(running.job_id)
        self.assertTrue(cancelled.cancel_requested)
        self.eventually(lambda: self.jobs.get(running.job_id).status == "CANCELLED")
        self.assertNotIn("phone_A", executor.active)

    def test_restart_interrupts_running_and_preserves_queued(self):
        account = self.binding("account_a", "phone_A")
        original = JobScheduler(self.jobs, ReadyDevices())
        running = original.enqueue(account, self.workflow_id, {})
        queued = original.enqueue(account, self.workflow_id, {})
        original.reserve_device("phone_A", running.job_id)

        JobScheduler(self.jobs, ReadyDevices())

        recovered = self.jobs.get(running.job_id)
        self.assertEqual(recovered.status, "INTERRUPTED")
        self.assertIn("not replayed", recovered.error)
        self.assertIn(self.jobs.get(queued.job_id).status, {"QUEUED", "WAITING_FOR_DEVICE"})

    def test_restart_attention_gate_blocks_affected_device_only(self):
        phone_1 = self.binding("account_1", "phone_1")
        phone_2 = self.binding("account_2", "phone_2")
        original = JobScheduler(
            self.jobs, ReadyDevices(("phone_1", "phone_2"))
        )
        job_a = original.enqueue(phone_1, self.workflow_id, {"job": "A"})
        job_b = original.enqueue(phone_1, self.workflow_id, {"job": "B"})
        job_c = original.enqueue(phone_2, self.workflow_id, {"job": "C"})
        original.reserve_device("phone_1", job_a.job_id)

        restarted = JobScheduler(
            self.jobs, ReadyDevices(("phone_1", "phone_2"))
        )
        recovered = self.jobs.get(job_a.job_id)
        self.assertEqual(recovered.status, "INTERRUPTED")
        self.assertEqual(recovered.recovery_status, "NEEDS_ATTENTION")
        executor = self.executor(restarted)

        self.assertTrue(self.service.wait_for_started(1))
        self.assertEqual(self.service.active_job_ids(), {job_c.job_id})
        self.assertEqual(self.jobs.get(job_b.job_id).status, "WAITING_FOR_DEVICE")
        self.assertNotIn(job_b.job_id, dict(self.service.started))
        self.assertIn(
            "needs_attention",
            restarted.is_device_ready("phone_1")["reasons"],
        )
        self.assertEqual(self.jobs.get(job_c.job_id).status, "RUNNING")

        self.service.complete_job(job_c.job_id)
        self.eventually(lambda: self.jobs.get(job_c.job_id).status == "COMPLETED")
        executor.cancel(job_b.job_id)
        self.assertTrue(executor.wait_until_idle())

    def test_unavailable_fixed_device_is_not_reassigned(self):
        account = self.binding("account_a", "phone_A")
        scheduler = JobScheduler(self.jobs, ReadyDevices({"phone_B"}))
        job = scheduler.enqueue(account, self.workflow_id, {})
        self.executor(scheduler)
        time.sleep(0.05)

        persisted = self.jobs.get(job.job_id)
        self.assertEqual(persisted.device_id, "phone_A")
        self.assertEqual(persisted.status, "WAITING_FOR_DEVICE")
        self.assertEqual(self.service.started, [])

    def recovery_workflows(self):
        recovery = new_workflow("Return phone to ready")
        recovery["steps"] = [
            {"id": "home", "type": "key", "key": "HOME"},
            {
                "id": "ready",
                "type": "wait_for_text",
                "text": "Ready screen",
                "timeout_seconds": 5,
                "roi": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.2},
            },
        ]
        self.workflows.save(recovery, create=True)
        business = self.workflows.read(self.workflow_id)
        business["recovery_workflow_id"] = recovery["id"]
        self.workflows.save(business)
        return business, recovery

    def failed_job(self, business, recovery):
        account = self.binding("recover_account", "phone_A")
        scheduler = JobScheduler(self.jobs, ReadyDevices())
        job = scheduler.enqueue(
            account,
            business["id"],
            {},
            recovery_workflow_id=recovery["id"],
        )
        scheduler.reserve_device(job.device_id, job.job_id)
        return scheduler, scheduler.fail_job(job.job_id, "Unexpected screen")

    def test_recovery_uses_existing_workflow_service_and_never_retries_business_job(self):
        business, recovery = self.recovery_workflows()
        scheduler, job = self.failed_job(business, recovery)
        executor = BackgroundJobExecutor(scheduler, self.workflows, self.service)
        self.assertEqual(job.recovery_status, "NEEDS_ATTENTION")
        self.assertIsNone(job.recovery_run_id)
        self.assertIsNone(job.recovery_error)
        self.assertIsNone(job.recovered_at)

        started = executor.recover(job.job_id)
        self.assertEqual(started.recovery_status, "RECOVERING")
        self.eventually(lambda: len(self.service.recovery_started) == 1)
        recovery_run, device_id, variables = self.service.recovery_started[0]
        self.assertEqual(recovery_run["id"], recovery["id"])
        self.assertEqual(recovery_run["steps"][-1]["type"], "wait_for_text")
        self.assertEqual(device_id, "phone_A")
        self.assertEqual(variables, {})

        self.service.complete_recovery("passed")
        self.eventually(
            lambda: self.jobs.get(job.job_id).recovery_status == "READY"
        )
        recovered = self.jobs.get(job.job_id)
        self.assertEqual(recovered.status, "FAILED")
        self.assertEqual(self.service.started, [])
        self.assertNotIn("phone_A", scheduler.reservations)

    def test_successful_recovery_releases_attention_gate_for_next_job(self):
        business, recovery = self.recovery_workflows()
        scheduler, failed = self.failed_job(business, recovery)
        account = self.accounts.get("recover_account")
        queued = scheduler.enqueue(account, business["id"], {})
        self.assertEqual(queued.status, "WAITING_FOR_DEVICE")
        self.assertIn(
            "needs_attention",
            scheduler.is_device_ready("phone_A")["reasons"],
        )
        executor = self.executor(scheduler)
        executor.recover(failed.job_id)
        self.eventually(lambda: len(self.service.recovery_started) == 1)
        self.assertEqual(self.service.started, [])

        self.service.complete_recovery("passed")
        self.eventually(
            lambda: self.jobs.get(failed.job_id).recovery_status == "READY"
        )
        self.assertTrue(self.service.wait_for_started(1))
        self.assertEqual(self.service.started, [(queued.job_id, "phone_A")])
        self.assertEqual(self.jobs.get(queued.job_id).status, "RUNNING")
        self.assertNotIn(
            "needs_attention",
            scheduler.is_device_ready("phone_A")["reasons"],
        )

        self.service.complete_job(queued.job_id)
        self.assertTrue(executor.wait_until_idle())

    def test_failed_recovery_requires_attention_and_verify_ready_runs_only_final_wait(self):
        business, recovery = self.recovery_workflows()
        scheduler, job = self.failed_job(business, recovery)
        executor = BackgroundJobExecutor(scheduler, self.workflows, self.service)
        executor.recover(job.job_id)
        self.eventually(lambda: len(self.service.recovery_started) == 1)
        self.service.complete_recovery("failed")
        self.eventually(
            lambda: self.jobs.get(job.job_id).recovery_status == "NEEDS_ATTENTION"
        )

        executor.recover(job.job_id, verify_only=True)
        self.eventually(lambda: len(self.service.recovery_started) == 2)
        verification, _, _ = self.service.recovery_started[-1]
        self.assertEqual(len(verification["steps"]), 1)
        self.assertEqual(verification["steps"][0]["type"], "wait_for_text")
        self.service.complete_recovery("passed")
        self.eventually(
            lambda: self.jobs.get(job.job_id).recovery_status == "READY"
        )

    def test_recovery_requires_configuration_and_verified_final_condition(self):
        account = self.binding("no_recovery", "phone_A")
        scheduler = JobScheduler(self.jobs, ReadyDevices())
        job = scheduler.enqueue(account, self.workflow_id, {})
        scheduler.reserve_device(job.device_id, job.job_id)
        job = scheduler.fail_job(job.job_id, "Failed")
        executor = BackgroundJobExecutor(scheduler, self.workflows, self.service)
        with self.assertRaisesRegex(Exception, "No recovery workflow"):
            executor.recover(job.job_id)

        invalid = new_workflow("Unverified recovery")
        invalid["steps"] = [{"id": "home", "type": "key", "key": "HOME"}]
        self.workflows.save(invalid, create=True)
        business = self.workflows.read(self.workflow_id)
        business["recovery_workflow_id"] = invalid["id"]
        with self.assertRaisesRegex(ValueError, "end with wait_for_text"):
            self.workflows.save(business)

    def test_critical_step_metadata_is_preserved_but_rejected_for_recovery(self):
        workflow = new_workflow("Critical business action")
        workflow["steps"] = [
            {
                "id": "confirm",
                "type": "tap",
                "x": 0.5,
                "y": 0.8,
                "label": "Confirm transfer",
                "critical": True,
            }
        ]
        saved = self.workflows.save(workflow, create=True)
        self.assertTrue(saved["steps"][0]["critical"])

        recovery = new_workflow("Unsafe recovery")
        recovery["steps"] = [
            {
                "id": "critical",
                "type": "tap",
                "x": 0.5,
                "y": 0.5,
                "critical": True,
            },
            {
                "id": "ready",
                "type": "wait_for_text",
                "text": "Ready",
                "timeout_seconds": 5,
            },
        ]
        self.workflows.save(recovery, create=True)
        workflow["recovery_workflow_id"] = recovery["id"]
        with self.assertRaisesRegex(ValueError, "critical"):
            self.workflows.save(workflow)

    def test_backend_restart_marks_incomplete_recovery_needs_attention(self):
        business, recovery = self.recovery_workflows()
        scheduler, job = self.failed_job(business, recovery)
        scheduler.begin_recovery(job.job_id, recovery["id"])

        JobScheduler(self.jobs, ReadyDevices())

        restored = self.jobs.get(job.job_id)
        self.assertEqual(restored.status, "FAILED")
        self.assertEqual(restored.recovery_status, "NEEDS_ATTENTION")
        self.assertIn("not replayed", restored.recovery_error)


if __name__ == "__main__":
    unittest.main()
