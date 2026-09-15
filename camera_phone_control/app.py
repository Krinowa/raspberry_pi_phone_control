"""Local external-camera dashboard. Every Android input comes from a browser action."""

import argparse
import logging
import secrets
import threading
import time
from dataclasses import asdict

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from android_control import AndroidControl, DeviceError, validate_key
from calibration import Calibration
from camera import Camera

LOG = logging.getLogger("dashboard")
LEASE_SECONDS = 6


class ControlState:
    def __init__(self, android, camera):
        self.android, self.camera = android, camera
        self.lock = threading.RLock()
        self.action_lock = threading.Lock()
        self.device = None
        self.calibration = None
        self.signature = None
        self.owner = None
        self.expires = 0
        self.message = "Input disabled. Calibrate the phone display first."

    def disable(self, reason="Input disabled."):
        if self.owner:
            LOG.info("Control disabled: %s", reason)
        self.owner, self.expires, self.message = None, 0, reason

    def refresh(self):
        errors = []
        try:
            self.device = self.android.device()
        except DeviceError as exc:
            self.device = None
            errors.append(str(exc))
        camera = self.camera.status()
        if not camera["ready"]:
            errors.append(camera["error"])
        signature = (self.device, camera["width"], camera["height"])
        if errors:
            self.calibration = None
            self.disable(" ".join(errors))
        elif self.calibration and signature != self.signature:
            self.calibration = None
            self.disable(
                "Phone geometry or camera resolution changed. Recalibrate before enabling input."
            )
        elif self.owner and time.monotonic() >= self.expires:
            self.disable("Control heartbeat expired. Enable Manual Control again.")
        return camera

    def require_frame(self, frame_id):
        try:
            self.camera.validate_frame(frame_id)
        except ValueError as exc:
            self.disable(str(exc))
            raise

    def require_ready(self):
        camera = self.refresh()
        if not self.device or not camera["ready"]:
            raise DeviceError(self.message)
        return camera

    def require_input(self, client, frame_id):
        self.require_ready()
        self.require_frame(frame_id)
        if (
            self.owner != client
            or time.monotonic() >= self.expires
            or self.calibration is None
        ):
            raise ValueError("Input disabled. Enable Manual Control in this tab first.")

    def snapshot(self, client):
        camera = self.refresh()
        return {
            "device": asdict(self.device) if self.device else None,
            "camera": camera,
            "calibrated": self.calibration is not None,
            "corners": self.calibration.corners.tolist() if self.calibration else [],
            "enabled": self.owner is not None,
            "owned": self.owner == client and self.owner is not None,
            "message": self.message,
        }


