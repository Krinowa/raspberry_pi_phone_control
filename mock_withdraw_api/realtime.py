"""Small Pusher-compatible WebSocket server for the documented v2 events."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import threading
from itertools import count

from aiohttp import WSMsgType, web

from config import APP_KEY, HOST, WS_PORT
from state import MockError


class RealtimeServer:
    def __init__(self, state, host=HOST, port=WS_PORT):
        self.state = state
        self.host = host
        self.port = port
        self.loop = None
        self.thread = None
        self.runner = None
        self.sockets = {}
        self.serial = count(1)
        self.started = threading.Event()
        self.expiry_task = None
        state.publisher = self

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.thread.start()
        if not self.started.wait(5):
            raise RuntimeError("Mock realtime server did not start.")

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._start())
        self.started.set()
        self.loop.run_forever()

    async def _start(self):
        application = web.Application()
        application.router.add_get("/app/{key}", self._websocket)
        self.runner = web.AppRunner(application)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.host, self.port).start()
        self.expiry_task = asyncio.create_task(self._expiry_loop())

    def close(self):
        if not self.loop:
            return
        future = asyncio.run_coroutine_threadsafe(self._close(), self.loop)
        future.result(timeout=5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread:
            self.thread.join(timeout=5)

    async def _close(self):
        await self._disconnect_all()
        if self.expiry_task:
            self.expiry_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.expiry_task
            self.expiry_task = None
        if self.runner:
            await self.runner.cleanup()

    async def _send(self, ws, event, data, channel=None):
        message = {"event": event, "data": data}
        if channel:
            message["channel"] = channel
        await ws.send_str(json.dumps(message))

    async def _websocket(self, request):
        if request.match_info["key"] != APP_KEY:
            raise web.HTTPNotFound()
        ws = web.WebSocketResponse(max_msg_size=2 * 1024 * 1024, heartbeat=None)
        await ws.prepare(request)
        socket_id = f"{next(self.serial)}.{int(self.state.now() * 1000) % 1000000}"
        self.sockets[socket_id] = ws
        self.state.register_socket(socket_id)
        await self._send(
            ws,
            "pusher:connection_established",
            {"socket_id": socket_id, "activity_timeout": 30},
        )
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await self._handle(socket_id, ws, message.data)
                elif message.type in {WSMsgType.ERROR, WSMsgType.CLOSED}:
                    break
        finally:
            self.sockets.pop(socket_id, None)
            self.state.unregister_socket(socket_id)
        return ws

    @staticmethod
    def _data(message):
        data = message.get("data", {})
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            raise ValueError("Pusher data must be an object.")
        return data

    async def _handle(self, socket_id, ws, raw):
        try:
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError()
            event = message.get("event")
            data = self._data(message)
            channel = message.get("channel")
        except (TypeError, ValueError, json.JSONDecodeError):
            await self._send(
                ws, "pusher:error", {"code": 4000, "message": "Invalid message."}
            )
            return

        if event == "pusher:ping":
            await self._send(ws, "pusher:pong", {})
            return
        if event == "pusher:pong":
            return
        if event == "pusher:unsubscribe":
            self.state.unsubscribe(socket_id, data.get("channel"))
            return
        if event == "pusher:subscribe":
            channel = data.get("channel")
            try:
                session = self.state.consume_authorization(
                    socket_id,
                    channel,
                    data.get("auth"),
                    data.get("channel_data"),
                )
                self.state.subscribe(socket_id, channel, session["token"])
            except MockError as exc:
                await self._send(
                    ws,
                    "pusher:subscription_error",
                    {"status": exc.code, "message": str(exc)},
                    channel,
                )
                return
            succeeded = {}
            if channel.startswith("presence-"):
                succeeded = {
                    "presence": {
                        "count": 1,
                        "ids": [str(session["operator_id"])],
                        "hash": {
                            str(session["operator_id"]): {
                                "username": session["username"]
                            }
                        },
                    }
                }
            await self._send(
                ws, "pusher_internal:subscription_succeeded", succeeded, channel
            )
            return
        if isinstance(event, str) and event.startswith("client-"):
            asyncio.create_task(
                self._client_action(socket_id, ws, channel, event, data)
            )
            return
        await self._send(
            ws, "pusher:error", {"code": 4001, "message": "Unsupported event."}
        )

    async def _client_action(self, socket_id, ws, channel, event, data):
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            await self._action_result(
                ws, channel, request_id, 422, {}, "request_id is required."
            )
            return
        action = event[7:]
        try:
            token = self.state.token_for_session_channel(socket_id, channel)
            session = self.state.session(token)
        except MockError as exc:
            await self._action_result(ws, channel, request_id, exc.code, {}, str(exc))
            return
        with self.state.lock:
            self.state._log(
                self.state.recent_actions,
                {
                    "socket_id": socket_id,
                    "channel": channel,
                    "operator_id": session["operator_id"],
                    "event": event,
                    "request_id": request_id,
                    "payload": data,
                },
            )
            scenario = self.state.scenario

        if scenario == "socket_disconnect":
            await ws.close(code=4000, message=b"mock disconnect")
            return
        prefix = action.replace("-lock", "") if action == "renew-lock" else action
        if scenario == f"{prefix}_timeout_before_apply":
            return
        if action == "lock" and scenario in {"lock_404", "lock_409", "lock_423"}:
            code = int(scenario[-3:])
            await self._action_result(
                ws, channel, request_id, code, {}, f"Mock lock {code}."
            )
            return
        if action == "renew-lock" and scenario == "renew_409":
            await self._action_result(
                ws, channel, request_id, 409, {}, "Mock renew conflict."
            )
            return

        affected = set()
        try:
            if action == "pending":
                result = {"pending": self.state.pending(session)}
            elif action == "my-locked":
                result = {"withdrawal": self.state.my_locked(session)}
            elif action == "lock":
                withdrawal = self.state.lock_withdrawal(
                    session, data.get("withdraw_id")
                )
                result = {"withdrawal": withdrawal}
                affected.add(withdrawal["payment_record"])
            elif action == "renew-lock":
                result = {
                    "lock_expires_at": self.state.renew(
                        session, data.get("withdraw_id")
                    )
                }
            elif action == "release":
                affected.add(self.state.release(session, data.get("withdraw_id")))
                result = {}
            elif action == "cancel":
                affected.add(
                    self.state.cancel(
                        session, data.get("withdraw_id"), data.get("reason")
                    )
                )
                result = {}
            else:
                raise MockError(422, "Unsupported client action.")
        except MockError as exc:
            await self._action_result(ws, channel, request_id, exc.code, {}, str(exc))
            return

        if affected:
            await self._emit_queue_changed(affected)
        if scenario == f"{prefix}_timeout_after_apply":
            return
        delay = 0
        if scenario == "delayed_action_result":
            delay = 1.5
        elif scenario == "out_of_order_action_result":
            delay = 0.3 if action in {"pending", "lock"} else 0.05
        if delay:
            await asyncio.sleep(delay)
        await self._action_result(ws, channel, request_id, 200, result, "OK")
        if scenario == "duplicate_action_result":
            await self._action_result(ws, channel, request_id, 200, result, "OK")

    async def _action_result(self, ws, channel, request_id, code, data, message):
        payload = {
            "request_id": request_id,
            "code": code,
            "status": 1 if code == 200 else 0,
            "message": message,
            "data": data,
        }
        with self.state.lock:
            self.state._log(
                self.state.recent_results,
                {"channel": channel, "payload": payload},
            )
        await self._send(ws, "action.result", payload, channel)

    async def _emit_queue_changed(self, records):
        records = {str(record) for record in records}
        for record in records:
            channel = f"private-apk-withdraw.queue.{record}"
            with self.state.lock:
                self.state._log(
                    self.state.recent_queue_events,
                    {"channel": channel, "payload": {}},
                )
                targets = [
                    self.sockets.get(socket_id)
                    for socket_id, value in self.state.physical_sockets.items()
                    if channel in value["subscriptions"]
                ]
                duplicate = self.state.scenario == "duplicate_queue_changed"
            for target in [item for item in targets if item is not None]:
                await self._send(target, "queue.changed", {}, channel)
                if duplicate:
                    await self._send(target, "queue.changed", {}, channel)

    def emit_queue_changed(self, records):
        if not self.loop:
            return
        return asyncio.run_coroutine_threadsafe(
            self._emit_queue_changed(records), self.loop
        )

    async def _disconnect_all(self):
        for ws in list(self.sockets.values()):
            await ws.close(code=4001, message=b"mock forced disconnect")

    def disconnect_all(self):
        if self.loop:
            return asyncio.run_coroutine_threadsafe(self._disconnect_all(), self.loop)

    async def _expiry_loop(self):
        while True:
            await asyncio.sleep(0.5)
            affected = self.state.expire_locks()
            if affected:
                await self._emit_queue_changed(affected)
