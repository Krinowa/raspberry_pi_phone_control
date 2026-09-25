"""Conservative UI hints for recording; no device-specific data enters actions.

Unknown/secure input is left to the operator. UI labels are hints, not selectors.
"""

import cv2
import base64
import re
import numpy as np
import xml.etree.ElementTree as ET


from .validation import SENSITIVE
from ..context import Cancelled
from ..coordinates import normalized_to_device

BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
SAFE_FIELD = re.compile(
    r"search|customer.?name|user.?name|first.?name|last.?name|full.?name|reference|address|e.?mail|amount|memo|message|city|description",
    re.I,
)


def editable(node):
    return node.get("editable") == "true" or "EditText" in node.get("class", "")


def bounds(node):
    match = BOUNDS.fullmatch(node.get("bounds", ""))
    return tuple(map(int, match.groups())) if match else None


def inspect_target(context, step):
    """Fail closed for unknown text targets; don't collect editable field values."""
    result = {
        "known": False,
        "secure": False,
        "safe_input": False,
        "label": "",
        "target": None,
        "nodes": [],
    }

    if context.input_backend == "mock":
        return {**result, "known": True, "safe_input": True, "target": "mock-text"}
    try:
        root = ET.fromstring(context.screen.inspect())
    except (ET.ParseError, TypeError, ValueError):
        return result

    nodes = list(root.iter("node"))
    result["known"] = bool(nodes)
    result["nodes"] = nodes
    # Include labels/resource IDs, but never use an editable node's current value.
    for node in nodes:
        labels = " ".join(
            node.get(key, "") for key in ("resource-id", "content-desc", "hint")
        )
        if not editable(node):
            labels += " " + node.get("text", "")
        if node.get("password") == "true" or SENSITIVE.search(labels):
            result["secure"] = True

    focused = [
        node for node in nodes if node.get("focused") == "true" and editable(node)
    ]

    if len(focused) == 1:
        node = focused[0]
        labels = " ".join(
            node.get(key, "") for key in ("resource-id", "content-desc", "hint")
        )
        result["safe_input"] = (
            not result["secure"]
            and node.get("password") == "false"
            and bool(SAFE_FIELD.search(labels))
        )
        result["target"] = (node.get("resource-id"), node.get("bounds"))
        # Hints are stable field names; editable content-desc can contain its value.
        result["label"] = node.get("hint", "")[:160]

    if step["type"] == "tap":
        x, y = normalized_to_device(
            step["x"], step["y"], context.device.width, context.device.height
        )
        candidates = []
        for node in nodes:
            box = bounds(node)
            if (
                box
                and box[0] <= x < box[2]
                and box[1] <= y < box[3]
                and not editable(node)
            ):
                label = node.get("content-desc") or node.get("text")
                if label and len(label) <= 80:
                    candidates.append(((box[2] - box[0]) * (box[3] - box[1]), label))
        result["label"] = min(candidates)[1] if candidates else ""
    return result


def tap_preview(context, step, target):
    """Small pre-action PNG crop; skip sensitive/unknown screens and mask fields."""
    if not target["known"] or target["secure"] or context.input_backend == "mock":
        return None

    try:
        image = cv2.imdecode(
            np.frombuffer(context.screen.capture(), dtype=np.uint8), cv2.IMREAD_COLOR
        )
        if image is None or image.shape[:2] != (
            context.device.height,
            context.device.width,
        ):
            return None

        for node in target["nodes"]:
            if editable(node):
                box = bounds(node)
                if box:
                    left, top, right, bottom = box
                    image[max(0, top) : bottom, max(0, left) : right] = 0

        x, y = normalized_to_device(
            step["x"], step["y"], context.device.width, context.device.height
        )
        radius = max(48, context.device.width // 12)
        crop = image[max(0, y - radius) : y + radius, max(0, x - radius) : x + radius]
        crop = cv2.resize(crop, (96, 96))
        ok, encoded = cv2.imencode(".png", crop)
        return base64.b64encode(encoded).decode("ascii") if ok else None
    except Cancelled:
        raise
    except Exception:
        # Optional previews must not repeat or prevent an otherwise valid action.
        # Never log image bytes, UI text, typed values, or transport exceptions.
        return None