def create_app(camera, android=None):
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=8192, TRUSTED_HOSTS=["localhost", "127.0.0.1"])
    token = secrets.token_urlsafe(32)
    state = ControlState(android or AndroidControl(), camera)
    app.extensions["control_state"] = state
    app.config["CONTROL_TOKEN"] = token

    @app.before_request
    def protect_local_requests():
        # Reject cross-site fetches/embeds, including camera requests. No CORS enabled.
        if request.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
            return jsonify(error="Only same-origin local requests are accepted."), 403
        if request.method != "GET":
            if not secrets.compare_digest(
                request.headers.get("X-Control-Token", ""), token
            ):
                return jsonify(error="Invalid control token. Reload this page."), 403
            if not request.is_json:
                return jsonify(error="Expected a JSON request."), 415

    @app.after_request
    def local_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.errorhandler(ValueError)
    def invalid_input(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(DeviceError)
    def device_error(exc):
        with state.lock:
            state.disable(str(exc))
        LOG.warning("%s", exc)
        return jsonify(error=str(exc)), 409

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=exc.description), exc.code

    def payload():
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def client_id():
        client = request.headers.get("X-Control-Client", "")
        if not 8 <= len(client) <= 80 or not all(
            c.isalnum() or c == "-" for c in client
        ):
            raise ValueError("Missing browser identifier. Reload the page.")
        return client

    @app.get("/")
    def index():
        return render_template("index.html", control_token=token)

    @app.get("/api/status")
    def status():
        with state.lock:
            return jsonify(state.snapshot(client_id()))

    @app.get("/api/frame")
    def frame():
        if not camera.status()["ready"]:
            return jsonify(error=camera.status()["error"]), 503
        latest = camera.frame()
        if latest is None:
            return jsonify(error="Camera frame unavailable."), 503
        jpeg, number, _, _ = latest
        return Response(
            jpeg, mimetype="image/jpeg", headers={"X-Frame-Id": str(number)}
        )

    @app.post("/api/control")
    def control():
        data, client = payload(), client_id()
        if type(data.get("enabled")) is not bool:
            raise ValueError("enabled must be true or false.")
        with state.lock:
            if not data["enabled"]:
                state.disable()
            else:
                state.require_ready()
                state.require_frame(data.get("frame_id"))
                if state.calibration is None:
                    raise ValueError(
                        "Calibrate the four display corners before enabling input."
                    )
                if state.owner and state.owner != client:
                    raise ValueError(
                        "Another browser tab owns Manual Control. Disable it first."
                    )
                state.owner, state.expires = client, time.monotonic() + LEASE_SECONDS
                state.message = "Manual Control enabled. Each click sends one tap."
                LOG.info("Manual Control enabled")
            return jsonify(enabled=state.owner is not None)

    @app.post("/api/heartbeat")
    def heartbeat():
        data, client = payload(), client_id()
        with state.lock:
            if state.owner == client and time.monotonic() < state.expires:
                state.require_frame(data.get("frame_id"))
                state.expires = time.monotonic() + LEASE_SECONDS

            elif state.owner == client:
                state.disable("Control heartbeat expired. Enable Manual Control again.")
            return jsonify(enabled=state.owner == client)

    @app.route("/api/calibration", methods=["POST", "DELETE"])
    def calibrate():
        data = payload()
        with state.lock:
            state.disable("Calibration changed. Input disabled.")
            state.calibration = None

            if request.method == "DELETE":
                return jsonify(calibrated=False)

            camera_info = state.require_ready()
            state.require_frame(data.get("frame_id"))
            calibration = Calibration(data.get("corners"))
            state.calibration = calibration
            state.signature = (
                state.device,
                camera_info["width"],
                camera_info["height"],
            )
            state.message = "Calibration saved. You can enable Manual Control."
            LOG.info("Four-corner calibration saved for %s", state.device.serial)
            return jsonify(calibrated=True, corners=calibration.corners.tolist())

    @app.post("/api/key")
    def key():
        data, client = payload(), client_id()
        if set(data) != {"type", "value", "frame_id"}:
            raise ValueError("Expected type, value, and frame_id only.")
        validate_key(data["type"], data["value"])
        if type(data["frame_id"]) is not int:
            raise ValueError("A fresh camera frame is required.")
        if not state.action_lock.acquire(blocking=False):
            return jsonify(error="Controller busy. No action sent."), 409
        try:
            with state.lock:
                state.require_input(client, data["frame_id"])
                try:
                    state.android.send_key(state.device, data["type"], data["value"])
                except DeviceError as exc:
                    state.disable(str(exc))
                    raise
                return jsonify(sent=True)
        finally:
            state.action_lock.release()

    @app.post("/api/action")
    def action():
        data, client = payload(), client_id()
        # Drop overlapping inputs instead of building a queue of phone commands.
        if not state.action_lock.acquire(blocking=False):
            return jsonify(error="Controller busy. No action sent."), 409
        state.lock.acquire()

        try:
            state.require_input(client, data.get("frame_id"))

            name = data.get("action")

            if name not in (
                "tap",
                "back",
                "home",
                "recents",
                "swipe_up",
                "swipe_down",
                "swipe_left",
                "swipe_right",
                "screen_on",
                "screen_off",
            ):
                raise ValueError("Unsupported control action.")

            pixel, normalized = None, None

            if name == "tap":
                pixel, normalized = state.calibration.pixel(
                    [data.get("x"), data.get("y")],
                    state.device.width,
                    state.device.height,
                )

            try:
                state.android.send(state.device, name, pixel)
            except DeviceError as exc:
                state.disable(str(exc))
                raise

            return jsonify(action=name, pixel=pixel, normalized=normalized)
        finally:
            state.lock.release()
            state.action_lock.release()

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--camera",
        type=int,
        default=0,
        help="External camera index (default 0)",
    )

    parser.add_argument(
        "--camera-url",
        type=str,
        default=None,
        help="HTTP/MJPEG/RTSP camera stream URL",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8765,
    )

    parser.add_argument(
        "--width",
        type=int,
        default=1280,
    )

    parser.add_argument(
        "--height",
        type=int,
        default=720,
    )

    parser.add_argument(
        "--rotate",
        type=int,
        choices=[0, 90, 180, 270],
        default=0,
        help="Rotate camera image clockwise: 0, 90, 180, or 270 degrees",
    )

    args = parser.parse_args()

    if (
        args.camera < 0
        or not 1 <= args.port <= 65535
        or min(args.width, args.height) < 1
    ):
        parser.error("Invalid camera index, port, or frame size.")

    logging.basicConfig(
        level=logging.INFO,
        format="[%(name)s] %(message)s",
    )

    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    # Use network stream if provided,
    # otherwise use local camera index.
    camera_source = args.camera_url if args.camera_url else args.camera

    camera = Camera(
        camera_source,
        args.width,
        args.height,
        rotate=args.rotate,
    )

    app = create_app(camera)

    camera.start()

    LOG.info(
        "Open http://127.0.0.1:%d — input starts disabled",
        args.port,
    )

    try:
        app.run(
            host="127.0.0.1",
            port=args.port,
            threaded=True,
            debug=False,
            use_reloader=False,
        )

    finally:
        camera.close()


if __name__ == "__main__":
    main()
