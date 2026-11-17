"""External camera capture only. No Android screen capture or image analysis."""

import logging
import os
import sys
import threading
import time
from collections import deque

import cv2
import numpy as np

from diagnostic_logging import log_event, sanitize_url

from hardware.camera_capture_worker import (
    STABLE_IDENTITY_MISMATCH,
    CameraCaptureWorker,
)

LOG = logging.getLogger("camera")

DEFAULT_READ_FAILURE_GRACE_SECONDS = 3.0
DEFAULT_READ_RETRY_DELAY_SECONDS = 0.05
DEFAULT_FRAME_STALE_SECONDS = 3.0
DEFAULT_SLOW_READ_SECONDS = 2.0
DEFAULT_AVFOUNDATION_MODES = ((1920, 1080, 30.0), (1280, 720, 30.0))


def _env_float(name, default, *, minimum=0.0):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        LOG.warning("Ignoring invalid %s=%r; using %.2f", name, raw, default)
        return default
    if value < minimum:
        LOG.warning("Ignoring invalid %s=%r; using %.2f", name, raw, default)
        return default
    return value


def _avfoundation_modes_from_env():
    raw = os.environ.get("CAMERA_AVFOUNDATION_MODES")
    if raw is None or not raw.strip():
        return DEFAULT_AVFOUNDATION_MODES
    if raw.strip().casefold() in {"off", "none", "disabled"}:
        return ()
    modes = []
    try:
        for item in raw.split(","):
            size, fps = item.strip().casefold().split("@", 1)
            width, height = size.split("x", 1)
            mode = (int(width), int(height), float(fps))
            if min(mode) <= 0:
                raise ValueError
            modes.append(mode)
    except (TypeError, ValueError):
        LOG.warning(
            "Ignoring invalid CAMERA_AVFOUNDATION_MODES=%r; expected "
            "WIDTHxHEIGHT@FPS[,WIDTHxHEIGHT@FPS]",
            raw,
        )
        return DEFAULT_AVFOUNDATION_MODES
    return tuple(modes)


def rotate_frame(frame, rotation):
    """Apply one configured clockwise rotation to a raw camera frame."""
    if rotation == 0:
        return frame
    if rotation == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if rotation == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if rotation == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    raise ValueError("Camera rotation must be 0, 90, 180, or 270 degrees.")


