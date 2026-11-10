"""Tests for remote takeover (remote_control.py) and its hooks in workflows.

Follows the checklist in the RemoteViewer handoff. Workflows run for real on
the mock input backend; device locks are real files in a temporary directory.

Run from camera_phone_control:  python -m pytest tests/test_remote_control.py
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_frames  # noqa: E402
import remote_control  # noqa: E402
from automation.workflows.models import new_workflow  # noqa: E402
from automation.workflows.service import WorkflowService  # noqa: E402
from calibration import Calibration  # noqa: E402
from device_lock import DeviceBusy, DeviceLocks  # noqa: E402

SECRET = "test-only-secret-0123456789abcdefghij"
AUTH = {"Authorization": "Bearer " + SECRET}
BASE = "/agent/v1/devices/phone_A"
# The mock input backend locks the device under the serial "mock".
LOCK_IDENTITY = "mock"


class FakeBridge:
    def __init__(self):
        self.calls = []

    def tap(self, x, y):
        self.calls.append(("tap", round(x, 6), round(y, 6)))

    def swipe(self, x1, y1, x2, y2, duration_ms=300):
        self.calls.append(
            (
                "swipe",
                round(x1, 6),
                round(y1, 6),
                round(x2, 6),
                round(y2, 6),
                duration_ms,
            )
        )

    def type_text(self, text):
        self.calls.append(("text", text))

    def key(self, key):
        if key == "ESCAPE":
            raise remote_control.UnsupportedOperation("no escape")
        self.calls.append(("key", key))


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_INPUT_BACKEND", "mock")
    monkeypatch.setenv("PHONE_SCREEN_BACKEND", "mock")
    locks = DeviceLocks(tmp_path / "locks")
    service = WorkflowService(object(), locks, device_id="phone_A")
    bridge = FakeBridge()
    # Phone occupies the middle half of the camera picture.
    calibration = Calibration([[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]])
    flags = SimpleNamespace(local=False, job=None, calibration=calibration)
    remote = remote_control.RemoteControl(
        service,
        locks,
        remote_control.RemoteHistory(tmp_path / "history.jsonl"),
        local_operator=lambda device_id: flags.local,
        running_job=lambda device_id: flags.job,
        lock_identity=lambda device_id: LOCK_IDENTITY,
        open_input=lambda device_id: (bridge, flags.calibration),
        lease_seconds=0.6,
        input_wait_seconds=3.0,
    )
    app = Flask(__name__)
    known = lambda device_id: device_id == "phone_A"  # noqa: E731
    blueprint = agent_frames.make_blueprint(None, SECRET, known)
    remote_control.register_routes(blueprint, remote, known)
    app.register_blueprint(blueprint)
    yield SimpleNamespace(
        client=app.test_client(),
        remote=remote,
        service=service,
        locks=locks,
        bridge=bridge,
        flags=flags,
    )
    remote.close()
    for run_id in list(service.runs):
        service.cancel(run_id)
        service.wait(run_id, 5)


def takeover(client, session="s1", allow=False, headers=AUTH):
    return client.post(
        BASE + "/takeover",
        json={
            "session_id": session,
            "user_id": "alice",
            "reason": "payment screen stuck",
            "allow_during_job": allow,
        },
        headers=headers,
    )


def send(client, session="s1", headers=AUTH, **fields):
    return client.post(
        BASE + "/input", json={"session_id": session, **fields}, headers=headers
    )


def renew(client, session="s1"):
    return client.post(
        BASE + "/takeover/renew", json={"session_id": session}, headers=AUTH
    )


def workflow(*steps):
    flow = new_workflow("remote test", purpose="balance_check")
    flow["steps"] = [
        dict(step, id=f"step_{number}") for number, step in enumerate(steps, 1)
    ]
    return flow


def start_run(env, *steps):
    started = env.service.start(workflow(*steps), device_id="phone_A")
    return started["run_id"]


def step_number(env, run_id):
    state = env.service.get(run_id)["runner"].state
    return state.step_index if state else 0


# -- authentication and addressing -------------------------------------------


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong-0123456789abcdefghijklmnop"}]
)
def test_missing_or_wrong_secret_is_401_on_all_four(env, headers):
    client = env.client
    assert takeover(client, headers=headers).status_code == 401
    assert (
        client.post(
            BASE + "/takeover/renew", json={"session_id": "s1"}, headers=headers
        )
    ).status_code == 401
    assert (
        client.delete(BASE + "/takeover?session_id=s1", headers=headers)
    ).status_code == 401
    assert send(client, headers=headers, type="home").status_code == 401


def test_unknown_device_is_404(env):
    client = env.client
    other = "/agent/v1/devices/phone_Z"
    assert client.post(other + "/takeover", json={}, headers=AUTH).status_code == 404
    assert (
        client.post(other + "/takeover/renew", json={"session_id": "s1"}, headers=AUTH)
    ).status_code == 404
    assert (
        client.delete(other + "/takeover?session_id=s1", headers=AUTH).status_code
        == 404
    )
    assert (
        client.post(
            other + "/input", json={"session_id": "s1", "type": "home"}, headers=AUTH
        )
    ).status_code == 404


# -- takeover ----------------------------------------------------------------


def test_input_without_takeover_or_from_another_session_is_403(env):
    assert send(env.client, type="home").status_code == 403
    assert takeover(env.client).status_code == 200
    response = send(env.client, session="s2", type="home")
    assert response.status_code == 403
    assert env.bridge.calls == []


def test_idle_takeover_returns_no_job_and_is_idempotent(env):
    first = takeover(env.client)
    assert first.status_code == 200
    assert first.get_json() == {"interrupted_job": None}
    assert takeover(env.client).status_code == 200
    second = takeover(env.client, session="s2")
    assert second.status_code == 409
    assert second.get_json() == {"error": "held_by_remote"}


def test_local_operator_refuses(env):
    env.flags.local = True
    response = takeover(env.client, allow=True)
    assert response.status_code == 409
    assert response.get_json() == {"error": "local_operator"}


def test_job_running_refused_without_permission(env):
    run_id = start_run(env, {"type": "wait", "seconds": 30})
    assert wait_for(lambda: step_number(env, run_id) == 1)
    response = takeover(env.client, allow=False)
    assert response.status_code == 409
    assert response.get_json() == {"error": "job_running"}
    # The job keeps running.
    assert env.service.snapshot(run_id)["status"] == "running"


def test_claimed_job_without_a_run_yet_counts_as_running(env):
    env.flags.job = SimpleNamespace(
        job_id="job-1", workflow="flow_x", start_step_index=2
    )
    response = takeover(env.client, allow=False)
    assert response.get_json() == {"error": "job_running"}
    response = takeover(env.client, allow=True)
    assert response.status_code == 200
    assert response.get_json()["interrupted_job"] == {
        "id": "job-1",
        "workflow": "flow_x",
        "step_index": 2,
    }


@pytest.mark.parametrize("allow", [False, True])
def test_critical_step_is_refused_whatever_allow_during_job_says(env, allow):
    run_id = start_run(
        env,
        {"type": "wait", "seconds": 0},
        {"type": "wait", "seconds": 30, "critical": True},
    )
    assert wait_for(lambda: step_number(env, run_id) == 2)
    response = takeover(env.client, allow=allow)
    assert response.status_code == 409
    assert response.get_json() == {"error": "critical_step"}
    assert env.service.snapshot(run_id)["status"] == "running"


def test_takeover_with_permission_stops_the_job_and_reports_it(env):
    run_id = start_run(
        env,
        {"type": "wait", "seconds": 0},
        {"type": "wait", "seconds": 30},
    )
    assert wait_for(lambda: step_number(env, run_id) == 2)
    response = takeover(env.client, allow=True)
    assert response.status_code == 200
    assert response.get_json()["interrupted_job"] == {
        "id": run_id,
        "workflow": env.service.get(run_id)["workflow_id"],
        "step_index": 1,
    }
    snapshot = env.service.wait(run_id, 5)
    assert snapshot["status"] == "cancelled"
    assert snapshot["remote_takeover"]["user_id"] == "alice"
    assert snapshot["remote_takeover"]["session_id"] == "s1"
    assert snapshot["remote_takeover"]["reason"] == "payment screen stuck"
    # Once the job has let go, the remote session owns the OS device lock.
    assert wait_for(lambda: env.locks.owner(LOCK_IDENTITY) == "remote")
    assert send(env.client, type="home").status_code == 200
    assert env.bridge.calls == [("key", "HOME")]


def test_running_critical_step_finishes_before_the_remote_stop(env):
    # A stop requested inside a critical step (the race after the check) must
    # let that step finish; the run stops at the first check after it.
    run_id = start_run(
        env,
        {"type": "wait", "seconds": 0.4, "critical": True},
        {"type": "wait", "seconds": 30},
    )
    assert wait_for(lambda: step_number(env, run_id) == 1)
    env.service.get(run_id)["remote_stop"].set()
    time.sleep(0.2)
    assert env.service.snapshot(run_id)["status"] == "running"
    snapshot = env.service.wait(run_id, 5)
    assert snapshot["status"] == "cancelled"
    assert snapshot["step_index"] == 2
    timing = snapshot["timing"]["steps"]
    assert timing[0]["status"] == "completed"


def test_new_run_on_a_held_device_stops_before_any_input(env):
    assert takeover(env.client).status_code == 200
    run_id = start_run(env, {"type": "key", "key": "ENTER"})
    snapshot = env.service.wait(run_id, 5)
    # Stopped by the remote stop, or refused by the remote device lock.
    assert snapshot["status"] in {"cancelled", "failed"}
    assert snapshot["step_index"] == 0
    assert snapshot["remote_takeover"]["session_id"] == "s1"


# -- lease -------------------------------------------------------------------


def test_lease_expires_without_renew(env):
    assert takeover(env.client).status_code == 200
    assert renew(env.client).status_code == 200
    assert wait_for(lambda: env.locks.owner(LOCK_IDENTITY) == "remote")
    time.sleep(0.9)
    response = renew(env.client)
    assert response.status_code == 409
    assert env.locks.owner(LOCK_IDENTITY) is None
    assert not env.service.is_remote_held("phone_A")
    assert send(env.client, type="home").status_code == 403


def test_renew_keeps_the_lease(env):
    assert takeover(env.client).status_code == 200
    for _ in range(4):
        time.sleep(0.3)
        assert renew(env.client).status_code == 200
    assert renew(env.client, session="other").status_code == 409


def test_operator_take_back_makes_the_next_renew_409(env):
    assert takeover(env.client).status_code == 200
    assert env.remote.end_by_operator("phone_A")
    assert renew(env.client).status_code == 409
    assert send(env.client, type="home").status_code == 403


def test_release_always_200_and_frees_the_phone(env):
    assert (
        env.client.delete(
            BASE + "/takeover?session_id=nobody", headers=AUTH
        ).status_code
        == 200
    )
    assert takeover(env.client).status_code == 200
    assert wait_for(lambda: env.locks.owner(LOCK_IDENTITY) == "remote")
    # Another session cannot release it.
    assert (
        env.client.delete(BASE + "/takeover?session_id=s2", headers=AUTH).status_code
        == 200
    )
    assert env.remote.holder("phone_A") is not None
    assert (
        env.client.delete(BASE + "/takeover?session_id=s1", headers=AUTH).status_code
        == 200
    )
    assert env.remote.holder("phone_A") is None
    assert env.locks.owner(LOCK_IDENTITY) is None


def test_local_manual_input_is_blocked_while_remote_holds(env):
    assert takeover(env.client).status_code == 200
    assert wait_for(lambda: env.locks.owner(LOCK_IDENTITY) == "remote")
    with pytest.raises(DeviceBusy) as busy:
        with env.locks.hold(LOCK_IDENTITY, "manual"):
            pass
    assert busy.value.owner == "remote"


# -- input -------------------------------------------------------------------


def test_coordinates_are_fractions_of_the_camera_picture(env):
    assert takeover(env.client).status_code == 200
    assert send(env.client, type="tap", x=0.5, y=0.5).status_code == 200
    assert send(env.client, type="tap", x=0.25, y=0.75).status_code == 200
    assert (
        send(env.client, type="swipe", x1=0.5, y1=0.7, x2=0.5, y2=0.3, duration_ms=400)
    ).status_code == 200
    assert env.bridge.calls == [
        ("tap", 0.5, 0.5),
        ("tap", 0.0, 1.0),
        ("swipe", 0.5, 0.9, 0.5, 0.1, 400),
    ]


def test_point_outside_the_phone_is_ignored_with_200(env):
    assert takeover(env.client).status_code == 200
    assert send(env.client, type="tap", x=0.1, y=0.1).status_code == 200
    assert (
        send(env.client, type="swipe", x1=0.5, y1=0.5, x2=0.9, y2=0.5, duration_ms=300)
    ).status_code == 200
    assert env.bridge.calls == []


def test_text_is_passed_literally(env):
    assert takeover(env.client).status_code == 200
    text = "'; $(reboot) \" `id` & echo"
    assert send(env.client, type="text", text=text).status_code == 200
    assert env.bridge.calls == [("text", text)]


def test_keys_and_buttons(env):
    assert takeover(env.client).status_code == 200
    for key in (
        "enter",
        "backspace",
        "delete",
        "tab",
        "space",
        "up",
        "down",
        "left",
        "right",
    ):
        assert send(env.client, type="key", key=key).status_code == 200
    for button in ("home", "back", "wake"):
        assert send(env.client, type=button).status_code == 200
    assert env.bridge.calls == [
        ("key", "ENTER"),
        ("key", "BACKSPACE"),
        ("key", "DELETE"),
        ("key", "TAB"),
        ("text", " "),
        ("key", "UP"),
        ("key", "DOWN"),
        ("key", "LEFT"),
        ("key", "RIGHT"),
        ("key", "HOME"),
        ("key", "BACK"),
        ("key", "SCREEN_ON"),
    ]
    response = send(env.client, type="key", key="escape")
    assert response.status_code == 422
    assert response.get_json() == {"error": "unsupported_input"}


@pytest.mark.parametrize(
    "fields",
    [
        {"type": "tap", "x": 1.5, "y": 0.5},
        {"type": "tap", "x": True, "y": 0.5},
        {"type": "tap", "x": 0.5},
        {"type": "swipe", "x1": 0, "y1": 0, "x2": 1, "y2": 1, "duration_ms": 10},
        {"type": "text", "text": ""},
        {"type": "text", "text": "line\nbreak"},
        {"type": "text", "text": "x" * 201},
        {"type": "key", "key": "f1"},
        {"type": "home", "extra": 1},
        {"type": "launch"},
    ],
)
def test_invalid_input_is_400_and_sends_nothing(env, fields):
    assert takeover(env.client).status_code == 200
    assert send(env.client, **fields).status_code == 400
    assert env.bridge.calls == []


def test_uncalibrated_phone_refuses_points_but_not_buttons(env):
    env.flags.calibration = None
    assert takeover(env.client).status_code == 200
    response = send(env.client, type="tap", x=0.5, y=0.5)
    assert response.status_code == 409
    assert response.get_json() == {"error": "not_calibrated"}
    assert send(env.client, type="home").status_code == 200
    assert send(env.client, type="text", text="abc").status_code == 200
    assert env.bridge.calls == [("key", "HOME"), ("text", "abc")]


def test_input_waits_until_the_stopped_job_lets_go(env):
    # The remote input must not reach the phone until the job has released it.
    run_id = start_run(
        env,
        {"type": "wait", "seconds": 0},
        {"type": "wait", "seconds": 30},
    )
    assert wait_for(lambda: step_number(env, run_id) == 2)
    assert takeover(env.client, allow=True).status_code == 200
    assert send(env.client, type="home").status_code == 200
    assert env.service.snapshot(run_id)["status"] == "cancelled"


@pytest.mark.parametrize(
    "body",
    [
        {"session_id": "s1", "user_id": "alice", "reason": "r"},
        {
            "session_id": "s1",
            "user_id": "alice",
            "reason": "a\nb",
            "allow_during_job": False,
        },
        {
            "session_id": "s1",
            "user_id": "alice",
            "reason": "x" * 201,
            "allow_during_job": False,
        },
        {
            "session_id": "",
            "user_id": "alice",
            "reason": "r",
            "allow_during_job": False,
        },
        {
            "session_id": "s1",
            "user_id": "alice",
            "reason": "r",
            "allow_during_job": "yes",
        },
    ],
)
def test_invalid_takeover_is_400(env, body):
    response = env.client.post(BASE + "/takeover", json=body, headers=AUTH)
    assert response.status_code == 400
    assert env.remote.holder("phone_A") is None


# -- history -----------------------------------------------------------------


def test_history_records_who_why_and_how_it_ended(env):
    assert takeover(env.client).status_code == 200
    assert send(env.client, type="text", text="secret words").status_code == 200
    env.remote.end_by_operator("phone_A")
    ended, started = env.remote.history.recent()[:2]
    assert started["event"] == "started"
    assert started["user_id"] == "alice"
    assert started["reason"] == "payment screen stuck"
    assert ended["event"] == "ended"
    assert ended["ended_by"] == "operator"
    assert ended["inputs"] == 1
    # Typed text is never written to history.
    assert "secret words" not in Path(env.remote.history.path).read_text()


# -- job record --------------------------------------------------------------


@pytest.mark.parametrize(
    "snapshot_status, message",
    [
        ("cancelled", "Script cancelled due to takeover."),
        ("failed", "Device busy (remote). No further input sent."),
    ],
)
def test_job_is_recorded_as_interrupted_by_takeover(snapshot_status, message):
    import threading

    from automation.jobs.executor import BackgroundJobExecutor

    recorded = []
    takeover_details = {
        "session_id": "s-9f2c",
        "user_id": "alice",
        "reason": "payment screen stuck",
        "at": "2026-11-11T10:00:00+00:00",
    }
    scheduler = SimpleNamespace(
        get_job=lambda job_id: SimpleNamespace(cancel_requested=False),
        interrupt_job=lambda job_id, error: recorded.append((job_id, error)),
    )
    service = SimpleNamespace(
        start_for_job=lambda job, workflow, cancel_event=None: {"run_id": "r1"},
        wait=lambda run_id: {
            "status": snapshot_status,
            "message": message,
            "remote_takeover": takeover_details,
        },
    )
    storage = SimpleNamespace(read=lambda workflow_id: {"id": workflow_id, "steps": []})
    executor = BackgroundJobExecutor(scheduler, storage, service)
    job = SimpleNamespace(job_id="job-1", device_id="phone_A", workflow="flow_x")
    executor._execute(job, {"cancel": threading.Event()})
    assert recorded == [
        (
            "job-1",
            "Interrupted by remote takeover: user alice, session s-9f2c, at "
            "2026-11-11T10:00:00+00:00. Reason: payment screen stuck",
        )
    ]


def test_ending_during_an_input_keeps_the_lock_until_it_finishes(env):
    import threading

    started, finish = threading.Event(), threading.Event()

    def slow_key(key):
        started.set()
        finish.wait(5)
        env.bridge.calls.append(("key", key))

    env.bridge.key = slow_key
    assert takeover(env.client).status_code == 200
    assert wait_for(lambda: env.locks.owner(LOCK_IDENTITY) == "remote")
    result = {}
    sender = threading.Thread(
        target=lambda: result.setdefault(
            "status", send(env.client, type="home").status_code
        )
    )
    sender.start()
    assert started.wait(5)
    assert env.remote.end_by_operator("phone_A")
    # The input is still being sent: nobody else may take the phone yet.
    assert env.locks.owner(LOCK_IDENTITY) == "remote"
    finish.set()
    sender.join(5)
    assert result["status"] == 200
    assert env.locks.owner(LOCK_IDENTITY) is None
    # No further input from the ended session.
    assert send(env.client, type="home").status_code == 403


def test_remote_never_takes_the_phone_during_a_critical_manual_checkpoint(
    env, monkeypatch
):
    # A manual checkpoint releases the device lock while it waits. If a remote
    # stop slipped in just as the run entered that critical step, the remote
    # session must still wait until the step is over.
    run_id = start_run(
        env,
        {"type": "wait", "seconds": 0},
        {"type": "manual_checkpoint", "message": "Confirm payment", "critical": True},
        {"type": "wait", "seconds": 30},
    )
    assert wait_for(lambda: env.service.snapshot(run_id)["status"] == "waiting_manual")
    item = env.service.get(run_id)

    def raced_hold(device_id, details, *, allow_during_job, job_pending):
        env.service.remote_holds[device_id] = details
        item["remote_takeover"] = details
        item["remote_stop"].set()
        return {"run_id": run_id, "workflow": item["workflow_id"], "step_index": 1}

    monkeypatch.setattr(env.service, "hold_for_remote", raced_hold)
    assert takeover(env.client, allow=True).status_code == 200
    time.sleep(0.4)
    assert env.locks.owner(LOCK_IDENTITY) is None
    assert env.service.snapshot(run_id)["status"] == "waiting_manual"
    env.service.resume(run_id)
    snapshot = env.service.wait(run_id, 5)
    # The critical step completed; the run stopped at the next step.
    assert snapshot["status"] == "cancelled"
    assert snapshot["step_index"] == 3
    assert wait_for(lambda: env.locks.owner(LOCK_IDENTITY) == "remote")
