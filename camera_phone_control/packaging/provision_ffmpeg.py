"""Provision one approved FFmpeg executable from a verified EXE or ZIP."""

from __future__ import annotations

import argparse
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

MAX_EXECUTABLE_SIZE = 512 * 1024 * 1024
_ZIP_SIGNATURES = {b"PK\x03\x04", b"PK\x05\x06"}


class FFmpegProvisioningError(RuntimeError):
    pass


def _safe_archive_name(name):
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or name.startswith("/")
        or ":" in name
        or "//" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        return False
    parts = name.split("/")
    if parts[-1] == "":
        parts = parts[:-1]
    return bool(parts) and all(part not in {"", ".", ".."} for part in parts)


def _is_symlink(info):
    return stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK


def _copy_exact(source, destination, expected_size):
    written = 0
    with destination.open("xb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > expected_size or written > MAX_EXECUTABLE_SIZE:
                raise FFmpegProvisioningError(
                    "Approved FFmpeg extraction exceeded its declared size."
                )
            output.write(chunk)
    if written != expected_size:
        raise FFmpegProvisioningError("Approved FFmpeg ZIP extraction was incomplete.")


def provision_ffmpeg(source, destination):
    source = Path(source)
    destination = Path(destination)
    if not source.is_file() or source.is_symlink():
        raise FFmpegProvisioningError("Approved FFmpeg download is missing.")
    if destination.exists() or destination.is_symlink():
        raise FFmpegProvisioningError(
            "Approved FFmpeg destination must not already exist."
        )

    with source.open("rb") as stream:
        signature = stream.read(4)
    try:
        if signature[:2] == b"MZ":
            size = source.stat().st_size
            if size <= 0 or size > MAX_EXECUTABLE_SIZE:
                raise FFmpegProvisioningError(
                    "Approved FFmpeg executable has an invalid size."
                )
            with source.open("rb") as executable:
                _copy_exact(executable, destination, size)
            return destination
        if signature not in _ZIP_SIGNATURES:
            raise FFmpegProvisioningError(
                "Approved FFmpeg download must be a Windows executable or ZIP archive."
            )

        try:
            with zipfile.ZipFile(source, "r") as archive:
                matches = []
                for info in archive.infolist():
                    if not _safe_archive_name(info.filename) or _is_symlink(info):
                        raise FFmpegProvisioningError(
                            "Approved FFmpeg ZIP contains an unsafe entry."
                        )
                    if (
                        not info.is_dir()
                        and PurePosixPath(info.filename).name.casefold() == "ffmpeg.exe"
                    ):
                        matches.append(info)
                if len(matches) != 1:
                    raise FFmpegProvisioningError(
                        "Approved FFmpeg ZIP must contain exactly one ffmpeg.exe entry."
                    )
                match = matches[0]
                if (
                    match.flag_bits & 0x1
                    or match.file_size <= 0
                    or match.file_size > MAX_EXECUTABLE_SIZE
                ):
                    raise FFmpegProvisioningError(
                        "Approved FFmpeg ZIP contains an invalid ffmpeg.exe entry."
                    )
                with archive.open(match, "r") as executable:
                    _copy_exact(executable, destination, match.file_size)
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise FFmpegProvisioningError(
                "Approved FFmpeg ZIP archive is invalid or unsupported."
            ) from exc
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        executable = provision_ffmpeg(args.source, args.destination)
    except FFmpegProvisioningError as exc:
        parser.error(str(exc))
    print(f"Approved FFmpeg candidate provisioned: {executable.resolve()}")


if __name__ == "__main__":
    main()