class Camera:
    def __init__(
        self,
        source=0,
        width=1280,
        height=720,
        fps=15,
        rotate=0,
        read_failure_grace_seconds=None,
        read_retry_delay_seconds=None,
        frame_stale_seconds=None,
        slow_read_seconds=None,
        avfoundation_modes=None,
        use_capture_subprocess=None,
        capture_worker_factory=None,
    ):
        self.source = source
        self.width = width
        self.height = height
        self._fps = fps
        self.rotate = rotate
        self.read_failure_grace_seconds = (
            _env_float(
                "CAMERA_READ_FAILURE_GRACE_SECONDS",
                DEFAULT_READ_FAILURE_GRACE_SECONDS,
            )
            if read_failure_grace_seconds is None
            else read_failure_grace_seconds
        )
        self.read_retry_delay_seconds = (
            _env_float(
                "CAMERA_READ_RETRY_DELAY_SECONDS",
                DEFAULT_READ_RETRY_DELAY_SECONDS,
            )
            if read_retry_delay_seconds is None
            else read_retry_delay_seconds
        )
        self.frame_stale_seconds = (
            _env_float(
                "CAMERA_FRAME_STALE_SECONDS",
                max(DEFAULT_FRAME_STALE_SECONDS, self.read_failure_grace_seconds),
            )
            if frame_stale_seconds is None
            else frame_stale_seconds
        )
        self.slow_read_seconds = (
            _env_float("CAMERA_SLOW_READ_SECONDS", DEFAULT_SLOW_READ_SECONDS)
            if slow_read_seconds is None
            else slow_read_seconds
        )
        self.avfoundation_modes = (
            _avfoundation_modes_from_env()
            if avfoundation_modes is None
            else tuple(avfoundation_modes)
        )
        self.use_capture_subprocess = use_capture_subprocess
        self.capture_worker_factory = capture_worker_factory or CameraCaptureWorker

        self.lock = threading.Lock()
        # Wakes RemoteViewer frame streams when a new frame is stored.
        self.frame_condition = threading.Condition(self.lock)
        self.lifecycle_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.capture = None
        self.capture_worker = None
        self.worker_pid = None
        self.opened_event = threading.Event()
        self.open_succeeded = None
        self.identity_mismatch = False
        self.latest = None
        # (BGR array or None, wall-clock capture time in microseconds) for the
        # frame in ``latest``. None means only the JPEG crossed the process
        # boundary; wait_raw_frame() decodes it on demand.
        self.latest_raw = None
        self.raw_frame_width = None
        self.raw_frame_height = None
        self.last_successful_frame_at = None
        self.negotiated_capture = None
        self.sequence = 0
        self.timestamps = deque(maxlen=90)

        self.error = "Camera is starting..."
        self.preview_debug = os.environ.get(
            "CAMERA_PREVIEW_DEBUG", ""
        ).strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._debug_started = time.monotonic()
        self._debug_captures = 0
        self._debug_updates = 0
        self._debug_read_seconds = 0.0
        self._debug_encode_seconds = 0.0

    @property
    def fps(self):
        return self._fps

    @fps.setter
    def fps(self, value):
        self._fps = value
        worker = getattr(self, "capture_worker", None)
        update_fps = getattr(worker, "update_fps", None)
        if update_fps is not None:
            update_fps(value)

    def start(self):
        with self.lifecycle_lock:
            if self.thread is not None and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.opened_event.clear()
            self.open_succeeded = None
            with self.lock:
                self.identity_mismatch = False
                self.error = "Camera is starting..."
                self.latest = None
                self.timestamps.clear()
                self.last_successful_frame_at = None
                self.negotiated_capture = None
            self.thread = threading.Thread(
                target=(
                    self._capture_subprocess
                    if self._should_use_capture_subprocess()
                    else self._capture
                ),
                daemon=True,
            )

            self.thread.start()

    def _should_use_capture_subprocess(self):
        if self.use_capture_subprocess is not None:
            return bool(self.use_capture_subprocess)
        return (
            sys.platform == "darwin"
            and type(self.source) is int
            and getattr(self, "capture_backend", None) in (None, "avfoundation")
        )

    def _capture_subprocess(self):
        worker = None
        try:
            config = {
                "source": self.source,
                "device_id": getattr(self, "device_id", None),
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "rotation": self.rotate,
                "configured_source": getattr(self, "configured_source", None),
                "stable_id": (
                    getattr(self, "configured_source", None)
                    if isinstance(getattr(self, "configured_source", None), str)
                    and getattr(self, "configured_source", None).startswith(
                        "avfoundation:"
                    )
                    else None
                ),
                "camera_name": getattr(self, "camera_name", None),
                "avfoundation_modes": self.avfoundation_modes,
                "startup_grace_seconds": max(self.read_failure_grace_seconds, 10.0),
                "read_failure_grace_seconds": self.read_failure_grace_seconds,
                "read_retry_delay_seconds": self.read_retry_delay_seconds,
            }
            worker = self.capture_worker_factory(config)
            with self.lock:
                self.capture_worker = worker
            worker.start()
            self.worker_pid = worker.pid
            LOG.info(
                "AVFoundation camera worker started pid=%s resolved_index=%r "
                "stable_identity=%r",
                self.worker_pid,
                self.source,
                getattr(self, "configured_source", None),
            )

            while not self.stop_event.is_set():
                message = worker.receive(timeout=0.1)
                if message is None:
                    if not worker.is_alive():
                        raise RuntimeError(
                            "AVFoundation camera worker exited unexpectedly "
                            f"(pid={self.worker_pid}, exitcode={worker.exitcode})."
                        )
                    continue
                if message.get("type") == "error":
                    if message.get("error_code") == STABLE_IDENTITY_MISMATCH:
                        with self.lock:
                            self.identity_mismatch = True
                    raise RuntimeError(message.get("error") or "Camera worker failed.")
                if message.get("type") != "frame":
                    continue

                expected_device_id = getattr(self, "device_id", None)
                message_device_id = message.get("device_id")
                if (
                    expected_device_id is not None
                    and message_device_id != expected_device_id
                ):
                    raise RuntimeError(
                        "Camera worker returned a frame for the wrong logical "
                        f"device: expected={expected_device_id!r} "
                        f"received={message_device_id!r}."
                    )

                identity_validator = getattr(self, "identity_validator", None)
                if identity_validator is not None and not identity_validator():
                    with self.lock:
                        self.identity_mismatch = True
                    raise RuntimeError(
                        "Configured camera identity no longer resolves to this "
                        f"AVFoundation index: stable_identity="
                        f"{getattr(self, 'configured_source', None)!r} "
                        f"resolved_index={self.source!r}."
                    )

                received_at = time.monotonic()
                with self.lock:
                    if self.open_succeeded is None:
                        self.open_succeeded = True
                        self.opened_event.set()
                        LOG.info(
                            "Camera healthy after first valid worker frame "
                            "pid=%s resolved_index=%r stable_identity=%r",
                            self.worker_pid,
                            self.source,
                            getattr(self, "configured_source", None),
                        )
                    self.sequence += 1
                    self.latest = (
                        message["jpeg"],
                        self.sequence,
                        message["width"],
                        message["height"],
                    )
                    self.latest_raw = (
                        None,
                        message.get("captured_at_us") or time.time_ns() // 1000,
                    )
                    self.frame_condition.notify_all()
                    self.raw_frame_width = message["raw_width"]
                    self.raw_frame_height = message["raw_height"]
                    self.last_successful_frame_at = received_at
                    self.negotiated_capture = message.get("negotiated_capture")
                    self.timestamps.append(
                        (
                            self.sequence,
                            received_at,
                            message["width"],
                            message["height"],
                        )
                    )
                    self.error = ""
        except Exception as exc:
            if not self.stop_event.is_set():
                log_event(
                    LOG,
                    logging.ERROR,
                    "camera",
                    "camera_worker_exited",
                    device_id=getattr(self, "device_id", None),
                    camera_id=getattr(self, "configured_source", None),
                    error_code="camera_worker_exited",
                    exception_type=type(exc).__name__,
                )
                with self.lock:
                    if self.open_succeeded is None:
                        self.open_succeeded = False
                        self.opened_event.set()
                    self.error = str(exc)
                    self.latest = None
        finally:
            if worker is not None:
                worker.close()
            with self.lock:
                if self.capture_worker is worker:
                    self.capture_worker = None
                if self.open_succeeded is None:
                    self.open_succeeded = False
                    self.opened_event.set()

    def _open_capture(self):
        """
        Open either:
        - local camera index, e.g. 0 / 1 / 2
        - HTTP/MJPEG/RTSP URL
        """

        # ----------------------------------------------------
        # Network camera / DroidCam / RTSP
        # ----------------------------------------------------

        capture_backend = getattr(self, "capture_backend", None)
        if isinstance(self.source, str) and capture_backend == "v4l2":
            LOG.info("Opening V4L2 camera path: %s", self.source)
            return cv2.VideoCapture(self.source, cv2.CAP_V4L2)

        if isinstance(self.source, str):
            LOG.info(
                "Opening network camera stream: %s",
                sanitize_url(self.source),
            )

            return cv2.VideoCapture(
                self.source,
                cv2.CAP_FFMPEG,
            )

        # ----------------------------------------------------
        # Local camera
        # ----------------------------------------------------

        if capture_backend == "dshow":
            backend = cv2.CAP_DSHOW
        else:
            backend = cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY

        LOG.info(
            "Opening local camera index: %s",
            self.source,
        )

        return cv2.VideoCapture(
            self.source,
            backend,
        )

    @staticmethod
    def _capture_value(cap, prop):
        try:
            value = cap.get(prop)
        except Exception:
            return None
        return (
            value
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None
        )

    @staticmethod
    def _backend_name(cap):
        get_backend_name = getattr(cap, "getBackendName", None)
        if get_backend_name is None:
            return "AVFoundation" if sys.platform == "darwin" else "unknown"
        try:
            return get_backend_name()
        except Exception:
            return "AVFoundation" if sys.platform == "darwin" else "unknown"

    @staticmethod
    def _fourcc_name(value):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return "unknown"
        if number <= 0:
            return str(number)
        return "".join(chr((number >> (8 * offset)) & 0xFF) for offset in range(4))

    def _capture_diagnostics(self, cap):
        width = self._capture_value(cap, cv2.CAP_PROP_FRAME_WIDTH)
        height = self._capture_value(cap, cv2.CAP_PROP_FRAME_HEIGHT)
        fps = self._capture_value(cap, cv2.CAP_PROP_FPS)
        fourcc = self._capture_value(cap, getattr(cv2, "CAP_PROP_FOURCC", 6))
        return {
            "backend": self._backend_name(cap),
            "width": width,
            "height": height,
            "fps": fps,
            "fourcc": self._fourcc_name(fourcc),
        }

    def _log_capture_mode(self, cap, *, requested=None, level=logging.INFO):
        details = self._capture_diagnostics(cap)
        self.negotiated_capture = details
        LOG.log(
            level,
            "Camera capture mode requested=%s accepted=%sx%s@%s fourcc=%s "
            "backend=%s resolved_index=%r stable_identity=%r camera_name=%r",
            requested or "driver-default",
            details["width"],
            details["height"],
            details["fps"],
            details["fourcc"],
            details["backend"],
            self.source,
            getattr(self, "configured_source", None),
            getattr(self, "camera_name", None),
        )
        return details

    def _configure_capture(self, cap):
        is_avfoundation = sys.platform == "darwin" and type(self.source) is int
        if not is_avfoundation or not self.avfoundation_modes:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_FPS, self.fps)
            self._log_capture_mode(
                cap, requested=f"{self.width}x{self.height}@{self.fps}"
            )
            return

        for width, height, fps in self.avfoundation_modes:
            requested = f"{width}x{height}@{fps:g}"
            set_width = cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            set_height = cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            set_fps = cap.set(cv2.CAP_PROP_FPS, fps)
            details = self._log_capture_mode(cap, requested=requested)
            accepted_width = details["width"]
            accepted_height = details["height"]
            accepted_fps = details["fps"]
            dimensions_match = (
                accepted_width is not None
                and accepted_height is not None
                and abs(accepted_width - width) < 1
                and abs(accepted_height - height) < 1
            )
            fps_matches = accepted_fps in (None, 0) or abs(accepted_fps - fps) <= max(
                1.0, fps * 0.1
            )
            if (
                set_width
                and set_height
                and set_fps
                and dimensions_match
                and fps_matches
            ):
                return
            LOG.warning(
                "AVFoundation did not accept requested camera mode %s "
                "(set_width=%s set_height=%s set_fps=%s); trying fallback",
                requested,
                set_width,
                set_height,
                set_fps,
            )
        LOG.warning("No preferred AVFoundation camera mode was fully accepted")

    def _read_failure_message(
        self, cap, *, read_ok, frame_is_none, read_seconds, failure_seconds
    ):
        details = self._capture_diagnostics(cap)
        try:
            capture_opened = cap.isOpened()
        except Exception:
            capture_opened = None
        return (
            "Camera stopped delivering frames: "
            f"read_ok={read_ok} frame_is_none={frame_is_none} "
            f"read_seconds={read_seconds:.3f} "
            f"seconds_since_last_success={failure_seconds:.3f} "
            f"capture_opened={capture_opened} width={details['width']} "
            f"height={details['height']} fps={details['fps']} "
            f"fourcc={details['fourcc']} backend={details['backend']} "
            f"resolved_index={self.source!r} "
            f"stable_identity={getattr(self, 'configured_source', None)!r} "
            f"camera_name={getattr(self, 'camera_name', None)!r}."
        )

    def _capture(self):
        cap = None

        try:
            cap = self._open_capture()

            with self.lock:
                self.capture = cap

            if not cap.isOpened():
                raise RuntimeError(
                    f"Cannot open camera source {self.source!r}. "
                    "Check the camera index/URL, network connection, "
                    "and camera permissions."
                )

            self._configure_capture(cap)
            LOG.info(
                "Camera handle opened; waiting for first valid frame "
                "resolved_index=%r stable_identity=%r",
                self.source,
                getattr(self, "configured_source", None),
            )

            capture_started = time.monotonic()
            failure_started = None
            consecutive_read_failures = 0
            startup_grace_seconds = max(
                self.read_failure_grace_seconds,
                10.0 if sys.platform == "darwin" else self.read_failure_grace_seconds,
            )

            while not self.stop_event.is_set():

                started = time.monotonic()

                read_started = time.monotonic()
                ok, frame = cap.read()
                read_seconds = time.monotonic() - read_started

                if read_seconds >= self.slow_read_seconds:
                    details = self._capture_diagnostics(cap)
                    LOG.warning(
                        "Slow camera read completed in %.3fs read_ok=%s "
                        "frame_is_none=%s capture_opened=%s width=%s height=%s "
                        "fps=%s backend=%s resolved_index=%r stable_identity=%r",
                        read_seconds,
                        ok,
                        frame is None,
                        cap.isOpened(),
                        details["width"],
                        details["height"],
                        details["fps"],
                        details["backend"],
                        self.source,
                        getattr(self, "configured_source", None),
                    )

                if self.stop_event.is_set():
                    break

                if not ok or frame is None:
                    failure_started = failure_started or read_started
                    consecutive_read_failures += 1
                    now = time.monotonic()
                    last_success = self.last_successful_frame_at

                    if last_success is None:
                        failure_seconds = now - capture_started
                        failure_limit = startup_grace_seconds
                    else:
                        failure_seconds = now - last_success
                        failure_limit = self.read_failure_grace_seconds

                    message = self._read_failure_message(
                        cap,
                        read_ok=ok,
                        frame_is_none=frame is None,
                        read_seconds=read_seconds,
                        failure_seconds=failure_seconds,
                    )
                    if consecutive_read_failures == 1:
                        log_event(
                            LOG,
                            logging.WARNING,
                            "camera",
                            "camera_read_failed",
                            "Camera frame delivery failed; recovery will be attempted.",
                            device_id=getattr(self, "device_id", None),
                            camera_id=getattr(self, "configured_source", None),
                            error_code="camera_read_failed",
                            duration_ms=round(failure_seconds * 1000),
                            recovery_attempt=1,
                        )
                        log_event(
                            LOG,
                            logging.WARNING,
                            "camera",
                            "camera_recovery_started",
                            device_id=getattr(self, "device_id", None),
                            camera_id=getattr(self, "configured_source", None),
                            recovery_attempt=1,
                        )

                    if failure_seconds >= failure_limit:
                        raise RuntimeError(message)

                    self.stop_event.wait(self.read_retry_delay_seconds)
                    continue

                if consecutive_read_failures:
                    log_event(
                        LOG,
                        logging.INFO,
                        "camera",
                        "camera_recovered",
                        "Camera frame delivery recovered.",
                        device_id=getattr(self, "device_id", None),
                        camera_id=getattr(self, "configured_source", None),
                        failed_reads=consecutive_read_failures,
                        duration_ms=round((time.monotonic() - failure_started) * 1000),
                        outcome="recovered",
                    )
                failure_started = None
                consecutive_read_failures = 0
                self.last_successful_frame_at = time.monotonic()
                captured_at_us = time.time_ns() // 1000

                raw_frame_height, raw_frame_width = frame.shape[:2]

                # Rotate exactly once, before the shared JPEG is stored. Preview,
                # calibration, OCR, stability checks, and frame APIs all consume
                # this same encoded frame and its rotated dimensions.
                frame = rotate_frame(frame, self.rotate)

                encode_started = time.monotonic()
                ok, encoded = cv2.imencode(
                    ".jpg",
                    frame,
                    [
                        cv2.IMWRITE_JPEG_QUALITY,
                        80,
                    ],
                )
                encode_seconds = time.monotonic() - encode_started

                if not ok:
                    raise RuntimeError("Unable to encode the camera frame.")

                frame_width = frame.shape[1]
                frame_height = frame.shape[0]

                with self.lock:

                    if self.open_succeeded is None:
                        # Reassignment is successful only after this source has
                        # delivered and encoded a real frame.
                        self.open_succeeded = True
                        self.opened_event.set()
                        LOG.info(
                            "Camera healthy after first valid frame "
                            "resolved_index=%r stable_identity=%r",
                            self.source,
                            getattr(self, "configured_source", None),
                        )

                    self.sequence += 1

                    self.latest = (
                        encoded.tobytes(),
                        self.sequence,
                        frame_width,
                        frame_height,
                    )
                    # read() returns a new array each time and nothing below
                    # writes to it, so readers can share this reference.
                    self.latest_raw = (frame, captured_at_us)
                    self.frame_condition.notify_all()
                    self.raw_frame_width = raw_frame_width
                    self.raw_frame_height = raw_frame_height

                    self.timestamps.append(
                        (
                            self.sequence,
                            time.monotonic(),
                            frame_width,
                            frame_height,
                        )
                    )

                    self.error = ""

                if self.preview_debug:
                    self._debug_captures += 1
                    self._debug_updates += 1
                    self._debug_read_seconds += read_seconds
                    self._debug_encode_seconds += encode_seconds
                    debug_now = time.monotonic()
                    debug_elapsed = debug_now - self._debug_started
                    if debug_elapsed >= 1:
                        LOG.info(
                            "Camera capture FPS=%.1f latest-frame update FPS=%.1f "
                            "read=%.1fms encode=%.1fms sequence=%d threads=%d",
                            self._debug_captures / debug_elapsed,
                            self._debug_updates / debug_elapsed,
                            self._debug_read_seconds * 1000 / self._debug_captures,
                            self._debug_encode_seconds * 1000 / self._debug_captures,
                            self.sequence,
                            threading.active_count(),
                        )
                        self._debug_started = debug_now
                        self._debug_captures = 0
                        self._debug_updates = 0
                        self._debug_read_seconds = 0.0
                        self._debug_encode_seconds = 0.0

                elapsed = time.monotonic() - started

                wait_time = max(
                    0,
                    1 / self.fps - elapsed,
                )

                self.stop_event.wait(wait_time)

        except Exception as exc:
            log_event(
                LOG,
                logging.ERROR,
                "camera",
                (
                    "camera_open_failed"
                    if self.open_succeeded is None
                    else "camera_read_failed"
                ),
                device_id=getattr(self, "device_id", None),
                camera_id=getattr(self, "configured_source", None),
                error_code="camera_unavailable",
                exception_type=type(exc).__name__,
            )

            with self.lock:
                if self.open_succeeded is None:
                    self.open_succeeded = False
                    self.opened_event.set()
                self.error = str(exc)
                self.latest = None

        finally:

            should_release = False
            with self.lock:
                if self.capture is cap:
                    self.capture = None
                    should_release = True
                if self.open_succeeded is None:
                    self.open_succeeded = False
                    self.opened_event.set()
            if cap is not None and should_release:
                cap.release()

    def wait_until_opened(self, timeout=1.5):
        if not self.opened_event.wait(timeout):
            return None
        with self.lock:
            return self.open_succeeded

    def status(self):
        with self.lock:

            ready = bool(
                self.latest
                and self.timestamps
                and (
                    time.monotonic() - self.timestamps[-1][1] < self.frame_stale_seconds
                )
            )

            return {
                "ready": ready,
                # Keep "index" for compatibility
                # with your existing frontend.
                "index": self.source,
                "source": str(self.source),
                "width": (self.latest[2] if self.latest else None),
                "height": (self.latest[3] if self.latest else None),
                "error": ("" if ready else (self.error or "Camera feed is stale.")),
                "worker_pid": self.worker_pid,
                "worker_alive": bool(
                    self.capture_worker is not None and self.capture_worker.is_alive()
                ),
                "identity_mismatch": self.identity_mismatch,
            }

    def frame(self):
        with self.lock:
            return self.latest

    def wait_raw_frame(self, after_sequence, timeout=5.0):
        """Block until a frame newer than ``after_sequence`` exists.

        Returns ``(sequence, capture_time_us, bgr)`` for the newest frame, or
        None on timeout. Only the newest frame is kept, so a slow caller
        skips frames instead of queueing them.
        """
        with self.frame_condition:
            if not self.frame_condition.wait_for(
                lambda: self.latest is not None and self.latest[1] > after_sequence,
                timeout,
            ):
                return None
            jpeg, sequence = self.latest[:2]
            bgr, captured_at_us = self.latest_raw
        if bgr is None:
            bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:
                return None
        return sequence, captured_at_us, bgr

    def observation(self):
        """Atomically pair the latest JPEG/sequence/geometry with capture time."""
        with self.lock:
            if self.latest is None or not self.timestamps or self.stop_event.is_set():
                return None
            return (*self.latest, self.timestamps[-1][1])

    def validate_frame(self, frame_id):
        with self.lock:

            if type(frame_id) is not int or not self.latest:
                raise ValueError("A fresh camera frame is required.")

            for (
                number,
                timestamp,
                width,
                height,
            ) in self.timestamps:

                if number == frame_id and (
                    time.monotonic() - timestamp < self.frame_stale_seconds
                ):
                    if (
                        width,
                        height,
                    ) == self.latest[2:]:

                        return

            raise ValueError(
                "Displayed camera frame is stale. "
                "Input disabled; wait for live video."
            )

    def close(self, timeout=2):
        """Stop capture without releasing a handle underneath ``read()``.

        OpenCV backends may execute native code inside ``read()``. The capture
        thread or AVFoundation child remains the sole owner that releases its
        VideoCapture before a replacement is opened.
        """
        with self.lifecycle_lock:
            self.stop_event.set()
            worker = self.capture_worker
            if worker is not None:
                worker.request_stop()
            thread = self.thread
            if thread is None:
                return
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise RuntimeError(
                    "Camera capture thread did not stop; the source was not replaced."
                )
