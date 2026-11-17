"""Runtime environment required before packaged application imports."""

import os
import sys
import traceback
from pathlib import Path


def _install_package_smoke_diagnostics():
    report = os.environ.get("PHONE_CONTROLLER_PACKAGE_SMOKE_EARLY_REPORT", "").strip()
    stdout_path = os.environ.get("PHONE_CONTROLLER_PACKAGE_SMOKE_STDOUT", "").strip()
    stderr_path = os.environ.get("PHONE_CONTROLLER_PACKAGE_SMOKE_STDERR", "").strip()
    if not report:
        return

    if stdout_path:
        sys.stdout = open(stdout_path, "a", encoding="utf-8", buffering=1)
    if stderr_path:
        sys.stderr = open(stderr_path, "a", encoding="utf-8", buffering=1)

    def write_unhandled(exception_type, exception, tb):
        try:
            Path(report).write_text(
                "".join(traceback.format_exception(exception_type, exception, tb)),
                encoding="utf-8",
            )
        except Exception:
            pass
        if sys.stderr is not None:
            traceback.print_exception(exception_type, exception, tb, file=sys.stderr)

    sys.excepthook = write_unhandled


_install_package_smoke_diagnostics()

root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
os.environ.setdefault(
    "PHONE_CONTROLLER_OCR_MODELS_DIR", os.fspath(root / "paddle_models")
)
tools = root / "tools"
if tools.is_dir():
    os.environ.setdefault("PHONE_CONTROLLER_TOOLS_DIR", os.fspath(tools))
    os.environ["PATH"] = os.fspath(tools) + os.pathsep + os.environ.get("PATH", "")
