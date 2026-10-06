"""Withdraw Bot v2 HTTP boundary. No physical actions or implicit request retries."""

from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import pyotp
import requests

API_PATH = "/api/admin-withdraw/v2"


class APIError(RuntimeError):
    def __init__(self, code, message="Withdraw API request failed.", *, retry_after=60):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class UncertainResult(APIError):
    def __init__(self):
        super().__init__(
            0, "External request outcome is uncertain; reconciliation is required."
        )


def base_origin(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/", API_PATH, API_PATH + "/")
    ):
        raise ValueError(
            "Withdraw API base_url must be an HTTPS host (optionally with the v2 path)."
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def response_data(body, code=200):
    if (
        not isinstance(body, dict)
        or type(body.get("status")) is not int
        or body["status"] != 1
    ):
        raise APIError(code, "Withdraw API returned an unsuccessful response.")
    data = body.get("data")
    # PHP may serialize an empty response as [] rather than {}.
    if data == []:
        return {}
    if not isinstance(data, dict):
        raise APIError(code, "Withdraw API response data must be an object.")
    return deepcopy(data)


def retry_delay(headers):
    value = headers.get("Retry-After", "60")
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        try:
            return max(
                1,
                (
                    parsedate_to_datetime(value) - datetime.now(timezone.utc)
                ).total_seconds(),
            )
        except (ValueError, TypeError, OverflowError):
            return 60


class WithdrawClient:
    def __init__(self, base_url, *, http=None):
        self.origin = base_origin(base_url)
        self.http = http or requests.Session()
        self.http.trust_env = (
            False  # Do not substitute .netrc credentials for this worker.
        )
        self.token = None
        self.profile = None

    def request(self, method, path, *, raw=False, **kwargs):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("API paths must stay on the configured API host.")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            response = self.http.request(
                method,
                self.origin + path,
                headers=headers,
                timeout=(5, 20),
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException:
            raise UncertainResult() from None
        if response.status_code != 200:
            raise APIError(
                response.status_code,
                f"Withdraw API returned HTTP {response.status_code}.",
                retry_after=retry_delay(response.headers),
            )
        try:
            body = response.json()
        except ValueError:
            raise UncertainResult() from None
        if raw:
            if not isinstance(body, dict):
                raise APIError(0, "Invalid private-channel authorization response.")
            return body
        return response_data(body)

    def login(self, username, password, totp_secret, operator_id=None):
        self.token = self.profile = None
        try:
            code = pyotp.TOTP(totp_secret).now()
        except Exception:
            raise ValueError("External TOTP credential is invalid.") from None
        profile = self.request(
            "POST",
            API_PATH + "/login",
            json={
                "username": username,
                "password": password,
                "g2f_code": code,
            },
        )
        valid_operator = (
            profile.get("role") == "operator"
            and type(profile.get("id")) is int
            and profile["id"] > 0
        )
        if not valid_operator or (
            operator_id is not None and profile["id"] != operator_id
        ):
            raise APIError(
                403,
                "Login account is not an authorized operator or does not match this device.",
            )
        if not isinstance(profile.get("token"), str) or not profile["token"]:
            raise APIError(0, "Login response is missing the session token.")
        if not isinstance(profile.get("realtime"), dict):
            raise APIError(0, "Login response is missing realtime configuration.")
        self.token, self.profile = profile["token"], profile
        return deepcopy(profile)

    def authorize(self, endpoint, socket_id, channel):
        body = self.request(
            "POST",
            endpoint,
            raw=True,
            json={
                "socket_id": socket_id,
                "channel_name": channel,
            },
        )
        if not isinstance(body.get("auth"), str) or not body["auth"]:
            raise APIError(0, "Private-channel authorization was not granted.")
        return body

    def logout(self):
        try:
            return self.request("POST", API_PATH + "/logout")
        finally:
            self.token = self.profile = None

    def confirm_with_receipt(self, withdraw_id, receipt_path):
        if type(withdraw_id) is not int or withdraw_id <= 0:
            raise ValueError("Invalid withdrawal ID.")
        path = Path(receipt_path)
        if (
            path.is_symlink()
            or not path.is_file()
            or not 0 < path.stat().st_size <= 10 * 1024 * 1024
        ):
            raise ValueError("Receipt must be a nonempty regular file, at most 10 MB.")
        mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".pdf": "application/pdf",
        }.get(path.suffix.lower())
        if mime is None:
            raise ValueError("Unsupported receipt format.")
        with path.open("rb") as stream:
            return self.request(
                "POST",
                f"{API_PATH}/withdrawals/{withdraw_id}/confirm",
                files={"proof": (path.name, stream, mime)},
            )

    def reconcile_confirmation(self, tx_id, withdraw_id):
        # The documented tx_id search is PARTIAL. Match both identifiers exactly,
        # and scan pages; a substring match must never count as confirmation.
        for page in range(1, 101):
            data = self.request(
                "GET",
                API_PATH + "/withdrawals/my-approved",
                params={"tx_id": tx_id, "page": page},
            )
            items = data.get("withdrawals")
            if not isinstance(items, list):
                raise APIError(0, "Invalid my-approved response.")
            for item in items:
                if (
                    isinstance(item, dict)
                    and item.get("tx_id") == tx_id
                    and item.get("id") == withdraw_id
                    and type(item.get("id")) is int
                    and type(item.get("status")) is int
                    and item["status"] in (1, 6)
                ):
                    return True
            pagination = data.get("pagination", {})
            last = pagination.get("last_page")
            if type(last) is not int or last < page:
                raise APIError(0, "Invalid my-approved pagination.")
            if page == last:
                return False
        raise APIError(
            0, "Confirmation search exceeded the page limit; review is required."
        )

    def close(self):
        self.http.close()
        self.token = self.profile = None
