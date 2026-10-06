"""Exact documented client-lock fields; preserve raw detail separately."""

from copy import deepcopy
from .balance import final_amount


def normalize_locked_withdrawal(raw):
    if not isinstance(raw, dict):
        raise ValueError("client-lock did not return a withdrawal object.")

    result = {"amount": format(final_amount(raw), ".2f")}
    result["final_amount"] = result["amount"]
    for target in (
        "account_number",
        "account_name",
        "bank_name",
        "holder_name",
        "mobile_no",
    ):
        value = raw.get(target)
        if target in ("holder_name", "mobile_no") and value in (None, ""):
            continue  # Optional unless the local workflow requires it.
        # Account numbers are strings so leading zeroes cannot be lost.
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 256
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError(
                f"Cannot confidently resolve {target}; physical payment is disabled."
            )
        result[target] = value.strip()

    for key in ("tx_id", "order_id"):
        if not isinstance(raw.get(key), str) or not raw[key] or len(raw[key]) > 256:
            raise ValueError(f"Locked withdrawal is missing {key}.")
        result[key] = raw[key]

    if type(raw.get("id")) is not int or raw["id"] <= 0:
        raise ValueError("Locked withdrawal is missing its numeric ID.")
    result["withdraw_id"] = str(raw["id"])
    return result


def retained_withdrawal(raw):
    if not isinstance(raw, dict):
        raise ValueError("Expected a withdrawal detail object.")
    return deepcopy(
        raw
    )  # Including unknown fields; private journal only, never logs/UI.
