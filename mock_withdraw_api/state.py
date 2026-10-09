"""Thread-safe in-memory state and documented Withdraw v2 business semantics."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import pyotp

from config import (
    APP_KEY,
    APP_SECRET,
    AUTH_ENDPOINT,
    HOST,
    IDLE_TIMEOUT_SECONDS,
    LOCK_TTL_SECONDS,
    PASSWORD,
    PROOF_ROOT,
    SESSION_MAX_SECONDS,
    SHORT_LOCK_TTL_SECONDS,
    TOKEN_TTL_SECONDS,
    TOTP_SECRET,
    WS_PORT,
)
from scenarios import SCENARIOS
from seed_data import users as seeded_users
from seed_data import withdrawals as seeded_withdrawals


class MockError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def utc_iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


class MockState:
    def __init__(self, *, ws_host=HOST, ws_port=WS_PORT):
        self.lock = threading.RLock()
        self.publisher = None
        self.ws_host = ws_host
        self.ws_port = ws_port
        self.reset()

    def now(self):
        return time.time() + self.time_offset

    def reset(self):
        with getattr(self, "lock", threading.RLock()):
            self.users = seeded_users()
            self.withdrawals = seeded_withdrawals()
            self.tokens = {}
            self.login_serial = 0

            self.next_withdrawal_id = int(time.time() * 1000)

            self.scenario = "normal"
            self.time_offset = 0
            self.authorizations = {}
            self.physical_sockets = {}
            self.recent_actions = []
            self.recent_results = []
            self.recent_queue_events = []
            self.received_proofs = []
        PROOF_ROOT.mkdir(parents=True, exist_ok=True)
        for path in PROOF_ROOT.iterdir():
            if path.is_file() and not path.is_symlink():
                path.unlink()

    def _log(self, collection, value):
        collection.append({"at": utc_iso(self.now()), **deepcopy(value)})
        del collection[:-200]

    def set_scenario(self, value):
        if value not in SCENARIOS:
            raise MockError(422, "Unsupported mock scenario.")
        with self.lock:
            self.scenario = value
        return value

    def clear_withdrawals(self):
        """Clear the test queue/history without invalidating active sessions."""
        with self.lock:
            affected = {item["payment_record"] for item in self.withdrawals.values()}
            self.withdrawals = {}
            # self.next_withdrawal_id = 936000
            return affected

    def login(self, username, password, g2f_code):
        with self.lock:
            user = self.users.get(username)
            valid_code = pyotp.TOTP(TOTP_SECRET).verify(
                str(g2f_code or ""), valid_window=1
            )
            if user is None or password != PASSWORD or not valid_code:
                raise MockError(401, "Invalid username, password or Google 2FA code.")
            self.login_serial += 1
            serial = self.login_serial
            token = f"mock-token-{user['id']}-session-{serial}"
            session_id = f"{user['id']}-{serial}"
            stamp = self.now()
            session = {
                "token": token,
                "session_id": session_id,
                "operator_id": user["id"],
                "username": username,
                "payment_records": list(user["payment_records"]),
                "created_at": stamp,
                "last_activity": stamp,
                "token_expires_at": stamp + TOKEN_TTL_SECONDS,
                "session_max_expires_at": stamp + SESSION_MAX_SECONDS,
                "revoked": False,
            }
            self.tokens[token] = session
            return self.profile(session)

    def session(self, token, *, touch=True):
        with self.lock:
            session = self.tokens.get(token)
            stamp = self.now()
            expired = (
                not session
                or session["revoked"]
                or self.scenario == "session_expired"
                or stamp >= session["token_expires_at"]
                or stamp >= session["session_max_expires_at"]
                or stamp - session["last_activity"] >= IDLE_TIMEOUT_SECONDS
            )
            if expired:
                raise MockError(401, "Session is expired or revoked.")
            if touch:
                session["last_activity"] = stamp
            return session

    def profile(self, session):
        queues = [
            f"private-apk-withdraw.queue.{record}"
            for record in session["payment_records"]
        ]
        return {
            "token": session["token"],
            "id": session["operator_id"],
            "username": session["username"],
            "role": "operator",
            "payment_records": list(session["payment_records"]),
            "token_expires_at": utc_iso(session["token_expires_at"]),
            "session_max_expires_at": utc_iso(session["session_max_expires_at"]),
            "idle_timeout_minutes": IDLE_TIMEOUT_SECONDS // 60,
            "lock_ttl_minutes": LOCK_TTL_SECONDS // 60,
            "realtime": {
                "key": APP_KEY,
                "host": self.ws_host,
                "port": self.ws_port,
                "scheme": "http",
                "max_payload_kb": 64,
                "auth_endpoint": AUTH_ENDPOINT,
                "channels": {
                    "session": "private-apk-withdraw.session." + session["session_id"],
                    "queue": queues,
                    "online": "presence-apk-withdraw.online",
                },
                "events": {"queue": "queue.changed", "result": "action.result"},
                "actions": [
                    "client-pending",
                    "client-my-locked",
                    "client-lock",
                    "client-renew-lock",
                    "client-release",
                    "client-cancel",
                ],
            },
        }

    def authorize(self, token, socket_id, channel):
        if (
            not isinstance(socket_id, str)
            or not socket_id
            or not isinstance(channel, str)
            or not channel
        ):
            raise MockError(422, "socket_id and channel_name are required.")
        session = self.session(token)
        if self.scenario == "auth_failure":
            raise MockError(403, "Mock channel authorization failure.")
        profile = self.profile(session)
        allowed = {
            profile["realtime"]["channels"]["session"],
            *profile["realtime"]["channels"]["queue"],
            profile["realtime"]["channels"]["online"],
        }
        if channel not in allowed:
            raise MockError(403, "Session cannot access this channel.")
        channel_data = None
        signature_value = f"{socket_id}:{channel}"
        response = {}
        if channel.startswith("presence-"):
            channel_data = '{"user_id":"%s","user_info":{"username":"%s"}}' % (
                session["operator_id"],
                session["username"],
            )
            signature_value += ":" + channel_data
            response["channel_data"] = channel_data
        signature = hmac.new(
            APP_SECRET.encode(), signature_value.encode(), hashlib.sha256
        ).hexdigest()
        auth = APP_KEY + ":" + signature
        response["auth"] = auth
        with self.lock:
            self.authorizations[(socket_id, channel)] = {
                "token": token,
                "auth": auth,
                "channel_data": channel_data,
            }
        return response

    def consume_authorization(self, socket_id, channel, auth, channel_data=None):
        with self.lock:
            grant = self.authorizations.get((socket_id, channel))
            if (
                not grant
                or not hmac.compare_digest(grant["auth"], str(auth or ""))
                or grant["channel_data"] != channel_data
            ):
                raise MockError(403, "Invalid private-channel authorization.")
            return self.session(grant["token"], touch=False)

    def register_socket(self, socket_id):
        with self.lock:
            self.physical_sockets[socket_id] = {"subscriptions": {}}

    def unregister_socket(self, socket_id):
        with self.lock:
            self.physical_sockets.pop(socket_id, None)

    def subscribe(self, socket_id, channel, token):
        with self.lock:
            self.physical_sockets[socket_id]["subscriptions"][channel] = token

    def unsubscribe(self, socket_id, channel):
        with self.lock:
            if socket_id in self.physical_sockets:
                self.physical_sockets[socket_id]["subscriptions"].pop(channel, None)

    def token_for_session_channel(self, socket_id, channel):
        with self.lock:
            token = (
                self.physical_sockets.get(socket_id, {})
                .get("subscriptions", {})
                .get(channel)
            )
        if not token:
            raise MockError(403, "Session channel is not subscribed.")
        return token

    def _expire_locks_locked(self):
        affected = set()
        for item in self.withdrawals.values():
            expiry = item.get("lock_expires_at_timestamp")
            if item["status"] == "LOCKED" and expiry and self.now() >= expiry:
                affected.add(item["payment_record"])
                item.update(
                    status="PENDING",
                    locked_by=None,
                    lock_expires_at=None,
                    lock_expires_at_timestamp=None,
                )
        return affected

    def expire_locks(self):
        with self.lock:
            return self._expire_locks_locked()

    def _visible(self, session, item):
        return item["payment_record"] in session["payment_records"]

    @staticmethod
    def _public(item, session=None):
        hidden = {"lock_expires_at_timestamp", "proof_files"}
        result = {
            key: deepcopy(value) for key, value in item.items() if key not in hidden
        }
        result["locked_by_me"] = bool(
            session and item.get("locked_by") == session["token"]
        )
        return result

    def pending(self, session):
        with self.lock:
            self._expire_locks_locked()
            items = [
                self._public(item, session)
                for item in self.withdrawals.values()
                if item["status"] == "PENDING" and self._visible(session, item)
            ]
            items.sort(
                key=lambda item: (-item["priority"], item["created_at"], item["id"])
            )
            return items

    def my_locked(self, session):
        with self.lock:
            self._expire_locks_locked()
            for item in self.withdrawals.values():
                if item["status"] == "LOCKED" and item["locked_by"] == session["token"]:
                    return self._public(item, session)
            return None

    def lock_withdrawal(self, session, withdraw_id):
        with self.lock:
            self._expire_locks_locked()
            if any(
                item["status"] == "LOCKED" and item["locked_by"] == session["token"]
                for item in self.withdrawals.values()
            ):
                raise MockError(409, "This session already owns a withdrawal lock.")
            item = self.withdrawals.get(withdraw_id)

            if item is None:
                raise MockError(404, "Withdrawal was not found.")
            if not self._visible(session, item):
                raise MockError(
                    409, "Payment record is not accessible to this session."
                )
            if item["status"] == "LOCKED":
                raise MockError(423, "Withdrawal is locked by another session.")
            if item["status"] != "PENDING":
                raise MockError(409, "Withdrawal is no longer payable.")
            ttl = (
                SHORT_LOCK_TTL_SECONDS
                if self.scenario == "short_lock_ttl"
                else LOCK_TTL_SECONDS
            )
            expiry = self.now() + ttl
            item.update(
                status="LOCKED",
                locked_by=session["token"],
                lock_expires_at=utc_iso(expiry),
                lock_expires_at_timestamp=expiry,
            )
            return self._public(item, session)

    def _owned(self, session, withdraw_id):
        self._expire_locks_locked()
        item = self.withdrawals.get(withdraw_id)
        if item is None:
            raise MockError(404, "Withdrawal was not found.")
        if item["status"] != "LOCKED" or item["locked_by"] != session["token"]:
            raise MockError(409, "Withdrawal lock is not owned by this session.")
        return item

    def renew(self, session, withdraw_id):
        with self.lock:
            item = self._owned(session, withdraw_id)
            ttl = (
                SHORT_LOCK_TTL_SECONDS
                if self.scenario == "short_lock_ttl"
                else LOCK_TTL_SECONDS
            )
            expiry = self.now() + ttl
            item["lock_expires_at"] = utc_iso(expiry)
            item["lock_expires_at_timestamp"] = expiry
            return item["lock_expires_at"]

    def release(self, session, withdraw_id):
        with self.lock:
            item = self._owned(session, withdraw_id)
            item.update(
                status="PENDING",
                locked_by=None,
                lock_expires_at=None,
                lock_expires_at_timestamp=None,
            )
            return item["payment_record"]

    def cancel(self, session, withdraw_id, reason):
        allowed = {
            "invalid account number",
            "bank rejected payment",
            "operator unable to complete payment",
        }
        if reason not in allowed:
            raise MockError(422, "Unsupported cancellation reason.")
        with self.lock:
            item = self._owned(session, withdraw_id)
            item.update(
                status="CANCELLED",
                cancel_reason=reason,
                locked_by=None,
                lock_expires_at=None,
                lock_expires_at_timestamp=None,
            )
            return item["payment_record"]

    def confirm(self, session, withdraw_id, proof_paths):
        with self.lock:
            item = self._owned(session, withdraw_id)
            item.update(
                status="PROOF_PENDING",
                confirmed_by_operator=session["operator_id"],
                completed_at=utc_iso(self.now()),
                locked_by=None,
                lock_expires_at=None,
                lock_expires_at_timestamp=None,
                proof_files=[str(path) for path in proof_paths],
                proof_rejected=False,
            )
            self.received_proofs.extend(str(path) for path in proof_paths)
            return self._public(item, session), item["payment_record"]

    def approved(self, session, tx_id=None, page=1, per_page=50):
        with self.lock:
            items = [
                item
                for item in self.withdrawals.values()
                if item["status"] == "PROOF_PENDING"
                and item.get("confirmed_by_operator") == session["operator_id"]
                and (not tx_id or tx_id in item["tx_id"])
            ]
            items.sort(key=lambda item: item.get("completed_at") or "", reverse=True)
            last_page = max(1, (len(items) + per_page - 1) // per_page)
            if page < 1 or page > last_page:
                raise MockError(422, "Invalid page.")
            sliced = items[(page - 1) * per_page : page * per_page]
            values = []
            for item in sliced:
                value = self._public(item, session)
                value["status"] = 6
                values.append(value)
            return {
                "withdrawals": values,
                "pagination": {
                    "current_page": page,
                    "last_page": last_page,
                    "per_page": per_page,
                    "total": len(items),
                },
            }

    def logout(self, token):
        session = self.session(token)
        affected = set()
        with self.lock:
            for item in self.withdrawals.values():
                if item["status"] == "LOCKED" and item["locked_by"] == token:
                    affected.add(item["payment_record"])
                    item.update(
                        status="PENDING",
                        locked_by=None,
                        lock_expires_at=None,
                        lock_expires_at_timestamp=None,
                    )
            session["revoked"] = True
        return affected

    def add_withdrawal(self, data):
        if not isinstance(data, dict):
            raise MockError(422, "Expected a JSON object.")

        def required_text(key, label):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise MockError(422, f"{label} is required.")
            return value.strip()

        record_value = data.get("payment_record")
        if type(record_value) not in {str, int} or not str(record_value).strip():
            raise MockError(422, "payment_record is required.")
        record = str(record_value).strip()
        amount = data.get("final_amount")
        try:
            parsed_amount = Decimal(str(amount).strip())
            if not parsed_amount.is_finite() or parsed_amount <= 0:
                raise InvalidOperation()
            normalized = format(parsed_amount, ".2f")
            if Decimal(normalized) <= 0:
                raise InvalidOperation()
        except (AttributeError, InvalidOperation, ValueError):
            raise MockError(422, "final_amount must be a positive decimal.") from None

        priority_value = data.get("priority", 0)
        try:
            if type(priority_value) is int:
                priority = priority_value
            elif isinstance(priority_value, str) and priority_value.strip():
                priority = int(priority_value.strip())
            else:
                raise ValueError()
        except (TypeError, ValueError):
            raise MockError(422, "priority must be an integer.") from None
        if priority < 0:
            raise MockError(422, "priority must be zero or greater.")

        bank_name = required_text("bank_name", "bank_name")
        account_number = required_text("account_number", "account_number")
        account_name = required_text("account_name", "account_name")
        holder_name = data.get("holder_name", account_name)
        if not isinstance(holder_name, str) or not holder_name.strip():
            raise MockError(422, "holder_name must be non-empty when provided.")
        holder_name = holder_name.strip()

        with self.lock:
            ident = data.get("id", self.next_withdrawal_id)
            if type(ident) is not int or ident <= 0 or ident in self.withdrawals:
                raise MockError(422, "Withdrawal id must be a new positive integer.")
            self.next_withdrawal_id = max(self.next_withdrawal_id + 1, ident + 1)
            stamp = utc_iso(self.now())
            item = {
                "id": ident,
                "order_id": str(data.get("order_id", f"MOCK-{ident}")),
                "tx_id": str(data.get("tx_id", f"MOCK-TX-{ident}")),
                "payment_record": record,
                "type": "withdraw",
                "withdraw_amount": normalized,
                "final_amount": normalized,
                "priority": priority,
                "account_number": account_number,
                "account_name": account_name,
                "holder_name": holder_name,
                "bank_name": bank_name,
                "mobile_no": str(data.get("mobile_no", "0199999999")),
                "created_at": str(data.get("created_at", stamp)),
                "status": "PENDING",
                "locked_by": None,
                "lock_expires_at": None,
                "lock_expires_at_timestamp": None,
                "cancel_reason": None,
                "confirmed_by_operator": None,
                "completed_at": None,
                "proof_files": [],
            }
            self.withdrawals[ident] = item
            return self._public(item), record

    def advance_time(self, seconds):
        if not isinstance(seconds, (int, float)) or seconds < 0 or seconds > 86400:
            raise MockError(422, "seconds must be between 0 and 86400.")
        with self.lock:
            self.time_offset += seconds
            return self._expire_locks_locked()

    def snapshot(self):
        with self.lock:
            pending = [
                self._public(x)
                for x in self.withdrawals.values()
                if x["status"] == "PENDING"
            ]
            locked = [
                self._public(x)
                for x in self.withdrawals.values()
                if x["status"] == "LOCKED"
            ]
            cancelled = [
                self._public(x)
                for x in self.withdrawals.values()
                if x["status"] == "CANCELLED"
            ]
            confirmed = [
                self._public(x)
                for x in self.withdrawals.values()
                if x["status"] == "PROOF_PENDING"
            ]
            sockets = deepcopy(self.physical_sockets)
            return {
                "scenario": self.scenario,
                "operators": deepcopy(self.users),
                "sessions": [
                    {key: value for key, value in session.items() if key != "token"}
                    for session in self.tokens.values()
                ],
                "tokens": [
                    {
                        "token": token,
                        "operator_id": value["operator_id"],
                        "revoked": value["revoked"],
                    }
                    for token, value in self.tokens.items()
                ],
                "payment_records": sorted(
                    {r for u in self.users.values() for r in u["payment_records"]}
                ),
                "physical_websocket_count": len(sockets),
                "socket_ids": sorted(sockets),
                "channel_subscriptions": sockets,
                "channel_authorizers": [
                    {"socket_id": key[0], "channel": key[1], "token": value["token"]}
                    for key, value in self.authorizations.items()
                ],
                "pending_withdrawals": pending,
                "locked_withdrawals": locked,
                "cancelled_withdrawals": cancelled,
                "confirmed_withdrawals": confirmed,
                "my_approved_history": confirmed,
                "received_proof_files": list(self.received_proofs),
                "recent_client_actions": deepcopy(self.recent_actions),
                "recent_action_results": deepcopy(self.recent_results),
                "recent_queue_changed": deepcopy(self.recent_queue_events),
            }
