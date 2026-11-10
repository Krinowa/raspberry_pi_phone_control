"""Validate required offline FFmpeg and PaddleOCR assets in a frozen onedir build."""

from __future__ import annotations

import argparse
from pathlib import Path

from prepare_ocr_models import _validate as validate_models
from validate_ffmpeg import validate_ffmpeg


def validate_bundle(distribution):
    distribution = Path(distribution).resolve()
    executable = distribution / "PhoneController.exe"
    resources = distribution / "_internal"
    if not executable.is_file():
        raise RuntimeError(f"Frozen application executable is missing: {executable}")
    if not resources.is_dir():
        raise RuntimeError(f"Frozen resource directory is missing: {resources}")
    validate_ffmpeg(resources / "tools" / "ffmpeg.exe")
    validate_models(resources / "paddle_models")
    forbidden = tuple(distribution.rglob(".env")) + tuple(
        distribution.rglob("tesseract.exe")
    )
    if forbidden:
        raise RuntimeError(
            "Forbidden secret/Tesseract packaging input found: "
            + ", ".join(str(path) for path in forbidden)
        )
    return distribution


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("distribution", type=Path)
    args = parser.parse_args(argv)
    try:
        distribution = validate_bundle(args.distribution)
    except RuntimeError as exc:
        parser.error(str(exc))
    print(f"Frozen offline assets ready: {distribution}")


if __name__ == "__main__":
    main()
