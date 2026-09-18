"""Validate the whole document and resolve variables before any phone input."""

import base64
import binascii
import math
import re
from copy import deepcopy
from ..coordinates import unit
from .models import STEP_TYPES, VARIABLE_TYPES

IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}")
SENSITIVE = re.compile(
    r"(?:^|[^a-z])(?:pin|otp)(?:$|[^a-z])|password|passwd|passcode|secret|biometric|approval_code",
    re.I,
)
KEYS = {
    "ENTER",
    "BACKSPACE",
    "TAB",
    "HOME",
    "BACK",
    "RECENTS",
    "SCREEN_ON",
    "SCREEN_OFF",
}


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(
            "Identifiers must start with a letter and contain only letters, digits, _ or - (max 80)."
        )
    return value


def text(value, label="Text", maximum=4096):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{label} must be a string of at most {maximum} characters.")
    return value


def fields(obj, allowed, required):
    if not isinstance(obj, dict) or set(obj) - set(allowed) or set(required) - set(obj):
        raise ValueError("Missing or unsupported workflow fields.")


def number(value, minimum, maximum):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError("Numeric value is outside the supported range.")


def point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("A coordinate pair is required.")
    for v in value:
        unit(v)


def validate_variable_value(definition, value):
    if value is None or value == "":
        if definition.get("required", False):
            raise ValueError("A required workflow variable is missing.")
        return ""
    if definition["type"] == "text":
        return text(value)
    number(value, -1e15, 1e15)
    return str(value)


def validate_workflow(workflow, *, allow_empty=False):
    fields(
        workflow,
        {
            "id",
            "name",
            "version",
            "variables",
            "steps",
            "created_at",
            "updated_at",
            "financial",
        },
        {"id", "name", "version", "variables", "steps"},
    )
    identifier(workflow["id"])
    if not text(workflow["name"], "Name", 160).strip():
        raise ValueError("Workflow name is required.")
    if type(workflow["version"]) is not int or workflow["version"] != 1:
        raise ValueError("Unsupported workflow version.")
    if type(workflow.get("financial", False)) is not bool:
        raise ValueError("financial must be a boolean.")
    for field in ("created_at", "updated_at"):
        if field in workflow:
            text(workflow[field], "Timestamp", 80)
    variables = workflow["variables"]
    steps = workflow["steps"]

    if not isinstance(variables, list) or len(variables) > 100:
        raise ValueError("At most 100 variable definitions are supported.")
    if (
        not isinstance(steps, list)
        or len(steps) > 1000
        or (not steps and not allow_empty)
    ):
        raise ValueError("A workflow must have 1–1000 steps.")

    names = set()
    for definition in variables:
        fields(
            definition,
            {
                "name",
                "label",
                "type",
                "required",
                "default",
                "sensitive",
                "ask_when_running",
            },
            {"name", "label", "type", "required", "default"},
        )
        name = identifier(definition["name"])
        text(definition["label"], "Variable label", 160)

        if name in names:
            raise ValueError("Duplicate variable name.")
        names.add(name)

        if definition.get("sensitive", False) is not False or SENSITIVE.search(
            name + " " + definition["label"]
        ):
            raise ValueError("Sensitive input must use a manual checkpoint.")
        if (
            not isinstance(definition["type"], str)
            or definition["type"] not in VARIABLE_TYPES
            or type(definition["required"]) is not bool
        ):
            raise ValueError("Unsupported variable type or required flag.")
        if type(definition.get("ask_when_running", True)) is not bool:
            raise ValueError("Ask When Running must be a boolean.")
        if not definition.get("ask_when_running", True):
            validate_variable_value(definition, definition["default"])
        if definition["default"] is not None:
            validate_variable_value(definition, definition["default"])

    ids = set()

    for step in steps:
        if (
            not isinstance(step, dict)
            or not isinstance(step.get("type"), str)
            or step["type"] not in STEP_TYPES
        ):
            raise ValueError("Unsupported workflow step type.")

        kind = step["type"]
        specific = {
            "tap": {"x", "y"},
            "swipe": {"start", "end", "duration_ms"},
            "input": {"source", "value", "variable", "message", "sensitive"},
            "key": {"key"},
            "wait": {"seconds"},
            "manual_checkpoint": {"message"},
        }[kind]

        fields(
            step,
            {"id", "type", "label", "preview", "recorded_at"} | specific,
            {"id", "type"},
        )

        if "label" in step:
            text(step["label"], "Step name", 160)
        if "recorded_at" in step:
            text(step["recorded_at"], "Recorded timestamp", 80)
        if "preview" in step:
            encoded = text(step["preview"], "Preview", 80000)
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("Invalid PNG preview.") from None
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Invalid PNG preview.")

        sid = identifier(step["id"])

        if sid in ids:
            raise ValueError("Duplicate step ID.")
        ids.add(sid)
        if kind == "tap":
            unit(step.get("x"))
            unit(step.get("y"))
        elif kind == "swipe":
            point(step.get("start"))
            point(step.get("end"))
            duration = step.get("duration_ms", 300)
            if type(duration) is not int:
                raise ValueError("Swipe duration must be an integer.")
            number(duration, 1, 60000)
        elif kind == "wait":
            number(step.get("seconds"), 0, 3600)
        elif kind == "key":
            if not isinstance(step.get("key"), str) or step["key"].upper() not in KEYS:
                raise ValueError("Unsupported workflow key.")
        elif kind == "manual_checkpoint":
            text(step.get("message"), "Checkpoint message", 500)
        elif kind == "input":
            source = step.get("source")
            if step.get("sensitive", False) is not False:
                raise ValueError("Sensitive input must use a manual checkpoint.")
            if source == "fixed":
                text(step.get("value"))
                if "variable" in step or "message" in step:
                    raise ValueError("Conflicting input source fields.")
            elif source == "variable":
                if (
                    not isinstance(step.get("variable"), str)
                    or step["variable"] not in names
                ):
                    raise ValueError("Input references an undefined variable.")
                if "value" in step or "message" in step:
                    raise ValueError("Conflicting input source fields.")
            elif source == "manual":
                text(
                    step.get("message", "Complete this step manually."),
                    "Checkpoint message",
                    500,
                )
                if "value" in step or "variable" in step:
                    raise ValueError("Manual inputs cannot contain stored values.")
            else:
                raise ValueError("Input source must be fixed, variable, or manual.")

    if workflow.get("financial") and steps and steps[-1]["type"] != "manual_checkpoint":
        raise ValueError(
            "A financial flow must end with a manual authorization checkpoint."
        )
    return deepcopy(workflow)


def resolve_variables(workflow, supplied=None):
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict):
        raise ValueError("Variables must be an object.")
    names = {v["name"] for v in workflow["variables"]}
    if set(supplied) - names:
        raise ValueError("Unknown workflow variable supplied.")
    return {
        v["name"]: validate_variable_value(v, supplied.get(v["name"], v.get("default")))
        for v in workflow["variables"]
    }
