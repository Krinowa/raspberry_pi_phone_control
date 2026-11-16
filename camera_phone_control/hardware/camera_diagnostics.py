"""Read-only Windows camera identity and encoding diagnostics."""

from __future__ import annotations

import ctypes
import os
import platform
import sys
from datetime import datetime, timezone

from .camera_identity import (
    CameraIdentityError,
    _windows_ansi_encoding,
    _windows_directshow_devices,
    _windows_pnp_devices,
    discover_windows_devices,
)


def _windows_code_page(function_name):
    try:
        value = int(getattr(ctypes.windll.kernel32, function_name)())
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return value or None


def _safe_error(exc):
    return {
        "type": type(exc).__name__,
        "code": getattr(exc, "error_code", "camera_discovery_failed"),
        "message": str(exc)[:500],
    }


def collect_windows_camera_diagnostics():
    """Inspect enumeration without reading or changing saved camera mappings."""
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "platform": sys.platform,
        "windows_release": platform.release(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "code_pages": {
            "ansi": _windows_code_page("GetACP"),
            "ansi_encoding": _windows_ansi_encoding(),
            "oem": _windows_code_page("GetOEMCP"),
            "console_input": _windows_code_page("GetConsoleCP"),
            "console_output": _windows_code_page("GetConsoleOutputCP"),
        },
        "pnp_devices": [],
        "directshow_devices": [],
        "mapped_identities": [],
        "errors": [],
    }
    if os.name != "nt" or sys.platform != "win32":
        report["errors"].append(
            {
                "type": "UnsupportedPlatform",
                "code": "windows_required",
                "message": "Windows camera diagnostics require Windows.",
            }
        )
        return report

    try:
        pnp_devices = _windows_pnp_devices()
        report["pnp_devices"] = pnp_devices
    except CameraIdentityError as exc:
        report["errors"].append(_safe_error(exc))
        pnp_devices = None

    directshow_devices, directshow_error, executable = _windows_directshow_devices(
        include_status=True
    )
    report["directshow_executable"] = executable
    report["directshow_devices"] = directshow_devices
    report["directshow_enumeration_error"] = directshow_error

    if pnp_devices is not None:
        try:
            report["mapped_identities"] = discover_windows_devices(
                pnp_devices=pnp_devices,
                directshow_result=(
                    directshow_devices,
                    directshow_error,
                    executable,
                ),
            )
        except (CameraIdentityError, TypeError, ValueError) as exc:
            report["errors"].append(_safe_error(exc))
    return report
