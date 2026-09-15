"""
Appium launch and warning acknowledgement proof of concept for Maybank MAE.

Flow:
1. Detect one physical Android phone through USB/ADB.
2. Connect to Appium.
3. Confirm MAE is installed.
4. Resolve and launch its launcher activity.
5. Print current package/activity.
6. Detect the MAE malware-risk warning.
7. If warning appears:
   - Print visible UI element metadata.
   - Print elements containing "Proceed".
   - Click the identified Proceed Anyway button once.
8. Read basic UI hierarchy information.
9. Close the Appium session.

No login, PIN, OTP, biometrics, transfer, or payment actions.
"""

import json
import shutil
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

APPIUM_SERVER_URL = "http://127.0.0.1:4723"

APP_PACKAGE = "com.maybank2u.life"

MAE_WARNING_TEXT = (
    "Our anti-malware tool has identified app(s) " "that may put your MAE app at risk"
)

PROCEED_BUTTON_TEXT = "Proceed Anyway"
PROCEED_BUTTON_ID = "app_shield_error_undefined"
PROCEED_BUTTON_CLASS = "android.view.ViewGroup"


class PocError(Exception):
    pass


def log(tag, message):
    print(f"[{tag}] {message}", flush=True)


# ============================================================
# ADB
# ============================================================


