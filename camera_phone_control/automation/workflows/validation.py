"""Validate the whole document and resolve variables before any phone input."""

import base64
import binascii
import math
import re
import unicodedata
from copy import deepcopy
from automation.transfer_methods import (
    LEGACY_TRANSFER_METHODS,
    TRANSFER_METHODS,
    validate_transfer_method,
)
from ..coordinates import unit, normalized_roi
from .models import STEP_TYPES, VARIABLE_TYPES, WORKFLOW_PURPOSES

IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}")
SENSITIVE = re.compile(
    r"(?:^|[^a-z])(?:pin|otp)(?:$|[^a-z])|password|passwd|passcode|secret|biometric|approval_code",
    re.I,
)
PROTECTED_SCREEN_INPUT = re.compile(
    r"randomi[sz]ed|secure.?keypad|authoriz|authoris|confirm.*(?:pay|transfer)|final.*(?:pay|transfer)",
    re.I,
)
KEYBOARD_KINDS = {"numeric", "alphabetic", "custom"}
NUMERIC_KEY_LABELS = set("0123456789.") | {"BACKSPACE"}
ALPHABETIC_KEY_LABELS = set('abcdefghijklmnopqrstuvwxyz0123456789 @#$+(;"') | {
    "backspace",
    "shift",
}
MAX_CUSTOM_KEYS = 100
MAX_CUSTOM_KEY_LABEL = 32
MAX_CONDITION_DEPTH = 5
KEYS = {
    "ENTER",
    "BACKSPACE",
    "TAB",
    "HOME",
    "BACK",
    "RECENTS",
    "SCREEN_ON",
    "SCREEN_OFF",
    "LEFT",
    "RIGHT",
    "UP",
    "DOWN",
    "DELETE",
    "MOVE_HOME",
    "END",
    "PAGEUP",
    "PAGEDOWN",
}


def _workflow_method_definition(method):
    return TRANSFER_METHODS.get(method) or LEGACY_TRANSFER_METHODS.get(method)


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
    if value is None:
        if definition.get("required", False):
            raise ValueError("A required workflow variable is missing.")
        return ""
    if definition["type"] == "text":
        return text(value)
    number(value, -1e15, 1e15)
    return str(value)


def normalize_workflow(workflow):
    """Remove retired image-matching fields from version 1 workflows."""
    if not isinstance(workflow, dict):
        return workflow
    normalized = deepcopy(workflow)
    # Version 1 workflows created before purpose metadata were ordinary
    # payment/automation recipes. Startup migration upgrades the safe subset
    # already assigned as balance readers before normal reads begin.
    normalized.setdefault("purpose", "payment")
    normalized.pop("visual_references", None)
    steps = normalized.get("steps")
    if isinstance(steps, list):
        pending = list(steps)
        while pending:
            step = pending.pop()
            if isinstance(step, dict):
                step.pop("expect_after", None)
                for branch in ("then_steps", "else_steps"):
                    nested = step.get(branch)
                    if isinstance(nested, list):
                        pending.extend(nested)
    return normalized


def normalize_capture_keyboard_labels(kind, labels):
    if not isinstance(kind, str) or kind not in KEYBOARD_KINDS:
        raise ValueError("Keyboard kind must be numeric, alphabetic, or custom.")
    if not isinstance(labels, list) or not labels:
        raise ValueError("Choose at least one keyboard key.")
    normalized = []
    for label in labels:
        if (
            kind == "numeric"
            and isinstance(label, str)
            and label.casefold() == "backspace"
        ):
            label = "BACKSPACE"
        elif kind == "alphabetic" and isinstance(label, str):
            folded = label.casefold()
            label = folded if folded in {"backspace", "shift"} else label
        normalized.append(label)
    validate_keyboard_labels(kind, normalized)
    return normalized


