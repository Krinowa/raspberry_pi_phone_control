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
_PROFILE_DEFINITIONS = {
    "account_number": {
        "identifier_field": "account_number",
        "workflow_variable": "account_number",
        "api_type": "bank",
        "recipient_name_field": "account_name",
        "validation_type": "nonempty_text",
        "verification_modes": ("account_number",),
    },
    "fps_phone_number": {
        "identifier_field": "mobile_no",
        "workflow_variable": "fps_phone_number",
        "api_type": "kuaizhuan",
        "recipient_name_field": "holder_name",
        "validation_type": "hk_phone_number",
        "verification_modes": ("fps_phone",),
    },
    "fps_email": {
        "identifier_field": "mobile_no",
        "workflow_variable": "fps_email",
        "api_type": "kuaizhuan",
        "recipient_name_field": "holder_name",
        "validation_type": "email",
        "verification_modes": ("exact",),
    },
    "fps_id": {
        "identifier_field": "mobile_no",
        "workflow_variable": "fps_id",
        "api_type": "kuaizhuan",
        "recipient_name_field": "holder_name",
        "validation_type": "fps_id",
        "verification_modes": ("exact",),
    },
    # Compatibility profile for explicitly configured pre-v2.1.3 aliases.
    # It is not used by any built-in live method and performs no inference.
    "mobile_no": {
        "identifier_field": "mobile_no",
        "workflow_variable": "mobile_no",
        "api_type": "kuaizhuan",
        "recipient_name_field": "holder_name",
        "validation_type": "nonempty_text",
        "verification_modes": ("exact", "account_number"),
    },
}


def _definition(value, label, profile):
    semantics = _PROFILE_DEFINITIONS[profile]
    return {
        "value": value,
        "label": label,
        "identifier_profile": profile,
        "identifier_field": semantics["identifier_field"],
        "workflow_variable": semantics["workflow_variable"],
        "api_type": semantics["api_type"],
        "recipient_name_field": semantics["recipient_name_field"],
        "verification_modes": list(semantics["verification_modes"]),
        "validation": {
            "type": semantics["validation_type"],
            "max_length": 256,
            "preserve_leading_zeroes": True,
        },
    }


BUILTIN_TRANSFER_METHODS = {
    "bank_account": _definition(
        "bank_account", "Bank Account Transfer", "account_number"
    ),
    "phone_no": _definition("phone_no", "FPS Phone Number", "fps_phone_number"),
    "email": _definition("email", "FPS Email Address", "fps_email"),
    "fps_id": _definition("fps_id", "FPS ID", "fps_id"),
}

# These definitions exist only so historical workflow documents and unresolved
# journal metadata can be inspected without rewriting them. validate_transfer_method
# deliberately does not consult this registry.
LEGACY_TRANSFER_METHODS = {
    "mobile_number": _definition(
        "mobile_number", "Legacy Mobile Number Transfer", "mobile_no"
    )
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
        if (
            not isinstance(profile, str)
            or profile not in _PROFILE_DEFINITIONS
            or profile == "mobile_no"
        ):
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
        "workflow_variable": method["workflow_variable"],
        "api_type": method["api_type"],
        "recipient_name_field": method["recipient_name_field"],
        "verification_modes": method["verification_modes"],
        "validation": method["validation"],
    }
    encoded = json.dumps(semantics, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _pre_v213_fingerprint(definition):
    semantics = {
        "value": definition["value"],
        "identifier_profile": definition["identifier_profile"],
        "identifier_field": definition["identifier_field"],
        "validation": {
            "type": "nonempty_text",
            "max_length": 256,
            "preserve_leading_zeroes": True,
        },
    }
    encoded = json.dumps(semantics, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_transfer_method(value):
    if not isinstance(value, str) or value not in TRANSFER_METHODS:
        raise ValueError("Withdrawal transfer_method is missing or unsupported.")
    return value


def validate_stored_transfer_method(value, profile=None, fingerprint=None):
    if value in LEGACY_TRANSFER_METHODS:
        definition = deepcopy(LEGACY_TRANSFER_METHODS[value])
        if profile is None and fingerprint is None:
            return definition
        if profile != definition["identifier_profile"]:
            raise ValueError(
                "Stored withdrawal transfer-method profile is incompatible."
            )
        if fingerprint == _pre_v213_fingerprint(definition):
            return definition
        raise ValueError("Stored withdrawal transfer-method configuration changed.")
    definition = transfer_method_definition(value)
    # Old releases did not persist profile/fingerprint. Only immutable built-ins
    # may use that legacy representation.
    if profile is None and fingerprint is None and value in BUILTIN_TRANSFER_METHODS:
        return definition
    if profile != definition["identifier_profile"]:
        raise ValueError("Stored withdrawal transfer-method profile is incompatible.")
    if fingerprint not in {
        transfer_method_fingerprint(value),
        _pre_v213_fingerprint(definition),
    }:
        raise ValueError("Stored withdrawal transfer-method configuration changed.")
    return definition


def validate_recipient_identifier(raw, method):
    """Return the exact method-specific identifier without format inference."""
    definition = transfer_method_definition(method)
    field = definition["identifier_field"]
    value = raw.get(field) if isinstance(raw, dict) else None
    validation = definition["validation"]
    maximum = validation["max_length"]
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(
            f"Cannot confidently resolve {field}; physical payment is disabled."
        )
    kind = validation["type"]
    if kind == "hk_phone_number":
        normalized = re.sub(r"[ -]", "", value)
        valid = re.fullmatch(r"[0-9]{8}", normalized) is not None
    elif kind == "email":
        valid = (
            len(value) <= 254
            and re.fullmatch(
                r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
                r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
                r"[A-Za-z]{2,63}",
                value,
            )
            is not None
        )
    elif kind == "fps_id":
        # Hong Kong FPS identifiers are issued as 7- or 9-digit values.
        valid = re.fullmatch(r"(?:[0-9]{7}|[0-9]{9})", value) is not None
    else:
        valid = True
    if not valid:
        raise ValueError(
            f"Cannot confidently resolve {field} for transfer_method {method}; "
            "physical payment is disabled."
        )
    return field, value


def validate_transfer_method_payload(raw, method, *, require_recipient_name=False):
    """Validate v2.1.3 method semantics without inferring from other fields."""
    definition = transfer_method_definition(method)
    if not isinstance(raw, dict) or raw.get("type") != definition["api_type"]:
        raise ValueError(
            f"Withdrawal type does not match transfer_method {method}; "
            "physical payment is disabled."
        )
    field, identifier = validate_recipient_identifier(raw, method)
    name_field = definition["recipient_name_field"]
    name = raw.get(name_field)
    if require_recipient_name and (
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or len(name) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise ValueError(
            f"Cannot confidently resolve {name_field}; physical payment is disabled."
        )
    return field, identifier, name_field, name
