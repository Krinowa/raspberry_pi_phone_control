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
    "capture_screen",
    "manual_checkpoint",
}
VARIABLE_TYPES = {"text", "number"}


def now():
    return datetime.now(timezone.utc).isoformat()


def new_workflow(name):
    return {
        "id": "flow_" + uuid.uuid4().hex,
        "name": name,
        "version": 1,
        "variables": [],
        "keyboards": {},
        "steps": [],
        "financial": False,
    }
