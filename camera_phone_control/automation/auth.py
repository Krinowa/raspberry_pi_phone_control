"""Local SQLite-backed dashboard authentication with TOTP two-factor login."""

from __future__ import annotations

import base64
import io
import os
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

DEFAULT_AUTH_PATH = Path(__file__).resolve().parents[1] / "data" / "auth.db"
MIN_PASSWORD_LENGTH = 8
RECOVERY_CODE_COUNT = 10


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _totp_module():
    try:
        import pyotp
    except ImportError as exc:
        raise RuntimeError(
            "TOTP authentication requires the pyotp dependency."
        ) from exc
    return pyotp


def load_or_create_secret_key(data_dir):
    configured = os.environ.get("PHONE_DASHBOARD_SECRET_KEY")
    if configured:
        return configured
    path = Path(
        os.environ.get("PHONE_DASHBOARD_SECRET_FILE")
        or Path(data_dir) / "session_secret.key"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        value = path.read_text(encoding="ascii").strip()
        if len(value) < 32:
            raise ValueError("The dashboard session secret file is invalid.")
        return value
    value = secrets.token_urlsafe(48)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(value + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return value


class AttemptLimiter:
    def __init__(self, *, attempts=5, window_seconds=60, cooldown_seconds=60):
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self._attempts = defaultdict(deque)
        self._blocked_until = {}
        self._lock = threading.Lock()

    def check(self, key):
        now = time.monotonic()
        with self._lock:
            if self._blocked_until.get(key, 0) > now:
                return False
            values = self._attempts[key]
            while values and values[0] <= now - self.window_seconds:
                values.popleft()
            return len(values) < self.attempts

    def fail(self, key):
        now = time.monotonic()
        with self._lock:
            values = self._attempts[key]
            values.append(now)
            if len(values) >= self.attempts:
                self._blocked_until[key] = now + self.cooldown_seconds

    def success(self, key):
        with self._lock:
            self._attempts.pop(key, None)
            self._blocked_until.pop(key, None)


class UserStore:
    """Authentication-only SQLite storage; application data remains file-backed."""

    def __init__(self, path=None):
        default_path = (
            Path(os.environ["PHONE_APP_DATA_DIR"]) / "auth.db"
            if os.environ.get("PHONE_APP_DATA_DIR")
            else DEFAULT_AUTH_PATH
        )
        self.path = Path(path or default_path)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    totp_secret TEXT NOT NULL,
                    two_factor_enabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recovery_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    code_hash TEXT NOT NULL,
                    used_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS recovery_codes_user_unused
                    ON recovery_codes(user_id, used_at);
                """)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _user(row):
        return dict(row) if row is not None else None

    def has_users(self):
        with self._lock, self._connect() as connection:
            return bool(connection.execute("SELECT 1 FROM users LIMIT 1").fetchone())

    def has_enabled_users(self):
        with self._lock, self._connect() as connection:
            return bool(
                connection.execute(
                    "SELECT 1 FROM users WHERE two_factor_enabled = 1 LIMIT 1"
                ).fetchone()
            )

    def get_user(self, user_id):
        if type(user_id) is not int:
            return None
        with self._lock, self._connect() as connection:
            return self._user(
                connection.execute(
                    "SELECT * FROM users WHERE id = ?", (user_id,)
                ).fetchone()
            )

    def create_pending_admin(self, username, password, confirm_password):
        username = username.strip() if isinstance(username, str) else ""
        if not username:
            raise ValueError("Username cannot be empty.")
        if len(username) > 80:
            raise ValueError("Username must be at most 80 characters.")
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )
        if password != confirm_password:
            raise ValueError("Password confirmation does not match.")
        secret = _totp_module().random_base32()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
            if existing is not None:
                if not existing["two_factor_enabled"] and check_password_hash(
                    existing["password_hash"], password
                ):
                    return self._user(existing)
                raise ValueError("That username already exists.")
            if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise ValueError(
                    "Administrator setup is already in progress. Use the original username and password to continue."
                )
            cursor = connection.execute(
                """
                INSERT INTO users (
                    username, password_hash, totp_secret,
                    two_factor_enabled, created_at
                ) VALUES (?, ?, ?, 0, ?)
                """,
                (username, generate_password_hash(password), secret, _utc_now()),
            )
            user_id = cursor.lastrowid
        return self.get_user(user_id)

    def create_admin(self, username, password):
        """Compatibility helper; interactive setup always confirms TOTP."""
        user = self.create_pending_admin(username, password, password)
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE users SET two_factor_enabled = 1 WHERE id = ?", (user["id"],)
            )
        return {"id": user["id"], "username": user["username"], "role": "admin"}

    def authenticate_admin(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ?", (username.strip(),)
            ).fetchone()
        if row is None or not check_password_hash(row["password_hash"], password):
            return None
        return self._user(row)

    def complete_setup(self, user_id):
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        codes = [
            "".join(secrets.choice(alphabet) for _ in range(5))
            + "-"
            + "".join(secrets.choice(alphabet) for _ in range(5))
            for _ in range(RECOVERY_CODE_COUNT)
        ]
        created_at = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE users SET two_factor_enabled = 1 WHERE id = ?", (user_id,)
            )
            connection.execute(
                "DELETE FROM recovery_codes WHERE user_id = ?", (user_id,)
            )
            connection.executemany(
                """
                INSERT INTO recovery_codes (user_id, code_hash, used_at, created_at)
                VALUES (?, ?, NULL, ?)
                """,
                [(user_id, generate_password_hash(code), created_at) for code in codes],
            )
        return codes

    def use_recovery_code(self, user_id, code):
        if not isinstance(code, str) or not code.strip():
            return False
        candidate = code.strip().upper()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT id, code_hash FROM recovery_codes
                   WHERE user_id = ? AND used_at IS NULL""",
                (user_id,),
            ).fetchall()
            # Evaluate every unused hash so a valid code's position does not
            # create an avoidable early-return timing signal.
            matches = [
                row["id"]
                for row in rows
                if check_password_hash(row["code_hash"], candidate)
            ]
            matched_id = matches[0] if matches else None
            if matched_id is None:
                return False
            changed = connection.execute(
                """UPDATE recovery_codes SET used_at = ?
                   WHERE id = ? AND used_at IS NULL""",
                (_utc_now(), matched_id),
            ).rowcount
            return changed == 1


def authenticated_admin():
    user_id = session.get("user_id")
    if type(user_id) is int and user_id > 0:
        return True
    username = session.get("username")
    return (
        isinstance(username, str) and bool(username) and session.get("role") == "admin"
    )


def require_admin():
    if not authenticated_admin():
        return jsonify(ok=False, error="Admin login required."), 403
    return None


def admin_required(view):
    @wraps(view)
    def protected(*args, **kwargs):
        denied = require_admin()
        return denied if denied is not None else view(*args, **kwargs)

    return protected


def _csrf_token():
    token = session.get("auth_csrf")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session["auth_csrf"] = token
    return token


def _valid_csrf():
    expected = session.get("auth_csrf", "")
    supplied = request.form.get("csrf_token", "")
    return bool(expected and secrets.compare_digest(expected, supplied))


def _qr_data_uri(uri):
    try:
        import qrcode
    except ImportError as exc:
        raise RuntimeError("Authenticator QR generation requires qrcode[pil].") from exc
    output = io.BytesIO()
    qrcode.make(uri).save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode(
        "ascii"
    )


def register_auth(app, payload, store):
    limiter = AttemptLimiter()
    app.extensions["auth_attempt_limiter"] = limiter

    def render(mode, *, error=None, status=200, **context):
        return (
            render_template(
                "auth.html",
                mode=mode,
                error=error,
                csrf_token=_csrf_token(),
                **context,
            ),
            status,
        )

    def rate_key(action, username=""):
        return (request.remote_addr or "local", action, username.casefold())

    def rate_limited(key):
        if limiter.check(key):
            return None
        return render(
            "message",
            error="Too many attempts. Wait one minute and try again.",
            status=429,
            title="Please wait",
        )

    def finish_login(user):
        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["role"] = "admin"
        _csrf_token()

    @app.route("/setup", methods=["GET", "POST"])
    def auth_setup():
        if store.has_enabled_users():
            return redirect(url_for("auth_login_page"))
        if request.method == "GET":
            return render("setup")
        if not _valid_csrf():
            return render(
                "setup", error="The setup form expired. Try again.", status=400
            )
        key = rate_key("setup", request.form.get("username", ""))
        limited = rate_limited(key)
        if limited:
            return limited
        try:
            user = store.create_pending_admin(
                request.form.get("username"),
                request.form.get("password"),
                request.form.get("confirm_password"),
            )
        except ValueError as exc:
            limiter.fail(key)
            return render("setup", error=str(exc), status=400)
        limiter.success(key)
        session.clear()
        session["setup_user_id"] = user["id"]
        return redirect(url_for("auth_setup_verify"))

    @app.route("/setup/verify", methods=["GET", "POST"])
    def auth_setup_verify():
        user = store.get_user(session.get("setup_user_id"))
        if user is None or user["two_factor_enabled"]:
            return redirect(url_for("auth_setup"))
        totp = _totp_module().TOTP(user["totp_secret"])
        error = None
        if request.method == "POST":
            if not _valid_csrf():
                return redirect(url_for("auth_setup_verify"))
            key = rate_key("setup-totp", user["username"])
            limited = rate_limited(key)
            if limited:
                return limited
            if not totp.verify(request.form.get("code", "").strip(), valid_window=1):
                limiter.fail(key)
                error = "Invalid authentication code."
            else:
                limiter.success(key)
                codes = store.complete_setup(user["id"])
                finish_login(user)
                return render(
                    "recovery_codes",
                    title="Save your recovery codes",
                    recovery_codes=codes,
                )
        uri = totp.provisioning_uri(user["username"], issuer_name="Phone Controller")
        return render(
            "setup_verify",
            error=error,
            username=user["username"],
            manual_secret=user["totp_secret"],
            qr_data_uri=_qr_data_uri(uri),
        )

    @app.route("/login", methods=["GET", "POST"])
    def auth_login_page():
        if authenticated_admin():
            return redirect(url_for("index"))
        if not store.has_enabled_users():
            return redirect(url_for("auth_setup"))
        if request.method == "GET":
            return render("login")
        if not _valid_csrf():
            return render(
                "login", error="The login form expired. Try again.", status=400
            )
        username = request.form.get("username", "")
        key = rate_key("password", username)
        limited = rate_limited(key)
        if limited:
            return limited
        user = store.authenticate_admin(username, request.form.get("password", ""))
        if user is None or not user["two_factor_enabled"]:
            limiter.fail(key)
            return render("login", error="Invalid username or password.", status=401)
        limiter.success(key)
        session.clear()
        session["pending_2fa_user_id"] = user["id"]
        return redirect(url_for("auth_verify"))

    @app.route("/login/2fa", methods=["GET", "POST"])
    def auth_verify():
        user = store.get_user(session.get("pending_2fa_user_id"))
        if user is None or not user["two_factor_enabled"]:
            session.clear()
            return redirect(url_for("auth_login_page"))
        if request.method == "GET":
            return render("verify", username=user["username"])
        if not _valid_csrf():
            return redirect(url_for("auth_verify"))
        key = rate_key("totp", user["username"])
        limited = rate_limited(key)
        if limited:
            return limited
        valid = (
            _totp_module()
            .TOTP(user["totp_secret"])
            .verify(request.form.get("code", "").strip(), valid_window=1)
        )
        if not valid:
            limiter.fail(key)
            return render(
                "verify",
                error="Invalid authentication code.",
                status=401,
                username=user["username"],
            )
        limiter.success(key)
        finish_login(user)
        return redirect(url_for("index", mode="admin"))

    @app.route("/login/recovery", methods=["GET", "POST"])
    def auth_recovery():
        user = store.get_user(session.get("pending_2fa_user_id"))
        if user is None or not user["two_factor_enabled"]:
            session.clear()
            return redirect(url_for("auth_login_page"))
        if request.method == "GET":
            return render("recovery", username=user["username"])
        if not _valid_csrf():
            return redirect(url_for("auth_recovery"))
        key = rate_key("recovery", user["username"])
        limited = rate_limited(key)
        if limited:
            return limited
        if not store.use_recovery_code(user["id"], request.form.get("code", "")):
            limiter.fail(key)
            return render(
                "recovery",
                error="Invalid or already used recovery code.",
                status=401,
                username=user["username"],
            )
        limiter.success(key)
        finish_login(user)
        return redirect(url_for("index", mode="admin"))

    @app.post("/logout")
    def auth_logout_page():
        if not _valid_csrf():
            return redirect(url_for("index"))
        session.clear()
        return redirect(url_for("index"))

    @app.get("/api/auth/session")
    def auth_session():
        if not authenticated_admin():
            return jsonify(
                authenticated=False,
                username=None,
                role=None,
                admin_configured=store.has_enabled_users(),
                developer_mode=False,
            )
        return jsonify(
            authenticated=True,
            username=session["username"],
            role="admin",
            admin_configured=True,
            developer_mode=session.get("developer_mode") is True,
        )

    @app.post("/api/auth/login")
    def auth_login_api():
        data = payload()
        if set(data) != {"username", "password"}:
            return jsonify(ok=False, error="Invalid username or password."), 401
        key = rate_key("api-password", data.get("username", ""))
        if not limiter.check(key):
            return jsonify(ok=False, error="Too many attempts. Try again later."), 429
        user = store.authenticate_admin(data["username"], data["password"])
        if user is None or not user["two_factor_enabled"]:
            limiter.fail(key)
            session.clear()
            return jsonify(ok=False, error="Invalid username or password."), 401
        limiter.success(key)
        session.clear()
        session["pending_2fa_user_id"] = user["id"]
        return jsonify(authenticated=False, requires_2fa=True)

    @app.post("/api/auth/verify")
    def auth_verify_api():
        data = payload()
        user = store.get_user(session.get("pending_2fa_user_id"))
        if user is None or set(data) != {"code"}:
            return jsonify(ok=False, error="Invalid authentication code."), 401
        key = rate_key("api-totp", user["username"])
        if not limiter.check(key):
            return jsonify(ok=False, error="Too many attempts. Try again later."), 429
        if (
            not _totp_module()
            .TOTP(user["totp_secret"])
            .verify(data["code"], valid_window=1)
        ):
            limiter.fail(key)
            return jsonify(ok=False, error="Invalid authentication code."), 401
        limiter.success(key)
        finish_login(user)
        return jsonify(authenticated=True, username=user["username"], role="admin")

    @app.post("/api/auth/logout")
    def auth_logout():
        session.clear()
        return jsonify(ok=True, authenticated=False)
