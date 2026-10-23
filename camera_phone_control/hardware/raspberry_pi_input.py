"""Manual input adapter for the existing Bluetooth HID API, without ADB discovery."""

from dataclasses import dataclass

from .device_config import DEFAULT_DEVICE_ID, get_device_config
from .pi_api_client import PiApiClient, PiApiError


@dataclass(frozen=True)
class PiControlTarget:
    # Compatibility with ControlState's existing lock/signature machinery.
    # This is an endpoint identity, NOT an Android serial or pixel geometry.
    serial: str
    label: str = "Raspberry Pi Bluetooth HID"
    normalized: bool = True
    width: None = None
    height: None = None
    rotation: None = None
    device_id: str = DEFAULT_DEVICE_ID


class RaspberryPiInputBridge:
    ACTIONS = {
        "tap",
        "swipe_up",
        "swipe_down",
        "swipe_left",
        "swipe_right",
        "home",
        "back",
        "screen_on",
    }

    SWIPES = {
        "swipe_up": (0.5, 0.75, 0.5, 0.25),
        "swipe_down": (0.5, 0.25, 0.5, 0.75),
        "swipe_left": (0.75, 0.5, 0.25, 0.5),
        "swipe_right": (0.25, 0.5, 0.75, 0.5),
    }

    def __init__(self, client=None, *, device_id=None):
        if client is None:
            client = PiApiClient(device_id=device_id)
        client_device_id = getattr(client, "device_id", None)
        if not isinstance(client_device_id, str):
            client_device_id = get_device_config(device_id).device_id
        elif device_id is not None and client_device_id != device_id:
            raise ValueError(
                f"Pi client belongs to {client_device_id!r}, not {device_id!r}."
            )

        self.client = client
        self.device_config = get_device_config(
            client_device_id,
            pi_base_url=self.client.base_url,
        )
        self.device_id = self.device_config.device_id
        self._set_target()

    def _set_target(self):
        # Keep phone_A's persisted calibration and lock identity unchanged.
        serial = "pi-api:" + self.client.base_url
        if self.device_id != DEFAULT_DEVICE_ID:
            serial += "#" + self.device_id
        self.target = PiControlTarget(serial, device_id=self.device_id)

    def select_device(self, device_id):
        self.client.select_device(device_id)
        self.device_config = get_device_config(
            device_id,
            pi_base_url=self.client.base_url,
        )
        self.device_id = self.device_config.device_id
        self._set_target()

    def clear_device(self):
        self.device_config = None
        self.device_id = None
        self.target = None

    def devices(self):
        return self.client.devices()

    def enrollment(self):
        return self.client.enrollment()

    def start_enrollment(self, device_id):
        return self.client.start_enrollment(device_id)

    def stop_enrollment(self):
        return self.client.stop_enrollment()

    def replace_device(self, device_id):
        return self.client.replace_device(device_id)

    def delete_device(self, device_id):
        return self.client.delete_device(device_id)

    def device_status(self, device_id):
        return self.client.device_status(device_id)

    @property
    def last_command_id(self):
        return self.client.last_command_id

    @property
    def last_action_status(self):
        return self.client.last_action_status

    @property
    def last_action_http_status(self):
        return self.client.last_action_http_status

    def tap(self, x, y):
        return self.client.tap(x, y)

    def swipe(self, x1, y1, x2, y2):
        return self.client.swipe(x1, y1, x2, y2)

    def type_text(self, text):
        return self.client.type_text(text)

    def key(self, key):
        return self.client.key(key)

    def home(self):
        return self.client.home()

    def back(self):
        return self.client.back()

    def wake(self):
        return self.client.wake()

    def health(self):
        return self.client.health()

    def status(self):
        return self.client.status()

    def connection_state(self):
        if self.device_id is None:
            return {
                "api": "online",
                "phone": "waiting",
                "message": "Pi Online / No registered phone selected",
            }
        try:
            connected = self.status()["connected"]
        except PiApiError as exc:
            result = {"api": "offline", "phone": "unknown", "message": str(exc)}
            if exc.error_code:
                result["error_code"] = exc.error_code
                result["error_params"] = exc.error_params
            return result
        return {
            "api": "online",
            "phone": "connected" if connected else "waiting",
            "message": "Phone Connected" if connected else "Pi Online / Phone Waiting",
        }

    def action(self, action, point=None):
        if action not in self.ACTIONS:
            raise ValueError(
                "This action is not supported by the Pi API; no input sent."
            )
        if action == "tap":
            if point is None:
                raise ValueError("A calibrated phone coordinate is required.")
            return self.tap(*point)
        if action in self.SWIPES:
            return self.swipe(*self.SWIPES[action])
        return {"home": self.home, "back": self.back, "screen_on": self.wake}[action]()
