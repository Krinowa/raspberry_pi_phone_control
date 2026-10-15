"""Shared Pusher transport with isolated logical sessions per phone."""

from dataclasses import dataclass, field
import json
import logging
import os
import re
import ssl
import threading
import time
import uuid
from urllib.parse import quote

import certifi
import websocket

from .client import APIError, UncertainResult, response_data

LOG = logging.getLogger("withdraw_v2")
ACTIONS = {
    "pending": "queue",
    "my-locked": "my-locked",
    "lock": "lock",
    "renew-lock": "renew-lock",
    "release": "release",
    "cancel": "cancel",
}


def _sanitized_realtime_reason(error):
    """Return useful transport diagnostics without exposing credentials."""
    reason = str(error).strip() or error.__class__.__name__
    reason = re.sub(r"(?i)(/app/)[^?\s]+", r"\1<redacted>", reason)
    reason = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer <redacted>", reason)
    reason = re.sub(
        r"(?i)\b(?:authorization|token|password|otp|secret|auth|cookie|set-cookie)\b"
        r"\s*[:=]\s*[^\s,;]+",
        "<redacted>",
        reason,
    )
    return reason.replace("\r", " ").replace("\n", " ")[:500]


def _safe_realtime_error(error):
    """Wrap a sanitized transport failure for session/status propagation."""
    reason = _sanitized_realtime_reason(error)
    return APIError(
        0,
        f"Realtime connection failed: {error.__class__.__name__}: {reason}",
    )


def _safe_websocket_endpoint(identity):
    """Describe the returned Pusher endpoint while redacting its public app key."""
    if identity is None:
        return "unconfigured"
    host, port, _key, scheme = identity
    websocket_scheme = "wss" if scheme == "https" else "ws"
    return (
        f"{websocket_scheme}://{host}:{port}/app/<redacted>"
        "?protocol=7&client=phone-controller&version=1.0"
    )


def _settings(realtime):
    channels = realtime.get("channels", {})
    session = channels.get("session")
    queues = channels.get("queue")
    if not isinstance(session, str) or not session.startswith("private-"):
        raise APIError(0, "Missing private session channel.")
    if not isinstance(queues, list) or not all(
        isinstance(channel, str) and channel.startswith("private-")
        for channel in queues
    ):
        raise APIError(0, "Missing payment-record queue channels.")

    host, port, key = realtime.get("host"), realtime.get("port"), realtime.get("key")
    scheme = realtime.get("scheme")
    insecure_local = (
        os.environ.get("WITHDRAW_ALLOW_INSECURE_LOCALHOST") == "1"
        and scheme == "http"
        and host in {"127.0.0.1", "localhost"}
    )
    if (
        not isinstance(host, str)
        or any(character in host for character in "/@?#\\")
        or not host
        or type(port) is not int
        or not 1 <= port <= 65535
        or not isinstance(key, str)
        or not key
        or (scheme != "https" and not insecure_local)
    ):
        raise APIError(
            0, "Realtime connection must provide a valid TLS host, port and key."
        )

    endpoint = realtime.get("auth_endpoint")
    if (
        not isinstance(endpoint, str)
        or not endpoint.startswith("/")
        or endpoint.startswith("//")
    ):
        raise APIError(0, "Private-channel auth must use the configured API host.")
    return (host, port, key, scheme), endpoint, session, set(queues)


@dataclass
class _LogicalSession:
    owner: str
    client: object
    endpoint: str
    session_channel: str
    queue_channels: set
    on_queue_changed: object
    pending: dict = field(default_factory=dict)
    state: str = "CONNECTING"
    error: Exception | None = None
    generation: int = 0
    ready: threading.Event = field(default_factory=threading.Event)


