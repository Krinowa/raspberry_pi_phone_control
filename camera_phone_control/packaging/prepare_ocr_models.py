"""Download and validate the OCR assets that are embedded in the Windows build."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

MODEL_NAMES = (
    "PP-OCRv5_mobile_det",
    "en_PP-OCRv5_mobile_rec",
    "PP-OCRv5_mobile_rec",
    "latin_PP-OCRv5_mobile_rec",
    "PP-LCNet_x1_0_textline_ori",
)

MODEL_PREPARATION_PROFILES = (
    ("en", "en_PP-OCRv5_mobile_rec"),
    ("chinese_cht", "PP-OCRv5_mobile_rec"),
    ("ms", "latin_PP-OCRv5_mobile_rec"),
)

REQUIRED_MODEL_FILES = (
    "config.json",
    "inference.json",
    "inference.pdiparams",
    "inference.yml",
)


def _validate(root):
    root = Path(root)
    incomplete = []
    for name in MODEL_NAMES:
        directory = root / name
        missing = [
            filename
            for filename in REQUIRED_MODEL_FILES
            if not (directory / filename).is_file()
            or (directory / filename).stat().st_size == 0
        ]
        if not directory.is_dir():
            missing = ["model directory"]
        if missing:
            incomplete.append(f"{name} ({', '.join(missing)})")
    if incomplete:
        raise RuntimeError(
            "OCR model preparation failed; required offline model assets are "
            "missing or empty: " + "; ".join(incomplete)
        )
    return tuple(root / name for name in MODEL_NAMES)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-root",
        type=Path,
        required=True,
        help=(
            "Dedicated build cache to populate. Do not point this at a developer "
            "profile cache when preparing a release."
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate existing assets without importing PaddleOCR or downloading.",
    )
    args = parser.parse_args(argv)
    cache_root = args.cache_root.expanduser().resolve()
    os.environ["PADDLE_PDX_CACHE_HOME"] = os.fspath(cache_root)

    if not args.validate_only:
        # Enabling text-line orientation for this build-time warmup downloads the
        # optional model too. Runtime behavior still follows the existing setting.
        from paddleocr import PaddleOCR

        for language, recognition_model in MODEL_PREPARATION_PROFILES:
            PaddleOCR(
                lang=language,
                ocr_version="PP-OCRv5",
                device="cpu",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=True,
                text_detection_model_name="PP-OCRv5_mobile_det",
                text_recognition_model_name=recognition_model,
                textline_orientation_model_name="PP-LCNet_x1_0_textline_ori",
            )
    model_root = cache_root / "official_models"
    _validate(model_root)
    print(f"OCR models ready: {model_root}")


if __name__ == "__main__":
    main()
