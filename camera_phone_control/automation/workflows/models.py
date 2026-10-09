"""Version 1 JSON documents use dicts; runtime state never contains input values."""

import uuid
from datetime import datetime, timezone

STEP_TYPES = {
    "tap",
    "swipe",
    "input",
    "key",
    "wait",
    "wait_until_stable",
    "wait_for_text",
    "if_text",
    "capture_screen",
    "ocr_value",
    "manual_checkpoint",
}
VARIABLE_TYPES = {"text", "number"}
WORKFLOW_PURPOSES = {"payment", "balance_check"}


def now():
    return datetime.now(timezone.utc).isoformat()


def new_workflow(name, purpose="payment"):
    return {
        "id": "flow_" + uuid.uuid4().hex,
        "name": name,
        "purpose": purpose,
        "version": 1,
        "variables": [],
        "keyboards": {},
        "steps": [],
        "financial": False,
    }
