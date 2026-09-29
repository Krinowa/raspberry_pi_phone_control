"""PaddleOCR-backed semantic text detection for calibrated camera images."""

import copy
import itertools
import json
import logging
import math
import multiprocessing
import os
from pathlib import Path
import resource
import threading
import time
import uuid
from collections import deque

import cv2
import numpy as np

from .state import ScreenStateError, TextDetectionUnavailable
from ..workflows.timing import measure_timing, record_timing

LOG = logging.getLogger("camera_ocr")


def _build_paddle_engine(
    language,
    textline_orientation,
    cpu_threads,
    recognition_batch_size=None,
):
    """Construct Paddle only in the OCR child process."""
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise TextDetectionUnavailable(
            "Camera OCR requires PaddleOCR and PaddlePaddle. "
            "Install the camera OCR dependencies and restart the server."
        ) from exc

    options = dict(
        device="cpu",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=textline_orientation,
        cpu_threads=cpu_threads,
    )
    if recognition_batch_size is not None:
        options["text_recognition_batch_size"] = recognition_batch_size
    if language == "en":
        options.update(
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="en_PP-OCRv5_mobile_rec",
        )
    else:
        options.update(lang=language, ocr_version="PP-OCRv5")
    return PaddleOCR(**options)


def _plain_result(result):
    """Convert Paddle result objects into small, pickle-safe dictionaries."""
    if isinstance(result, dict):
        data = result
    else:
        data = getattr(result, "json", None)
        if callable(data):
            data = data()
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                data = None
        if not isinstance(data, dict):
            try:
                data = dict(result)
            except (TypeError, ValueError):
                data = {}
    nested = data.get("res") if isinstance(data, dict) else None
    source = nested if isinstance(nested, dict) else data
    source = source if isinstance(source, dict) else {}
    boxes = source.get("rec_boxes")
    if boxes is None:
        boxes = source.get("rec_polys")
    return {
        "res": {
            "rec_texts": [str(value) for value in source.get("rec_texts", [])],
            "rec_scores": np.asarray(source.get("rec_scores", [])).tolist(),
            "rec_boxes": [] if boxes is None else np.asarray(boxes).tolist(),
        }
    }


