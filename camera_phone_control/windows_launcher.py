"""Silent Windows launcher for the packaged Phone Controller dashboard."""

from __future__ import annotations

import argparse
import base64
import ctypes
import importlib
import importlib.machinery
import json
import logging
import math
import multiprocessing
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from ctypes import wintypes
from pathlib import Path

from diagnostic_logging import (
    application_version,
    configure_production_logging,
    log_event,
    register_sensitive_environment,
    sanitize_text,
    write_startup_error,
)
from windows_runtime import RuntimePaths, default_data_root

LOG = logging.getLogger("windows-launcher")
APP_MUTEX_NAME = "Local\\PhoneController.Backend"
SHUTDOWN_EVENT_NAME = "Local\\PhoneController.Shutdown"
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0
HEALTH_CLIENT = "launcher-health"
PACKAGE_SMOKE_OCR_LANGUAGES = ("en", "zh-HK", "ms")


def _package_smoke_ocr_inference(provider, language, image, expected=None):
    """Run real inference; retain text only for explicitly supplied public fixtures."""
    from automation.screen.camera_ocr import _ocr_language_profile

    worker_for_language = getattr(provider, "_worker_for_language", None)
    if worker_for_language is None:
        raise RuntimeError("Frozen application has no OCR language inference hook.")
    results, timings = worker_for_language(language).request(image)
    if not isinstance(results, list) or not isinstance(timings, dict):
        raise RuntimeError(
            f"Frozen {language} OCR inference returned an invalid response."
        )
    if expected is not None and timings.get("console_streams_absent") is not True:
        raise RuntimeError("Frozen OCR child must retain absent console streams.")
    inference = timings.get("inference")
    if (
        type(inference) not in (int, float)
        or not math.isfinite(inference)
        or inference < 0
    ):
        raise RuntimeError(
            f"Frozen {language} OCR inference returned invalid timing data."
        )
    region_count = 0
    recognized = []
    confidences = []
    for item in results:
        if not isinstance(item, dict):
            raise RuntimeError(
                f"Frozen {language} OCR inference returned an invalid result."
            )
        result = item.get("res", item)
        texts = result.get("rec_texts", []) if isinstance(result, dict) else None
        if not isinstance(texts, list):
            raise RuntimeError(
                f"Frozen {language} OCR inference returned invalid recognition data."
            )
        region_count += len(texts)
        recognized.extend(texts)
        confidences.extend(result.get("rec_scores", []))
    if expected is not None and (
        "".join(expected.split()).casefold()
        != "".join("".join(recognized).split()).casefold()
    ):
        raise RuntimeError(
            f"Frozen {language} OCR did not recognize its public fixture: {recognized!r}."
        )
    return {
        "language": language,
        "recognition_model": _ocr_language_profile(language)[2],
        "result_batch_count": len(results),
        "region_count": region_count,
        "inference_ms": round(inference * 1000, 3),
        "worker_id": timings.get("worker_id") or timings.get("worker_pid"),
        **({
            "fixture_matched": True,
            "console_streams_absent": True,
            "recognized_texts": recognized,
            "confidence_scores": confidences,
            "worker_pid": timings.get("worker_pid"),
            "request_id": timings.get("request_id"),
        } if expected is not None else {}),
    }


def _package_smoke_fixtures(resources):
    import cv2
    import numpy as np

    data = json.loads((resources / "ocr_smoke_fixtures.json").read_text(encoding="utf-8"))
    fixtures = {}
    for item in data["fixtures"]:
        image = cv2.imdecode(
            np.frombuffer(base64.b64decode(item["png_base64"], validate=True), np.uint8),
            cv2.IMREAD_COLOR,
        )
        if image is None or not item["expected"].strip():
            raise RuntimeError("Invalid packaged OCR fixture.")
        fixtures[item["language"]] = (image, item["expected"])
    if set(fixtures) != set(PACKAGE_SMOKE_OCR_LANGUAGES):
        raise RuntimeError("Packaged OCR language fixtures are incomplete.")
    return fixtures


