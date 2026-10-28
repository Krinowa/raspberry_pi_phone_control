"""Local external-camera dashboard. Every Android input comes from a browser action."""

import argparse
import base64
import logging
import os
import secrets
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from types import SimpleNamespace

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException
from diagnostic_logging import (
    EVENT_DEDUPLICATOR,
    configure_development_logging,
    log_event,
    register_secrets,
)

from android_control import AndroidControl, DeviceError, validate_key
from calibration import Calibration, CalibrationStorage
from camera import Camera
from camera_manager import (
    CameraConfig,
    CameraManager,
    CameraManagerError,
    UnavailableCamera,
)
from device_lock import DeviceBusy, DeviceLocks, NoAutomation
from automation.backends import backend_names, select_device
from automation.context import AutomationContext
from automation.coordinates import normalized_to_device, unit
from automation.diagnostics import install_recent_log_handler
from automation.error_codes import exception_error_fields, error_fields
from automation.screen.camera import CalibratedPreviewCache
from automation.screen.stability import ScreenStabilityError
from automation.workflows.api import register_workflows
from automation.jobs.api import register_jobs
from automation.jobs.storage import DeviceWorkflowAssignmentStore
from automation.integrations.withdraw_v2.balance import balance_check_workflow
from automation.integrations.withdraw_v2.manager import register_withdraw
from hardware.pi_api_client import PiApiError, validate_text as validate_pi_text
from hardware.device_config import DEFAULT_DEVICE_ID
from hardware.raspberry_pi_input import PiControlTarget, RaspberryPiInputBridge

LOG = logging.getLogger("dashboard")
LEASE_SECONDS = 6
DEVELOPER_MODE_KEY = "ocean"


def local_dashboard_access(view):
    """Admin is a local dashboard view; mutations remain token protected."""
    return view


def allow_local_dashboard():
    return None


