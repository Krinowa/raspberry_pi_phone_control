"""Read one USB phone's geometry and send individual, explicit ADB inputs."""

import logging
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass

LOG = logging.getLogger("android")
KEY_EVENTS = {
    "enter": "KEYCODE_ENTER",
    "backspace": "KEYCODE_DEL",
    "tab": "KEYCODE_TAB",
}


def validate_key(kind, value):
    # A single printable ASCII character, never a string of commands or Unicode.
    if not isinstance(kind, str) or not isinstance(value, str):
        raise ValueError("Invalid keyboard input.")
    if kind == "text" and len(value) == 1 and " " <= value <= "~":
        return
    if kind == "key" and value in KEY_EVENTS:
        return
    raise ValueError("Unsupported keyboard input.")


class DeviceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Device:
    serial: str
    width: int
    height: int
    rotation: int


def parse_size(output):
    sizes = dict(re.findall(r"(Physical|Override) size:\s*(\d+x\d+)", output))
    value = sizes.get("Override", sizes.get("Physical"))

    if not value:
        raise DeviceError("Cannot read screen resolution from adb shell wm size.")
    width, height = map(int, value.split("x"))

    if min(width, height) < 2:
        raise DeviceError("Android reported an invalid screen size.")
    return width, height


class AndroidControl:
    def send_key(self, device, kind, value):
        validate_key(kind, value)
        if kind == "text":
            # adb joins shell arguments on the device. Quote for that remote shell;
            # subprocess's argument list already avoids a local shell. Android's
            # input text interprets %s as space; single-character requests cannot
            # accidentally combine a literal percent with a following s.
            args = ["text", shlex.quote("%s" if value == " " else value)]
        else:
            args = ["keyevent", KEY_EVENTS[value]]
        try:
            self._adb("-s", device.serial, "shell", "input", *args)
        except DeviceError:
            # Do not expose command arguments or device output in logs/errors.
            raise DeviceError(
                "Keyboard input failed. Check the phone and USB connection. "
                "An input may have occurred; it will not be retried."
            ) from None
        # Keyboard values must never enter the normal action log.

    def _adb(self, *args):

        if not shutil.which("adb"):
            raise DeviceError(
                "ADB is missing. Install it with brew install android-platform-tools."
            )

        try:
            result = subprocess.run(
                ["adb", *args], capture_output=True, text=True, timeout=8, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise DeviceError(
                "ADB timed out. An input may have occurred; it will not be retried."
            ) from exc
        except OSError as exc:
            raise DeviceError("Cannot execute ADB; check its installation.") from exc

        if result.returncode:
            raise DeviceError(
                "ADB failed; check the USB cable, phone authorization, and device state."
            )
        return result.stdout.strip()

    def device(self):
        rows = []

        for line in self._adb("devices", "-l").splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0] == "List" or parts[0].startswith("*"):
                continue

            if not parts[0].startswith("emulator-"):
                rows.append(parts)

        if not rows:
            raise DeviceError("No physical phone connected. Connect one phone by USB.")

        if len(rows) != 1:
            raise DeviceError(
                "More than one device connected. Leave only one USB phone attached."
            )
        serial, state, *details = rows[0]

        if state == "unauthorized":
            raise DeviceError(
                "Phone unauthorized. Accept the USB debugging prompt on the phone."
            )

        if state != "device":
            raise DeviceError(
                f"Phone is {state}. Check USB and debugging authorization."
            )

        if not any(item.startswith("usb:") for item in details):
            raise DeviceError(
                "Only a physical USB transport is supported; disconnect wireless ADB."
            )

        if self._adb("-s", serial, "shell", "getprop", "ro.kernel.qemu") == "1":
            raise DeviceError("Emulators are not supported.")
        width, height = parse_size(self._adb("-s", serial, "shell", "wm", "size"))
        display = self._adb("-s", serial, "shell", "dumpsys", "input")
        match = re.search(r"SurfaceOrientation:\s*([0-3])\b", display)

        if match:
            rotation = int(match[1])
        else:
            display = self._adb("-s", serial, "shell", "dumpsys", "window", "displays")
            match = re.search(r"mRotation=(?:ROTATION_)?(270|180|90|[0-3])\b", display)
            if not match:
                raise DeviceError(
                    "Cannot determine phone rotation. This Android build needs a rotation parser update."
                )
            value = int(match[1])
            rotation = value // 90 if value >= 90 else value

        if rotation % 2:
            width, height = height, width

        return Device(serial, width, height, rotation)

    def send(self, device, action, pixel=None):
        if action == "tap" and pixel is not None:
            x, y = pixel

            if not (0 <= x < device.width and 0 <= y < device.height):
                raise ValueError("Tap is outside the Android display.")
            args = ["tap", str(x), str(y)]

        elif action in {"back", "home"}:
            args = ["keyevent", "4" if action == "back" else "3"]

        elif action == "screen_on":
            args = ["keyevent", "KEYCODE_WAKEUP"]

        elif action == "screen_off":
            args = ["keyevent", "KEYCODE_SLEEP"]

        elif action == "recents":
            args = ["keyevent", "KEYCODE_APP_SWITCH"]

        # ========================================================
        # VERTICAL SWIPES
        # ========================================================
        elif action in {"swipe_up", "swipe_down"}:
            x = round((device.width - 1) * 0.5)
            top, bottom = [
                round((device.height - 1) * fraction) for fraction in (0.25, 0.75)
            ]
            start, end = (bottom, top) if action == "swipe_up" else (top, bottom)
            args = ["swipe", str(x), str(start), str(x), str(end), "300"]

        # ========================================================
        # HORIZONTAL SWIPES
        # ========================================================
        elif action in {"swipe_left", "swipe_right"}:
            y = round((device.height - 1) * 0.5)
            left = round((device.width - 1) * 0.25)
            right = round((device.width - 1) * 0.75)

            if action == "swipe_left":
                start_x = right
                end_x = left

            else:
                start_x = left
                end_x = right

            args = [
                "swipe",
                str(start_x),
                str(y),
                str(end_x),
                str(y),
                "300",
            ]

        else:
            raise ValueError("Unsupported control action.")
        self._adb("-s", device.serial, "shell", "input", *args)
        LOG.info(
            "Manual %s on %s%s", action, device.serial, f" at {pixel}" if pixel else ""
        )
