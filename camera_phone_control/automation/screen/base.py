"""Screen acquisition contract; capabilities may differ by provider."""

from ..input.base import UnsupportedOperation


class ScreenProvider:
    def capture(self):
        raise UnsupportedOperation(
            "Image capture is unavailable for this screen provider."
        )

    def inspect(self):
        raise UnsupportedOperation(
            "Structured inspection is unavailable for this screen provider."
        )
