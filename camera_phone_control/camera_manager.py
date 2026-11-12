"""Persistent logical-device to external-camera routing."""

from __future__ import annotations

import json
import logging
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
from diagnostic_logging import log_event
from hardware.device_config import DEFAULT_DEVICE_ID, validate_device_id
from hardware.camera_identity import (
    CameraIdentityError,
    discover_camera_devices,
    resolve_camera_source,
)

DEFAULT_CAMERA_CONFIG_PATH = (
    Path(__file__).resolve().parent / "automation" / "camera_data" / "cameras.json"
)
ROTATIONS = {0, 90, 180, 270}
CAMERA_REOPEN_DELAY_SECONDS = 0.25
# The capture worker's first-frame grace begins only after the spawned process
# has imported OpenCV, opened AVFoundation, and negotiated a capture mode.
# Leave a bounded margin around that 10-second worker grace.
CAMERA_STARTUP_TIMEOUT_SECONDS = 15.0
CAMERA_RECOVERY_RETRY_SECONDS = 3.0
CAMERA_RECOVERY_MAX_ATTEMPTS = 3
CAMERA_SLOW_RECOVERY_RETRY_SECONDS = 15.0
CAMERA_RECOVERY_RESET_SECONDS = 30.0
AVFOUNDATION_SOURCE_PREFIX = "avfoundation:"
STABLE_CAMERA_PREFIXES = ("avfoundation:", "v4l2:", "v4l2-path:", "dshow:")
LOG = logging.getLogger("camera-manager")


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


def avfoundation_camera_source(unique_id):
    if (
        not isinstance(unique_id, str)
        or not unique_id
        or any(ord(char) < 32 for char in unique_id)
    ):
        raise ValueError("AVFoundation camera unique_id is invalid.")
    return AVFOUNDATION_SOURCE_PREFIX + unique_id


def is_avfoundation_camera_source(source):
    return isinstance(source, str) and source.startswith(AVFOUNDATION_SOURCE_PREFIX)


def is_stable_camera_id(value):
    return isinstance(value, str) and value.startswith(STABLE_CAMERA_PREFIXES)


