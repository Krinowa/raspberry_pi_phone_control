"""Script helpers: shared controller, explicit waits, and cooperative cancellation."""

import math
import time
from contextlib import contextmanager

from android_control import validate_key
from .backends import bind
from .coordinates import device_to_normalized


class Cancelled(Exception):
    def __init__(self, takeover=False):
        self.takeover = takeover


class AutomationContext:
    def __init__(
        self,
        device,
        controller,
        cancel_event,
        logger,
        state,
        takeover_requested=None,
        *,
        input_backend=None
    ):
        self.device = device
        self.controller = controller
        self.cancel_event = cancel_event
        self.logger = logger
        self.state = state
        self.takeover_requested = takeover_requested or (lambda: False)
        self.cancelled_for_takeover = False
        self.input_backend, self.input, self.screen = bind(
            self, input_backend=input_backend
        )
        self.manual_handler = None

    def check_cancelled(self):
        if self.takeover_requested():
            if not self.cancelled_for_takeover:
                self.state.status = "takeover_requested"
                self.state.takeover_requested = True
                self.state.message = (
                    "Explicit takeover requested. Cancelling at a safe point."
                )
                self.logger.info(
                    "Cancellation detected: explicit takeover on %s", self.device.serial
                )
            self.cancelled_for_takeover = True
            self.cancel_event.set()  # Latch cancellation; never resume this run.
        if self.cancel_event.is_set():
            raise Cancelled(takeover=self.cancelled_for_takeover)

    @contextmanager
    def _step(self, name):
        self.check_cancelled()
        self.state.current_step = name
        self.logger.info("Step started: %s", name)
        yield
        self.check_cancelled()
        self.logger.info("Step completed: %s", name)

    def _check_device(self):
        self.check_cancelled()
        self._health_check()
        self.check_cancelled()

    def send(self, action, pixel=None):
        point = None
        if action == "tap" and pixel is not None:
            point = device_to_normalized(*pixel, self.device.width, self.device.height)
        return self.input.action(action, point)

    def send_key(self, kind, value):
        validate_key(kind, value)
        return self.input.type_text(value) if kind == "text" else self.input.key(value)

    def launch_app(self, package):
        return self.input.launch_app(package)

    def manual_checkpoint(self, message):
        self.check_cancelled()
        if self.manual_handler is None:
            raise RuntimeError("This runner does not support manual checkpoints.")
        self.manual_handler(message)
        self.check_cancelled()

    def wait(self, seconds):
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or seconds < 0
        ):
            raise ValueError("Wait duration must be a finite nonnegative number.")
        with self._step("wait"):
            deadline = time.monotonic() + seconds
            while True:
                self.check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # The file signal works across Flask/runner processes; poll it
                # frequently while retaining instant local Ctrl+C cancellation.
                if self.cancel_event.wait(min(remaining, 0.05)):
                    raise Cancelled()

    def dump_ui(self):
        return self.screen.inspect()
