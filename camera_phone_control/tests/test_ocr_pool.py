"""Persistent Paddle worker-pool dispatch and configuration tests."""

import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from app import create_app
from automation.screen.camera_ocr import _PaddleWorkerClient, _PaddleWorkerPool
from automation.screen.ocr_settings import (
    OCRPoolBusy,
    OCRSettingsService,
    OCRSettingsStorage,
)
from automation.screen.state import ScreenStateError
from automation.jobs.storage import AccountBindingStore, JobStore
from automation.workflows.storage import WorkflowStorage
from device_lock import DeviceLocks
from tests.test_camera_ocr_worker import protocol_worker
from tests.test_external_jobs import FakeCamera
from test_control import authenticate_admin


def ample_resources():
    return {
        "physical_cpu_cores": 8,
        "logical_cpu_threads": 8,
        "physical_cpu_fallback": False,
        "total_ram_bytes": 32 * 1024 ** 3,
        "available_ram_bytes": 20 * 1024 ** 3,
        "current_system_cpu_percent": 20.0,
        "current_system_ram_percent": 40.0,
    }


class PaddleWorkerPoolTests(unittest.TestCase):
    def setUp(self):
        self.context = multiprocessing.get_context("spawn")
        self.initializations = self.context.Value("i", 0)
        self.pools = []

    def tearDown(self):
        for pool in self.pools:
            pool.close()

    def pool(self, size, timeout=2.0):
        config = {
            "language": "en",
            "textline_orientation": False,
            "cpu_threads": 1,
            "initializations": self.initializations,
        }

        def factory(worker_id):
            return _PaddleWorkerClient(
                config,
                timeout,
                context=self.context,
                target=protocol_worker,
            )

        pool = _PaddleWorkerPool(
            config,
            timeout,
            size,
            client_factory=factory,
        )
        self.pools.append(pool)
        return pool

    @staticmethod
    def concurrent(pool, commands):
        barrier = threading.Barrier(len(commands) + 1)
        results = {}

        def request(command):
            barrier.wait()
            results[command] = pool.request(
                np.full((2, 2), command, np.uint8)
            )

        threads = [threading.Thread(target=request, args=(value,)) for value in commands]
        for thread in threads:
            thread.start()
        started = time.monotonic()
        barrier.wait()
        for thread in threads:
            thread.join(3)
        return results, time.monotonic() - started

    def test_pool_size_one_serializes_requests_and_records_metrics(self):
        pool = self.pool(1)
        pool.start()
        results, elapsed = self.concurrent(pool, [210, 211])

        self.assertGreaterEqual(elapsed, 0.35)
        self.assertEqual(
            {timing["worker_id"] for _, timing in results.values()},
            {1},
        )
        waits = sorted(timing["queue_wait_ms"] for _, timing in results.values())
        self.assertGreater(waits[1], 100)
        recent = pool.recent_metrics()
        self.assertEqual(recent["sample_count"], 2)
        self.assertGreater(recent["average_queue_wait_ms"], 50)
        self.assertGreater(recent["average_inference_ms"], 0)
        for result, timing in results.values():
            self.assertIn("Value", result[0]["res"]["rec_texts"][0])
            self.assertEqual(timing["pool_size"], 1)
            self.assertIn("inference_ms", timing)
            self.assertIn("total_ocr_ms", timing)

    def test_pool_size_two_runs_two_requests_in_parallel_and_correlates_results(self):
        pool = self.pool(2)
        pool.start()
        results, elapsed = self.concurrent(pool, [212, 213])

        self.assertLess(elapsed, 0.35)
        self.assertEqual(
            {timing["worker_id"] for _, timing in results.values()},
            {1, 2},
        )
        self.assertEqual(results[212][0][0]["res"]["rec_texts"], ["Value 212"])
        self.assertEqual(results[213][0][0]["res"]["rec_texts"], ["Value 213"])
        self.assertNotEqual(
            results[212][1]["request_id"],
            results[213][1]["request_id"],
        )

    def test_third_request_waits_when_two_workers_are_busy(self):
        pool = self.pool(2)
        pool.start()
        results, elapsed = self.concurrent(pool, [214, 215, 216])

        self.assertGreaterEqual(elapsed, 0.35)
        waits = sorted(timing["queue_wait_ms"] for _, timing in results.values())
        self.assertGreater(waits[-1], 100)

    def test_crashed_worker_recovers_without_stalling_later_request(self):
        pool = self.pool(1)
        pool.start()
        with self.assertRaisesRegex(ScreenStateError, "stopped unexpectedly"):
            pool.request(np.full((2, 2), 251, np.uint8))
        result, timing = pool.request(np.full((2, 2), 7, np.uint8))
        self.assertEqual(result[0]["res"]["rec_texts"], ["Value 7"])
        self.assertEqual(timing["worker_id"], 1)
        self.assertGreaterEqual(self.initializations.value, 2)

    def test_repeated_inference_errors_release_slot_and_keep_ipc_correlated(self):
        pool = self.pool(1)
        pool.start()
        pid = pool.slots[0]["client"].pid
        for index in range(4):
            with self.assertRaisesRegex(ScreenStateError, "RuntimeError: test failure") as caught:
                pool.request(
                    np.full((2, 2), 250, np.uint8),
                    request_id=f"failed-{index}",
                )
            self.assertEqual(caught.exception.ocr_worker_id, pid)
            status = pool.status()
            self.assertEqual(status["busy_workers"], 0)
            self.assertEqual(status["queued_requests"], 0)
            self.assertEqual(status["workers"][0]["state"], "IDLE")
        for command in (7, 8, 9):
            result, timing = pool.request(
                np.full((2, 2), command, np.uint8),
                request_id=f"success-{command}",
            )
            self.assertEqual(result[0]["res"]["rec_texts"], [f"Value {command}"])
            self.assertEqual(timing["request_id"], f"success-{command}")
            self.assertEqual(timing["worker_pid"], pid)
        self.assertEqual(self.initializations.value, 1)

    def test_multiple_workers_remain_usable_after_inference_error(self):
        pool = self.pool(2)
        pool.start()
        pids = [slot["client"].pid for slot in pool.slots]
        with self.assertRaises(ScreenStateError):
            pool.request(np.full((2, 2), 250, np.uint8))
        for command, worker_id in ((7, 2), (8, 1)):
            result, timing = pool.request(np.full((2, 2), command, np.uint8))
            self.assertEqual(result[0]["res"]["rec_texts"], [f"Value {command}"])
            self.assertEqual(timing["worker_id"], worker_id)
            self.assertEqual(timing["worker_pid"], pids[worker_id - 1])
        self.assertEqual(self.initializations.value, 2)

    def test_safe_restart_up_and_down_and_rejects_active_inference(self):
        pool = self.pool(1)
        pool.start()
        running = threading.Thread(
            target=lambda: pool.request(np.full((2, 2), 220, np.uint8))
        )
        running.start()
        deadline = time.monotonic() + 1
        while pool.status()["busy_workers"] != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        with self.assertRaises(OCRPoolBusy):
            pool.restart(2)
        running.join(2)

        self.assertEqual(pool.restart(2)["pool_size"], 2)
        self.assertEqual(len(pool.status()["workers"]), 2)
        self.assertEqual(pool.restart(1)["pool_size"], 1)
        self.assertEqual(len(pool.status()["workers"]), 1)


class OCRSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "ocr-settings.json"

    def test_environment_default_and_validation(self):
        service = OCRSettingsService(
            OCRSettingsStorage(self.path),
            environment={"CAMERA_OCR_WORKER_POOL_SIZE": "3"},
            resource_provider=ample_resources,
        )
        self.assertEqual(service.worker_pool_size, 3)
        self.assertEqual(service.source, "environment")
        with self.assertRaisesRegex(ValueError, "integer from 1 to 64"):
            OCRSettingsService(
                OCRSettingsStorage(self.path),
                environment={"CAMERA_OCR_WORKER_POOL_SIZE": "65"},
                resource_provider=ample_resources,
            )
        with self.assertRaisesRegex(ValueError, "integer from 1 to"):
            service.update({"worker_pool_size": 0})

    def test_persisted_setting_overrides_environment(self):
        storage = OCRSettingsStorage(self.path)
        storage.save(4)
        service = OCRSettingsService(
            storage,
            environment={"CAMERA_OCR_WORKER_POOL_SIZE": "1"},
            resource_provider=ample_resources,
        )
        self.assertEqual(service.worker_pool_size, 4)
        self.assertEqual(service.source, "persisted")

    def test_save_marks_restart_required_and_service_restarts_when_idle(self):
        class Pool:
            pool_size = 1
            busy = False

            def has_active_requests(self):
                return self.busy

            def restart(self, size):
                self.pool_size = size

            def status(self):
                return {
                    "pool_size": self.pool_size,
                    "busy_workers": int(self.busy),
                    "queued_requests": 0,
                    "accepting": True,
                    "workers": [],
                }

        service = OCRSettingsService(
            OCRSettingsStorage(self.path),
            environment={"CAMERA_OCR_WORKER_POOL_SIZE": "1"},
            resource_provider=ample_resources,
        )
        pool = Pool()
        service.attach(pool)
        saved = service.update({"worker_pool_size": 2})
        self.assertTrue(saved["restart_required"])
        pool.busy = True
        with self.assertRaises(OCRPoolBusy):
            service.restart()
        pool.busy = False
        restarted = service.restart()
        self.assertEqual(restarted["pool_size"], 2)
        self.assertFalse(restarted["restart_required"])

    def test_dashboard_settings_api_uses_backend_validation_and_persistence(self):
        storage = OCRSettingsStorage(self.path)
        with patch.dict(os.environ, {"CAMERA_OCR_WORKER_POOL_SIZE": "2"}):
            app = create_app(
                FakeCamera(),
                Mock(),
                DeviceLocks(self.path.parent / "locks"),
                WorkflowStorage(self.path.parent / "workflows"),
                manual_backend="appium",
                ocr_settings_storage=storage,
                ocr_resource_provider=ample_resources,
                account_store=AccountBindingStore(self.path.parent / "accounts.json"),
                job_store=JobStore(self.path.parent / "jobs.json"),
            )
        client = app.test_client()
        authenticate_admin(client)
        headers = {"X-Control-Token": app.config["CONTROL_TOKEN"]}

        initial = client.get("/api/ocr/settings")
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.json["worker_pool_size"], 2)
        status = client.get("/api/ocr/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json["physical_cpu_cores"], 8)
        self.assertEqual(status.json["cpu_worker_ceiling"], 6)
        self.assertIn("recommended_worker_count", status.json)
        self.assertIn("average_queue_wait_ms", status.json)
        saved = client.put(
            "/api/ocr/settings",
            json={"worker_pool_size": 3},
            headers=headers,
        )
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertTrue(saved.json["restart_required"])
        invalid = client.put(
            "/api/ocr/settings",
            json={"worker_pool_size": 9},
            headers=headers,
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(OCRSettingsStorage(self.path).load(), 3)


if __name__ == "__main__":
    unittest.main()