def adb(*args):
    result = subprocess.run(
        ["adb", *args],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    if result.returncode != 0:
        raise PocError(f"ADB command failed:\n{result.stderr.strip()}")

    return result.stdout.strip()


def get_connected_phone():
    if not shutil.which("adb"):
        raise PocError("ADB is not installed or not available in PATH.")

    log(
        "ADB",
        "Checking connected devices...",
    )

    output = adb(
        "devices",
        "-l",
    )

    print(output)

    devices = []

    for line in output.splitlines()[1:]:
        line = line.strip()

        if not line:
            continue

        parts = line.split()

        if len(parts) < 2:
            continue

        serial = parts[0]
        state = parts[1]

        if state == "unauthorized":
            raise PocError(
                f"Phone {serial} is unauthorized. "
                "Accept the USB debugging prompt on the phone."
            )

        if state == "offline":
            raise PocError(f"Phone {serial} is offline.")

        if state != "device":
            continue

        if serial.startswith("emulator-"):
            continue

        try:
            is_emulator = adb(
                "-s",
                serial,
                "shell",
                "getprop",
                "ro.kernel.qemu",
            )

            if is_emulator == "1":
                continue

        except Exception:
            continue

        devices.append(serial)

    if not devices:
        raise PocError("No authorized physical Android phone found.")

    if len(devices) > 1:
        raise PocError(
            "More than one Android phone is connected. " "Disconnect the others first."
        )

    serial = devices[0]

    log(
        "ADB",
        f"Phone connected: {serial}",
    )

    return serial


# ============================================================
# APPIUM SERVER
# ============================================================


def check_appium_server():
    log(
        "APPIUM",
        "Checking Appium server...",
    )

    url = APPIUM_SERVER_URL.rstrip("/") + "/status"

    try:
        with urllib.request.urlopen(
            url,
            timeout=5,
        ) as response:

            status = json.load(response)

    except Exception as exc:
        raise PocError(
            "Cannot connect to Appium server.\n\n"
            "Start Appium in another terminal:\n\n"
            "appium --address 127.0.0.1 "
            "--port 4723 --log-level info"
        ) from exc

    if status.get("value", {}).get("ready") is not True:
        raise PocError("Appium server is running but not ready.")

    log(
        "APPIUM",
        "Server ready",
    )


# ============================================================
# APP CHECK
# ============================================================


def check_app_installed(serial):
    log(
        "APP",
        f"Checking package {APP_PACKAGE}...",
    )

    packages = adb(
        "-s",
        serial,
        "shell",
        "pm",
        "list",
        "packages",
        APP_PACKAGE,
    )

    expected = f"package:{APP_PACKAGE}"

    if expected not in packages.splitlines():
        raise PocError(f"{APP_PACKAGE} is not installed on the phone.")

    log(
        "APP",
        "Maybank MAE is installed",
    )


# ============================================================
# RESOLVE LAUNCHER
# ============================================================


def resolve_launcher_activity(serial):
    log(
        "APP",
        "Resolving MAE launcher activity...",
    )

    result = adb(
        "-s",
        serial,
        "shell",
        "cmd",
        "package",
        "resolve-activity",
        "--brief",
        "-a",
        "android.intent.action.MAIN",
        "-c",
        "android.intent.category.LAUNCHER",
        APP_PACKAGE,
    )

    if not result:
        raise PocError("Could not resolve MAE launcher activity.")

    lines = [line.strip() for line in result.splitlines() if line.strip()]

    component = lines[-1]

    if "/" not in component:
        raise PocError(f"Unexpected launcher activity result: {component}")

    log(
        "APP",
        f"Launcher component: {component}",
    )

    return component


# ============================================================
# UI READING
# ============================================================


def read_ui(driver):
    """
    Read exposed Appium XML only.

    No screenshots.
    No coordinate fallback.
    """

    try:
        source = driver.page_source

    except Exception as exc:
        raise PocError(
            f"UI hierarchy unavailable " f"({type(exc).__name__}); stopping."
        ) from exc

    if not source:
        raise PocError("UI hierarchy returned empty content; stopping.")

    try:
        root = ET.fromstring(source)

    except ET.ParseError as exc:
        raise PocError("UI hierarchy returned malformed XML; stopping.") from exc

    return root


# ============================================================
# WARNING DETECTION
# ============================================================


def has_mae_warning(root):
    nodes = [
        node
        for node in root.iter()
        if (node.get("package") == APP_PACKAGE and node.get("displayed") != "false")
    ]

    for attribute in (
        "text",
        "content-desc",
    ):
        content = " ".join(
            node.get(
                attribute,
                "",
            )
            for node in nodes
        )

        normalized = " ".join(content.split())

        if MAE_WARNING_TEXT in normalized:
            return True

    return False


# ============================================================
# PRINT VISIBLE ELEMENTS
# ============================================================


def print_visible_elements(driver):
    """
    Print visible element metadata only.

    Useful for understanding how Appium exposes
    the warning screen.

    This function does not click anything.
    """

    from appium.webdriver.common.appiumby import AppiumBy

    log(
        "UI",
        "Printing visible MAE UI elements...",
    )

    try:
        elements = driver.find_elements(
            AppiumBy.XPATH,
            "//*",
        )

    except Exception as exc:
        log(
            "UI",
            f"Unable to enumerate elements: {exc}",
        )

        return

    printed = 0

    for i, element in enumerate(elements[:150]):

        try:
            if not element.is_displayed():
                continue

            element_class = element.get_attribute("class") or ""

            resource_id = element.get_attribute("resource-id") or ""

            text = element.text or ""

            description = element.get_attribute("content-desc") or ""

            clickable = element.get_attribute("clickable") or ""

            enabled = element.is_enabled()

            package = element.get_attribute("package") or ""

            print(
                f"[ELEMENT {i}] "
                f"class={element_class!r} | "
                f"id={resource_id!r} | "
                f"text={text!r} | "
                f"desc={description!r} | "
                f"clickable={clickable!r} | "
                f"enabled={enabled} | "
                f"package={package!r}"
            )

            printed += 1

        except Exception:
            continue

    log(
        "UI",
        f"Printed {printed} visible elements",
    )


# ============================================================
# PRINT "PROCEED" ELEMENTS
# ============================================================


def print_proceed_elements(driver):
    """
    Print elements whose visible/accessibility text
    contains the word 'Proceed'.

    Read-only inspection only.
    """

    from appium.webdriver.common.appiumby import AppiumBy

    log(
        "UI",
        'Looking for elements containing "Proceed"...',
    )

    try:
        matches = driver.find_elements(
            AppiumBy.XPATH,
            (
                "//*[contains(@text, 'Proceed') "
                "or contains(@content-desc, 'Proceed')]"
            ),
        )

    except Exception as exc:
        log(
            "UI",
            f'Unable to search for "Proceed": {exc}',
        )

        return

    if not matches:
        log(
            "UI",
            'No exposed elements containing "Proceed" found',
        )

        return

    for i, element in enumerate(matches):

        try:
            print(
                f"[PROCEED {i}] "
                f"class={element.get_attribute('class')!r} | "
                f"id={element.get_attribute('resource-id')!r} | "
                f"text={(element.text or '')!r} | "
                f"desc={element.get_attribute('content-desc')!r} | "
                f"clickable={element.get_attribute('clickable')!r} | "
                f"enabled={element.is_enabled()} | "
                f"package={element.get_attribute('package')!r}"
            )

        except Exception:
            continue


# ============================================================
# SPECIFIC WARNING ACKNOWLEDGEMENT
# ============================================================


def acknowledge_mae_warning(
    driver,
    timeout=10,
):
    """
    Detect the MAE malware-risk warning.

    Print UI metadata for inspection.

    Click only the supplied MAE element, once, after verifying the warning.
    A non-breaking space in its accessibility description is treated as a space.
    """

    from appium.webdriver.common.appiumby import AppiumBy

    log(
        "UI",
        "Checking for the specified " "MAE malware-risk warning...",
    )

    if driver.current_package != APP_PACKAGE:
        raise PocError("MAE is not in the foreground; stopping without a click.")

    root = read_ui(driver)

    if not has_mae_warning(root):

        log(
            "UI",
            "Specified MAE warning not present",
        )

        return False

    log(
        "UI",
        "MAE malware-risk warning detected.",
    )

    # --------------------------------------------------------
    # READ-ONLY INSPECTION
    # --------------------------------------------------------

    print()
    print("================ ELEMENT INSPECTION ================")

    print_visible_elements(driver)

    print()

    print_proceed_elements(driver)

    print("====================================================")

    # --------------------------------------------------------
    # IDENTIFIED BUTTON: exact resource ID, class, package and description
    # --------------------------------------------------------

    # XPath preserves the raw ID; AppiumBy.ID may add an Android package prefix.
    locator = (
        f'//*[@package="{APP_PACKAGE}" '
        f'and @resource-id="{PROCEED_BUTTON_ID}" '
        f'and @class="{PROCEED_BUTTON_CLASS}"]'
    )
    deadline = time.monotonic() + timeout

    while True:
        if driver.current_package != APP_PACKAGE:
            raise PocError("MAE left the foreground; stopping without a click.")
        if not has_mae_warning(read_ui(driver)):
            log("UI", "Warning no longer present; no click performed")
            return False

        matches = [
            element
            for element in driver.find_elements(AppiumBy.XPATH, locator)
            if " ".join((element.get_attribute("content-desc") or "").split())
            == PROCEED_BUTTON_TEXT
        ]
        if len(matches) > 1:
            raise PocError("Multiple matching Proceed Anyway buttons; stopping.")
        if matches:
            button = matches[0]
            if (
                button.is_displayed()
                and button.is_enabled()
                and button.get_attribute("clickable") == "true"
            ):
                break
        if time.monotonic() >= deadline:
            raise PocError(
                "Warning present, but the identified Proceed Anyway button is unavailable."
            )
        time.sleep(0.5)

    # Recheck immediately before the only click attempt; never use element index 40.
    if driver.current_package != APP_PACKAGE or not has_mae_warning(read_ui(driver)):
        raise PocError("Warning screen changed before click; stopping.")
    expected_attributes = {
        "package": APP_PACKAGE,
        "resource-id": PROCEED_BUTTON_ID,
        "class": PROCEED_BUTTON_CLASS,
        "clickable": "true",
    }
    if (
        any(
            button.get_attribute(key) != value
            for key, value in expected_attributes.items()
        )
        or " ".join((button.get_attribute("content-desc") or "").split())
        != PROCEED_BUTTON_TEXT
        or not button.is_displayed()
        or not button.is_enabled()
    ):
        raise PocError("Proceed Anyway element changed before click; stopping.")

    log("UI", "Identified Proceed Anyway button found; acknowledging MAE warning")
    try:
        button.click()
    except Exception as exc:
        raise PocError("Click failed or outcome is uncertain; not retrying.") from exc
    log("UI", "Proceed Anyway click successful")
    time.sleep(2)
    return True


# ============================================================
# BASIC UI INSPECTION
# ============================================================


def inspect_ui(driver):
    log(
        "UI",
        "Trying to read UI hierarchy...",
    )

    root = read_ui(driver)

    nodes = [node for node in root.iter() if node is not root]

    log(
        "UI",
        f"UI hierarchy accessible: " f"{len(nodes)} nodes",
    )

    clickable = [node for node in nodes if node.get("clickable") == "true"]

    log(
        "UI",
        f"Clickable nodes exposed: " f"{len(clickable)}",
    )


# ============================================================
# MAIN
# ============================================================


def main():
    driver = None

    try:

        # ----------------------------------------------------
        # 1. PHONE
        # ----------------------------------------------------

        serial = get_connected_phone()

        # ----------------------------------------------------
        # 2. APPIUM SERVER
        # ----------------------------------------------------

        check_appium_server()

        # ----------------------------------------------------
        # 3. CHECK MAE
        # ----------------------------------------------------

        check_app_installed(serial)

        component = resolve_launcher_activity(serial)

        # ----------------------------------------------------
        # 4. IMPORT APPIUM
        # ----------------------------------------------------

        try:
            from appium import webdriver

            from appium.options.android import (
                UiAutomator2Options,
            )

            from appium.webdriver.client_config import (
                AppiumClientConfig,
            )

        except ImportError as exc:

            raise PocError(
                "Python dependencies missing.\n\n"
                "Run:\n"
                "pip install -r requirements.txt"
            ) from exc

        # ----------------------------------------------------
        # 5. SESSION
        # ----------------------------------------------------

        log(
            "APPIUM",
            "Creating session...",
        )

        options = UiAutomator2Options().load_capabilities(
            {
                "platformName": "Android",
                "appium:automationName": "UiAutomator2",
                "appium:deviceName": "USB Android Phone",
                "appium:udid": serial,
                "appium:noReset": True,
                "appium:fullReset": False,
                "appium:autoLaunch": False,
                "appium:skipUnlock": True,
                "appium:autoGrantPermissions": False,
                "appium:skipLogcatCapture": True,
                "appium:printPageSourceOnFindFailure": False,
                "appium:newCommandTimeout": 60,
            }
        )

        client_config = AppiumClientConfig(
            remote_server_addr=APPIUM_SERVER_URL,
            timeout=120,
        )

        driver = webdriver.Remote(
            options=options,
            client_config=client_config,
        )

        log(
            "APPIUM",
            "Session created",
        )

        # ----------------------------------------------------
        # 6. LAUNCH MAE
        # ----------------------------------------------------

        log(
            "APP",
            "Launching Maybank MAE...",
        )

        driver.execute_script(
            "mobile: startActivity",
            {
                "component": component,
                "action": "android.intent.action.MAIN",
                "categories": ["android.intent.category.LAUNCHER"],
                "wait": True,
                "stop": False,
            },
        )

        time.sleep(5)

        # ----------------------------------------------------
        # 7. CURRENT STATE
        # ----------------------------------------------------

        try:
            log(
                "APP",
                f"Current package: " f"{driver.current_package}",
            )

        except Exception:
            log(
                "APP",
                "Unable to read current package",
            )

        try:
            log(
                "APP",
                f"Current activity: " f"{driver.current_activity}",
            )

        except Exception:
            log(
                "APP",
                "Unable to read current activity",
            )

        # ----------------------------------------------------
        # 8. WARNING CHECK + ELEMENT PRINT
        # ----------------------------------------------------

        acknowledge_mae_warning(driver)

        # ----------------------------------------------------
        # 9. READ-ONLY INSPECTION AFTER THE OPTIONAL CLICK
        # ----------------------------------------------------

        inspect_ui(driver)

        log(
            "SUCCESS",
            "MAE launch and inspection completed",
        )

        return 0

    except KeyboardInterrupt:

        log(
            "ERROR",
            "Interrupted by user",
        )

        return 130

    except Exception as exc:

        log(
            "ERROR",
            str(exc),
        )

        return 1

    finally:

        if driver is not None:

            try:
                driver.quit()

                log(
                    "APPIUM",
                    "Session closed",
                )

            except Exception as exc:

                log(
                    "ERROR",
                    ("Failed to close Appium session: " f"{exc}"),
                )


if __name__ == "__main__":
    sys.exit(main())
