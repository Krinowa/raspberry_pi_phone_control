"""No phone input. Text payloads are deliberately excluded from logs/history."""

import math
from .base import InputBridge
from ..coordinates import unit


class MockInputBridge(InputBridge):
    def __init__(self, logger):
        self.logger = logger
        self.commands = []

    def emit(self, operation, **details):
        command = {"type": operation, **details}
        self.commands.append(command)
        self.logger.info("MOCK %s %s", operation.upper(), details)

    def tap(self, x, y):
        self.emit("tap", x=unit(x), y=unit(y))

    def swipe(self, x1, y1, x2, y2, duration_ms=300):
        if type(duration_ms) is not int or not 1 <= duration_ms <= 60000:
            raise ValueError("Swipe duration must be 1–60000 milliseconds.")
        self.emit(
            "swipe",
            start=[unit(x1), unit(y1)],
            end=[unit(x2), unit(y2)],
            duration_ms=duration_ms,
        )

    def move(self, dx, dy):
        if any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in (dx, dy)
        ):
            raise ValueError("Pointer deltas must be finite numbers.")
        self.emit("move", dx=dx, dy=dy)

    def click(self):
        self.emit("click")

    def key(self, key):
        if not isinstance(key, str) or not key:
            raise ValueError("A key name is required.")
        self.emit("key", key=key.upper())

    def type_text(self, text):
        if not isinstance(text, str):
            raise ValueError("Text must be a string.")
        self.emit("input", characters=len(text))

    def launch_app(self, package):
        self.emit("launch_app")
