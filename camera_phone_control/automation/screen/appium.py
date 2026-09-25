"""Current Android screenshot (PNG bytes) and UIAutomator XML provider."""

from .base import ScreenProvider


class AppiumScreenProvider(ScreenProvider):
    def __init__(self, controller, device):
        self.controller, self.device = controller, device

    def capture(self):
        return self.controller.screenshot(self.device)

    def inspect(self):
        return self.controller.dump_ui(self.device)


class DeferredAppiumScreenProvider(ScreenProvider):
    """Resolve the existing USB screen device only when a screen read is requested."""

    def __init__(self, controller):
        self.controller = controller

    def _provider(self):
        device = self.controller.device(authorized_only=True)
        return AppiumScreenProvider(self.controller, device)

    def capture(self):
        return self._provider().capture()

    def inspect(self):
        return self._provider().inspect()
