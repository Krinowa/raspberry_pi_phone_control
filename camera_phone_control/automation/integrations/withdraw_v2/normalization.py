"""Exact documented client-lock fields; preserve raw detail separately."""

from copy import deepcopy
from automation.transfer_methods import (
    transfer_method_definition,
    validate_transfer_method_payload,
    validate_transfer_method,
)
from .balance import final_amount


def normalize_locked_withdrawal(raw):
    if not isinstance(raw, dict):
        raise ValueError("client-lock did not return a withdrawal object.")

    method = validate_transfer_method(raw.get("transfer_method"))
    identifier_field, identifier, name_field, recipient_name = (
        validate_transfer_method_payload(raw, method, require_recipient_name=True)
    )
    definition = transfer_method_definition(method)
    result = {
        "amount": format(final_amount(raw), ".2f"),
        "transfer_method": method,
        identifier_field: identifier,
        definition["workflow_variable"]: identifier,
        name_field: recipient_name,
    }
    result["final_amount"] = result["amount"]
    if definition["api_type"] == "bank":
        result["bank_name"] = raw["bank_name"]

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
