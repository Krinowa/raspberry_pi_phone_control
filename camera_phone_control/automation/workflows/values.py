"""Conservative value extraction through the existing calibrated OCR provider."""

import re
from decimal import Decimal

from automation.coordinates import normalized_roi
from automation.screen.state import ScreenStateError

_AMOUNT = re.compile(r"(?<![\w.,+-])(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}(?![\w.,])")
_PREFIX = re.compile(
    r"^(?:(?:Available\s+Balance|Balance)\s*:?\s*)?(?:RM|MYR)?\s*$", re.I
)


def parse_currency(value):
    """Accept one unambiguous nonnegative MYR-style amount; never repair OCR."""
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError("Currency OCR must contain one readable amount.")

    text = " ".join(value.split())
    # Currency prefixes may touch the digits (RM8230.50).
    text = re.sub(r"\b(RM|MYR)(?=\d)", r"\1 ", text, flags=re.I)
    matches = list(_AMOUNT.finditer(text))

    if len(matches) != 1:
        raise ValueError("Currency OCR must contain exactly one unambiguous amount.")
    match = matches[0]
    if not _PREFIX.fullmatch(text[: match.start()]) or text[match.end() :].strip():
        raise ValueError("Currency OCR contains unexpected or malformed text.")

    amount = Decimal(match.group().replace(",", ""))

    if not amount.is_finite() or amount < 0:
        raise ValueError("Currency OCR must be finite and nonnegative.")
    return amount


def read_currency(provider, roi, context):
    """Read only the selected calibrated crop; share the normal OCR pool."""
    context.check_cancelled()
    region = normalized_roi(roi)
    if not all(
        callable(getattr(provider, name, None))
        for name in (
            "capture_snapshot",
            "_crop_roi",
            "visible_texts_from_image",
        )
    ):
        raise ScreenStateError(
            "Read Balance requires a calibrated camera OCR provider."
        )

    image = provider.capture_snapshot()
    context.check_cancelled()
    crop = provider._crop_roi(image, region)
    texts = provider.visible_texts_from_image(crop)
    context.check_cancelled()
    try:
        return parse_currency(" ".join(texts))
    except ValueError as exc:
        raise ScreenStateError(str(exc)) from None
