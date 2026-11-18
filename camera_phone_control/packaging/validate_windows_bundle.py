"""Validate required offline FFmpeg and PaddleOCR assets in a frozen onedir build."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from prepare_ocr_models import _validate as validate_models
from validate_ffmpeg import validate_ffmpeg

CHARDET_MYPYC_MODULES = (
    "models._format",
    "pipeline.ascii",
    "pipeline.confusion",
    "pipeline.escape",
    "pipeline.language",
    "pipeline.magic",
    "pipeline.markup",
    "pipeline.orchestrator",
    "pipeline.postprocess",
    "pipeline.statistical",
    "pipeline.structural",
    "pipeline.utf1632",
    "pipeline.utf8",
    "pipeline.validity",
)

PADDLEX_OCR_CORE_METADATA = (
    "imagesize",
    "opencv-contrib-python",
    "pyclipper",
    "pypdfium2",
    "python-bidi",
    "shapely",
)


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


def _read_diagnostic(path):
    path = Path(path)
    if not path.is_file():
        return "<not created>"
    try:
        value = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"<could not read {path}: {exc}>"
    return value if value else "<empty>"


def _smoke_failure_details(
    *, command, cwd, exit_code, stdout, stderr, diagnostic_paths
):
    def output_text(value):
        if value is None or value == "":
            return "<empty>"
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    sections = [
        "Frozen application UI/API smoke-test diagnostics:",
        f"command: {subprocess.list2cmdline([os.fspath(item) for item in command])}",
        f"cwd: {cwd}",
        f"exit code: {exit_code}",
        "captured stdout:\n" + output_text(stdout),
        "captured stderr:\n" + output_text(stderr),
    ]
    for label, path in diagnostic_paths:
        sections.append(f"{label} ({path}):\n{_read_diagnostic(path)}")
    return "\n\n".join(sections)


def _validate_smoke_report(report_path, compiled_modules):
    if not report_path.is_file():
        raise RuntimeError("Frozen application did not create its smoke-test report.")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Frozen smoke-test report is invalid: {exc}") from exc

    if report.get("exit_code") != 0 or report.get("phase") != "complete":
        raise RuntimeError(
            "Frozen smoke-test report did not record successful completion."
        )
    imported = {
        item.get("module")
        for item in report.get("compiled_imports", [])
        if isinstance(item, dict)
    }
    expected = set(compiled_modules)
    if imported != expected:
        missing = sorted(expected - imported)
        unexpected = sorted(imported - expected)
        raise RuntimeError(
            "Frozen smoke test did not import the exact protected module set: "
            f"missing={missing!r} unexpected={unexpected!r}."
        )
    if report.get("multiprocessing_exit_code") != 0:
        raise RuntimeError("Frozen multiprocessing smoke check did not succeed.")
    ocr_status = report.get("ocr_worker_status")
    workers = ocr_status.get("workers", []) if isinstance(ocr_status, dict) else []
    if not workers or any(item.get("state") != "IDLE" for item in workers):
        raise RuntimeError("Frozen PaddleOCR worker/model smoke check did not succeed.")
    language_statuses = report.get("ocr_language_statuses")
    if not isinstance(language_statuses, dict) or set(language_statuses) != {
        "en",
        "zh-HK",
        "zh-TW",
    }:
        raise RuntimeError("Frozen OCR language smoke checks were incomplete.")
    for language, status in language_statuses.items():
        language_workers = status.get("workers", []) if isinstance(status, dict) else []
        if not language_workers or any(
            item.get("state") != "IDLE" for item in language_workers
        ):
            raise RuntimeError(
                f"Frozen {language} PaddleOCR worker/model smoke check failed."
            )
    inference_results = report.get("ocr_inference_results")
    if not isinstance(inference_results, list) or not all(
        isinstance(item, dict) for item in inference_results
    ):
        raise RuntimeError("Frozen OCR inference smoke checks were not reported.")
    expected_inference_languages = {"en", "zh-HK", "ms"}
    reported_inference_languages = {
        item.get("language") for item in inference_results if isinstance(item, dict)
    }
    if reported_inference_languages != expected_inference_languages:
        raise RuntimeError(
            "Frozen OCR inference smoke checks were incomplete: "
            f"{sorted(reported_inference_languages)!r}."
        )
    expected_models = {
        "en": "en_PP-OCRv5_mobile_rec",
        "zh-HK": "PP-OCRv5_mobile_rec",
        "ms": "latin_PP-OCRv5_mobile_rec",
    }
    for item in inference_results:
        if (
            type(item.get("result_batch_count")) is not int
            or item["result_batch_count"] < 1
            or type(item.get("region_count")) is not int
            or item["region_count"] < 1
            or type(item.get("inference_ms")) not in (int, float)
            or not math.isfinite(item["inference_ms"])
            or item["inference_ms"] < 0
            or not isinstance(item.get("worker_id"), int)
        ):
            raise RuntimeError(
                "Frozen OCR inference smoke check returned invalid diagnostics."
            )
        if item.get("recognition_model") != expected_models[item["language"]]:
            raise RuntimeError(
                "Frozen OCR inference used an unexpected recognition model."
            )
        if item.get("fixture_matched") is not True or item.get("console_streams_absent") is not True:
            raise RuntimeError("Frozen OCR fixture/absent-stream validation failed.")
    if report.get("console_streams_absent") is not True:
        raise RuntimeError("Frozen windowed application had console streams.")
    recovery = report.get("ocr_recovery", {})
    roi_reports = report.get("roi_api", {})
    if set(recovery) != expected_inference_languages or set(roi_reports) != expected_inference_languages:
        raise RuntimeError("Frozen OCR recovery/ROI checks were incomplete.")
    for language in expected_inference_languages:
        item = recovery[language]
        subsequent = item.get("subsequent_results", [])
        status = item.get("status", {})
        if (
            not item.get("controlled_error", {}).get("wrapper_type")
            or len(subsequent) != 3
            or any(result.get("fixture_matched") is not True or result.get("console_streams_absent") is not True for result in subsequent)
            or status.get("busy_workers") != 0
            or status.get("queued_requests") != 0
        ):
            raise RuntimeError(f"Frozen {language} OCR recovery check failed.")
        roi = roi_reports[language]
        if (
            len(roi) != 3
            or [result.get("found") for result in roi] != [True, False, True]
            or any(result.get("status_code") != 200 for result in roi)
        ):
            raise RuntimeError(f"Frozen {language} ROI API check failed.")
    if report.get("runtime_model_download_count") != 0:
        raise RuntimeError("Frozen OCR attempted to create runtime model downloads.")
    shutdown_statuses = report.get("ocr_shutdown_statuses")
    if not isinstance(shutdown_statuses, dict) or set(shutdown_statuses) != {
        "en",
        "zh-HK",
        "ms",
    }:
        raise RuntimeError("Frozen OCR shutdown checks were incomplete.")
    for language, status in shutdown_statuses.items():
        workers = status.get("workers", []) if isinstance(status, dict) else []
        if (
            status.get("accepting") is not False
            or status.get("busy_workers") != 0
            or status.get("queued_requests") != 0
            or not workers
            or any(worker.get("pid") is not None for worker in workers)
        ):
            raise RuntimeError(
                f"Frozen {language} OCR shutdown check did not succeed."
            )
    endpoint_statuses = {
        item.get("endpoint"): item.get("status_code")
        for item in report.get("endpoints", [])
        if isinstance(item, dict)
    }
    expected_endpoints = {
        "/": 200,
        "/api/status": 200,
        "/static/app.js": 200,
        "/static/i18n.js": 200,
        "/static/workflows.js": 200,
        "/static/style.css": 200,
        "/api/workflow-options/transfer-methods": 200,
    }
    if endpoint_statuses != expected_endpoints:
        raise RuntimeError(
            "Frozen Flask endpoint checks were incomplete or unsuccessful: "
            f"{endpoint_statuses!r}."
        )
    http_health = report.get("http_health")
    if not isinstance(http_health, dict) or (
        http_health.get("status_code") != 200
        or http_health.get("thread_stopped") is not True
    ):
        raise RuntimeError("Frozen local HTTP listener health check did not succeed.")
    if report.get("transfer_method_values") != [
        "bank_account",
        "email",
        "fps_id",
        "phone_no",
    ]:
        raise RuntimeError(
            "Frozen transfer-method registry check was incomplete or unsuccessful."
        )
    expected_remoteviewer_routes = {
        ("/agent/v1/devices/<device_id>/frames", "GET"),
        ("/agent/v1/devices/<device_id>/takeover", "POST"),
        ("/agent/v1/devices/<device_id>/takeover/renew", "POST"),
        ("/agent/v1/devices/<device_id>/takeover", "DELETE"),
        ("/agent/v1/devices/<device_id>/input", "POST"),
        ("/agent/v1/devices/<device_id>/screen", "GET"),
        ("/agent/v1/devices/<device_id>/screen", "PUT"),
        ("/api/remote-control", "GET"),
        ("/api/devices/<device_id>/remote-control/end", "POST"),
    }
    reported_routes = {
        (item.get("route"), item.get("method"))
        for item in report.get("remoteviewer_routes", [])
        if isinstance(item, dict)
    }
    if reported_routes != expected_remoteviewer_routes:
        raise RuntimeError(
            "Frozen RemoteViewer route checks were incomplete: "
            f"{sorted(reported_routes)!r}."
        )
    return report


def _run_frozen_smoke_test(executable, compiled_modules):
    with tempfile.TemporaryDirectory(prefix="phone-controller-package-smoke-") as data:
        data_root = Path(data)
        report_path = data_root / "frozen-smoke-report.json"
        early_report_path = data_root / "frozen-smoke-early-traceback.txt"
        stdout_path = data_root / "frozen-smoke-stdout.txt"
        stderr_path = data_root / "frozen-smoke-stderr.txt"
        command = [str(executable), "--package-smoke-test"]
        diagnostic_paths = (
            ("launcher smoke report", report_path),
            ("early unhandled traceback", early_report_path),
            ("redirected stdout", stdout_path),
            ("redirected stderr", stderr_path),
            ("persistent application log", data_root / "logs" / "phone-controller.log"),
            ("startup error log", data_root / "logs" / "startup-error.log"),
        )
        environment = os.environ.copy()
        environment["PHONE_CONTROLLER_DATA_DIR"] = data
        environment["PHONE_CONTROLLER_PACKAGE_SMOKE_TEST"] = "1"
        environment["PHONE_CONTROLLER_PACKAGE_SMOKE_REPORT"] = os.fspath(report_path)
        environment["PHONE_CONTROLLER_PACKAGE_SMOKE_EARLY_REPORT"] = os.fspath(
            early_report_path
        )
        # Keep the windowed bootloader's absent streams in parent AND children.
        environment.pop("PHONE_CONTROLLER_PACKAGE_SMOKE_STDOUT", None)
        environment.pop("PHONE_CONTROLLER_PACKAGE_SMOKE_STDERR", None)
        environment["CAMERA_OCR_LANGUAGE"] = "zh-HK"
        environment["CAMERA_OCR_WORKER_POOL_SIZE"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        environment["HTTP_PROXY"] = "http://127.0.0.1:9"
        environment["HTTPS_PROXY"] = "http://127.0.0.1:9"
        environment["ALL_PROXY"] = "http://127.0.0.1:9"
        environment["NO_PROXY"] = "127.0.0.1,localhost"
        completed = None
        try:
            try:
                completed = subprocess.run(
                    command,
                    cwd=executable.parent,
                    env=environment,
                    timeout=240,
                    check=False,
                    capture_output=True,
                    text=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    "Frozen application UI/API smoke test timed out.\n\n"
                    + _smoke_failure_details(
                        command=command,
                        cwd=executable.parent,
                        exit_code="<timed out after 240 seconds>",
                        stdout=exc.stdout,
                        stderr=exc.stderr,
                        diagnostic_paths=diagnostic_paths,
                    )
                ) from exc
            try:
                if completed.returncode != 0:
                    raise RuntimeError(
                        "Frozen application UI/API smoke test failed.\n\n"
                        + _smoke_failure_details(
                            command=command,
                            cwd=executable.parent,
                            exit_code=completed.returncode,
                            stdout=completed.stdout,
                            stderr=completed.stderr,
                            diagnostic_paths=diagnostic_paths,
                        )
                    )
                report = _validate_smoke_report(report_path, compiled_modules)
            except RuntimeError as exc:
                if "Frozen application UI/API smoke test failed" in str(exc):
                    raise
                raise RuntimeError(
                    f"{exc}\n\n"
                    + _smoke_failure_details(
                        command=command,
                        cwd=executable.parent,
                        exit_code=completed.returncode,
                        stdout=completed.stdout,
                        stderr=completed.stderr,
                        diagnostic_paths=diagnostic_paths,
                    )
                ) from exc
        finally:
            diagnostics_value = os.environ.get(
                "PHONE_CONTROLLER_PACKAGE_DIAGNOSTICS_DIR", ""
            ).strip()
            if diagnostics_value:
                diagnostics_root = Path(diagnostics_value).resolve()
                diagnostics_root.mkdir(parents=True, exist_ok=True)
                for label, path in diagnostic_paths:
                    if path.is_file():
                        filename = label.replace(" ", "-") + path.suffix
                        shutil.copy2(path, diagnostics_root / filename)
                process_report = {
                    "command": [os.fspath(item) for item in command],
                    "return_code": (
                        completed.returncode if completed is not None else None
                    ),
                }
                (diagnostics_root / "frozen-smoke-process.json").write_text(
                    json.dumps(process_report, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
        print(
            "Frozen application smoke test verified: "
            f"native_modules={len(report['compiled_imports'])} "
            "multiprocessing_exit_code=0 "
            "endpoints=/,/api/status,/static/app.js,/static/i18n.js,"
            "/static/workflows.js,/static/style.css,"
            "/api/workflow-options/transfer-methods "
            "remoteviewer_routes=9"
        )


def _forbidden_bundle_inputs(distribution):
    distribution = Path(distribution)
    forbidden = tuple(distribution.rglob(".env")) + tuple(
        distribution.rglob("tesseract.exe")
    )
    forbidden += tuple(
        path
        for path in distribution.rglob("*")
        if path.is_file()
        and (
            path.suffix.casefold() in {".key", ".p12", ".pfx", ".secret"}
            or path.name.casefold() in {"id_rsa", "id_ed25519"}
        )
    )
    return forbidden


def _validate_cython_runtime_data(resources):
    cpp_support = Path(resources) / "Cython" / "Utility" / "CppSupport.cpp"
    if (
        not cpp_support.is_file()
        or cpp_support.is_symlink()
        or cpp_support.stat().st_size == 0
    ):
        raise RuntimeError(
            "Frozen Cython runtime utility data is missing or invalid: "
            f"{cpp_support}"
        )
    return cpp_support


def _validate_chardet_runtime_extensions(resources):
    chardet_root = Path(resources) / "chardet"
    invalid = []
    for module in CHARDET_MYPYC_MODULES:
        relative = Path(*module.split("."))
        matches = tuple(
            (chardet_root / relative.parent).glob(f"{relative.name}__mypyc*.pyd")
        )
        if (
            len(matches) != 1
            or not matches[0].is_file()
            or matches[0].is_symlink()
            or matches[0].stat().st_size == 0
        ):
            invalid.append(module)
    if invalid:
        raise RuntimeError(
            "Frozen chardet mypyc runtime extensions are missing, empty, "
            "symlinked, or ambiguous: " + ", ".join(invalid)
        )
    return chardet_root


def _validate_paddlex_ocr_core_metadata(resources):
    resources = Path(resources)
    discovered = {}
    for dist in importlib.metadata.distributions(path=[os.fspath(resources)]):
        name = dist.metadata.get("Name")
        if name:
            discovered.setdefault(name.casefold(), []).append(dist)
    invalid = []
    for distribution in PADDLEX_OCR_CORE_METADATA:
        matches = discovered.get(distribution.casefold(), ())
        if (
            len(matches) != 1
            or not matches[0].version
            or not (Path(matches[0]._path) / "METADATA").is_file()
            or (Path(matches[0]._path) / "METADATA").is_symlink()
        ):
            invalid.append(distribution)
    if invalid:
        raise RuntimeError(
            "Frozen PaddleX OCR-core distribution metadata is missing, empty, "
            "symlinked, or ambiguous: " + ", ".join(invalid)
        )
    return resources


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
    _validate_cython_runtime_data(resources)
    _validate_chardet_runtime_extensions(resources)
    _validate_paddlex_ocr_core_metadata(resources)
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
    forbidden = _forbidden_bundle_inputs(distribution)
    if forbidden:
        raise RuntimeError(
            "Forbidden secret/Tesseract packaging input found: "
            + ", ".join(str(path) for path in forbidden)
        )
    _validate_installer_definition()
    if run_smoke_test:
        _run_frozen_smoke_test(executable, protected_manifest["compiled_modules"])
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