def validate_keyboard_labels(kind, labels):
    if not isinstance(kind, str) or kind not in KEYBOARD_KINDS:
        raise ValueError("Keyboard kind must be numeric, alphabetic, or custom.")
    maximum = (
        len(NUMERIC_KEY_LABELS)
        if kind == "numeric"
        else (len(ALPHABETIC_KEY_LABELS) if kind == "alphabetic" else MAX_CUSTOM_KEYS)
    )
    if not labels or len(labels) > maximum:
        raise ValueError("A keyboard must contain a supported number of keys.")
    if any(not isinstance(label, str) for label in labels):
        raise ValueError("Keyboard key labels must be strings.")
    if len(set(labels)) != len(labels):
        raise ValueError("Keyboard key labels must not contain duplicates.")
    if kind == "numeric" and any(label not in NUMERIC_KEY_LABELS for label in labels):
        raise ValueError("Numeric keyboards support digits, decimal, and backspace.")
    if kind == "alphabetic" and any(
        label not in ALPHABETIC_KEY_LABELS for label in labels
    ):
        raise ValueError(
            "Alphabetic keyboards support lowercase a-z, digits 0-9, the "
            "configured symbols, space, backspace, and shift."
        )
    if kind == "custom":
        for label in labels:
            if (
                not label.strip()
                or len(label) > MAX_CUSTOM_KEY_LABEL
                or any(
                    unicodedata.category(character).startswith("C")
                    for character in label
                )
            ):
                raise ValueError(
                    "Custom key labels must be 1-32 visible characters without controls."
                )


def validate_keyboard_map(keyboard_id, keyboard):
    identifier(keyboard_id)
    fields(
        keyboard,
        {"id", "name", "kind", "keys"},
        {"id", "name", "kind", "keys"},
    )
    if keyboard["id"] != keyboard_id:
        raise ValueError("Keyboard ID does not match its workflow key.")
    name = text(keyboard["name"], "Keyboard name", 160).strip()
    if not name:
        raise ValueError("Keyboard name is required.")
    if PROTECTED_SCREEN_INPUT.search(name):
        raise ValueError("Protected or randomized keyboards must remain manual.")
    kind = keyboard["kind"]
    if not isinstance(kind, str) or kind not in KEYBOARD_KINDS:
        raise ValueError("Keyboard kind must be numeric, alphabetic, or custom.")
    keys = keyboard["keys"]
    if not isinstance(keys, dict):
        raise ValueError("Keyboard keys must be an object.")
    validate_keyboard_labels(kind, list(keys))
    for label, coordinate in keys.items():
        if not isinstance(coordinate, dict) or set(coordinate) != {"x", "y"}:
            raise ValueError("Malformed screen-keyboard coordinates.")
        unit(coordinate["x"])
        unit(coordinate["y"])


