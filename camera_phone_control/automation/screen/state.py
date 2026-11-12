"""Provider boundary for future semantic screen-state checks.

Providers may use an Android accessibility service, camera OCR, or deterministic
test data. Workflow code should depend on this interface rather than a capture
or recognition implementation.
"""

import math
import re
import time
from decimal import Decimal, InvalidOperation
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


VERIFY_TEXT_FAILURE = "Text verification failed. Payment confirmation was not sent."


def normalize_verified_text(value):
    """Normalize text without applying fuzzy or locale-specific matching."""
    return " ".join(str(value).split()).casefold()


_JOINT_ACCOUNT_SEPARATOR = re.compile(r"\bo\s*/\s*b\b", re.I)
_MASKED_ACCOUNT_MARKERS = re.compile(r"[*x•●…]", re.I)
_VERIFIED_AMOUNT = re.compile(
    r"(?:(?:RM|MYR)\s*)?(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2})",
    re.I,
)


def _normalized_account_name(value):
    return " ".join(str(value).casefold().replace(",", " ").replace("，", " ").split())


def _masked_name_token_matches(displayed, expected):
    if "*" not in displayed:
        return displayed == expected
    pattern = (
        "^"
        + "".join(".*" if char == "*" else re.escape(char) for char in displayed)
        + "$"
    )
    return re.fullmatch(pattern, expected) is not None


def _account_name_candidate_matches(displayed, expected):
    displayed_name = _normalized_account_name(displayed)
    expected_name = _normalized_account_name(expected)
    if not displayed_name or not expected_name:
        return False
    if "*" not in displayed_name:
        return displayed_name == expected_name

    displayed_tokens = displayed_name.split()
    expected_tokens = expected_name.split()
    meaningful_tokens = [
        token for token in displayed_tokens if any(char.isalpha() for char in token)
    ]
    visible_letters = sum(
        char.isalpha() for token in displayed_tokens for char in token
    )
    if (
        len(meaningful_tokens) < 2
        or visible_letters < 4
        or len(displayed_tokens) != len(expected_tokens)
        or len(meaningful_tokens) != len(displayed_tokens)
    ):
        return False
    return all(
        _masked_name_token_matches(displayed_token, expected_token)
        for displayed_token, expected_token in zip(displayed_tokens, expected_tokens)
    )


def _account_name_matches(visible_texts, expected):
    for candidate in visible_texts:
        holders = _JOINT_ACCOUNT_SEPARATOR.split(str(candidate))
        if any(_account_name_candidate_matches(holder, expected) for holder in holders):
            return True
    return False


def _ocr_candidate_box(candidate):
    box = candidate.get("box") if isinstance(candidate, dict) else None
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        left, top, right, bottom = (float(value) for value in box)
    except (TypeError, ValueError):
        return None
    if (
        not all(math.isfinite(value) for value in (left, top, right, bottom))
        or right <= left
        or bottom <= top
    ):
        return None
    return left, top, right, bottom


def _account_name_line_is_safe(text):
    """Reject lines that clearly are not a name before spatial grouping."""
    value = " ".join(str(text).split())
    if not value or len(value) > 120:
        return False
    without_joint_separator = _JOINT_ACCOUNT_SEPARATOR.sub(" ", value)
    if "/" in without_joint_separator or any(char.isdigit() for char in value):
        return False
    return all(
        char.isalpha()
        or char.isspace()
        or char in {"'", "’", "-", ",", "，", ".", "*", "x", "X", "•", "●", "…"}
        for char in without_joint_separator
    )


def _account_name_lines_are_adjacent(first, second):
    first_box = first[1]
    second_box = second[1]
    first_height = first_box[3] - first_box[1]
    second_height = second_box[3] - second_box[1]
    line_height = max(first_height, second_height)
    vertical_gap = second_box[1] - first_box[3]
    if vertical_gap < (-0.25 * line_height) or vertical_gap > (1.25 * line_height):
        return False

    overlap = min(first_box[2], second_box[2]) - max(first_box[0], second_box[0])
    narrower_width = min(first_box[2] - first_box[0], second_box[2] - second_box[0])
    left_alignment = abs(first_box[0] - second_box[0])
    return overlap >= (0.2 * narrower_width) or left_alignment <= (2 * line_height)


def account_name_ocr_candidates(candidates):
    """Return individual and conservatively reconstructed OCR name lines."""
    individual = []
    positioned = []
    for index, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            continue
        text = " ".join(str(candidate.get("text") or "").split())
        if not text:
            continue
        individual.append(text)
        box = _ocr_candidate_box(candidate)
        if box is not None and _account_name_line_is_safe(text):
            positioned.append((text, box, index))

    positioned.sort(key=lambda item: (item[1][1], item[1][0], item[2]))
    reconstructed = []
    for start in range(len(positioned)):
        first_tokens = _normalized_account_name(positioned[start][0]).split()
        if (
            len(first_tokens) < 2
            and _JOINT_ACCOUNT_SEPARATOR.search(positioned[start][0]) is None
        ):
            continue
        for size in (2, 3):
            window = positioned[start : start + size]
            if len(window) != size:
                continue
            if not all(
                _account_name_lines_are_adjacent(window[index], window[index + 1])
                for index in range(size - 1)
            ):
                continue
            reconstructed.append(" ".join(item[0] for item in window))
    return [*individual, *reconstructed]


