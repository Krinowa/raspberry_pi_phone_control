"""Persistent logical-device to external-camera routing."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from camera import Camera, rotate_frame
from hardware.device_config import DEFAULT_DEVICE_ID, validate_device_id

DEFAULT_CAMERA_CONFIG_PATH = (
    Path(__file__).resolve().parent / "automation" / "camera_data" / "cameras.json"
)
ROTATIONS = {0, 90, 180, 270}


class CameraManagerError(RuntimeError):
    pass


def normalize_camera_source(value):
    if type(value) is int:
        if value < 0:
            raise ValueError("camera_source must be a non-negative index or URL.")
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value or len(value) > 2048 or any(ord(char) < 32 for char in value):
            raise ValueError("camera_source must be a non-empty index or URL.")
        if value.isdecimal():
            return int(value)
        return value
    raise ValueError("camera_source must be a non-negative index or URL.")


@dataclass(frozen=True)
class CameraConfig:
    device_id: str
    camera_source: int | str | None
    rotation: int = 0
    enabled: bool = True
    display_name: str | None = None

    def __post_init__(self):
        validate_device_id(self.device_id)
        if self.camera_source is not None:
            object.__setattr__(
                self, "camera_source", normalize_camera_source(self.camera_source)
            )
        if type(self.rotation) is not int or self.rotation not in ROTATIONS:
            raise ValueError("rotation must be 0, 90, 180, or 270.")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be true or false.")
        if self.display_name is not None and (
            not isinstance(self.display_name, str)
            or not self.display_name.strip()
            or len(self.display_name) > 160
            or any(ord(char) < 32 for char in self.display_name)
        ):
            raise ValueError("display_name must be a non-empty string when provided.")

    def public(self):
        return asdict(self)


class CameraConfigStorage:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_CAMERA_CONFIG_PATH)
        self.lock = threading.RLock()

    def load(self):
        with self.lock:
            if not self.path.exists():
                return {}
            if self.path.is_symlink():
                raise OSError("Camera configuration path cannot be a symlink.")
            if self.path.stat().st_size > 256 * 1024:
                raise ValueError("Camera configuration file is unexpectedly large.")
            with self.path.open(encoding="utf-8") as stream:
                raw = json.load(stream)
        if not isinstance(raw, dict):
            raise ValueError("Camera configuration must be a JSON object.")
        configs = {}
        for device_id, value in raw.items():
            if not isinstance(value, dict):
                raise ValueError(f"Camera configuration for {device_id!r} is invalid.")
            configs[device_id] = CameraConfig(
                device_id=device_id,
                camera_source=value.get("camera_source"),
                rotation=value.get("rotation", 0),
                enabled=value.get("enabled", True),
                display_name=value.get("display_name"),
            )
        return configs

    def save(self, configs):
        data = {}
        for device_id, config in sorted(configs.items()):
            entry = config.public()
            entry.pop("device_id")
            if entry["display_name"] is None:
                entry.pop("display_name")
            data[device_id] = entry
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise OSError("Camera configuration path cannot be a symlink.")
            fd, temporary = tempfile.mkstemp(
                dir=self.path.parent, prefix=".cameras-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, indent=2, allow_nan=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


class UnavailableCamera:
    def __init__(self, config, error):
        self.source = config.camera_source
        self.rotate = config.rotation
        self.error = error

    def status(self):
        return {
            "ready": False,
            "index": self.source,
            "source": None if self.source is None else str(self.source),
            "width": None,
            "height": None,
            "error": self.error,
        }

    def frame(self):
        return None

    def observation(self):
        return None

    def validate_frame(self, frame_id):
        raise ValueError(self.error)

    def close(self):
        return None


class CameraManager:
    """Own reusable Camera instances without coupling them to OCR workers."""

    def __init__(
        self,
        *,
        storage=None,
        default_source=0,
        default_rotation=0,
        width=1280,
        height=720,
        fps=15,
        monitor_fps=3,
        camera_factory=Camera,
        default_camera=None,
        shared_camera=False,
        discovery_cache_seconds=20,
    ):
        self.storage = storage or CameraConfigStorage()
        self.width, self.height = width, height
        self.active_fps, self.monitor_fps = fps, monitor_fps
        self.camera_factory = camera_factory
        self.default_camera = default_camera
        self.shared_camera = shared_camera
        self.lock = threading.RLock()
        self.discovery_lock = threading.Lock()
        self.discovery_cache_seconds = discovery_cache_seconds
        self.discovery_cache = None
        self.discovery_cached_at = 0.0
        self.cameras = {}
        self.last_opened = {}
        self.active_device_id = None
        self.configs = self.storage.load()
        if DEFAULT_DEVICE_ID not in self.configs:
            self.configs[DEFAULT_DEVICE_ID] = CameraConfig(
                DEFAULT_DEVICE_ID, default_source, default_rotation, True
            )
        if default_camera is not None:
            default_config = self.configs[DEFAULT_DEVICE_ID]
            camera_matches_config = (
                getattr(default_camera, "source", None) == default_config.camera_source
                and getattr(default_camera, "rotate", None) == default_config.rotation
            )
            if shared_camera or camera_matches_config:
                self.cameras[DEFAULT_DEVICE_ID] = default_camera
            else:
                # A persisted per-device assignment takes precedence over a
                # legacy Camera constructed from command-line defaults.
                close = getattr(default_camera, "close", None)
                if close is not None:
                    close()

    def config(self, device_id):
        validate_device_id(device_id)
        with self.lock:
            return self.configs.get(device_id, CameraConfig(device_id, None, 0, False))

    def all_configs(self):
        with self.lock:
            return {key: value.public() for key, value in self.configs.items()}

    def _new_camera(self, config, device_id):
        if not config.enabled:
            return UnavailableCamera(config, "No camera is enabled for this phone.")
        if config.camera_source is None:
            return UnavailableCamera(config, "No camera is assigned to this phone.")
        for other_id, other_camera in self.cameras.items():
            if (
                other_id != device_id
                and getattr(other_camera, "source", None) == config.camera_source
            ):
                return UnavailableCamera(
                    config,
                    f"Camera source {config.camera_source!r} is already in use by {other_id}.",
                )
        camera = self.camera_factory(
            config.camera_source,
            self.width,
            self.height,
            (
                self.active_fps
                if device_id == self.active_device_id
                else self.monitor_fps
            ),
            config.rotation,
        )
        camera.start()
        self.last_opened[device_id] = time.monotonic()
        return camera

    def get_camera(self, device_id, *, open_camera=True):
        config = self.config(device_id)
        with self.lock:
            camera = self.cameras.get(device_id)
            thread = getattr(camera, "thread", None)
            if (
                camera is not None
                and thread is not None
                and not thread.is_alive()
                and time.monotonic() - self.last_opened.get(device_id, 0) >= 3
            ):
                camera.start()
                self.last_opened[device_id] = time.monotonic()
            if camera is None:
                if not open_camera:
                    return None
                camera = self._new_camera(config, device_id)
                self.cameras[device_id] = camera
            return camera

    def activate(self, device_id):
        validate_device_id(device_id)
        with self.lock:
            if self.shared_camera and self.default_camera is not None:
                self.active_device_id = device_id
                self.cameras[device_id] = self.default_camera
                return self.default_camera
            previous = self.active_device_id
            if previous != device_id and previous is not None:
                previous_camera = self.cameras.get(previous)
                if previous_camera is not None and hasattr(previous_camera, "fps"):
                    previous_camera.fps = self.monitor_fps
            self.active_device_id = device_id
            camera = self.get_camera(device_id)
            if hasattr(camera, "fps"):
                camera.fps = self.active_fps
            return camera

    def update(self, device_id, *, camera_source, rotation, enabled, display_name=None):
        config = CameraConfig(device_id, camera_source, rotation, enabled, display_name)
        with self.lock:
            previous = self.configs.get(device_id)
            if previous != config:
                if config.enabled and config.camera_source is not None:
                    duplicate = next(
                        (
                            other_id
                            for other_id, other in self.configs.items()
                            if other_id != device_id
                            and other.enabled
                            and other.camera_source == config.camera_source
                        ),
                        None,
                    )
                    if duplicate is not None:
                        raise ValueError(
                            f"Camera source {config.camera_source!r} is already assigned to {duplicate}."
                        )
                updated = dict(self.configs)
                updated[device_id] = config
                try:
                    self.storage.save(updated)
                except (OSError, TypeError, ValueError) as exc:
                    raise CameraManagerError(
                        "Camera settings could not be saved to local storage."
                    ) from exc
                self._release_locked(device_id)
                self.configs[device_id] = config
            return config

    def _release_locked(self, device_id):
        camera = self.cameras.pop(device_id, None)
        self.last_opened.pop(device_id, None)
        if camera is not None and camera not in self.cameras.values():
            close = getattr(camera, "close", None)
            if close is not None:
                close()

    def release_device(self, device_id):
        with self.lock:
            self._release_locked(device_id)

    def release_all(self):
        with self.lock:
            cameras = {id(camera): camera for camera in self.cameras.values()}
            self.cameras.clear()
            self.last_opened.clear()
            for camera in cameras.values():
                close = getattr(camera, "close", None)
                if close is not None:
                    close()

    def status(self, device_id, *, open_camera=True):
        config = self.config(device_id)
        camera = self.get_camera(device_id, open_camera=open_camera)
        status = (
            camera.status()
            if camera is not None
            else {
                "ready": False,
                "source": (
                    None if config.camera_source is None else str(config.camera_source)
                ),
                "width": None,
                "height": None,
                "error": "Camera has not been opened.",
            }
        )
        return {"config": config.public(), "status": status}

    def is_current(self, device_id, camera):
        with self.lock:
            return self.cameras.get(device_id) is camera

    @staticmethod
    def _rotate_frame(frame, rotation):
        return rotate_frame(frame, rotation)

    def test_source(self, source, rotation=0):
        source = normalize_camera_source(source)
        if type(rotation) is not int or rotation not in ROTATIONS:
            raise ValueError("rotation must be 0, 90, 180, or 270.")
        with self.lock:
            for device_id, camera in self.cameras.items():
                if getattr(camera, "source", None) != source:
                    continue
                current = camera.frame()
                if current is None:
                    return {
                        "available": False,
                        "error": camera.status().get("error")
                        or "Camera frame unavailable.",
                    }, None
                jpeg, _, width, height = current
                current_rotation = getattr(camera, "rotate", 0)
                adjustment = (rotation - current_rotation) % 360
                if adjustment:
                    image = cv2.imdecode(
                        np.frombuffer(jpeg, np.uint8),
                        cv2.IMREAD_COLOR,
                    )
                    if image is None:
                        raise CameraManagerError(
                            "The current camera frame could not be decoded."
                        )
                    image = self._rotate_frame(image, adjustment)
                    ok, encoded = cv2.imencode(".jpg", image)
                    if not ok:
                        raise CameraManagerError(
                            "The test camera frame could not be encoded."
                        )
                    jpeg = encoded.tobytes()
                    height, width = image.shape[:2]
                return {
                    "available": True,
                    "device_id": device_id,
                    "width": width,
                    "height": height,
                }, jpeg

            backend = cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY
            capture = None
            try:
                capture = (
                    cv2.VideoCapture(source, backend)
                    if type(source) is int
                    else cv2.VideoCapture(source)
                )
                if not capture.isOpened():
                    return {
                        "available": False,
                        "error": f"Cannot open camera source {source!r}.",
                    }, None
                ok, frame = capture.read()
                if not ok or frame is None:
                    return {
                        "available": False,
                        "error": f"Camera source {source!r} did not return a frame.",
                    }, None
                frame = self._rotate_frame(frame, rotation)
                ok, encoded = cv2.imencode(".jpg", frame)
                if not ok:
                    raise CameraManagerError(
                        "The test camera frame could not be encoded."
                    )
                height, width = frame.shape[:2]
                return {
                    "available": True,
                    "width": width,
                    "height": height,
                }, encoded.tobytes()
            except CameraManagerError:
                raise
            except Exception as exc:
                return {
                    "available": False,
                    "error": f"Cannot test camera source {source!r}: {exc}.",
                }, None
            finally:
                if capture is not None:
                    capture.release()

    def discover(self, first=0, last=10, *, force=False):
        with self.discovery_lock:
            now = time.monotonic()
            if (
                not force
                and self.discovery_cache is not None
                and now - self.discovery_cached_at < self.discovery_cache_seconds
            ):
                return [dict(item) for item in self.discovery_cache]

            cameras = []
            backend = cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY
            with self.lock:
                opened = {
                    camera.source: camera.status()["ready"]
                    for camera in self.cameras.values()
                    if type(getattr(camera, "source", None)) is int
                }
            for source in range(first, last + 1):
                if source in opened:
                    if opened[source]:
                        cameras.append({"source": source, "available": True})
                    continue
                capture = None
                try:
                    capture = cv2.VideoCapture(source, backend)
                    if capture.isOpened():
                        readable, frame = capture.read()
                        if readable and frame is not None:
                            cameras.append({"source": source, "available": True})
                except Exception:
                    # An invalid index is omitted from discovery. A configured
                    # source is still returned by the per-device camera route.
                    pass
                finally:
                    if capture is not None:
                        capture.release()
            self.discovery_cache = [dict(item) for item in cameras]
            self.discovery_cached_at = time.monotonic()
            return cameras
