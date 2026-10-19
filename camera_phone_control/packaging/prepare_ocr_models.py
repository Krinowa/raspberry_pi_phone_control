"""Download and validate the OCR assets that are embedded in the Windows build."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

MODEL_NAMES = (
    "PP-OCRv5_mobile_det",
    "en_PP-OCRv5_mobile_rec",
    "PP-LCNet_x1_0_textline_ori",
)


def _validate(root):
    missing = []
    for name in MODEL_NAMES:
        directory = root / name
        if not directory.is_dir() or not all(
            (directory / filename).is_file()
            for filename in ("inference.json", "inference.pdiparams")
        ):
            missing.append(name)
    if missing:
        raise RuntimeError("OCR model preparation failed: " + ", ".join(missing))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path.home() / ".paddlex",
        help="PaddleX cache containing the official_models directory.",
    )
    args = parser.parse_args(argv)
    cache_root = args.cache_root.expanduser().resolve()
    os.environ["PADDLE_PDX_CACHE_HOME"] = os.fspath(cache_root)

    # Enabling text-line orientation for this build-time warmup downloads the
    # optional model too. Runtime behavior still follows the existing setting.
    from paddleocr import PaddleOCR

    PaddleOCR(
        lang="en",
        ocr_version="PP-OCRv5",
        device="cpu",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=True,
        text_detection_model_name="PP-OCRvthe c5_mobile_det",
        text_recognition_model_name="en_PP-OCRv5_mobile_rec",
        textline_orientation_model_name="PP-LCNet_x1_0_textline_ori",
    )
    model_root = cache_root / "official_models"
    _validate(model_root)
    print(f"OCR models ready: {model_root}")


if __name__ == "__main__":
    main()