class SharedPusherTransport:
    """One physical socket and reconnect loop for all registered phone sessions.

    Sessions must receive the same Pusher host/port/app key. Authorization stays
    per channel and uses the owning session's client and bearer token.
    """

    def __init__(self, *, connect=websocket.create_connection):
        self.connect_transport = connect
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.sessions = {}
        self.pending = {}
        self.identity = None
        self.ws = None
        self.thread = None
        self.connected = False
        self.error = None
        self.socket_id = None
        self.subscribed = set()
        self.subscribing = set()
        self.reauthorize = set()
        self.channel_authorizers = {}
        self.last_received = time.monotonic()
        self.ping_sent = None
        self.activity_timeout = 60

    @property
    def physical_connected(self):
        with self.lock:
            return self.connected

    def register(self, owner, client, realtime, on_queue_changed):
        identity, endpoint, session_channel, queue_channels = _settings(realtime)
        host, port, key, scheme = identity
        LOG.info(
            "Withdraw realtime metadata device=%s host=%s port=%s scheme=%s "
            "key_present=%s auth_endpoint=%s session_channel=%s queue_channels=%s",
            owner,
            host,
            port,
            scheme,
            bool(key),
            endpoint,
            session_channel,
            sorted(queue_channels),
        )
        with self.lock:
            if not self.sessions:
                self.error = None
            if self.identity is not None and self.identity != identity:
                raise APIError(
                    0,
                    "Enabled Withdraw accounts must use the same realtime "
                    "Pusher endpoint.",
                )
            for name, registered in self.sessions.items():
                if name != owner and registered.session_channel == session_channel:
                    raise APIError(
                        0, "Withdraw session channels must be unique per phone."
                    )
            existing = self.sessions.get(owner)
            if existing:
                for request_id in list(existing.pending):
                    self._fail_request_locked(request_id)
            session = _LogicalSession(
                owner,
                client,
                endpoint,
                session_channel,
                queue_channels,
                on_queue_changed,
            )
            self.sessions[owner] = session
            self.identity = identity
            # A registration can race with the previous final unregister while
            # its socket thread is still unwinding. Revive that one thread
            # instead of leaving the new session stopped or spawning a second.
            self.stop.clear()
            # Re-authorize the logical session after a token refresh. Shared queue
            # subscriptions stay intact and continue serving healthy sessions.
            self.subscribed.discard(session_channel)
            self.subscribing.discard(session_channel)
            if existing and self.connected:
                self.reauthorize.add(session_channel)
            if self.connected:
                self._set_state_locked(session, "SUBSCRIBING")
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(
                    target=self._run,
                    name="withdraw-shared-socket",
                    daemon=True,
                )
                self.thread.start()
            LOG.info(
                "Registered Withdraw realtime logical session device=%s operator=%s",
                owner,
                (
                    getattr(client, "profile", {}).get("id")
                    if isinstance(getattr(client, "profile", None), dict)
                    else "unknown"
                ),
            )
            self.wake.set()
            return session

    def unregister(self, owner):
        ws = None
        thread = None
        with self.lock:
            session = self.sessions.pop(owner, None)
            if session:
                session.state = "CLOSED"
                session.ready.set()
                for request_id in list(session.pending):
                    self._fail_request_locked(request_id)
                self.reauthorize.update(
                    channel
                    for channel, authorizer in self.channel_authorizers.items()
                    if authorizer == owner
                )
            if not self.sessions:
                self.identity = None
                self.stop.set()
                ws, thread = self.ws, self.thread
            if session:
                LOG.info(
                    "Unregistered Withdraw realtime logical session device=%s", owner
                )
            self.wake.set()
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)

    def close(self):
        with self.lock:
            owners = list(self.sessions)
        for owner in owners:
            self.unregister(owner)

    def wait_ready(self, owner, timeout=20):
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                session = self.sessions.get(owner)
                if session is None:
                    raise UncertainResult()
                if session.state == "READY":
                    return True
                if session.state == "ERROR":
                    raise session.error or APIError(0, "Realtime session failed.")
                event = session.ready
                event.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not event.wait(remaining):
                return False

    def session_state(self, owner):
        with self.lock:
            session = self.sessions.get(owner)
            return session.state if session else "DISCONNECTED"

    def session_error(self, owner):
        with self.lock:
            session = self.sessions.get(owner)
            error = session.error if session and session.error else self.error
            return str(error) if error else None

    def generation(self, owner):
        with self.lock:
            session = self.sessions.get(owner)
            return session.generation if session else 0

    def _set_state_locked(self, session, state, error=None):
        session.state, session.error = state, error
        session.ready.set()

    def _channel_owners_locked(self):
        result = {}
        for session in self.sessions.values():
            result.setdefault(session.session_channel, []).append(session)
            for channel in session.queue_channels:
                result.setdefault(channel, []).append(session)
        return result

    def _run(self):
        backoff = 1
        try:
            while not self.stop.is_set():
                with self.lock:
                    if not self.sessions:
                        return
                try:
                    if not self.physical_connected:
                        try:
                            self._open()
                        except Exception as exc:
                            safe_error = _safe_realtime_error(exc)
                            with self.lock:
                                safe_endpoint = _safe_websocket_endpoint(self.identity)
                            LOG.warning(
                                "Withdraw realtime connection failed "
                                "endpoint=%s error=%s: %s",
                                safe_endpoint,
                                type(exc).__name__,
                                _sanitized_realtime_reason(exc),
                            )
                            self._mark_disconnected(safe_error)
                            if self.stop.is_set():
                                break
                            self.wake.wait(backoff)
                            self.wake.clear()
                            backoff = min(30, backoff * 2)
                            continue
                        backoff = 1
                    self._sync_subscriptions()
                    self._read_once()
                except Exception as exc:
                    safe_error = _safe_realtime_error(exc)
                    if not self.stop.is_set():
                        LOG.warning(
                            "Shared withdrawal realtime transport disconnected "
                            "error=%s: %s; reconnecting",
                            type(exc).__name__,
                            _sanitized_realtime_reason(exc),
                        )
                    self._mark_disconnected(safe_error)
                    if self.stop.is_set():
                        break
                    self.wake.wait(backoff)
                    self.wake.clear()
                    backoff = min(30, backoff * 2)
        finally:
            self._mark_disconnected()
            with self.lock:
                if self.thread is threading.current_thread():
                    self.thread = None

    def _open(self):
        with self.lock:
            identity = self.identity
        if identity is None:
            raise UncertainResult()
        host, port, key, scheme = identity
        websocket_scheme = "wss" if scheme == "https" else "ws"
        url = (
            f'{websocket_scheme}://{host}:{port}/app/{quote(key, safe="")}'
            "?protocol=7&client=phone-controller&version=1.0"
        )
        safe_endpoint = _safe_websocket_endpoint(identity)
        LOG.info("Withdraw realtime websocket URL structure %s", safe_endpoint)
        LOG.info(
            "Withdraw realtime TCP/TLS/WebSocket open attempt endpoint=%s",
            safe_endpoint,
        )
        connect_options = {"timeout": 10, "enable_multithread": True}
        if websocket_scheme == "wss":
            connect_options["sslopt"] = {
                "cert_reqs": ssl.CERT_REQUIRED,
                "ca_certs": certifi.where(),
            }
        ws = self.connect_transport(url, **connect_options)
        LOG.info(
            "Withdraw realtime WebSocket open succeeded endpoint=%s",
            safe_endpoint,
        )
        with self.lock:
            self.ws = ws
        LOG.info("Withdraw realtime waiting for pusher:connection_established")
        first = self._receive()
        if first["event"] != "pusher:connection_established":
            raise APIError(0, "Pusher connection was not established.")
        socket_id = first["data"].get("socket_id")
        if not isinstance(socket_id, str) or not socket_id:
            raise APIError(0, "Pusher socket ID is missing.")
        timeout = first["data"].get("activity_timeout", 60)
        with self.lock:
            self.socket_id = socket_id
            self.error = None
            self.activity_timeout = (
                timeout
                if isinstance(timeout, (int, float)) and 1 <= timeout <= 120
                else 60
            )
            self.connected = True
            self.subscribed.clear()
            self.subscribing.clear()
            self.reauthorize.clear()
            self.channel_authorizers.clear()
            for session in self.sessions.values():
                self._set_state_locked(session, "SUBSCRIBING")
        LOG.info(
            "Withdraw realtime received pusher:connection_established socket_id=%s",
            socket_id,
        )
        ws.settimeout(1)

    def _sync_subscriptions(self):
        with self.lock:
            owners = self._channel_owners_locked()
            obsolete = self.subscribed - set(owners)
            reauthorize = self.reauthorize & set(owners)
            self.reauthorize -= reauthorize
            additions = set(owners) - self.subscribed - self.subscribing
            for channel in reauthorize:
                for session in owners[channel]:
                    if session.state == "READY":
                        self._set_state_locked(session, "SUBSCRIBING")
        for channel in obsolete | reauthorize:
            self._send("pusher:unsubscribe", {"channel": channel})
            with self.lock:
                self.subscribed.discard(channel)
                self.subscribing.discard(channel)
                self.channel_authorizers.pop(channel, None)
        additions |= reauthorize
        for channel in additions:
            authorized = False
            last_error = None
            attempted = False
            for session in owners[channel]:
                if session.state == "ERROR":
                    continue
                attempted = True
                try:
                    LOG.info(
                        "Authorizing Withdraw realtime channel owner=%s "
                        "endpoint=%s socket_id=%s channel=%s",
                        session.owner,
                        session.endpoint,
                        self.socket_id,
                        channel,
                    )
                    auth = session.client.authorize(
                        session.endpoint, self.socket_id, channel
                    )
                    LOG.info(
                        "Withdraw realtime channel authorization accepted "
                        "owner=%s channel=%s",
                        session.owner,
                        channel,
                    )
                    self._send(
                        "pusher:subscribe",
                        {"channel": channel, "auth": auth["auth"]},
                    )
                    with self.lock:
                        self.subscribing.add(channel)
                        self.channel_authorizers[channel] = session.owner
                    authorized = True
                    break
                except APIError as exc:
                    last_error = exc
                    with self.lock:
                        self._set_state_locked(session, "ERROR", exc)
            if attempted and not authorized:
                # No healthy logical owner can authenticate this channel. Keep
                # the physical socket alive for all unrelated sessions.
                LOG.warning(
                    "Withdraw channel authorization failed channel=%s code=%s",
                    channel,
                    getattr(last_error, "code", 0),
                )
        self.wake.clear()

    def _read_once(self):
        try:
            self._dispatch(self._receive())
        except websocket.WebSocketTimeoutException:
            stamp = time.monotonic()
            if self.ping_sent is not None and stamp - self.ping_sent > 10:
                raise UncertainResult()
            if (
                self.ping_sent is None
                and stamp - self.last_received >= self.activity_timeout
            ):
                self._send("pusher:ping", {})
                self.ping_sent = stamp

    def _send(self, event, data, channel=None):
        message = {"event": event, "data": data}
        if channel:
            message["channel"] = channel
        with self.send_lock:
            with self.lock:
                ws = self.ws
            if ws is None:
                raise UncertainResult()
            ws.send(json.dumps(message))

    def _receive(self):
        with self.lock:
            ws = self.ws
        raw = ws.recv()
        if not raw or len(raw) > 2 * 1024 * 1024:
            raise UncertainResult()
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise UncertainResult()
        data = message.get("data", {})
        if isinstance(data, str):
            data = json.loads(data)
        message["data"] = data
        self.last_received, self.ping_sent = time.monotonic(), None
        return message

    def _dispatch(self, message):
        event, data = message.get("event"), message.get("data", {})
        channel = message.get("channel")
        if event == "pusher:ping":
            self._send("pusher:pong", {})
        elif event == "pusher:error":
            raise APIError(0, "Pusher rejected the connection.")
        elif event == "pusher:subscription_error":
            code = data.get("status", data.get("code", 0))
            code = code if type(code) is int else 0
            with self.lock:
                self.subscribing.discard(channel)
                self.channel_authorizers.pop(channel, None)
                for session in self._channel_owners_locked().get(channel, []):
                    self._set_state_locked(
                        session,
                        "ERROR",
                        APIError(
                            code,
                            "Pusher rejected a private-channel subscription.",
                        ),
                    )
        elif event == "pusher_internal:subscription_succeeded":
            LOG.info("Subscribed Withdraw realtime channel %s", channel)
            with self.lock:
                self.subscribing.discard(channel)
                self.subscribed.add(channel)
                for session in self.sessions.values():
                    required = {session.session_channel, *session.queue_channels}
                    if required <= self.subscribed and session.state not in {
                        "READY",
                        "ERROR",
                    }:
                        session.generation += 1
                        self._set_state_locked(session, "READY")
        elif event == "queue.changed":
            with self.lock:
                callbacks = [
                    session.on_queue_changed
                    for session in self.sessions.values()
                    if channel in session.queue_channels and session.state == "READY"
                ]
            for callback in callbacks:
                try:
                    callback()
                except Exception:
                    LOG.exception("Withdraw queue callback failed")
        elif event == "action.result" and isinstance(data, dict):
            request_id = data.get("request_id")
            with self.lock:
                entry = self.pending.get(request_id)
                if entry and entry["channel"] == channel:
                    entry["reply"] = data
                    entry["event"].set()

    def _fail_request_locked(self, request_id):
        entry = self.pending.get(request_id)
        if entry:
            entry["event"].set()

    def _mark_disconnected(self, error=None):
        with self.lock:
            ws, self.ws = self.ws, None
            self.connected = False
            if error is not None:
                self.error = error
            self.socket_id = None
            self.subscribed.clear()
            self.subscribing.clear()
            self.reauthorize.clear()
            self.channel_authorizers.clear()
            for session in self.sessions.values():
                if session.state != "ERROR":
                    self._set_state_locked(
                        session,
                        "WAITING_FOR_CONNECTION",
                        error or session.error,
                    )
                for request_id in list(session.pending):
                    self._fail_request_locked(request_id)
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    def action(self, owner, action, *, timeout=10, **fields):
        if action not in ACTIONS or "request_id" in fields:
            raise ValueError("Unsupported client action.")
        request_id = uuid.uuid4().hex
        entry = {"event": threading.Event(), "reply": None}
        with self.lock:
            session = self.sessions.get(owner)
            if (
                session is None
                or session.state != "READY"
                or len(session.pending) >= 64
            ):
                raise UncertainResult()
            entry["channel"] = session.session_channel
            session.pending[request_id] = entry
            self.pending[request_id] = entry
        try:
            try:
                if action == "lock":
                    LOG.info(
                        "[withdraw-dispatch] sending client-lock device=%s withdrawal=%s request_id=%s",
                        owner,
                        fields.get("withdraw_id"),
                        request_id,
                    )
                self._send(
                    "client-" + action,
                    {"request_id": request_id, **fields},
                    entry["channel"],
                )
            except Exception:
                raise UncertainResult() from None
            if not entry["event"].wait(timeout) or entry["reply"] is None:
                raise UncertainResult()
            reply = entry["reply"]
            code = reply.get("code")
            LOG.info(
                "Withdrawal action device_id=%s action=%s request_id=%s code=%s",
                owner,
                action,
                request_id,
                code if type(code) is int else "invalid",
            )
            if type(code) is not int or code != 200:
                detail = "Withdrawal socket action was rejected."
                reason = reply.get("message") or reply.get("error")
                if not isinstance(reason, str) and isinstance(reply.get("data"), dict):
                    reason = reply["data"].get("message") or reply["data"].get("reason")
                if (
                    code == 409
                    and isinstance(reason, str)
                    and "payment" in reason.casefold()
                    and "record" in reason.casefold()
                ):
                    detail = "Payment record is not accessible to this session."
                raise APIError(
                    code if type(code) is int else 0,
                    detail,
                )
            return response_data(reply, code)
        finally:
            with self.lock:
                session = self.sessions.get(owner)
                if session:
                    session.pending.pop(request_id, None)
                self.pending.pop(request_id, None)


