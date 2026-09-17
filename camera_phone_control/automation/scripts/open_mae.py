"""Open MAE and handle the explicitly selected startup buttons when present."""

import re
import time
import xml.etree.ElementTree as ET

PACKAGE = "com.maybank2u.life"
AGREE_RESOURCE_ID = "precheck_tnc_agree_button"
PROCEED_RESOURCE_ID = "app_shield_error_undefined"
MAX_STARTUP_STEPS = 10
STABLE_CHECKS = 2
POLL_INTERVAL = 0.2
NORMAL_TIMEOUT = 10.0
STARTUP_STATES = [
    {"name": "terms", "resource_id": AGREE_RESOURCE_ID, "content_desc": "Agree"},
    {
        "name": "anti_malware",
        "resource_id": PROCEED_RESOURCE_ID,
        "content_desc": "Proceed Anyway",
    },
    {
        "name": "confirm",
        "resource_id": PROCEED_RESOURCE_ID,
        "content_desc": "Agree & Proceed",
    },
    {"name": "risky_settings", "resource_id": "", "content_desc": "Proceed Anyway"},
]


def resource_id_matches(actual_id, resource_id, package=PACKAGE):
    if resource_id is None:
        return True
    if resource_id == "":
        return actual_id == ""
    return actual_id in {resource_id, f"{package}:id/{resource_id}"}


def find_button_node(
    root,
    resource_id,
    content_desc,
    package=PACKAGE,
):
    matches = []

    for node in root.iter("node"):
        if normalize_text(node.get("content-desc")) != normalize_text(content_desc):
            continue

        if node.get("package", package) != package:
            continue

        if not resource_id_matches(
            node.get("resource-id", ""),
            resource_id,
            package,
        ):
            continue

        if (
            node.get("clickable") != "true"
            or node.get("enabled") == "false"
            or node.get("visible-to-user") == "false"
        ):
            continue

        matches.append(node)

    if not matches:
        return None

    if len(matches) != 1:
        raise ValueError(f"Multiple matching buttons found for {content_desc!r}.")

    return matches[0]


def node_center(node, device):
    bounds = re.fullmatch(
        r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
        node.get("bounds", ""),
    )

    if bounds is None:
        raise ValueError("Invalid button bounds.")

    left, top, right, bottom = map(
        int,
        bounds.groups(),
    )

    if not (0 <= left < right <= device.width and 0 <= top < bottom <= device.height):
        raise ValueError("Button is outside the display.")

    return (
        (left + right) // 2,
        (top + bottom) // 2,
    )


def wait_for_any_button(
    context,
    candidates,
    timeout=NORMAL_TIMEOUT,
    interval=POLL_INTERVAL,
):
    deadline = time.monotonic() + timeout

    last_ui = None
    previous_name = None
    stable = 0

    while time.monotonic() < deadline:
        context.check_cancelled()

        # Dump once only.
        last_ui = context.dump_ui()

        try:
            root = ET.fromstring(last_ui)
        except ET.ParseError:
            context.wait(interval)
            continue

        found_state = None
        found_node = None

        for state in candidates:
            node = find_button_node(
                root,
                state["resource_id"],
                state["content_desc"],
            )

            if node is None:
                continue

            if found_state is not None:
                raise ValueError("Multiple startup states found; no input sent.")

            found_state = state
            found_node = node

        if found_state is not None:
            current_name = found_state["name"]

            if current_name == previous_name:
                stable += 1
            else:
                stable = 1

                context.logger.info(
                    "Detected startup state: %s",
                    current_name,
                )

            previous_name = current_name

            if stable >= STABLE_CHECKS:
                context.logger.info(
                    "Stable UI confirmed: %s (%d/%d)",
                    current_name,
                    stable,
                    STABLE_CHECKS,
                )

                return (
                    found_state,
                    last_ui,
                    found_node,
                )

        else:
            previous_name = None
            stable = 0

        remaining = deadline - time.monotonic()

        if remaining <= 0:
            break

        context.wait(min(interval, remaining))

    return None, last_ui, None


