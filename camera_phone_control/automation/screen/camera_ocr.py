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
        if image is None or image.size == 0:
            raise ScreenStateError("Camera OCR received an empty phone display image.")

        clahe = cv2.createCLAHE(
            clipLimit=2.0,
            tileGridSize=(8, 8),
        )

        prepared = clahe.apply(image)

        prepared = cv2.resize(
            prepared,
            None,
            fx=1.5,
            fy=1.5,
            interpolation=cv2.INTER_CUBIC,
        )

        return prepared

    def capture_snapshot(self):
        """Capture the current calibrated phone display without running OCR."""

        session = self.external_screen.session()

        image, _, _ = session.capture_for_ocr()

        if image is None:
            raise ScreenStateError("Camera OCR could not capture the phone display.")

        # Important:
        # copy it because the camera/session may update later.
        return image.copy()

    def _recognize_image(self, image, output_format=None):
        prepared = self._prepare_image(image)

        success, encoded = cv2.imencode(
            ".png",
            prepared,
        )

        if not success:
            raise ScreenStateError("Camera OCR could not encode the phone display.")

        command = self._tesseract_command()

        try:
            arguments = [
                command,
                "stdin",
                "stdout",
                "--psm",
                "11",
                "-l",
                self.language,
            ]

            if output_format:
                arguments.append(output_format)

            result = subprocess.run(
                arguments,
                input=encoded.tobytes(),
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
            rows = csv.DictReader(io.StringIO(raw), delimiter="\t")

            for row in rows:
                if row.get("level") != "5":
                    continue

                text = " ".join((row.get("text") or "").split())

                if not text:
                    continue

                try:
                    confidence = float(row.get("conf", ""))
                    key = tuple(
                        int(row[field])
                        for field in ("block_num", "par_num", "line_num")
                    )
                except (TypeError, ValueError, KeyError):
                    continue

                if not math.isfinite(confidence) or confidence < 0:
                    continue

                group = groups.setdefault(key, {"words": [], "confidences": []})
                group["words"].append(text)
                group["confidences"].append(confidence)
        except csv.Error:
            return []

        return [
            {
                "text": " ".join(group["words"]),
                "confidence": sum(group["confidences"]) / len(group["confidences"]),
            }
            for group in groups.values()
            if group["words"]
        ]

    def _lines_from_raw(self, raw):
        result = []

        for line in raw.splitlines():
            value = " ".join(line.split())

            if not value:
                continue

            if value not in result:
                result.append(value)

        return result

    def visible_texts_from_image(self, image):
        """OCR an image using the legacy list-of-strings result format."""

        raw = self._recognize_image(image)

        return self._lines_from_raw(raw)

    def visible_text_candidates_from_image(self, image):
        """Return screen-ordered OCR lines with aggregate word confidence."""
        raw = self._recognize_image(image, "tsv")

        return self._candidates_from_tsv(raw)

    def visible_text_candidates(self):
        snapshot = self.capture_snapshot()

        return self.visible_text_candidates_from_image(snapshot)

    def visible_texts(self):
        snapshot = self.capture_snapshot()

        return self.visible_texts_from_image(snapshot)

    def contains_text(self, text):
        expected = " ".join(text.split()).casefold()

        if not expected:
            return False

        visible = self.visible_texts()

        complete_text = " ".join(visible).casefold()

        return expected in complete_text
