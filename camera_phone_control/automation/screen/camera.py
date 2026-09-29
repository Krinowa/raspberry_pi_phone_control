"""Display-only observations from the dashboard's existing external camera."""

import time

import cv2
import numpy as np

from .stability import ScreenStabilityError
from ..workflows.timing import record_timing


class CalibratedCamera:
    def __init__(self, state):
        self.state = state

    def session(self):
        with self.state.lock:
            calibration = self.state.calibration

            if calibration is None or self.state.signature is None:
                raise ScreenStabilityError(
                    "Phone display must be calibrated before observing the screen."
                )

            return _DisplaySession(
                self.state,
                calibration,
                self.state.signature,
            )


class _DisplaySession:
    def __init__(self, state, calibration, signature):
        self.state = state
        self.calibration = calibration
        self.signature = signature

        self.last_id = None
        self.image = None

        self.last_ocr_id = None
        self.ocr_image = None

        self.last_color_id = None
        self.color_image = None

    def _sample(self, timings=None):
        started = time.monotonic()
        with self.state.lock:
            if (
                self.state.calibration is not self.calibration
                or self.state.signature != self.signature
            ):
                raise ScreenStabilityError(
                    "Phone display calibration changed; restart the workflow."
                )

            camera = self.state.camera

        # Camera.observation() has its own short lock. Do not nest the dashboard
        # state lock around it, and never hold either lock while decoding or
        # warping the copied JPEG bytes.
        observe = getattr(camera, "observation", None)
        sample = observe() if observe else None
        with self.state.lock:
            if (
                self.state.calibration is not self.calibration
                or self.state.signature != self.signature
            ):
                raise ScreenStabilityError(
                    "Phone display calibration changed; restart the workflow."
                )
        if timings is not None:
            timings["capture"] = time.monotonic() - started

        if sample is None:
            raise ScreenStabilityError(
                "External camera is unavailable; no camera frames arrived."
            )

        jpeg, sequence, width, height, timestamp = sample

        if (width, height) != self.signature[1:]:
            raise ScreenStabilityError(
                "Camera resolution changed. Recalibrate the phone display."
            )

        return jpeg, sequence, width, height, timestamp

    def capture(self):
        """Return a tiny normalized frame for screen-stability detection."""
        started = time.monotonic()
        try:
            jpeg, sequence, _, _, timestamp = self._sample()

            if sequence != self.last_id:
                # Stability detection intentionally uses a very small image.
                # This is cheap and ignores tiny text/pixel changes.
                try:
                    image = cv2.imdecode(
                        np.frombuffer(jpeg, np.uint8),
                        cv2.IMREAD_REDUCED_GRAYSCALE_4,
                    )
                except cv2.error:
                    raise ScreenStabilityError(
                        "External camera frame could not be decoded."
                    ) from None

                if image is None:
                    raise ScreenStabilityError(
                        "External camera frame could not be decoded."
                    )

                height, width = image.shape

                matrix = (
                    np.diag([95.0, 159.0, 1.0])
                    @ self.calibration.transform
                    @ np.diag(
                        [
                            1.0 / width,
                            1.0 / height,
                            1.0,
                        ]
                    )
                )

                crop = cv2.warpPerspective(
                    image,
                    matrix,
                    (96, 160),
                )

                # Exclude outer display borders where phone edges may mix
                # with the desk/background.
                self.image = cv2.GaussianBlur(
                    crop[2:-2, 2:-2],
                    (3, 3),
                    0,
                )

                self.last_id = sequence

            return self.image, sequence, timestamp
        finally:
            record_timing("camera_frame", time.monotonic() - started, count=1)

    def _warp_calibrated(
        self,
        image,
        frame_width,
        frame_height,
        output_width,
        output_height,
        timings,
    ):
        height, width = image.shape[:2]
        if output_width is None:
            device = self.signature[0]
            device_width = getattr(device, "width", None)
            device_height = getattr(device, "height", None)
            if (
                type(device_width) is int
                and type(device_height) is int
                and min(device_width, device_height) >= 2
            ):
                aspect = device_width / device_height
            else:
                # Pi control exposes normalized coordinates without Android
                # pixel geometry. Estimate the phone aspect from the calibrated
                # quadrilateral in actual camera pixels.
                points = self.calibration.corners * np.array(
                    [frame_width, frame_height]
                )
                horizontal = (
                    np.linalg.norm(points[1] - points[0])
                    + np.linalg.norm(points[2] - points[3])
                ) / 2
                vertical = (
                    np.linalg.norm(points[3] - points[0])
                    + np.linalg.norm(points[2] - points[1])
                ) / 2
                aspect = horizontal / vertical
            output_width = round(output_height * float(np.clip(aspect, 0.3, 2.5)))

        if timings is not None:
            timings.update(warped_width=output_width, warped_height=output_height)

        matrix = (
            np.diag([float(output_width - 1), float(output_height - 1), 1.0])
            @ self.calibration.transform
            @ np.diag([1.0 / width, 1.0 / height, 1.0])
        )
        warp_started = time.monotonic()
        crop = cv2.warpPerspective(image, matrix, (output_width, output_height))
        if timings is not None:
            timings["warp"] = time.monotonic() - warp_started

        # Keep only the two interpolation-edge pixels around the calibrated display.
        return crop[2 : output_height - 2, 2 : output_width - 2]

    def _capture_calibrated(
        self,
        decode_mode,
        image_attribute,
        sequence_attribute,
        output_width=None,
        output_height=1000,
        *,
        timings=None,
    ):
        jpeg, sequence, frame_width, frame_height, timestamp = self._sample(timings)
        if timings is not None:
            timings.update(
                original_frame_width=getattr(
                    self.state.camera, "raw_frame_width", None
                ),
                original_frame_height=getattr(
                    self.state.camera, "raw_frame_height", None
                ),
                camera_frame_width=frame_width,
                camera_frame_height=frame_height,
            )

        if sequence != getattr(self, sequence_attribute):
            decode_started = time.monotonic()
            try:
                image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), decode_mode)
            except cv2.error:
                raise ScreenStabilityError(
                    "External camera frame could not be decoded."
                ) from None
            if image is None:
                raise ScreenStabilityError(
                    "External camera frame could not be decoded."
                )
            if timings is not None:
                timings["decode"] = time.monotonic() - decode_started
            calibrated = self._warp_calibrated(
                image,
                frame_width,
                frame_height,
                output_width,
                output_height,
                timings,
            )
            setattr(self, image_attribute, calibrated)
            setattr(self, sequence_attribute, sequence)

        result = getattr(self, image_attribute)
        if timings is not None and result is not None:
            timings.update(
                calibrated_width=result.shape[1],
                calibrated_height=result.shape[0],
            )
        return result, sequence, timestamp

    def capture_for_ocr(self, output_width=None, output_height=1000, *, timings=None):
        """Return the existing grayscale calibrated phone display for OCR."""
        return self._capture_calibrated(
            cv2.IMREAD_GRAYSCALE,
            "ocr_image",
            "last_ocr_id",
            output_width,
            output_height,
            timings=timings,
        )

    def capture_calibrated_color(
        self, output_width=None, output_height=1000, *, timings=None
    ):
        """Return the calibrated phone display while preserving camera color."""
        return self._capture_calibrated(
            cv2.IMREAD_COLOR,
            "color_image",
            "last_color_id",
            output_width,
            output_height,
            timings=timings,
        )
