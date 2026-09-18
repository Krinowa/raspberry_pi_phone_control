"""Current Android screenshot (PNG bytes) and UIAutomator XML provider."""

from .base import ScreenProvider


class AppiumScreenProvider(ScreenProvider):
    def __init__(self, controller, device):
        self.controller, self.device = controller, device

    def capture(self):
        return self.controller.screenshot(self.device)

    def inspect(self):
        return self.controller.dump_ui(self.device)
