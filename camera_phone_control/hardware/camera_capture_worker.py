"""Isolated AVFoundation capture for recoverable macOS camera hot-plugging."""

from __future__ import annotations

import logging
import multiprocessing
import queue
import time

import cv2

from diagnostic_logging import (
    configure_child_logging,
    current_logging_queue,
    log_event,
)

from hardware.camera_identity import discover_macos_devices

LOG = logging.getLogger("camera-worker")
STABLE_IDENTITY_MISMATCH = "stable_identity_mismatch"


class StableIdentityMismatchError(RuntimeError):
    """The configured camera no longer owns the worker's runtime index."""


def _capture_value(capture, prop):
    try:
        value = capture.get(prop)
    except Exception:
        return None
    return (
        value
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _backend_name(capture):
    get_backend_name = getattr(capture, "getBackendName", None)
    if get_backend_name is None:
        return "AVFoundation"
    try:
        return get_backend_name()
    except Exception:
        return "AVFoundation"


def _fourcc_name(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "unknown"
    if number <= 0:
        return str(number)
    return "".join(chr((number >> (8 * offset)) & 0xFF) for offset in range(4))


def _diagnostics(capture):
    return {
        "backend": _backend_name(capture),
        "width": _capture_value(capture, cv2.CAP_PROP_FRAME_WIDTH),
        "height": _capture_value(capture, cv2.CAP_PROP_FRAME_HEIGHT),
        "fps": _capture_value(capture, cv2.CAP_PROP_FPS),
        "fourcc": _fourcc_name(
            _capture_value(capture, getattr(cv2, "CAP_PROP_FOURCC", 6))
        ),
    }


def _configure_capture(capture, modes):
    last = None
    for width, height, fps in modes:
        requested = f"{width}x{height}@{fps:g}"
        set_width = capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        set_height = capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        set_fps = capture.set(cv2.CAP_PROP_FPS, fps)
        details = _diagnostics(capture)
        details["requested"] = requested
        last = details
        dimensions_match = (
            details["width"] is not None
            and details["height"] is not None
            and abs(details["width"] - width) < 1
            and abs(details["height"] - height) < 1
        )
        accepted_fps = details["fps"]
        fps_matches = accepted_fps in (None, 0) or abs(accepted_fps - fps) <= max(
            1.0, fps * 0.1
        )
        LOG.info(
            "Camera capture mode requested=%s accepted=%sx%s@%s fourcc=%s "
            "backend=%s",
            requested,
            details["width"],
            details["height"],
            details["fps"],
            details["fourcc"],
            details["backend"],
        )
        if set_width and set_height and set_fps and dimensions_match and fps_matches:
            return details
        LOG.warning(
            "AVFoundation did not accept requested camera mode %s "
            "(set_width=%s set_height=%s set_fps=%s); trying fallback",
            requested,
            set_width,
            set_height,
            set_fps,
        )
    if last is None:
        last = _diagnostics(capture)
        last["requested"] = "driver-default"
    else:
        LOG.warning("No preferred AVFoundation camera mode was fully accepted")
    return last


def _configure_requested_capture(capture, width, height, fps):
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    capture.set(cv2.CAP_PROP_FPS, fps)
    details = _diagnostics(capture)
    details["requested"] = f"{width}x{height}@{fps}"
    return details


def _rotate_frame(frame, rotation):
    if rotation == 0:
        return frame
    if rotation == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if rotation == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if rotation == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    raise ValueError("Camera rotation must be 0, 90, 180, or 270 degrees.")


def _put_latest(output_queue, message):
    """Publish without allowing camera frames to accumulate in memory."""
    while True:
        try:
            output_queue.put_nowait(message)
            return
        except queue.Full:
            try:
                output_queue.get_nowait()
            except queue.Empty:
                time.sleep(0.001)


def _failure_message(
    capture,
    config,
    *,
    read_ok,
    frame_is_none,
    read_seconds,
    failure_seconds,
):
    details = _diagnostics(capture)
    try:
        capture_opened = capture.isOpened()
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
        f"resolved_index={config['source']!r} "
        f"stable_identity={config.get('configured_source')!r} "
        f"camera_name={config.get('camera_name')!r}."
    )


def capture_worker_main(
    output_queue,
    stop_event,
    config,
    capture_factory=None,
    identity_discovery=None,
):
    """Own one AVFoundation handle until stopped or frame delivery fails."""
    configure_child_logging(config.get("_diagnostic_log_queue"))
    capture = None
    capture_factory = capture_factory or cv2.VideoCapture
    identity_discovery = identity_discovery or discover_macos_devices
    delivered_frame = False
    try:
        source = config["source"]
        capture = capture_factory(source, cv2.CAP_AVFOUNDATION)
        if not capture.isOpened():
            raise RuntimeError(
                f"Cannot open camera source {source!r}. Check the camera connection "
                "and camera permissions."
            )

        modes = config.get("avfoundation_modes", ())
        negotiated = (
            _configure_capture(capture, modes)
            if modes
            else _configure_requested_capture(
                capture, config["width"], config["height"], config["fps"]
            )
        )
        capture_started = time.monotonic()
        last_success = None
        failure_started = None
        consecutive_read_failures = 0
        sequence = 0

        while not stop_event.is_set():
            iteration_started = time.monotonic()
            read_started = time.monotonic()
            read_ok, frame = capture.read()
            read_seconds = time.monotonic() - read_started
            if stop_event.is_set():
                break

            if not read_ok or frame is None:
                failure_started = failure_started or read_started
                consecutive_read_failures += 1
                now = time.monotonic()
                if last_success is None:
                    failure_seconds = now - capture_started
                    failure_limit = config["startup_grace_seconds"]
                else:
                    failure_seconds = now - last_success
                    failure_limit = config["read_failure_grace_seconds"]
                message = _failure_message(
                    capture,
                    config,
                    read_ok=read_ok,
                    frame_is_none=frame is None,
                    read_seconds=read_seconds,
                    failure_seconds=failure_seconds,
                )
                if consecutive_read_failures == 1:
                    LOG.warning(
                        "%s Retrying for up to %.2fs before recovery.",
                        message,
                        failure_limit,
                    )
                if failure_seconds >= failure_limit:
                    raise RuntimeError(message)
                stop_event.wait(config["read_retry_delay_seconds"])
                continue

            if consecutive_read_failures:
                LOG.info(
                    "Camera frame delivery recovered without reopen after %d "
                    "failed reads and %.3fs",
                    consecutive_read_failures,
                    time.monotonic() - failure_started,
                )
            failure_started = None
            consecutive_read_failures = 0
            last_success = time.monotonic()
            stable_id = config.get("stable_id")
            if stable_id is not None:
                identities = identity_discovery()
                if not any(
                    item.get("stable_id") == stable_id
                    and item.get("runtime_source") == source
                    for item in identities
                ):
                    raise StableIdentityMismatchError(
                        "Configured camera identity no longer maps to this "
                        f"AVFoundation index: stable_identity={stable_id!r} "
                        f"resolved_index={source!r}."
                    )
            raw_height, raw_width = frame.shape[:2]
            frame = _rotate_frame(frame, config["rotation"])
            encoded_ok, encoded = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80]
            )
            if not encoded_ok:
                raise RuntimeError("Unable to encode the camera frame.")
            sequence += 1
            delivered_frame = True
            _put_latest(
                output_queue,
                {
                    "type": "frame",
                    "device_id": config.get("device_id"),
                    "jpeg": encoded.tobytes(),
                    "sequence": sequence,
                    "width": frame.shape[1],
                    "height": frame.shape[0],
                    "raw_width": raw_width,
                    "raw_height": raw_height,
                    "negotiated_capture": negotiated,
                },
            )
            elapsed = time.monotonic() - iteration_started
            fps_value = config.get("fps_value")
            target_fps = fps_value.value if fps_value is not None else config["fps"]
            stop_event.wait(max(0, 1 / target_fps - elapsed))
    except Exception as exc:
        log_event(
            LOG,
            logging.ERROR,
            "camera",
            "camera_worker_exited",
            device_id=config.get("device_id"),
            camera_id=config.get("stable_id") or config.get("camera_name"),
            error_code="camera_worker_exited",
            exception_type=type(exc).__name__,
        )
        message = {
            "type": "error",
            "error": str(exc),
            "opened": delivered_frame,
        }
        if isinstance(exc, StableIdentityMismatchError):
            message["error_code"] = STABLE_IDENTITY_MISMATCH
        _put_latest(output_queue, message)
    finally:
        if capture is not None:
            capture.release()


