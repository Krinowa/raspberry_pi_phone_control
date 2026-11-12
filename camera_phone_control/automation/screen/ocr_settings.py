"""Hardware-aware configuration and recommendations for the PaddleOCR pool."""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

LOG = logging.getLogger("ocr_settings")

DEFAULT_OCR_SETTINGS_PATH = (
    Path(__file__).resolve().parents[1] / "camera_data" / "ocr_settings.json"
)
DEFAULT_POOL_SIZE = 2
MIN_POOL_SIZE = 1
ABSOLUTE_POOL_SIZE_CAP = 8
MAX_STORED_POOL_SIZE = 64
DEFAULT_CPU_CAPACITY_PERCENT = 80
MIN_CPU_CAPACITY_PERCENT = 25
MAX_CPU_CAPACITY_PERCENT = 90
DEFAULT_WORKER_MEMORY_BYTES = 900 * 1024 * 1024
MIN_SYSTEM_MEMORY_RESERVE_BYTES = 2 * 1024 * 1024 * 1024
MIN_LIVE_MEMORY_RESERVE_BYTES = 1024 * 1024 * 1024
METRICS_WINDOW_SECONDS = 30.0


def validate_pool_size(value, maximum=ABSOLUTE_POOL_SIZE_CAP):
    if type(value) is not int or not MIN_POOL_SIZE <= value <= maximum:
        raise ValueError(f"worker_pool_size must be an integer from 1 to {maximum}.")
    return value


def _validate_stored_pool_size(value):
    # Read a previously saved value without silently rewriting it. Startup
    # separately clamps it to the locally detected safe ceiling.
    return validate_pool_size(value, MAX_STORED_POOL_SIZE)


def _cpu_capacity_percent(environment):
    raw = str(
        environment.get("OCR_CPU_CAPACITY_PERCENT", DEFAULT_CPU_CAPACITY_PERCENT)
    ).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            "OCR_CPU_CAPACITY_PERCENT must be an integer from 25 to 90."
        ) from exc
    if not MIN_CPU_CAPACITY_PERCENT <= value <= MAX_CPU_CAPACITY_PERCENT:
        raise ValueError("OCR_CPU_CAPACITY_PERCENT must be an integer from 25 to 90.")
    return value


def detect_hardware_resources():
    """Return a cross-platform resource snapshot, preferring psutil."""
    logical = os.cpu_count() or 1
    physical = None
    total_ram = available_ram = 0
    cpu_percent = ram_percent = 0.0
    try:
        import psutil

        physical = psutil.cpu_count(logical=False)
        logical = psutil.cpu_count(logical=True) or logical
        memory = psutil.virtual_memory()
        total_ram = int(memory.total)
        available_ram = int(memory.available)
        ram_percent = float(memory.percent)
        cpu_percent = float(psutil.cpu_percent(interval=None))
    except (ImportError, OSError, RuntimeError, ValueError):
        try:
            total_ram = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
            available_ram = int(
                os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES")
            )
            if total_ram:
                ram_percent = 100.0 * (1.0 - available_ram / total_ram)
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    fallback = not isinstance(physical, int) or physical < 1
    if fallback:
        physical = logical
    return {
        "physical_cpu_cores": max(1, int(physical)),
        "logical_cpu_threads": max(1, int(logical)),
        "physical_cpu_fallback": fallback,
        "total_ram_bytes": max(0, total_ram),
        "available_ram_bytes": max(0, available_ram),
        "current_system_cpu_percent": max(0.0, min(100.0, cpu_percent)),
        "current_system_ram_percent": max(0.0, min(100.0, ram_percent)),
    }


