"""Camera-backed semantic text detection using local Tesseract OCR."""

import csv
import io
import math
import os
import shutil
import subprocess

import cv2

from .state import ScreenStateError, TextDetectionUnavailable


class CameraOCRScreenStateProvider:
    def __init__(
        self,
        external_screen,
        *,
        language="eng",
        timeout=5.0,
    ):
        self.external_screen = external_screen
        self.language = language
        self.timeout = timeout

    def _tesseract_command(self):
        configured = os.environ.get(
            "TESSERACT_CMD",
            "",
        ).strip()

        if configured:
            return configured

        command = shutil.which("tesseract")

        if not command:
            raise TextDetectionUnavailable(
                "Camera OCR is unavailable because Tesseract is not installed."
            )

        return command

    def _prepare_image(self, image):
        """
        Prepare the captured phone display for OCR.

        Main goal:
        - Keep preprocessing lightweight.
        - Avoid enlarging the entire phone screen unless really necessary.
        - Ensure Tesseract receives a grayscale uint8 image.
        """

        if image is None or image.size == 0:
            raise ScreenStateError("Camera OCR received an empty phone display image.")

        # Ensure grayscale.
        #
        # capture_for_ocr() may already return grayscale,
        # but this keeps the provider safe if it returns BGR/BGRA later.
        if len(image.shape) == 3:
            channels = image.shape[2]

            if channels == 4:
                image = cv2.cvtColor(
                    image,
                    cv2.COLOR_BGRA2GRAY,
                )

            elif channels == 3:
                image = cv2.cvtColor(
                    image,
                    cv2.COLOR_BGR2GRAY,
                )

            else:
                raise ScreenStateError(
                    f"Camera OCR received an unsupported image with "
                    f"{channels} channels."
                )

        # Tesseract/OpenCV work best here with uint8 grayscale data.
        if image.dtype != "uint8":
            image = cv2.normalize(
                image,
                None,
                0,
                255,
                cv2.NORM_MINMAX,
            ).astype("uint8")

        # Improve local contrast.
        #
        # This is useful for phone screens captured by a camera where
        # brightness may vary across the display.
        clahe = cv2.createCLAHE(
            clipLimit=2.0,
            tileGridSize=(8, 8),
        )

        prepared = clahe.apply(image)

        # IMPORTANT:
        #
        # The old implementation resized the whole image by 1.5x.
        #
        # 1.5x width × 1.5x height = 2.25x as many pixels for Tesseract.
        #
        # That can be quite expensive on a Raspberry Pi.
        #
        # Leave the image at its native captured resolution first.
        #
        # If OCR accuracy becomes too low for very small text,
        # try 1.15x or 1.25x instead of 1.5x.
        #
        # Example:
        #
        # prepared = cv2.resize(
        #     prepared,
        #     None,
        #     fx=1.25,
        #     fy=1.25,
        #     interpolation=cv2.INTER_LINEAR,
        # )

        return prepared

    def _encode_pgm(self, image):
        """
        Encode a grayscale image as binary PGM.

        PGM is deliberately used instead of PNG here.

        PNG:
            raw image
                ↓
            CPU compression
                ↓
            compressed PNG
                ↓
            Tesseract decodes PNG

        PGM:
            tiny header
                +
            raw grayscale pixels

        This avoids unnecessary PNG compression work.
        """

        if image is None or image.size == 0:
            raise ScreenStateError("Camera OCR could not encode an empty image.")

        if len(image.shape) != 2:
            raise ScreenStateError(
                "Camera OCR PGM encoding requires a grayscale image."
            )

        height, width = image.shape

        header = f"P5\n{width} {height}\n255\n".encode("ascii")

        return header + image.tobytes()

    def capture_snapshot(self):
        """Capture the current calibrated phone display without running OCR."""

        session = self.external_screen.session()

        image, _, _ = session.capture_for_ocr()

        if image is None:
            raise ScreenStateError("Camera OCR could not capture the phone display.")

        # Important:
        # copy it because the camera/session may update later.
        return image.copy()

    def _recognize_image(
        self,
        image,
        output_format=None,
        *,
        psm=11,
    ):
        """
        Run Tesseract OCR against an image.

        PSM 11:
            Sparse text.

        This is suitable for whole phone screens where text may appear
        in unrelated regions of the UI.
        """

        prepared = self._prepare_image(image)

        encoded = self._encode_pgm(prepared)

        command = self._tesseract_command()

        try:
            arguments = [
                command,
                "stdin",
                "stdout",
                # LSTM OCR engine.
                "--oem",
                "1",
                # Sparse-text mode works reasonably well for phone UIs.
                "--psm",
                str(psm),
                "-l",
                self.language,
            ]

            if output_format:
                arguments.append(output_format)

            result = subprocess.run(
                arguments,
                input=encoded,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                check=False,
            )

        except subprocess.TimeoutExpired as exc:
            raise ScreenStateError(
                "Camera OCR timed out while reading the phone screen."
            ) from exc

        except OSError as exc:
            raise TextDetectionUnavailable(
                "Camera OCR could not start Tesseract."
            ) from exc

        if result.returncode != 0:
            message = result.stderr.decode(
                "utf-8",
                errors="replace",
            ).strip()

            raise ScreenStateError(
                "Camera OCR failed" + (f": {message}" if message else ".")
            )

        return result.stdout.decode(
            "utf-8",
            errors="replace",
        )

    def _candidates_from_tsv(self, raw):
        """Group valid Tesseract word rows into screen-ordered text lines."""

        groups = {}

        try:
            rows = csv.DictReader(
                io.StringIO(raw),
                delimiter="\t",
            )

            for row in rows:
                # Tesseract TSV level:
                #
                # 1 = page
                # 2 = block
                # 3 = paragraph
                # 4 = line
                # 5 = word
                if row.get("level") != "5":
                    continue

                text = " ".join((row.get("text") or "").split())

                if not text:
                    continue

                try:
                    confidence = float(row.get("conf", ""))

                    key = tuple(
                        int(row[field])
                        for field in (
                            "block_num",
                            "par_num",
                            "line_num",
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                    KeyError,
                ):
                    continue

                if not math.isfinite(confidence) or confidence < 0:
                    continue

                group = groups.setdefault(
                    key,
                    {
                        "words": [],
                        "confidences": [],
                    },
                )

                group["words"].append(text)
                group["confidences"].append(confidence)

        except csv.Error:
            return []

        result = []

        for group in groups.values():
            if not group["words"]:
                continue

            confidences = group["confidences"]

            result.append(
                {
                    "text": " ".join(group["words"]),
                    "confidence": (sum(confidences) / len(confidences)),
                }
            )

        return result

    def _lines_from_raw(self, raw):
        """Convert Tesseract plain-text output into unique non-empty lines."""

        result = []

        for line in raw.splitlines():
            value = " ".join(line.split())

            if not value:
                continue

            if value not in result:
                result.append(value)

        return result

    def visible_texts_from_image(
        self,
        image,
        *,
        psm=11,
    ):
        """
        OCR an image using the legacy list-of-strings result format.
        """

        raw = self._recognize_image(
            image,
            psm=psm,
        )

        return self._lines_from_raw(raw)

    def visible_text_candidates_from_image(
        self,
        image,
        *,
        psm=11,
    ):
        """
        Return screen-ordered OCR lines with aggregate word confidence.
        """

        raw = self._recognize_image(
            image,
            "tsv",
            psm=psm,
        )

        return self._candidates_from_tsv(raw)

    def visible_text_candidates(self):
        """
        Capture the current screen and return OCR candidates.
        """

        snapshot = self.capture_snapshot()

        return self.visible_text_candidates_from_image(snapshot)

    def visible_texts(self):
        """
        Capture the current screen and return detected text lines.
        """

        snapshot = self.capture_snapshot()

        return self.visible_texts_from_image(snapshot)

    def contains_text(
        self,
        text,
        *,
        image=None,
    ):
        """
        Check whether text exists on the current screen.

        An optional existing image may be supplied so callers can avoid
        capturing another camera frame.
        """

        expected = " ".join(text.split()).casefold()

        if not expected:
            return False

        if image is None:
            image = self.capture_snapshot()

        visible = self.visible_texts_from_image(image)

        complete_text = " ".join(visible).casefold()

        return expected in complete_text
