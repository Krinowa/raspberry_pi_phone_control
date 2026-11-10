"""Read-only camera frames for the RemoteViewer device agent.

Implements section 2 of RemoteViewer's docs/pi-interface.md:
``GET /agent/v1/devices/<device_id>/frames``. The agent runs on this same PC
and reads uncompressed frames over loopback. Nothing here accepts input for a
phone; it only serves the pictures the dashboard already captures.

The stream is unencrypted and protected only by the loopback address and the
shared secret, so the app must keep listening on 127.0.0.1.
"""

import hmac
import logging
import os
import struct

import numpy as np
from flask import Blueprint, Response, abort, request

LOG = logging.getLogger("agent_frames")

SECRET_FILE_ENV = "REMOTEVIEWER_PI_SECRET_FILE"
MIN_SECRET_LENGTH = 32
MAX_SECRET_FILE_BYTES = 4096
MAX_FRAME_SIDE = 4096
# magic, width, height, pixel format, frame id, capture time (microseconds)
HEADER = struct.Struct("<4sIIIIQ")
PIXEL_FORMAT_BGR24 = 1
FRAME_WAIT_SECONDS = 5.0


def load_secret(path):
    """Read the shared secret from ``path``. Errors never include its value."""
    try:
        with open(path, "rb") as stream:
            raw = stream.read(MAX_SECRET_FILE_BYTES + 1)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot read the RemoteViewer secret file ({exc.strerror})."
        ) from None
    if len(raw) > MAX_SECRET_FILE_BYTES:
        raise RuntimeError("The RemoteViewer secret file is unexpectedly large.")
    try:
        secret = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise RuntimeError("The RemoteViewer secret file is not UTF-8 text.") from None
    if len(secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            "The RemoteViewer secret must be at least "
            f"{MIN_SECRET_LENGTH} characters."
        )
    return secret


def secret_from_environment():
    """Return the configured secret, or None when the endpoint is not set up."""
    path = os.environ.get(SECRET_FILE_ENV, "").strip()
    return load_secret(path) if path else None


def make_blueprint(camera_manager, secret, known_device):
    """secret: str, or None to refuse every request. known_device: id -> bool."""
    blueprint = Blueprint("agent_v1", __name__, url_prefix="/agent/v1")
    expected = None if secret is None else ("Bearer " + secret).encode()

    @blueprint.before_request
    def require_secret():
        supplied = request.headers.get("Authorization", "").encode()
        # Without a configured secret nothing can match: fail closed.
        if expected is None or not hmac.compare_digest(supplied, expected):
            abort(401)

    @blueprint.get("/devices/<device_id>/frames")
    def frames(device_id):
        if not known_device(device_id):
            abort(404)
        camera = camera_manager.get_camera(device_id)
        wait_raw_frame = getattr(camera, "wait_raw_frame", None)

        def stream():
            if wait_raw_frame is None:
                return  # no camera assigned or connected; the agent retries
            # Start after the frame already held so the first one sent is fresh.
            sent = getattr(camera, "sequence", 0)
            while camera_manager.is_current(device_id, camera):
                item = wait_raw_frame(sent, FRAME_WAIT_SECONDS)
                if item is None:
                    return  # camera stopped; the agent reconnects by itself
                sent, captured_at_us, bgr = item
                height, width = bgr.shape[:2]
                # The agent requires even sizes: crop one pixel if odd.
                height -= height % 2
                width -= width % 2
                if not 0 < width <= MAX_FRAME_SIDE or not 0 < height <= MAX_FRAME_SIDE:
                    LOG.warning(
                        "Frame size %dx%d for %s is outside the agent's limits.",
                        width,
                        height,
                        device_id,
                    )
                    return
                yield HEADER.pack(
                    b"RVF1",
                    width,
                    height,
                    PIXEL_FORMAT_BGR24,
                    sent & 0xFFFFFFFF,
                    captured_at_us,
                )
                # tobytes() copies, so the capture loop cannot alter sent data.
                yield np.ascontiguousarray(bgr[:height, :width]).tobytes()

        return Response(
            stream(),
            mimetype="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )

    return blueprint
