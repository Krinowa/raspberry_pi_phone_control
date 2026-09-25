"""Current backend: reuse AndroidControl's ADB transport; no new Appium session."""

from .base import InputBridge
from ..coordinates import normalized_to_device

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


def validate_text(text):
    if (
        not isinstance(text, str)
        or len(text) > 4096
        or any(not " " <= ch <= "~" for ch in text)
    ):
        raise ValueError(
            "Current text input supports up to 4096 printable ASCII characters."
        )


class AppiumInputBridge(InputBridge):
    def __init__(self, controller, device, check_ready):
        self.controller, self.device, self.check_ready = controller, device, check_ready

    def tap(self, x, y):
        self.controller.send(
            self.device,
            "tap",
            normalized_to_device(x, y, self.device.width, self.device.height),
        )

    def swipe(self, x1, y1, x2, y2, duration_ms=300):
        start = normalized_to_device(x1, y1, self.device.width, self.device.height)
        end = normalized_to_device(x2, y2, self.device.width, self.device.height)
        self.controller.swipe(self.device, start, end, duration_ms)

    def key(self, key):
        if not isinstance(key, str) or key.upper() not in KEYS:
            raise ValueError("Unsupported key.")
        key = key.lower()
        if key == "move_home":
            self.controller.send_key(self.device, "key", "home")
        elif key in {
            "enter",
            "backspace",
            "tab",
            "left",
            "right",
            "up",
            "down",
            "delete",
            "end",
            "pageup",
            "pagedown",
        }:
            self.controller.send_key(self.device, "key", key)
        else:
            self.controller.send(self.device, key)

    def type_text(self, text):
        validate_text(text)  # Validate all text before sending any characters.
        for char in text:
            self.check_ready()  # Cancellation/takeover also between characters.
            self.controller.send_key(self.device, "text", char)

    def action(self, action, point=None):
        pixel = (
            normalized_to_device(*point, self.device.width, self.device.height)
            if action == "tap"
            else None
        )
        self.controller.send(self.device, action, pixel)

    def launch_app(self, package):
        self.controller.launch_app(self.device, package)
