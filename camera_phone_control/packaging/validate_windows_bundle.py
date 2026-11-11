"""Validate required offline FFmpeg and PaddleOCR assets in a frozen onedir build."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from prepare_ocr_models import _validate as validate_models
from validate_ffmpeg import validate_ffmpeg


def _validate_installer_definition():
    definition = (
        Path(__file__).with_name("PhoneController.iss").read_text(encoding="utf-8")
    )
    required = (
        'Source: "..\\dist\\PhoneController\\*"',
        'DestDir: "{app}"',
        "recursesubdirs",
        "createallsubdirs",
    )
    if not all(value in definition for value in required):
        raise RuntimeError(
            "Inno Setup no longer installs the complete protected onedir distribution."
        )


def _normalize_archive_name(name):
    value = str(name).replace("\\", "/")
    for suffix in (".pyc", ".pyo", ".py"):
        if value.casefold().endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value.strip("/").replace("/", ".")


def _archive_modules(executable):
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as exc:
        raise RuntimeError(
            "PyInstaller is required for protected archive inspection."
        ) from exc
    try:
        archive = CArchiveReader(str(executable))
    except Exception as exc:
        raise RuntimeError(f"Could not inspect PyInstaller executable: {exc}") from exc

    exe_modules = set()
    pyz_modules = set()
    for name, entry in archive.toc.items():
        typecode = entry[-1]
        if typecode in {"m", "M", "s"}:
            exe_modules.add(_normalize_archive_name(name))
        if typecode == "z":
            try:
                nested = archive.open_embedded_archive(name)
            except Exception as exc:
                raise RuntimeError(
                    f"Could not inspect nested PyInstaller archive {name!r}: {exc}"
                ) from exc
            pyz_modules.update(_normalize_archive_name(item) for item in nested.toc)
    if not pyz_modules:
        raise RuntimeError("No nested PYZ archive was found in PhoneController.exe.")
    return exe_modules, pyz_modules


def _validate_protected_modules(distribution, resources, manifest_path):
    manifest_path = Path(manifest_path).resolve()
    if not manifest_path.is_file():
        raise RuntimeError(f"Protected build manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    extensions = manifest.get("extensions") or {}
    extension_hashes = manifest.get("extension_sha256") or {}
    compiled_modules = manifest.get("compiled_modules") or []
    if set(extensions) != set(compiled_modules) or set(extension_hashes) != set(
        compiled_modules
    ):
        raise RuntimeError("Protected manifest extension set is incomplete.")

    attestation_path = resources / "protected-attestation.json"
    if not attestation_path.is_file():
        raise RuntimeError("Protected build attestation is missing from the bundle.")
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    for field in (
        "build_id",
        "compiled_modules",
        "bootstrap_modules",
        "excluded_modules",
        "extensions",
        "extension_sha256",
    ):
        if attestation.get(field) != manifest.get(field):
            raise RuntimeError(
                f"Protected bundle attestation does not match this build: {field}."
            )

    missing_extensions = [
        module
        for module, relative in extensions.items()
        if not (resources / relative).is_file()
    ]
    if missing_extensions:
        raise RuntimeError(
            "Required protected native extensions are missing: "
            + ", ".join(missing_extensions)
        )
    mismatched_extensions = [
        module
        for module, relative in extensions.items()
        if hashlib.sha256((resources / relative).read_bytes()).hexdigest()
        != extension_hashes[module]
    ]
    if mismatched_extensions:
        raise RuntimeError(
            "Protected native extension hashes do not match this build: "
            + ", ".join(mismatched_extensions)
        )

    forbidden_files = []
    for relative in manifest.get("compiled_sources", []):
        source = Path(relative)
        for root in (distribution, resources):
            parent = root / source.parent
            for suffix in (".py", ".pyc", ".c", ".cpp"):
                candidate = parent / (source.stem + suffix)
                if candidate.exists():
                    forbidden_files.append(candidate)
    if forbidden_files:
        raise RuntimeError(
            "Proprietary source/development files were distributed: "
            + ", ".join(str(path) for path in forbidden_files)
        )

    exe_modules, pyz_modules = _archive_modules(distribution / "PhoneController.exe")
    protected_names = set(compiled_modules) | set(manifest.get("excluded_modules", []))
    archived_in_exe = sorted(protected_names & exe_modules)
    archived_in_pyz = sorted(protected_names & pyz_modules)
    if archived_in_exe or archived_in_pyz:
        locations = []
        if archived_in_exe:
            locations.append("EXE: " + ", ".join(archived_in_exe))
        if archived_in_pyz:
            locations.append("PYZ: " + ", ".join(archived_in_pyz))
        raise RuntimeError(
            "Proprietary modules remain recoverable from PyInstaller archives: "
            + "; ".join(locations)
        )
    return manifest


def _run_frozen_smoke_test(executable):
    with tempfile.TemporaryDirectory(prefix="phone-controller-package-smoke-") as data:
        environment = os.environ.copy()
        environment["PHONE_CONTROLLER_DATA_DIR"] = data
        environment["PHONE_CONTROLLER_PACKAGE_SMOKE_TEST"] = "1"
        try:
            completed = subprocess.run(
                [str(executable), "--package-smoke-test"],
                env=environment,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "Frozen application UI/API smoke test timed out."
            ) from exc
    if completed.returncode != 0:
        raise RuntimeError(
            "Frozen application UI/API smoke test failed with exit code "
            f"{completed.returncode}."
        )


def validate_bundle(distribution, *, manifest_path=None, run_smoke_test=True):
    distribution = Path(distribution).resolve()
    executable = distribution / "PhoneController.exe"
    resources = distribution / "_internal"
    if not executable.is_file():
        raise RuntimeError(f"Frozen application executable is missing: {executable}")
    if not resources.is_dir():
        raise RuntimeError(f"Frozen resource directory is missing: {resources}")
    validate_ffmpeg(resources / "tools" / "ffmpeg.exe")
    validate_models(resources / "paddle_models")
    manifest_path = manifest_path or os.environ.get(
        "PHONE_CONTROLLER_PROTECTED_MANIFEST", ""
    )
    if not manifest_path:
        raise RuntimeError("PHONE_CONTROLLER_PROTECTED_MANIFEST is required.")
    protected_manifest = _validate_protected_modules(
        distribution, resources, manifest_path
    )
    print(
        "Protected archive verified: "
        f"build_id={protected_manifest['build_id']} "
        f"native_modules={len(protected_manifest['compiled_modules'])} "
        "protected_exe_bytecode=0 protected_pyz_bytecode=0"
    )
    forbidden = tuple(distribution.rglob(".env")) + tuple(
        distribution.rglob("tesseract.exe")
    )
    forbidden += tuple(
        path
        for path in distribution.rglob("*")
        if path.is_file()
        and (
            path.suffix.casefold() in {".key", ".p12", ".pfx"}
            or path.name.casefold() in {"id_rsa", "id_ed25519"}
        )
    )
    if forbidden:
        raise RuntimeError(
            "Forbidden secret/Tesseract packaging input found: "
            + ", ".join(str(path) for path in forbidden)
        )
    _validate_installer_definition()
    if run_smoke_test:
        _run_frozen_smoke_test(executable)
    return distribution


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("distribution", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--skip-smoke-test", action="store_true")
    args = parser.parse_args(argv)
    try:
        distribution = validate_bundle(
            args.distribution,
            manifest_path=args.manifest,
            run_smoke_test=not args.skip_smoke_test,
        )
    except RuntimeError as exc:
        parser.error(str(exc))
    print(f"Frozen offline assets ready: {distribution}")


if __name__ == "__main__":
    main()