class ControlState:
    def __init__(
        self,
        android,
        camera,
        device_locks=None,
        *,
        pi_bridge=None,
        calibration_storage=None,
        camera_manager=None,
    ):
        self.android, self.camera = android, camera
        self.camera_manager = camera_manager
        self.pi_bridge = pi_bridge
        self.pi_connection = {
            "api": "offline",
            "phone": "unknown",
            "message": "Waiting for Pi status.",
        }
        self.pi_status_notice = None
        self.pi_devices = {}
        self.pi_catalog_initialized = False
        self.lock = threading.RLock()
        self.action_lock = threading.Lock()
        self.device_locks = device_locks or DeviceLocks()
        self.device = None
        self.calibration = None
        self.signature = None
        self.calibration_storage = calibration_storage
        self.calibration_restore_key = None
        self.owner = None
        self.manual_claim = None
        self.lease_timer = None
        self.expires = 0
        self.message = "Input disabled. Calibrate the phone display first."

    def disable(self, reason="Input disabled."):
        if self.owner:
            LOG.info("Control disabled: %s", reason)
        self.owner, self.expires, self.message = None, 0, reason
        if self.lease_timer:
            self.lease_timer.cancel()
            self.lease_timer = None
        if self.manual_claim:
            claim = self.manual_claim
            self.manual_claim = None
            claim.close()

    def renew_lease(self):
        self.expires = time.monotonic() + LEASE_SECONDS
        if self.lease_timer:
            self.lease_timer.cancel()
        self.lease_timer = threading.Timer(LEASE_SECONDS, self.expire_lease)
        self.lease_timer.daemon = True
        self.lease_timer.start()

    def expire_lease(self):
        # Release persistent ownership even if no browser/status requests arrive.
        # state.lock lets any in-flight manual command finish before releasing it.
        with self.lock:
            if self.owner and time.monotonic() >= self.expires:
                self.disable("Control heartbeat expired. Enable Manual Control again.")

    @property
    def pending(self):
        return self.manual_claim is not None

    def claim_manual(self):
        try:
            acquired = self.manual_claim.try_acquire()
        except Exception:
            self.disable("Manual ownership acquisition failed. Input disabled.")
            raise DeviceError(
                "Cannot acquire manual ownership. Check local device locks."
            ) from None
        if acquired:
            # The handoff is complete. Retain the browser lease, but use the
            # ordinary per-command OS lock so a new explicit run can start later.
            claim, self.manual_claim = self.manual_claim, None
            try:
                self.message = "Manual control available again."
                LOG.info("Manual control available again on %s", claim.serial)
            finally:
                claim.close()
            return True
        self.message = (
            "Takeover requested. Waiting for automation to finish its current command."
        )
        return False

    def refresh_pi_status(self):
        # Explicit checks only; snapshots, inputs and heartbeats use last-known state.
        self.pi_connection = self.pi_bridge.connection_state()
        return self.pi_connection

    def select_pi_device(self, device_id):
        """Switch the idle dashboard to another phone on the same Pi."""
        if self.pi_bridge is None:
            raise ValueError("Raspberry Pi input is not selected.")
        self.disable("Phone selection changed. Input disabled.")
        self.pi_bridge.select_device(device_id)
        if self.camera_manager is not None:
            self.camera = self.camera_manager.activate(device_id)
        self.calibration = None
        self.signature = None
        self.calibration_restore_key = None
        self.refresh_pi_status()
        self.refresh()
        return self.pi_bridge.device_id

    def clear_pi_device(self):
        self.disable("No registered phone is available. Input disabled.")
        self.pi_bridge.clear_device()
        if self.camera_manager is not None:
            self.camera_manager.release_all()
            self.camera = UnavailableCamera(
                CameraConfig("phone_A", None, 0, False),
                "Select a registered phone before using its camera.",
            )
        self.device = None
        self.calibration = None
        self.signature = None
        self.calibration_restore_key = None
        self.pi_connection = self.pi_bridge.connection_state()

    def update_pi_devices(self, catalog):
        devices = catalog.get("devices")
        if isinstance(devices, list):
            self.pi_devices = {
                item["id"]: item
                for item in devices
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            selected = self.pi_devices.get(self.pi_bridge.device_id)
            connected = (
                selected.get("connected") if isinstance(selected, dict) else False
            )
            self.pi_connection = {
                "api": "online",
                "phone": "connected" if connected else "waiting",
                "message": (
                    "Phone Connected" if connected else "Pi Online / Phone Waiting"
                ),
            }

    def selected_pi_connected(self):
        if self.pi_bridge is None:
            return None
        selected = self.pi_bridge.device_id
        if selected is None:
            return False
        details = self.pi_devices.get(selected)
        return details.get("connected") if isinstance(details, dict) else None

    def refresh(self):
        errors = []
        if self.camera_manager is not None:
            selected_camera_device = (
                self.pi_bridge.device_id
                if self.pi_bridge is not None
                else DEFAULT_DEVICE_ID
            )
            if selected_camera_device is not None:
                self.camera = self.camera_manager.get_camera(selected_camera_device)
        try:
            if self.pi_bridge is not None:
                if self.pi_connection["phone"] == "connected":
                    self.device = self.pi_bridge.target
                else:
                    self.device = None
                    errors.append(self.pi_connection["message"])
            else:
                self.device = select_device(
                    self.android,
                    authorized_only=False,
                    input_backend="appium",
                )
        except DeviceError as exc:
            self.device = None
            errors.append(str(exc))

        camera = self.camera.status()
        if not camera["ready"]:
            errors.append(camera["error"])

        signature = (self.device, camera["width"], camera["height"])

        if errors:
            self.calibration = None
            self.calibration_restore_key = None
            self.disable(" ".join(errors))
            if (
                self.pi_bridge is not None
                and self.pi_connection["phone"] != "connected"
            ):
                # Track the displayed failure, independently of later explicit checks.
                self.pi_status_notice = self.message
        elif self.calibration and signature != self.signature:
            self.calibration = None
            self.calibration_restore_key = None
            self.disable(
                "Phone geometry or camera resolution changed. Recalibrate before enabling input."
            )
        if (
            not errors
            and self.calibration is None
            and self.calibration_storage is not None
        ):
            restore_key = (
                signature,
                getattr(self.camera, "configured_source", self.camera.source),
                self.camera.rotate,
            )
            if restore_key != self.calibration_restore_key:
                self.calibration_restore_key = restore_key
                restored = self.calibration_storage.load(
                    self.camera,
                    camera,
                    self.device,
                )
                if restored is not None:
                    self.calibration = restored
                    self.signature = signature
                    self.message = (
                        "Saved calibration loaded. Enable Manual Control when ready."
                    )
        if not errors and self.owner and time.monotonic() >= self.expires:
            self.disable("Control heartbeat expired. Enable Manual Control again.")

        if not errors and self.pi_status_notice is not None:
            if self.message == self.pi_status_notice:
                self.message = (
                    "Phone Connected. Enable Manual Control when ready."
                    if self.calibration
                    else "Phone Connected. Calibrate the phone display before enabling input."
                )
            # Never replace a newer safety or control message with Pi recovery text.
            self.pi_status_notice = None
        return camera

    def require_frame(self, frame_id):
        try:
            self.camera.validate_frame(frame_id)
        except ValueError as exc:
            self.disable(str(exc))
            raise

    def require_ready(self):
        camera = self.refresh()
        if not self.device or not camera["ready"]:
            raise DeviceError(self.message)
        return camera

    @contextmanager
    def input_control(self, client, frame_id):
        if self.device is None:
            self.require_ready()
        if self.pending:
            raise DeviceBusy("automation")
        serial = self.device.serial
        with self.device_locks.hold(serial, "manual"):
            self.require_ready()
            self.require_frame(frame_id)
            if self.device.serial != serial:
                raise DeviceError(
                    "Phone changed while acquiring control. Enable input again."
                )
            if self.owner != client or self.calibration is None:
                raise ValueError(
                    "Input disabled. Enable Manual Control in this tab first."
                )
            yield

    def snapshot(self, client):
        camera = self.refresh()
        owner = self.device_locks.owner(self.device.serial) if self.device else None
        control_status = (
            "takeover_requested"
            if self.pending
            else (
                "automation"
                if owner == "automation"
                else "manual" if self.owner else "free"
            )
        )
        if owner == "automation" and not self.pending and self.owner:
            self.disable(
                "Automation running. Manual control disabled. Use Take Over to interrupt."
            )
        message_error_code = None
        message_error_params = {}
        if self.pi_connection.get(
            "error_code"
        ) and self.message == self.pi_connection.get("message"):
            message_error_code = self.pi_connection["error_code"]
            message_error_params = self.pi_connection.get("error_params", {})
        return {
            "device": asdict(self.device) if self.device else None,
            "manual_backend": (
                "raspberry_pi" if self.pi_bridge is not None else "appium"
            ),
            "pi": self.pi_connection,
            "supported_actions": (
                sorted(self.pi_bridge.ACTIONS) if self.pi_bridge is not None else None
            ),
            "camera": camera,
            "calibrated": self.calibration is not None,
            "corners": self.calibration.corners.tolist() if self.calibration else [],
            "enabled": self.owner is not None,
            "owned": self.owner == client
            and self.owner is not None
            and not self.pending,
            "pending": self.pending,
            "pending_owned": self.pending and self.owner == client,
            "control_status": control_status,
            "message": self.message,
            "message_error_code": message_error_code,
            "message_error_params": message_error_params,
        }


def create_app(
    camera,
    android=None,
    device_locks=None,
    workflow_storage=None,
    *,
    manual_backend=None,
    pi_client=None,
    calibration_storage=None,
    device_id=None,
    camera_manager=None,
    account_store=None,
    job_store=None,
    device_workflow_store=None,
    start_job_executor=False,
    start_withdraw_workers=None,
    ocr_settings_storage=None,
    ocr_resource_provider=None,
    screenshot_store=None,
    auth_store=None,
):
    # Manual and workflow input are independent: explicit argument, env, default.
    manual_backend = (
        manual_backend or os.environ.get("PHONE_MANUAL_INPUT_BACKEND") or "raspberry_pi"
    )
    if manual_backend not in {"raspberry_pi", "appium"}:
        raise ValueError("PHONE_MANUAL_INPUT_BACKEND must be raspberry_pi or appium.")
    pi_bridge = (
        RaspberryPiInputBridge(pi_client, device_id=device_id)
        if manual_backend == "raspberry_pi"
        else None
    )
    selected_camera_device = pi_bridge.device_id if pi_bridge is not None else "phone_A"
    if not isinstance(selected_camera_device, str):
        selected_camera_device = "phone_A"
    if camera_manager is None:
        legacy_source = getattr(camera, "source", 0)
        if not isinstance(legacy_source, (int, str)) or isinstance(legacy_source, bool):
            legacy_source = 0
        legacy_rotation = getattr(camera, "rotate", 0)
        if legacy_rotation not in {0, 90, 180, 270}:
            legacy_rotation = 0
        camera_manager = CameraManager(
            default_source=legacy_source,
            default_rotation=legacy_rotation,
            default_camera=camera,
            # Existing test/adaptor cameras may intentionally represent one
            # shared feed. A real Camera must be managed per device so saved
            # source and rotation changes rebuild the actual frame pipeline.
            shared_camera=not isinstance(camera, Camera),
        )
    camera = camera_manager.activate(selected_camera_device)
    resource_root = (
        os.fspath(getattr(sys, "_MEIPASS"))
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")
        else os.path.dirname(os.path.abspath(__file__))
    )
    app = Flask(
        __name__,
        static_folder=os.path.join(resource_root, "static"),
        template_folder=os.path.join(resource_root, "templates"),
    )
    session_secret = os.environ.get("PHONE_SESSION_SECRET") or secrets.token_hex(32)
    app.config.update(
        MAX_CONTENT_LENGTH=1024 * 1024,
        TRUSTED_HOSTS=["localhost", "127.0.0.1"],
        SECRET_KEY=session_secret,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("PHONE_SESSION_COOKIE_SECURE", "")
        .strip()
        .casefold()
        in {"1", "true", "yes", "on"},
    )
    preview_debug = os.environ.get("CAMERA_PREVIEW_DEBUG", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    app.config["CAMERA_PREVIEW_DEBUG"] = preview_debug
    request_metrics = {
        "lock": threading.Lock(),
        "active": 0,
        "window_started": time.monotonic(),
        "frames": 0,
        "first_sequence": None,
        "last_sequence": None,
        "last_response": None,
        "interval_total": 0.0,
        "interval_max": 0.0,
        "route_total": 0.0,
    }
    app.extensions["preview_request_metrics"] = request_metrics
    calibrated_preview_cache = CalibratedPreviewCache()
    app.extensions["calibrated_preview_cache"] = calibrated_preview_cache
    token = secrets.token_urlsafe(32)
    if calibration_storage is None and all(
        hasattr(camera, field) for field in ("source", "rotate")
    ):
        calibration_storage = CalibrationStorage()
    state = ControlState(
        android or AndroidControl(),
        camera,
        device_locks,
        pi_bridge=pi_bridge,
        calibration_storage=calibration_storage,
        camera_manager=camera_manager,
    )
    app.extensions["control_state"] = state
    app.extensions["camera_manager"] = camera_manager
    app.extensions["device_workflow_assignment_store"] = (
        device_workflow_store or DeviceWorkflowAssignmentStore()
    )
    app.config["CONTROL_TOKEN"] = token
    register_secrets(token, session_secret)
    diagnostics = install_recent_log_handler(secrets=(token, session_secret))
    app.extensions["recent_diagnostics"] = diagnostics

    @app.before_request
    def protect_local_requests():
        g.request_id = uuid.uuid4().hex
        if preview_debug:
            g.preview_request_started = time.monotonic()
            with request_metrics["lock"]:
                request_metrics["active"] += 1
        # Reject cross-site fetches/embeds, including camera requests. No CORS enabled.
        if request.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
            return (
                jsonify(
                    error_fields(
                        "Only same-origin local requests are accepted.",
                        "same_origin_required",
                    )
                ),
                403,
            )
        if request.method != "GET":
            if not secrets.compare_digest(
                request.headers.get("X-Control-Token", ""), token
            ):
                return (
                    jsonify(
                        error_fields(
                            "Invalid control token. Reload this page.",
                            "invalid_control_token",
                        )
                    ),
                    403,
                )
            if not request.is_json:
                return (
                    jsonify(
                        error_fields(
                            "Expected a JSON request.", "json_request_required"
                        )
                    ),
                    415,
                )

    @app.after_request
    def local_headers(response):
        response.headers["X-Request-Id"] = getattr(g, "request_id", "")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        if preview_debug and hasattr(g, "preview_request_started"):
            with request_metrics["lock"]:
                request_metrics["active"] -= 1
        return response

    @app.errorhandler(ValueError)
    def invalid_input(exc):
        return jsonify(ok=False, **exception_error_fields(exc)), 400

    @app.errorhandler(CameraManagerError)
    def camera_error(exc):
        LOG.warning("Camera operation failed: %s", exc)
        return (
            jsonify(
                ok=False,
                **error_fields(str(exc), "camera_operation_failed"),
            ),
            503,
        )

    @app.errorhandler(PiApiError)
    @app.errorhandler(DeviceError)
    def device_error(exc):
        with state.lock:
            state.disable(str(exc))
        LOG.warning("%s", exc)
        fields = exception_error_fields(exc)
        if isinstance(exc, DeviceError) and "error_code" not in fields:
            fields.update(
                error_code="device_command_failed",
                error_params={},
            )
        return jsonify(**fields), (
            404 if getattr(exc, "http_status", None) == 404 else 409
        )

    @app.errorhandler(DeviceBusy)
    def device_busy(exc):
        with state.lock:
            if state.pending:
                return (
                    jsonify(
                        ok=False,
                        error="takeover_requested",
                        error_code="device_busy",
                        error_params={"owner": exc.owner},
                        owner=exc.owner,
                        pending=True,
                    ),
                    409,
                )
            state.disable(
                "Device busy. Enable Manual Control again after it is released."
            )
        return (
            jsonify(
                ok=False,
                error="device_busy",
                error_code="device_busy",
                error_params={"owner": exc.owner},
                owner=exc.owner,
            ),
            409,
        )

    @app.errorhandler(NoAutomation)
    def no_automation(exc):
        route = request.url_rule.rule if request.url_rule is not None else request.path
        if EVENT_DEDUPLICATOR.should_log("device", "device_not_ready", (route,)):
            log_event(
                LOG,
                logging.WARNING,
                "device",
                "device_not_ready",
                request_id=getattr(g, "request_id", None),
                route=route,
                error_code="device_not_ready",
                exception_type=type(exc).__name__,
            )
        return (
            jsonify(
                ok=False,
                error="no_automation",
                error_code="device_not_ready",
                error_params={},
                owner="free",
            ),
            409,
        )

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(ok=False, error=exc.description), exc.code

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        log_event(
            LOG,
            logging.ERROR,
            "api",
            "api_request_failed",
            request_id=getattr(g, "request_id", None),
            route=(
                request.url_rule.rule if request.url_rule is not None else request.path
            ),
            method=request.method,
            error_code="dashboard_request_failed",
            exception_type=type(exc).__name__,
        )
        return (
            jsonify(
                ok=False,
                **error_fields(
                    "The dashboard could not complete this request.",
                    "dashboard_request_failed",
                ),
            ),
            500,
        )

    def payload():
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def client_id():
        client = request.headers.get("X-Control-Client", "")
        if not 8 <= len(client) <= 80 or not all(
            c.isalnum() or c == "-" for c in client
        ):
            raise ValueError("Missing browser identifier. Reload the page.")
        return client

    @app.get("/")
    def index():
        supplied_key = request.args.get("key")
        if isinstance(supplied_key, str) and secrets.compare_digest(
            supplied_key, DEVELOPER_MODE_KEY
        ):
            session["developer_mode"] = True
            return redirect(url_for("index"))
        return render_template(
            "index.html",
            control_token=token,
            preview_debug=preview_debug,
            developer_mode=session.get("developer_mode") is True,
        )

    @app.get("/api/status")
    def status():
        with state.lock:
            return jsonify(state.snapshot(client_id()))

    @app.get("/api/diagnostics/logs")
    def diagnostics_logs():
        if session.get("developer_mode") is not True:
            return jsonify(error="Developer mode required."), 403
        query = request.args.get("query", "")
        if len(query) > 100:
            raise ValueError("Diagnostic log filter must be at most 100 characters.")
        try:
            limit = int(request.args.get("limit", "300"))
        except ValueError:
            raise ValueError("Diagnostic log limit must be an integer.") from None
        if not 1 <= limit <= 500:
            raise ValueError("Diagnostic log limit must be between 1 and 500.")
        return jsonify(logs=diagnostics.recent(limit=limit, query=query))

    frontend_diagnostic_events = {
        "frontend_api_network_failed",
        "frontend_action_failed",
        "frontend_preview_unavailable",
        "frontend_preview_recovered",
        "frontend_manual_assistance_failed",
        "frontend_workflow_command_failed",
        "frontend_recorder_command_uncertain",
    }
    frontend_diagnostic_fields = {
        "device_id",
        "job_id",
        "operation",
        "error_code",
        "status",
        "consecutive_failures",
    }
    frontend_diagnostic_rate = {"lock": threading.Lock(), "times": []}

    @app.post("/api/diagnostics/frontend")
    def frontend_diagnostic():
        data = payload()
        if set(data) != {"event", "params"}:
            raise ValueError("Invalid frontend diagnostic fields.")
        event = data.get("event")
        params = data.get("params")
        if event not in frontend_diagnostic_events or not isinstance(params, dict):
            raise ValueError("Invalid frontend diagnostic event.")
        if set(params) - frontend_diagnostic_fields or len(params) > 5:
            raise ValueError("Invalid frontend diagnostic parameters.")
        safe = {}
        for key, value in params.items():
            if isinstance(value, str):
                if not 1 <= len(value) <= 120 or any(ord(char) < 32 for char in value):
                    raise ValueError("Invalid frontend diagnostic parameter.")
            elif not isinstance(value, (int, bool)):
                raise ValueError("Invalid frontend diagnostic parameter.")
            safe[key] = value
        current = time.monotonic()
        with frontend_diagnostic_rate["lock"]:
            recent = [
                stamp
                for stamp in frontend_diagnostic_rate["times"]
                if current - stamp < 60
            ]
            frontend_diagnostic_rate["times"] = recent
            if len(recent) >= 30:
                return jsonify(ok=True)
            recent.append(current)
        identity = tuple((key, safe[key]) for key in sorted(safe))
        if EVENT_DEDUPLICATOR.should_log("frontend", event, identity):
            log_event(
                LOG,
                (
                    logging.INFO
                    if event == "frontend_preview_recovered"
                    else logging.WARNING
                ),
                "frontend",
                event,
                request_id=getattr(g, "request_id", None),
                **safe,
            )
        return jsonify(ok=True)

    @app.get("/api/pi/status")
    def pi_status():
        if state.pi_bridge is None:
            return jsonify(
                api="not_selected",
                phone="unknown",
                message="Pi manual control is not selected.",
            )
        with state.lock:
            return jsonify(state.refresh_pi_status())

    def require_pi_bridge():
        if state.pi_bridge is None:
            raise ValueError("Device management requires Raspberry Pi input.")
        return state.pi_bridge

    def registered_pi_devices():
        bridge = require_pi_bridge()
        catalog = bridge.devices()
        devices = catalog.get("devices")
        if not isinstance(devices, list):
            raise PiApiError("Pi /devices response is missing the device list.")
        return (
            bridge,
            catalog,
            {
                item.get("id")
                for item in devices
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            },
        )

    def selected_camera_device_id():
        if state.pi_bridge is not None:
            return state.pi_bridge.device_id
        return "phone_A"

    def workflow_assignment_details(device_id, *, include_available=False):
        store = app.extensions["device_workflow_assignment_store"]
        allowed = store.get(device_id)
        integration = store.integration(device_id)
        balance_workflow_id = integration.get("balance_workflow_id")
        workflow_storage = app.extensions["workflow_storage"]
        workflows = workflow_storage.list()
        if include_available:
            for item in workflows:
                try:
                    balance_check_workflow(workflow_storage.read(item["id"]))
                    item["balance_check_eligible"] = True
                    item["balance_check_error"] = None
                except ValueError as error:
                    item["balance_check_eligible"] = False
                    item["balance_check_error"] = str(error)
        known = {item["id"]: item for item in workflows}
        assigned_ids = list(known) if allowed is None else list(allowed)
        assigned = [
            {
                "id": workflow_id,
                "name": known.get(workflow_id, {}).get("name"),
                "purpose": known.get(workflow_id, {}).get("purpose"),
                "missing": workflow_id not in known,
            }
            for workflow_id in assigned_ids
        ]
        result = {
            "device_id": device_id,
            "configured": allowed is not None,
            "unrestricted": allowed is None,
            "allowed_workflow_ids": None if allowed is None else list(allowed),
            "assigned_workflows": assigned,
            "missing_workflow_ids": [
                workflow_id for workflow_id in assigned_ids if workflow_id not in known
            ],
            "balance_workflow_id": balance_workflow_id,
            "balance_workflow": (
                {
                    "id": balance_workflow_id,
                    "name": known.get(balance_workflow_id, {}).get("name"),
                    "purpose": known.get(balance_workflow_id, {}).get("purpose"),
                    "missing": balance_workflow_id not in known,
                    "balance_check_eligible": known.get(balance_workflow_id, {}).get(
                        "balance_check_eligible", False
                    ),
                }
                if balance_workflow_id
                else None
            ),
        }
        if include_available:
            result["available_workflows"] = workflows
        return result

    def camera_work_active():
        workflow_service = app.extensions.get("workflow_service")
        recorder = app.extensions.get("workflow_recorder")
        return (workflow_service is not None and workflow_service.has_active_run()) or (
            recorder is not None and recorder.active
        )

    @app.get("/api/cameras")
    def cameras():
        probe = request.args.get("probe", "true").casefold()
        refresh = request.args.get("refresh", "false").casefold()
        if probe not in {"false", "true", "0", "1"}:
            raise ValueError("probe must be true or false.")
        if refresh not in {"false", "true", "0", "1"}:
            raise ValueError("refresh must be true or false.")
        result = {
            "configs": camera_manager.all_configs(),
            "selected_device": selected_camera_device_id(),
        }
        if probe in {"true", "1"}:
            result["cameras"] = camera_manager.discover(force=refresh in {"true", "1"})
        return jsonify(result)

    @app.post("/api/cameras/test")
    @local_dashboard_access
    def test_camera():
        data = payload()
        if set(data) != {"camera_source", "rotation"}:
            raise ValueError("Expected camera_source and rotation only.")
        result, jpeg = camera_manager.test_source(
            data["camera_source"], data["rotation"]
        )
        response = {"ok": bool(result.get("available")), **result}
        if jpeg is not None:
            response["preview"] = "data:image/jpeg;base64," + base64.b64encode(
                jpeg
            ).decode("ascii")
        return jsonify(response), (200 if response["ok"] else 422)

    @app.route("/api/devices/<device_id>/camera", methods=["GET", "PUT"])
    def device_camera(device_id):
        # config() performs the shared logical-ID validation.
        camera_manager.config(device_id)
        if request.method == "GET":
            selected = device_id == selected_camera_device_id()
            status = camera_manager.status(device_id, open_camera=True)
            return jsonify({**status, "selected": selected})

        denied = allow_local_dashboard()
        if denied is not None:
            return denied

        data = payload()
        allowed = {
            "camera_source",
            "camera_id",
            "rotation",
            "enabled",
            "display_name",
        }
        if (
            not set(data).issubset(allowed)
            or not {"rotation", "enabled"}.issubset(data)
            or not ({"camera_source", "camera_id"} & set(data))
        ):
            raise ValueError(
                "Expected rotation, enabled, and camera_source or camera_id; display_name is optional."
            )
        with state.lock:
            if camera_work_active():
                raise ValueError(
                    "Stop the active workflow or recording before changing cameras."
                )
            previous_config = camera_manager.config(device_id)
            selected_device_id = selected_camera_device_id()
            previous_selected_config = camera_manager.config(selected_device_id)
            config, affected_devices = camera_manager.reassign(
                device_id,
                camera_source=(
                    data.get("camera_source") if "camera_source" in data else None
                ),
                rotation=data["rotation"],
                enabled=data["enabled"],
                display_name=data.get("display_name", previous_config.display_name),
                camera_id=data.get("camera_id"),
            )
            changed = config != previous_config
            calibration_revalidation = (
                config.camera_source != previous_config.camera_source
                or config.camera_id != previous_config.camera_id
                or config.rotation != previous_config.rotation
            )
            selected = device_id == selected_device_id
            selected_config = camera_manager.config(selected_device_id)
            selected_changed = (
                selected_device_id in affected_devices
                and selected_config != previous_selected_config
            )
            selected_calibration_revalidation = selected_changed and (
                selected_config.camera_source != previous_selected_config.camera_source
                or selected_config.camera_id != previous_selected_config.camera_id
                or selected_config.rotation != previous_selected_config.rotation
            )
            if selected_changed:
                state.disable("Camera assignment changed. Recalibrate before input.")
                if selected_calibration_revalidation:
                    state.calibration = None
                    state.signature = None
                    state.calibration_restore_key = None
                state.camera = camera_manager.activate(selected_device_id)
            details = camera_manager.status(device_id, open_camera=selected)
        return jsonify(
            ok=True,
            selected=selected,
            changed=changed,
            affected_devices=sorted(affected_devices),
            selected_camera_changed=selected_changed,
            calibration_revalidation_required=calibration_revalidation,
            selected_calibration_revalidation_required=selected_calibration_revalidation,
            **details,
        )

    @app.get("/api/devices/<device_id>/camera/status")
    def device_camera_status(device_id):
        selected = device_id == selected_camera_device_id()
        status = camera_manager.status(device_id, open_camera=True)
        return jsonify({**status, "selected": selected})

    @app.get("/api/devices")
    def pi_devices():
        bridge = require_pi_bridge()
        result = dict(bridge.devices())
        devices = result.get("devices")
        if isinstance(devices, list):
            devices = [
                dict(item) if isinstance(item, dict) else item for item in devices
            ]
            result["devices"] = devices
        with state.lock:
            state.pi_catalog_initialized = True
            state.update_pi_devices(result)
            registered = (
                [
                    item.get("id")
                    for item in devices
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                ]
                if isinstance(devices, list)
                else []
            )
            workflow_service = app.extensions.get("workflow_service")
            recorder = app.extensions.get("workflow_recorder")
            active_work = (
                workflow_service is not None and workflow_service.has_active_run()
            ) or (recorder is not None and recorder.active)
            if bridge.device_id not in registered and not active_work:
                fallback = (
                    "phone_A"
                    if "phone_A" in registered
                    else (registered[0] if registered else None)
                )
                if fallback is None:
                    state.clear_pi_device()
                else:
                    selected = state.select_pi_device(fallback)
                    if workflow_service is not None:
                        workflow_service.device_id = selected
            result["selected_device"] = bridge.device_id
        if isinstance(devices, list):
            assignment_store = app.extensions["device_workflow_assignment_store"]
            for item in devices:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                item["workflow_assignment"] = workflow_assignment_details(item["id"])
                item["ready_for_work"] = assignment_store.ready_for_work(item["id"])
        return jsonify(result)

    @app.put("/api/devices/<device_id>/work-readiness")
    @local_dashboard_access
    def device_work_readiness(device_id):
        _, _, registered = registered_pi_devices()
        if device_id not in registered:
            raise PiApiError(
                f"Pi device {device_id!r} is not registered.", http_status=404
            )
        data = payload()
        if set(data) != {"ready"} or type(data.get("ready")) is not bool:
            raise ValueError("Expected ready as a boolean.")
        manager = app.extensions.get("withdraw_manager")
        if manager is not None:
            ready = manager.set_work_readiness(device_id, data["ready"])
        else:
            ready = app.extensions[
                "device_workflow_assignment_store"
            ].set_ready_for_work(device_id, data["ready"])
        return jsonify(ok=True, device_id=device_id, ready_for_work=ready)

    @app.route("/api/devices/<device_id>/workflows", methods=["GET", "PUT"])
    def device_workflows(device_id):
        bridge, _, registered = registered_pi_devices()
        del bridge
        if device_id not in registered:
            raise PiApiError(
                f"Pi device {device_id!r} is not registered.", http_status=404
            )
        if request.method == "GET":
            return jsonify(
                workflow_assignment_details(device_id, include_available=True)
            )

        denied = allow_local_dashboard()
        if denied is not None:
            return denied

        data = payload()

        if (
            not isinstance(data, dict)
            or "workflow_ids" not in data
            or set(data) - {"workflow_ids", "balance_workflow_id"}
            or not isinstance(data["workflow_ids"], list)
        ):
            raise ValueError("Expected workflow_ids and optional balance_workflow_id.")

        if len(data["workflow_ids"]) > 1:
            raise ValueError("Only one workflow can be assigned to a phone.")

        workflow_storage = app.extensions["workflow_storage"]
        assignment_store = app.extensions["device_workflow_assignment_store"]
        balance_workflow_id = data.get(
            "balance_workflow_id",
            assignment_store.integration(device_id)["balance_workflow_id"],
        )
        if balance_workflow_id is not None and not isinstance(balance_workflow_id, str):
            raise ValueError("balance_workflow_id must be a workflow ID or null.")

        loaded_workflows = {}
        for workflow_id in [*data["workflow_ids"], balance_workflow_id]:
            if workflow_id is None:
                continue
            try:
                loaded_workflows[workflow_id] = workflow_storage.read(workflow_id)
            except FileNotFoundError:
                raise ValueError(
                    f"Workflow {workflow_id!r} does not exist and cannot be assigned."
                ) from None

        for workflow_id in data["workflow_ids"]:
            if loaded_workflows[workflow_id].get("purpose") != "payment":
                raise ValueError(
                    "Only a Payment purpose workflow can be assigned as the "
                    "Payment Workflow."
                )

        if balance_workflow_id is not None:
            balance_check_workflow(loaded_workflows[balance_workflow_id])

        withdraw_manager = app.extensions.get("withdraw_manager")
        if withdraw_manager is not None:
            withdraw_manager.workflow_assignment_changed(
                device_id, data["workflow_ids"]
            )
        assignment_store.set_routes(
            device_id, data["workflow_ids"], balance_workflow_id
        )
        if withdraw_manager is not None:
            withdraw_manager.reload_after_workflow_assignment(device_id)
        return jsonify(workflow_assignment_details(device_id, include_available=True))

    @app.get("/api/devices/<device_id>/status")
    def pi_device_status(device_id):
        return jsonify(require_pi_bridge().device_status(device_id))

    @app.post("/api/devices/select")
    def select_pi_device():
        data = payload()
        if set(data) != {"device_id"}:
            raise ValueError("Expected device_id only.")

        bridge, _, registered = registered_pi_devices()

        if data["device_id"] not in registered:
            raise ValueError("Select a registered phone returned by the Pi.")

        workflow_service = app.extensions.get("workflow_service")
        recorder = app.extensions.get("workflow_recorder")

        with state.lock:
            if recorder is not None and recorder.active:
                raise ValueError(
                    "Stop or discard the recording before switching phones."
                )

            selected = state.select_pi_device(data["device_id"])

            if workflow_service is not None:
                workflow_service.device_id = selected

            return jsonify(ok=True, selected_device=selected)

    @app.get("/api/enrollment")
    def pi_enrollment():
        return jsonify(require_pi_bridge().enrollment())

    @app.post("/api/enrollment/start")
    @local_dashboard_access
    def start_pi_enrollment():
        data = payload()
        if set(data) != {"target_device_id"}:
            raise ValueError("Expected target_device_id only.")
        bridge, _, registered = registered_pi_devices()
        if data["target_device_id"] in registered:
            raise ValueError(
                "This device ID is already registered. Use Replace on its device card."
            )
        result = bridge.start_enrollment(data["target_device_id"])
        app.extensions["device_workflow_assignment_store"].set_ready_for_work(
            data["target_device_id"], False
        )
        return jsonify(result)

    @app.post("/api/enrollment/stop")
    @local_dashboard_access
    def stop_pi_enrollment():
        data = payload()
        if data:
            raise ValueError("Stop Enrollment expects an empty JSON object.")
        return jsonify(require_pi_bridge().stop_enrollment())

    @app.post("/api/devices/<device_id>/replace")
    @local_dashboard_access
    def replace_pi_device(device_id):
        data = payload()
        if data:
            raise ValueError("Replace expects an empty JSON object.")
        bridge, _, registered = registered_pi_devices()
        if device_id not in registered:
            raise ValueError(
                "Replace is available only for a registered phone. Use Add Phone first."
            )
        return jsonify(bridge.replace_device(device_id))

    @app.delete("/api/devices/<device_id>")
    @local_dashboard_access
    def delete_pi_device(device_id):
        data = payload()
        if data:
            raise ValueError("Delete Phone expects an empty JSON object.")
        bridge = require_pi_bridge()
        workflow_service = app.extensions.get("workflow_service")
        recorder = app.extensions.get("workflow_recorder")
        with state.lock:
            if workflow_service is not None and workflow_service.has_active_run():
                raise ValueError(
                    "Wait for the active workflow to finish before deleting a phone."
                )
            if recorder is not None and recorder.active:
                raise ValueError(
                    "Stop or discard the recording before deleting a phone."
                )

        _, _, registered = registered_pi_devices()
        if device_id not in registered:
            raise PiApiError(
                f"Pi device {device_id!r} is not registered.",
                http_status=404,
            )
        try:
            result = bridge.delete_device(device_id)
        except PiApiError as exc:
            if exc.http_status != 404:
                raise
            # It existed in the immediately preceding catalog, so a 404 here
            # means it disappeared during the delete race. Reconcile normally.
            result = {"ok": True, "already_deleted": True}
        catalog = bridge.devices()
        devices = catalog.get("devices")
        if not isinstance(devices, list):
            raise PiApiError("Pi /devices response is missing the device list.")
        remaining = [
            item.get("id")
            for item in devices
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]

        with state.lock:
            state.update_pi_devices(catalog)
            if bridge.device_id == device_id:
                fallback = (
                    "phone_A"
                    if "phone_A" in remaining
                    else (remaining[0] if remaining else None)
                )
                if fallback is None:
                    state.clear_pi_device()
                    if workflow_service is not None:
                        workflow_service.device_id = None
                else:
                    selected = state.select_pi_device(fallback)
                    if workflow_service is not None:
                        workflow_service.device_id = selected
            camera_manager.release_device(device_id)
            app.extensions["device_workflow_assignment_store"].delete(device_id)

        response = dict(result)
        response.update(catalog)
        response["selected_device"] = bridge.device_id
        return jsonify(response)

    @app.get("/api/frame")
    @app.get("/api/devices/<device_id>/camera/frame")
    def frame(device_id=None):
        with state.lock:
            with camera_manager.lock:
                selected = selected_camera_device_id()
                requested_device = device_id or selected
                if requested_device is None:
                    return (
                        jsonify(
                            **error_fields(
                                "Select a phone before requesting a camera frame.",
                                "camera_device_required",
                            )
                        ),
                        409,
                    )
                camera_manager.config(requested_device)
                active_camera = camera_manager.get_camera(requested_device)

                if requested_device == selected:
                    state.camera = active_camera
                camera_status = active_camera.status()
                if not camera_status["ready"]:
                    return (
                        jsonify(
                            **error_fields(camera_status["error"], "camera_unavailable")
                        ),
                        503,
                    )
                latest = active_camera.frame()
                if latest is None:
                    return (
                        jsonify(
                            **error_fields(
                                "Camera frame unavailable.", "camera_frame_unavailable"
                            )
                        ),
                        503,
                    )
                if not camera_manager.is_current(requested_device, active_camera):
                    return (
                        jsonify(
                            **error_fields(
                                "Camera selection changed; request a new frame.",
                                "camera_selection_changed",
                            )
                        ),
                        409,
                    )
        jpeg, number, _, _ = latest
        if preview_debug:
            now = time.monotonic()
            report = None
            with request_metrics["lock"]:
                previous = request_metrics["last_response"]
                if previous is not None:
                    interval = now - previous
                    request_metrics["interval_total"] += interval
                    request_metrics["interval_max"] = max(
                        request_metrics["interval_max"], interval
                    )
                request_metrics["last_response"] = now
                request_metrics["frames"] += 1
                request_metrics["first_sequence"] = (
                    number
                    if request_metrics["first_sequence"] is None
                    else request_metrics["first_sequence"]
                )
                request_metrics["last_sequence"] = number
                request_metrics["route_total"] += now - getattr(
                    g, "preview_request_started", now
                )
                elapsed = now - request_metrics["window_started"]
                if elapsed >= 1:
                    count = request_metrics["frames"]
                    report = (
                        count / elapsed,
                        (
                            request_metrics["last_sequence"]
                            - request_metrics["first_sequence"]
                        )
                        / elapsed,
                        (request_metrics["interval_total"] / max(1, count - 1)),
                        request_metrics["interval_max"],
                        request_metrics["route_total"] / count,
                        request_metrics["active"],
                    )
                    request_metrics.update(
                        window_started=now,
                        frames=0,
                        first_sequence=None,
                        last_sequence=None,
                        last_response=None,
                        interval_total=0.0,
                        interval_max=0.0,
                        route_total=0.0,
                    )
            if report is not None:
                LOG.info(
                    "Preview response FPS=%.1f camera-sequence FPS=%.1f "
                    "yield interval avg=%.1fms max=%.1fms route=%.1fms "
                    "active_requests=%d threads=%d",
                    report[0],
                    report[1],
                    report[2] * 1000,
                    report[3] * 1000,
                    report[4] * 1000,
                    report[5],
                    threading.active_count(),
                )
        return Response(
            jpeg,
            mimetype="image/jpeg",
            headers={
                "X-Frame-Id": str(number),
                "X-Camera-Device-Id": requested_device,
            },
        )

    @app.get("/api/devices/<device_id>/camera/preview")
    def calibrated_preview(device_id):
        """Return this logical phone's calibrated view of its shared camera."""
        with state.lock:
            with camera_manager.lock:
                selected = selected_camera_device_id()
                camera_manager.config(device_id)
                active_camera = camera_manager.get_camera(device_id)
                camera_status = active_camera.status()
                if not camera_status["ready"]:
                    return (
                        jsonify(
                            **error_fields(camera_status["error"], "camera_unavailable")
                        ),
                        503,
                    )
                latest = active_camera.frame()
                if latest is None:
                    return (
                        jsonify(
                            **error_fields(
                                "Camera frame unavailable.", "camera_frame_unavailable"
                            )
                        ),
                        503,
                    )
                preview_frame = (
                    latest[0],
                    latest[1],
                    camera_status["width"],
                    camera_status["height"],
                )

                if state.pi_bridge is not None:
                    serial = "pi-api:" + state.pi_bridge.client.base_url
                    if device_id != DEFAULT_DEVICE_ID:
                        serial += "#" + device_id
                    preview_device = PiControlTarget(serial, device_id=device_id)
                elif device_id == selected:
                    if state.device is None:
                        state.require_ready()
                    preview_device = state.device
                else:
                    return (
                        jsonify(
                            **error_fields(
                                "This logical phone is not available.",
                                "device_unavailable",
                                {"device_id": device_id},
                            )
                        ),
                        404,
                    )

                if state.calibration_storage is None:
                    calibration = None
                else:
                    calibration = state.calibration_storage.load(
                        active_camera,
                        camera_status,
                        preview_device,
                    )
                if calibration is None:
                    return (
                        jsonify(
                            **error_fields(
                                "Calibration required for this phone.",
                                "calibration_missing",
                                {"device_id": device_id},
                            )
                        ),
                        409,
                    )

        try:
            jpeg, number, width, height, cache_hit = calibrated_preview_cache.encode(
                device_id,
                active_camera,
                calibration,
                preview_device,
                preview_frame,
            )
        except ScreenStabilityError as exc:
            return jsonify(**error_fields(str(exc), "camera_preview_failed")), 503

        with camera_manager.lock:
            if not camera_manager.is_current(device_id, active_camera):
                return (
                    jsonify(
                        **error_fields(
                            "Camera selection changed; request a new preview.",
                            "camera_selection_changed",
                        )
                    ),
                    409,
                )
        return Response(
            jpeg,
            mimetype="image/jpeg",
            headers={
                "X-Frame-Id": str(number),
                "X-Camera-Device-Id": device_id,
                "X-Preview-Coordinate-Space": "screen_normalized",
                "X-Preview-Width": str(width),
                "X-Preview-Height": str(height),
                "X-Preview-Cache": "hit" if cache_hit else "miss",
            },
        )

    @app.post("/api/control")
    def control():
        data, client = payload(), client_id()
        if type(data.get("enabled")) is not bool:
            raise ValueError("enabled must be true or false.")
        with state.lock:
            if not data["enabled"]:
                state.disable()
            else:
                if state.pi_bridge is not None:
                    state.refresh_pi_status()
                state.require_ready()
                state.require_frame(data.get("frame_id"))
                if state.calibration is None:
                    raise ValueError(
                        "Calibrate the four display corners before enabling input."
                    )
                if state.owner and state.owner != client:
                    raise ValueError(
                        "Another browser tab owns Manual Control. Disable it first."
                    )
                if state.pending:
                    raise DeviceBusy("automation")
                with state.device_locks.hold(state.device.serial, "manual"):
                    state.owner = client
                    state.renew_lease()
                    state.message = "Manual control active."
                    LOG.info("Manual Control enabled")
            return jsonify(
                enabled=state.owner is not None and not state.pending,
                pending=state.pending,
            ), (202 if state.pending else 200)

    @app.post("/api/takeover")
    @local_dashboard_access
    def takeover():
        data, client = payload(), client_id()
        if data:
            raise ValueError("Take Over expects an empty JSON object.")
        with state.lock:
            state.refresh()
            if state.device is None:
                raise DeviceError(state.message)
            # Cancelling automation is safe without calibration or a live camera.
            # Only resuming actual manual input needs those existing safeguards.
            if (
                not state.pending
                and state.device_locks.owner(state.device.serial) == "automation"
            ):
                state.disable("Automation running. Manual control disabled.")
            if state.owner and state.owner != client:
                raise ValueError(
                    "Another browser tab owns Manual Control. Disable it first."
                )
            if not state.pending:
                state.manual_claim = state.device_locks.request_takeover(
                    state.device.serial
                )
                state.owner = client
                LOG.info("Takeover requested on %s", state.device.serial)
            state.renew_lease()
            state.message = "Takeover requested. Waiting for automation to yield."
            # Never wait for ADB or claim manual ownership inside this request.
            return (
                jsonify(
                    enabled=False, pending=True, control_status="takeover_requested"
                ),
                202,
            )

    @app.post("/api/heartbeat")
    def heartbeat():
        data, client = payload(), client_id()
        with state.lock:
            if state.owner == client and time.monotonic() < state.expires:
                state.require_ready()
                state.require_frame(data.get("frame_id"))
                if state.owner == client:
                    if state.pending:
                        state.renew_lease()
                        if state.claim_manual() and state.calibration is None:
                            state.disable(
                                "Automation stopped. Calibrate before enabling manual input."
                            )
                    elif state.device_locks.owner(state.device.serial) == "automation":
                        state.disable(
                            "Automation running. Manual control disabled. Use Take Over to interrupt."
                        )
                    else:
                        state.renew_lease()

            elif state.owner == client:
                state.disable("Control heartbeat expired. Enable Manual Control again.")
            return jsonify(
                enabled=state.owner == client and not state.pending,
                pending=state.owner == client and state.pending,
            )

    @app.route("/api/calibration", methods=["POST", "DELETE"])
    @local_dashboard_access
    def calibrate():
        data = payload()
        with state.lock:
            state.disable("Calibration changed. Input disabled.")
            state.calibration = None
            state.calibration_restore_key = None

            if request.method == "DELETE":
                if state.calibration_storage is not None:
                    try:
                        calibration_device = state.device or (
                            state.pi_bridge.target if state.pi_bridge else None
                        )
                        state.calibration_storage.clear(calibration_device)
                    except OSError as exc:
                        LOG.error("Could not clear saved camera calibration: %s", exc)
                        raise ValueError(
                            "Calibration could not be cleared from local storage."
                        ) from exc
                return jsonify(calibrated=False)

            camera_info = state.require_ready()
            state.require_frame(data.get("frame_id"))
            calibration = Calibration(data.get("corners"))
            signature = (
                state.device,
                camera_info["width"],
                camera_info["height"],
            )
            if state.calibration_storage is not None:
                try:
                    state.calibration_storage.save(
                        calibration,
                        state.camera,
                        camera_info,
                        state.device,
                    )
                except (OSError, TypeError, ValueError) as exc:
                    LOG.error("Could not save camera calibration: %s", exc)
                    raise ValueError(
                        "Calibration could not be saved to local storage."
                    ) from exc
            state.calibration = calibration
            state.signature = signature
            if state.calibration_storage is not None:
                state.calibration_restore_key = (
                    signature,
                    getattr(
                        state.camera,
                        "configured_source",
                        state.camera.source,
                    ),
                    state.camera.rotate,
                )
            state.message = "Calibration saved. You can enable Manual Control."
            LOG.info("Four-corner calibration saved for %s", state.device.serial)
            return jsonify(calibrated=True, corners=calibration.corners.tolist())

    @app.post("/api/key")
    def key():
        data, client = payload(), client_id()
        if set(data) != {"type", "value", "frame_id"}:
            raise ValueError("Expected type, value, and frame_id only.")
        validate_key(data["type"], data["value"])
        if type(data["frame_id"]) is not int:
            raise ValueError("A fresh camera frame is required.")
        if not state.action_lock.acquire(blocking=False):
            return jsonify(error="Controller busy. No action sent."), 409
        try:
            with state.lock, state.input_control(client, data["frame_id"]):
                recorded = None
                try:
                    if state.pi_bridge is not None:
                        if data["type"] == "text":
                            step = {
                                "type": "input",
                                "source": "fixed",
                                "value": data["value"],
                            }
                            perform = lambda: state.pi_bridge.type_text(data["value"])
                        else:
                            key_name = (
                                "MOVE_HOME"
                                if data["value"] == "home"
                                else data["value"].upper()
                            )
                            step = {"type": "key", "key": key_name}
                            perform = lambda: state.pi_bridge.key(data["value"])
                        recorded = app.extensions["record_manual"](step, perform)
                    else:
                        context = AutomationContext(
                            state.device,
                            state.android,
                            threading.Event(),
                            LOG,
                            SimpleNamespace(),
                            input_backend="appium",
                        )
                        context.send_key(data["type"], data["value"])
                except (DeviceError, PiApiError) as exc:
                    state.disable(str(exc))
                    raise
                return jsonify(sent=True, recording=recorded)
        finally:
            state.action_lock.release()

    @app.post("/api/text")
    def text_input():
        data, client = payload(), client_id()
        if set(data) != {"text", "frame_id"}:
            raise ValueError("Expected text and frame_id only.")
        validate_pi_text(data["text"])
        if not state.action_lock.acquire(blocking=False):
            return jsonify(error="Controller busy. No action sent."), 409
        try:
            with state.lock, state.input_control(client, data["frame_id"]):
                recorded = None
                if state.pi_bridge is not None:
                    recorded = app.extensions["record_manual"](
                        {"type": "input", "source": "fixed", "value": data["text"]},
                        lambda: state.pi_bridge.type_text(data["text"]),
                    )
                else:
                    context = AutomationContext(
                        state.device,
                        state.android,
                        threading.Event(),
                        LOG,
                        SimpleNamespace(),
                        input_backend="appium",
                    )
                    context.input.type_text(data["text"])
                return jsonify(sent=True, recording=recorded)
        finally:
            state.action_lock.release()

    @app.post("/api/action")
    def action():
        data, client = payload(), client_id()
        # Drop overlapping inputs instead of building a queue of phone commands.
        if not state.action_lock.acquire(blocking=False):
            return jsonify(error="Controller busy. No action sent."), 409
        try:
            with state.lock, state.input_control(client, data.get("frame_id")):
                name = data.get("action")

                if name not in (
                    "tap",
                    "swipe",
                    "back",
                    "home",
                    "recents",
                    "swipe_up",
                    "swipe_down",
                    "swipe_left",
                    "swipe_right",
                    "screen_on",
                    "screen_off",
                ):
                    raise ValueError("Unsupported control action.")

                pixel, normalized = None, None
                swipe_start, swipe_end, swipe_duration = None, None, None
                coordinate_space = data.get("coordinate_space", "camera_normalized")
                if coordinate_space not in {
                    "camera_normalized",
                    "screen_normalized",
                }:
                    raise ValueError("Unsupported coordinate_space.")

                def action_point(value):
                    if not isinstance(value, (list, tuple)) or len(value) != 2:
                        raise ValueError("A normalized coordinate is required.")
                    if coordinate_space == "screen_normalized":
                        return [unit(value[0]), unit(value[1])]
                    return state.calibration.map(value)

                if name == "tap":
                    normalized = action_point([data.get("x"), data.get("y")])
                    if state.pi_bridge is not None:
                        pixel = None
                    else:
                        pixel = normalized_to_device(
                            normalized[0],
                            normalized[1],
                            state.device.width,
                            state.device.height,
                        )
                elif name == "swipe":
                    start, end = data.get("start"), data.get("end")
                    swipe_duration = data.get("duration_ms", 300)
                    if (
                        type(swipe_duration) is not int
                        or not 50 <= swipe_duration <= 60000
                    ):
                        raise ValueError(
                            "Swipe duration must be 50–60000 milliseconds."
                        )
                    if state.pi_bridge is not None:
                        swipe_start = action_point(start)
                        swipe_end = action_point(end)
                    else:
                        swipe_start = action_point(start)
                        swipe_end = action_point(end)

                recorded = None
                try:
                    if state.pi_bridge is not None:
                        if name != "swipe" and name not in state.pi_bridge.ACTIONS:
                            raise ValueError(
                                "This action is not supported; no input sent."
                            )
                        if name == "tap":
                            step = {
                                "type": "tap",
                                "x": normalized[0],
                                "y": normalized[1],
                            }
                        elif name == "swipe":
                            step = {
                                "type": "swipe",
                                "start": list(swipe_start),
                                "end": list(swipe_end),
                                "duration_ms": swipe_duration,
                            }
                        elif name in state.pi_bridge.SWIPES:
                            points = state.pi_bridge.SWIPES[name]
                            step = {
                                "type": "swipe",
                                "start": list(points[:2]),
                                "end": list(points[2:]),
                            }
                        else:
                            step = {"type": "key", "key": name.upper()}
                        if name == "swipe":
                            operation = lambda: state.pi_bridge.swipe(
                                *swipe_start, *swipe_end
                            )
                        else:
                            operation = lambda: state.pi_bridge.action(name, normalized)
                        recorded = app.extensions["record_manual"](step, operation)
                    else:
                        context = AutomationContext(
                            state.device,
                            state.android,
                            threading.Event(),
                            LOG,
                            SimpleNamespace(),
                            input_backend="appium",
                        )
                        if name == "swipe":
                            context.input.swipe(
                                *swipe_start, *swipe_end, swipe_duration
                            )
                        else:
                            context.send(name, pixel)
                except (DeviceError, PiApiError) as exc:
                    state.disable(str(exc))
                    raise

                return jsonify(
                    action=name, pixel=pixel, normalized=normalized, recording=recorded
                )
        finally:
            state.action_lock.release()

    register_workflows(
        app,
        state,
        payload,
        client_id,
        workflow_storage,
        device_id=device_id,
        ocr_settings_storage=ocr_settings_storage,
        ocr_resource_provider=ocr_resource_provider,
        screenshot_store=screenshot_store,
        admin_required=local_dashboard_access,
        require_admin=allow_local_dashboard,
    )
    assignment_store = app.extensions["device_workflow_assignment_store"]
    legacy_balance_workflow_ids = {
        integration["balance_workflow_id"]
        for integration in assignment_store.integrations().values()
        if integration.get("balance_workflow_id")
    }
    workflow_store = app.extensions["workflow_storage"]
    workflow_store.migrate_legacy_purposes(
        legacy_balance_workflow_ids,
        balance_check_workflow,
    )
    # A legacy workflow may have occupied both roles. Once it is classified as
    # Balance Check it must not remain silently authorized as the device's
    # Payment Workflow. Preserve the balance route and clear only that
    # incompatible payment assignment.
    for assigned_device_id, integration in assignment_store.integrations().items():
        allowed = assignment_store.get(assigned_device_id)
        if not allowed:
            continue
        try:
            incompatible = any(
                workflow_store.read(workflow_id).get("purpose") != "payment"
                for workflow_id in allowed
            )
        except FileNotFoundError:
            incompatible = False
        if incompatible:
            assignment_store.set_routes(
                assigned_device_id,
                [],
                integration.get("balance_workflow_id"),
            )
            LOG.warning(
                "Cleared incompatible Payment Workflow assignment for %s after "
                "workflow-purpose migration.",
                assigned_device_id,
            )
    register_jobs(
        app,
        state,
        payload,
        account_store=account_store,
        job_store=job_store,
        start_executor=start_job_executor,
        admin_required=local_dashboard_access,
        require_admin=allow_local_dashboard,
    )
    # Keep workers off in embedded/test applications unless explicitly
    # requested. The normal server starts them beside the job executor.
    if start_withdraw_workers is None:
        start_withdraw_workers = start_job_executor
    register_withdraw(
        app,
        payload,
        admin_required=local_dashboard_access,
        ensure_registered_device=app.extensions["ensure_registered_device"],
        start_workers=start_withdraw_workers,
    )
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--camera",
        type=int,
        default=0,
        help="External camera index (default 0)",
    )

    parser.add_argument(
        "--camera-url",
        type=str,
        default=None,
        help="HTTP/MJPEG/RTSP camera stream URL",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8765,
    )

    parser.add_argument(
        "--width",
        type=int,
        default=1280,
    )

    parser.add_argument(
        "--height",
        type=int,
        default=720,
    )

    parser.add_argument(
        "--rotate",
        type=int,
        choices=[0, 90, 180, 270],
        default=0,
        help="Rotate camera image clockwise: 0, 90, 180, or 270 degrees",
    )

    args = parser.parse_args()

    if (
        args.camera < 0
        or not 1 <= args.port <= 65535
        or min(args.width, args.height) < 1
    ):
        parser.error("Invalid camera index, port, or frame size.")

    configure_development_logging()

    # Use network stream if provided,
    # otherwise use local camera index.
    camera_source = args.camera_url if args.camera_url else args.camera

    camera_manager = CameraManager(
        default_source=camera_source,
        default_rotation=args.rotate,
        width=args.width,
        height=args.height,
    )
    camera = camera_manager.activate("phone_A")
    app = create_app(
        camera,
        camera_manager=camera_manager,
        start_job_executor=True,
    )

    screen_provider = app.extensions.get("workflow_screen_state_provider")
    start_ocr_worker = getattr(screen_provider, "start_worker", None)
    if start_ocr_worker is not None:

        def warm_ocr_worker():
            try:
                start_ocr_worker()
            except Exception as exc:
                log_event(
                    LOG,
                    logging.ERROR,
                    "ocr",
                    "ocr_worker_start_failed",
                    error_code="ocr_worker_start_failed",
                    exception_type=type(exc).__name__,
                )

        threading.Thread(
            target=warm_ocr_worker,
            name="camera-ocr-warmup",
            daemon=True,
        ).start()

    LOG.info(
        "Open http://127.0.0.1:%d — input starts disabled",
        args.port,
    )

    try:
        app.run(
            host="127.0.0.1",
            port=args.port,
            threaded=True,
            debug=False,
            use_reloader=False,
        )

    finally:
        withdraw_manager = app.extensions.get("withdraw_manager")
        if withdraw_manager is not None:
            withdraw_manager.close()
        job_executor = app.extensions.get("job_executor")
        if job_executor is not None:
            job_executor.close()
        screen_provider = app.extensions.get("workflow_screen_state_provider")
        close_provider = getattr(screen_provider, "close", None)
        if close_provider is not None:
            close_provider()
        with app.extensions["control_state"].lock:
            app.extensions["control_state"].disable()
        camera_manager.release_all()


if __name__ == "__main__":
    main()
