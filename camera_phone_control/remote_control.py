"""Remote takeover of a phone by the RemoteViewer device agent.

Implements the control addresses of RemoteViewer's docs/pi-interface.md
(sections 3, 4 and 5) on the existing ``/agent/v1`` blueprint, whose
``before_request`` already enforces the Bearer secret:

    POST   /agent/v1/devices/<id>/takeover
    POST   /agent/v1/devices/<id>/takeover/renew
    DELETE /agent/v1/devices/<id>/takeover?session_id=...
    POST   /agent/v1/devices/<id>/input
    GET    /agent/v1/devices/<id>/screen
    PUT    /agent/v1/devices/<id>/screen

The agent has already checked that the remote user may do this. This module
enforces the device lock, the lease and the input order, and records who did
what. Input goes through the same AutomationContext input bridges that
workflows use, so it behaves the same with the Pi and Appium backends.

A takeover is a lease: without a renew for LEASE_SECONDS the phone is released.
"""

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from flask import abort, jsonify, request

from automation.input.base import UnsupportedOperation
from calibration import Calibration
from automation.workflows.service import RemoteTakeoverRefused
from device_lock import DeviceBusy

LOG = logging.getLogger("remote_control")

LEASE_SECONDS = 6.0
# How long one input waits for a stopping job to hand over the phone.
INPUT_WAIT_SECONDS = 15.0
LOCK_POLL_SECONDS = 0.1
MAX_ID_LENGTH = 64
MAX_REASON_LENGTH = 200
MAX_TEXT_LENGTH = 200
HISTORY_LIMIT = 200

KEYS = {
    "enter": "ENTER",
    "backspace": "BACKSPACE",
    "delete": "DELETE",
    "tab": "TAB",
    "escape": "ESCAPE",
    "space": None,  # typed as text: neither backend has a SPACE key
    "up": "UP",
    "down": "DOWN",
    "left": "LEFT",
    "right": "RIGHT",
}
BUTTONS = {"home": "HOME", "back": "BACK", "wake": "SCREEN_ON"}