def _account_number_matches(visible_texts, expected):
    expected_digits = re.sub(r"\D", "", str(expected))
    if not expected_digits:
        return False
    for value in visible_texts:
        text = str(value)
        visible_digits = re.sub(r"\D", "", text)
        if not visible_digits:
            continue
        masked_or_partial = _MASKED_ACCOUNT_MARKERS.search(text) or (
            len(visible_digits) < len(expected_digits)
        )
        if masked_or_partial:
            if len(visible_digits) >= 4 and expected_digits.endswith(visible_digits):
                return True
        elif visible_digits == expected_digits:
            return True
    return False


def _verified_amount(value):
    text = " ".join(str(value).split())
    match = _VERIFIED_AMOUNT.fullmatch(text)
    if match is None:
        return None
    try:
        amount = Decimal(f"{match.group(1).replace(',', '')}.{match.group(2)}")
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def text_verification_matches(visible_texts, expected, match_mode, last_n=None):
    expected_text = normalize_verified_text(expected)
    if not expected_text:
        return False
    observed = [normalize_verified_text(value) for value in visible_texts]
    observed = [value for value in observed if value]
    complete_text = " ".join(observed)
    if match_mode == "exact":
        return expected_text in observed or expected_text == complete_text
    if match_mode == "contains":
        return expected_text in complete_text
    if match_mode == "last_n_digits":
        expected_digits = re.sub(r"\D", "", str(expected))
        if type(last_n) is not int or last_n <= 0 or len(expected_digits) < last_n:
            return False
        expected_suffix = expected_digits[-last_n:]
        observed_digit_groups = [re.sub(r"\D", "", value) for value in observed]
        observed_digit_groups.append(re.sub(r"\D", "", complete_text))
        return any(
            len(digits) >= last_n and digits[-last_n:] == expected_suffix
            for digits in observed_digit_groups
        )
    if match_mode == "account_name":
        return _account_name_matches(visible_texts, expected)
    if match_mode == "account_number":
        return _account_number_matches(visible_texts, expected)
    if match_mode == "amount":
        expected_amount = _verified_amount(expected)
        return expected_amount is not None and any(
            _verified_amount(value) == expected_amount for value in visible_texts
        )
    return False


def verify_text_on_screen(
    provider,
    context,
    expected,
    timeout_seconds,
    *,
    match_mode,
    last_n=None,
    roi=None,
    interval=0.2,
    clock=time.monotonic,
):
    """Poll OCR until an assertion matches, without exposing expected text."""
    if provider is None:
        provider = UnavailableScreenStateProvider()
    wait_started = time.monotonic()
    started = clock()
    deadline = started + timeout_seconds
    provider_interval = getattr(provider, "poll_interval", None)
    polling_interval = (
        provider_interval
        if isinstance(provider_interval, (int, float)) and provider_interval > 0
        else interval
    )
    try:
        while True:
            context.check_cancelled()
            try:
                if match_mode == "account_name":
                    candidate_reader_name = (
                        "visible_text_candidates"
                        if roi is None
                        else "visible_text_candidates_in_roi"
                    )
                    candidate_reader = getattr(provider, candidate_reader_name, None)
                    if callable(candidate_reader):
                        candidates = (
                            candidate_reader() if roi is None else candidate_reader(roi)
                        )
                        visible = account_name_ocr_candidates(candidates)
                    elif roi is None:
                        visible = provider.visible_texts()
                    else:
                        inspect_roi = getattr(provider, "visible_texts_in_roi", None)
                        if not callable(inspect_roi):
                            raise TextDetectionUnavailable(
                                "The current screen-state provider cannot verify text in a region."
                            )
                        visible = inspect_roi(roi)
                elif roi is None:
                    visible = provider.visible_texts()
                else:
                    inspect_roi = getattr(provider, "visible_texts_in_roi", None)
                    if not callable(inspect_roi):
                        raise TextDetectionUnavailable(
                            "The current screen-state provider cannot verify text in a region."
                        )
                    visible = inspect_roi(roi)
            except TextDetectionUnavailable:
                raise
            except ScreenStateError:
                raise

            if text_verification_matches(visible, expected, match_mode, last_n):
                return
            remaining = deadline - clock()
            if remaining <= 0:
                raise ScreenStateError(VERIFY_TEXT_FAILURE)
            sleep_started = time.monotonic()
            context.cancel_event.wait(min(polling_interval, remaining))
            record_timing("polling_wait", time.monotonic() - sleep_started)
    finally:
        record_timing("wait_for_condition", time.monotonic() - wait_started)


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