def _package_smoke_recovery(provider, language, image, expected, report=None):
    """Exercise real predictor error IPC, then require fresh valid inference."""
    import numpy as np
    from automation.screen.state import ScreenStateError

    pool = provider._worker_for_language(language)
    report = {} if report is None else report
    report["status_before"] = pool.status()
    try:
        pool.request(np.zeros((0, 0, 3), dtype=np.uint8))
    except ScreenStateError as exc:
        diagnostics = dict(getattr(exc, "ocr_diagnostics", {}))
        diagnostics.update(
            wrapper_type=type(exc).__name__,
            message=sanitize_text(str(exc)),
            worker_pid=getattr(exc, "ocr_worker_id", None),
        )
    else:
        raise RuntimeError(f"Frozen {language} OCR accepted an empty fixture image.")
    report["controlled_error"] = diagnostics
    report["status_after_error"] = pool.status()
    report["subsequent_results"] = []
    for _ in range(3):
        report["subsequent_results"].append(
            _package_smoke_ocr_inference(provider, language, image, expected)
        )
    status = pool.status()
    report["status"] = status
    if status["busy_workers"] or status["queued_requests"]:
        raise RuntimeError("Frozen OCR retained a request after recovery.")
    return report


def _package_smoke_roi(app, provider, fixtures):
    """Use fixture snapshots at the camera boundary; real API, crop and OCR."""
    client = app.test_client()
    client.environ_base["HTTP_X_CONTROL_TOKEN"] = app.config["CONTROL_TOKEN"]
    with client.session_transaction() as session:
        session["username"] = "package-smoke"
        session["role"] = "admin"
    original_capture = provider.capture_snapshot
    reports = {}
    try:
        for language, (image, expected) in fixtures.items():
            provider.capture_snapshot = lambda: image.copy()
            # The ROI handler creates an isolated provider from this language.
            original_language = provider.language
            provider.language = language
            try:
                preview = client.post("/api/ocr/roi-preview", json={})
                if preview.status_code != 200:
                    raise RuntimeError(f"Frozen ROI preview failed: {preview.get_data(as_text=True)}")
                token = preview.get_json()["token"]
                responses = []
                for target, expected_found in ((expected, True), ("ZZZUNMATCHEDZZZ", False), (expected, True)):
                    response = client.post("/api/ocr/roi-test", json={
                        "token": token,
                        "roi": {"x": 0, "y": 0, "width": 1, "height": 1},
                        "text": target,
                    })
                    body = response.get_json()
                    if response.status_code != 200 or body.get("found") is not expected_found:
                        raise RuntimeError(f"Frozen {language} ROI verification failed: {body!r}")
                    responses.append({"status_code": response.status_code, "found": body["found"], "texts": body["texts"]})
                reports[language] = responses
            finally:
                provider.language = original_language
    finally:
        provider.capture_snapshot = original_capture
    return reports


def _configure_logging(log_directory: Path):
    return configure_production_logging(
        log_directory, version=application_version(), packaged=True
    )


def _message_box(title, message, style=0x10):
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, str(message), str(title), style)
    else:
        LOG.error("Could not display Windows message box: %s", title)


def _win32_handle(function, *args):
    handle = function(*args)
    if not handle:
        raise ctypes.WinError()
    return handle


def _acquire_instance_mutex():
    if os.name != "nt":
        return None, False
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = _win32_handle(kernel32.CreateMutexW, None, False, APP_MUTEX_NAME)
    return handle, kernel32.GetLastError() == ERROR_ALREADY_EXISTS


def _create_shutdown_event():
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateEventW.argtypes = (
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.CreateEventW.restype = wintypes.HANDLE
    return _win32_handle(kernel32.CreateEventW, None, True, False, SHUTDOWN_EVENT_NAME)


def _signal_shutdown():
    if os.name != "nt":
        return False
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, SHUTDOWN_EVENT_NAME)
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def _close_handle(handle):
    if handle and os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)


