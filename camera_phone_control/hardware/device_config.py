"""Stable logical phone identities and their current controller endpoints."""

from dataclasses import dataclass, replace
import os
import re

from dotenv import load_dotenv

load_dotenv()

DEFAULT_DEVICE_ID = "phone_A"
DEFAULT_PI_BASE_URL = "http://192.168.68.61:8766"


@dataclass(frozen=True)
class DeviceConfig:
    device_id: str
    pi_base_url: str


DEVICE_CONFIGS = {
    DEFAULT_DEVICE_ID: DeviceConfig(
        device_id=DEFAULT_DEVICE_ID,
        pi_base_url=DEFAULT_PI_BASE_URL,
    ),
}

DEVICE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def validate_device_id(device_id):
    if not isinstance(device_id, str) or not DEVICE_ID_PATTERN.fullmatch(device_id):
        raise ValueError(
            "device_id must be 1-64 letters, numbers, underscores or hyphens."
        )
    return device_id


def get_device_config(device_id=None, *, pi_base_url=None):
    """Resolve one configured logical device without selecting new hardware."""
    selected = validate_device_id(DEFAULT_DEVICE_ID if device_id is None else device_id)

    config = DEVICE_CONFIGS.get(
        selected,
        DeviceConfig(
            device_id=selected,
            pi_base_url=DEFAULT_PI_BASE_URL,
        ),
    )

    endpoint = (
        pi_base_url
        if pi_base_url is not None
        else os.getenv("PI_API_BASE_URL", config.pi_base_url)
    )

    return replace(
        config,
        pi_base_url=endpoint.rstrip("/"),
    )
