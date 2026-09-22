"""Display-only observations from the dashboard's existing external camera."""

import cv2
import numpy as np

from .stability import ScreenStabilityError


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

    def _sample(self):
        with self.state.lock:
            if (
                self.state.calibration is not self.calibration
                or self.state.signature != self.signature
            ):
                raise ScreenStabilityError(
                    "Phone display calibration changed; restart the workflow."
                )

            observe = getattr(
                self.state.camera,
                "observation",
                None,
            )

            sample = observe() if observe else None

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

    def capture_for_ocr(
        self,
        output_width=600,
        output_height=1000,
    ):
        """Return a higher-resolution calibrated phone display for OCR.

        Unlike capture(), this deliberately avoids the tiny 96x160
        representation and does not blur the text.
        """

        jpeg, sequence, _, _, timestamp = self._sample()

        if sequence != self.last_ocr_id:
            try:
                image = cv2.imdecode(
                    np.frombuffer(jpeg, np.uint8),
                    cv2.IMREAD_GRAYSCALE,
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
                np.diag(
                    [
                        float(output_width - 1),
                        float(output_height - 1),
                        1.0,
                    ]
                )
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
                (output_width, output_height),
            )

            # Remove a small outer border to avoid phone bezel/background
            # interfering with OCR.
            border_x = max(2, int(output_width * 0.01))
            border_y = max(2, int(output_height * 0.01))

            crop = crop[
                border_y : output_height - border_y,
                border_x : output_width - border_x,
            ]

            self.ocr_image = crop
            self.last_ocr_id = sequence

        return self.ocr_image, sequence, timestamp
