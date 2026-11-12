"""Tests for the RemoteViewer frame endpoint (agent_frames.py).

Run from camera_phone_control:  python -m pytest tests/test_agent_frames.py
"""

import struct
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_frames  # noqa: E402
import camera as camera_module  # noqa: E402
from camera import Camera  # noqa: E402
from camera_manager import CameraConfigStorage, CameraManager  # noqa: E402

SECRET = "test-only-secret-0123456789abcdefghij"
AUTH = {"Authorization": "Bearer " + SECRET}
URL = "/agent/v1/devices/phone_A/frames"
HEADER = struct.Struct("<4sIIIIQ")
# Odd on purpose: the endpoint must crop to 640x360.
CAMERA_WIDTH, CAMERA_HEIGHT = 641, 361


class FakeCapture:
    """Stands in for cv2.VideoCapture; every frame is filled with its number."""

    def __init__(self, *args):
        self.count = 0

    def isOpened(self):
        return True

    def set(self, prop, value):
        return True

    def get(self, prop):
        return 0.0

    def getBackendName(self):
        return "FAKE"

    def read(self):
        self.count += 1
        value = self.count % 256
        return True, np.full((CAMERA_HEIGHT, CAMERA_WIDTH, 3), value, np.uint8)

    def release(self):
        pass


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(camera_module.cv2, "VideoCapture", FakeCapture)

    def factory(source, width, height, fps, rotation):
        return Camera(source, width, height, 50, rotation, use_capture_subprocess=False)

    manager = CameraManager(
        storage=CameraConfigStorage(tmp_path / "cameras.json"),
        camera_factory=factory,
        identity_discovery=False,
    )
    yield manager
    manager.release_all()


def make_client(manager, secret=SECRET, known=("phone_A",)):
    app = Flask(__name__)
    app.register_blueprint(
        agent_frames.make_blueprint(manager, secret, lambda device_id: device_id in known)
    )
    return app.test_client()


def read_frames(response, count):
    """Parse ``count`` frames from a streamed response."""
    chunks = response.iter_encoded()
    buffer = b""
    frames = []
    while len(frames) < count:
        while len(buffer) < HEADER.size:
            buffer += next(chunks)
        magic, width, height, pixel_format, frame_id, captured = HEADER.unpack(
            buffer[: HEADER.size]
        )
        size = width * height * 3
        while len(buffer) < HEADER.size + size:
            buffer += next(chunks)
        pixels = buffer[HEADER.size : HEADER.size + size]
        buffer = buffer[HEADER.size + size :]
        frames.append((magic, width, height, pixel_format, frame_id, captured, pixels))
    return frames


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-secret-0123456789abcdefghijkl"},
        {"Authorization": SECRET},
        {"Authorization": "Bearer " + SECRET + "x"},
        {"Authorization": "Bearer " + SECRET[:-1]},
        {"Authorization": "Basic " + SECRET},
        {"Authorization": "Bearer "},
    ],
)
def test_missing_or_wrong_secret_is_401(manager, headers):
    assert make_client(manager).get(URL, headers=headers).status_code == 401


def test_everything_is_401_when_no_secret_is_configured(manager):
    client = make_client(manager, secret=None)
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers={"Authorization": "Bearer "}).status_code == 401
    assert client.get(URL, headers=AUTH).status_code == 401


def test_unknown_device_is_404_only_after_authorisation(manager):
    client = make_client(manager)
    unknown = "/agent/v1/devices/phone_missing/frames"
    assert client.get(unknown, headers=AUTH).status_code == 404
    assert client.get(unknown).status_code == 401