def _paddle_worker(connection, config):
    """Persistent child entry point. The model is initialized exactly once."""
    worker_pid = os.getpid()
    try:
        started = time.monotonic()
        engine = _build_paddle_engine(
            config["language"],
            config["textline_orientation"],
            config["cpu_threads"],
            config.get("recognition_batch_size"),
        )
        connection.send(("ready", worker_pid, time.monotonic() - started))
    except BaseException as exc:
        try:
            connection.send(
                ("startup_error", worker_pid, type(exc).__name__, str(exc)[:500])
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
        connection.close()
        return

    try:
        while True:
            try:
                message = connection.recv()
            except (EOFError, OSError):
                break
            if not message:
                continue
            if message[0] == "shutdown":
                break
            if message[0] != "ocr":
                continue
            _, request_id, image = message
            try:
                usage_before = resource.getrusage(resource.RUSAGE_SELF)
                try:
                    import psutil

                    native_threads = psutil.Process().num_threads()
                except (ImportError, OSError):
                    native_threads = None
                started = time.monotonic()
                results = [_plain_result(item) for item in engine.predict(input=image)]
                inference = time.monotonic() - started
                usage_after = resource.getrusage(resource.RUSAGE_SELF)
                connection.send(
                    (
                        "result",
                        request_id,
                        results,
                        inference,
                        worker_pid,
                        {
                            "cpu_user": usage_after.ru_utime - usage_before.ru_utime,
                            "cpu_system": usage_after.ru_stime - usage_before.ru_stime,
                            "native_threads": native_threads,
                            "input_width": image.shape[1],
                            "input_height": image.shape[0],
                        },
                    )
                )
            except BaseException as exc:
                try:
                    connection.send(
                        (
                            "error",
                            request_id,
                            type(exc).__name__,
                            str(exc)[:500],
                            worker_pid,
                        )
                    )
                except (BrokenPipeError, EOFError, OSError):
                    break
    finally:
        connection.close()


class _PaddleWorkerClient:
    """Own one persistent spawned Paddle process and its request pipe."""

    def __init__(self, config, timeout, *, context=None, target=None):
        self.config = dict(config)
        self.timeout = float(timeout)
        self.context = context or multiprocessing.get_context("spawn")
        self.target = target or _paddle_worker
        self._connection = None
        self._process = None
        self._lock = threading.Lock()
        self.initialization_seconds = None

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    def _receive(self, deadline, operation):
        while True:
            process, connection = self._process, self._connection
            if process is None or connection is None:
                raise ScreenStateError("PaddleOCR worker is unavailable.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._stop(force=True)
                raise ScreenStateError(
                    f"PaddleOCR worker timed out during {operation}."
                )
            try:
                if connection.poll(min(0.05, remaining)):
                    return connection.recv()
            except (BrokenPipeError, EOFError, OSError) as exc:
                self._stop(force=True)
                raise ScreenStateError(
                    "PaddleOCR worker stopped unexpectedly."
                ) from exc
            if not process.is_alive():
                exitcode = process.exitcode
                self._stop(force=True)
                raise ScreenStateError(
                    f"PaddleOCR worker stopped unexpectedly (exit code {exitcode})."
                )

    def _start(self):
        if self._process is not None and self._process.is_alive():
            return 0.0
        self._stop(force=True)
        parent, child = self.context.Pipe(duplex=True)
        process = self.context.Process(
            target=self.target,
            args=(child, self.config),
            name="camera-paddle-ocr",
            daemon=True,
        )
        started = time.monotonic()
        try:
            process.start()
            self._connection, self._process = parent, process
            child.close()
            message = self._receive(started + self.timeout, "startup")
        except Exception:
            if self._process is None:
                parent.close()
                try:
                    process.close()
                except ValueError:
                    pass
            try:
                child.close()
            except OSError:
                pass
            self._stop(force=True)
            raise
        if message[0] == "startup_error":
            _, pid, error_type, detail = message
            self._stop(force=True)
            raise TextDetectionUnavailable(
                f"PaddleOCR worker {pid} could not initialize: "
                f"{error_type}: {detail}"
            )
        if message[0] != "ready":
            self._stop(force=True)
            raise ScreenStateError(
                "PaddleOCR worker returned an invalid startup response."
            )
        _, pid, self.initialization_seconds = message
        LOG.info(
            "PaddleOCR worker ready pid=%d main_pid=%d model_init=%.2fs "
            "language=%s textline_orientation=%s cpu_threads=%d rec_batch=%s",
            pid,
            os.getpid(),
            self.initialization_seconds,
            self.config["language"],
            self.config["textline_orientation"],
            self.config["cpu_threads"],
            self.config.get("recognition_batch_size"),
        )
        return time.monotonic() - started

    def request(self, image, *, request_id=None):
        queued_at = time.monotonic()
        with self._lock:
            queue_wait = time.monotonic() - queued_at
            startup = self._start()
            request_id = request_id or uuid.uuid4().hex
            send_started = time.monotonic()
            try:
                self._connection.send(("ocr", request_id, image))
            except (BrokenPipeError, EOFError, OSError) as exc:
                self._stop(force=True)
                raise ScreenStateError(
                    "PaddleOCR worker stopped before accepting the image."
                ) from exc
            send_seconds = time.monotonic() - send_started
            wait_started = time.monotonic()
            message = self._receive(wait_started + self.timeout, "inference")
            wait_seconds = time.monotonic() - wait_started
            if len(message) < 2 or message[1] != request_id:
                self._stop(force=True)
                raise ScreenStateError(
                    "PaddleOCR worker returned a mismatched response."
                )
            if message[0] == "error":
                _, _, error_type, detail, pid = message
                raise ScreenStateError(
                    f"PaddleOCR worker {pid} failed: {error_type}: {detail}"
                )
            if message[0] != "result":
                self._stop(force=True)
                raise ScreenStateError("PaddleOCR worker returned an invalid response.")
            _, _, results, inference, pid, *extra = message
            diagnostics = extra[0] if extra and isinstance(extra[0], dict) else {}
            return results, {
                "queue_wait": queue_wait,
                "worker_start": startup,
                "ipc_send": send_seconds,
                "ipc_wait": wait_seconds,
                "inference": inference,
                "worker_pid": pid,
                **diagnostics,
            }

    def start(self):
        """Initialize the persistent model process without running inference."""
        with self._lock:
            return self._start()

    def _stop(self, *, force):
        process, connection = self._process, self._connection
        self._process = self._connection = None
        if connection is not None:
            if not force and process is not None and process.is_alive():
                try:
                    connection.send(("shutdown",))
                except (BrokenPipeError, EOFError, OSError):
                    pass
            try:
                connection.close()
            except OSError:
                pass
        if process is not None:
            process.join(2.0 if not force else 0.2)
            if process.is_alive():
                process.terminate()
                process.join(1.0)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(1.0)
            try:
                process.close()
            except (ValueError, AttributeError):
                pass

    def close(self):
        with self._lock:
            self._stop(force=False)


class _PaddleWorkerPool:
    """Fair dispatcher over independent persistent Paddle worker processes."""

    def __init__(
        self,
        config,
        timeout,
        pool_size=2,
        *,
        client_factory=None,
    ):
        from .ocr_settings import validate_pool_size

        self.config = dict(config)
        self.timeout = float(timeout)
        self.pool_size = validate_pool_size(pool_size)
        self.client_factory = client_factory or self._new_client
        self.condition = threading.Condition(threading.RLock())
        self.queue = []
        self.slots = []
        self.accepting = True
        self.restarting = False
        self._next_worker = 0
        self.metrics = deque(maxlen=256)
        self._build_slots(self.pool_size)

    def _new_client(self, worker_id):
        return _PaddleWorkerClient(self.config, self.timeout)

    def _build_slots(self, size):
        self.slots = [
            {
                "worker_id": index + 1,
                "client": self.client_factory(index + 1),
                "state": "STARTING",
                "failures": 0,
                "retry_at": 0.0,
            }
            for index in range(size)
        ]
        self.pool_size = size
        self._next_worker = 0

    @property
    def pid(self):
        with self.condition:
            return next(
                (slot["client"].pid for slot in self.slots if slot["client"].pid),
                None,
            )

    def _available_slot(self, current):
        for offset in range(self.pool_size):
            index = (self._next_worker + offset) % self.pool_size
            slot = self.slots[index]
            if slot["state"] in {"IDLE", "STARTING"}:
                self._next_worker = (index + 1) % self.pool_size
                return slot
            if slot["state"] == "FAILED" and current >= slot["retry_at"]:
                try:
                    slot["client"].close()
                except Exception:
                    pass
                slot["client"] = self.client_factory(slot["worker_id"])
                slot["state"] = "STARTING"
                self._next_worker = (index + 1) % self.pool_size
                return slot
        return None

    def start(self):
        """Warm every model once; failed slots remain recoverable with backoff."""
        with self.condition:
            if self.has_active_requests():
                return self.status()
            self.restarting = True
        try:
            errors = []
            for slot in self.slots:
                with self.condition:
                    if slot["state"] == "IDLE":
                        continue
                    slot["state"] = "STARTING"
                try:
                    slot["client"].start()
                except Exception as exc:
                    errors.append(exc)
                    self._mark_failed(slot)
                else:
                    with self.condition:
                        slot["state"] = "IDLE"
                        slot["failures"] = 0
                        self.condition.notify_all()
            if errors and len(errors) == len(self.slots):
                raise errors[0]
        finally:
            with self.condition:
                self.restarting = False
                self.condition.notify_all()
        return self.status()

    def _mark_failed(self, slot):
        with self.condition:
            slot["failures"] += 1
            # Fast first recovery, then bounded exponential backoff. Persistent
            # failures remain visible and are retried only when work arrives.
            delay = min(30.0, 0.25 * (2 ** min(slot["failures"] - 1, 7)))
            slot["retry_at"] = time.monotonic() + delay
            slot["state"] = "FAILED"
            self.condition.notify_all()

    def request(self, image):
        submitted = time.monotonic()
        request_id = uuid.uuid4().hex
        deadline = submitted + self.timeout
        with self.condition:
            if not self.accepting:
                raise ScreenStateError("PaddleOCR worker pool is shutting down.")
            self.queue.append(request_id)
            slot = None
            while slot is None:
                if not self.accepting:
                    if request_id in self.queue:
                        self.queue.remove(request_id)
                    self.condition.notify_all()
                    raise ScreenStateError("PaddleOCR worker pool is shutting down.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if request_id in self.queue:
                        self.queue.remove(request_id)
                    self.condition.notify_all()
                    raise ScreenStateError(
                        "PaddleOCR worker pool timed out waiting for an available worker."
                    )
                if not self.restarting and self.queue and self.queue[0] == request_id:
                    slot = self._available_slot(time.monotonic())
                    if slot is not None:
                        self.queue.pop(0)
                        slot["state"] = "BUSY"
                        busy_at_dispatch = sum(
                            item["state"] == "BUSY" for item in self.slots
                        )
                        queued_at_dispatch = len(self.queue)
                        break
                retry_delays = [
                    item["retry_at"] - time.monotonic()
                    for item in self.slots
                    if item["state"] == "FAILED" and item["retry_at"] > time.monotonic()
                ]
                wait_for = min(0.05, remaining)
                if retry_delays:
                    wait_for = min(wait_for, max(0.001, min(retry_delays)))
                self.condition.wait(wait_for)

        dispatcher_queue_wait = time.monotonic() - submitted
        total_started = submitted
        healthy = False
        try:
            results, timings = slot["client"].request(
                image,
                request_id=request_id,
            )
            healthy = True
            # Include lazy/recovery startup in queue delay because inference
            # cannot begin until that worker's persistent model is ready.
            queue_wait = (
                dispatcher_queue_wait
                + timings.get("queue_wait", 0.0)
                + timings.get("worker_start", 0.0)
            )
            total_ocr = time.monotonic() - total_started
            with self.condition:
                self.metrics.append(
                    {
                        "timestamp": time.monotonic(),
                        "pool_size": self.pool_size,
                        "queue_wait_ms": queue_wait * 1000.0,
                        "inference_ms": timings.get("inference", 0.0) * 1000.0,
                        "busy_ratio": busy_at_dispatch / self.pool_size,
                    }
                )
            return results, {
                **timings,
                "request_id": request_id,
                "worker_id": slot["worker_id"],
                "dispatcher_queue_wait": dispatcher_queue_wait,
                "queue_wait": queue_wait,
                "queue_wait_ms": queue_wait * 1000.0,
                "inference_ms": timings.get("inference", 0.0) * 1000.0,
                "total_ocr": total_ocr,
                "total_ocr_ms": total_ocr * 1000.0,
                "pool_size": self.pool_size,
                "busy_workers": busy_at_dispatch,
                "queued_requests": queued_at_dispatch,
            }
        finally:
            if healthy or slot["client"].pid is not None:
                with self.condition:
                    slot["state"] = "IDLE"
                    if healthy:
                        slot["failures"] = 0
                    self.condition.notify_all()
            else:
                self._mark_failed(slot)

    def has_active_requests(self):
        with self.condition:
            return bool(self.queue) or any(
                slot["state"] == "BUSY" for slot in self.slots
            )

    def status(self):
        with self.condition:
            status = {
                "pool_size": self.pool_size,
                "busy_workers": sum(slot["state"] == "BUSY" for slot in self.slots),
                "queued_requests": len(self.queue),
                "accepting": self.accepting,
                "restarting": self.restarting,
                "workers": [
                    {
                        "worker_id": slot["worker_id"],
                        "state": slot["state"],
                        "pid": slot["client"].pid,
                    }
                    for slot in self.slots
                ],
            }
        rss_values = []
        try:
            import psutil

            for worker in status["workers"]:
                if worker["pid"] is None:
                    continue
                try:
                    rss = int(psutil.Process(worker["pid"]).memory_info().rss)
                except (OSError, psutil.Error):
                    continue
                worker["rss_bytes"] = rss
                rss_values.append(rss)
        except ImportError:
            pass
        status["observed_worker_rss_bytes"] = (
            round(sum(rss_values) / len(rss_values)) if rss_values else None
        )
        return status

    def recent_metrics(self, window_seconds=30.0):
        """Return bounded recent metrics used by the recommendation service."""
        cutoff = time.monotonic() - float(window_seconds)
        with self.condition:
            records = [item for item in self.metrics if item["timestamp"] >= cutoff]
            current_size = self.pool_size
        current = [item for item in records if item["pool_size"] == current_size]
        lower = [item for item in records if item["pool_size"] < current_size]

        def average(items, name):
            if not items:
                return None
            return sum(item[name] for item in items) / len(items)

        return {
            "sample_count": len(current),
            "average_queue_wait_ms": average(current, "queue_wait_ms") or 0.0,
            "average_inference_ms": average(current, "inference_ms") or 0.0,
            "average_busy_ratio": average(current, "busy_ratio") or 0.0,
            "lower_pool_inference_ms": average(lower, "inference_ms"),
            "lower_pool_queue_wait_ms": average(lower, "queue_wait_ms"),
        }

    def restart(self, pool_size):
        from .ocr_settings import OCRPoolBusy, validate_pool_size

        pool_size = validate_pool_size(pool_size)
        with self.condition:
            if self.has_active_requests():
                raise OCRPoolBusy(
                    "OCR workers are busy. Wait for active OCR requests to finish."
                )
            self.restarting = True
            old_slots = self.slots
        try:
            for slot in old_slots:
                slot["client"].close()
            with self.condition:
                self._build_slots(pool_size)
            errors = []
            for slot in self.slots:
                try:
                    slot["client"].start()
                except Exception as exc:
                    errors.append(exc)
                    self._mark_failed(slot)
                else:
                    with self.condition:
                        slot["state"] = "IDLE"
                        self.condition.notify_all()
            if errors and len(errors) == len(self.slots):
                raise errors[0]
        finally:
            with self.condition:
                self.restarting = False
                self.condition.notify_all()
        return self.status()

    def close(self):
        with self.condition:
            self.accepting = False
            while any(slot["state"] == "BUSY" for slot in self.slots):
                self.condition.wait(0.05)
            slots = list(self.slots)
            self.condition.notify_all()
        for slot in slots:
            slot["client"].close()


class CameraOCRScreenStateProvider:
    """Read phone UI text through a shared persistent PaddleOCR process pool."""

    def __init__(
        self,
        external_screen,
        *,
        language="en",
        timeout=30.0,
        worker_pool_size=2,
        _shared_worker=None,
    ):
        self.external_screen = external_screen
        self.language = language
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("Camera OCR timeout must be positive.")
        self.debug_dir = os.environ.get("CAMERA_OCR_DEBUG_DIR", "").strip()
        self.debug_logging = self._environment_flag("CAMERA_OCR_DEBUG") or bool(
            self.debug_dir
        )
        self.textline_orientation = self._environment_flag(
            "CAMERA_OCR_TEXTLINE_ORIENTATION"
        )
        self.scale = self._configured_scale()
        self.cpu_threads = self._configured_cpu_threads()
        self.recognition_batch_size = self._configured_recognition_batch_size()
        self.cache_threshold = self._configured_cache_threshold()
        self.change_threshold = self._configured_change_threshold()
        self.change_ratio_threshold = self._configured_change_ratio_threshold()
        self.forced_ocr_interval = self._configured_forced_interval()
        self.poll_interval = self._configured_poll_interval()
        self.settle_seconds = self._configured_settle_seconds()
        self._debug_sequence = itertools.count(1)
        self._cache_by_scope = {}
        self._change_baseline = self._change_candidate = None
        self._change_candidate_since = None
        self._inference_lock = threading.Lock()
        self._owns_worker = _shared_worker is None
        self._worker = _shared_worker or _PaddleWorkerPool(
            {
                "language": self.language,
                "textline_orientation": self.textline_orientation,
                "cpu_threads": self.cpu_threads,
                "recognition_batch_size": self.recognition_batch_size,
            },
            self.timeout,
            worker_pool_size,
        )

    def for_screen(self, external_screen):
        """Create an isolated per-screen cache while sharing Paddle inference."""
        return type(self)(
            external_screen,
            language=self.language,
            timeout=self.timeout,
            worker_pool_size=self._worker.pool_size,
            _shared_worker=self._worker,
        )

    @staticmethod
    def _environment_flag(name):
        return os.environ.get(name, "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _configured_scale():
        value = os.environ.get("CAMERA_OCR_SCALE", "1.0").strip()
        try:
            scale = float(value)
        except ValueError:
            scale = 1.0
        if scale not in {1.0, 1.25, 1.5}:
            LOG.warning("Unsupported CAMERA_OCR_SCALE=%r; using 1.0.", value)
            return 1.0
        return scale

    @staticmethod
    def _configured_cpu_threads():
        value = os.environ.get("CAMERA_OCR_CPU_THREADS", "1").strip()
        try:
            threads = int(value)
        except ValueError:
            threads = 1
        if not 1 <= threads <= 16:
            LOG.warning("Unsupported CAMERA_OCR_CPU_THREADS=%r; using 1.", value)
            return 1
        return threads

    @staticmethod
    def _configured_recognition_batch_size():
        value = os.environ.get("CAMERA_OCR_RECOGNITION_BATCH_SIZE", "1").strip()
        try:
            batch_size = int(value)
        except ValueError:
            batch_size = 1
        if not 1 <= batch_size <= 32:
            LOG.warning(
                "Unsupported CAMERA_OCR_RECOGNITION_BATCH_SIZE=%r; using 1.",
                value,
            )
            return 1
        return batch_size

    @staticmethod
    def _configured_cache_threshold():
        value = os.environ.get("CAMERA_OCR_CACHE_THRESHOLD", "0.001").strip()
        try:
            threshold = float(value)
        except ValueError:
            threshold = 0.001
        if not 0 <= threshold <= 0.1:
            LOG.warning(
                "Unsupported CAMERA_OCR_CACHE_THRESHOLD=%r; using 0.001.", value
            )
            return 0.001
        return threshold

    @staticmethod
    def _configured_float(name, default, low, high):
        value = os.environ.get(name, str(default)).strip()
        try:
            configured = float(value)
        except ValueError:
            configured = default
        if not low <= configured <= high:
            LOG.warning("Unsupported %s=%r; using %s.", name, value, default)
            return default
        return configured

    @classmethod
    def _configured_change_threshold(cls):
        # Signature pixels use the original 0..255 grayscale range. Three levels
        # matches the existing stability detector's camera-noise tolerance.
        return cls._configured_float("CAMERA_OCR_CHANGE_THRESHOLD", 3.0, 0.0, 32.0)

    @classmethod
    def _configured_change_ratio_threshold(cls):
        return cls._configured_float(
            "CAMERA_OCR_CHANGE_RATIO_THRESHOLD", 0.002, 0.0, 0.25
        )

    @classmethod
    def _configured_forced_interval(cls):
        return cls._configured_float(
            "CAMERA_OCR_FORCED_INTERVAL_SECONDS", 0.45, 0.2, 30.0
        )

    @classmethod
    def _configured_poll_interval(cls):
        return cls._configured_float(
            "CAMERA_OCR_POLL_INTERVAL_SECONDS", 0.075, 0.025, 0.5
        )

    @classmethod
    def _configured_settle_seconds(cls):
        return cls._configured_float("CAMERA_OCR_SETTLE_SECONDS", 0.12, 0.0, 0.5)

    @staticmethod
    def _grayscale(image):
        if image is None or not hasattr(image, "size") or image.size == 0:
            raise ScreenStateError("Camera OCR received an empty phone display image.")
        if len(image.shape) == 3:
            channels = image.shape[2]
            if channels == 4:
                image = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
            elif channels == 3:
                image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            else:
                raise ScreenStateError(
                    f"Camera OCR received an unsupported image with {channels} channels."
                )
        elif len(image.shape) != 2:
            raise ScreenStateError("Camera OCR received an unsupported image shape.")
        if image.dtype != np.uint8:
            image = cv2.normalize(image, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        return image

    def _prepare_image(self, image):
        prepared = self._grayscale(image)
        if self.scale != 1.0:
            prepared = cv2.resize(
                prepared,
                None,
                fx=self.scale,
                fy=self.scale,
                interpolation=cv2.INTER_CUBIC,
            )
        return cv2.cvtColor(prepared, cv2.COLOR_GRAY2BGR)

    @classmethod
    def _screen_signature(cls, image):
        gray = cls._grayscale(image)
        signature = cv2.resize(gray, (96, 160), interpolation=cv2.INTER_AREA)
        return cv2.GaussianBlur(signature, (3, 3), 0)

    @staticmethod
    def _signature_difference(current, previous):
        difference = cv2.absdiff(current, previous)
        return (
            float(difference.mean()),
            float(np.count_nonzero(difference >= 12)) / difference.size,
        )

    def _signatures_match_for_change(self, current, previous):
        if previous is None:
            return False, None, None
        mean_difference, changed_ratio = self._signature_difference(current, previous)
        return (
            mean_difference <= self.change_threshold
            and changed_ratio <= self.change_ratio_threshold,
            mean_difference,
            changed_ratio,
        )

    def _cache_match(self, signature, scope):
        cached = self._cache_by_scope.get(scope)
        if self.cache_threshold <= 0 or cached is None:
            return False, None, None
        cached_signature, cached_candidates = cached
        raw_mean, changed_ratio = self._signature_difference(
            signature, cached_signature
        )
        mean_difference = raw_mean / 255.0
        return (
            mean_difference <= self.cache_threshold
            and changed_ratio <= self.cache_threshold,
            mean_difference,
            changed_ratio,
        )

    def capture_snapshot(self):
        image, _, _ = self._capture_snapshot_with_timings()
        return image

    @staticmethod
    def _camera_seconds(timings):
        return sum(
            timings.get(name, 0.0) for name in ("capture", "decode", "warp", "copy")
        )

    def capture_change_baseline(self):
        """Remember the screen immediately before an input action without OCR."""
        self._change_baseline = self._change_candidate = None
        self._change_candidate_since = None
        snapshot, sequence, timings = self._capture_snapshot_with_timings()
        self._change_baseline = self._screen_signature(snapshot)
        self._change_candidate = None
        record_timing(
            "camera_frame",
            self._camera_seconds(timings),
            count=int(sequence is not None),
        )

    def _capture_snapshot_with_timings(self):
        timings = {}
        session = self.external_screen.session()
        image, sequence, _ = session.capture_for_ocr(timings=timings)
        if image is None:
            raise ScreenStateError("Camera OCR could not capture the phone display.")
        copy_started = time.monotonic()
        snapshot = image.copy()
        timings["copy"] = time.monotonic() - copy_started
        return snapshot, sequence, timings

    def _save_debug_image(self, image):
        if not self.debug_dir:
            return None
        try:
            directory = Path(self.debug_dir).expanduser()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"paddle-ocr-{next(self._debug_sequence):04d}.png"
            if not cv2.imwrite(str(path), image):
                raise OSError("OpenCV did not encode the debug image.")
            LOG.info("Saved exact PaddleOCR input to %s", path)
            return path
        except (OSError, cv2.error) as exc:
            LOG.warning("Could not save Camera OCR debug image: %s", exc)
            return None

    @staticmethod
    def _result_data(result):
        if not isinstance(result, dict):
            return {}
        nested = result.get("res")
        return nested if isinstance(nested, dict) else result

    @staticmethod
    def _position(box, index):
        try:
            values = np.asarray(box, dtype=float)
            if values.shape == (4,):
                return float(values[1]), float(values[0]), index
            if values.ndim == 2 and values.shape[1] == 2:
                return float(values[:, 1].min()), float(values[:, 0].min()), index
        except (TypeError, ValueError):
            pass
        return math.inf, math.inf, index

    @classmethod
    def _candidates_from_results(cls, results):
        candidates, position = [], 0
        for result in results or []:
            data = cls._result_data(result)
            texts = data.get("rec_texts")
            scores = data.get("rec_scores")
            texts = [] if texts is None else texts
            scores = [] if scores is None else scores
            boxes = data.get("rec_boxes")
            if boxes is None:
                boxes = data.get("rec_polys")
            boxes = [] if boxes is None else boxes
            for index, (raw_text, raw_score) in enumerate(zip(texts, scores)):
                text = " ".join(str(raw_text).split())
                if not text:
                    continue
                try:
                    confidence = float(raw_score)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(confidence) or confidence < 0:
                    continue
                if confidence <= 1:
                    confidence *= 100
                confidence = min(confidence, 100.0)
                box = boxes[index] if index < len(boxes) else None
                candidates.append(
                    (
                        cls._position(box, position),
                        {
                            "text": text,
                            "confidence": confidence,
                            "min_confidence": confidence,
                        },
                    )
                )
                position += 1
        candidates.sort(key=lambda item: item[0])
        return [candidate for _, candidate in candidates]

    @staticmethod
    def _timing(timings, name):
        value = timings.get(name)
        return "n/a" if value is None else f"{value:.3f}s"

    def _log_result(self, timings, prepared, candidates, cache_hit, mean, changed):
        if self.debug_logging:
            LOG.info(
                "OCR timing capture=%s decode=%s warp=%s copy=%s preprocess=%s "
                "queue_wait=%s worker_start=%s ipc_send=%s ipc_wait=%s "
                "child_inference=%s child_cpu_user=%s child_cpu_system=%s "
                "total=%s cache=%s cache_mean=%s cache_changed=%s "
                "worker_id=%s worker_pid=%s pool_size=%s busy=%s queued=%s "
                "main_pid=%d native_threads=%s regions=%d "
                "original=%sx%s camera=%sx%s warped=%sx%s calibrated=%sx%s "
                "input=%dx%d "
                "confidences=%r",
                self._timing(timings, "capture"),
                self._timing(timings, "decode"),
                self._timing(timings, "warp"),
                self._timing(timings, "copy"),
                self._timing(timings, "preprocess"),
                self._timing(timings, "queue_wait"),
                self._timing(timings, "worker_start"),
                self._timing(timings, "ipc_send"),
                self._timing(timings, "ipc_wait"),
                self._timing(timings, "inference"),
                self._timing(timings, "cpu_user"),
                self._timing(timings, "cpu_system"),
                self._timing(timings, "total"),
                cache_hit,
                "n/a" if mean is None else f"{mean:.5f}",
                "n/a" if changed is None else f"{changed:.5f}",
                timings.get("worker_id"),
                timings.get("worker_pid"),
                timings.get("pool_size"),
                timings.get("busy_workers"),
                timings.get("queued_requests"),
                os.getpid(),
                timings.get("native_threads"),
                len(candidates),
                timings.get("original_frame_width", "n/a"),
                timings.get("original_frame_height", "n/a"),
                timings.get("camera_frame_width", "n/a"),
                timings.get("camera_frame_height", "n/a"),
                timings.get("warped_width", "n/a"),
                timings.get("warped_height", "n/a"),
                timings.get("calibrated_width", "n/a"),
                timings.get("calibrated_height", "n/a"),
                prepared.shape[1],
                prepared.shape[0],
                [round(item["confidence"], 1) for item in candidates],
            )
        else:
            LOG.debug(
                "PaddleOCR total=%.3fs cache=%s worker_pid=%s main_pid=%d regions=%d",
                timings["total"],
                cache_hit,
                timings.get("worker_pid"),
                os.getpid(),
                len(candidates),
            )

    @staticmethod
    def _record_workflow_timings(
        timings, frame_id, cache_hit, *, failed=False, scope="full"
    ):
        camera_seconds = CameraOCRScreenStateProvider._camera_seconds(timings)
        record_timing("camera_frame", camera_seconds, count=int(frame_id is not None))
        record_timing("ocr_preprocessing", timings.get("preprocess", 0.0))
        record_timing("ocr_queue", timings.get("queue_wait", 0.0))
        record_timing(
            "ocr_ipc",
            sum(
                timings.get(name, 0.0)
                for name in ("worker_start", "ipc_send", "ipc_wait")
            ),
        )
        record_timing("ocr_inference", timings.get("inference", 0.0))
        record_timing("ocr_postprocessing", timings.get("postprocess", 0.0))
        record_timing(
            "ocr_call",
            0.0,
            event={
                "frame_id": (
                    int(frame_id) if isinstance(frame_id, np.integer) else frame_id
                ),
                "cache_hit": cache_hit,
                "scope": scope,
                "failed": failed,
                "worker_pid": timings.get("worker_pid"),
                "worker_id": timings.get("worker_id"),
                "pool_size": timings.get("pool_size"),
                "busy_workers": timings.get("busy_workers"),
                "queued_requests": timings.get("queued_requests"),
                "queue_wait_ms": timings.get("queue_wait", 0.0) * 1000.0,
                "inference_ms": timings.get("inference", 0.0) * 1000.0,
                "total_ocr_ms": timings.get(
                    "total_ocr_ms", timings.get("total", 0.0) * 1000.0
                ),
                "total_seconds": timings.get("total", 0.0),
                "preprocessing_seconds": timings.get("preprocess", 0.0),
                "queue_wait_seconds": timings.get("queue_wait", 0.0),
                "ipc_send_seconds": timings.get("ipc_send", 0.0),
                "ipc_wait_seconds": timings.get("ipc_wait", 0.0),
                "inference_seconds": timings.get("inference", 0.0),
                "postprocessing_seconds": timings.get("postprocess", 0.0),
                "original_frame_width": timings.get("original_frame_width"),
                "original_frame_height": timings.get("original_frame_height"),
                "camera_frame_width": timings.get("camera_frame_width"),
                "camera_frame_height": timings.get("camera_frame_height"),
                "warped_width": timings.get("warped_width"),
                "warped_height": timings.get("warped_height"),
                "calibrated_width": timings.get("calibrated_width"),
                "calibrated_height": timings.get("calibrated_height"),
                "ocr_input_width": timings.get("input_width"),
                "ocr_input_height": timings.get("input_height"),
                "region_count": timings.get("region_count"),
                "cpu_user_seconds": timings.get("cpu_user"),
                "cpu_system_seconds": timings.get("cpu_system"),
                "native_thread_count": timings.get("native_threads"),
            },
        )

    def _recognize_candidates(
        self, image, *, timings=None, total_started=None, frame_id=None, scope="full"
    ):
        timings = {} if timings is None else timings
        total_started = time.monotonic() if total_started is None else total_started
        preprocess_started = time.monotonic()
        signature = self._screen_signature(image)
        timings["signature"] = time.monotonic() - preprocess_started
        inference_queued = time.monotonic()
        with self._inference_lock:
            provider_queue_wait = time.monotonic() - inference_queued
            cache_hit, mean_difference, changed_ratio = self._cache_match(
                signature, scope
            )
            if cache_hit:
                pool_status = self._worker.status()
                candidates = copy.deepcopy(self._cache_by_scope[scope][1])
                timings.update(
                    preprocess=timings["signature"],
                    queue_wait=provider_queue_wait,
                    worker_start=0.0,
                    ipc_send=0.0,
                    ipc_wait=0.0,
                    inference=0.0,
                    worker_pid=None,
                    worker_id=None,
                    pool_size=pool_status["pool_size"],
                    busy_workers=pool_status["busy_workers"],
                    queued_requests=pool_status["queued_requests"],
                    total_ocr_ms=0.0,
                )
                prepared = self._grayscale(image)
            else:
                prepare_started = time.monotonic()
                prepared = self._prepare_image(image)
                timings["preprocess"] = (
                    timings["signature"] + time.monotonic() - prepare_started
                )
                timings["input_width"] = prepared.shape[1]
                timings["input_height"] = prepared.shape[0]
                self._save_debug_image(prepared)
                request_started = time.monotonic()
                try:
                    results, worker_timings = self._worker.request(prepared)
                except Exception:
                    # A failed recognition invalidates even an older successful
                    # screen result; the next request must ask Paddle again.
                    self._cache_by_scope.clear()
                    timings["queue_wait"] = provider_queue_wait
                    timings["ipc_wait"] = time.monotonic() - request_started
                    timings["total"] = time.monotonic() - total_started
                    self._record_workflow_timings(
                        timings, frame_id, False, failed=True, scope=scope
                    )
                    raise
                worker_queue_wait = worker_timings.pop("queue_wait", 0.0)
                timings.update(worker_timings)
                timings["queue_wait"] = provider_queue_wait + worker_queue_wait
                postprocess_started = time.monotonic()
                candidates = self._candidates_from_results(results)
                timings["postprocess"] = time.monotonic() - postprocess_started
                self._cache_by_scope[scope] = (
                    signature.copy(),
                    copy.deepcopy(candidates),
                )
        timings["input_width"] = prepared.shape[1]
        timings["input_height"] = prepared.shape[0]
        timings["region_count"] = len(candidates)
        timings.setdefault("postprocess", 0.0)
        timings["total"] = time.monotonic() - total_started
        self._log_result(
            timings,
            prepared,
            candidates,
            cache_hit,
            mean_difference,
            changed_ratio,
        )
        self._record_workflow_timings(timings, frame_id, cache_hit, scope=scope)
        return candidates

    def close(self):
        if self._owns_worker:
            self._worker.close()

    def start_worker(self):
        return self._worker.start()

    def visible_text_candidates_from_image(self, image):
        return self._recognize_candidates(image)

    def visible_texts_from_image(self, image):
        return [item["text"] for item in self.visible_text_candidates_from_image(image)]

    def visible_text_candidates(self):
        total_started = time.monotonic()
        image, sequence, timings = self._capture_snapshot_with_timings()
        return self._recognize_candidates(
            image,
            timings=timings,
            total_started=total_started,
            frame_id=sequence,
        )

    def visible_texts(self):
        return [item["text"] for item in self.visible_text_candidates()]

    def contains_text(self, text, *, image=None):
        expected = " ".join(text.split()).casefold()
        if not expected:
            return False
        if image is None:
            total_started = time.monotonic()
            snapshot, sequence, timings = self._capture_snapshot_with_timings()
            candidates = self._recognize_candidates(
                snapshot,
                timings=timings,
                total_started=total_started,
                frame_id=sequence,
            )
        else:
            candidates = self.visible_text_candidates_from_image(image)
        with measure_timing("ocr_matching"):
            complete_text = " ".join(item["text"] for item in candidates).casefold()
            return expected in complete_text

    @staticmethod
    def _crop_roi(image, roi):
        x, y, width, height = roi
        image_height, image_width = image.shape[:2]
        left = min(image_width - 1, max(0, int(math.floor(x * image_width))))
        top = min(image_height - 1, max(0, int(math.floor(y * image_height))))
        right = min(
            image_width,
            max(left + 1, int(math.ceil((x + width) * image_width))),
        )
        bottom = min(
            image_height,
            max(top + 1, int(math.ceil((y + height) * image_height))),
        )
        return image[top:bottom, left:right]

    @staticmethod
    def _candidates_contain(candidates, expected):
        with measure_timing("ocr_matching"):
            complete_text = " ".join(item["text"] for item in candidates).casefold()
            return expected in complete_text

    def poll_contains_text(self, text, *, roi=None, force=False):
        """Cheaply gate wait polling, then run ROI OCR with a full fallback.

        Returns ``(ocr_was_run, text_found)`` so the caller can maintain the
        forced-OCR deadline without changing the public ``contains_text`` API.
        """
        expected = " ".join(text.split()).casefold()
        if not expected:
            return False, False

        total_started = time.monotonic()
        snapshot, sequence, timings = self._capture_snapshot_with_timings()
        signature_started = time.monotonic()
        signature = self._screen_signature(snapshot)
        signature_seconds = time.monotonic() - signature_started

        should_check = force or self._change_baseline is None
        reason = "forced" if force else "no_baseline"
        now = time.monotonic()
        if not should_check:
            unchanged, mean_difference, changed_ratio = (
                self._signatures_match_for_change(
                    signature,
                    self._change_baseline,
                )
            )
            if unchanged:
                self._change_candidate = None
                self._change_candidate_since = None
                reason = "unchanged"
            else:
                settled, _, _ = self._signatures_match_for_change(
                    signature, self._change_candidate
                )
                if (
                    settled
                    and self._change_candidate_since is not None
                    and now - self._change_candidate_since >= self.settle_seconds
                ):
                    should_check = True
                    reason = "changed_and_settled"
                else:
                    if not settled:
                        self._change_candidate = signature.copy()
                        self._change_candidate_since = now
                    reason = "change_settling"
            if self.debug_logging:
                LOG.info(
                    "OCR gate frame=%s reason=%s mean_difference=%s "
                    "changed_ratio=%s forced=%s",
                    sequence,
                    reason,
                    "n/a" if mean_difference is None else f"{mean_difference:.3f}",
                    "n/a" if changed_ratio is None else f"{changed_ratio:.5f}",
                    force,
                )

        if not should_check:
            record_timing(
                "camera_frame",
                self._camera_seconds(timings) + signature_seconds,
                count=int(sequence is not None),
            )
            record_timing(
                "ocr_skip",
                0.0,
                count=1,
                event={"frame_id": sequence, "reason": reason},
            )
            return False, False

        self._change_baseline = signature.copy()
        self._change_candidate = None
        self._change_candidate_since = None
        record_timing("ocr_trigger", 0.0, count=1, event={"reason": reason})
        if roi is not None:
            roi_image = self._crop_roi(snapshot, roi)
            roi_timings = dict(timings)
            candidates = self._recognize_candidates(
                roi_image,
                timings=roi_timings,
                total_started=total_started,
                frame_id=sequence,
                scope="roi:" + ",".join(f"{value:.6g}" for value in roi),
            )
            if self._candidates_contain(candidates, expected):
                return True, True
            # Capture/warp happened once; do not count it again for fallback.
            fallback_timings = {
                key: value
                for key, value in timings.items()
                if key not in {"capture", "decode", "warp", "copy"}
            }
            candidates = self._recognize_candidates(
                snapshot,
                timings=fallback_timings,
                frame_id=sequence,
                scope="full_fallback",
            )
        else:
            candidates = self._recognize_candidates(
                snapshot,
                timings=timings,
                total_started=total_started,
                frame_id=sequence,
                scope="full",
            )
        return True, self._candidates_contain(candidates, expected)


PaddleOCRScreenStateProvider = CameraOCRScreenStateProvider

# Temporary comparison path. PaddleOCR remains the default and does not import
# or require Tesseract during normal operation.
if os.environ.get("CAMERA_OCR_ENGINE", "paddle").strip().casefold() == "tesseract":
    from .camera_ocr_tesseract import CameraOCRScreenStateProvider as _TesseractProvider

    CameraOCRScreenStateProvider = _TesseractProvider
