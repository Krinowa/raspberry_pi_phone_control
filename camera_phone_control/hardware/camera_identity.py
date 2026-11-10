"""Cross-platform discovery and resolution of physical camera identities."""

from __future__ import annotations

import ctypes
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from diagnostic_logging import EVENT_DEDUPLICATOR, log_event

LOG = logging.getLogger("camera-identity")


class CameraIdentityError(RuntimeError):
    pass


def _device(stable_id, name, backend, runtime_source, **metadata):
    if not all(
        isinstance(value, str) and value for value in (stable_id, name, backend)
    ):
        raise CameraIdentityError("Camera discovery returned incomplete identity data.")
    return {
        "stable_id": stable_id,
        "name": name,
        "backend": backend,
        "runtime_source": runtime_source,
        **metadata,
    }


def discover_macos_devices():
    """Enumerate AVFoundation devices in OpenCV's numeric-index order."""
    if sys.platform != "darwin":
        return []
    objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    avfoundation = ctypes.CDLL(
        "/System/Library/Frameworks/AVFoundation.framework/AVFoundation"
    )
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    send = objc.objc_msgSend

    def message(receiver, selector, restype=ctypes.c_void_p, argtypes=(), args=()):
        send.restype = restype
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, *argtypes]
        return send(receiver, objc.sel_registerName(selector.encode("ascii")), *args)

    def string(receiver):
        value = message(receiver, "UTF8String", ctypes.c_char_p)
        return value.decode("utf-8") if value else ""

    pool_class = objc.objc_getClass(b"NSAutoreleasePool")
    pool = message(message(pool_class, "alloc"), "init")
    try:
        device_class = objc.objc_getClass(b"AVCaptureDevice")
        discovered = {}
        for constant in ("AVMediaTypeVideo", "AVMediaTypeMuxed"):
            media_type = ctypes.c_void_p.in_dll(avfoundation, constant).value
            devices = message(
                device_class,
                "devicesWithMediaType:",
                argtypes=(ctypes.c_void_p,),
                args=(media_type,),
            )
            count = message(devices, "count", ctypes.c_ulong)
            for index in range(count):
                camera = message(
                    devices,
                    "objectAtIndex:",
                    argtypes=(ctypes.c_ulong,),
                    args=(index,),
                )
                unique_id = string(message(camera, "uniqueID"))
                name = string(message(camera, "localizedName"))
                if unique_id and name:
                    discovered[unique_id] = name

        # OpenCV 4.11 AVFoundation combines video/muxed devices and sorts by
        # uniqueID before interpreting a numeric source.
        return [
            _device(
                f"avfoundation:{unique_id}",
                discovered[unique_id],
                "avfoundation",
                index,
            )
            for index, unique_id in enumerate(sorted(discovered))
        ]
    finally:
        message(pool, "drain", restype=None)


def _linux_name(runtime_path, sys_class_dir):
    name_path = Path(sys_class_dir) / Path(runtime_path).name / "name"
    try:
        value = name_path.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    return value or Path(runtime_path).name


def discover_linux_devices(
    by_id_dir="/dev/v4l/by-id",
    sys_class_dir="/sys/class/video4linux",
    fallback_pattern="/dev/video*",
):
    if not sys.platform.startswith("linux"):
        return []
    by_id = Path(by_id_dir)
    result = []
    if by_id.is_dir():
        links = sorted(path for path in by_id.iterdir() if path.is_symlink())
        capture_links = [path for path in links if "video-index0" in path.name]
        for path in capture_links or links:
            try:
                runtime_path = str(path.resolve(strict=True))
            except OSError:
                continue
            if not re.fullmatch(r"video\d+", Path(runtime_path).name):
                continue
            result.append(
                _device(
                    f"v4l2:{path.name}",
                    _linux_name(runtime_path, sys_class_dir),
                    "v4l2",
                    runtime_path,
                )
            )
        if result:
            return result

    for runtime_path in sorted(glob.glob(fallback_pattern)):
        if re.fullmatch(r"/dev/video\d+", runtime_path):
            result.append(
                _device(
                    f"v4l2-path:{runtime_path}",
                    _linux_name(runtime_path, sys_class_dir),
                    "v4l2",
                    runtime_path,
                    stable=False,
                )
            )
    return result


def _windows_pnp_devices():
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-PnpDevice -PresentOnly | Where-Object {$_.Class -in @('Camera','Image')} | Select-Object FriendlyName,InstanceId | ConvertTo-Json -Compress",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode:
        raise CameraIdentityError("Windows camera identity discovery failed.")
    raw = json.loads(completed.stdout or "[]")
    return raw if isinstance(raw, list) else [raw]


