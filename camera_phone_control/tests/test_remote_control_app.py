"""End-to-end check of remote takeover inside the real dashboard app.

The Pi is replaced by an in-memory HTTP client; everything else (blueprint,
same-origin protection, device locks, workflow service, job eligibility, the
Pi workflow input bridge) is the production code.

Run from camera_phone_control:  python -m pytest tests/test_remote_control_app.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module  # noqa: E402
import automation.input.raspberry_pi as pi_input  # noqa: E402
import camera as camera_module  # noqa: E402
import remote_control  # noqa: E402
from calibration import Calibration, CalibrationStorage  # noqa: E402
from camera import Camera  # noqa: E402
from camera_manager import CameraConfigStorage, CameraManager  # noqa: E402
from device_lock import DeviceLocks  # noqa: E402

SECRET = "test-only-secret-0123456789abcdefghij"
AUTH = {"Authorization": "Bearer " + SECRET}
BASE = "/agent/v1/devices/phone_A"
PI_URL = "http://pi.test:8080"


class FakePi:
    """Records physical commands sent to the Pi; one instance per device id."""

    sent = []

    def __init__(
        self, base_url=None, connect_timeout=None, read_timeout=None, *, device_id=None
    ):
        self.base_url = base_url or PI_URL
        self.device_id = device_id or "phone_A"
        self.last_command_id = None
        self.last_action_status = None
        self.last_action_http_status = None

    def devices(self):
        return {"devices": [{"id": "phone_A", "connected": True}]}

    def status(self):
        return {"connected": True}

    def health(self):
        return {"ok": True}

    def tap(self, x, y):
        FakePi.sent.append((self.device_id, "tap", round(x, 6), round(y, 6)))

    def swipe(self, x1, y1, x2, y2):
        FakePi.sent.append((self.device_id, "swipe", x1, y1, x2, y2))

    def type_text(self, text):
        FakePi.sent.append((self.device_id, "text", text))

    def key(self, key):
        FakePi.sent.append((self.device_id, "key", key))

    def home(self):
        FakePi.sent.append((self.device_id, "home"))

    def back(self):
        FakePi.sent.append((self.device_id, "back"))

    def wake(self):
        FakePi.sent.append((self.device_id, "wake"))


class FakeCapture:
    def __init__(self, *args):
        pass

    def isOpened(self):
        return True

    def set(self, prop, value):
        return True

    def get(self, prop):
        return 0.0

    def getBackendName(self):
        return "FAKE"

    def read(self):
        return True, np.zeros((360, 640, 3), np.uint8)

    def release(self):
        pass


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    FakePi.sent = []
    monkeypatch.setattr(camera_module.cv2, "VideoCapture", FakeCapture)
    monkeypatch.setattr(pi_input, "PiApiClient", FakePi)
    monkeypatch.setattr(app_module, "PiApiClient", FakePi)
    monkeypatch.setenv("PHONE_INPUT_BACKEND", "raspberry_pi")
    monkeypatch.setenv("PHONE_SCREEN_BACKEND", "mock")

    def factory(source, width, height, fps, rotation):
        return Camera(source, width, height, 50, rotation, use_capture_subprocess=False)

    manager = CameraManager(
        storage=CameraConfigStorage(tmp_path / "cameras.json"),
        camera_factory=factory,
        identity_discovery=False,
    )
    flask_app = app_module.create_app(
        None,
        device_locks=DeviceLocks(tmp_path / "locks"),
        manual_backend="raspberry_pi",
        pi_client=FakePi(PI_URL),
        calibration_storage=CalibrationStorage(tmp_path / "calibration.json"),
        camera_manager=manager,
        agent_secret=SECRET,
        remote_history=remote_control.RemoteHistory(tmp_path / "history.jsonl"),
        **stores(tmp_path),
    )
    state_calibration = Calibration(
        [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]]
    )
    yield SimpleNamespace(
        app=flask_app, client=flask_app.test_client(), calibration=state_calibration
    )
    flask_app.extensions["remote_control"].close()
    manager.release_all()


def stores(tmp_path):
    from automation.jobs.storage import (
        AccountBindingStore,
        DeviceWorkflowAssignmentStore,
        JobStore,
    )
    from automation.workflows.storage import WorkflowStorage

    return {
        "workflow_storage": WorkflowStorage(tmp_path / "workflows"),
        "account_store": AccountBindingStore(tmp_path / "accounts.json"),
        "job_store": JobStore(tmp_path / "jobs.json"),
        "device_workflow_store": DeviceWorkflowAssignmentStore(
            tmp_path / "device_workflows.json"
        ),
    }


def test_agent_post_needs_no_control_token_but_needs_the_secret(dashboard):
    body = {
        "session_id": "s1",
        "user_id": "tester",
        "reason": "test",
        "allow_during_job": False,
    }
    assert dashboard.client.post(BASE + "/takeover", json=body).status_code == 401
    response = dashboard.client.post(BASE + "/takeover", json=body, headers=AUTH)
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {"interrupted_job": None}
    assert (
        dashboard.client.delete(BASE + "/takeover?session_id=s1", headers=AUTH)
    ).status_code == 200
    # Dashboard endpoints still require the control token.
    assert (
        dashboard.client.post(
            "/api/devices/phone_A/remote-control/end", json={}
        ).status_code
        == 403
    )


def test_tap_reaches_the_pi_through_the_workflow_input_bridge(dashboard, monkeypatch):
    remote = dashboard.app.extensions["remote_control"]
    real_open = remote.open_input

    def open_with_calibration(device_id):
        bridge, _ = real_open(device_id)
        return bridge, dashboard.calibration

    monkeypatch.setattr(remote, "open_input", open_with_calibration)
    body = {
        "session_id": "s1",
        "user_id": "tester",
        "reason": "test",
        "allow_during_job": False,
    }
    assert (
        dashboard.client.post(BASE + "/takeover", json=body, headers=AUTH).status_code
        == 200
    )
    for fields in (
        {"type": "tap", "x": 0.5, "y": 0.5},
        {"type": "text", "text": "'; $(reboot) \""},
        {"type": "home"},
        {"type": "key", "key": "enter"},
    ):
        response = dashboard.client.post(
            BASE + "/input", json={"session_id": "s1", **fields}, headers=AUTH
        )
        assert response.status_code == 200, response.get_json()
    assert FakePi.sent == [
        ("phone_A", "tap", 0.5, 0.5),
        ("phone_A", "text", "'; $(reboot) \""),
        ("phone_A", "home"),
        ("phone_A", "key", "ENTER"),
    ]
    status = dashboard.client.get("/api/remote-control").get_json()
    assert status["active"][0]["user_id"] == "tester"


def test_held_phone_is_not_eligible_for_new_jobs(dashboard):
    body = {
        "session_id": "s1",
        "user_id": "tester",
        "reason": "test",
        "allow_during_job": False,
    }
    assert (
        dashboard.client.post(BASE + "/takeover", json=body, headers=AUTH).status_code
        == 200
    )
    readiness = dashboard.app.extensions["device_eligibility"]
    assert "remote_control" in readiness.inspect("phone_A")["reasons"]


def test_only_enrolled_phones_can_be_controlled(dashboard, monkeypatch):
    # A camera assignment alone gives frames, not control.
    manager = dashboard.app.extensions["camera_manager"]
    configs = dict(manager.all_configs())
    configs["phone_B"] = configs.get("phone_A")
    monkeypatch.setattr(manager, "all_configs", lambda: configs)
    body = {
        "session_id": "s1",
        "user_id": "tester",
        "reason": "test",
        "allow_during_job": False,
    }
    response = dashboard.client.post(
        "/agent/v1/devices/phone_B/takeover", json=body, headers=AUTH
    )
    assert response.status_code == 404
    assert (
        dashboard.client.post(BASE + "/takeover", json=body, headers=AUTH).status_code
        == 200
    )