def calculate_hardware_capacity(
    resources,
    cpu_capacity_percent=DEFAULT_CPU_CAPACITY_PERCENT,
    worker_memory_bytes=DEFAULT_WORKER_MEMORY_BYTES,
    existing_worker_count=0,
):
    """Calculate conservative CPU, RAM, and final pool ceilings."""
    physical = max(1, int(resources["physical_cpu_cores"]))
    cpu_ceiling = max(1, math.floor(physical * cpu_capacity_percent / 100.0))

    total = max(0, int(resources.get("total_ram_bytes", 0)))
    available = max(0, int(resources.get("available_ram_bytes", 0)))
    worker_memory_bytes = max(1, int(worker_memory_bytes))
    if not total or not available:
        # Unknown memory must not disable OCR. One worker is the only safe
        # assumption until reliable memory data becomes available.
        ram_ceiling = 1
    else:
        system_reserve = max(
            MIN_SYSTEM_MEMORY_RESERVE_BYTES,
            int(total * 0.25),
        )
        total_budget = max(0, total - system_reserve)
        # Available RAM already excludes running Paddle workers. Add their
        # observed/estimated footprint back when calculating a total pool
        # ceiling, then retain a live reserve for non-OCR work.
        live_budget = max(
            0,
            available
            + max(0, int(existing_worker_count)) * worker_memory_bytes
            - MIN_LIVE_MEMORY_RESERVE_BYTES,
        )
        usable = min(total_budget, live_budget)
        ram_ceiling = max(1, usable // worker_memory_bytes)

    return {
        "cpu_worker_ceiling": min(cpu_ceiling, ABSOLUTE_POOL_SIZE_CAP),
        "ram_worker_ceiling": min(int(ram_ceiling), ABSOLUTE_POOL_SIZE_CAP),
        "hardware_worker_ceiling": max(
            1,
            min(cpu_ceiling, int(ram_ceiling), ABSOLUTE_POOL_SIZE_CAP),
        ),
        "estimated_worker_memory_bytes": worker_memory_bytes,
        "absolute_worker_safety_cap": ABSOLUTE_POOL_SIZE_CAP,
    }


class OCRSettingsStorage:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_OCR_SETTINGS_PATH)
        self.lock = threading.RLock()

    def load(self):
        with self.lock:
            if not self.path.exists():
                return None
            if self.path.is_symlink():
                raise OSError("OCR settings path cannot be a symlink.")
            if self.path.stat().st_size > 16 * 1024:
                raise ValueError("OCR settings file is unexpectedly large.")
            with self.path.open(encoding="utf-8") as stream:
                data = json.load(stream)
        if not isinstance(data, dict) or set(data) != {"worker_pool_size"}:
            raise ValueError("Stored OCR settings are invalid.")
        return _validate_stored_pool_size(data["worker_pool_size"])

    def save(self, worker_pool_size):
        worker_pool_size = validate_pool_size(worker_pool_size)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise OSError("OCR settings path cannot be a symlink.")
            fd, temporary = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=".ocr-settings-",
                suffix=".tmp",
            )
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(
                        {"worker_pool_size": worker_pool_size},
                        stream,
                        indent=2,
                    )
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


class OCRPoolBusy(RuntimeError):
    pass


