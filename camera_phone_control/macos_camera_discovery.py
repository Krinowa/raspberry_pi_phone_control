"""Backward-compatible AVFoundation discovery wrapper."""

from hardware.camera_identity import discover_macos_devices


def discover_avfoundation_devices():
    return [
        {
            "unique_id": item["stable_id"].split(":", 1)[1],
            "name": item["name"],
            "index": item["runtime_source"],
        }
        for item in discover_macos_devices()
    ]
