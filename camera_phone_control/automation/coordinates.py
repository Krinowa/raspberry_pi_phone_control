"""Shared normalized display coordinate conversions (inclusive edges)."""

import math


def unit(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ValueError("Coordinates must be finite numbers between 0 and 1.")
    return value


def dimensions(width, height):
    if any(type(v) is not int or v < 2 for v in (width, height)):
        raise ValueError("Display dimensions must be integers of at least 2 pixels.")


def normalized_to_device(x_ratio, y_ratio, width, height):
    dimensions(width, height)
    return tuple(
        math.floor(unit(v) * (size - 1) + 0.5 + 1e-9)
        for v, size in ((x_ratio, width), (y_ratio, height))
    )


def device_to_normalized(x, y, width, height):
    dimensions(width, height)
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
        for v in (x, y)
    ):
        raise ValueError("Device coordinates must be finite numbers.")
    return unit(x / (width - 1)), unit(y / (height - 1))


def normalized_roi(value):
    """Validate a screen-relative rectangle; retain legacy array compatibility."""
    if isinstance(value, dict):
        if set(value) != {"x", "y", "width", "height"}:
            raise ValueError("Text ROI requires x, y, width, height.")
        value = [value[key] for key in ("x", "y", "width", "height")]
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("Text ROI must be [x, y, width, height] or an object.")
    try:
        x, y, width, height = [unit(v) for v in value]
    except ValueError:
        raise ValueError(
            "Text ROI values must be finite numbers between 0 and 1."
        ) from None
    if (
        x >= 1
        or y >= 1
        or width <= 0
        or height <= 0
        or x + width > 1 + 1e-12
        or y + height > 1 + 1e-12
    ):
        raise ValueError(
            "Text ROI must have positive size and remain within the screen."
        )
    return [x, y, min(width, 1 - x), min(height, 1 - y)]
