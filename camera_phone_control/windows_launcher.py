"""Silent Windows launcher for the packaged Phone Controller dashboard."""

from __future__ import annotations

import argparse
import ctypes
import importlib
import importlib.machinery
import json
import logging
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


def _configure_logging(log_directory: Path):
    return configure_production_logging(
        log_directory, version=application_version(), packaged=True
    )


def _message_box(title, message):
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, str(message), str(title), 0x10)
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


def _create_application(paths):
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

    camera_manager = CameraManager(
        storage=CameraConfigStorage(paths.camera / "cameras.json"),
        default_source=0,
        default_rotation=0,
        width=1280,
        height=720,
    )
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
            start_job_executor=True,
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
        )


def _close_application(app, camera_manager):
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
        "--package-smoke-test", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    return args


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
    app = camera_manager = None
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
        app, camera_manager = _create_application(paths)
        client = app.test_client()
        checks = (
            ("/", {"follow_redirects": True}),
            ("/api/status", {"headers": {"X-Control-Client": "package-smoke"}}),
            ("/static/app.js", {}),
        )
        report["phase"] = "flask_endpoints"
        for endpoint, request_options in checks:
            report["current_endpoint"] = endpoint
            response = client.get(endpoint, **request_options)
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
