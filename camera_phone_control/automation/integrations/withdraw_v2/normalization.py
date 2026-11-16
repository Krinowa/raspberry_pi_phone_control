"""Exact documented client-lock fields; preserve raw detail separately."""

from copy import deepcopy
from automation.transfer_methods import (
    validate_recipient_identifier,
    validate_transfer_method,
)
from .balance import final_amount


def normalize_locked_withdrawal(raw):
    if not isinstance(raw, dict):
        raise ValueError("client-lock did not return a withdrawal object.")

    method = validate_transfer_method(raw.get("transfer_method"))
    identifier_field, identifier = validate_recipient_identifier(raw, method)
    result = {
        "amount": format(final_amount(raw), ".2f"),
        "transfer_method": method,
        identifier_field: identifier,
    }
    # Preserve another documented identifier when the provider includes it, but
    # never use its presence to select the transfer method.
    for other_field in ("account_number", "mobile_no"):
        if other_field == identifier_field or raw.get(other_field) in (None, ""):
            continue
        value = raw[other_field]
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 256
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError(
                f"Cannot confidently resolve {other_field}; physical payment is disabled."
            )
        result[other_field] = value.strip()
    result["final_amount"] = result["amount"]
    for target in (
        "account_name",
        "bank_name",
        "holder_name",
    ):
        value = raw.get(target)
        if target == "holder_name" and value in (None, ""):
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