class CameraCaptureWorker:
    """Parent-side lifecycle wrapper for one spawned AVFoundation process."""

    def __init__(self, config, *, context=None, target=capture_worker_main):
        self.config = dict(config)
        log_queue = current_logging_queue()
        if log_queue is not None:
            self.config["_diagnostic_log_queue"] = log_queue
        self.context = context or multiprocessing.get_context("spawn")
        self.fps_value = self.context.Value("d", float(self.config["fps"]))
        self.config["fps_value"] = self.fps_value
        self.output_queue = self.context.Queue(maxsize=1)
        self.stop_event = self.context.Event()
        self.process = self.context.Process(
            target=target,
            args=(self.output_queue, self.stop_event, self.config),
            name=f"camera-capture-{self.config['source']}",
            daemon=True,
        )
        self._closed = False

    @property
    def pid(self):
        return self.process.pid

    @property
    def exitcode(self):
        return self.process.exitcode

    def start(self):
        self.process.start()

    def is_alive(self):
        return self.process.is_alive()

    def receive(self, timeout=0.1):
        try:
            return self.output_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def request_stop(self):
        self.stop_event.set()

    def update_fps(self, fps):
        self.fps_value.value = float(fps)

    def close(self, timeout=1.0):
        if self._closed:
            return
        self._closed = True
        self.request_stop()
        phase_timeout = max(0.05, timeout / 3)
        self.process.join(phase_timeout)
        if self.process.is_alive():
            LOG.warning("Terminating unresponsive camera worker pid=%s", self.pid)
            self.process.terminate()
            self.process.join(phase_timeout)
        if self.process.is_alive():
            LOG.error("Killing unresponsive camera worker pid=%s", self.pid)
            self.process.kill()
            self.process.join(phase_timeout)
        if self.process.is_alive():
            raise RuntimeError(
                f"Camera worker pid={self.pid} could not be stopped safely."
            )
        self.output_queue.close()
        self.output_queue.join_thread()
