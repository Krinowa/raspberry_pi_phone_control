"""Workflow input contract over the existing Pi manual-input bridge."""

from hardware.pi_api_client import PiApiClient
from hardware.raspberry_pi_input import RaspberryPiInputBridge
from .base import InputBridge, UnsupportedOperation


class RaspberryPiWorkflowInput(InputBridge):
    KEYBOARD_KEYS = {
        "ENTER",
        "BACKSPACE",
        "TAB",
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

    def __init__(self, target):
        # Use the same endpoint for execution and device ownership, even if the
        # process environment changes after selection. Construction does no I/O.
        self.bridge = RaspberryPiInputBridge(
            PiApiClient(target.serial.removeprefix("pi-api:"))
        )

    def tap(self, x, y):
        return self.bridge.tap(x, y)

    def swipe(self, x1, y1, x2, y2, duration_ms=300):
        if type(duration_ms) is not int or not 1 <= duration_ms <= 60000:
            raise ValueError("Swipe duration must be 1–60000 milliseconds.")
        # The existing Pi API accepts geometry only; its HID server owns timing.
        return self.bridge.swipe(x1, y1, x2, y2)

    def type_text(self, text):
        # Empty optional variables were already a no-op in legacy playback.
        if text == "":
            return None
        return self.bridge.type_text(text)

    def key(self, key):
        name = key.upper() if isinstance(key, str) else ""
        navigation = {
            "HOME": self.bridge.home,
            "BACK": self.bridge.back,
            "SCREEN_ON": self.bridge.wake,
        }
        if name in navigation:
            return navigation[name]()
        if name in self.KEYBOARD_KEYS:
            return self.bridge.key("HOME" if name == "MOVE_HOME" else name)
        raise UnsupportedOperation(
            "This workflow key is not supported by Pi input; no input sent."
        )
