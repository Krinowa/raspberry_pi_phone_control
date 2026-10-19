"""Workflow routes share the dashboard's local token, ownership and health gates."""

import base64
import uuid
import cv2

import logging
import os
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

from flask import jsonify, request

from ..backends import backend_names, select_device
from ..context import AutomationContext
from ..coordinates import normalized_roi
from ..coordinates import unit
from .values import parse_currency
from .timing import WorkflowTiming
from ..input.base import UnsupportedOperation
from ..screen.accessibility import AccessibilityScreenStateProvider
from ..screen.camera import CalibratedCamera
from ..screen.camera_ocr import CameraOCRScreenStateProvider
from ..screen.ocr_settings import OCRPoolBusy, OCRSettingsService
from ..screen.state import ScreenStateError
from ..screen.text_learning import text_suggestions
from .capture import inspect_target, tap_preview
from .recorder import Recorder
from .runner import (
    WorkflowRunner,
    format_input_value,
    screen_keyboard_points,
    type_with_screen_keyboard,
)
from .service import WorkflowService
from .storage import WorkflowStorage
from hardware.raspberry_pi_input import PiControlTarget

LOG = logging.getLogger("workflow_api")


def register_workflows(
    app,
    state,
    payload,
    client_id,
    storage=None,
    *,
    device_id=None,
    ocr_settings_storage=None,
    ocr_resource_provider=None,
    screenshot_store=None,
    admin_required,
    require_admin,
):
    storage = storage or WorkflowStorage()
    recorder = Recorder()
    lock = state.lock

    external_screen = CalibratedCamera(state)

    accessibility_url = os.environ.get(
        "PHONE_ACCESSIBILITY_URL",
        "",
    ).strip()

    ocr_settings = OCRSettingsService(
        ocr_settings_storage,
        resource_provider=ocr_resource_provider,
    )

    if accessibility_url:
        screen_state_provider = AccessibilityScreenStateProvider(accessibility_url)
    else:
        screen_state_provider = CameraOCRScreenStateProvider(
            external_screen,
            worker_pool_size=ocr_settings.worker_pool_size,
        )

    # Alternate providers may implement the public screen API without a
    # Paddle pool. In that case settings stay readable and status reports that
    # no runtime pool is attached.
    worker_pool = getattr(screen_state_provider, "_worker", None)
    if worker_pool is not None:
        ocr_settings.attach(worker_pool)

    def resolve_job_screen(target_device_id):
        # Dashboard selection is presentation state. A queued job instead uses
        # the camera and persisted calibration belonging to its fixed device.
        if (
            state.pi_bridge is None
            or state.camera_manager is None
            or state.calibration_storage is None
            or not hasattr(screen_state_provider, "for_screen")
        ):
            return external_screen, screen_state_provider
        camera = state.camera_manager.get_camera(target_device_id)
        camera_info = camera.status()
        serial = "pi-api:" + state.pi_bridge.client.base_url
        if target_device_id != "phone_A":
            serial += "#" + target_device_id
        device = PiControlTarget(serial, device_id=target_device_id)
        calibration = state.calibration_storage.load(camera, camera_info, device)
        isolated_state = SimpleNamespace(
            lock=threading.RLock(),
            calibration=calibration,
            signature=(device, camera_info["width"], camera_info["height"]),
            camera=camera,
        )
        isolated_screen = CalibratedCamera(isolated_state)
        return isolated_screen, screen_state_provider.for_screen(isolated_screen)

    service = WorkflowService(
        state.android,
        state.device_locks,
        external_screen,
        screen_state_provider,
        device_id=device_id,
        screen_resolver=resolve_job_screen,
        screenshot_store=screenshot_store,
    )

    app.extensions.update(
        workflow_storage=storage,
        workflow_recorder=recorder,
        workflow_service=service,
        workflow_screen_state_provider=screen_state_provider,
        ocr_settings_service=ocr_settings,
    )

    @app.get("/api/ocr/settings")
    def get_ocr_settings():
        return jsonify(ocr_settings.settings())

    @app.put("/api/ocr/settings")
    @admin_required
    def update_ocr_settings():
        return jsonify(ocr_settings.update(payload()))

    @app.get("/api/ocr/status")
    def get_ocr_status():
        return jsonify(ocr_settings.status())

    @app.post("/api/ocr/restart")
    @admin_required
    def restart_ocr_workers():
        if payload():
            raise ValueError("Restart OCR Workers expects an empty JSON object.")
        try:
            return jsonify(ocr_settings.restart())
        except OCRPoolBusy as exc:
            return jsonify(ok=False, error=str(exc)), 409

    # Bounded, short-lived calibrated snapshots: testing uses exactly the image
    # displayed in the picker, even if the live phone changes meanwhile.
    roi_previews = {}
    roi_preview_lock = threading.Lock()

    @app.post("/api/ocr/roi-preview")
    @admin_required
    def roi_preview():
        if not hasattr(screen_state_provider, "_crop_roi"):
            raise ValueError("ROI testing requires the PaddleOCR camera provider.")

        image = screen_state_provider.capture_snapshot()
        ok, encoded = cv2.imencode(".png", image)

        if not ok:
            raise ValueError("Could not encode calibrated preview.")
        token = uuid.uuid4().hex
        with roi_preview_lock:
            now = time.monotonic()
            for key, (stamp, _) in list(roi_previews.items()):
                if now - stamp > 300:
                    roi_previews.pop(key)
            while len(roi_previews) >= 8:
                roi_previews.pop(next(iter(roi_previews)))
            roi_previews[token] = (now, image)
        return jsonify(
            token=token,
            width=image.shape[1],
            height=image.shape[0],
            image="data:image/png;base64," + base64.b64encode(encoded).decode(),
        )

    @app.post("/api/ocr/roi-test")
    @admin_required
    def roi_test():
        data = payload()
        roi = normalized_roi(data.get("roi"))
        text = data.get("text")
        value_test = data.get("parser") == "currency"
        if not value_test and (
            not isinstance(text, str) or not text.strip() or len(text) > 500
        ):
            raise ValueError("Text to detect must be between 1 and 500 characters.")
        token = data.get("token")
        if not isinstance(token, str):
            raise ValueError("Refresh the calibrated preview first.")
        with roi_preview_lock:
            saved = roi_previews.get(token)
        if saved is None or time.monotonic() - saved[0] > 300:
            raise ValueError("Calibrated preview expired; refresh it first.")
        timing = WorkflowTiming()
        timing.start()
        timing.begin_step(1, "Test ROI OCR")
        try:
            crop = screen_state_provider._crop_roi(saved[1], roi)
            expected = "" if value_test else " ".join(text.split()).casefold()
            # Share pool dispatch, but never overwrite a running device's caches.
            provider = screen_state_provider.for_screen(external_screen)
            candidates = provider._recognize_candidates(
                crop,
                scope="roi:" + repr(tuple(roi)),
                expected=expected,
                timings={"roi": dict(zip(("x", "y", "width", "height"), roi))},
            )
            found = screen_state_provider._candidates_contain(candidates, expected)
        finally:
            timing.end_step()
            timing.finish()
        return jsonify(
            found=found,
            texts=[item["text"] for item in candidates],
            value=(
                format(
                    parse_currency(" ".join(item["text"] for item in candidates)), ".2f"
                )
                if value_test
                else None
            ),
            timing=timing.snapshot(),
        )

    @app.get("/api/ocr/texts")
    def debug_ocr_texts():
        started = time.monotonic()
        preview_debug = app.config.get("CAMERA_PREVIEW_DEBUG", False)
        if preview_debug:
            metrics = app.extensions.get("preview_request_metrics", {})
            metrics_lock = metrics.get("lock")
            if metrics_lock is not None:
                with metrics_lock:
                    active_requests = metrics.get("active", 0)
            else:
                active_requests = 0
            LOG.info(
                "OCR request started active_requests=%d threads=%d",
                active_requests,
                threading.active_count(),
            )
        try:
            texts = screen_state_provider.visible_texts()
        finally:
            if preview_debug:
                LOG.info(
                    "OCR request finished duration=%.3fs threads=%d",
                    time.monotonic() - started,
                    threading.active_count(),
                )

        return jsonify(
            texts=texts,
            count=len(texts),
        )

    @app.before_request
    def workflow_request_identity():
        if request.method != "GET" and request.path.startswith(
            (
                "/api/workflows",
                "/api/workflow-runs",
                "/api/recorder",
            )
        ):
            client_id()

    def recorder_id():
        value = request.headers.get("X-Recorder-Client") or client_id()

        if not 8 <= len(value) <= 80 or not all(c.isalnum() or c == "-" for c in value):
            raise ValueError("Invalid recording identifier.")

        return value

    def recording_screenshot():
        capture = getattr(
            screen_state_provider,
            "capture_snapshot",
            None,
        )

        if not callable(capture):
            return None

        try:
            image = capture()

            ok, encoded = cv2.imencode(
                ".jpg",
                image,
                [cv2.IMWRITE_JPEG_QUALITY, 75],
            )

            if not ok:
                return None

            return {
                "image": base64.b64encode(encoded).decode("ascii"),
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
                "format": "jpeg",
            }

        except Exception as exc:
            LOG.info(
                "Recording screenshot skipped: %s",
                exc,
            )
            return None

    def record_manual(step, perform):
        # The caller already holds the manual ownership/frame/action gates.
        with lock:
            if not recorder.active:
                perform()
                return None

            owner = recorder_id()

            step = dict(step)

            # Capture the screen BEFORE the physical action happens.
            if step.get("type") in {
                "tap",
                "swipe",
                "key",
                "input",
            }:
                screenshot = recording_screenshot()

                if screenshot:
                    step["recording_screenshot"] = screenshot

            candidate = recorder.prepare_manual(
                owner,
                step,
            )

            try:
                # Physical action is executed exactly once and immediately.
                perform()

            except Exception:
                recorder.fail_recording()
                raise

            return recorder.commit_recording(
                owner,
                candidate,
                "manual-text" if step["type"] == "input" else None,
            )

    app.extensions["record_manual"] = record_manual

    # =====================================================================
    # Errors/helpers
    # =====================================================================

    @app.errorhandler(FileNotFoundError)
    def missing_file(exc):
        return jsonify(error="Workflow not found."), 404

    @app.errorhandler(ScreenStateError)
    def screen_state_error(exc):
        return jsonify(error=str(exc)), 503

    def run_snapshot(run_id):
        try:
            return service.snapshot(run_id)

        except KeyError:
            return None

    def ensure_purpose_change_is_unassigned(existing, candidate):
        """Reject an edit that would leave a device assigned to the wrong role."""
        if existing.get("purpose") == candidate.get("purpose"):
            return
        assignments = app.extensions.get("device_workflow_assignment_store")
        if assignments is None:
            return
        workflow_id = existing["id"]
        for device_id, integration in assignments.integrations().items():
            if (
                integration.get("balance_workflow_id") == workflow_id
                and candidate.get("purpose") != "balance_check"
            ):
                raise ValueError(
                    f"Workflow is assigned as the Balance Check Workflow for {device_id}; "
                    "change that assignment before changing its purpose."
                )
            allowed = assignments.get(device_id)
            if (
                allowed is not None
                and workflow_id in allowed
                and candidate.get("purpose") != "payment"
            ):
                raise ValueError(
                    f"Workflow is assigned as the Payment Workflow for {device_id}; "
                    "change that assignment before changing its purpose."
                )

    # =====================================================================
    # Workflow routes
    # =====================================================================

    @app.get("/api/workflows")
    def list_workflows():
        selected_device_id = (
            state.pi_bridge.device_id
            if state.pi_bridge is not None
            else (service.device_id or "phone_A")
        )
        allowed = app.extensions["device_workflow_assignment_store"].get(
            selected_device_id
        )
        return jsonify(
            workflows=storage.list(),
            input_backend=backend_names()[0],
            screen_backend=backend_names()[1],
            selected_device_id=selected_device_id,
            allowed_workflow_ids=None if allowed is None else list(allowed),
            workflow_access_unrestricted=allowed is None,
        )

    @app.post("/api/workflows")
    @admin_required
    def create_workflow():
        return (
            jsonify(
                storage.save(
                    payload(),
                    create=True,
                )
            ),
            201,
        )

    @app.route(
        "/api/workflows/<workflow_id>",
        methods=[
            "GET",
            "PUT",
            "DELETE",
        ],
    )
    def workflow(workflow_id):
        if request.method == "GET":
            return jsonify(storage.read(workflow_id))

        denied = require_admin()
        if denied is not None:
            return denied

        if request.method == "DELETE":
            storage.delete(workflow_id)

            return jsonify(deleted=True)

        data = payload()

        if data.get("id") != workflow_id:
            raise ValueError("Workflow ID must match URL.")

        existing = storage.read(workflow_id)
        ensure_purpose_change_is_unassigned(existing, data)

        return jsonify(storage.save(data))

    @app.post("/api/workflows/<workflow_id>/run")
    def run_workflow(workflow_id):
        data = payload()
        client = client_id()

        if set(data) - {"variables"}:
            raise ValueError("Expected variables only.")

        selected_workflow = storage.read(workflow_id)
        target_device_id = (
            state.pi_bridge.device_id
            if state.pi_bridge is not None
            else (service.device_id or "phone_A")
        )
        if not app.extensions["device_workflow_assignment_store"].allows(
            target_device_id, workflow_id
        ):
            raise ValueError(f"Workflow is not assigned to device {target_device_id}.")

        with lock:
            if recorder.workflow is not None:
                raise ValueError("Save or cancel recording " "before playback.")

            with state.lock:
                if (
                    state.pi_bridge is not None
                    and state.selected_pi_connected() is False
                ):
                    raise ValueError(
                        "The selected phone is disconnected. Reconnect it before running a workflow."
                    )
                if state.owner and state.owner != client:
                    raise ValueError("Another browser owns " "manual control.")

                state.disable("Workflow starting. " "Manual control disabled.")

            return (
                jsonify(
                    service.start(
                        selected_workflow,
                        data.get("variables"),
                        device_id=target_device_id,
                    )
                ),
                202,
            )

    @app.get("/api/workflow-runs/<run_id>")
    def run_status(run_id):
        result = run_snapshot(run_id)

        return (
            (jsonify(result), 200)
            if result
            else (
                jsonify(error="Run not found."),
                404,
            )
        )

    @app.post("/api/workflow-runs/" "<run_id>/<operation>")
    def run_operation(
        run_id,
        operation,
    ):
        data = payload()

        if operation == "resume":
            if set(data) != {"step_index"} or type(data["step_index"]) is not int:
                raise ValueError(
                    "Resume requires the " "displayed checkpoint " "step_index."
                )

        elif data:
            raise ValueError("Expected an empty " "JSON object.")

        if run_snapshot(run_id) is None:
            return (
                jsonify(error="Run not found."),
                404,
            )

        if operation == "cancel":
            return jsonify(service.cancel(run_id))

        if operation != "resume":
            return (
                jsonify(error=("Unknown run operation.")),
                404,
            )

        client = client_id()

        with state.lock:
            if state.owner and state.owner != client:
                raise ValueError("Another browser owns " "manual control.")

            state.disable("Resuming workflow. " "Manual control disabled.")

        return jsonify(
            service.resume(
                run_id,
                data["step_index"],
            )
        )

    # =====================================================================
    # Recorder routes
    # =====================================================================

    @app.post("/api/recorder/start")
    @admin_required
    def start_recording():
        data = payload()
        client = client_id()

        if set(data) - {
            "name",
            "execute",
            "financial",
            "purpose",
        }:
            raise ValueError("Unexpected recording option.")

        with lock:
            if any(item["thread"].is_alive() for item in service.runs.values()):
                raise ValueError("Stop the running workflow " "before recording.")

            return jsonify(
                recorder.start(
                    recorder_id(),
                    data.get(
                        "name",
                        "New Recording",
                    ),
                    data.get(
                        "execute",
                        True,
                    ),
                    data.get(
                        "financial",
                        False,
                    ),
                    data.get("purpose", "payment"),
                )
            )

    @app.post("/api/recorder/edit")
    @admin_required
    def edit_saved_workflow():
        data = payload()

        if set(data) != {"workflow_id"}:
            raise ValueError("Choose a saved workflow " "to edit.")

        with lock:
            if any(item["thread"].is_alive() for item in service.runs.values()):
                raise ValueError("Stop the running workflow " "before editing.")

            return jsonify(
                recorder.edit(
                    recorder_id(),
                    storage.read(data["workflow_id"]),
                )
            )

    @app.get("/api/recorder")
    def recording_status():
        with lock:
            if recorder.workflow is not None:
                recorder.require_owner(recorder_id())

            return jsonify(recorder.snapshot())

    @contextmanager
    def recording_control(
        client,
        frame_id,
    ):
        if backend_names()[0] == "mock":
            device = select_device(state.android)

            with state.device_locks.hold(
                device.serial,
                "manual",
            ):
                yield device

        else:
            with state.input_control(
                client,
                frame_id,
            ):
                yield state.device

    @contextmanager
    def recording_sample_control(client, frame_id):
        # A Pi manual backend remains independent of the workflow backend.
        if state.pi_bridge is not None or backend_names()[0] != "mock":
            with state.input_control(client, frame_id):
                yield state.device
        else:
            device = select_device(state.android)
            with state.device_locks.hold(device.serial, "manual"):
                yield device

    @app.post("/api/recorder/text-suggestions")
    @admin_required
    def recording_text_suggestions():
        data = payload()

        if set(data) - {"step_id", "current"}:
            raise ValueError("Unexpected text-suggestion fields.")

        current = data.get("current", False)

        if type(current) is not bool:
            raise ValueError("Current-screen flag must be a boolean.")

        owner = recorder_id()
        with lock:
            draft = recorder.authorize_text_suggestions(
                owner,
                data.get("step_id"),
                current,
            )
            pending_target = recorder.pending_expected_text
        # OCR must not hold the controller/heartbeat lock.
        try:
            candidate_reader = getattr(
                screen_state_provider,
                "visible_text_candidates",
                None,
            )
            values = (
                candidate_reader()
                if callable(candidate_reader)
                else screen_state_provider.visible_texts()
            )
            suggestions = text_suggestions(values)
        except Exception:
            return (
                jsonify(
                    error=(
                        "Could not read useful text from the current screen. Enter manually."
                    )
                ),
                503,
            )
        with lock:
            recorder.require_owner(owner)
            if recorder.workflow is not draft:
                raise ValueError("Recording changed. Request suggestions again.")
            if pending_target is not None and (
                recorder.pending_expected_text != pending_target
            ):
                raise ValueError("Expected-text selection was cancelled.")
        return jsonify(suggestions=suggestions)

    @app.post("/api/recorder/expected-text")
    @admin_required
    def add_recording_expected_text():
        data = payload()

        if set(data) - {"text", "timeout_seconds", "roi"}:
            raise ValueError("Unexpected expected-text fields.")

        with lock:
            return jsonify(
                recorder.add_pending_expected_text(
                    recorder_id(),
                    data.get("text"),
                    data.get("timeout_seconds", 10),
                    data.get("roi"),
                )
            )

    @app.post("/api/recorder/expected-text/cancel")
    @admin_required
    def cancel_recording_expected_text():
        data = payload()

        if data:
            raise ValueError("Expected an empty JSON object.")

        with lock:
            return jsonify(recorder.cancel_expected_text(recorder_id()))

    @app.post("/api/recorder/alternate-screen/start")
    @admin_required
    def start_recording_alternate_screen():
        data = payload()
        if set(data) - {"text", "timeout_seconds", "roi", "after_step_id"}:
            raise ValueError("Unexpected Special Case fields.")
        with lock:
            return jsonify(
                recorder.start_alternate_screen(
                    recorder_id(),
                    data.get("text"),
                    data.get("timeout_seconds", 0.8),
                    data.get("roi"),
                    data.get("after_step_id"),
                )
            )

    @app.post("/api/recorder/alternate-screen/<operation>")
    @admin_required
    def recording_alternate_screen_operation(operation):
        if payload():
            raise ValueError("Expected an empty JSON object.")
        with lock:
            if operation == "finish":
                return jsonify(recorder.finish_alternate_screen(recorder_id()))
            if operation == "cancel":
                return jsonify(recorder.cancel_alternate_screen(recorder_id()))
        return jsonify(error="Unknown Special Case operation."), 404

    @app.post("/api/recorder/keyboard-capture/start")
    @admin_required
    def start_keyboard_capture():
        data = payload()
        if set(data) - {"name", "keys", "kind"} or not {
            "name",
            "keys",
        } <= set(data):
            raise ValueError("Keyboard name and keys are required.")
        with lock:
            return jsonify(
                recorder.start_keyboard_capture(
                    recorder_id(),
                    data["name"],
                    data["keys"],
                    data.get("kind", "numeric"),
                )
            )

    @app.post("/api/recorder/keyboard-capture/key")
    @admin_required
    def capture_keyboard_key():
        data = payload()
        if set(data) - {
            "key",
            "point",
            "camera_point",
            "coordinate_space",
            "frame_id",
        }:
            raise ValueError("Unexpected keyboard capture fields.")
        if ("point" in data) == ("camera_point" in data):
            raise ValueError("Provide exactly one keyboard capture coordinate.")
        with lock:
            point = data.get("point")
            if "camera_point" in data:
                state.require_ready()
                state.require_frame(data.get("frame_id"))
                if state.calibration is None:
                    raise ValueError(
                        "Calibrate the phone display before capturing keyboard keys."
                    )
                coordinate_space = data.get("coordinate_space", "camera_normalized")
                if coordinate_space == "screen_normalized":
                    source = data["camera_point"]
                    if not isinstance(source, (list, tuple)) or len(source) != 2:
                        raise ValueError("A keyboard key coordinate is required.")
                    point = [unit(source[0]), unit(source[1])]
                elif coordinate_space == "camera_normalized":
                    point = state.calibration.map(data["camera_point"])
                else:
                    raise ValueError("Unsupported coordinate_space.")
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("A keyboard key coordinate is required.")
            return jsonify(
                recorder.capture_keyboard_key(
                    recorder_id(), data.get("key"), point[0], point[1]
                )
            )

    @app.post("/api/recorder/keyboard-capture/save")
    @admin_required
    def save_keyboard_capture():
        if payload():
            raise ValueError("Expected an empty JSON object.")
        with lock:
            return jsonify(recorder.save_keyboard_capture(recorder_id()))

    @app.post("/api/recorder/keyboard-capture/cancel")
    @admin_required
    def cancel_keyboard_capture():
        if payload():
            raise ValueError("Expected an empty JSON object.")
        with lock:
            return jsonify(recorder.cancel_keyboard_capture(recorder_id()))

    @app.post("/api/recorder/keyboard-delete")
    @admin_required
    def delete_recording_keyboard():
        data = payload()
        if set(data) != {"keyboard_id"}:
            raise ValueError("Choose one captured keyboard to delete.")
        with lock:
            return jsonify(recorder.delete_keyboard(recorder_id(), data["keyboard_id"]))

    @app.post("/api/recorder/variable-input")
    @admin_required
    def record_variable_input():
        data = payload()
        allowed = {
            "variable",
            "input_method",
            "keyboard_id",
            "sample",
            "type_sample",
            "frame_id",
            "amount_format",
        }
        required = {"variable", "input_method", "type_sample"}
        if set(data) - allowed or not required <= set(data):
            raise ValueError("Variable, input method, and sample action are required.")
        if type(data["type_sample"]) is not bool:
            raise ValueError("type_sample must be a boolean.")
        method = data["input_method"]
        if method not in {"hid", "screen_keyboard"}:
            raise ValueError("Choose Bluetooth HID or a captured on-screen keyboard.")
        sample = data.get("sample", "")
        if not isinstance(sample, str) or len(sample) > 4096:
            raise ValueError("Default value must be text up to 4096 characters.")
        if data["type_sample"] and sample == "":
            raise ValueError("Enter a Default value before using Add & Type Now.")
        variable = (
            {**data["variable"], "default": sample or None}
            if isinstance(data["variable"], dict)
            else data["variable"]
        )

        client = client_id()
        owner = recorder_id()
        action_acquired = data["type_sample"] and state.action_lock.acquire(
            blocking=False
        )
        if data["type_sample"] and not action_acquired:
            return (
                jsonify(
                    error=(
                        "Controller busy. Wait for the current action to finish; "
                        "sample was not sent."
                    )
                ),
                409,
            )
        action_started = False
        try:
            with lock:
                recorder.require_owner(owner)
                step = {
                    "type": "input",
                    "source": "variable",
                    "variable": (
                        variable.get("name") if isinstance(variable, dict) else None
                    ),
                    **(
                        {"amount_format": data.get("amount_format", "raw")}
                        if isinstance(variable, dict)
                        and variable.get("name") == "amount"
                        else {}
                    ),
                    **(
                        {
                            "input_method": "screen_keyboard",
                            "keyboard_id": data.get("keyboard_id"),
                        }
                        if method == "screen_keyboard"
                        else {}
                    ),
                }
                candidate = recorder.prepare(owner, step, variable)
                typed_sample = (
                    format_input_value(step, sample) if data["type_sample"] else sample
                )
                keyboard = None
                if method == "screen_keyboard":
                    keyboard = recorder.workflow.get("keyboards", {}).get(
                        data.get("keyboard_id")
                    )
                    if keyboard is None:
                        raise ValueError(
                            "Input references an undefined captured keyboard."
                        )
                    # Validate the complete sample before sending its first tap.
                    try:
                        screen_keyboard_points(keyboard, typed_sample)
                    except UnsupportedOperation as exc:
                        raise ValueError(str(exc)) from None

                if not data["type_sample"]:
                    return jsonify(recorder.commit_recording(owner, candidate))

                with recording_sample_control(client, data.get("frame_id")) as device:
                    action_started = bool(sample)
                    if state.pi_bridge is not None:
                        if method == "screen_keyboard":
                            type_with_screen_keyboard(
                                state.pi_bridge,
                                time.sleep,
                                keyboard,
                                typed_sample,
                            )
                        else:
                            state.pi_bridge.type_text(typed_sample)
                    else:
                        context = AutomationContext(
                            device,
                            state.android,
                            threading.Event(),
                            logging.getLogger("recorder"),
                            SimpleNamespace(),
                            input_backend=(
                                "mock" if backend_names()[0] == "mock" else "appium"
                            ),
                        )
                        if method == "screen_keyboard":
                            type_with_screen_keyboard(
                                context.input,
                                context.wait,
                                keyboard,
                                typed_sample,
                            )
                        else:
                            context.input.type_text(typed_sample)
                    with state.lock:
                        if state.owner == client:
                            state.renew_lease()
                return jsonify(recorder.commit_recording(owner, candidate))
        except Exception:
            if action_started:
                with lock:
                    recorder.fail_recording()
                    state.disable(
                        "Recording stopped after uncertain input; no retry attempted."
                    )
            raise
        finally:
            if action_acquired:
                state.action_lock.release()

    @app.post("/api/recorder/review")
    @admin_required
    def review_recording():
        data = payload()

        with lock:
            return jsonify(
                recorder.review(
                    recorder_id(),
                    data.get("operation"),
                    data,
                )
            )

    @app.post("/api/recorder/step")
    @admin_required
    def record_step():
        data = payload()
        client = client_id()

        if set(data) - {
            "step",
            "variable",
            "frame_id",
            "camera_start",
            "camera_end",
            "coordinate_space",
            "capture",
        }:
            raise ValueError("Unexpected recorded " "step fields.")

        with lock:
            recorder.require_owner(recorder_id())

            step = data.get("step")

            if not isinstance(
                step,
                dict,
            ):
                raise ValueError("A step object " "is required.")

            step = dict(step)

            if "camera_start" in data:
                state.require_ready()

                state.require_frame(data.get("frame_id"))

                if state.calibration is None:
                    raise ValueError(
                        "Calibrate the phone "
                        "display before ri thinkecording "
                        "preview gestures."
                    )

                coordinate_space = data.get("coordinate_space", "camera_normalized")

                def recorded_point(value):
                    if coordinate_space == "screen_normalized":
                        if not isinstance(value, (list, tuple)) or len(value) != 2:
                            raise ValueError("A normalized coordinate is required.")
                        return [unit(value[0]), unit(value[1])]
                    if coordinate_space == "camera_normalized":
                        return state.calibration.map(value)
                    raise ValueError("Unsupported coordinate_space.")

                start = recorded_point(data["camera_start"])

                if step.get("type") == "tap":
                    step.update(
                        x=start[0],
                        y=start[1],
                    )

                elif step.get("type") == "swipe":
                    step.update(
                        start=start,
                        end=recorded_point(data.get("camera_end")),
                    )

                else:
                    raise ValueError(
                        "Camera coordinates " "require a tap or " "swipe step."
                    )

            if step.get("type") in {
                "tap",
                "swipe",
                "key",
                "input",
            }:
                screenshot = recording_screenshot()

                if screenshot:
                    step["recording_screenshot"] = screenshot

            capture = data.get(
                "capture",
                False,
            )

            if type(capture) is not bool:
                raise ValueError("Capture flag must " "be a boolean.")

            if capture and (
                step.get("type")
                not in {
                    "tap",
                    "swipe",
                    "input",
                    "key",
                }
                or (step.get("type") == "input" and step.get("source") != "fixed")
            ):
                raise ValueError(
                    "Only performed actions " "can be captured while " "recording."
                )

            candidate = recorder.prepare(
                recorder_id(),
                step,
                data.get("variable"),
            )

            target_key = None
            notice = None

            execute = recorder.execute and (
                capture
                or step.get("type")
                in {
                    "tap",
                    "swipe",
                    "key",
                }
            )

            if state.pi_bridge is not None and (capture or execute):
                raise ValueError(
                    "Use the phone preview " "and manual controls " "to record actions."
                )

            if capture or execute:
                if not (state.action_lock.acquire(blocking=False)):
                    raise ValueError(
                        "Controller busy. " "Step was not recorded " "or sent."
                    )

                try:
                    with recording_control(
                        client,
                        data.get("frame_id"),
                    ) as device:
                        context = AutomationContext(
                            device,
                            state.android,
                            threading.Event(),
                            logging.getLogger("recorder"),
                            SimpleNamespace(),
                            input_backend=(
                                "mock" if (backend_names()[0] == "mock") else "appium"
                            ),
                        )

                        if capture:
                            target = inspect_target(
                                context,
                                step,
                            )

                            typing = step["type"] == "input" or (
                                step["type"] == "key"
                                and step["key"].upper()
                                not in {
                                    "HOME",
                                    "BACK",
                                    "SCREEN_ON",
                                    "SCREEN_OFF",
                                    "RECENTS",
                                }
                            )

                            if (
                                target["secure"]
                                or not target["known"]
                                or (typing and not target["safe_input"])
                            ):
                                step = {
                                    "type": ("manual_checkpoint"),
                                    "label": ("Secure input"),
                                    "message": (
                                        "Complete secure or "
                                        "unrecognized input "
                                        "manually on the phone."
                                    ),
                                }

                                execute = False

                                notice = (
                                    "Complete this part "
                                    "directly on the phone. "
                                    "A manual checkpoint "
                                    "was recorded; no input "
                                    "value was saved."
                                )

                            else:
                                if target["label"]:
                                    step["label"] = target["label"]

                                if typing:
                                    target_key = target["target"]

                                if step["type"] == "tap":
                                    preview = tap_preview(
                                        context,
                                        step,
                                        target,
                                    )

                                    if preview:
                                        step["preview"] = preview

                            candidate = recorder.prepare_capture(
                                recorder_id(),
                                step,
                                target_key,
                            )

                        if execute:
                            # Physical action is sent exactly once.
                            WorkflowRunner(
                                context,
                                external_screen,
                                screen_state_provider,
                            ).execute_step(
                                step,
                                {},
                            )

                        with state.lock:
                            if (
                                context.input_backend != "mock"
                                and state.owner == client
                            ):
                                state.renew_lease()

                except Exception:
                    recorder.fail_recording()

                    state.disable(
                        "Recording stopped after "
                        "uncertain input; no retry "
                        "attempted."
                    )

                    raise

                finally:
                    state.action_lock.release()

            result = recorder.commit_recording(
                recorder_id(),
                candidate,
                target_key,
            )

            if notice:
                result["notice"] = notice

            return jsonify(result)

    @app.post("/api/recorder/<operation>")
    @admin_required
    def recording_operation(operation):
        data = payload()
        client = client_id()

        if data:
            raise ValueError("Expected an empty " "JSON object.")

        with lock:
            if operation == "undo":
                return jsonify(recorder.undo(recorder_id()))

            if operation == "stop":
                return jsonify(recorder.stop(recorder_id()))

            if operation == "cancel":
                recorder.cancel(recorder_id())

                return jsonify(
                    active=False,
                    workflow=None,
                )

            if operation == "save":
                recorder.require_owner(recorder_id())

                if recorder.active:
                    raise ValueError("Stop recording " "before saving.")

                editing = recorder.editing_existing

                if editing:
                    existing = storage.read(recorder.workflow["id"])
                    ensure_purpose_change_is_unassigned(existing, recorder.workflow)

                result = storage.save(
                    recorder.workflow,
                    create=not editing,
                )

                if editing:
                    recorder.workflow = result

                    result = recorder.snapshot()["workflow"]

                recorder.cancel(recorder_id())

                return (
                    jsonify(result),
                    (200 if editing else 201),
                )

            return (
                jsonify(error=("Unknown recorder " "operation.")),
                404,
            )
