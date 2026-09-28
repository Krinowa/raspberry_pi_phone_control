"""External camera capture only. No Android screen capture or image analysis."""

import logging
import os
import sys
import threading
import time
from collections import deque

import cv2

LOG = logging.getLogger("camera")


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
    ):
        self.source = source
        self.width = width
        self.height = height
        self.fps = fps
        self.rotate = rotate

        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.capture = None
        self.opened_event = threading.Event()
        self.open_succeeded = None
        self.latest = None
        self.raw_frame_width = None
        self.raw_frame_height = None
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

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.opened_event.clear()
        self.open_succeeded = None
        self.thread = threading.Thread(
            target=self._capture,
            daemon=True,
        )

        self.thread.start()

    def _open_capture(self):
        """
        Open either:
        - local camera index, e.g. 0 / 1 / 2
        - HTTP/MJPEG/RTSP URL
        """

        # ----------------------------------------------------
        # Network camera / DroidCam / RTSP
        # ----------------------------------------------------

        if isinstance(self.source, str):
            LOG.info(
                "Opening network camera stream: %s",
                self.source,
            )

            return cv2.VideoCapture(
                self.source,
                cv2.CAP_FFMPEG,
            )

        # ----------------------------------------------------
        # Local camera
        # ----------------------------------------------------

        backend = cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY

        LOG.info(
            "Opening local camera index: %s",
            self.source,
        )

        return cv2.VideoCapture(
            self.source,
            backend,
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

            # These are mainly useful for local cameras.
            # Some network streams ignore them.
            cap.set(
                cv2.CAP_PROP_FRAME_WIDTH,
                self.width,
            )

            cap.set(
                cv2.CAP_PROP_FRAME_HEIGHT,
                self.height,
            )

            cap.set(
                cv2.CAP_PROP_FPS,
                self.fps,
            )

            LOG.info("Camera source opened successfully")

            while not self.stop_event.is_set():

                started = time.monotonic()

                read_started = time.monotonic()
                ok, frame = cap.read()
                read_seconds = time.monotonic() - read_started

                if not ok or frame is None:
                    raise RuntimeError(
                        "Camera stopped delivering frames. "
                        "Check the camera stream/network and restart the server."
                    )

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

                    self.sequence += 1

                    self.latest = (
                        encoded.tobytes(),
                        self.sequence,
                        frame_width,
                        frame_height,
                    )
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

            LOG.error(
                "%s",
                exc,
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
                and (time.monotonic() - self.timestamps[-1][1] < 2)
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
            }

    def frame(self):
        with self.lock:
            return self.latest

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

                if number == frame_id and (time.monotonic() - timestamp < 2):
                    if (
                        width,
                        height,
                    ) == self.latest[2:]:

                        return

            raise ValueError(
                "Displayed camera frame is stale. "
                "Input disabled; wait for live video."
            )

    def close(self):
        self.stop_event.set()

        # Releasing from the controlling thread also unblocks a backend read
        # that has not returned yet. This ensures a reassignment does not open
        # the same physical source while the old AVFoundation handle survives.
        with self.lock:
            capture = self.capture
            self.capture = None
        if capture is not None:
            capture.release()

        if self.thread:
            self.thread.join(timeout=2)
