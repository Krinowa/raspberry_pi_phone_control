"""Silent Windows launcher for the packaged Phone Controller dashboard."""

from __future__ import annotations

import argparse
import ctypes
import logging
import multiprocessing
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path

from windows_runtime import RuntimePaths

LOG = logging.getLogger("windows-launcher")
APP_MUTEX_NAME = "Local\\PhoneController.Backend"
SHUTDOWN_EVENT_NAME = "Local\\PhoneController.Shutdown"
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0
HEALTH_CLIENT = "launcher-health"


def _configure_logging(log_directory: Path):
    log_directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_directory / "phone-controller.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def _message_box(title, message):
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, str(message), str(title), 0x10)
    else:
        LOG.error("%s: %s", title, message)


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
        os.environ["PATH"] = os.fspath(tools) + os.pathsep + os.environ.get("PATH", "")

    from automation.auth import load_or_create_secret_key

    os.environ.setdefault(
        "PHONE_SESSION_SECRET", load_or_create_secret_key(paths.config)
    )


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
    except Exception:
        LOG.exception("PaddleOCR worker warmup failed")


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
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    return args


def main(argv=None):
    args = _parse_args(argv)
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
    app = camera_manager = server = None
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
        _configure_logging(paths.logs)
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
        LOG.info("Phone Controller listening at %s", dashboard_url)
        server.serve_forever()
        return 0
    except Exception as exc:
        LOG.exception("Phone Controller startup failed")
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
        if server is not None:
            server.server_close()
        if app is not None and camera_manager is not None:
            try:
                _close_application(app, camera_manager)
            except Exception:
                LOG.exception("Phone Controller cleanup failed")
        _close_handle(shutdown_handle)
        _close_handle(mutex_handle)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