class OCRSettingsService:
    def __init__(
        self,
        storage=None,
        *,
        environment=None,
        resource_provider=None,
        clock=None,
    ):
        self.storage = storage or OCRSettingsStorage()
        self.environment = os.environ if environment is None else environment
        self.resource_provider = resource_provider or detect_hardware_resources
        self.clock = clock or time.monotonic
        self.lock = threading.RLock()
        self.resource_history = deque(maxlen=64)
        self.cpu_capacity_percent = _cpu_capacity_percent(self.environment)
        self.initial_resources = self.resource_provider()
        initial_capacity = calculate_hardware_capacity(
            self.initial_resources,
            self.cpu_capacity_percent,
        )
        self.initial_hardware_ceiling = initial_capacity["hardware_worker_ceiling"]

        persisted = self.storage.load()
        if persisted is None:
            raw = str(
                self.environment.get(
                    "CAMERA_OCR_WORKER_POOL_SIZE",
                    DEFAULT_POOL_SIZE,
                )
            ).strip()
            try:
                configured = int(raw)
            except ValueError as exc:
                raise ValueError(
                    "CAMERA_OCR_WORKER_POOL_SIZE must be a positive integer."
                ) from exc
            configured = _validate_stored_pool_size(configured)
            self.source = (
                "environment"
                if "CAMERA_OCR_WORKER_POOL_SIZE" in self.environment
                else "default"
            )
        else:
            configured = persisted
            self.source = "persisted"

        self.stored_worker_pool_size = configured
        self.worker_pool_size = min(configured, self.initial_hardware_ceiling)
        self.startup_clamped = configured != self.worker_pool_size
        self.startup_warning = None
        if self.startup_clamped:
            self.startup_warning = (
                f"Configured OCR worker pool size {configured} exceeds the detected "
                f"safe ceiling {self.initial_hardware_ceiling}; started "
                f"{self.worker_pool_size} worker(s). The stored setting was not changed."
            )
            LOG.warning(self.startup_warning)
        self.pool = None

    def attach(self, pool):
        with self.lock:
            self.pool = pool

    def _resource_snapshot(self):
        resources = self.resource_provider()
        now = self.clock()
        with self.lock:
            self.resource_history.append((now, resources))
            cutoff = now - METRICS_WINDOW_SECONDS
            while self.resource_history and self.resource_history[0][0] < cutoff:
                self.resource_history.popleft()
            recent = [item[1] for item in self.resource_history]
            resource_window_seconds = (
                now - self.resource_history[0][0] if self.resource_history else 0.0
            )
        resources = dict(resources)
        resources["average_system_cpu_percent"] = sum(
            item["current_system_cpu_percent"] for item in recent
        ) / len(recent)
        resources["average_system_ram_percent"] = sum(
            item["current_system_ram_percent"] for item in recent
        ) / len(recent)
        resources["resource_sample_count"] = len(recent)
        resources["resource_window_seconds"] = resource_window_seconds
        return resources

    @staticmethod
    def _pool_metrics(pool):
        if pool is None or not hasattr(pool, "recent_metrics"):
            return {
                "sample_count": 0,
                "average_queue_wait_ms": 0.0,
                "average_inference_ms": 0.0,
                "average_busy_ratio": 0.0,
                "lower_pool_inference_ms": None,
                "lower_pool_queue_wait_ms": None,
            }
        return pool.recent_metrics(METRICS_WINDOW_SECONDS)

    @staticmethod
    def _worker_memory(pool_status):
        observed = pool_status.get("observed_worker_rss_bytes")
        if isinstance(observed, (int, float)) and observed > 0:
            return int(observed), "observed"
        return DEFAULT_WORKER_MEMORY_BYTES, "estimated"

    def _recommend(self, status, resources, metrics, capacity):
        current = max(1, int(status.get("pool_size") or self.worker_pool_size))
        ceiling = capacity["hardware_worker_ceiling"]
        cpu = resources["current_system_cpu_percent"]
        average_cpu = resources["average_system_cpu_percent"]
        ram = resources["current_system_ram_percent"]
        available = resources["available_ram_bytes"]
        queue_wait = metrics["average_queue_wait_ms"]
        inference = metrics["average_inference_ms"]
        samples = metrics["sample_count"]
        busy_ratio = metrics["average_busy_ratio"]
        queued = int(status.get("queued_requests", 0))
        worker_memory = capacity["estimated_worker_memory_bytes"]

        if current > ceiling:
            return ceiling, "Current pool exceeds the detected hardware safety ceiling."

        severe_cpu = (
            resources["resource_sample_count"] >= 3
            and resources["resource_window_seconds"] >= 15.0
            and average_cpu >= 90.0
        )
        if severe_cpu and inference >= 1500.0 and current > 1:
            return current - 1, (
                "Sustained CPU saturation and slow OCR inference indicate contention."
            )

        if current >= ceiling:
            return current, "Current workers already match the hardware safety ceiling."
        if cpu > 80.0:
            return current, "CPU headroom is too low to add another OCR worker."
        if ram >= 85.0 or available < worker_memory + MIN_LIVE_MEMORY_RESERVE_BYTES:
            return current, "Memory headroom is too low to add another OCR worker."
        if samples < 3:
            return current, "Collecting recent OCR performance samples."
        if queue_wait < 250.0 and queued == 0:
            return current, "OCR queue pressure is low."
        if busy_ratio < 0.65 and queued == 0:
            return current, "Workers are frequently idle despite recent queue delay."

        lower_inference = metrics.get("lower_pool_inference_ms")
        lower_queue = metrics.get("lower_pool_queue_wait_ms")
        if (
            lower_inference
            and inference > lower_inference * 1.35
            and (not lower_queue or queue_wait >= lower_queue * 0.8)
        ):
            return current, (
                "Inference slowed after the last increase without enough queue improvement."
            )

        if cpu < 70.0:
            return min(current + 1, ceiling), (
                "Sustained OCR queue wait has available CPU and memory headroom."
            )
        return current, "CPU usage is elevated; keep the current worker count."

    def _status_data(self):
        with self.lock:
            pool = self.pool
        pool_status = (
            pool.status()
            if pool is not None
            else {
                "pool_size": 0,
                "busy_workers": 0,
                "queued_requests": 0,
                "workers": [],
                "accepting": False,
            }
        )
        resources = self._resource_snapshot()
        worker_memory, memory_source = self._worker_memory(pool_status)
        capacity = calculate_hardware_capacity(
            resources,
            self.cpu_capacity_percent,
            worker_memory,
            pool_status.get("pool_size", 0),
        )
        metrics = self._pool_metrics(pool)
        recommendation, reason = self._recommend(
            pool_status,
            resources,
            metrics,
            capacity,
        )
        return (
            pool_status,
            resources,
            capacity,
            metrics,
            recommendation,
            reason,
            memory_source,
        )

    def settings(self):
        pool_status, _, capacity, _, recommendation, reason, memory_source = (
            self._status_data()
        )
        active_size = pool_status["pool_size"] or None
        return {
            "worker_pool_size": self.worker_pool_size,
            "stored_worker_pool_size": self.stored_worker_pool_size,
            "source": self.source,
            "maximum_worker_pool_size": capacity["hardware_worker_ceiling"],
            "restart_required": (
                active_size is not None and active_size != self.worker_pool_size
            ),
            "startup_clamped": self.startup_clamped,
            "warning": self.startup_warning,
            "recommended_worker_count": recommendation,
            "recommendation_reason": reason,
            "worker_memory_source": memory_source,
        }

    def update(self, data):
        if not isinstance(data, dict) or set(data) != {"worker_pool_size"}:
            raise ValueError("Expected worker_pool_size only.")
        resources = self._resource_snapshot()
        with self.lock:
            pool = self.pool
        pool_status = pool.status() if pool is not None else {}
        worker_memory, _ = self._worker_memory(pool_status)
        maximum = calculate_hardware_capacity(
            resources,
            self.cpu_capacity_percent,
            worker_memory,
            pool_status.get("pool_size", 0),
        )["hardware_worker_ceiling"]
        size = validate_pool_size(data["worker_pool_size"], maximum)
        self.storage.save(size)
        with self.lock:
            self.worker_pool_size = size
            self.stored_worker_pool_size = size
            self.source = "persisted"
            self.startup_clamped = False
            self.startup_warning = None
        return self.settings()

    def status(self):
        (
            pool_status,
            resources,
            capacity,
            metrics,
            recommendation,
            reason,
            memory_source,
        ) = self._status_data()
        pool_status.update(
            configured_pool_size=self.worker_pool_size,
            stored_worker_pool_size=self.stored_worker_pool_size,
            restart_required=(pool_status["pool_size"] != self.worker_pool_size),
            physical_cpu_cores=resources["physical_cpu_cores"],
            logical_cpu_threads=resources["logical_cpu_threads"],
            physical_cpu_fallback=resources["physical_cpu_fallback"],
            cpu_capacity_percent=self.cpu_capacity_percent,
            cpu_worker_ceiling=capacity["cpu_worker_ceiling"],
            ram_worker_ceiling=capacity["ram_worker_ceiling"],
            hardware_worker_ceiling=capacity["hardware_worker_ceiling"],
            absolute_worker_safety_cap=capacity["absolute_worker_safety_cap"],
            current_worker_count=pool_status["pool_size"],
            recommended_worker_count=recommendation,
            recommendation_reason=reason,
            current_cpu_percent=resources["current_system_cpu_percent"],
            current_ram_percent=resources["current_system_ram_percent"],
            total_ram_bytes=resources["total_ram_bytes"],
            available_ram_bytes=resources["available_ram_bytes"],
            average_queue_wait_ms=metrics["average_queue_wait_ms"],
            average_inference_ms=metrics["average_inference_ms"],
            metrics_sample_count=metrics["sample_count"],
            observed_worker_rss_bytes=pool_status.get("observed_worker_rss_bytes"),
            estimated_worker_memory_bytes=capacity["estimated_worker_memory_bytes"],
            worker_memory_source=memory_source,
            startup_clamped=self.startup_clamped,
            warning=self.startup_warning,
        )
        return pool_status

    def restart(self):
        with self.lock:
            pool = self.pool
            size = self.worker_pool_size
        if pool is None:
            raise RuntimeError("PaddleOCR worker pool is unavailable.")
        if pool.has_active_requests():
            raise OCRPoolBusy(
                "OCR workers are busy. Wait for active OCR requests to finish."
            )
        pool.restart(size)
        return self.status()
