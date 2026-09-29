"""Persistent calibrated-screen artifacts produced by workflow runs."""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path

import cv2

from ..screen.state import ScreenStateError

DEFAULT_SCREENSHOT_ROOT = Path(__file__).resolve().parents[3] / "receipt"


class WorkflowScreenshotStore:
    def __init__(self, root=None, *, clock=None):
        self.root = Path(root or DEFAULT_SCREENSHOT_ROOT)
        self.clock = clock or (lambda: datetime.now().astimezone())

    def capture(self, screen, *, run_id, step_id, label=None):
        if screen is None:
            raise ScreenStateError(
                "Screenshot capture requires a calibrated phone-screen camera."
            )
        try:
            image, _, _ = screen.session().capture_calibrated_color()
        except ScreenStateError:
            raise
        except Exception as exc:
            raise ScreenStateError(
                "Could not obtain the calibrated phone screen for screenshot capture."
            ) from exc
        if image is None or not hasattr(image, "size") or image.size == 0:
            raise ScreenStateError(
                "Screenshot capture received an empty calibrated phone screen."
            )
        if len(image.shape) != 3 or image.shape[2] != 3:
            raise ScreenStateError(
                "Screenshot capture requires a color calibrated phone screen."
            )

        captured = self.clock()
        if captured.tzinfo is None:
            captured = captured.astimezone()
        day = captured.strftime("%Y-%m-%d")
        directory = self.root / day
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink():
                raise OSError("Screenshot date directory cannot be a symlink.")
            ok, encoded = cv2.imencode(".jpg", image)
            if not ok:
                raise OSError("JPEG encoding failed.")
            stamp = captured.strftime("%H%M%S_%f")[:10]
            # The random suffix and exclusive create guarantee that captures made
            # in the same millisecond never overwrite one another.
            filename = f"{run_id}_{step_id}_{stamp}_{uuid.uuid4().hex[:8]}.jpg"
            destination = directory / filename
            with destination.open("xb") as stream:
                stream.write(encoded.tobytes())
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, cv2.error) as exc:
            raise ScreenStateError(
                "Calibrated phone screenshot could not be saved."
            ) from exc

        return {
            "step_id": step_id,
            "label": label or None,
            "captured_at": captured.isoformat(),
            "relative_path": f"{day}/{filename}",
        }
