"""Runtime environment required before packaged application imports."""

import os
import sys
from pathlib import Path

root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
os.environ.setdefault(
    "PHONE_CONTROLLER_OCR_MODELS_DIR", os.fspath(root / "paddle_models")
)
tools = root / "tools"
if tools.is_dir():
    os.environ.setdefault("PHONE_CONTROLLER_TOOLS_DIR", os.fspath(tools))
    os.environ["PATH"] = os.fspath(tools) + os.pathsep + os.environ.get("PATH", "")