class PusherSocket:
    """Per-phone logical session facade backed by a shared transport."""

    def __init__(
        self,
        client,
        *,
        device_id,
        on_queue_changed=lambda: None,
        transport=None,
        connect=websocket.create_connection,
    ):
        self.client = client
        self.device_id = device_id
        self.on_queue_changed = on_queue_changed
        self.transport = transport or SharedPusherTransport(connect=connect)
        self.registered = False

    @property
    def connected(self):
        return (
            self.registered and self.transport.session_state(self.device_id) == "READY"
        )

    @property
    def physical_connected(self):
        return self.transport.physical_connected

    @property
    def session_state(self):
        return self.transport.session_state(self.device_id)

    @property
    def error(self):
        return self.transport.session_error(self.device_id)

    @property
    def generation(self):
        return self.transport.generation(self.device_id)

    def connect(self, realtime):
        self.transport.register(
            self.device_id, self.client, realtime, self.on_queue_changed
        )
        self.registered = True
        if not self.transport.wait_ready(self.device_id, 20):
            raise APIError(
                0,
                self.error or "Realtime connection did not become ready in time.",
            )

    def wait_ready(self, timeout=20):
        return self.transport.wait_ready(self.device_id, timeout)

    def action(self, action, *, timeout=10, **fields):
        return self.transport.action(self.device_id, action, timeout=timeout, **fields)

    def close(self):
        if self.registered:
            self.transport.unregister(self.device_id)
            self.registered = False
