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
    roi=None,
    clock=time.monotonic,
    required=True,
):
    """Poll a provider without coupling workflow execution to its data source."""
    if provider is None:
        provider = UnavailableScreenStateProvider()

    wait_started = time.monotonic()
    started = clock()
    deadline = started + timeout_seconds
    last_ocr_at = started
    optimized_poll = getattr(provider, "poll_contains_text", None)
    forced_interval = getattr(provider, "forced_ocr_interval", None)
    provider_interval = getattr(provider, "poll_interval", None)
    polling_interval = (
        provider_interval
        if isinstance(provider_interval, (int, float)) and provider_interval > 0
        else interval
    )

    roi_options = {}
    if roi is not None and callable(getattr(provider, "new_roi_wait", None)):
        roi_options["roi_state"] = provider.new_roi_wait()

    try:
        while True:
            context.check_cancelled()

            record_timing("polling_wait", 0.0, count=1)
            if (
                callable(optimized_poll)
                and isinstance(forced_interval, (int, float))
                and forced_interval > 0
            ):
                now = clock()
                checked, found = optimized_poll(
                    expected_text,
                    roi=roi,
                    force=now - last_ocr_at >= forced_interval,
                    **roi_options,
                )
                if checked:
                    last_ocr_at = clock()
            else:
                found = provider.contains_text(expected_text)

            if found:
                return True if not required else None

            remaining = deadline - clock()

            if remaining <= 0:
                if not required:
                    return False
                raise ScreenStateError(
                    f'Text "{expected_text}" was not detected within '
                    f"{timeout_seconds:g} seconds."
                )

            sleep_started = time.monotonic()
            context.cancel_event.wait(min(polling_interval, remaining))
            record_timing("polling_wait", time.monotonic() - sleep_started)
    finally:
        record_timing("wait_for_condition", time.monotonic() - wait_started)