def wait_for_transition(
    context,
    old_state,
    timeout=NORMAL_TIMEOUT,
):
    deadline = time.monotonic() + timeout
    absent = 0

    while time.monotonic() < deadline:
        context.check_cancelled()

        ui = context.dump_ui()

        try:
            root = ET.fromstring(ui)
        except ET.ParseError:
            context.wait(POLL_INTERVAL)
            continue

        # Is old state still present?
        old_node = find_button_node(
            root,
            old_state["resource_id"],
            old_state["content_desc"],
        )

        # If another known state has already appeared,
        # transition is definitely complete.
        for state in STARTUP_STATES:
            if state["name"] == old_state["name"]:
                continue

            node = find_button_node(
                root,
                state["resource_id"],
                state["content_desc"],
            )

            if node is not None:
                context.logger.info(
                    "Transition confirmed: %s -> %s",
                    old_state["name"],
                    state["name"],
                )
                return True

        if old_node is None:
            absent += 1

            if absent >= STABLE_CHECKS:
                context.logger.info(
                    "Transition confirmed: %s disappeared.",
                    old_state["name"],
                )
                return True
        else:
            absent = 0

        remaining = deadline - time.monotonic()

        if remaining > 0:
            context.wait(min(POLL_INTERVAL, remaining))

    return False


def normalize_text(value):
    if value is None:
        return ""

    return " ".join(value.replace("\xa0", " ").split())


# def button_center(
#     ui,
#     device,
#     resource_id,
#     content_desc,
#     package=PACKAGE,
# ):
#     """Locate one available button by ID/label using its current bounds."""

#     try:
#         root = ET.fromstring(ui)
#     except ET.ParseError:
#         raise ValueError("Cannot parse the Android UI hierarchy.") from None

#     matches = []

#     for node in root.iter("node"):
#         # Must match the visible/accessibility label.
#         if normalize_text(node.get("content-desc")) != normalize_text(content_desc):
#             continue

#         # Must belong to MAE.
#         if node.get("package", package) != package:
#             continue

#         # resource_id handling:
#         #
#         # None -> ignore resource-id
#         # ""   -> require resource-id=""
#         # text -> require exact/full Android resource id
#         if not resource_id_matches(node.get("resource-id", ""), resource_id, package):
#             continue
#         if (
#             node.get("clickable") != "true"
#             or node.get("enabled") == "false"
#             or node.get("visible-to-user") == "false"
#         ):
#             continue

#         matches.append(node)

#     if not matches:
#         return None

#     if len(matches) != 1:
#         raise ValueError(
#             f"Multiple matching buttons found for {content_desc!r}; " "no input sent."
#         )

#     node = matches[0]

#     bounds = re.fullmatch(
#         r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
#         node.get("bounds", ""),
#     )

#     if bounds is None:
#         raise ValueError("The button has invalid bounds; no input sent.")

#     left, top, right, bottom = map(
#         int,
#         bounds.groups(),
#     )

#     if not (0 <= left < right <= device.width and 0 <= top < bottom <= device.height):
#         raise ValueError("The button is outside the display; no input sent.")

#     return (
#         (left + right) // 2,
#         (top + bottom) // 2,
#     )


# def click_button(context, ui, resource_id, content_desc):
#     """Send at most one tap through the cancellation-aware context."""
#     context.check_cancelled()
#     if ui is None:
#         return False
#     point = button_center(ui, context.device, resource_id, content_desc)
#     if point is None:
#         return False
#     context.send("tap", point)
#     return True


def run(context):
    context.logger.info("Opening MAE application.")

    context.check_cancelled()

    # Wake the screen.
    context.send("screen_on")
    context.wait(0.4)

    # Dismiss a simple swipe-only lock screen.
    context.send("swipe_up")
    context.wait(0.4)

    # Return to Home.
    context.send("home")
    context.wait(0.4)

    # Launch MAE.
    context.launch_app(PACKAGE)

    for _ in range(MAX_STARTUP_STEPS):
        context.logger.info("Waiting for MAE startup state...")
        state, ui, node = wait_for_any_button(
            context,
            STARTUP_STATES,
        )
        if state is None:
            context.logger.info(
                "No known startup button detected before timeout. "
                "Stopping; MAE landing page is not yet verified."
            )
            return

        point = node_center(
            node,
            context.device,
        )

        context.logger.info(
            "Tapping startup state %s at %s",
            state["name"],
            point,
        )

        context.send(
            "tap",
            point,
        )

        context.logger.info("Startup button tapped: %s", state["name"])
        context.logger.info("Waiting for next MAE state...")
        if not wait_for_transition(context, state):
            raise TimeoutError(
                f"No verified transition after tapping {state['name']}; "
                "stopped without repeating the tap."
            )
        # Always capture new XML on the next iteration after the transition.

    raise RuntimeError("MAE startup step limit reached; stopped.")
