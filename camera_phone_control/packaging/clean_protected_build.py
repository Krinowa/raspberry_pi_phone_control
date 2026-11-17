"""Remove only generated Windows release artifacts that could mask a new build."""

from __future__ import annotations

import shutil
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
GENERATED_DIRECTORIES = (
    APP_ROOT / "build" / "protected",
    APP_ROOT / "build" / "protected-temp",
    APP_ROOT / "build" / "phone_controller",
    APP_ROOT / "build" / "PhoneController",
    APP_ROOT / "build" / "pyinstaller-cache",
    APP_ROOT / "dist" / "PhoneController",
)
GENERATED_FILES = (APP_ROOT / "installer" / "PhoneControllerSetup.exe",)


def _inside_app_root(path):
    resolved = Path(path).resolve()
    root = APP_ROOT.resolve()
    return resolved != root and root in resolved.parents


def clean():
    for directory in GENERATED_DIRECTORIES:
        if not _inside_app_root(directory):
            raise RuntimeError(f"Refusing to clean unsafe path: {directory}")
        if directory.exists():
            shutil.rmtree(directory)
    for artifact in GENERATED_FILES:
        if not _inside_app_root(artifact):
            raise RuntimeError(f"Refusing to clean unsafe path: {artifact}")
        if artifact.exists():
            artifact.unlink()


if __name__ == "__main__":
    clean()
    print("Stale protected build artifacts removed.")
