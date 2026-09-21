"""One browser-owned recording draft; independent of runner state."""

from copy import deepcopy
import uuid
from .models import new_workflow, now
from .validation import validate_workflow, SENSITIVE


class Recorder:
    def __init__(self):
        self.owner = None
        self.workflow = None
        self.active = False
        self.execute = False
        self.input_target = None
        self.editing_existing = False

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

        return self.snapshot()

    def prepare(self, owner, step, variable=None):
        self.require_owner(owner)

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
        return self.snapshot()

    def cancel(self, owner):
        self.require_owner(owner)
        self.owner, self.workflow, self.active, self.execute = None, None, False, False
        self.editing_existing = False
        self.input_target = None

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

    def prepare_manual(self, owner, step):
        """Group acknowledged printable input only; keys remain explicit steps."""
        step = deepcopy(step)
        step["recorded_at"] = now()
        self.require_owner(owner)
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
            if operation == "delete":
                candidate["steps"].pop(index)
            elif operation == "fields":
                allowed = {
                    "tap": {"x", "y"},
                    "swipe": {"start", "end", "duration_ms"},
                    "key": {"key"},
                    "wait": {"seconds"},
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
                    and SENSITIVE.search(label)
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
                            if k not in {"source", "variable", "message", "value"}
                        },
                        "id": old["id"],
                        "type": "input",
                        "source": "fixed",
                        "value": new_value,
                        "label": old.get("label", ""),
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
                        }
                else:
                    raise ValueError("Choose fixed, variable or manual input.")
            else:
                raise ValueError("Unknown review operation.")

        used = {s.get("variable") for s in candidate["steps"]}

        candidate["variables"] = [
            v for v in candidate["variables"] if v["name"] in used
        ]
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
        }
