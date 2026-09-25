"""One browser-owned recording draft; independent of runner state."""

from copy import deepcopy
import re
import uuid

from .models import new_workflow, now
from ..coordinates import unit
from .validation import (
    PROTECTED_SCREEN_INPUT,
    SENSITIVE,
    normalize_capture_keyboard_labels,
    validate_keyboard_map,
    validate_workflow,
)


class Recorder:
    def __init__(self):
        self.owner = None
        self.workflow = None
        self.active = False
        self.execute = False
        self.input_target = None
        self.editing_existing = False
        self.mark_next_screen = False
        self.pending_expected_text = None
        self.keyboard_capture = None

    def _clear_annotation_state(self):
        self.mark_next_screen = False
        self.pending_expected_text = None
        self.keyboard_capture = None

    def _require_not_annotating(self):
        if self.pending_expected_text is not None:
            raise ValueError("Finish or cancel the expected-text check first.")
        if self.keyboard_capture is not None:
            raise ValueError("Finish or cancel keyboard capture first.")

    def start_keyboard_capture(self, owner, name, keys, kind="numeric"):
        self.require_owner(owner)
        self._require_not_annotating()
        previous = self.workflow["steps"][-1] if self.workflow["steps"] else None
        if previous and (
            previous["type"] == "manual_checkpoint"
            or SENSITIVE.search(previous.get("label") or "")
            or PROTECTED_SCREEN_INPUT.search(previous.get("label") or "")
        ):
            raise ValueError(
                "Keyboard capture is unavailable after a secure or manual checkpoint."
            )
        keys = normalize_capture_keyboard_labels(kind, keys)
        existing_names = {
            keyboard["name"].strip().casefold()
            for keyboard in self.workflow.get("keyboards", {}).values()
        }
        keyboard = {
            "id": "keyboard_" + uuid.uuid4().hex,
            "name": name,
            "kind": kind,
            "keys": {},
        }
        # Validate the metadata with one temporary point, then keep the draft empty.
        validate_keyboard_map(
            keyboard["id"], {**keyboard, "keys": {keys[0]: {"x": 0, "y": 0}}}
        )
        keyboard["name"] = keyboard["name"].strip()
        if keyboard["name"].casefold() in existing_names:
            raise ValueError("A captured keyboard already uses this name.")
        self.mark_next_screen = False
        self.keyboard_capture = {**keyboard, "requested_keys": list(keys)}
        return self.snapshot()

    def capture_keyboard_key(self, owner, label, x, y):
        self.require_owner(owner)
        if self.keyboard_capture is None:
            raise ValueError("No keyboard capture is active.")
        if label not in self.keyboard_capture["requested_keys"]:
            raise ValueError("Choose a requested keyboard key.")
        self.keyboard_capture["keys"][label] = {"x": unit(x), "y": unit(y)}
        return self.snapshot()

    def save_keyboard_capture(self, owner):
        self.require_owner(owner)
        if self.keyboard_capture is None:
            raise ValueError("No keyboard capture is active.")
        capture = deepcopy(self.keyboard_capture)
        if set(capture["keys"]) != set(capture["requested_keys"]):
            raise ValueError("Capture every selected key before saving the keyboard.")
        capture.pop("requested_keys")
        validate_keyboard_map(capture["id"], capture)
        candidate = deepcopy(self.workflow)
        candidate.setdefault("keyboards", {})[capture["id"]] = capture
        check = deepcopy(candidate)
        check["financial"] = False
        validate_workflow(check, allow_empty=True)
        self.workflow = candidate
        self.keyboard_capture = None
        return self.snapshot()

    def cancel_keyboard_capture(self, owner):
        self.require_owner(owner)
        if self.keyboard_capture is None:
            raise ValueError("No keyboard capture is active.")
        self.keyboard_capture = None
        return self.snapshot()

    def delete_keyboard(self, owner, keyboard_id):
        self.require_owner(owner)
        self._require_not_annotating()
        keyboards = self.workflow.get("keyboards", {})
        if keyboard_id not in keyboards:
            raise ValueError("Captured keyboard not found.")
        references = [
            step
            for step in self.workflow["steps"]
            if step.get("input_method") == "screen_keyboard"
            and step.get("keyboard_id") == keyboard_id
        ]
        if references:
            raise ValueError(
                "This keyboard is used by an input step. Update that step before deleting it."
            )
        candidate = deepcopy(self.workflow)
        del candidate["keyboards"][keyboard_id]
        check = deepcopy(candidate)
        check["financial"] = False
        validate_workflow(check, allow_empty=True)
        self.workflow = candidate
        return self.snapshot()

    def require_owner(self, owner):
        if self.owner != owner or self.workflow is None:
            raise ValueError("No recording belongs to this browser.")

    def start(self, owner, name, execute=False, financial=False):
        if self.workflow is not None:
            raise ValueError("Save or cancel the current recording first.")

        if type(execute) is not bool or type(financial) is not bool:
            raise ValueError("Recording options must be booleans.")

        workflow = new_workflow(name)
        workflow["financial"] = financial
        validate_workflow(workflow, allow_empty=True)

        self.owner, self.workflow, self.active, self.execute = (
            owner,
            workflow,
            True,
            execute,
        )

        self.input_target = None
        self.editing_existing = False
        self._clear_annotation_state()

        return self.snapshot()

    def edit(self, owner, workflow):
        if self.workflow is not None:
            raise ValueError("Save or cancel the current workflow first.")

        candidate = deepcopy(workflow)
        validate_workflow(candidate)

        self.workflow = candidate
        self.owner = owner
        self.active = False
        self.execute = False
        self.input_target = None
        self.editing_existing = True
        self._clear_annotation_state()

        return self.snapshot()

    def prepare(self, owner, step, variable=None):
        self.require_owner(owner)
        self._require_not_annotating()

        if not self.active:
            raise ValueError("Recording is stopped.")

        candidate = deepcopy(self.workflow)
        step = deepcopy(step)
        step["id"] = "step_" + uuid.uuid4().hex

        if variable is not None:
            if not isinstance(variable, dict):
                raise ValueError("A variable definition must be an object.")
            old = next(
                (
                    v
                    for v in candidate["variables"]
                    if v["name"] == variable.get("name")
                ),
                None,
            )

            if old is not None and old != variable:
                raise ValueError("Variable already exists with another definition.")

            if old is None:
                candidate["variables"].append(variable)

        candidate["steps"].append(step)
        # A financial draft may be incomplete until Save; validate other fields now.
        check = deepcopy(candidate)
        check["financial"] = False
        validate_workflow(check)
        return candidate

    def undo(self, owner):
        self.require_owner(owner)
        self._require_not_annotating()
        self.input_target = None

        if self.workflow["steps"]:
            self.workflow["steps"].pop()

        used = {s.get("variable") for s in self.workflow["steps"]}
        self.workflow["variables"] = [
            v for v in self.workflow["variables"] if v["name"] in used
        ]
        return self.snapshot()

    def stop(self, owner):
        self.require_owner(owner)
        self.active = False
        self._clear_annotation_state()
        return self.snapshot()

    def cancel(self, owner):
        self.require_owner(owner)
        self.owner, self.workflow, self.active, self.execute = None, None, False, False
        self.editing_existing = False
        self.input_target = None
        self._clear_annotation_state()

    def fail_recording(self):
        self.active = False
        self.input_target = None
        self._clear_annotation_state()

    def prepare_capture(self, owner, step, target=None):
        """Merge consecutive chunks from the same focused text field."""
        step = deepcopy(step)
        step["recorded_at"] = now()
        candidate = self.prepare(owner, step)
        previous = self.workflow["steps"][-1] if self.workflow["steps"] else None
        if previous and target is not None and target == self.input_target:
            if previous["type"] == "input" and previous.get("source") == "fixed":
                if step["type"] == "input" and step.get("source") == "fixed":
                    candidate["steps"][-2]["value"] += step["value"]
                    candidate["steps"].pop()
                elif (
                    step["type"] == "key" and step.get("key", "").upper() == "BACKSPACE"
                ):
                    if previous["value"]:
                        candidate["steps"][-2]["value"] = previous["value"][:-1]
                        candidate["steps"].pop()
        if (
            previous
            and step["type"] == "manual_checkpoint"
            and previous.get("label") == "Secure input"
        ):
            candidate["steps"].pop()
        check = deepcopy(candidate)
        check["financial"] = False
        validate_workflow(check)
        return candidate

    def require_expected_text_action(self, step_id):
        step = next((s for s in self.workflow["steps"] if s["id"] == step_id), None)
        if not step:
            raise ValueError("Recorded step not found.")
        navigation = step["type"] in {"tap", "swipe"} or (
            step["type"] == "key" and step.get("key", "").upper() in {"HOME", "BACK"}
        )
        label = step.get("label") or ""
        sensitive = SENSITIVE.search(label) or re.search(
            r"authoriz|authoris|confirm.*(?:pay|transfer)|final.*(?:pay|transfer)",
            label,
            re.I,
        )
        if not navigation or sensitive:
            raise ValueError("Expected text requires a navigation action.")
        return step

    def commit_recording(self, owner, candidate, input_target=None):
        """Commit a successful action and expose an armed navigation target."""
        self.require_owner(owner)
        self.workflow = candidate
        self.input_target = input_target

        if self.mark_next_screen and candidate["steps"]:
            step = candidate["steps"][-1]

            try:
                self.require_expected_text_action(step["id"])
            except ValueError:
                pass
            else:
                self.mark_next_screen = False
                self.pending_expected_text = step["id"]

        return self.snapshot()

    def toggle_mark_next_screen(self, owner):
        self.require_owner(owner)

        if not self.active:
            raise ValueError("Recording is stopped.")

        self._require_not_annotating()
        self.mark_next_screen = not self.mark_next_screen
        return self.snapshot()

    def begin_current_screen_annotation(self, owner):
        self.require_owner(owner)

        if not self.active:
            raise ValueError("Recording is stopped.")

        if self.pending_expected_text == "current":
            return self.workflow

        self._require_not_annotating()

        previous = self.workflow["steps"][-1] if self.workflow["steps"] else None

        if previous and (
            previous["type"] == "manual_checkpoint"
            or SENSITIVE.search(previous.get("label") or "")
        ):
            raise ValueError(
                "Expected text is unavailable after a secure or manual checkpoint."
            )

        self.mark_next_screen = False
        self.pending_expected_text = "current"
        return self.workflow

    def authorize_text_suggestions(self, owner, step_id=None, current=False):
        self.require_owner(owner)

        if current:
            return self.begin_current_screen_annotation(owner)

        self.require_expected_text_action(step_id)

        if self.active and self.pending_expected_text not in {None, step_id}:
            raise ValueError("Expected-text target changed. Cancel and try again.")

        return self.workflow

    def cancel_expected_text(self, owner):
        self.require_owner(owner)
        self._clear_annotation_state()
        return self.snapshot()

    def add_pending_expected_text(self, owner, text, timeout_seconds=10):
        self.require_owner(owner)

        if not self.active or self.pending_expected_text is None:
            raise ValueError("No recording-time expected-text check is open.")

        candidate = deepcopy(self.workflow)
        target = self.pending_expected_text
        step = {
            "id": "step_" + uuid.uuid4().hex,
            "type": "wait_for_text",
            "text": text,
            "timeout_seconds": timeout_seconds,
        }

        if target == "current":
            candidate["steps"].append(step)
        else:
            index = next(
                (
                    index
                    for index, item in enumerate(candidate["steps"])
                    if item["id"] == target
                ),
                None,
            )

            if index is None:
                raise ValueError("Recorded action is no longer available.")

            self.require_expected_text_action(target)
            candidate["steps"].insert(index + 1, step)

        for item in candidate["steps"]:
            if item["type"] == "wait_for_text" and isinstance(item.get("text"), str):
                item["text"] = item["text"].strip()

        check = deepcopy(candidate)
        check["financial"] = False
        validate_workflow(check, allow_empty=True)
        self.workflow = candidate
        self._clear_annotation_state()
        return self.snapshot()

    def prepare_manual(self, owner, step):
        """Group acknowledged printable input only; keys remain explicit steps."""
        step = deepcopy(step)
        step["recorded_at"] = now()
        self.require_owner(owner)
        self._require_not_annotating()
        previous = self.workflow["steps"][-1] if self.workflow["steps"] else None
        if (
            self.active
            and self.input_target == "manual-text"
            and previous
            and previous["type"] == "input"
            and previous.get("source") == "fixed"
            and step["type"] == "input"
            and step.get("source") == "fixed"
            and len(previous["value"]) + len(step["value"]) <= 4096
        ):
            candidate = deepcopy(self.workflow)
            candidate["steps"][-1]["value"] += step["value"]
            check = deepcopy(candidate)
            check["financial"] = False
            validate_workflow(check)
            return candidate
        return self.prepare(owner, step)

    def review(self, owner, operation, data):
        self.require_owner(owner)
        if self.active:
            raise ValueError("Stop recording before editing the flow.")
        candidate = deepcopy(self.workflow)

        if operation == "rename_flow":
            candidate["name"] = data.get("name")
            if "financial" in data:
                candidate["financial"] = data["financial"]

        elif operation == "insert":
            step = deepcopy(data.get("step"))
            if not isinstance(step, dict) or step.get("type") not in {
                "tap",
                "swipe",
                "input",
                "key",
                "wait",
                "wait_until_stable",
                "wait_for_text",
                "manual_checkpoint",
            }:
                raise ValueError("Choose a supported step type.")
            index = data.get("index", len(candidate["steps"]))

            if type(index) is not int or not 0 <= index <= len(candidate["steps"]):
                raise ValueError("Invalid insertion position.")
            step["id"] = "step_" + uuid.uuid4().hex
            candidate["steps"].insert(index, step)
        else:
            index = next(
                (
                    i
                    for i, step in enumerate(candidate["steps"])
                    if step["id"] == data.get("step_id")
                ),
                None,
            )

            if index is None:
                raise ValueError("Recorded step not found.")
            old = candidate["steps"][index]
            if operation in {"expected_text", "remove_expected_text"}:
                self.require_expected_text_action(old["id"])
                following = candidate["steps"][index + 1 : index + 2]
                wait = (
                    following[0]
                    if following and following[0]["type"] == "wait_for_text"
                    else None
                )
                if operation == "remove_expected_text":
                    if wait is not None:
                        candidate["steps"].pop(index + 1)
                else:
                    fields = {
                        "text": data.get("text"),
                        "timeout_seconds": data.get("timeout_seconds", 10),
                    }
                    if wait is not None:
                        wait.update(fields)
                    else:
                        candidate["steps"].insert(
                            index + 1,
                            {
                                "id": "step_" + uuid.uuid4().hex,
                                "type": "wait_for_text",
                                **fields,
                            },
                        )
            elif operation == "delete":
                candidate["steps"].pop(index)
            elif operation == "fields":
                allowed = {
                    "tap": {"x", "y"},
                    "swipe": {"start", "end", "duration_ms"},
                    "key": {"key"},
                    "wait": {"seconds"},
                    "wait_until_stable": {"stable_ms", "timeout_seconds"},
                    "wait_for_text": {"text", "timeout_seconds"},
                    "manual_checkpoint": {"message"},
                }.get(old["type"], set())
                changes = data.get("fields")
                if not isinstance(changes, dict) or set(changes) - allowed:
                    raise ValueError("Unsupported fields for this step.")
                old.update(deepcopy(changes))
            elif operation == "wait":
                if old["type"] != "wait":
                    raise ValueError("Choose a wait step to edit its duration.")
                old["seconds"] = data.get("seconds")
            elif operation == "rename_step":
                label = data.get("label")
                if (
                    isinstance(label, str)
                    and (
                        SENSITIVE.search(label)
                        or (
                            old["type"] == "input"
                            and old.get("input_method") == "screen_keyboard"
                            and PROTECTED_SCREEN_INPUT.search(label)
                        )
                    )
                    and old["type"] != "manual_checkpoint"
                ):
                    candidate["steps"][index] = {
                        "id": old["id"],
                        "type": "manual_checkpoint",
                        "label": label,
                        "message": "Complete secure authentication manually.",
                    }
                else:
                    old["label"] = label
            elif operation == "move":
                destination = data.get("index")
                if type(destination) is not int or not 0 <= destination < len(
                    candidate["steps"]
                ):
                    raise ValueError("Invalid step position.")
                candidate["steps"].insert(destination, candidate["steps"].pop(index))
            elif operation == "input":
                source = data.get("source")
                input_method = data.get("input_method", "hid")
                method_fields = (
                    {
                        "input_method": "screen_keyboard",
                        "keyboard_id": data.get("keyboard_id"),
                    }
                    if input_method == "screen_keyboard"
                    else {}
                )
                if source == "manual":
                    candidate["steps"][index] = {
                        "id": old["id"],
                        "type": "manual_checkpoint",
                        "label": "Manual input",
                        "message": "Complete this step manually.",
                    }
                elif old["type"] != "input":
                    raise ValueError(
                        "Only recorded text inputs can become fixed or variable."
                    )
                elif source == "fixed":
                    new_value = data.get("value")

                    if (
                        self.editing_existing
                        and old.get("source") == "fixed"
                        and new_value in {None, "", "(hidden)"}
                    ):
                        new_value = old.get("value", "")

                    candidate["steps"][index] = {
                        **{
                            k: v
                            for k, v in old.items()
                            if k
                            not in {
                                "source",
                                "variable",
                                "message",
                                "value",
                                "input_method",
                                "keyboard_id",
                            }
                        },
                        "id": old["id"],
                        "type": "input",
                        "source": "fixed",
                        "value": new_value,
                        "label": old.get("label", ""),
                        **method_fields,
                    }
                elif source == "variable":
                    variable = deepcopy(data.get("variable"))

                    if not isinstance(variable, dict):
                        raise ValueError("Variable definition is required.")

                    sensitive_label = (
                        str(variable.get("name", ""))
                        + " "
                        + str(variable.get("label", ""))
                    )

                    if variable.get(
                        "sensitive", False
                    ) is not False or SENSITIVE.search(sensitive_label):
                        candidate["steps"][index] = {
                            "id": old["id"],
                            "type": "manual_checkpoint",
                            "label": "Secure input",
                            "message": "Complete secure authentication manually.",
                        }
                    else:
                        existing = next(
                            (
                                v
                                for v in candidate["variables"]
                                if v["name"] == variable.get("name")
                            ),
                            None,
                        )
                        if existing:
                            existing.update(variable)
                        else:
                            candidate["variables"].append(variable)
                        # Remove the captured value and preview on conversion.
                        candidate["steps"][index] = {
                            "id": old["id"],
                            "type": "input",
                            "source": "variable",
                            "variable": variable.get("name"),
                            "label": old.get("label", ""),
                            **method_fields,
                        }
                else:
                    raise ValueError("Choose fixed, variable or manual input.")
            else:
                raise ValueError("Unknown review operation.")

        used = {s.get("variable") for s in candidate["steps"]}

        candidate["variables"] = [
            v for v in candidate["variables"] if v["name"] in used
        ]
        for step in candidate["steps"]:
            if step["type"] == "wait_for_text" and isinstance(step.get("text"), str):
                step["text"] = step["text"].strip()
        check = deepcopy(candidate)
        check["financial"] = False
        validate_workflow(check, allow_empty=True)

        if type(candidate.get("financial", False)) is not bool:
            raise ValueError("Financial flow must be a boolean.")
        self.workflow = candidate
        return self.snapshot()

    def snapshot(self):
        workflow = deepcopy(self.workflow)
        if self.editing_existing and workflow:
            for step in workflow["steps"]:
                if step["type"] == "input" and step.get("source") == "fixed":
                    step["value"] = "(hidden)"
                step.pop("preview", None)
            for variable in workflow["variables"]:
                variable["default"] = None
        return {
            "workflow": workflow,
            "editing_existing": self.editing_existing,
            "active": self.active,
            "execute": self.execute,
            "mark_next_screen": self.mark_next_screen,
            "expected_text_pending": self.pending_expected_text is not None,
            "pending_expected_text_step_id": (
                self.pending_expected_text
                if self.pending_expected_text not in {None, "current"}
                else None
            ),
            "keyboard_capture": deepcopy(self.keyboard_capture),
        }
