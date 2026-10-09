"""Cross-platform discovery and resolution of physical camera identities."""

from __future__ import annotations

import ctypes
import glob
import json
import re
import subprocess
import sys
from pathlib import Path


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
    completed = subprocess.run(command, capture_output=True, text=True, timeout=10)
    if completed.returncode:
        raise CameraIdentityError("Windows camera identity discovery failed.")
    raw = json.loads(completed.stdout or "[]")
    return raw if isinstance(raw, list) else [raw]


def _windows_directshow_names():
    """Use ffmpeg's DirectShow enumeration when available for runtime order."""
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
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
        )
    except (OSError, subprocess.SubprocessError):
        return []
    names = []
    for line in completed.stderr.splitlines():
        match = re.search(r'"([^"]+)"\s*\(video\)', line)
        if match:
            names.append(match.group(1))
    return names


def discover_windows_devices():
    if sys.platform != "win32":
        return []
    pnp_devices = _windows_pnp_devices()
    dshow_names = _windows_directshow_names()
    name_counts = {name: dshow_names.count(name) for name in set(dshow_names)}
    result = []
    for item in pnp_devices:
        name = item.get("FriendlyName") if isinstance(item, dict) else None
        instance_id = item.get("InstanceId") if isinstance(item, dict) else None
        if not name or not instance_id:
            continue
        # A unique friendly name can join PnP identity to DirectShow order. If
        # it is absent or duplicated, leave it unresolved instead of guessing.
        runtime_source = dshow_names.index(name) if name_counts.get(name) == 1 else None
        result.append(
            _device(
                "dshow:" + instance_id,
                name,
                "dshow",
                runtime_source,
                resolvable=runtime_source is not None,
            )
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
