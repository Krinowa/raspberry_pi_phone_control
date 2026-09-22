"""HTTP client for the Android accessibility text helper."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .state import ScreenStateError, TextDetectionUnavailable


class AccessibilityScreenStateProvider:
    def __init__(self, base_url, timeout=2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get_json(self, path):
        request = Request(
            self.base_url + path,
            headers={
                "Accept": "application/json",
            },
            method="GET",
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (URLError, HTTPError, TimeoutError, ValueError) as exc:
            raise TextDetectionUnavailable(
                "Android accessibility text service is unavailable."
            ) from exc

        return data

    def visible_texts(self) -> list[str]:
        data = self._get_json("/texts")

        values = data.get("texts")

        if not isinstance(values, list):
            raise ScreenStateError(
                "Accessibility text service returned an invalid response."
            )

        result = []

        for value in values:
            if not isinstance(value, str):
                continue

            value = value.strip()

            if value and value not in result:
                result.append(value)

        return result

    def contains_text(self, text: str) -> bool:
        expected = text.casefold()

        return any(expected in value.casefold() for value in self.visible_texts())