def resolve_ffmpeg_executable(*, environment=None, frozen_root=None, which=None):
    """Prefer the vetted frozen FFmpeg tool, preserving PATH fallback for source use."""
    environment = os.environ if environment is None else environment
    configured_tools = environment.get("PHONE_CONTROLLER_TOOLS_DIR")
    roots = []
    if configured_tools:
        roots.append(Path(configured_tools))
    bundle_root = frozen_root
    if bundle_root is None:
        bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        frozen_tools = Path(bundle_root) / "tools"
        roots.append(frozen_tools)
    else:
        frozen_tools = None
    for root in roots:
        candidate = root / "ffmpeg.exe"
        if candidate.is_file():
            return str(candidate)
    if frozen_tools is not None:
        # Production builds are required to contain this exact resource. Do not
        # silently change camera enumeration behavior by consulting global PATH
        # if a frozen installation is damaged or incomplete.
        return str(frozen_tools / "ffmpeg.exe")
    return (which or shutil.which)("ffmpeg") or "ffmpeg"


def _windows_directshow_devices(*, include_status=False):
    """Return DirectShow video devices and their PnP-capable moniker names."""
    executable = resolve_ffmpeg_executable()
    enumeration_error = None
    try:
        completed = subprocess.run(
            [
                executable,
                "-hide_banner",
                "-list_devices",
                "true",
                "-f",
                "dshow",
                "-i",
                "dummy",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        devices = []
        enumeration_error = "ffmpeg_unavailable"
        return (devices, enumeration_error, executable) if include_status else devices
    except subprocess.TimeoutExpired:
        devices = []
        enumeration_error = "directshow_enumeration_timeout"
        return (devices, enumeration_error, executable) if include_status else devices
    except (OSError, subprocess.SubprocessError):
        devices = []
        enumeration_error = "directshow_enumeration_failed"
        return (devices, enumeration_error, executable) if include_status else devices
    devices = []
    current = None
    for line in completed.stderr.splitlines():
        match = re.search(r'"([^"]+)"\s*\(video\)', line)
        if match:
            current = {
                "index": len(devices),
                "name": match.group(1),
                "alternative_name": None,
            }
            devices.append(current)
            continue
        if re.search(r'"[^"]+"\s*\(audio\)', line):
            current = None
            continue
        alternative = re.search(r'Alternative name\s+"([^"]+)"', line)
        if current is not None and alternative:
            current["alternative_name"] = alternative.group(1)
    if not devices:
        enumeration_error = "directshow_no_video_devices"
    return (devices, enumeration_error, executable) if include_status else devices


def _windows_directshow_names():
    """Compatibility wrapper exposing DirectShow names in runtime-index order."""
    return [item["name"] for item in _windows_directshow_devices()]


def _windows_identity_token(value):
    """Normalize a PnP InstanceId or DirectShow moniker for safe comparison."""
    normalized = re.sub(r"[\\\\/#]+", "#", str(value or "").strip().casefold())
    return normalized.strip("#")


def _windows_moniker_matches_instance(instance_id, alternative_name):
    needle = _windows_identity_token(instance_id)
    haystack = _windows_identity_token(alternative_name)
    if not needle or not haystack:
        return False
    start = haystack.find(needle)
    while start >= 0:
        before_ok = start == 0 or haystack[start - 1] in "#_?"
        end = start + len(needle)
        after_ok = end == len(haystack) or haystack[end] == "#"
        if before_ok and after_ok:
            return True
        start = haystack.find(needle, start + 1)
    return False


def _windows_mapping_failure_reason(
    *,
    directshow_devices,
    directshow_error,
    pnp_name_count,
    friendly_candidates,
    moniker_candidates,
):
    if not directshow_devices:
        return directshow_error or "directshow_enumeration_empty"
    if len(moniker_candidates) > 1:
        return "pnp_moniker_ambiguous"
    if len(friendly_candidates) == 0:
        return "not_enumerated_by_directshow"
    if pnp_name_count > 1:
        return "duplicate_pnp_friendly_name"
    if len(friendly_candidates) > 1:
        return "duplicate_directshow_friendly_name"
    return "pnp_moniker_not_matched"


def discover_windows_devices():
    if sys.platform != "win32":
        return []
    pnp_devices = _windows_pnp_devices()
    directshow_devices, directshow_error, directshow_executable = (
        _windows_directshow_devices(include_status=True)
    )
    pnp_name_counts = {}
    for item in pnp_devices:
        if isinstance(item, dict) and item.get("FriendlyName"):
            pnp_name_counts[item["FriendlyName"]] = (
                pnp_name_counts.get(item["FriendlyName"], 0) + 1
            )
    result = []
    for item in pnp_devices:
        name = item.get("FriendlyName") if isinstance(item, dict) else None
        instance_id = item.get("InstanceId") if isinstance(item, dict) else None
        if not name or not instance_id:
            continue
        moniker_candidates = [
            device["index"]
            for device in directshow_devices
            if _windows_moniker_matches_instance(
                instance_id, device.get("alternative_name")
            )
        ]
        friendly_candidates = [
            device["index"]
            for device in directshow_devices
            if device.get("name") == name
        ]
        mapping_method = None
        failure_reason = None
        if len(moniker_candidates) == 1:
            runtime_source = moniker_candidates[0]
            mapping_method = "pnp_moniker"
        elif (
            not moniker_candidates
            and pnp_name_counts.get(name) == 1
            and len(friendly_candidates) == 1
        ):
            # Preserve the previous behavior only when the name is a proven
            # one-to-one join on both sides. Duplicate names remain unresolved.
            runtime_source = friendly_candidates[0]
            mapping_method = "unique_friendly_name"
        else:
            runtime_source = None
            failure_reason = _windows_mapping_failure_reason(
                directshow_devices=directshow_devices,
                directshow_error=directshow_error,
                pnp_name_count=pnp_name_counts.get(name, 0),
                friendly_candidates=friendly_candidates,
                moniker_candidates=moniker_candidates,
            )
        runtime_candidates = moniker_candidates or friendly_candidates
        stable_id = "dshow:" + instance_id
        result.append(
            _device(
                stable_id,
                name,
                "dshow",
                runtime_source,
                resolvable=runtime_source is not None,
                mapping_method=mapping_method,
                mapping_failure_reason=failure_reason,
                runtime_index_candidates=runtime_candidates,
            )
        )
        event = (
            "windows_camera_identity_mapped"
            if runtime_source is not None
            else "windows_camera_identity_unresolved"
        )
        diagnostic_identity = (
            stable_id,
            repr(runtime_source),
            failure_reason or mapping_method,
            tuple(runtime_candidates),
        )
        if EVENT_DEDUPLICATOR.should_log("camera", event, diagnostic_identity):
            log_event(
                LOG,
                logging.INFO if runtime_source is not None else logging.WARNING,
                "camera",
                event,
                message=(
                    "Windows camera identity mapped to DirectShow runtime order."
                    if runtime_source is not None
                    else "Windows camera identity could not be mapped safely."
                ),
                camera_id=stable_id,
                camera_name=name,
                runtime_source=runtime_source,
                runtime_index_candidates=runtime_candidates,
                mapping_method=mapping_method,
                mapping_failure_reason=failure_reason,
                pnp_identities=[
                    value.get("InstanceId")
                    for value in pnp_devices
                    if isinstance(value, dict) and value.get("InstanceId")
                ],
                directshow_video_devices=directshow_devices,
                directshow_enumeration_error=directshow_error,
                directshow_executable=directshow_executable,
                capture_backend="dshow",
                msmf_runtime_mapping_used=False,
            )
    return result


def discover_camera_devices():
    if sys.platform == "darwin":
        return discover_macos_devices()
    if sys.platform.startswith("linux"):
        return discover_linux_devices()
    if sys.platform == "win32":
        return discover_windows_devices()
    return []


def resolve_camera_source(stable_id, *, devices=None, discovery=None):
    if not isinstance(stable_id, str) or not stable_id:
        raise CameraIdentityError("A stable camera ID is required.")
    if devices is None:
        devices = (discovery or discover_camera_devices)()
    matches = [item for item in devices if item.get("stable_id") == stable_id]
    if not matches:
        raise CameraIdentityError(f"Configured camera {stable_id!r} is unavailable.")
    if len(matches) != 1:
        raise CameraIdentityError(
            f"Camera identity {stable_id!r} is ambiguous; refusing to choose a source."
        )
    runtime_source = matches[0].get("runtime_source")
    if runtime_source is None:
        raise CameraIdentityError(
            f"Camera identity {stable_id!r} cannot be mapped safely to a runtime source."
        )
    return runtime_source
