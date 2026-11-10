"""Validate the approved self-contained Windows FFmpeg build input."""

from __future__ import annotations

import argparse
import struct
import subprocess
from pathlib import Path

PE_MACHINE_AMD64 = 0x8664


class FFmpegValidationError(RuntimeError):
    pass


def _pe_machine(path):
    with Path(path).open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise FFmpegValidationError("FFmpeg is not a Windows PE executable.")
        stream.seek(0x3C)
        offset_bytes = stream.read(4)
        if len(offset_bytes) != 4:
            raise FFmpegValidationError("FFmpeg has an incomplete PE header.")
        stream.seek(struct.unpack("<I", offset_bytes)[0])
        if stream.read(4) != b"PE\0\0":
            raise FFmpegValidationError("FFmpeg has an invalid PE signature.")
        machine = stream.read(2)
        if len(machine) != 2:
            raise FFmpegValidationError("FFmpeg has an incomplete COFF header.")
        return struct.unpack("<H", machine)[0]


def validate_ffmpeg(path):
    executable = Path(path).resolve()
    if not executable.is_file() or executable.is_symlink():
        raise FFmpegValidationError(
            f"Required approved FFmpeg executable is missing: {executable}"
        )
    if _pe_machine(executable) != PE_MACHINE_AMD64:
        raise FFmpegValidationError("FFmpeg must be a Windows x64 executable.")
    try:
        version = subprocess.run(
            [str(executable), "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        devices = subprocess.run(
            [str(executable), "-hide_banner", "-devices"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FFmpegValidationError(
            "FFmpeg could not run. Use an approved self-contained Windows x64 "
            "build; dynamically linked builds with missing DLLs are unsupported."
        ) from exc
    if (
        version.returncode != 0
        or "ffmpeg version" not in (version.stdout + version.stderr).casefold()
    ):
        raise FFmpegValidationError("FFmpeg version validation failed.")
    if (
        devices.returncode != 0
        or "dshow" not in (devices.stdout + devices.stderr).casefold()
    ):
        raise FFmpegValidationError(
            "FFmpeg does not expose the required DirectShow input device."
        )
    return executable


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    args = parser.parse_args(argv)
    try:
        executable = validate_ffmpeg(args.executable)
    except FFmpegValidationError as exc:
        parser.error(str(exc))
    print(f"Approved FFmpeg ready: {executable}")


if __name__ == "__main__":
    main()