def normalize_camera_id(value):
    if value is None:
        return None
    if (
        not is_stable_camera_id(value)
        or len(value) > 2048
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("camera_id must be a supported stable camera identity.")
    return value


@dataclass(frozen=True)
class CameraConfig:
    device_id: str
    camera_source: int | str | None
    rotation: int = 0
    enabled: bool = True
    display_name: str | None = None
    camera_id: str | None = None

    def __post_init__(self):
        validate_device_id(self.device_id)
        if self.camera_source is not None:
            object.__setattr__(
                self, "camera_source", normalize_camera_source(self.camera_source)
            )
        object.__setattr__(self, "camera_id", normalize_camera_id(self.camera_id))
        if self.camera_id is None and is_stable_camera_id(self.camera_source):
            object.__setattr__(self, "camera_id", self.camera_source)
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
                camera_id=value.get("camera_id"),
            )
        return configs

    def save(self, configs):
        data = {}
        for device_id, config in sorted(configs.items()):
            entry = config.public()
            entry.pop("device_id")
            if entry["display_name"] is None:
                entry.pop("display_name")
            if entry["camera_id"] is None:
                entry.pop("camera_id")
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
        self.device_id = config.device_id
        self.source = config.camera_id or config.camera_source
        self.configured_source = config.camera_id or config.camera_source
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
        startup_timeout=CAMERA_STARTUP_TIMEOUT_SECONDS,
        recovery_retry_seconds=CAMERA_RECOVERY_RETRY_SECONDS,
        recovery_max_attempts=CAMERA_RECOVERY_MAX_ATTEMPTS,
        slow_recovery_retry_seconds=CAMERA_SLOW_RECOVERY_RETRY_SECONDS,
        recovery_reset_seconds=CAMERA_RECOVERY_RESET_SECONDS,
        identity_discovery=None,
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
        self.identity_signature = None
        self.startup_timeout = startup_timeout
        self.recovery_retry_seconds = recovery_retry_seconds
        self.recovery_max_attempts = recovery_max_attempts
        self.slow_recovery_retry_seconds = slow_recovery_retry_seconds
        self.recovery_reset_seconds = recovery_reset_seconds
        self.identity_discovery = (
            None if identity_discovery is False else identity_discovery
        )
        if identity_discovery is None and (
            sys.platform == "darwin"
            or sys.platform == "win32"
            or sys.platform.startswith("linux")
        ):
            self.identity_discovery = discover_camera_devices
        self.cameras = {}
        self.last_opened = {}
        self.recovery_attempts = {}
        self.disconnected_devices = set()
        self.active_device_id = None
        self.configs = self.storage.load()
        if DEFAULT_DEVICE_ID not in self.configs:
            self.configs[DEFAULT_DEVICE_ID] = CameraConfig(
                DEFAULT_DEVICE_ID, default_source, default_rotation, True
            )
        self.source_users = self._source_users_for(self.configs)
        self._validate_shared_sources(self.configs)
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

    @staticmethod
    def _source_users_for(configs):
        """Return configured camera assignments as source -> logical device IDs."""
        users = {}
        for device_id, config in configs.items():
            assignment = config.camera_id or config.camera_source
            if not config.enabled or assignment is None:
                continue
            users.setdefault(assignment, set()).add(device_id)
        return users

    def _identity_devices(self):
        if self.identity_discovery is None:
            return []
        devices = self.identity_discovery()
        if not isinstance(devices, list):
            raise CameraManagerError("Camera identity discovery returned invalid data.")
        result = []
        seen = set()
        for item in devices:
            if not isinstance(item, dict):
                raise CameraManagerError(
                    "Camera identity discovery returned invalid data."
                )
            # Accept the earlier macOS-only schema for injected tests and
            # callers while exposing only the normalized schema internally.
            if "stable_id" not in item and "unique_id" in item:
                item = {
                    "stable_id": avfoundation_camera_source(item.get("unique_id")),
                    "name": item.get("name"),
                    "backend": "avfoundation",
                    "runtime_source": item.get("index"),
                }
            stable_id = item.get("stable_id")
            name = item.get("name")
            backend = item.get("backend")
            runtime_source = item.get("runtime_source")
            if (
                not is_stable_camera_id(stable_id)
                or not isinstance(name, str)
                or not name
                or not isinstance(backend, str)
                or not backend
                or (
                    runtime_source is not None
                    and not (
                        type(runtime_source) is int
                        and runtime_source >= 0
                        or isinstance(runtime_source, str)
                        and runtime_source
                    )
                )
            ):
                raise CameraManagerError(
                    "Camera identity discovery returned invalid data."
                )
            if stable_id in seen:
                raise CameraManagerError(
                    f"Camera identity {stable_id!r} is ambiguous; refusing to choose a source."
                )
            seen.add(stable_id)
            result.append(dict(item))
        signature = tuple(
            sorted(
                (
                    item["stable_id"],
                    item["backend"],
                    repr(item["runtime_source"]),
                )
                for item in result
            )
        )
        previous_signature = self.identity_signature
        self.identity_signature = signature
        if previous_signature is not None and signature != previous_signature:
            previous = {item[0]: item[2] for item in previous_signature}
            current = {item[0]: item[2] for item in signature}
            log_event(
                LOG,
                logging.INFO,
                "camera",
                "camera_discovery_changed",
                message="Operating-system camera discovery changed.",
                previous_camera_count=len(previous),
                camera_count=len(current),
                added_count=len(current.keys() - previous.keys()),
                removed_count=len(previous.keys() - current.keys()),
                remapped_count=sum(
                    1
                    for stable_id in current.keys() & previous.keys()
                    if current[stable_id] != previous[stable_id]
                ),
            )
            self._invalidate_discovery_cache()
        return result

    def _invalidate_discovery_cache(self):
        """Discard picker results after a camera connectivity transition."""
        self.discovery_cache = None
        self.discovery_cached_at = 0.0

    @staticmethod
    def _identity_backend(stable_id):
        if str(stable_id).startswith("avfoundation:"):
            return "avfoundation"
        if str(stable_id).startswith("dshow:"):
            return "dshow"
        return "v4l2"

    def _configured_identity_placeholders(self, identities):
        """Keep configured stable cameras visible while the OS is not listing them."""
        known = {item["stable_id"] for item in identities}
        placeholders = []
        for config in self.configs.values():
            stable_id = config.camera_id or config.camera_source
            if not is_stable_camera_id(stable_id) or stable_id in known:
                continue
            known.add(stable_id)
            placeholders.append(
                {
                    "stable_id": stable_id,
                    "runtime_source": None,
                    "name": config.display_name or stable_id,
                    "backend": self._identity_backend(stable_id),
                }
            )
        return placeholders

    def _record_camera_disconnected(self, device_id, configured_source, reason):
        if device_id in self.disconnected_devices:
            return
        self.disconnected_devices.add(device_id)
        log_event(
            LOG,
            logging.WARNING,
            "camera",
            "camera_disconnected",
            message="Configured camera is not available from operating-system discovery.",
            device_id=device_id,
            camera_id=configured_source,
            error_code=reason,
        )

    def _record_camera_rediscovered(self, config, runtime_source):
        if config.device_id not in self.disconnected_devices:
            return
        self.disconnected_devices.discard(config.device_id)
        log_event(
            LOG,
            logging.INFO,
            "camera",
            "camera_rediscovered",
            message="Configured camera was rediscovered and will be reopened.",
            device_id=config.device_id,
            camera_id=config.camera_id or config.camera_source,
            runtime_source=runtime_source,
        )

    def _resolve_source(self, source, identities=None):
        if not is_stable_camera_id(source):
            return source, None
        identities = self._identity_devices() if identities is None else identities
        try:
            runtime_source = resolve_camera_source(source, devices=identities)
        except CameraIdentityError as exc:
            matches = [item for item in identities if item["stable_id"] == source]
            identity = dict(matches[0]) if len(matches) == 1 else None
            if identity is not None:
                identity["resolution_error"] = str(exc)
            return None, identity
        identity = next(item for item in identities if item["stable_id"] == source)
        return runtime_source, identity

    def _identity_still_matches(self, stable_id, runtime_source):
        """Fail closed if a live AVFoundation index changes physical identity."""
        # Do not acquire the assignment lock here. During enable/reassign the
        # manager deliberately holds that lock while it waits for the first
        # frame, and the capture thread calls this validator before publishing
        # that frame. Taking the lock here would deadlock startup until the
        # manager timeout and then make rollback race the still-blocked thread.
        # Discovery has its own synchronization and this check reads no mutable
        # assignment state.
        identities = self._identity_devices()
        resolved_source, _ = self._resolve_source(stable_id, identities)
        return resolved_source == runtime_source

    def _validate_shared_sources(self, configs, identities=None):
        """Allow camera sharing, but require one rotation per physical source.

        A Camera object rotates the captured frame before publishing it. Two
        logical phones can therefore share one live Camera only when they use
        the same rotation. Their four-corner calibrations remain independent.
        """
        configured_rotations = {}
        for device_id, config in configs.items():
            configured = config.camera_id or config.camera_source
            if not config.enabled or configured is None:
                continue
            previous = configured_rotations.get(configured)
            if previous is not None and previous[1] != config.rotation:
                previous_device, previous_rotation = previous
                raise ValueError(
                    f"Phones sharing camera source {configured!r} must use the "
                    f"same rotation. {previous_device} uses {previous_rotation}°, "
                    f"{device_id} uses {config.rotation}°."
                )
            configured_rotations.setdefault(configured, (device_id, config.rotation))

        requires_identity = any(
            config.enabled
            and is_stable_camera_id(config.camera_id or config.camera_source)
            for config in configs.values()
        )
        identities = (
            self._identity_devices()
            if identities is None and requires_identity
            else identities or []
        )

        runtime_rotations = {}
        for device_id, config in configs.items():
            configured = config.camera_id or config.camera_source
            if not config.enabled or configured is None:
                continue

            runtime_source, _ = self._resolve_source(configured, identities)
            if runtime_source is None:
                continue

            previous = runtime_rotations.get(runtime_source)
            if previous is not None and previous[1] != config.rotation:
                previous_device, previous_rotation = previous
                raise ValueError(
                    f"Phones sharing physical camera {runtime_source!r} must use "
                    f"the same rotation. {previous_device} uses "
                    f"{previous_rotation}°, {device_id} uses {config.rotation}°."
                )
            runtime_rotations.setdefault(runtime_source, (device_id, config.rotation))

    def _device_ids_for_camera(self, camera):
        return [
            device_id
            for device_id, assigned in self.cameras.items()
            if assigned is camera
        ]

    def _close_shared_camera_locked(self, camera, *, preserve_recovery_state=False):
        """Close one physical Camera and remove every logical alias to it.

        Closing a shared capture for only one device would leave the other
        device mappings pointing at a dead Camera object. Remove all aliases
        only after close succeeds so a close failure preserves the authoritative
        mappings. Other logical devices reopen lazily and reuse the replacement.
        """
        device_ids = self._device_ids_for_camera(camera)
        close = getattr(camera, "close", None)
        if close is not None:
            close()
        self._invalidate_discovery_cache()

        for device_id in device_ids:
            if self.cameras.get(device_id) is camera:
                self.cameras.pop(device_id, None)
            self.last_opened.pop(device_id, None)
            if not preserve_recovery_state:
                self.recovery_attempts.pop(device_id, None)
        return device_ids

    def _find_shared_camera(self, configured_source, runtime_source, rotation):
        stable_identity = is_stable_camera_id(configured_source)
        for camera in self.cameras.values():
            if isinstance(camera, UnavailableCamera):
                continue
            if getattr(camera, "identity_mismatch", False):
                continue
            source_matches = (
                getattr(camera, "configured_source", None) == configured_source
                if stable_identity
                else getattr(camera, "source", None) == runtime_source
            )
            if source_matches and getattr(camera, "rotate", None) == rotation:
                return camera
        return None

    def _new_camera(self, config, device_id, *, identities=None):
        if not config.enabled:
            return UnavailableCamera(config, "No camera is enabled for this phone.")
        configured = config.camera_id or config.camera_source
        if configured is None:
            return UnavailableCamera(config, "No camera is assigned to this phone.")
        identities = (
            self._identity_devices()
            if identities is None and is_stable_camera_id(configured)
            else identities or []
        )
        runtime_source, identity = self._resolve_source(configured, identities)
        if runtime_source is None:
            self._record_camera_disconnected(
                device_id,
                configured,
                "camera_identity_unavailable",
            )
            self.last_opened[device_id] = time.monotonic()
            return UnavailableCamera(
                config,
                (
                    identity.get("resolution_error")
                    if identity is not None and identity.get("resolution_error")
                    else f"Configured camera {config.display_name or configured!r} is disconnected."
                ),
            )

        self._record_camera_rediscovered(config, runtime_source)

        shared = self._find_shared_camera(configured, runtime_source, config.rotation)

        if shared is not None:
            LOG.info(
                "Reusing camera source=%r for device_id=%s",
                runtime_source,
                device_id,
            )
            self.last_opened[device_id] = time.monotonic()
            return shared

        if is_stable_camera_id(configured):
            # Runtime indexes can be reassigned when USB cameras reconnect.
            # Never open a second capture on an index still held by a different
            # stable identity; retire that stale mapping first and let its
            # logical users reopen lazily against their own stable identity.
            runtime_conflicts = {
                id(camera): camera
                for camera in self.cameras.values()
                if not isinstance(camera, UnavailableCamera)
                and getattr(camera, "source", None) == runtime_source
                and getattr(camera, "configured_source", None) != configured
            }
            for conflict in runtime_conflicts.values():
                LOG.warning(
                    "Closing stale camera runtime mapping source=%r "
                    "configured_identity=%r replacement_identity=%r",
                    runtime_source,
                    getattr(conflict, "configured_source", None),
                    configured,
                )
                self._close_shared_camera_locked(conflict)

        camera = self.camera_factory(
            runtime_source,
            self.width,
            self.height,
            (
                self.active_fps
                if device_id == self.active_device_id
                else self.monitor_fps
            ),
            config.rotation,
        )
        camera.device_id = device_id
        camera.configured_source = configured
        camera.camera_name = identity["name"] if identity else config.display_name
        camera.camera_id = config.camera_id
        camera.capture_backend = identity["backend"] if identity else None
        if is_avfoundation_camera_source(configured):
            camera.identity_validator = lambda: self._identity_still_matches(
                configured, runtime_source
            )
        camera.start()
        self.last_opened[device_id] = time.monotonic()
        return camera

    def get_camera(self, device_id, *, open_camera=True):
        config = self.config(device_id)
        with self.lock:
            camera = self.cameras.get(device_id)
            configured = config.camera_id or config.camera_source
            identity_mismatch_transitioned = False

            if (
                is_avfoundation_camera_source(configured)
                and camera is not None
                and not isinstance(camera, UnavailableCamera)
                and getattr(camera, "identity_mismatch", False)
            ):
                camera_status = camera.status()
                error = camera_status.get("error") or (
                    f"Configured camera {config.display_name or configured!r} "
                    "is disconnected."
                )
                LOG.warning(
                    "Configured AVFoundation camera became unavailable "
                    "device_id=%s stable_identity=%r previous_index=%r",
                    device_id,
                    configured,
                    getattr(camera, "source", None),
                )
                self._record_camera_disconnected(
                    device_id,
                    configured,
                    "camera_identity_mismatch",
                )
                self._close_shared_camera_locked(camera)
                camera = UnavailableCamera(config, error)
                self.cameras[device_id] = camera
                self.last_opened[device_id] = time.monotonic()
                identity_mismatch_transitioned = True

            if (
                is_avfoundation_camera_source(configured)
                and camera is not None
                and not isinstance(camera, UnavailableCamera)
            ):
                identities = self._identity_devices()
                resolved_source, _ = self._resolve_source(configured, identities)
                current_source = getattr(camera, "source", None)
                if resolved_source != current_source:
                    LOG.warning(
                        "Stopping camera whose AVFoundation runtime mapping changed "
                        "device_id=%s stable_identity=%r previous_index=%r "
                        "resolved_index=%r",
                        device_id,
                        configured,
                        current_source,
                        resolved_source,
                    )
                    self._close_shared_camera_locked(camera)
                    time.sleep(CAMERA_REOPEN_DELAY_SECONDS)
                    camera = self._new_camera(
                        config,
                        device_id,
                        identities=identities,
                    )
                    self.cameras[device_id] = camera

            thread = getattr(camera, "thread", None)
            camera_status = camera.status() if camera is not None else None
            if camera_status is not None and camera_status.get("ready"):
                recovered_device_ids = self._device_ids_for_camera(camera) or [
                    device_id
                ]
                recovered = any(
                    recovered_device_id in self.recovery_attempts
                    for recovered_device_id in recovered_device_ids
                )
                for recovered_device_id in recovered_device_ids:
                    self.recovery_attempts.pop(recovered_device_id, None)
                if recovered:
                    self._invalidate_discovery_cache()
                    LOG.info(
                        "Camera recovery complete device_ids=%s source=%r",
                        sorted(recovered_device_ids),
                        getattr(camera, "source", None),
                    )

            if (
                is_stable_camera_id(config.camera_id or config.camera_source)
                and isinstance(camera, UnavailableCamera)
                and not identity_mismatch_transitioned
                and time.monotonic() - self.last_opened.get(device_id, 0)
                >= self.slow_recovery_retry_seconds
            ):
                self.cameras.pop(device_id, None)
                camera = self._new_camera(config, device_id)
                self.cameras[device_id] = camera
                thread = getattr(camera, "thread", None)
                camera_status = camera.status()

            open_succeeded = getattr(camera, "open_succeeded", None)
            camera_unhealthy = (
                camera_status is not None
                and open_succeeded is True
                and not camera_status.get("ready", False)
            )
            camera_failed_to_open = open_succeeded is False
            thread_dead = thread is not None and not thread.is_alive()
            recovery_attempts = self.recovery_attempts.get(device_id, 0)
            recovery_delay = (
                self.recovery_retry_seconds
                if recovery_attempts < self.recovery_max_attempts
                else self.slow_recovery_retry_seconds
            )

            if (
                camera is not None
                and not isinstance(camera, UnavailableCamera)
                and (camera_unhealthy or camera_failed_to_open or thread_dead)
                and time.monotonic() - self.last_opened.get(device_id, 0)
                >= recovery_delay
            ):
                if recovery_attempts < self.recovery_max_attempts:
                    self.recovery_attempts[device_id] = recovery_attempts + 1
                else:
                    LOG.info(
                        "Camera remains unavailable; starting slow recovery retry "
                        "device_id=%s source=%r retry_seconds=%s",
                        device_id,
                        configured,
                        self.slow_recovery_retry_seconds,
                    )
                if is_stable_camera_id(config.camera_id or config.camera_source):
                    self._close_shared_camera_locked(
                        camera, preserve_recovery_state=True
                    )
                    time.sleep(CAMERA_REOPEN_DELAY_SECONDS)
                    camera = self._new_camera(config, device_id)
                    self.cameras[device_id] = camera
                else:
                    camera.start()
                    opened_at = time.monotonic()
                    for shared_device_id in self._device_ids_for_camera(camera):
                        self.last_opened[shared_device_id] = opened_at

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

    def reassign(
        self,
        device_id,
        *,
        camera_source,
        rotation,
        enabled,
        display_name=None,
        camera_id=None,
    ):
        """Apply one logical-device camera assignment atomically.

        Multiple logical phones may reference the same physical camera. The
        physical capture is reused when the resolved source and rotation match.
        Each phone keeps its own logical configuration and calibration.
        """
        config = CameraConfig(
            device_id, camera_source, rotation, enabled, display_name, camera_id
        )

        with self.lock:
            previous = self.configs.get(
                device_id, CameraConfig(device_id, None, 0, False)
            )
            if previous == config:
                return config, set()

            self.disconnected_devices.discard(device_id)
            self._invalidate_discovery_cache()

            # A metadata-only save does not change the physical assignment.
            if (
                previous.camera_source == config.camera_source
                and previous.camera_id == config.camera_id
                and previous.rotation == config.rotation
                and previous.enabled == config.enabled
            ):
                updated = dict(self.configs)
                updated[device_id] = config
                self.storage.save(updated)
                self.configs = updated
                self.source_users = self._source_users_for(updated)
                return config, set()

            updated = dict(self.configs)
            updated[device_id] = config
            affected = {device_id}

            # Sharing is valid, but one physical Camera publishes one rotated
            # frame, so every logical user of that capture must agree on rotation.
            self._validate_shared_sources(updated)
            new_users = self._source_users_for(updated)

            LOG.info(
                "Camera reassignment device_id=%s old_source=%r new_source=%r "
                "shared_users=%s",
                device_id,
                previous.camera_id or previous.camera_source,
                config.camera_id or config.camera_source,
                sorted(new_users.get(config.camera_id or config.camera_source, set())),
            )

            old_configs = self.configs
            old_users = self.source_users
            reopen = {
                device_id
                for device_id in affected
                if device_id in self.cameras or device_id == self.active_device_id
            }

            released = False
            released_devices = set()
            try:
                for affected_id in sorted(affected):
                    if self._release_locked(affected_id):
                        released = True
                        released_devices.add(affected_id)
            except Exception as exc:
                try:
                    self._open_devices_locked(released_devices & reopen)
                except Exception as rollback_exc:
                    LOG.exception(
                        "Failed to restore cameras after capture shutdown failed"
                    )
                    raise CameraManagerError(
                        "Camera change failed, and the previous camera assignments "
                        "could not be reopened. Check the camera connections."
                    ) from rollback_exc
                raise CameraManagerError(
                    "Camera could not be stopped safely; previous assignments "
                    "were restored."
                ) from exc

            if released:
                # Some camera backends retain an exclusive handle briefly after
                # the final logical user releases the physical capture.
                time.sleep(CAMERA_REOPEN_DELAY_SECONDS)

            self.configs = updated
            self.source_users = new_users

            try:
                self._open_devices_locked(reopen)
                self.storage.save(updated)
            except Exception as exc:
                LOG.exception(
                    "Camera reassignment failed; restoring previous assignments "
                    "device_id=%s old_source=%r new_source=%r",
                    device_id,
                    previous.camera_source,
                    config.camera_source,
                )

                rollback_released = False
                try:
                    for affected_id in sorted(affected):
                        rollback_released = (
                            self._release_locked(affected_id) or rollback_released
                        )
                except Exception as rollback_stop_exc:
                    LOG.exception("Failed to stop changed camera while rolling back")
                    raise CameraManagerError(
                        "Camera change failed, and rollback could not safely stop "
                        "the changed camera. Check the camera connections."
                    ) from rollback_stop_exc

                self.configs = old_configs
                self.source_users = old_users

                if rollback_released:
                    time.sleep(CAMERA_REOPEN_DELAY_SECONDS)

                try:
                    self._open_devices_locked(reopen)
                except Exception as rollback_exc:
                    LOG.exception("Failed to reopen a camera while rolling back")
                    raise CameraManagerError(
                        "Camera change failed, and the previous camera assignments "
                        "could not be reopened. Check the camera connections."
                    ) from rollback_exc

                if isinstance(exc, (OSError, TypeError, ValueError)):
                    raise CameraManagerError(
                        "Camera settings could not be applied; previous assignments were restored."
                    ) from exc
                raise CameraManagerError(
                    "Camera could not be reopened; previous assignments were restored."
                ) from exc

            LOG.info(
                "Camera reassignment complete device_id=%s old_source=%r "
                "new_source=%r affected=%s users=%s",
                device_id,
                previous.camera_source,
                config.camera_source,
                sorted(affected),
                {key: sorted(value) for key, value in self.source_users.items()},
            )
            return self.configs[device_id], affected

    def update(
        self,
        device_id,
        *,
        camera_source,
        rotation,
        enabled,
        display_name=None,
        camera_id=None,
    ):
        config, _ = self.reassign(
            device_id,
            camera_source=camera_source,
            rotation=rotation,
            enabled=enabled,
            display_name=display_name,
            camera_id=camera_id,
        )
        return config

    def _open_devices_locked(self, device_ids):
        for device_id in sorted(device_ids):
            config = self.configs.get(device_id)
            if config is None:
                continue
            LOG.info(
                "Reopening camera device_id=%s source=%r",
                device_id,
                config.camera_source,
            )
            camera = self._new_camera(config, device_id)
            self.cameras[device_id] = camera
            self._wait_for_camera_startup(camera, config)
            LOG.info(
                "Camera reopen result device_id=%s source=%r ready=%s error=%s",
                device_id,
                config.camera_source,
                camera.status().get("ready"),
                camera.status().get("error"),
            )

    def _wait_for_camera_startup(self, camera, config):
        """Wait for a first valid frame while distinguishing STARTING from FAILED."""
        wait_until_opened = getattr(camera, "wait_until_opened", None)
        if wait_until_opened is None:
            return

        deadline = time.monotonic() + self.startup_timeout
        first_wait = True
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            wait_timeout = self.startup_timeout if first_wait else remaining
            first_wait = False
            opened = wait_until_opened(timeout=wait_timeout)
            status = camera.status()
            open_succeeded = getattr(camera, "open_succeeded", opened)
            status_error = status.get("error")
            if status_error == "Camera is starting...":
                status_error = ""

            # Camera publishes its first frame and open_succeeded under the same
            # lock. Re-read status after the wait so a frame arriving at the
            # deadline is accepted instead of being rolled back.
            if status.get("ready") and open_succeeded is not False:
                return

            if getattr(camera, "identity_mismatch", False):
                raise CameraManagerError(
                    status_error
                    or "The configured camera identity changed while opening."
                )
            if opened is False or open_succeeded is False:
                raise CameraManagerError(
                    status_error
                    or f"Cannot open camera source {config.camera_source!r}."
                )

            thread = getattr(camera, "thread", None)
            if thread is not None and not thread.is_alive():
                raise CameraManagerError(
                    status_error
                    or "The camera capture worker exited before its first frame."
                )

            if time.monotonic() >= deadline:
                raise CameraManagerError(
                    "Timed out waiting for the camera's first valid frame."
                )

            # Compatibility with test/fake camera implementations whose wait
            # method can return immediately while they remain STARTING.
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    def _release_locked(self, device_id):
        camera = self.cameras.pop(device_id, None)
        self.last_opened.pop(device_id, None)
        if camera is not None and camera not in self.cameras.values():
            LOG.info(
                "Releasing camera device_id=%s source=%r",
                device_id,
                getattr(camera, "source", None),
            )
            close = getattr(camera, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    # Preserve the authoritative mapping when the old capture
                    # thread still owns its source. Opening a replacement now
                    # could overlap VideoCapture handles.
                    self.cameras[device_id] = camera
                    raise
            self.recovery_attempts.pop(device_id, None)
            self._invalidate_discovery_cache()
            return True
        return False

    def release_device(self, device_id):
        with self.lock:
            self._release_locked(device_id)
            self.disconnected_devices.discard(device_id)

    def release_all(self):
        with self.lock:
            cameras = {id(camera): camera for camera in self.cameras.values()}
            self.cameras.clear()
            self.last_opened.clear()
            self.recovery_attempts.clear()
            self.disconnected_devices.clear()
            self._invalidate_discovery_cache()
            for camera in cameras.values():
                close = getattr(camera, "close", None)
                if close is not None:
                    close()

    def status(self, device_id, *, open_camera=True):
        with self.lock:
            config = self.config(device_id)
            camera = self.get_camera(device_id, open_camera=open_camera)
            status = (
                camera.status()
                if camera is not None
                else {
                    "ready": False,
                    "source": (
                        None
                        if (config.camera_id or config.camera_source) is None
                        else str(config.camera_id or config.camera_source)
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
            identity = None
            runtime_source = source
            if is_stable_camera_id(source):
                runtime_source, identity = self._resolve_source(source)
                if runtime_source is None:
                    return {
                        "available": False,
                        "source": source,
                        "error": "The configured camera is disconnected.",
                    }, None
            for device_id, camera in self.cameras.items():
                source_matches = (
                    getattr(camera, "configured_source", None) == source
                    if identity is not None
                    else getattr(camera, "source", None) == runtime_source
                )
                if not source_matches:
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
                result = {
                    "available": True,
                    "device_id": device_id,
                    "width": width,
                    "height": height,
                }
                if identity is not None:
                    result.update(
                        source=source,
                        stable_id=source,
                        runtime_source=runtime_source,
                        index=runtime_source,
                        backend=identity["backend"],
                        name=identity["name"],
                    )
                return result, jpeg

            if identity is not None and identity["backend"] == "dshow":
                backend = cv2.CAP_DSHOW
            elif identity is not None and identity["backend"] == "v4l2":
                backend = cv2.CAP_V4L2
            else:
                backend = (
                    cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY
                )
            capture = None
            try:
                capture = (
                    cv2.VideoCapture(runtime_source, backend)
                    if type(runtime_source) is int
                    else cv2.VideoCapture(runtime_source)
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
                result = {
                    "available": True,
                    "width": width,
                    "height": height,
                }
                if identity is not None:
                    result.update(
                        source=source,
                        stable_id=source,
                        runtime_source=runtime_source,
                        index=runtime_source,
                        backend=identity["backend"],
                        name=identity["name"],
                    )
                return result, encoded.tobytes()
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

            with self.lock:
                cameras = []
                default_backend = (
                    cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY
                )
                opened = {
                    camera.source: camera.status()["ready"]
                    for camera in self.cameras.values()
                    if getattr(camera, "source", None) is not None
                }
                identities = self._identity_devices() if self.identity_discovery else []
                candidates = (
                    identities + self._configured_identity_placeholders(identities)
                    if self.identity_discovery
                    else [
                        {"runtime_source": source} for source in range(first, last + 1)
                    ]
                )
                for item in candidates:
                    source = item["runtime_source"]
                    if source is None:
                        cameras.append(
                            {
                                "source": item["stable_id"],
                                "stable_id": item["stable_id"],
                                "runtime_source": None,
                                "name": item["name"],
                                "backend": item["backend"],
                                "available": False,
                            }
                        )
                        continue
                    if source in opened:
                        cameras.append(
                            {
                                "source": item.get("stable_id", source),
                                "stable_id": item.get("stable_id"),
                                "runtime_source": source,
                                "index": source,
                                "name": item.get("name"),
                                "backend": item.get("backend"),
                                "available": bool(opened[source]),
                            }
                        )
                        continue
                    capture = None
                    try:
                        candidate_backend = {
                            "dshow": cv2.CAP_DSHOW,
                            "v4l2": cv2.CAP_V4L2,
                        }.get(item.get("backend"), default_backend)
                        capture = cv2.VideoCapture(source, candidate_backend)
                        if capture.isOpened():
                            readable, frame = capture.read()
                            if readable and frame is not None:
                                cameras.append(
                                    {
                                        "source": item.get("stable_id", source),
                                        "stable_id": item.get("stable_id"),
                                        "runtime_source": source,
                                        "index": source,
                                        "name": item.get("name"),
                                        "backend": item.get("backend"),
                                        "available": True,
                                    }
                                )
                    except Exception:
                        # An invalid index is omitted from discovery. A configured
                        # source is still returned by the per-device camera route.
                        pass
                    finally:
                        if capture is not None:
                            capture.release()
                    if item.get("stable_id") is not None and not any(
                        camera.get("stable_id") == item["stable_id"]
                        for camera in cameras
                    ):
                        # OS enumeration is authoritative for identity. Keep a
                        # temporarily unreadable camera selectable as unavailable
                        # instead of making it disappear from the picker.
                        cameras.append(
                            {
                                "source": item["stable_id"],
                                "stable_id": item["stable_id"],
                                "runtime_source": source,
                                "index": source,
                                "name": item.get("name"),
                                "backend": item.get("backend"),
                                "available": False,
                            }
                        )
                for camera in cameras:
                    if camera.get("stable_id") is None:
                        camera.pop("index", None)
                        camera.pop("runtime_source", None)
                        camera.pop("stable_id", None)
                    if camera.get("name") is None:
                        camera.pop("name")
                    if camera.get("backend") is None:
                        camera.pop("backend")
                self.discovery_cache = [dict(item) for item in cameras]
                self.discovery_cached_at = time.monotonic()
                return cameras
