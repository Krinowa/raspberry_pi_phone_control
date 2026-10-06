"""One Pusher connection and bounded request correlation table per worker."""

import json
import logging
import threading
import time
import uuid
from urllib.parse import quote

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


class PusherSocket:
    def __init__(
        self,
        client,
        *,
        device_id,
        on_queue_changed=lambda: None,
        connect=websocket.create_connection,
    ):
        self.client, self.device_id = client, device_id
        self.on_queue_changed, self.connect_transport = on_queue_changed, connect
        self.pending = {}
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.ws = None
        self.connected = False
        self.session_channel = None
        self.queue_channels = set()
        self.last_received = time.monotonic()
        self.ping_sent = None
        self.activity_timeout = 60

    def connect(self, realtime):
        if self.thread is not None:
            raise RuntimeError("Use a fresh socket instance after disconnect.")

        channels = realtime.get("channels", {})
        self.session_channel = channels.get("session")
        queues = channels.get("queue")

        if not isinstance(
            self.session_channel, str
        ) or not self.session_channel.startswith("private-"):
            raise APIError(0, "Missing private session channel.")
        if not isinstance(queues, list) or not all(
            isinstance(c, str) and c.startswith("private-") for c in queues
        ):
            raise APIError(0, "Missing payment-record queue channels.")

        self.queue_channels = set(queues)
        host, port, key = (
            realtime.get("host"),
            realtime.get("port"),
            realtime.get("key"),
        )
        if (
            not isinstance(host, str)
            or any(c in host for c in "/@?#\\")
            or not host
            or type(port) is not int
            or not 1 <= port <= 65535
            or not isinstance(key, str)
            or not key
            or realtime.get("scheme") != "https"
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

        try:
            self.ws = self.connect_transport(
                f'wss://{host}:{port}/app/{quote(key, safe="")}?protocol=7&client=phone-controller&version=1.0',
                timeout=10,
                enable_multithread=True,
            )
            first = self._receive()
            if first["event"] != "pusher:connection_established":
                raise APIError(0, "Pusher connection was not established.")

            socket_id = first["data"].get("socket_id")
            if not isinstance(socket_id, str) or not socket_id:
                raise APIError(0, "Pusher socket ID is missing.")

            timeout = first["data"].get("activity_timeout", 60)
            if isinstance(timeout, (int, float)) and 1 <= timeout <= 120:
                self.activity_timeout = timeout
            remaining = {self.session_channel, *self.queue_channels}

            for channel in remaining:
                auth = self.client.authorize(endpoint, socket_id, channel)
                self._send(
                    "pusher:subscribe", {"channel": channel, "auth": auth["auth"]}
                )

            deadline = time.monotonic() + 20
            while remaining:
                if time.monotonic() > deadline:
                    raise APIError(0, "Timed out subscribing to withdrawal channels.")
                message = self._receive()
                if message["event"] == "pusher_internal:subscription_succeeded":
                    remaining.discard(message.get("channel"))
                else:
                    self._dispatch(message)

            self.connected = True
            self.ws.settimeout(1)
            self.thread = threading.Thread(
                target=self._reader,
                name="withdraw-socket-" + self.device_id,
                daemon=True,
            )
            self.thread.start()
        except APIError:
            self.close()
            raise
        except Exception:
            self.close()
            raise UncertainResult() from None

    def _send(self, event, data, channel=None):
        message = {"event": event, "data": data}
        if channel:
            message["channel"] = channel
        with self.send_lock:
            self.ws.send(json.dumps(message))

    def _receive(self):
        raw = self.ws.recv()
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
        if event == "pusher:ping":
            self._send("pusher:pong", {})
        elif event in {"pusher:error", "pusher:subscription_error"}:
            raise APIError(0, "Pusher rejected the connection or subscription.")
        elif event == "queue.changed" and message.get("channel") in self.queue_channels:
            self.on_queue_changed()
        elif (
            event == "action.result"
            and message.get("channel") == self.session_channel
            and isinstance(data, dict)
        ):
            with self.lock:
                pending = self.pending.get(data.get("request_id"))
                if pending:
                    # request_id is the protocol's authoritative correlation key.
                    pending["reply"] = data
                    pending["event"].set()

    def _reader(self):
        try:
            while not self.stop.is_set():
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
        except Exception:
            # Never emit transport exceptions: they may include tokens/payloads.
            LOG.warning("Withdrawal socket disconnected device_id=%s", self.device_id)
        finally:
            self.connected = False
            self.stop.set()
            with self.lock:
                for pending in self.pending.values():
                    pending["event"].set()
            if self.ws:
                self.ws.close()

    def action(self, action, *, timeout=10, **fields):
        if action not in ACTIONS or "request_id" in fields:
            raise ValueError("Unsupported client action.")
        request_id = uuid.uuid4().hex
        entry = {"event": threading.Event(), "reply": None}

        with self.lock:
            if not self.connected or len(self.pending) >= 64:
                raise UncertainResult()
            self.pending[request_id] = entry
        try:
            try:
                self._send(
                    "client-" + action,
                    {"request_id": request_id, **fields},
                    self.session_channel,
                )
            except Exception:
                raise UncertainResult() from None
            if not entry["event"].wait(timeout) or entry["reply"] is None:
                raise UncertainResult()

            reply = entry["reply"]
            code = reply.get("code")
            LOG.info(
                "Withdrawal action device_id=%s action=%s request_id=%s code=%s",
                self.device_id,
                action,
                request_id,
                code if type(code) is int else "invalid",
            )

            if type(code) is not int or code != 200:
                raise APIError(
                    code if type(code) is int else 0,
                    "Withdrawal socket action was rejected.",
                )
            return response_data(reply, code)
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def close(self):
        self.connected = False
        self.stop.set()
        if self.ws:
            self.ws.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)
        with self.lock:
            for pending in self.pending.values():
                pending["event"].set()
