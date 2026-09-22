"""Workflow routes share the dashboard's local token, ownership and health gates."""

import logging
import os
import threading
from contextlib import contextmanager
from types import SimpleNamespace

from flask import jsonify, request

from ..backends import backend_names, select_device
from ..context import AutomationContext
from ..screen.accessibility import AccessibilityScreenStateProvider
from ..screen.camera import CalibratedCamera
from ..screen.camera_ocr import CameraOCRScreenStateProvider
from ..screen.text_learning import text_suggestions
from .capture import inspect_target, tap_preview
from .recorder import Recorder
from .runner import WorkflowRunner
from .service import WorkflowService
from .storage import WorkflowStorage


def register_workflows(app, state, payload, client_id, storage=None):
    storage = storage or WorkflowStorage()
    recorder = Recorder()
    lock = state.lock

    external_screen = CalibratedCamera(state)

    accessibility_url = os.environ.get(
        "PHONE_ACCESSIBILITY_URL",
        "",
    ).strip()

    if accessibility_url:
        screen_state_provider = AccessibilityScreenStateProvider(accessibility_url)
    else:
        screen_state_provider = CameraOCRScreenStateProvider(external_screen)

    service = WorkflowService(
        state.android,
        state.device_locks,
        external_screen,
        screen_state_provider,
    )

    app.extensions.update(
        workflow_storage=storage,
        workflow_recorder=recorder,
        workflow_service=service,
    )

    @app.get("/api/ocr/texts")
    def debug_ocr_texts():
        texts = screen_state_provider.visible_texts()

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

    def record_manual(step, perform):
        # The caller already holds the manual ownership/frame/action gates.
        with lock:
            if not recorder.active:
                perform()
                return None

            owner = recorder_id()

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

    def run_snapshot(run_id):
        try:
            return service.snapshot(run_id)

        except KeyError:
            return None

    # =====================================================================
    # Workflow routes
    # =====================================================================

    @app.get("/api/workflows")
    def list_workflows():
        return jsonify(
            workflows=storage.list(),
            input_backend=backend_names()[0],
            screen_backend=backend_names()[1],
        )

    @app.post("/api/workflows")
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

        if request.method == "DELETE":
            storage.delete(workflow_id)

            return jsonify(deleted=True)

        data = payload()

        if data.get("id") != workflow_id:
            raise ValueError("Workflow ID must match URL.")

        storage.read(workflow_id)

        return jsonify(storage.save(data))

    @app.post("/api/workflows/<workflow_id>/run")
    def run_workflow(workflow_id):
        data = payload()
        client = client_id()

        if set(data) - {"variables"}:
            raise ValueError("Expected variables only.")

        with lock:
            if recorder.workflow is not None:
                raise ValueError("Save or cancel recording " "before playback.")

            with state.lock:
                if state.owner and state.owner != client:
                    raise ValueError("Another browser owns " "manual control.")

                state.disable("Workflow starting. " "Manual control disabled.")

            return (
                jsonify(
                    service.start(
                        storage.read(workflow_id),
                        data.get("variables"),
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
    def start_recording():
        data = payload()
        client = client_id()

        if set(data) - {
            "name",
            "execute",
            "financial",
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
                )
            )

    @app.post("/api/recorder/edit")
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

    @app.post("/api/recorder/text-suggestions")
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

    @app.post("/api/recorder/mark-next-screen")
    def mark_next_screen():
        data = payload()

        if data:
            raise ValueError("Expected an empty JSON object.")

        with lock:
            return jsonify(recorder.toggle_mark_next_screen(recorder_id()))

    @app.post("/api/recorder/expected-text")
    def add_recording_expected_text():
        data = payload()

        if set(data) - {"text", "timeout_seconds"}:
            raise ValueError("Unexpected expected-text fields.")

        with lock:
            return jsonify(
                recorder.add_pending_expected_text(
                    recorder_id(),
                    data.get("text"),
                    data.get("timeout_seconds", 10),
                )
            )

    @app.post("/api/recorder/expected-text/cancel")
    def cancel_recording_expected_text():
        data = payload()

        if data:
            raise ValueError("Expected an empty JSON object.")

        with lock:
            return jsonify(recorder.cancel_expected_text(recorder_id()))

    @app.post("/api/recorder/review")
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
    def record_step():
        data = payload()
        client = client_id()

        if set(data) - {
            "step",
            "variable",
            "frame_id",
            "camera_start",
            "camera_end",
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
                        "display before recording "
                        "preview gestures."
                    )

                start = state.calibration.map(data["camera_start"])

                if step.get("type") == "tap":
                    step.update(
                        x=start[0],
                        y=start[1],
                    )

                elif step.get("type") == "swipe":
                    step.update(
                        start=start,
                        end=(state.calibration.map(data.get("camera_end"))),
                    )

                else:
                    raise ValueError(
                        "Camera coordinates " "require a tap or " "swipe step."
                    )

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
                    storage.read(recorder.workflow["id"])

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
