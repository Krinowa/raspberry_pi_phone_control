"""Deterministic, on-demand OCR suggestions; no action learning or polling."""

import math
import re

MIN_SUGGESTION_CONFIDENCE = 60.0


def normalize_text(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def is_good_anchor(value):
    value = normalize_text(value)
    if not 4 <= len(value) <= 80:
        return False
    letters = sum(c.isalpha() for c in value)
    if letters / len(value) < 0.65 or sum(c.isdigit() for c in value) > letters:
        return False
    words = value.split()
    if len(words) > 12 or re.search(r"(.)\1{3,}", value, re.I):
        return False
    # Fragmented single-character OCR and symbol-heavy tokens are rarely labels.
    if sum(len(w) == 1 for w in words) > max(1, len(words) / 2):
        return False
    if any(sum(c.isalpha() for c in w) == 0 and w not in {"&", "-"} for w in words):
        return False
    return True


def text_suggestions(values, min_confidence=MIN_SUGGESTION_CONFIDENCE):
    candidates = []
    positions = {}

    for position, raw in enumerate(values):
        confidence = None

        if isinstance(raw, dict):
            value = normalize_text(raw.get("text"))
            confidence = raw.get("confidence")

            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence)
            ):
                continue

            if confidence < min_confidence:
                continue
        else:
            value = normalize_text(raw)

        key = "".join(value.casefold().split())

        if not is_good_anchor(value):
            continue

        candidate = {
            "text": value,
            "confidence": confidence,
            "position": positions.setdefault(key, position),
        }

        existing = next((item for item in candidates if item["key"] == key), None)

        if existing is not None:
            if confidence is not None and (
                existing["confidence"] is None or confidence > existing["confidence"]
            ):
                existing.update(candidate)
            continue

        candidate["key"] = key
        candidates.append(candidate)

    # Confidence has already removed untrustworthy OCR. Rank the survivors by
    # UI-label readability, then confidence, with screen order as the final
    # tie-breaker. Fallback strings retain the same readability behavior.
    candidates.sort(
        key=lambda item: (
            (
                0
                if len(item["text"].split()) <= 4
                else 1 if len(item["text"].split()) <= 6 else 2
            ),
            len(item["text"]),
            -sum(c.isalpha() for c in item["text"]) / len(item["text"]),
            -(item["confidence"] if item["confidence"] is not None else min_confidence),
            item["position"],
        )
    )

    return [item["text"] for item in candidates[:5]]