class Refused(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plain(value, limit):
    """A non-empty string of at most `limit` characters, no control characters."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


def _fraction(value):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def validate_input(data):
    """Raise Refused(400) unless data is one input as the contract defines it."""
    kind = data.get("type")
    fields = {
        "tap": {"x", "y"},
        "swipe": {"x1", "y1", "x2", "y2", "duration_ms"},
        "text": {"text"},
        "key": {"key"},
        "home": set(),
        "back": set(),
        "wake": set(),
    }.get(kind)
    if fields is None or set(data) != fields | {"session_id", "type"}:
        raise Refused(400, "invalid_input")
    if kind in {"tap", "swipe"} and not all(
        _fraction(data[name]) for name in fields - {"duration_ms"}
    ):
        raise Refused(400, "invalid_input")
    if kind == "swipe" and (
        type(data["duration_ms"]) is not int or not 50 <= data["duration_ms"] <= 3000
    ):
        raise Refused(400, "invalid_input")
    if kind == "text" and not _plain(data["text"], MAX_TEXT_LENGTH):
        raise Refused(400, "invalid_input")
    if kind == "key" and data["key"] not in KEYS:
        raise Refused(400, "invalid_input")


def perform_input(bridge, calibration, data):
    """Carry out one validated input. Returns False if it was ignored.

    Points are fractions of the camera picture; the calibration maps them to
    the phone screen. A point outside the phone is ignored. Text goes to the
    bridge as data only, never through a shell.
    """
    kind = data["type"]
    try:
        if kind == "tap":
            point = calibration.map([data["x"], data["y"]])
        elif kind == "swipe":
            start = calibration.map([data["x1"], data["y1"]])
            end = calibration.map([data["x2"], data["y2"]])
    except ValueError:
        return False  # outside the calibrated phone display
    if kind == "tap":
        bridge.tap(*point)
    elif kind == "swipe":
        bridge.swipe(*start, *end, data["duration_ms"])
    elif kind == "text":
        bridge.type_text(data["text"])
    elif kind == "key":
        name = KEYS[data["key"]]
        if name is None:
            bridge.type_text(" ")
        else:
            bridge.key(name)
    else:
        bridge.key(BUTTONS[kind])
    return True


class RemoteHistory:
    """Append-only JSON-lines audit of remote sessions; never holds typed text."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def append(self, record):
        line = json.dumps(record, sort_keys=True) + "\n"
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as stream:
                    stream.write(line)
            except OSError as exc:
                LOG.error("Cannot write remote control history (%s).", exc.strerror)

    def recent(self, limit=HISTORY_LIMIT):
        with self.lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return []
            except OSError as exc:
                LOG.error("Cannot read remote control history (%s).", exc.strerror)
                return []
        records = []
        for line in reversed(lines):
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
            if len(records) >= limit:
                break
        return records


@dataclass(eq=False)
class Session:
    device_id: str
    session_id: str
    user_id: str
    reason: str
    started_at: str
    interrupted_job: dict | None
    lock_identity: str | None
    expires: float = 0.0
    timer: threading.Timer | None = None
    lock_fd: int | None = None
    ended: threading.Event = field(default_factory=threading.Event)
    has_lock: threading.Event = field(default_factory=threading.Event)
    input_lock: threading.Lock = field(default_factory=threading.Lock)
    channel: tuple | None = None
    inputs: int = 0
    # True while an input is being sent; the lock outlives the session until
    # that input has finished, so nobody else can touch the phone meanwhile.
    sending: bool = False

    def public(self):
        return {
            "device_id": self.device_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "reason": self.reason,
            "started_at": self.started_at,
            "interrupted_job": self.interrupted_job,
            "inputs": self.inputs,
            "waiting_for_job": not self.has_lock.is_set(),
        }


class RemoteControl:
    """Who holds which phone remotely, and the lease that keeps it held.

    Collaborators are callables so this class stays independent of Flask state:
      local_operator(device_id) -> bool   dashboard operator holds the phone
      running_job(device_id) -> Job|None  a job claimed and running there
      lock_identity(device_id) -> str|None
      open_input(device_id) -> (bridge, calibration|None)
      read_screen(device_id) -> calibration|None   the device's calibration
      save_screen(device_id, calibration, changed_by)  store it and use it now
    """

    def __init__(
        self,
        workflow_service,
        device_locks,
        history,
        *,
        local_operator,
        running_job,
        lock_identity,
        open_input,
        read_screen,
        save_screen,
        lease_seconds=LEASE_SECONDS,
        input_wait_seconds=INPUT_WAIT_SECONDS,
    ):
        self.workflow_service = workflow_service
        self.device_locks = device_locks
        self.history = history
        self.local_operator = local_operator
        self.running_job = running_job
        self.lock_identity = lock_identity
        self.open_input = open_input
        self.read_screen = read_screen
        self.save_screen = save_screen
        self.lease_seconds = lease_seconds
        self.input_wait_seconds = input_wait_seconds
        self.lock = threading.RLock()
        self.sessions = {}

    # -- takeover lifecycle -------------------------------------------------

    def takeover(self, device_id, session_id, user_id, reason, allow_during_job):
        with self.lock:
            current = self.sessions.get(device_id)
            if current is not None:
                if current.session_id == session_id:
                    self._renew(current)
                    return current.interrupted_job
                raise Refused(409, "held_by_remote")
            if self.local_operator(device_id):
                raise Refused(409, "local_operator")
            job = self.running_job(device_id)
            started_at = _now()
            details = {
                "session_id": session_id,
                "user_id": user_id,
                "reason": reason,
                "at": started_at,
            }
            try:
                run = self.workflow_service.hold_for_remote(
                    device_id,
                    details,
                    allow_during_job=allow_during_job,
                    job_pending=job is not None,
                )
            except RemoteTakeoverRefused as exc:
                raise Refused(409, exc.code) from None
            interrupted = None
            if run is not None or job is not None:
                interrupted = {
                    "id": job.job_id if job is not None else run["run_id"],
                    "workflow": run["workflow"] if run else job.workflow,
                    "step_index": (run["step_index"] if run else job.start_step_index),
                }
            session = Session(
                device_id,
                session_id,
                user_id,
                reason,
                started_at,
                interrupted,
                self.lock_identity(device_id),
            )
            self.sessions[device_id] = session
            self._renew(session)
        threading.Thread(
            target=self._acquire_lock,
            args=(session,),
            name=f"remote-lock-{device_id}",
            daemon=True,
        ).start()
        LOG.info(
            "Remote takeover of %s by %s (session %s); interrupted job: %s",
            device_id,
            user_id,
            session_id,
            interrupted["id"] if interrupted else "none",
        )
        self.history.append(
            {
                "event": "started",
                "at": started_at,
                "device_id": device_id,
                "session_id": session_id,
                "user_id": user_id,
                "reason": reason,
                "interrupted_job": interrupted,
            }
        )
        return interrupted

    def renew(self, device_id, session_id):
        with self.lock:
            session = self.sessions.get(device_id)
            if session is None or session.session_id != session_id:
                return False
            self._renew(session)
            return True

    def release(self, device_id, session_id, ended_by="remote"):
        with self.lock:
            session = self.sessions.get(device_id)
            if session is None or (
                session_id is not None and session.session_id != session_id
            ):
                return False
            self._end(session, ended_by)
            return True

    def end_by_operator(self, device_id):
        """The dashboard operator takes the phone back."""
        return self.release(device_id, None, "operator")

    def holder(self, device_id):
        with self.lock:
            session = self.sessions.get(device_id)
            return session.public() if session else None

    def active(self):
        with self.lock:
            return [session.public() for session in self.sessions.values()]

    def close(self):
        with self.lock:
            for session in list(self.sessions.values()):
                self._end(session, "shutdown")

    def _renew(self, session):
        session.expires = time.monotonic() + self.lease_seconds
        if session.timer is not None:
            session.timer.cancel()
        session.timer = threading.Timer(
            self.lease_seconds, self._expire, args=(session,)
        )
        session.timer.daemon = True
        session.timer.start()

    def _expire(self, session):
        with self.lock:
            if (
                self.sessions.get(session.device_id) is session
                and time.monotonic() >= session.expires
            ):
                self._end(session, "lease_expired")

    def _end(self, session, ended_by):
        # Caller holds self.lock.
        del self.sessions[session.device_id]
        session.ended.set()
        if session.timer is not None:
            session.timer.cancel()
        if not session.sending:
            self._close_lock(session)
        self.workflow_service.release_remote(session.device_id)
        LOG.info(
            "Remote control of %s ended (%s); session %s, %d inputs",
            session.device_id,
            ended_by,
            session.session_id,
            session.inputs,
        )
        self.history.append(
            {
                "event": "ended",
                "at": _now(),
                "device_id": session.device_id,
                "session_id": session.session_id,
                "user_id": session.user_id,
                "reason": session.reason,
                "ended_by": ended_by,
                "inputs": session.inputs,
                "interrupted_job": session.interrupted_job,
            }
        )

    @staticmethod
    def _close_lock(session):
        # Caller holds self.lock.
        if session.lock_fd is not None:
            os.close(session.lock_fd)  # releases the OS device lock
            session.lock_fd = None

    def _acquire_lock(self, session):
        """Take the OS device lock once the stopped job has let go of it."""
        if session.lock_identity is None:
            session.has_lock.set()  # no lockable device yet; input will fail
            return
        while not session.ended.is_set():
            if self.workflow_service.critical_step_running(session.device_id):
                # A critical step may release the lock at a manual checkpoint;
                # wait until the job has left it.
                session.ended.wait(LOCK_POLL_SECONDS)
                continue
            try:
                fd = self.device_locks.claim(session.lock_identity, "remote")
            except DeviceBusy:
                session.ended.wait(LOCK_POLL_SECONDS)
                continue
            except OSError as exc:
                LOG.error("Remote control cannot lock the device (%s).", exc.strerror)
                session.ended.wait(1.0)
                continue
            with self.lock:
                if self.sessions.get(session.device_id) is session:
                    session.lock_fd = fd
                    session.has_lock.set()
                    return
            os.close(fd)
            return

    # -- input ---------------------------------------------------------------

    def _require_holder(self, device_id, session_id):
        with self.lock:
            session = self.sessions.get(device_id)
            if session is None or session.session_id != session_id:
                raise Refused(403, "not_holder")
            return session

    def _wait_until_held(self, session):
        """Wait until the stopped job has handed the device lock over."""
        deadline = time.monotonic() + self.input_wait_seconds
        while not session.has_lock.wait(0.05):
            if session.ended.is_set():
                raise Refused(403, "not_holder")
            if time.monotonic() >= deadline:
                raise Refused(409, "job_stopping")

    def input(self, device_id, data):
        session = self._require_holder(device_id, data["session_id"])
        with session.input_lock:
            self._wait_until_held(session)
            if session.channel is None:
                try:
                    bridge, calibration = self.open_input(device_id)
                except Refused:
                    raise
                except Exception as exc:
                    LOG.warning(
                        "Remote input for %s cannot be prepared (%s).",
                        device_id,
                        type(exc).__name__,
                    )
                    raise Refused(409, "device_unavailable") from None
                session.channel = (bridge, calibration)
            bridge, calibration = session.channel
            if calibration is None and data["type"] in {"tap", "swipe"}:
                # Retry loading next time: the operator may calibrate meanwhile.
                session.channel = None
                raise Refused(409, "not_calibrated")
            with self.lock:
                # Checked and marked atomically with _end(): once this passes,
                # the device lock stays held until the input has finished.
                if self.sessions.get(device_id) is not session:
                    raise Refused(403, "not_holder")
                if session.lock_fd is None:
                    raise Refused(409, "device_unavailable")
                session.sending = True
            try:
                performed = perform_input(bridge, calibration, data)
            except UnsupportedOperation:
                raise Refused(422, "unsupported_input") from None
            except ValueError:
                # Backend validation (e.g. non-ASCII text on the Pi). No input sent.
                raise Refused(422, "unsupported_input") from None
            except Exception as exc:
                # The input may or may not have reached the phone. Never retry it,
                # and never log the exception text: it may include typed values.
                LOG.warning(
                    "Remote %s input on %s failed (%s).",
                    data["type"],
                    device_id,
                    type(exc).__name__,
                )
                raise Refused(502, "input_failed") from None
            finally:
                with self.lock:
                    session.sending = False
                    if session.ended.is_set():
                        self._close_lock(session)
            session.inputs += 1
            LOG.info(
                "Remote input %s on %s by %s (session %s)%s",
                data["type"],
                device_id,
                session.user_id,
                session.session_id,
                "" if performed else ": outside the phone, ignored",
            )

    # -- screen position (calibration) ---------------------------------------

    def screen(self, device_id):
        """The device's calibration corners, or None when it is not calibrated."""
        calibration = self.read_screen(device_id)
        return calibration.corners.tolist() if calibration is not None else None

    def set_screen(self, device_id, session_id, corners):
        """Replace the device's calibration; only the takeover holder may.

        The new corners are saved and used at once by the dashboard, the bot and
        the next remote input, exactly like a dashboard calibration.
        """
        session = self._require_holder(device_id, session_id)
        try:
            calibration = Calibration(corners)
        except (ValueError, TypeError):
            raise Refused(400, "bad_corners") from None
        # Serialised with input, and only once the bot has let go of the phone.
        with session.input_lock:
            self._wait_until_held(session)
            with self.lock:
                if self.sessions.get(device_id) is not session:
                    raise Refused(403, "not_holder")
            at = _now()
            changed_by = {
                "source": "remote",
                "session_id": session.session_id,
                "user_id": session.user_id,
                "at": at,
            }
            self.save_screen(device_id, calibration, changed_by)
            # The next input reloads the bridge with the new calibration.
            session.channel = None
        LOG.info(
            "Calibration of %s changed remotely by %s (session %s)",
            device_id,
            session.user_id,
            session.session_id,
        )
        self.history.append(
            {
                "event": "calibrated",
                "at": at,
                "device_id": device_id,
                "session_id": session.session_id,
                "user_id": session.user_id,
                "reason": session.reason,
                "corners": calibration.corners.tolist(),
            }
        )
        return calibration.corners.tolist()


def register_routes(blueprint, remote, known_device, viewable_device=None):
    """Add the control addresses to the authenticated agent blueprint.

    known_device: id -> bool, phones that can be controlled. viewable_device:
    id -> bool, devices whose screen position may be read (defaults to
    known_device); a viewable but uncontrollable device reports no corners.
    """
    viewable_device = viewable_device or known_device

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise Refused(400, "invalid_json")
        return data

    def session_id_of(data):
        value = data.get("session_id")
        if not _plain(value, MAX_ID_LENGTH):
            raise Refused(400, "invalid_session_id")
        return value

    def require_device(device_id):
        if not known_device(device_id):
            abort(404)

    @blueprint.errorhandler(Refused)
    def refused(exc):
        return jsonify(error=exc.code), exc.status

    @blueprint.post("/devices/<device_id>/takeover")
    def takeover(device_id):
        require_device(device_id)
        data = body()
        if set(data) != {"session_id", "user_id", "reason", "allow_during_job"}:
            raise Refused(400, "invalid_takeover")
        session_id = session_id_of(data)
        if (
            not _plain(data["user_id"], MAX_ID_LENGTH)
            or not _plain(data["reason"], MAX_REASON_LENGTH)
            or type(data["allow_during_job"]) is not bool
        ):
            raise Refused(400, "invalid_takeover")
        interrupted = remote.takeover(
            device_id,
            session_id,
            data["user_id"],
            data["reason"],
            data["allow_during_job"],
        )
        return jsonify(interrupted_job=interrupted)

    @blueprint.post("/devices/<device_id>/takeover/renew")
    def renew(device_id):
        require_device(device_id)
        if not remote.renew(device_id, session_id_of(body())):
            raise Refused(409, "not_holder")
        return jsonify(ok=True)

    @blueprint.delete("/devices/<device_id>/takeover")
    def release(device_id):
        require_device(device_id)
        session_id = request.args.get("session_id", "")
        if _plain(session_id, MAX_ID_LENGTH):
            remote.release(device_id, session_id)
        return jsonify(ok=True)

    @blueprint.post("/devices/<device_id>/input")
    def remote_input(device_id):
        require_device(device_id)
        data = body()
        session_id_of(data)
        validate_input(data)
        remote.input(device_id, data)
        return jsonify(ok=True)

    @blueprint.get("/devices/<device_id>/screen")
    def screen(device_id):
        if not viewable_device(device_id):
            abort(404)
        if not known_device(device_id):
            return jsonify(corners=None)
        return jsonify(corners=remote.screen(device_id))

    @blueprint.put("/devices/<device_id>/screen")
    def set_screen(device_id):
        require_device(device_id)
        data = body()
        if set(data) != {"session_id", "corners"}:
            raise Refused(400, "bad_corners")
        session_id = session_id_of(data)
        corners = remote.set_screen(device_id, session_id, data["corners"])
        return jsonify(corners=corners)
