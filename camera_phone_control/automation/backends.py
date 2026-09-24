"""Backend selection and context-owned guards. Defaults preserve current input."""

import os
from android_control import Device, DeviceError
from .input.appium import AppiumInputBridge
from .input.mock import MockInputBridge
from .input.raspberry_pi import RaspberryPiWorkflowInput
from hardware.raspberry_pi_input import RaspberryPiInputBridge
from .screen.appium import AppiumScreenProvider, DeferredAppiumScreenProvider
from .screen.base import ScreenProvider


class BackendConfigurationError(ValueError):
    pass


def backend_names():
    input_name = os.environ.get("PHONE_INPUT_BACKEND", "raspberry_pi")
    screen_name = os.environ.get("PHONE_SCREEN_BACKEND", "appium")
    if input_name not in {"appium", "mock", "raspberry_pi"}:
        raise BackendConfigurationError(
            "Unsupported PHONE_INPUT_BACKEND; use appium, raspberry_pi or mock."
        )
    if screen_name not in {"appium", "mock"}:
        raise BackendConfigurationError(
            "Unsupported PHONE_SCREEN_BACKEND; use appium or mock."
        )
    return input_name, screen_name


def select_device(
    controller,
    serial=None,
    *,
    authorized_only=True,
    input_backend=None,
    device_id=None,
):
    input_name = input_backend or backend_names()[0]
    if input_name == "raspberry_pi":
        target = RaspberryPiInputBridge(device_id=device_id).target
        if serial is not None and serial != target.serial:
            raise BackendConfigurationError(
                "Pi input uses PI_API_BASE_URL, not a USB serial."
            )
        return target
    if input_name == "mock":
        return Device("mock", 1080, 1920, 0)
    return controller.device(serial=serial, authorized_only=authorized_only)


class GuardedInput:
    """Expose only actions that run within the owning AutomationContext."""

    def __init__(self, context, backend):
        self._context, self._backend = context, backend

    def _call(self, name, *args):
        label = {
            "key": "keyboard input",
            "type_text": "keyboard input",
            "action": "navigation",
            "launch_app": "launch app",
        }.get(name, "input " + name)
        with self._context._step(label):
            self._context._check_device()
            return getattr(self._backend, name)(*args)

    def tap(self, x, y):
        return self._call("tap", x, y)

    def move(self, dx, dy):
        return self._call("move", dx, dy)

    def click(self):
        return self._call("click")

    def swipe(self, x1, y1, x2, y2, duration_ms=300):
        return self._call("swipe", x1, y1, x2, y2, duration_ms)

    def key(self, key):
        return self._call("key", key)

    def type_text(self, text):
        return self._call("type_text", text)

    def action(self, action, point=None):
        return self._call("action", action, point)

    def launch_app(self, package):
        return self._call("launch_app", package)


class GuardedScreen:
    def __init__(self, context, backend):
        self._context, self._backend = context, backend

    def _call(self, name):
        with self._context._step(
            "inspect ui" if name == "inspect" else "capture screen"
        ):
            self._context._check_device()
            return getattr(self._backend, name)()

    def capture(self):
        return self._call("capture")

    def inspect(self):
        return self._call("inspect")


def bind(context, input_backend=None):
    configured_input, screen_name = backend_names()
    input_name = input_backend or configured_input

    def check_ready():
        if input_name in {"mock", "raspberry_pi"}:
            return
        current = context.controller.device(
            serial=context.device.serial, authorized_only=True
        )
        if current != context.device:
            raise DeviceError("Phone geometry changed. Script stopped.")

    context._health_check = check_ready

    if input_name == "mock":
        bridge = MockInputBridge(context.logger)
    elif input_name == "raspberry_pi":
        bridge = RaspberryPiWorkflowInput(context.device)
    else:
        bridge = AppiumInputBridge(
            context.controller, context.device, context._check_device
        )

    # Mock input never touches hardware, even if the default screen name is appium.
    if input_name == "mock" or screen_name == "mock":
        screen = ScreenProvider()
    elif input_name == "raspberry_pi":
        # Keep USB screen handling independent; input-only runs never discover USB.
        screen = DeferredAppiumScreenProvider(context.controller)
    else:
        screen = AppiumScreenProvider(context.controller, context.device)
    return input_name, GuardedInput(context, bridge), GuardedScreen(context, screen)
