"""Edit these values, or override them with environment variables."""

import os

APPIUM_SERVER_URL = os.getenv("APPIUM_SERVER_URL", "http://127.0.0.1:4723")
DEVICE_NAME = os.getenv("ANDROID_DEVICE_NAME", "USB Android phone")
# Optional when only one USB phone is attached; use the serial from adb devices.
DEVICE_UDID = os.getenv("ANDROID_UDID", "")
APP_PACKAGE = os.getenv("APP_PACKAGE", "com.android.settings")
APP_ACTIVITY = os.getenv("APP_ACTIVITY", ".Settings")

# Exact matching only: text, accessibility_id, or resource_id.
SELECTOR_TYPE = os.getenv("SELECTOR_TYPE", "text")
SELECTOR_VALUE = os.getenv("SELECTOR_VALUE", "About phone")
# Human-reviewed visible text/content description of that navigation item.
# Required even for an opaque resource ID. Never configure a payment/auth action.
SAFE_ELEMENT_LABEL = os.getenv("SAFE_ELEMENT_LABEL", "About phone")

ELEMENT_TIMEOUT = 10
