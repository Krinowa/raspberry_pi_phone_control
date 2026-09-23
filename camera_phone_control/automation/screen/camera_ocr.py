"""PaddleOCR-backed semantic text detection for calibrated camera images."""

import copy
import itertools
import json
import logging
import math
import multiprocessing
import os
from pathlib import Path
import threading
import time
import uuid

import cv2
import numpy as np

from .state import ScreenStateError, TextDetectionUnavailable

LOG = logging.getLogger("camera_ocr")


def _build_paddle_engine(language, textline_orientation, cpu_threads):
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
                started = time.monotonic()
                results = [_plain_result(item) for item in engine.predict(input=image)]
                connection.send(
                    (
                        "result",
                        request_id,
                        results,
                        time.monotonic() - started,
                        worker_pid,
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
            "language=%s textline_orientation=%s cpu_threads=%d",
            pid,
            os.getpid(),
            self.initialization_seconds,
            self.config["language"],
            self.config["textline_orientation"],
            self.config["cpu_threads"],
        )
        return time.monotonic() - started

    def request(self, image):
        with self._lock:
            startup = self._start()
            request_id = uuid.uuid4().hex
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
            _, _, results, inference, pid = message
            return results, {
                "worker_start": startup,
                "ipc_send": send_seconds,
                "ipc_wait": wait_seconds,
                "inference": inference,
                "worker_pid": pid,
            }

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


class CameraOCRScreenStateProvider:
    """Read phone UI text through one persistent PaddleOCR child process."""

    _inference_lock = threading.Lock()

    def __init__(self, external_screen, *, language="en", timeout=30.0):
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
        self.cache_threshold = self._configured_cache_threshold()
        self._debug_sequence = itertools.count(1)
        self._cache_signature = self._cache_candidates = None
        self._worker = _PaddleWorkerClient(
            {
                "language": self.language,
                "textline_orientation": self.textline_orientation,
                "cpu_threads": self.cpu_threads,
            },
            self.timeout,
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
        value = os.environ.get("CAMERA_OCR_CPU_THREADS", "4").strip()
        try:
            threads = int(value)
        except ValueError:
            threads = 4
        if not 1 <= threads <= 16:
            LOG.warning("Unsupported CAMERA_OCR_CPU_THREADS=%r; using 4.", value)
            return 4
        return threads

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

    def _cache_match(self, signature):
        if (
            self.cache_threshold <= 0
            or self._cache_signature is None
            or self._cache_candidates is None
        ):
            return False, None, None
        difference = cv2.absdiff(signature, self._cache_signature)
        mean_difference = float(difference.mean()) / 255.0
        changed_ratio = float(np.count_nonzero(difference >= 12)) / difference.size
        return (
            mean_difference <= self.cache_threshold
            and changed_ratio <= self.cache_threshold,
            mean_difference,
            changed_ratio,
        )

    def capture_snapshot(self):
        image, _, _ = self._capture_snapshot_with_timings()
        return image

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
                "worker_start=%s ipc_send=%s ipc_wait=%s child_inference=%s "
                "total=%s cache=%s cache_mean=%s cache_changed=%s "
                "worker_pid=%s main_pid=%d regions=%d image=%dx%d detections=%r",
                self._timing(timings, "capture"),
                self._timing(timings, "decode"),
                self._timing(timings, "warp"),
                self._timing(timings, "copy"),
                self._timing(timings, "preprocess"),
                self._timing(timings, "worker_start"),
                self._timing(timings, "ipc_send"),
                self._timing(timings, "ipc_wait"),
                self._timing(timings, "inference"),
                self._timing(timings, "total"),
                cache_hit,
                "n/a" if mean is None else f"{mean:.5f}",
                "n/a" if changed is None else f"{changed:.5f}",
                timings.get("worker_pid"),
                os.getpid(),
                len(candidates),
                prepared.shape[1],
                prepared.shape[0],
                [
                    {"text": item["text"], "confidence": round(item["confidence"], 1)}
                    for item in candidates
                ],
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

    def _recognize_candidates(self, image, *, timings=None, total_started=None):
        timings = {} if timings is None else timings
        total_started = time.monotonic() if total_started is None else total_started
        preprocess_started = time.monotonic()
        signature = self._screen_signature(image)
        timings["signature"] = time.monotonic() - preprocess_started
        with self._inference_lock:
            cache_hit, mean_difference, changed_ratio = self._cache_match(signature)
            if cache_hit:
                candidates = copy.deepcopy(self._cache_candidates)
                timings.update(
                    preprocess=time.monotonic() - preprocess_started,
                    worker_start=0.0,
                    ipc_send=0.0,
                    ipc_wait=0.0,
                    inference=0.0,
                    worker_pid=self._worker.pid,
                )
                prepared = self._grayscale(image)
            else:
                prepared = self._prepare_image(image)
                timings["preprocess"] = time.monotonic() - preprocess_started
                self._save_debug_image(prepared)
                try:
                    results, worker_timings = self._worker.request(prepared)
                except Exception:
                    # A failed recognition invalidates even an older successful
                    # screen result; the next request must ask Paddle again.
                    self._cache_signature = self._cache_candidates = None
                    raise
                timings.update(worker_timings)
                candidates = self._candidates_from_results(results)
                self._cache_signature = signature.copy()
                self._cache_candidates = copy.deepcopy(candidates)
        timings["total"] = time.monotonic() - total_started
        self._log_result(
            timings,
            prepared,
            candidates,
            cache_hit,
            mean_difference,
            changed_ratio,
        )
        return candidates

    def close(self):
        self._worker.close()

    def visible_text_candidates_from_image(self, image):
        return self._recognize_candidates(image)

    def visible_texts_from_image(self, image):
        return [item["text"] for item in self.visible_text_candidates_from_image(image)]

    def visible_text_candidates(self):
        total_started = time.monotonic()
        image, _, timings = self._capture_snapshot_with_timings()
        return self._recognize_candidates(
            image,
            timings=timings,
            total_started=total_started,
        )

    def visible_texts(self):
        return [item["text"] for item in self.visible_text_candidates()]

    def contains_text(self, text, *, image=None):
        expected = " ".join(text.split()).casefold()
        if not expected:
            return False
        if image is None:
            total_started = time.monotonic()
            snapshot, _, timings = self._capture_snapshot_with_timings()
            candidates = self._recognize_candidates(
                snapshot,
                timings=timings,
                total_started=total_started,
            )
        else:
            candidates = self.visible_text_candidates_from_image(image)
        complete_text = " ".join(item["text"] for item in candidates).casefold()
        return expected in complete_text


PaddleOCRScreenStateProvider = CameraOCRScreenStateProvider

# Temporary comparison path. PaddleOCR remains the default and does not import
# or require Tesseract during normal operation.
if os.environ.get("CAMERA_OCR_ENGINE", "paddle").strip().casefold() == "tesseract":
    from .camera_ocr_tesseract import CameraOCRScreenStateProvider as _TesseractProvider

    CameraOCRScreenStateProvider = _TesseractProvider