def test_stream_has_the_documented_frame_format(manager):
    response = make_client(manager).get(URL, headers=AUTH, buffered=False)
    try:
        assert response.status_code == 200
        assert response.mimetype == "application/octet-stream"
        assert response.headers["Cache-Control"] == "no-store"
        frames = read_frames(response, 3)
    finally:
        response.close()
    now_us = time.time_ns() // 1000
    previous_id = None
    for magic, width, height, pixel_format, frame_id, captured, pixels in frames:
        assert magic == b"RVF1"
        assert (width, height) == (640, 360)  # cropped to even
        assert pixel_format == 1
        assert len(pixels) == 640 * 360 * 3
        # The fake camera fills frame N with N, and the id is the capture count.
        assert set(pixels) == {frame_id % 256}
        assert 0 <= now_us - captured < 500_000
        assert previous_id is None or frame_id > previous_id
        previous_id = frame_id


def test_slow_reader_skips_frames_instead_of_queueing(manager):
    response = make_client(manager).get(URL, headers=AUTH, buffered=False)
    try:
        first = read_frames(response, 1)[0]
        time.sleep(0.4)  # about 20 frames pass at the test camera's 50 fps
        second = read_frames(response, 1)[0]
        third = read_frames(response, 1)[0]
    finally:
        response.close()
    assert second[4] > first[4]
    assert third[4] - first[4] > 5
    assert time.time_ns() // 1000 - third[5] < 500_000


def test_stream_ends_when_the_camera_stops(manager, monkeypatch):
    monkeypatch.setattr(agent_frames, "FRAME_WAIT_SECONDS", 0.2)
    response = make_client(manager).get(URL, headers=AUTH, buffered=False)
    try:
        read_frames(response, 1)
        manager.get_camera("phone_A").close()
        started = time.monotonic()
        for _ in response.iter_encoded():
            pass
    finally:
        response.close()
    assert time.monotonic() - started < 2


def test_device_without_a_camera_gives_an_empty_stream(manager):
    # Registered on the Pi, but no camera has been assigned to it yet.
    response = make_client(manager, known=("phone_A", "phone_B")).get(
        "/agent/v1/devices/phone_B/frames", headers=AUTH
    )
    assert response.status_code == 200
    assert response.data == b""


def test_jpeg_only_frames_are_decoded():
    """The macOS capture subprocess hands over JPEG only."""
    camera = Camera(use_capture_subprocess=False)
    picture = np.full((48, 64, 3), 200, np.uint8)
    ok, encoded = cv2.imencode(".jpg", picture)
    assert ok
    with camera.frame_condition:
        camera.sequence = 7
        camera.latest = (encoded.tobytes(), 7, 64, 48)
        camera.latest_raw = (None, 1_700_000_000_000_000)
        camera.frame_condition.notify_all()
    sequence, captured, bgr = camera.wait_raw_frame(6, timeout=1)
    assert (sequence, captured) == (7, 1_700_000_000_000_000)
    assert bgr.shape == (48, 64, 3)
    assert abs(int(bgr[0, 0, 0]) - 200) <= 2
    assert camera.wait_raw_frame(7, timeout=0.05) is None


def test_load_secret_strips_whitespace(tmp_path):
    path = tmp_path / "pi.secret"
    path.write_text(SECRET + "\r\n")
    assert agent_frames.load_secret(path) == SECRET


def test_load_secret_rejects_short_secret_without_revealing_it(tmp_path):
    path = tmp_path / "pi.secret"
    path.write_text("too-short-value\n")
    with pytest.raises(RuntimeError) as error:
        agent_frames.load_secret(path)
    assert "too-short-value" not in str(error.value)
    assert "at least 32" in str(error.value)


def test_load_secret_reports_a_missing_file(tmp_path):
    with pytest.raises(RuntimeError):
        agent_frames.load_secret(tmp_path / "absent.secret")


def test_secret_from_environment(tmp_path, monkeypatch):
    monkeypatch.delenv(agent_frames.SECRET_FILE_ENV, raising=False)
    assert agent_frames.secret_from_environment() is None
    path = tmp_path / "pi.secret"
    path.write_text(SECRET)
    monkeypatch.setenv(agent_frames.SECRET_FILE_ENV, str(path))
    assert agent_frames.secret_from_environment() == SECRET
