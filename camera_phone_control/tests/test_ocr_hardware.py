"""Hardware ceilings and conservative OCR pool recommendation tests."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from automation.screen.ocr_settings import (
    DEFAULT_WORKER_MEMORY_BYTES,
    OCRSettingsService,
    OCRSettingsStorage,
    calculate_hardware_capacity,
    detect_hardware_resources,
)

GIB = 1024 ** 3


def resources(
    *,
    physical=8,
    logical=8,
    total=32 * GIB,
    available=20 * GIB,
    cpu=40.0,
    ram=45.0,
):
    return {
        "physical_cpu_cores": physical,
        "logical_cpu_threads": logical,
        "physical_cpu_fallback": False,
        "total_ram_bytes": total,
        "available_ram_bytes": available,
        "current_system_cpu_percent": cpu,
        "current_system_ram_percent": ram,
    }


class Pool:
    def __init__(self, size=2, metrics=None, queued=0, rss=None):
        self.pool_size = size
        self.queued = queued
        self.rss = rss
        self.metrics = metrics or {
            "sample_count": 5,
            "average_queue_wait_ms": 500.0,
            "average_inference_ms": 700.0,
            "average_busy_ratio": 0.9,
            "lower_pool_inference_ms": None,
            "lower_pool_queue_wait_ms": None,
        }

    def status(self):
        return {
            "pool_size": self.pool_size,
            "busy_workers": 0,
            "queued_requests": self.queued,
            "workers": [],
            "accepting": True,
            "observed_worker_rss_bytes": self.rss,
        }

    def recent_metrics(self, window_seconds):
        return dict(self.metrics)

    def has_active_requests(self):
        return False

    def restart(self, size):
        self.pool_size = size


class OCRHardwareCapacityTests(unittest.TestCase):
    def test_cpu_budget_uses_floor_of_physical_cores(self):
        self.assertEqual(
            calculate_hardware_capacity(resources(physical=8), 80)[
                "cpu_worker_ceiling"
            ],
            6,
        )
        self.assertEqual(
            calculate_hardware_capacity(resources(physical=4), 80)[
                "cpu_worker_ceiling"
            ],
            3,
        )

    def test_physical_detection_falls_back_to_logical_threads(self):
        memory = SimpleNamespace(total=16 * GIB, available=12 * GIB, percent=25.0)
        fake_psutil = SimpleNamespace(
            cpu_count=lambda logical: 12 if logical else None,
            virtual_memory=lambda: memory,
            cpu_percent=lambda interval=None: 20.0,
        )
        with patch.dict(sys.modules, {"psutil": fake_psutil}):
            detected = detect_hardware_resources()
        self.assertEqual(detected["physical_cpu_cores"], 12)
        self.assertEqual(detected["logical_cpu_threads"], 12)
        self.assertTrue(detected["physical_cpu_fallback"])

    def test_ram_ceiling_can_reduce_cpu_and_final_ceiling_uses_minimum(self):
        capacity = calculate_hardware_capacity(
            resources(physical=8, total=4 * GIB, available=3 * GIB),
            80,
            DEFAULT_WORKER_MEMORY_BYTES,
        )
        self.assertEqual(capacity["cpu_worker_ceiling"], 6)
        self.assertEqual(capacity["ram_worker_ceiling"], 2)
        self.assertEqual(capacity["hardware_worker_ceiling"], 2)

    def test_cpu_capacity_budget_is_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = OCRSettingsStorage(Path(directory) / "settings.json")
            with self.assertRaisesRegex(ValueError, "25 to 90"):
                OCRSettingsService(
                    storage,
                    environment={"OCR_CPU_CAPACITY_PERCENT": "95"},
                    resource_provider=resources,
                )

    def test_persisted_value_above_ceiling_starts_safe_without_rewriting(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = OCRSettingsStorage(Path(directory) / "settings.json")
            storage.save(8)
            service = OCRSettingsService(
                storage,
                environment={},
                resource_provider=lambda: resources(physical=4),
            )
            self.assertEqual(service.worker_pool_size, 3)
            self.assertEqual(service.stored_worker_pool_size, 8)
            self.assertTrue(service.startup_clamped)
            self.assertIn("safe ceiling 3", service.startup_warning)
            self.assertEqual(storage.load(), 8)


class OCRRecommendationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = OCRSettingsStorage(Path(temporary.name) / "settings.json")

    def service(self, snapshot, pool=None, clock=None):
        service = OCRSettingsService(
            self.storage,
            environment={"CAMERA_OCR_WORKER_POOL_SIZE": "2"},
            resource_provider=lambda: dict(snapshot),
            clock=clock,
        )
        service.attach(pool or Pool())
        return service

    def test_high_queue_low_cpu_and_sufficient_ram_increases_one_step(self):
        status = self.service(resources(cpu=40, ram=45)).status()
        self.assertEqual(status["recommended_worker_count"], 3)
        self.assertIn("queue wait", status["recommendation_reason"])

    def test_high_cpu_prevents_increase(self):
        status = self.service(resources(cpu=85)).status()
        self.assertEqual(status["recommended_worker_count"], 2)
        self.assertIn("CPU headroom", status["recommendation_reason"])

    def test_high_ram_prevents_increase(self):
        status = self.service(resources(ram=90)).status()
        self.assertEqual(status["recommended_worker_count"], 2)
        self.assertIn("Memory headroom", status["recommendation_reason"])

    def test_low_queue_pressure_keeps_current_count(self):
        pool = Pool(metrics={
            "sample_count": 5,
            "average_queue_wait_ms": 10.0,
            "average_inference_ms": 600.0,
            "average_busy_ratio": 0.9,
            "lower_pool_inference_ms": None,
            "lower_pool_queue_wait_ms": None,
        })
        status = self.service(resources(), pool).status()
        self.assertEqual(status["recommended_worker_count"], 2)
        self.assertIn("queue pressure is low", status["recommendation_reason"])

    def test_idle_workers_do_not_cause_an_increase(self):
        pool = Pool(metrics={
            "sample_count": 5,
            "average_queue_wait_ms": 500.0,
            "average_inference_ms": 600.0,
            "average_busy_ratio": 0.25,
            "lower_pool_inference_ms": None,
            "lower_pool_queue_wait_ms": None,
        })
        status = self.service(resources(), pool).status()
        self.assertEqual(status["recommended_worker_count"], 2)
        self.assertIn("frequently idle", status["recommendation_reason"])

    def test_sustained_cpu_and_slow_inference_can_reduce_one_step(self):
        pool = Pool(size=3, metrics={
            "sample_count": 6,
            "average_queue_wait_ms": 400.0,
            "average_inference_ms": 1900.0,
            "average_busy_ratio": 1.0,
            "lower_pool_inference_ms": 900.0,
            "lower_pool_queue_wait_ms": 450.0,
        })
        current_time = [0.0]
        service = self.service(
            resources(cpu=95),
            pool,
            clock=lambda: current_time[0],
        )
        service.status()
        current_time[0] = 8.0
        service.status()
        current_time[0] = 16.0
        status = service.status()
        self.assertEqual(status["recommended_worker_count"], 2)
        self.assertIn("contention", status["recommendation_reason"])

    def test_recommendation_never_exceeds_hardware_ceiling(self):
        pool = Pool(size=3)
        status = self.service(resources(physical=4), pool).status()
        self.assertEqual(status["hardware_worker_ceiling"], 3)
        self.assertEqual(status["recommended_worker_count"], 3)

    def test_applying_recommendation_uses_save_then_existing_restart_flow(self):
        pool = Pool(size=2)
        service = self.service(resources(), pool)
        recommendation = service.status()["recommended_worker_count"]
        saved = service.update({"worker_pool_size": recommendation})
        self.assertTrue(saved["restart_required"])
        self.assertEqual(self.storage.load(), 3)
        restarted = service.restart()
        self.assertFalse(restarted["restart_required"])
        self.assertEqual(restarted["pool_size"], 3)


if __name__ == "__main__":
    unittest.main()
