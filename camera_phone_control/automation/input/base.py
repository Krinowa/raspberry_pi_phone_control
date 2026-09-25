"""Backend-independent input contract. Tap/swipe coordinates are normalized."""


class UnsupportedOperation(NotImplementedError):
    pass


class InputBridge:
    def tap(self, x, y):
        raise UnsupportedOperation("Tap is not supported by this input backend.")

    def move(self, dx, dy):
        raise UnsupportedOperation("Relative pointer motion is not supported.")

    def click(self):
        raise UnsupportedOperation("Pointer click is not supported.")

    def swipe(self, x1, y1, x2, y2, duration_ms=300):
        raise UnsupportedOperation("Swipe is not supported.")

    def key(self, key):
        raise UnsupportedOperation("Key input is not supported.")

    def type_text(self, text):
        raise UnsupportedOperation("Text input is not supported.")

    def launch_app(self, package):
        raise UnsupportedOperation("Application launch is not supported.")

    def action(self, action, point=None):
        # Compatibility for scripts that still use named navigation actions.
        if action == "tap":
            return self.tap(*point)
        swipes = {
            "swipe_up": (0.5, 0.75, 0.5, 0.25),
            "swipe_down": (0.5, 0.25, 0.5, 0.75),
            "swipe_left": (0.75, 0.5, 0.25, 0.5),
            "swipe_right": (0.25, 0.5, 0.75, 0.5),
        }
        if action in swipes:
            return self.swipe(*swipes[action])
        return self.key(action)
