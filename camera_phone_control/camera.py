"""External camera capture only. No Android screen capture or image analysis."""

import logging
import sys
import threading
import time
from collections import deque

import cv2

LOG = logging.getLogger("camera")


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
        self.latest = None
        self.sequence = 0
        self.timestamps = deque(maxlen=90)

        self.error = "Camera is starting..."

    def start(self):
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

                ok, frame = cap.read()

                if not ok or frame is None:
                    raise RuntimeError(
                        "Camera stopped delivering frames. "
                        "Check the camera stream/network and restart the server."
                    )

                # Rotate camera image before displaying it.
                if self.rotate == 90:
                    frame = cv2.rotate(
                        frame,
                        cv2.ROTATE_90_CLOCKWISE,
                    )

                elif self.rotate == 180:
                    frame = cv2.rotate(
                        frame,
                        cv2.ROTATE_180,
                    )

                elif self.rotate == 270:
                    frame = cv2.rotate(
                        frame,
                        cv2.ROTATE_90_COUNTERCLOCKWISE,
                    )

                ok, encoded = cv2.imencode(
                    ".jpg",
                    frame,
                    [
                        cv2.IMWRITE_JPEG_QUALITY,
                        80,
                    ],
                )

                if not ok:
                    raise RuntimeError("Unable to encode the camera frame.")

                frame_width = frame.shape[1]
                frame_height = frame.shape[0]

                with self.lock:

                    self.sequence += 1

                    self.latest = (
                        encoded.tobytes(),
                        self.sequence,
                        frame_width,
                        frame_height,
                    )

                    self.timestamps.append(
                        (
                            self.sequence,
                            time.monotonic(),
                            frame_width,
                            frame_height,
                        )
                    )

                    self.error = ""

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
                self.error = str(exc)
                self.latest = None

        finally:

            if cap is not None:
                cap.release()

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

        if self.thread:
            self.thread.join(timeout=2)
