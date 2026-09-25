"""Camera-backed semantic text detection using local Tesseract OCR."""

import csv
from difflib import SequenceMatcher
import io
import itertools
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import time

import cv2

from .state import ScreenStateError, TextDetectionUnavailable

LOG = logging.getLogger("camera_ocr")
PSM_MODES = {6, 11, 12}
PREPROCESS_MODES = {"grayscale", "clahe", "otsu", "adaptive", "sharpen"}


class CameraOCRScreenStateProvider:
    """Read text from the already calibrated external-camera phone crop."""

    def __init__(self, external_screen, *, language="eng", timeout=5.0):
        self.external_screen = external_screen
        self.language = language
        self.timeout = timeout
        self.preprocess_mode = self._configured_mode()
        self.psm = self._configured_psm()
        self.debug_dir = os.environ.get("CAMERA_OCR_DEBUG_DIR", "").strip()
        self.debug_logging = self._environment_flag("CAMERA_OCR_DEBUG") or bool(
            self.debug_dir
        )
        self.debug_sweep = self._environment_flag("CAMERA_OCR_DEBUG_SWEEP")
        self._debug_sweep_done = False
        self._debug_sequence = itertools.count(1)

    @staticmethod
    def _environment_flag(name):
        return os.environ.get(name, "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _configured_mode():
        mode = os.environ.get("CAMERA_OCR_PREPROCESS", "adaptive").strip().casefold()
        if mode not in PREPROCESS_MODES:
            LOG.warning(
                "Unknown CAMERA_OCR_PREPROCESS=%r; using adaptive.",
                mode,
            )
            return "adaptive"
        return mode

    @staticmethod
    def _configured_psm():
        value = os.environ.get("CAMERA_OCR_PSM", "11").strip()
        try:
            psm = int(value)
        except ValueError:
            psm = 11
        if psm not in PSM_MODES:
            LOG.warning("Unsupported CAMERA_OCR_PSM=%r; using 11.", value)
            return 11
        return psm

    def _tesseract_command(self):
        configured = os.environ.get("TESSERACT_CMD", "").strip()
        if configured:
            return configured
        command = shutil.which("tesseract")
        if not command:
            raise TextDetectionUnavailable(
                "Camera OCR is unavailable because Tesseract is not installed."
            )
        return command

    @staticmethod
    def _grayscale(image):
        if image is None or not hasattr(image, "size") or image.size == 0:
            raise ScreenStateError("Camera OCR received an empty phone display image.")
        if len(image.shape) == 3:
            channels = image.shape[2]
            if channels == 4:
                image = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
            elif channels == 3:
                image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            else:
                raise ScreenStateError(
                    f"Camera OCR received an unsupported image with {channels} channels."
                )
        elif len(image.shape) != 2:
            raise ScreenStateError("Camera OCR received an unsupported image shape.")
        if image.dtype != "uint8":
            image = cv2.normalize(image, None, 0, 255, cv2.NORM_MINMAX).astype("uint8")
        return image

    def _prepare_image(
        self,
        image,
        *,
        mode=None,
        scale=None,
        threshold=None,
    ):
        """Prepare one grayscale image without mutating the captured snapshot.

        ``threshold`` remains accepted for compatibility with the earlier helper:
        true selects adaptive thresholding and false selects the configured mode.
        """
        if threshold is True:
            mode = "adaptive"
        elif mode is None:
            mode = self.preprocess_mode
        if mode not in PREPROCESS_MODES:
            raise ValueError(f"Unknown OCR preprocessing mode: {mode}.")
        if scale is None:
            scale = 1.5 if mode == "adaptive" else 1.25
        if scale not in {1.0, 1.25, 1.5}:
            raise ValueError("OCR scale must be 1.0, 1.25, or 1.5.")

        gray = self._grayscale(image)
        if mode == "clahe":
            prepared = cv2.createCLAHE(
                clipLimit=2.0,
                tileGridSize=(8, 8),
            ).apply(gray)
        else:
            prepared = gray.copy()

        if scale != 1.0:
            prepared = cv2.resize(
                prepared,
                None,
                fx=scale,
                fy=scale,
                interpolation=cv2.INTER_CUBIC,
            )

        if mode == "otsu":
            blurred = cv2.GaussianBlur(prepared, (3, 3), 0)
            _, prepared = cv2.threshold(
                blurred,
                0,
                255,
                cv2.THRESH_BINARY + cv2.THRESH_OTSU,
            )
        elif mode == "adaptive":
            # Median filtering removes isolated camera noise without softening
            # letter edges as much as a Gaussian blur. Applying CLAHE first was
            # tested but amplified sensor noise into many false glyphs.
            prepared = cv2.medianBlur(prepared, 3)
            prepared = cv2.adaptiveThreshold(
                prepared,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                31,
                9,
            )
        elif mode == "sharpen":
            # Sharpen the original grayscale instead of CLAHE output. Camera
            # noise can be amplified into false glyphs when both are combined.
            blurred = cv2.GaussianBlur(prepared, (0, 0), 0.8)
            prepared = cv2.addWeighted(prepared, 1.35, blurred, -0.35, 0)

        # Tesseract may discard labels touching an image edge. Whitespace helps
        # navigation and disclosure text near the bottom of the phone screen.
        margin = max(12, round(min(prepared.shape) * 0.015))
        return cv2.copyMakeBorder(
            prepared,
            margin,
            margin,
            margin,
            margin,
            cv2.BORDER_CONSTANT,
            value=255,
        )

    @staticmethod
    def _encode_pgm(image):
        if image is None or not hasattr(image, "size") or image.size == 0:
            raise ScreenStateError("Camera OCR could not encode an empty image.")
        if len(image.shape) != 2:
            raise ScreenStateError(
                "Camera OCR PGM encoding requires a grayscale image."
            )
        height, width = image.shape
        return f"P5\n{width} {height}\n255\n".encode("ascii") + image.tobytes()

    def capture_snapshot(self):
        """Capture the current calibrated phone display without running OCR."""
        session = self.external_screen.session()
        image, _, _ = session.capture_for_ocr()
        if image is None:
            raise ScreenStateError("Camera OCR could not capture the phone display.")
        return image.copy()

    def _save_debug_input(self, encoded, *, mode, scale, psm, output_format):
        if not self.debug_dir:
            return
        try:
            directory = Path(self.debug_dir).expanduser()
            directory.mkdir(parents=True, exist_ok=True)
            number = next(self._debug_sequence)
            name = (
                f"ocr-{number:04d}-{mode}-{scale:g}x-psm{psm}-"
                f"{output_format or 'text'}.pgm"
            )
            path = directory / name
            path.write_bytes(encoded)
            LOG.info("Saved exact Tesseract input to %s", path)
        except OSError as exc:
            LOG.warning("Could not save Camera OCR debug image: %s", exc)

    def _log_result(self, raw, *, mode, scale, psm, output_format, duration):
        candidates = self._candidates_from_tsv(raw) if output_format == "tsv" else None
        texts = (
            [item["text"] for item in candidates]
            if candidates is not None
            else self._lines_from_raw(raw)
        )
        confidence = (
            sum(item["confidence"] for item in candidates) / len(candidates)
            if candidates
            else None
        )
        LOG.log(
            logging.INFO if self.debug_logging else logging.DEBUG,
            "OCR mode=%s scale=%.2fx psm=%d duration=%.3fs confidence=%s text=%r",
            mode,
            scale,
            psm,
            duration,
            "n/a" if confidence is None else f"{confidence:.1f}",
            " | ".join(texts)[:800],
        )

    def _recognize_image(
        self,
        image,
        output_format=None,
        *,
        psm=None,
        threshold=None,
        mode=None,
        scale=None,
    ):
        """Run one explicitly selected Tesseract pass."""
        psm = self.psm if psm is None else psm
        if psm not in PSM_MODES:
            raise ValueError("Tesseract PSM must be 6, 11, or 12.")
        if threshold is True:
            mode = "adaptive"
        elif mode is None:
            mode = self.preprocess_mode
        if scale is None:
            scale = 1.5 if mode == "adaptive" else 1.25

        started = time.monotonic()
        prepared = self._prepare_image(image, mode=mode, scale=scale)
        encoded = self._encode_pgm(prepared)
        self._save_debug_input(
            encoded,
            mode=mode,
            scale=scale,
            psm=psm,
            output_format=output_format,
        )
        arguments = [
            self._tesseract_command(),
            "stdin",
            "stdout",
            "--oem",
            "1",
            "--psm",
            str(psm),
            "-l",
            self.language,
        ]
        if output_format:
            arguments.append(output_format)
        try:
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
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise ScreenStateError(
                "Camera OCR failed" + (f": {message}" if message else ".")
            )
        raw = result.stdout.decode("utf-8", errors="replace")
        self._log_result(
            raw,
            mode=mode,
            scale=scale,
            psm=psm,
            output_format=output_format,
            duration=time.monotonic() - started,
        )
        return raw

    @staticmethod
    def _candidates_from_tsv(raw):
        """Group valid Tesseract word rows into screen-ordered lines."""
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
        result = []
        for group in groups.values():
            if not group["words"]:
                continue
            confidences = group["confidences"]
            result.append(
                {
                    "text": " ".join(group["words"]),
                    "confidence": sum(confidences) / len(confidences),
                    "min_confidence": min(confidences),
                }
            )
        return result

    @staticmethod
    def _lines_from_raw(raw):
        result = []
        for line in raw.splitlines():
            value = " ".join(line.split())
            if value and value not in result:
                result.append(value)
        return result

    def visible_texts_from_image(
        self,
        image,
        *,
        psm=None,
        threshold=None,
        mode=None,
        scale=None,
    ):
        options = {
            name: value
            for name, value in (
                ("psm", psm),
                ("threshold", threshold),
                ("mode", mode),
                ("scale", scale),
            )
            if value is not None
        }
        raw = self._recognize_image(image, **options)
        return self._lines_from_raw(raw)

    def visible_text_candidates_from_image(
        self,
        image,
        *,
        psm=None,
        threshold=None,
        mode=None,
        scale=None,
    ):
        options = {
            name: value
            for name, value in (
                ("psm", psm),
                ("threshold", threshold),
                ("mode", mode),
                ("scale", scale),
            )
            if value is not None
        }
        raw = self._recognize_image(image, "tsv", **options)
        return self._candidates_from_tsv(raw)

    @staticmethod
    def _needs_fallback(candidates):
        if not candidates:
            return True
        confidences = [item["confidence"] for item in candidates]
        if sum(confidences) / len(confidences) < 65:
            return True
        return any(
            item["confidence"] < 55
            and sum(character.isalpha() for character in item["text"]) >= 3
            for item in candidates
        )

    @staticmethod
    def _merge_candidates(primary, fallback):
        result = []
        positions = {}
        for candidate in [*primary, *fallback]:
            key = " ".join(candidate["text"].split()).casefold()
            if key in positions:
                index = positions[key]
                if candidate["confidence"] > result[index]["confidence"]:
                    result[index] = candidate
            else:
                similar = next(
                    (
                        index
                        for existing, index in positions.items()
                        if SequenceMatcher(None, existing, key).ratio() >= 0.75
                    ),
                    None,
                )
                if similar is not None:
                    if candidate["confidence"] > result[similar]["confidence"]:
                        old_key = next(
                            saved
                            for saved, index in positions.items()
                            if index == similar
                        )
                        del positions[old_key]
                        positions[key] = similar
                        result[similar] = candidate
                    continue
                positions[key] = len(result)
                result.append(candidate)
        return result

    def _default_candidates(self, image):
        return self.visible_text_candidates_from_image(
            image,
            psm=self.psm,
            mode=self.preprocess_mode,
            scale=1.5 if self.preprocess_mode == "adaptive" else 1.25,
        )

    def _fallback_candidates(self, image):
        return self.visible_text_candidates_from_image(
            image,
            psm=11,
            mode="clahe",
            scale=1.25,
        )

    def _run_debug_sweep(self, image):
        """Opt-in, one-shot comparison of requested modes and PSM values."""
        if not self.debug_sweep or self._debug_sweep_done:
            return
        self._debug_sweep_done = True
        strategies = [
            ("grayscale", 1.0),
            ("clahe", 1.25),
            ("otsu", 1.25),
            ("adaptive", 1.25),
            ("adaptive", 1.5),
            ("sharpen", 1.25),
            ("sharpen", 1.5),
        ]
        LOG.info("Starting one-shot Camera OCR preprocessing/PSM debug sweep.")
        for mode, scale in strategies:
            for psm in (6, 11, 12):
                try:
                    self.visible_text_candidates_from_image(
                        image,
                        psm=psm,
                        mode=mode,
                        scale=scale,
                    )
                except ScreenStateError as exc:
                    LOG.warning(
                        "OCR debug sweep failed for mode=%s scale=%.2fx psm=%d: %s",
                        mode,
                        scale,
                        psm,
                        exc,
                    )

    def visible_text_candidates(self):
        snapshot = self.capture_snapshot()
        self._run_debug_sweep(snapshot)
        primary = self._default_candidates(snapshot)
        if not self._needs_fallback(primary):
            return primary
        LOG.debug("OCR confidence was low; running one adaptive fallback pass.")
        return self._merge_candidates(primary, self._fallback_candidates(snapshot))

    def visible_texts(self):
        return [candidate["text"] for candidate in self.visible_text_candidates()]

    @staticmethod
    def _contains(candidates, expected):
        complete_text = " ".join(
            candidate["text"] for candidate in candidates
        ).casefold()
        return expected in complete_text

    def contains_text(self, text, *, image=None):
        expected = " ".join(text.split()).casefold()
        if not expected:
            return False
        snapshot = self.capture_snapshot() if image is None else image
        self._run_debug_sweep(snapshot)
        primary = self._default_candidates(snapshot)
        if self._contains(primary, expected):
            return True
        # An expected-text miss is sufficient reason for the single fallback.
        fallback = self._fallback_candidates(snapshot)
        return self._contains(fallback, expected)
