"""Four camera corners -> normalized Android display, with local persistence."""

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from automation.coordinates import normalized_to_device, unit
from hardware.device_config import DEFAULT_DEVICE_ID, validate_device_id

LOG = logging.getLogger("calibration")
DEFAULT_CALIBRATION_PATH = (
    Path(__file__).resolve().parent / "automation" / "camera_data" / "calibration.json"
)


def unit_point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("A point must contain x and y.")

    for coordinate in value:
        unit(coordinate)
    return np.array(value, dtype=float)


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


class Calibration:
    def __init__(self, corners):
        if not isinstance(corners, list) or len(corners) != 4:
            raise ValueError(
                "Select four corners: top-left, top-right, bottom-right, bottom-left."
            )

        self.corners = np.array([unit_point(p) for p in corners])

        turns = [
            cross(
                self.corners[(i + 1) % 4] - self.corners[i],
                self.corners[(i + 2) % 4] - self.corners[(i + 1) % 4],
            )
            for i in range(4)
        ]

        if not (all(t > 1e-6 for t in turns) or all(t < -1e-6 for t in turns)):
            raise ValueError(
                "Corners must form a convex, non-crossing display outline."
            )

        area = (
            abs(
                sum(cross(self.corners[i], self.corners[(i + 1) % 4]) for i in range(4))
            )
            / 2
        )

        if area < 0.005:
            raise ValueError(
                "Selected display is too small. Move the camera closer and recalibrate."
            )

        rows, values = [], []
        for (x, y), (u, v) in zip(self.corners, [(0, 0), (1, 0), (1, 1), (0, 1)]):
            rows.extend(
                ([x, y, 1, 0, 0, 0, -u * x, -u * y], [0, 0, 0, x, y, 1, -v * x, -v * y])
            )
            values.extend((u, v))

        try:
            matrix = np.array(rows)
            if np.linalg.cond(matrix) > 1e10:
                raise ValueError(
                    "Calibration is unstable. Select a clearer view of the four corners."
                )
            self.transform = np.append(np.linalg.solve(matrix, values), 1).reshape(3, 3)

        except np.linalg.LinAlgError as exc:
            raise ValueError(
                "Cannot calculate calibration from these corners."
            ) from exc

    def map(self, point):
        p = unit_point(point)

        sides = [
            cross(self.corners[(i + 1) % 4] - self.corners[i], p - self.corners[i])
            for i in range(4)
        ]

        if not (all(s >= -1e-9 for s in sides) or all(s <= 1e-9 for s in sides)):
            raise ValueError("Click is outside the calibrated phone display.")

        result = self.transform @ [p[0], p[1], 1]

        if abs(result[2]) < 1e-10:
            raise ValueError("Click cannot be mapped with this calibration.")
        normalized = result[:2] / result[2]

        if (
            not np.isfinite(normalized).all()
            or (normalized < -1e-6).any()
            or (normalized > 1 + 1e-6).any()
        ):
            raise ValueError("Mapped click is outside the phone display.")
        return np.clip(normalized, 0, 1).tolist()

    def pixel(self, point, width, height):
        u, v = self.map(point)
        return normalized_to_device(u, v, width, height), (u, v)


class CalibrationStorage:
    """Persist camera calibration separately for each logical Pi device."""

    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_CALIBRATION_PATH)
        self.lock = threading.RLock()

    @staticmethod
    def _camera_details(camera, camera_info):
        return {
            "source": camera.source,
            "rotation": camera.rotate,
            "width": camera_info["width"],
            "height": camera_info["height"],
        }

    @staticmethod
    def _device_details(device):
        # device_id routes controllers but does not change the physical geometry
        # represented by version-1 calibration files.
        details = asdict(device)
        details.pop("device_id", None)
        return details

    def path_for(self, device=None):
        """Return the legacy path for phone_A and a sibling file for others."""
        device_id = getattr(device, "device_id", None)
        if device_id in {None, DEFAULT_DEVICE_ID}:
            return self.path
        validate_device_id(device_id)
        return self.path.with_name(f"{self.path.stem}.{device_id}{self.path.suffix}")

    def load(self, camera, camera_info, device):
        """Return a compatible Calibration, or None for missing/unsafe data."""
        path = self.path_for(device)
        with self.lock:
            if not path.exists():
                return None
            if path.is_symlink():
                LOG.warning("Saved calibration file is invalid; starting uncalibrated.")
                return None
            try:
                if path.stat().st_size > 64 * 1024:
                    raise ValueError("Calibration file is unexpectedly large.")
                with path.open(encoding="utf-8") as stream:
                    data = json.load(stream)
                if not isinstance(data, dict) or data.get("version") != 1:
                    raise ValueError("Unsupported calibration format.")
                saved_camera = data.get("camera")
                saved_device = data.get("device")
                if not isinstance(saved_camera, dict) or not isinstance(
                    saved_device, dict
                ):
                    raise ValueError("Calibration context is missing.")
                if any(
                    type(saved_camera.get(field)) is not int
                    for field in ("rotation", "width", "height")
                ):
                    raise ValueError("Camera geometry is invalid.")

                current_camera = self._camera_details(camera, camera_info)
                if saved_camera.get("source") != current_camera["source"]:
                    LOG.info("Saved calibration ignored because camera source changed.")
                    return None
                if saved_camera.get("rotation") != current_camera["rotation"]:
                    LOG.info(
                        "Saved calibration ignored because camera rotation changed."
                    )
                    return None
                if (
                    saved_camera.get("width"),
                    saved_camera.get("height"),
                ) != (current_camera["width"], current_camera["height"]):
                    LOG.info(
                        "Saved calibration ignored because camera resolution changed."
                    )
                    return None
                if saved_device != self._device_details(device):
                    LOG.info(
                        "Saved calibration ignored because phone geometry changed."
                    )
                    return None

                calibration = Calibration(data.get("corners"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                LOG.warning("Saved calibration file is invalid; starting uncalibrated.")
                return None

            LOG.info("Loaded saved camera calibration.")
            return calibration

    def save(self, calibration, camera, camera_info, device):
        path = self.path_for(device)
        data = {
            "version": 1,
            "camera": self._camera_details(camera, camera_info),
            "device": self._device_details(device),
            "corners": calibration.corners.tolist(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                raise OSError("Calibration path cannot be a symlink.")
            fd, temporary = tempfile.mkstemp(
                dir=path.parent,
                prefix=".calibration-",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, indent=2, allow_nan=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        LOG.info("Saved camera calibration.")

    def clear(self, device=None):
        path = self.path_for(device)
        with self.lock:
            try:
                if path.is_symlink():
                    raise OSError("Calibration path cannot be a symlink.")
                path.unlink()
            except FileNotFoundError:
                pass