def validate_workflow(workflow, *, allow_empty=False):
    workflow = normalize_workflow(workflow)
    fields(
        workflow,
        {
            "id",
            "name",
            "version",
            "variables",
            "keyboards",
            "steps",
            "created_at",
            "updated_at",
            "financial",
            "purpose",
            "recovery_workflow_id",
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
    if workflow.get("purpose") not in WORKFLOW_PURPOSES:
        raise ValueError("Workflow purpose must be payment or balance_check.")

    recovery_workflow_id = workflow.get("recovery_workflow_id")
    if recovery_workflow_id is not None:
        identifier(recovery_workflow_id)
        if recovery_workflow_id == workflow["id"]:
            raise ValueError("A workflow cannot use itself as its recovery workflow.")

    for field in ("created_at", "updated_at"):
        if field in workflow:
            text(workflow[field], "Timestamp", 80)

    variables = workflow["variables"]
    keyboards = workflow.get("keyboards", {})
    steps = workflow["steps"]

    if not isinstance(keyboards, dict) or len(keyboards) > 32:
        raise ValueError("At most 32 captured keyboards are supported.")

    keyboard_names = set()

    for keyboard_id, keyboard in keyboards.items():
        validate_keyboard_map(keyboard_id, keyboard)
        keyboard_name = keyboard["name"].strip().casefold()
        if keyboard_name in keyboard_names:
            raise ValueError("Captured keyboard names must be unique.")
        keyboard_names.add(keyboard_name)

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

        if type(definition.get("sensitive", False)) is not bool:
            raise ValueError("Variable sensitive flag must be a boolean.")
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

    pending_steps = [(step, 0) for step in reversed(steps)]
    transfer_switches = []
    validated_step_count = 0
    while pending_steps:
        step, depth = pending_steps.pop()
        validated_step_count += 1
        if validated_step_count > 1000:
            raise ValueError("A workflow must have at most 1000 steps.")
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
            "input": {
                "source",
                "value",
                "variable",
                "message",
                "sensitive",
                "input_method",
                "keyboard_id",
                "amount_format",
            },
            "key": {"key"},
            "wait": {"seconds"},
            "wait_until_stable": {"stable_ms", "timeout_seconds"},
            "wait_for_text": {"text", "timeout_seconds", "roi"},
            "verify_text": {
                "source",
                "text",
                "variable",
                "match_mode",
                "last_n",
                "timeout_seconds",
                "roi",
            },
            "if_text": {
                "text",
                "timeout_seconds",
                "roi",
                "then_steps",
                "else_steps",
            },
            "switch_transfer_method": {"branches"},
            "capture_screen": {"output_key"},
            "ocr_value": {"roi", "parser", "output"},
            "manual_checkpoint": {"message"},
        }[kind]

        fields(
            step,
            {
                "id",
                "type",
                "label",
                "preview",
                "recording_screenshot",
                "recorded_at",
                "critical",
            }
            | specific,
            {"id", "type"},
        )

        if "label" in step:
            text(step["label"], "Step name", 160)
        if type(step.get("critical", False)) is not bool:
            raise ValueError("critical must be a boolean.")
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
        if "recording_screenshot" in step:
            screenshot = step["recording_screenshot"]

            fields(
                screenshot,
                {"image", "width", "height", "format"},
                {"image", "width", "height", "format"},
            )

            if screenshot["format"] != "jpeg":
                raise ValueError("Recording screenshot must be JPEG.")

            number(screenshot["width"], 1, 10000)
            number(screenshot["height"], 1, 10000)

            encoded = text(
                screenshot["image"],
                "Recording screenshot",
                1000000,
            )

            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("Invalid recording screenshot.") from None

            if not raw.startswith(b"\xff\xd8\xff"):
                raise ValueError("Invalid recording screenshot.")

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
        elif kind == "wait_until_stable":
            for field, low, high in (
                ("stable_ms", 100, 10000),
                ("timeout_seconds", 1, 300),
            ):
                try:
                    number(step.get(field), low, high)
                except ValueError:
                    raise ValueError(
                        f"{field} must be numeric between {low} and {high}."
                    ) from None
            if step["timeout_seconds"] < step["stable_ms"] / 1000:
                raise ValueError(
                    "timeout_seconds must allow the requested stability window."
                )
        elif kind == "capture_screen":
            if "output_key" in step:
                if step["output_key"] == "":
                    step.pop("output_key")
                else:
                    identifier(step["output_key"])
        elif kind == "ocr_value":
            if step.get("parser") != "currency":
                raise ValueError(
                    "Read Balance currently supports the currency parser only."
                )
            identifier(step.get("output"))
            step["roi"] = dict(
                zip(("x", "y", "width", "height"), normalized_roi(step.get("roi")))
            )
        elif kind == "verify_text":
            source = step.get("source")
            if source == "fixed":
                expected = text(step.get("text"), "Text to verify", 500).strip()
                if not expected:
                    raise ValueError("Text to verify must not be empty.")
                if "variable" in step:
                    raise ValueError("Conflicting verify_text source fields.")
                step["text"] = expected
            elif source == "variable":
                if (
                    not isinstance(step.get("variable"), str)
                    or step["variable"] not in names
                ):
                    raise ValueError("verify_text references an undefined variable.")
                if "text" in step:
                    raise ValueError("Conflicting verify_text source fields.")
            else:
                raise ValueError("verify_text source must be fixed or variable.")

            match_mode = step.get("match_mode")
            if match_mode not in {
                "exact",
                "contains",
                "last_n_digits",
                "account_name",
                "account_number",
                "fps_phone",
                "amount",
            }:
                raise ValueError("Unsupported verify_text match mode.")
            if match_mode == "last_n_digits":
                last_n = step.get("last_n")
                if type(last_n) is not int or not 1 <= last_n <= 64:
                    raise ValueError("last_n must be an integer between 1 and 64.")
            elif "last_n" in step:
                raise ValueError("last_n is only valid with last_n_digits matching.")

            try:
                number(step.get("timeout_seconds"), 0.5, 300)
            except ValueError:
                raise ValueError(
                    "timeout_seconds must be numeric between 0.5 and 300."
                ) from None
            if "roi" in step:
                values = normalized_roi(step["roi"])
                step["roi"] = (
                    dict(zip(("x", "y", "width", "height"), values))
                    if isinstance(step["roi"], dict)
                    else values
                )
        elif kind in {"wait_for_text", "if_text"}:
            expected = text(step.get("text"), "Text to detect", 500).strip()
            if not expected:
                raise ValueError("Text to detect must not be empty.")
            step["text"] = expected

            try:
                number(
                    step.get("timeout_seconds"),
                    0.1 if kind == "if_text" else 0.5,
                    300,
                )
            except ValueError:
                raise ValueError(
                    "timeout_seconds must be numeric between "
                    f"{'0.1' if kind == 'if_text' else '0.5'} and 300."
                ) from None

            if "roi" in step:
                values = normalized_roi(step["roi"])
                step["roi"] = (
                    dict(zip(("x", "y", "width", "height"), values))
                    if isinstance(step["roi"], dict)
                    else values
                )
            if kind == "if_text":
                if depth >= MAX_CONDITION_DEPTH:
                    raise ValueError(
                        f"if_text nesting cannot exceed {MAX_CONDITION_DEPTH} levels."
                    )
                for branch in ("then_steps", "else_steps"):
                    nested = step.get(branch)
                    if not isinstance(nested, list):
                        raise ValueError(f"{branch} must be a list of workflow steps.")
                    pending_steps.extend(
                        (child, depth + 1) for child in reversed(nested)
                    )

        elif kind == "switch_transfer_method":
            if depth >= MAX_CONDITION_DEPTH:
                raise ValueError(
                    "Transfer-method condition nesting cannot exceed "
                    f"{MAX_CONDITION_DEPTH} levels."
                )
            branches = step.get("branches")
            if not isinstance(branches, dict) or not branches:
                raise ValueError(
                    "Transfer-method condition must contain explicit branches."
                )
            unknown = (
                set(branches) - set(TRANSFER_METHODS) - set(LEGACY_TRANSFER_METHODS)
            )
            if unknown:
                raise ValueError(
                    "Transfer-method condition contains an unsupported branch."
                )
            for method, nested in branches.items():
                if not isinstance(nested, list):
                    raise ValueError(
                        f"Transfer-method branch {method} must be a list of workflow steps."
                    )
                if not nested and not allow_empty:
                    raise ValueError(
                        f"Transfer-method branch {method} must not be empty."
                    )
                pending_steps.extend((child, depth + 1) for child in reversed(nested))
            transfer_switches.append(step)

        elif kind == "key":
            if not isinstance(step.get("key"), str) or step["key"].upper() not in KEYS:
                raise ValueError("Unsupported workflow key.")
        elif kind == "manual_checkpoint":
            text(step.get("message"), "Checkpoint message", 500)
        elif kind == "input":
            source = step.get("source")
            if type(step.get("sensitive", False)) is not bool:
                raise ValueError("Input sensitive flag must be a boolean.")
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
            if "amount_format" in step:
                if source != "variable" or step.get("variable") != "amount":
                    raise ValueError(
                        "Amount input format is only valid for the amount variable."
                    )
                if step["amount_format"] not in {
                    "raw",
                    "implied_cents",
                    "whole_number",
                }:
                    raise ValueError("Unsupported amount input format.")
            input_method = step.get("input_method", "hid")
            if source == "manual":
                if "input_method" in step or "keyboard_id" in step:
                    raise ValueError("Manual inputs cannot select an input method.")
            elif input_method == "hid":
                if "keyboard_id" in step:
                    raise ValueError("HID input cannot reference a captured keyboard.")
            elif input_method == "screen_keyboard":
                keyboard_id = step.get("keyboard_id")
                if keyboard_id not in keyboards:
                    raise ValueError("Input references an undefined captured keyboard.")
                protected = " ".join(
                    str(value)
                    for value in (
                        step.get("label", ""),
                        step.get("variable", ""),
                        next(
                            (
                                definition["label"]
                                for definition in variables
                                if definition["name"] == step.get("variable")
                            ),
                            "",
                        ),
                    )
                )
                if PROTECTED_SCREEN_INPUT.search(protected):
                    raise ValueError("Protected input must use a manual checkpoint.")
            else:
                raise ValueError("Input method must be hid or screen_keyboard.")

    if transfer_switches and workflow.get("purpose", "payment") != "payment":
        raise ValueError(
            "Transfer-method conditions are only valid in payment workflows."
        )

    if not allow_empty and workflow.get("purpose", "payment") == "payment":
        method_definitions = {
            **LEGACY_TRANSFER_METHODS,
            **TRANSFER_METHODS,
        }
        identifier_fields = {
            definition["workflow_variable"]
            for definition in method_definitions.values()
        }

        def validate_identifier_scope(branch_steps, selected_method=None):
            selected_identifier = (
                _workflow_method_definition(selected_method)["workflow_variable"]
                if _workflow_method_definition(selected_method) is not None
                else None
            )
            for child in branch_steps:
                variable = child.get("variable")
                if (
                    child.get("type") in {"input", "verify_text"}
                    and variable in identifier_fields
                    and variable != selected_identifier
                ):
                    raise ValueError(
                        f"Recipient identifier {variable} must only be used in "
                        "a branch with the matching identifier profile."
                    )
                if child.get("type") == "if_text":
                    validate_identifier_scope(
                        child.get("then_steps", []), selected_method
                    )
                    validate_identifier_scope(
                        child.get("else_steps", []), selected_method
                    )
                elif child.get("type") == "switch_transfer_method":
                    for nested_method, nested_branch in child["branches"].items():
                        validate_identifier_scope(nested_branch, nested_method)

        if transfer_switches:
            validate_identifier_scope(steps)
        for switch in transfer_switches:
            for method, branch in switch["branches"].items():
                method_definition = _workflow_method_definition(method)
                identifier_field = method_definition["workflow_variable"]
                if not any(
                    child.get("type") == "input"
                    and child.get("source") == "variable"
                    and child.get("variable") == identifier_field
                    for child in branch
                ):
                    raise ValueError(
                        f"Transfer-method branch {method} must input {identifier_field}."
                    )
                if not any(
                    child.get("type") == "verify_text"
                    and child.get("source") == "variable"
                    and child.get("variable") == identifier_field
                    for child in branch
                ):
                    raise ValueError(
                        f"Transfer-method branch {method} must verify {identifier_field}."
                    )
                verification_modes = set(method_definition["verification_modes"])
                if any(
                    child.get("type") == "verify_text"
                    and child.get("source") == "variable"
                    and child.get("variable") == identifier_field
                    and child.get("match_mode") not in verification_modes
                    for child in branch
                ):
                    raise ValueError(
                        f"Transfer-method branch {method} uses an unsafe "
                        f"verification mode for {identifier_field}."
                    )

    if workflow.get("financial") and steps and steps[-1]["type"] != "manual_checkpoint":
        raise ValueError(
            "A financial flow must end with a manual authorization checkpoint."
        )
    return deepcopy(workflow)


def require_transfer_method_branch(workflow, method, *, recipient_identifier=None):
    """Fail before locking when a method has no explicit executable branch."""
    method = validate_transfer_method(method)
    pending = list(workflow.get("steps", []))
    switches = []
    while pending:
        step = pending.pop()
        if step.get("type") == "if_text":
            pending.extend(step.get("then_steps", []))
            pending.extend(step.get("else_steps", []))
        elif step.get("type") == "switch_transfer_method":
            switches.append(step)
            for branch in step.get("branches", {}).values():
                pending.extend(branch)
    if not switches:
        if method == "bank_account":
            return workflow
        raise ValueError(
            f"Payment workflow has no explicit branch for transfer_method {method}."
        )
    if any(not switch.get("branches", {}).get(method) for switch in switches):
        raise ValueError(
            f"Payment workflow has no explicit branch for transfer_method {method}."
        )
    if recipient_identifier is not None:
        definition = TRANSFER_METHODS[method]
        variable = definition["workflow_variable"]

        def identifier_inputs(steps):
            for child in steps:
                if (
                    child.get("type") == "input"
                    and child.get("source") == "variable"
                    and child.get("variable") == variable
                ):
                    yield child
                if child.get("type") == "if_text":
                    yield from identifier_inputs(child.get("then_steps", []))
                    yield from identifier_inputs(child.get("else_steps", []))

        for switch in switches:
            for input_step in identifier_inputs(switch["branches"][method]):
                if input_step.get("input_method", "hid") == "screen_keyboard":
                    keyboard = workflow.get("keyboards", {}).get(
                        input_step.get("keyboard_id")
                    )
                    keys = set((keyboard or {}).get("keys", {}))
                    missing = sorted(set(recipient_identifier) - keys)
                    uppercase = (
                        sorted(
                            character
                            for character in recipient_identifier
                            if character.isupper()
                        )
                        if (keyboard or {}).get("kind") == "alphabetic"
                        else []
                    )
                    if missing or uppercase:
                        raise ValueError(
                            f"Transfer-method branch {method} cannot safely type "
                            f"the API {definition['identifier_field']} with its "
                            "captured keyboard."
                        )
                elif (
                    not isinstance(recipient_identifier, str)
                    or not 1 <= len(recipient_identifier) <= 4096
                    or any(
                        not " " <= character <= "~"
                        for character in recipient_identifier
                    )
                ):
                    raise ValueError(
                        f"Transfer-method branch {method} cannot safely type the "
                        f"API {definition['identifier_field']} through HID."
                    )
    return workflow


def resolve_variables(workflow, supplied=None, *, execution_context=None):
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict):
        raise ValueError("Variables must be an object.")
    names = {v["name"] for v in workflow["variables"]}
    if set(supplied) - names:
        raise ValueError("Unknown workflow variable supplied.")
    resolved = {}
    inactive_identifiers = set()
    if execution_context and "transfer_method" in execution_context:
        selected_method = validate_transfer_method(execution_context["transfer_method"])
        selected_definition = TRANSFER_METHODS[selected_method]
        selected_identifier = selected_definition["workflow_variable"]
        inactive_identifiers = {
            definition["workflow_variable"]
            for definition in [
                *TRANSFER_METHODS.values(),
                *LEGACY_TRANSFER_METHODS.values(),
            ]
            if definition["workflow_variable"] != selected_identifier
        }
        inactive_identifiers.update(
            {"account_name", "holder_name"}
            - {selected_definition["recipient_name_field"]}
        )
    for definition in workflow["variables"]:
        name = definition["name"]
        value = supplied[name] if name in supplied else definition.get("default")
        if name in inactive_identifiers and value is None:
            continue
        try:
            resolved[name] = validate_variable_value(definition, value)
        except ValueError as exc:
            raise ValueError(f"Variable {name!r}: {exc}") from None
    return resolved


def validate_recovery_workflow(workflow):
    """Require an unattended workflow with an OCR-verified final ready state."""
    workflow = validate_workflow(workflow)
    if not workflow["steps"] or workflow["steps"][-1]["type"] != "wait_for_text":
        raise ValueError(
            "Recovery workflow must end with wait_for_text to verify the ready state."
        )
    pending = list(workflow["steps"])
    nested_steps = []
    while pending:
        step = pending.pop()
        nested_steps.append(step)
        if step["type"] == "if_text":
            pending.extend(step["then_steps"])
            pending.extend(step["else_steps"])
        elif step["type"] == "switch_transfer_method":
            for branch in step["branches"].values():
                pending.extend(branch)
    if any(
        step.get("critical") is True
        or step["type"] == "manual_checkpoint"
        or (step["type"] == "input" and step.get("source") == "manual")
        for step in nested_steps
    ):
        raise ValueError(
            "Recovery workflow cannot contain critical or manual checkpoint steps."
        )
    try:
        resolve_variables(workflow, {})
    except ValueError:
        raise ValueError(
            "Recovery workflow variables must have configured default values."
        ) from None
    return workflow
