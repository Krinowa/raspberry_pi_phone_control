"""Open MAE and handle the explicitly selected startup buttons when present."""

import re
import xml.etree.ElementTree as ET

PACKAGE = "com.maybank2u.life"
AGREE_RESOURCE_ID = "precheck_tnc_agree_button"
PROCEED_RESOURCE_ID = "app_shield_error_undefined"


def normalize_text(value):
    if value is None:
        return ""

    return " ".join(value.replace("\xa0", " ").split())


def button_center(
    ui,
    device,
    resource_id,
    content_desc,
    package=PACKAGE,
):
    """Locate one available button by ID/label using its current bounds."""

    try:
        root = ET.fromstring(ui)
    except ET.ParseError:
        raise ValueError("Cannot parse the Android UI hierarchy.") from None

    matches = []

    for node in root.iter("node"):
        # Must match the visible/accessibility label.
        if normalize_text(node.get("content-desc")) != normalize_text(content_desc):
            continue

        # Must belong to MAE.
        if node.get("package", package) != package:
            continue

        # resource_id handling:
        #
        # None -> ignore resource-id
        # ""   -> require resource-id=""
        # text -> require exact/full Android resource id
        if resource_id is not None:
            actual_id = node.get("resource-id", "")

            valid_ids = {
                resource_id,
                f"{package}:id/{resource_id}",
            }

            if actual_id not in valid_ids:
                continue

        matches.append(node)

    if not matches:
        return None

    if len(matches) != 1:
        raise ValueError(
            f"Multiple matching buttons found for {content_desc!r}; " "no input sent."
        )

    node = matches[0]

    if (
        node.get("clickable") != "true"
        or node.get("enabled") == "false"
        or node.get("visible-to-user") == "false"
    ):
        raise ValueError("The element is not an available button; no input sent.")

    bounds = re.fullmatch(
        r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
        node.get("bounds", ""),
    )

    if bounds is None:
        raise ValueError("The button has invalid bounds; no input sent.")

    left, top, right, bottom = map(
        int,
        bounds.groups(),
    )

    if not (0 <= left < right <= device.width and 0 <= top < bottom <= device.height):
        raise ValueError("The button is outside the display; no input sent.")

    return (
        (left + right) // 2,
        (top + bottom) // 2,
    )


def click_button(context, ui, resource_id, content_desc):
    """Send at most one tap through the cancellation-aware context."""
    point = button_center(ui, context.device, resource_id, content_desc)
    if point is None:
        return False
    context.send("tap", point)
    return True


def run(context):
    context.logger.info("Opening MAE application.")

    context.check_cancelled()

    # Wake the screen.
    context.send("screen_on")
    context.wait(0.8)

    # Dismiss a simple swipe-only lock screen.
    context.send("swipe_up")
    context.wait(1.0)

    # Return to Home.
    context.send("home")
    context.wait(0.8)

    # Launch MAE.
    context.launch_app(PACKAGE)

    # Give the app time to appear.
    context.wait(2.0)

    # Inspect current Android UI hierarchy.
    ui_term = context.dump_ui()

    if click_button(context, ui_term, AGREE_RESOURCE_ID, "Agree"):
        context.logger.info("MAE precheck Agree button tapped.")
        context.wait(1.0)
    else:
        context.logger.info("MAE precheck Agree button is not present.")

    # Inspect again after the transition; never reuse the precheck hierarchy.
    context.wait(1.0)
    ui_anti_malware = context.dump_ui()

    if click_button(context, ui_anti_malware, PROCEED_RESOURCE_ID, "Proceed Anyway"):
        context.logger.info("MAE Proceed Anyway button tapped.")
    else:
        context.logger.info("MAE Proceed Anyway button is not present.")

    context.wait(1.0)
    ui_confirm = context.dump_ui()

    if click_button(context, ui_confirm, PROCEED_RESOURCE_ID, "Agree & Proceed"):
        context.logger.info("MAE Agree & Proceed button tapped.")
    else:
        context.logger.info("MAE Agree & Proceed button is not present.")

    context.wait(1.0)
    ui_risk_setting = context.dump_ui()

    if click_button(context, ui_risk_setting, "", "Proceed Anyway"):
        context.logger.info("MAE Proceed Anyway button tapped.")
    else:
        context.logger.info("MAE Proceed Anyway button is not present.")

    context.logger.info("MAE application launch completed.")
