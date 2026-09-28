"""Provider boundary for future semantic screen-state checks.

Providers may use an Android accessibility service, camera OCR, or deterministic
test data. Workflow code should depend on this interface rather than a capture
or recognition implementation.
"""

import time
from typing import Protocol, runtime_checkable

from ..workflows.timing import record_timing


class ScreenStateError(RuntimeError):
    """Safe, user-facing semantic screen-state failure."""


class TextDetectionUnavailable(ScreenStateError):
    """The selected provider cannot inspect visible text."""


@runtime_checkable
class ScreenStateProvider(Protocol):
    def contains_text(self, text: str) -> bool:
        """Return whether visible UI text contains ``text``, ignoring case.

        Implementations perform literal substring matching. They must not apply
        fuzzy matching or interpret the value as a regular expression.
        """

    def visible_texts(self) -> list[str]:
        """Return currently visible, non-empty UI text strings.

        Used for explicitly requested OCR suggestions during recording review.
        Live physical actions must not invoke text detection.
        """


class UnavailableScreenStateProvider:
    @staticmethod
    def _raise_unavailable():
        raise TextDetectionUnavailable(
            "Text detection is not available for the current screen-state provider."
        )

    def contains_text(self, text: str) -> bool:
        self._raise_unavailable()

    def visible_texts(self) -> list[str]:
        self._raise_unavailable()


def wait_for_text(
    provider,
    context,
    expected_text,
    timeout_seconds,
    *,
    interval=0.2,
    clock=time.monotonic,
):
    """Poll a provider without coupling workflow execution to its data source."""
    if provider is None:
        provider = UnavailableScreenStateProvider()

    wait_started = time.monotonic()
    deadline = clock() + timeout_seconds

    try:
        while True:
            context.check_cancelled()

            record_timing("polling_wait", 0.0, count=1)
            if provider.contains_text(expected_text):
                return

            remaining = deadline - clock()

            if remaining <= 0:
                raise ScreenStateError(
                    f'Text "{expected_text}" was not detected within '
                    f"{timeout_seconds:g} seconds."
                )

            sleep_started = time.monotonic()
            context.cancel_event.wait(min(interval, remaining))
            record_timing("polling_wait", time.monotonic() - sleep_started)
    finally:
        record_timing("wait_for_condition", time.monotonic() - wait_started)
