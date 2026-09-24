"""Manual input adapter for the existing Bluetooth HID API, without ADB discovery."""

from dataclasses import dataclass

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

    def __init__(self, client=None):
        self.client = client if client is not None else PiApiClient()
        self.target = PiControlTarget("pi-api:" + self.client.base_url)

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
        try:
            connected = self.status()["connected"]
        except PiApiError as exc:
            return {"api": "offline", "phone": "unknown", "message": str(exc)}
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
