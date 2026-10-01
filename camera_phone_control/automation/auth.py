"""Local JSON-backed administrator authentication."""

from __future__ import annotations

import json
import os
import threading
from functools import wraps
from pathlib import Path

from flask import jsonify, session
from werkzeug.security import check_password_hash, generate_password_hash

DEFAULT_USER_PATH = Path(__file__).resolve().parent / "auth_data" / "users.json"


class UserStore:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_USER_PATH)
        self._lock = threading.RLock()

    def _read(self):
        if not self.path.exists():
            return {"users": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read admin user store: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("users"), list):
            raise ValueError("Admin user store must contain a users list.")
        return data

    def authenticate_admin(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        with self._lock:
            users = self._read()["users"]
        user = next(
            (
                item
                for item in users
                if isinstance(item, dict) and item.get("username") == username
            ),
            None,
        )
        if (
            not user
            or user.get("enabled") is not True
            or user.get("role") != "admin"
            or not isinstance(user.get("password_hash"), str)
            or not check_password_hash(user["password_hash"], password)
        ):
            return None
        return {"username": user["username"], "role": "admin"}

    def create_admin(self, username, password):
        username = username.strip() if isinstance(username, str) else ""
        if not username:
            raise ValueError("Username cannot be empty.")
        if not isinstance(password, str) or not password:
            raise ValueError("Password cannot be empty.")
        if len(username) > 80:
            raise ValueError("Username must be at most 80 characters.")
        with self._lock:
            data = self._read()
            if any(
                isinstance(item, dict) and item.get("username") == username
                for item in data["users"]
            ):
                raise ValueError("That username already exists.")
            data["users"].append(
                {
                    "username": username,
                    "password_hash": generate_password_hash(password),
                    "role": "admin",
                    "enabled": True,
                }
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        return {"username": username, "role": "admin", "enabled": True}


def authenticated_admin():
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


def register_auth(app, payload, store):
    @app.get("/api/auth/session")
    def auth_session():
        if not authenticated_admin():
            return jsonify(authenticated=False, username=None, role=None)
        return jsonify(authenticated=True, username=session["username"], role="admin")

    @app.post("/api/auth/login")
    def auth_login():
        data = payload()
        if set(data) != {"username", "password"}:
            return jsonify(ok=False, error="Invalid username or password."), 401
        user = store.authenticate_admin(data["username"], data["password"])
        if user is None:
            session.clear()
            return jsonify(ok=False, error="Invalid username or password."), 401
        session.clear()
        session["username"] = user["username"]
        session["role"] = user["role"]
        return jsonify(authenticated=True, **user)

    @app.post("/api/auth/logout")
    def auth_logout():
        session.clear()
        return jsonify(ok=True, authenticated=False)
