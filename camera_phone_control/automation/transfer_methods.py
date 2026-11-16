"""Validated startup registry for withdrawal transfer methods."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re

TRANSFER_METHODS_FILE_ENV = "PHONE_TRANSFER_METHODS_FILE"
_METHOD_VALUE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MAXIMUM_FILE_SIZE = 64 * 1024
_PROFILE_FIELDS = {
    "account_number": "account_number",
    "mobile_no": "mobile_no",
}


def _definition(value, label, profile):
    return {
        "value": value,
        "label": label,
        "identifier_profile": profile,
        "identifier_field": _PROFILE_FIELDS[profile],
        "validation": {
            "type": "nonempty_text",
            "max_length": 256,
            "preserve_leading_zeroes": True,
        },
    }


BUILTIN_TRANSFER_METHODS = {
    "bank_account": _definition(
        "bank_account", "Bank Account Transfer", "account_number"
    ),
    "mobile_number": _definition(
        "mobile_number", "Mobile Number Transfer", "mobile_no"
    ),
}

# Keep this object stable because workflow modules import it directly. Startup
# loading replaces its contents before workers or workflow runs are created.
TRANSFER_METHODS = deepcopy(BUILTIN_TRANSFER_METHODS)


class TransferMethodConfigurationError(ValueError):
    pass


def transfer_methods_config_path(environment=None):
    environment = os.environ if environment is None else environment
    override = environment.get(TRANSFER_METHODS_FILE_ENV)
    if override:
        return Path(override).expanduser().resolve()
    data_root = environment.get("PHONE_APP_DATA_DIR")
    if data_root:
        return (
            Path(data_root).expanduser().resolve() / "config" / "transfer_methods.json"
        )
    from windows_runtime import default_data_root

    return default_data_root(environment) / "config" / "transfer_methods.json"


def _safe_label(value):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 100
        and value == value.strip()
        and all(character.isprintable() for character in value)
    )


def _load_configured_methods(path):
    if not path.exists():
        return {}
    if path.is_symlink():
        raise TransferMethodConfigurationError(
            "Transfer-method configuration must not be a symbolic link."
        )
    if not path.is_file() or path.stat().st_size > _MAXIMUM_FILE_SIZE:
        raise TransferMethodConfigurationError(
            "Transfer-method configuration must be a regular JSON file of at most 64 KiB."
        )
    try:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TransferMethodConfigurationError(
            f"Transfer-method configuration is invalid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict) or set(data) != {"schema_version", "methods"}:
        raise TransferMethodConfigurationError(
            "Transfer-method configuration must contain schema_version and methods only."
        )
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise TransferMethodConfigurationError(
            "Unsupported transfer-method configuration schema_version."
        )
    if not isinstance(data["methods"], list) or len(data["methods"]) > 100:
        raise TransferMethodConfigurationError(
            "Transfer-method methods must be a list of at most 100 entries."
        )
    configured = {}
    for entry in data["methods"]:
        if not isinstance(entry, dict) or set(entry) != {
            "value",
            "label",
            "identifier_profile",
        }:
            raise TransferMethodConfigurationError(
                "Each transfer method must contain value, label and identifier_profile only."
            )
        value = entry["value"]
        label = entry["label"]
        profile = entry["identifier_profile"]
        if not isinstance(value, str) or _METHOD_VALUE.fullmatch(value) is None:
            raise TransferMethodConfigurationError(
                "Transfer-method values must be lowercase identifiers of at most 64 characters."
            )
        if value in BUILTIN_TRANSFER_METHODS:
            raise TransferMethodConfigurationError(
                f"Built-in transfer method {value!r} cannot be overridden."
            )
        if value in configured:
            raise TransferMethodConfigurationError(
                f"Duplicate transfer-method value {value!r}."
            )
        if not _safe_label(label):
            raise TransferMethodConfigurationError(
                f"Transfer-method label for {value!r} is unsafe."
            )
        if not isinstance(profile, str) or profile not in _PROFILE_FIELDS:
            raise TransferMethodConfigurationError(
                f"Transfer method {value!r} uses an unknown identifier_profile."
            )
        configured[value] = _definition(value, label, profile)
    return configured


def load_transfer_methods(path=None, *, environment=None):
    """Load one immutable-for-process registry snapshot at application startup."""
    path = Path(path) if path is not None else transfer_methods_config_path(environment)
    effective = deepcopy(BUILTIN_TRANSFER_METHODS)
    effective.update(_load_configured_methods(path))
    TRANSFER_METHODS.clear()
    TRANSFER_METHODS.update(effective)
    return transfer_method_options()


def transfer_method_options():
    """Return a safe copy suitable for workflow authoring APIs."""
    return [deepcopy(TRANSFER_METHODS[value]) for value in TRANSFER_METHODS]


def transfer_method_definition(value):
    value = validate_transfer_method(value)
    return deepcopy(TRANSFER_METHODS[value])


def transfer_method_fingerprint(value):
    method = transfer_method_definition(value)
    semantics = {
        "value": method["value"],
        "identifier_profile": method["identifier_profile"],
        "identifier_field": method["identifier_field"],
        "validation": method["validation"],
    }
    encoded = json.dumps(semantics, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_transfer_method(value):
    if not isinstance(value, str) or value not in TRANSFER_METHODS:
        raise ValueError("Withdrawal transfer_method is missing or unsupported.")
    return value


def validate_stored_transfer_method(value, profile=None, fingerprint=None):
    definition = transfer_method_definition(value)
    # Old releases did not persist profile/fingerprint. Only immutable built-ins
    # may use that legacy representation.
    if profile is None and fingerprint is None and value in BUILTIN_TRANSFER_METHODS:
        return definition
    if profile != definition["identifier_profile"]:
        raise ValueError("Stored withdrawal transfer-method profile is incompatible.")
    if fingerprint != transfer_method_fingerprint(value):
        raise ValueError("Stored withdrawal transfer-method configuration changed.")
    return definition


def validate_recipient_identifier(raw, method):
    """Return the exact method-specific identifier without format inference."""
    definition = transfer_method_definition(method)
    field = definition["identifier_field"]
    value = raw.get(field) if isinstance(raw, dict) else None
    maximum = definition["validation"]["max_length"]
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(
            f"Cannot confidently resolve {field}; physical payment is disabled."
        )
    return field, value.strip()