def _dashboard_healthy(url, timeout=1.0):
    request = urllib.request.Request(
        url.rstrip("/") + "/api/status",
        headers={"X-Control-Client": HEALTH_CLIENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def _wait_for_dashboard(url, deadline_seconds=90.0):
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if _dashboard_healthy(url):
            return True
        time.sleep(0.25)
    return False


def _open_when_ready(url):
    if _wait_for_dashboard(url):
        webbrowser.open(url, new=2)
    else:
        LOG.error("Dashboard health check did not succeed within the startup deadline.")
        _message_box(
            "Phone Controller",
            "Phone Controller did not become ready. Check the support log in "
            "%LOCALAPPDATA%\\Phone Controller\\logs.",
        )


def _prepare_environment(paths):
    from dotenv import load_dotenv

    load_dotenv(paths.config / ".env", override=False)
    os.environ.setdefault("PHONE_APP_DATA_DIR", os.fspath(paths.data))
    os.environ.setdefault(
        "PHONE_CONTROLLER_OCR_MODELS_DIR", os.fspath(paths.ocr_models)
    )
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", os.fspath(paths.cache / "paddlex"))
    tools = paths.resources / "tools"
    if tools.is_dir():
        os.environ.setdefault("PHONE_CONTROLLER_TOOLS_DIR", os.fspath(tools))
        os.environ["PATH"] = os.fspath(tools) + os.pathsep + os.environ.get("PATH", "")

    from automation.auth import load_or_create_secret_key

    os.environ.setdefault(
        "PHONE_SESSION_SECRET", load_or_create_secret_key(paths.config)
    )
    register_sensitive_environment()


class _PackageSmokeCamera:
    """Thread-free camera used only by the frozen packaging smoke test."""

    source = None
    configured_source = None
    rotate = 0
    fps = 0

    def __init__(self):
        self.closed = False

    def status(self):
        return {
            "ready": False,
            "index": None,
            "source": None,
            "width": None,
            "height": None,
            "error": "Synthetic camera is unavailable during package validation.",
        }

    def frame(self):
        return None

    def observation(self):
        return None

    def validate_frame(self, frame_id):
        raise ValueError("Synthetic camera has no frames.")

    def close(self):
        self.closed = True


def _create_application(paths, *, package_smoke=False):
    from app import create_app
    from automation.jobs.storage import (
        AccountBindingStore,
        DeviceWorkflowAssignmentStore,
        JobStore,
    )
    from automation.screen.ocr_settings import OCRSettingsStorage
    from automation.workflows.screenshots import WorkflowScreenshotStore
    from automation.workflows.storage import WorkflowStorage
    from calibration import CalibrationStorage
    from camera_manager import CameraConfigStorage, CameraManager
    from device_lock import DeviceLocks

    manager_options = {
        "storage": CameraConfigStorage(paths.camera / "cameras.json"),
        "default_source": 0,
        "default_rotation": 0,
        "width": 1280,
        "height": 720,
    }
    if package_smoke:
        # Application creation normally activates the configured physical
        # camera. A packaging check needs the complete Flask/import graph, not
        # USB hardware, and a blocked DirectShow read cannot be shut down on a
        # deterministic validation deadline.
        manager_options.update(
            default_camera=_PackageSmokeCamera(),
            shared_camera=True,
            identity_discovery=False,
        )
    camera_manager = CameraManager(**manager_options)
    try:
        camera = camera_manager.activate("phone_A")
        app = create_app(
            camera,
            camera_manager=camera_manager,
            calibration_storage=CalibrationStorage(paths.camera / "calibration.json"),
            workflow_storage=WorkflowStorage(paths.workflows),
            account_store=AccountBindingStore(paths.jobs / "accounts.json"),
            job_store=JobStore(paths.jobs / "jobs.json"),
            device_workflow_store=DeviceWorkflowAssignmentStore(
                paths.jobs / "device_workflows.json"
            ),
            ocr_settings_storage=OCRSettingsStorage(paths.camera / "ocr_settings.json"),
            screenshot_store=WorkflowScreenshotStore(paths.receipts),
            device_locks=DeviceLocks(paths.locks),
            start_job_executor=not package_smoke,
            start_withdraw_workers=not package_smoke,
        )
    except Exception:
        camera_manager.release_all()
        raise
    return app, camera_manager


def _warm_ocr(app):
    provider = app.extensions.get("workflow_screen_state_provider")
    start_worker = getattr(provider, "start_worker", None)
    if start_worker is None:
        return
    try:
        start_worker()
    except Exception as exc:
        log_event(
            LOG,
            logging.ERROR,
            "ocr",
            "ocr_worker_start_failed",
            error_code="ocr_worker_start_failed",
            exception_type=type(exc).__name__,
            exception_detail=sanitize_text(str(exc))[:500],
        )


def _close_application(app, camera_manager):
    try:
        remote = app.extensions.get("remote_control")
        if remote is not None:
            remote.close()
    finally:
        try:
            manager = app.extensions.get("withdraw_manager")
            if manager is not None:
                manager.close()
        finally:
            try:
                executor = app.extensions.get("job_executor")
                if executor is not None:
                    executor.close()
            finally:
                try:
                    provider = app.extensions.get("workflow_screen_state_provider")
                    close_provider = getattr(provider, "close", None)
                    if close_provider is not None:
                        close_provider()
                finally:
                    state = app.extensions.get("control_state")
                    if state is not None:
                        with state.lock:
                            state.disable()
                    camera_manager.release_all()


def _watch_for_shutdown(event_handle, server):
    if not event_handle or os.name != "nt":
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    result = kernel32.WaitForSingleObject(event_handle, 0xFFFFFFFF)
    if result == WAIT_OBJECT_0:
        LOG.info("Shutdown requested by installer or support command.")
        server.shutdown()


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--shutdown", action="store_true")
    parser.add_argument(
        "--camera-diagnostics",
        nargs="?",
        const="",
        metavar="OUTPUT_JSON",
        help="write read-only Windows camera discovery diagnostics and exit",
    )
    parser.add_argument(
        "--package-smoke-test", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    return args


def _run_camera_diagnostics(output_path):
    from hardware.camera_diagnostics import collect_windows_camera_diagnostics

    path = (
        Path(output_path).expanduser().resolve()
        if output_path
        else default_data_root() / "logs" / "camera-diagnostics.json"
    )
    try:
        report = collect_windows_camera_diagnostics()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as exc:
        _message_box("Camera diagnostics failed", str(exc))
        return 1
    if sys.stdout is not None:
        print(f"Camera diagnostics written to {path}")
    _message_box(
        "Camera diagnostics complete",
        f"Report written to:\n{path}",
        0x40,
    )
    return 0


def _run_package_smoke_test():
    """Exercise the frozen import graph and core Flask UI/API without a server."""
    report_path_value = os.environ.get(
        "PHONE_CONTROLLER_PACKAGE_SMOKE_REPORT", ""
    ).strip()
    report_path = Path(report_path_value) if report_path_value else None
    report = {
        "command": sys.argv,
        "executable": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
        "resource_root": os.fspath(getattr(sys, "_MEIPASS", "")),
        "phase": "starting",
        "compiled_imports": [],
        "endpoints": [],
        "errors": [],
        "console_streams_absent": sys.stdout is None and sys.stderr is None,
    }

    def save_report():
        if report_path is None:
            return
        try:
            report_path.write_text(
                json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
            )
        except Exception:
            if sys.stderr is not None:
                traceback.print_exc(file=sys.stderr)

    paths = None
    logging_runtime = None
    app = camera_manager = provider = None
    smoke_ocr_pools = {}
    exit_code = 1
    try:
        report["phase"] = "runtime_paths"
        paths = RuntimePaths.create()
        report["data_root"] = os.fspath(paths.data)
        report["ocr_models"] = os.fspath(paths.ocr_models)
        logging_runtime = _configure_logging(paths.logs)

        report["phase"] = "environment"
        _prepare_environment(paths)

        report["phase"] = "compiled_imports"
        attestation_path = paths.resources / "protected-attestation.json"
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        for name in attestation["compiled_modules"]:
            module = importlib.import_module(name)
            origin = Path(module.__file__).resolve()
            if not any(
                origin.name.endswith(suffix)
                for suffix in importlib.machinery.EXTENSION_SUFFIXES
            ):
                raise RuntimeError(
                    f"Protected module {name} did not load as a native extension: "
                    f"{origin}"
                )
            if paths.resources.resolve() not in origin.parents:
                raise RuntimeError(
                    f"Protected module {name} loaded outside frozen resources: {origin}"
                )
            report["compiled_imports"].append(
                {"module": name, "origin": os.fspath(origin)}
            )

        report["phase"] = "multiprocessing"
        from automation.screen.camera_ocr import _process_cpu_times

        context = multiprocessing.get_context("spawn")
        process = context.Process(
            target=_process_cpu_times, name="package-smoke-compiled-child"
        )
        process.start()
        process.join(30)
        if process.is_alive():
            process.terminate()
            process.join(5)
            raise RuntimeError("Compiled multiprocessing child did not exit.")
        if process.exitcode != 0:
            raise RuntimeError(
                "Compiled multiprocessing child failed with exit code "
                f"{process.exitcode}."
            )
        report["multiprocessing_exit_code"] = process.exitcode

        report["phase"] = "flask_create_app"
        app, camera_manager = _create_application(paths, package_smoke=True)
        report["phase"] = "ocr_worker"
        provider = app.extensions.get("workflow_screen_state_provider")
        start_worker = getattr(provider, "start_worker", None)
        if start_worker is None:
            raise RuntimeError("Frozen application has no OCR worker startup hook.")
        ocr_status = start_worker()
        report["ocr_worker_status"] = ocr_status
        workers = ocr_status.get("workers", []) if isinstance(ocr_status, dict) else []
        if not workers or any(item.get("state") != "IDLE" for item in workers):
            raise RuntimeError(
                f"Frozen OCR workers did not initialize successfully: {ocr_status!r}."
            )
        start_language_worker = getattr(provider, "start_language_worker", None)
        if start_language_worker is None:
            raise RuntimeError("Frozen application has no OCR language startup hook.")
        report["ocr_language_statuses"] = {}
        for language in ("en", "zh-HK", "zh-TW"):
            status = start_language_worker(language)
            report["ocr_language_statuses"][language] = status
            language_workers = (
                status.get("workers", []) if isinstance(status, dict) else []
            )
            if not language_workers or any(
                item.get("state") != "IDLE" for item in language_workers
            ):
                raise RuntimeError(
                    f"Frozen {language} OCR workers did not initialize: {status!r}."
                )
        report["phase"] = "ocr_inference"
        import cv2
        import numpy as np

        if not report["console_streams_absent"]:
            raise RuntimeError("Frozen OCR must be validated with absent console streams.")
        fixtures = _package_smoke_fixtures(paths.resources)
        report["ocr_inference_results"] = [
            _package_smoke_ocr_inference(provider, language, image, expected)
            for language, (image, expected) in fixtures.items()
        ]
        report["phase"] = "ocr_recovery"
        report["ocr_recovery"] = {}
        for language, (image, expected) in fixtures.items():
            language_report = report["ocr_recovery"][language] = {}
            _package_smoke_recovery(provider, language, image, expected, language_report)
        report["phase"] = "roi_api"
        report["roi_api"] = _package_smoke_roi(app, provider, fixtures)
        smoke_ocr_pools = {
            language: provider._worker_for_language(language)
            for language in PACKAGE_SMOKE_OCR_LANGUAGES
        }
        runtime_model_cache = paths.cache / "paddlex" / "official_models"
        downloaded_model_files = (
            list(runtime_model_cache.rglob("inference.pdiparams"))
            if runtime_model_cache.is_dir()
            else []
        )
        report["runtime_model_download_count"] = len(downloaded_model_files)
        if downloaded_model_files:
            raise RuntimeError(
                "Frozen OCR created runtime model downloads instead of using the "
                "bundled model directories."
            )
        required_routes = {
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
        registered_routes = {
            (rule.rule, method)
            for rule in app.url_map.iter_rules()
            for method in rule.methods - {"HEAD", "OPTIONS"}
        }
        missing_routes = sorted(required_routes - registered_routes)
        report["remoteviewer_routes"] = [
            {"route": route, "method": method}
            for route, method in sorted(required_routes)
        ]
        if missing_routes:
            raise RuntimeError(
                "RemoteViewer routes are missing from the frozen Flask app: "
                f"{missing_routes!r}."
            )
        client = app.test_client()
        checks = (
            ("/", {"follow_redirects": True}, b'id="remote-history"'),
            (
                "/api/status",
                {"headers": {"X-Control-Client": "package-smoke"}},
                None,
            ),
            ("/static/app.js", {}, b"/api/remote-control"),
            ("/static/i18n.js", {}, b'"remote.heading"'),
            ("/static/workflows.js", {}, b"switch_transfer_method"),
            ("/static/style.css", {}, b".remote-control-banner"),
            ("/api/workflow-options/transfer-methods", {}, None),
        )
        report["phase"] = "flask_endpoints"
        for endpoint, request_options, required_marker in checks:
            report["current_endpoint"] = endpoint
            response = client.get(endpoint, **request_options)
            try:
                result = {
                    "endpoint": endpoint,
                    "status_code": response.status_code,
                    "content_type": response.content_type,
                    "content_length": len(response.data),
                }
                if response.status_code != 200:
                    result["response_excerpt"] = response.get_data(as_text=True)[:2000]
                report["endpoints"].append(result)
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Smoke endpoint {endpoint} returned HTTP {response.status_code}."
                    )
                if required_marker is not None and required_marker not in response.data:
                    raise RuntimeError(
                        f"Smoke endpoint {endpoint} is missing its RemoteViewer asset marker."
                    )
                if endpoint == "/api/workflow-options/transfer-methods":
                    methods = response.get_json().get("transfer_methods", [])
                    values = {
                        item.get("value") for item in methods if isinstance(item, dict)
                    }
                    expected_values = {
                        "bank_account",
                        "phone_no",
                        "email",
                        "fps_id",
                    }
                    if values != expected_values:
                        raise RuntimeError(
                            "Frozen transfer-method API returned unexpected values: "
                            f"{sorted(values)!r}."
                        )
                    report["transfer_method_values"] = sorted(values)
            finally:
                response.close()
        report["phase"] = "http_health"
        from werkzeug.serving import make_server

        server = make_server("127.0.0.1", 0, app, threaded=True)
        server_thread = threading.Thread(
            target=server.serve_forever,
            name="package-smoke-http",
            daemon=True,
        )
        server_thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/status",
                headers={"X-Control-Client": "package-smoke"},
            )
            with urllib.request.urlopen(request, timeout=10.0) as response:
                report["http_health"] = {
                    "status_code": response.status,
                    "content_type": response.headers.get_content_type(),
                }
                response.read()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(10.0)
        report["http_health"]["thread_stopped"] = not server_thread.is_alive()
        if (
            report["http_health"]["status_code"] != 200
            or not report["http_health"]["thread_stopped"]
        ):
            raise RuntimeError("Frozen local HTTP health check did not succeed.")
        report.pop("current_endpoint", None)
        report["phase"] = "checks_complete"
        exit_code = 0
    except BaseException as exc:
        report["errors"].append(
            {
                "phase": report["phase"],
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
                "ocr_diagnostics": getattr(exc, "ocr_diagnostics", None),
                "worker_pid": getattr(exc, "ocr_worker_id", None),
            }
        )
        if sys.stderr is not None:
            traceback.print_exc(file=sys.stderr)
    finally:
        report["phase_before_cleanup"] = report["phase"]
        report["phase"] = "cleanup"
        try:
            if app is not None and camera_manager is not None:
                _close_application(app, camera_manager)
            if smoke_ocr_pools:
                shutdown_statuses = {
                    language: pool.status()
                    for language, pool in smoke_ocr_pools.items()
                }
                report["ocr_shutdown_statuses"] = shutdown_statuses
                for language, status in shutdown_statuses.items():
                    workers = status.get("workers", [])
                    if (
                        status.get("accepting") is not False
                        or status.get("busy_workers") != 0
                        or status.get("queued_requests") != 0
                        or not workers
                        or any(worker.get("pid") is not None for worker in workers)
                    ):
                        raise RuntimeError(
                            f"Frozen {language} OCR workers did not shut down cleanly."
                        )
            if logging_runtime is not None:
                logging_runtime.stop()
        except BaseException as exc:
            exit_code = 1
            report["errors"].append(
                {
                    "phase": "cleanup",
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
            if sys.stderr is not None:
                traceback.print_exc(file=sys.stderr)
        report["phase"] = "complete" if exit_code == 0 else "failed"
        report["exit_code"] = exit_code
        save_report()
    return exit_code


def main(argv=None):
    args = _parse_args(argv)
    if args.camera_diagnostics is not None:
        return _run_camera_diagnostics(args.camera_diagnostics)
    if args.package_smoke_test:
        return _run_package_smoke_test()
    if args.shutdown:
        signalled = _signal_shutdown()
        if signalled:
            url = f"http://127.0.0.1:{args.port}/"
            deadline = time.monotonic() + 30.0
            while _dashboard_healthy(url) and time.monotonic() < deadline:
                time.sleep(0.25)
            return 1 if _dashboard_healthy(url) else 0
        return 0

    dashboard_url = f"http://127.0.0.1:{args.port}/"
    paths = None
    mutex_handle = shutdown_handle = None
    app = camera_manager = server = logging_runtime = None
    try:
        mutex_handle, already_running = _acquire_instance_mutex()
        if already_running:
            if _wait_for_dashboard(dashboard_url):
                webbrowser.open(dashboard_url, new=2)
                return 0
            raise RuntimeError(
                "Another Phone Controller instance exists but its dashboard is not responding."
            )

        paths = RuntimePaths.create()
        logging_runtime = _configure_logging(paths.logs)
        shutdown_handle = _create_shutdown_event()
        _prepare_environment(paths)
        app, camera_manager = _create_application(paths)

        from werkzeug.serving import make_server

        server = make_server("127.0.0.1", args.port, app, threaded=True)
        threading.Thread(
            target=_warm_ocr, args=(app,), name="camera-ocr-warmup", daemon=True
        ).start()
        threading.Thread(
            target=_open_when_ready,
            args=(dashboard_url,),
            name="dashboard-browser",
            daemon=True,
        ).start()
        threading.Thread(
            target=_watch_for_shutdown,
            args=(shutdown_handle, server),
            name="windows-shutdown-event",
            daemon=True,
        ).start()
        log_event(
            LOG,
            logging.INFO,
            "app",
            "app_listening",
            host="127.0.0.1",
            port=args.port,
            app_version=logging_runtime.app_version,
            session_id=logging_runtime.session_id,
        )
        server.serve_forever()
        return 0
    except Exception as exc:
        if logging_runtime is not None:
            log_event(
                LOG,
                logging.CRITICAL,
                "app",
                "app_start_failed",
                "Phone Controller startup failed",
                error_code="app_start_failed",
                exception_type=type(exc).__name__,
                app_version=logging_runtime.app_version,
                session_id=logging_runtime.session_id,
            )
        else:
            try:
                fallback_root = (
                    paths.logs if paths is not None else default_data_root() / "logs"
                )
                write_startup_error(
                    fallback_root,
                    "Phone Controller startup failed",
                    exception_type=type(exc).__name__,
                )
            except Exception:
                pass
        _message_box(
            "Phone Controller could not start",
            f"{exc}\n\n"
            + (
                f"See {paths.logs / 'phone-controller.log'} for details."
                if paths is not None
                else "The per-user application data directory could not be initialized."
            ),
        )
        return 1
    finally:
        if logging_runtime is not None:
            log_event(
                LOG,
                logging.INFO,
                "app",
                "app_stopping",
                app_version=logging_runtime.app_version,
                session_id=logging_runtime.session_id,
            )
        if server is not None:
            server.server_close()
        if app is not None and camera_manager is not None:
            try:
                _close_application(app, camera_manager)
            except Exception as exc:
                log_event(
                    LOG,
                    logging.ERROR,
                    "app",
                    "app_cleanup_failed",
                    error_code="app_cleanup_failed",
                    exception_type=type(exc).__name__,
                )
        _close_handle(shutdown_handle)
        _close_handle(mutex_handle)
        if logging_runtime is not None:
            logging_runtime.stop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
