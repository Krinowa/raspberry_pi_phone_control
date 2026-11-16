"""Shared simulated camera fixture for dashboard/ownership regression tests."""
import time

CORNERS = [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]


class FakeCamera:
    def __init__(self):
        self.ready = True
        self.width, self.height = 1280, 720

    def status(self):
        return {"ready": self.ready, "width": self.width, "height": self.height,
                "index": 0, "error": "Camera unavailable." if not self.ready else None}

    def validate_frame(self, frame_id):
        if not self.ready or type(frame_id) is not int or frame_id != 123:
            raise ValueError("A fresh camera frame is required.")

    def frame(self):
        return b"simulated-jpeg", 123, time.monotonic(), (self.width, self.height)


def authenticate_admin(client):
    """Give legacy Admin-route tests a signed session without testing login again."""
    with client.session_transaction() as session:
        session["username"] = "test-admin"
        session["role"] = "admin"
    return client
