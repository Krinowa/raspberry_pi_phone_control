"""Standalone localhost HTTP API and realtime launcher."""

from __future__ import annotations

import os
import time
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request
from werkzeug.utils import secure_filename

from config import API_PREFIX, HOST, HTTP_PORT, PROOF_ROOT
from realtime import RealtimeServer
from scenarios import SCENARIOS
from state import MockError, MockState


def envelope(data=None, message="OK", status=1):
    return {
        "status": status,
        "message": message,
        "data": data if data is not None else {},
    }


def create_app(state=None, realtime=None):
    state = state or MockState()
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=12 * 1024 * 1024)
    app.extensions["mock_state"] = state
    app.extensions["mock_realtime"] = realtime

    @app.errorhandler(MockError)
    def mock_error(error):
        return jsonify(envelope({}, str(error), 0)), error.code

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(envelope({}, "Uploaded proof is too large.", 0)), 413

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise MockError(422, "Expected a JSON object.")
        return value

    def bearer():
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or not header[7:]:
            raise MockError(401, "Bearer token is required.")
        return header[7:]

    def session():
        return state.session(bearer())

    def publish(records):
        hub = app.extensions.get("mock_realtime") or state.publisher
        if hub and records:
            hub.emit_queue_changed(records)

    @app.post(API_PREFIX + "/login")
    def login():
        data = body()
        required = {"username", "password", "g2f_code"}
        if set(data) != required:
            raise MockError(422, "username, password and g2f_code are required.")
        return jsonify(envelope(state.login(**data), "Login successful."))

    @app.get(API_PREFIX + "/me")
    def me():
        return jsonify(envelope(state.profile(session())))

    @app.post(API_PREFIX + "/logout")
    def logout():
        affected = state.logout(bearer())
        publish(affected)
        return jsonify(envelope({}, "Logout successful."))

    @app.post(API_PREFIX + "/broadcasting/auth")
    def broadcasting_auth():
        data = body()
        if set(data) != {"socket_id", "channel_name"}:
            raise MockError(422, "socket_id and channel_name are required.")
        return jsonify(
            state.authorize(bearer(), data["socket_id"], data["channel_name"])
        )

    @app.post(API_PREFIX + "/withdrawals/<int:withdraw_id>/confirm")
    def confirm(withdraw_id):
        current = session()
        scenario = state.scenario
        if scenario == "confirm_timeout_before_apply":
            time.sleep(25)
            raise MockError(504, "Mock confirm timed out before apply.")
        uploads = []
        if request.files.get("proof"):
            uploads.append(request.files["proof"])
        uploads.extend(request.files.getlist("proofs[]"))
        uploads = [upload for upload in uploads if upload and upload.filename]
        if not uploads:
            raise MockError(422, "A proof or proofs[] upload is required.")
        saved = []
        PROOF_ROOT.mkdir(parents=True, exist_ok=True)
        for index, upload in enumerate(uploads, 1):
            name = secure_filename(upload.filename)
            suffix = Path(name).suffix.lower()

            if suffix not in {".jpg", ".jpeg", ".png", ".pdf"}:
                raise MockError(422, "Proof must be JPG, PNG or PDF.")

            target = PROOF_ROOT / f"{withdraw_id}-{int(state.now())}-{index}{suffix}"
            upload.save(target)

            if not target.is_file() or target.stat().st_size == 0:
                raise MockError(422, "Proof file is empty.")
            saved.append(target)

        result, record = state.confirm(current, withdraw_id, saved)
        publish({record})
        if scenario == "confirm_timeout_after_apply":
            time.sleep(25)
        return jsonify(envelope({"withdrawal": result}, "Proof submitted."))

    @app.get(API_PREFIX + "/withdrawals/my-approved")
    def my_approved():
        try:
            page = int(request.args.get("page", 1))
        except ValueError:
            raise MockError(422, "page must be an integer.") from None
        return jsonify(
            envelope(state.approved(session(), request.args.get("tx_id"), page))
        )

    @app.get("/mock/state")
    def mock_state():
        return jsonify(state.snapshot())

    @app.post("/mock/reset")
    def reset():
        hub = app.extensions.get("mock_realtime") or state.publisher
        if hub:
            future = hub.disconnect_all()
            if future:
                future.result(timeout=5)
        state.reset()
        return jsonify({"ok": True, "state": state.snapshot()})

    @app.post("/mock/scenario")
    def scenario():
        data = body()
        if set(data) != {"scenario"}:
            raise MockError(422, "scenario is required.")
        return jsonify({"ok": True, "scenario": state.set_scenario(data["scenario"])})

    @app.post("/mock/withdrawals")
    def add_withdrawal():
        value, record = state.add_withdrawal(body())
        publish({record})
        return jsonify({"ok": True, "withdrawal": value}), 201

    @app.post("/mock/withdrawals/reset")
    def reset_withdrawals():
        data = body()
        if data:
            raise MockError(422, "Reset withdrawals expects an empty object.")
        affected = state.clear_withdrawals()
        publish(affected)
        return jsonify({"ok": True, "state": state.snapshot()})

    @app.post("/mock/emit-queue-changed")
    def emit_queue_changed():
        data = body()
        records = data.get("payment_records", [data.get("payment_record", "5")])
        if not isinstance(records, list) or not records:
            raise MockError(422, "payment_records must be a nonempty list.")

        publish({str(value) for value in records})
        return jsonify({"ok": True, "payment_records": records})

    @app.post("/mock/disconnect")
    def disconnect():
        hub = app.extensions.get("mock_realtime") or state.publisher
        if hub:
            hub.disconnect_all()
        return jsonify({"ok": True})

    @app.post("/mock/expire-session")
    def expire_session():
        data = body()
        token = data.get("token")
        operator_id = data.get("operator_id")
        changed = 0
        with state.lock:
            for value in state.tokens.values():
                if token == value["token"] or operator_id == value["operator_id"]:
                    value["session_max_expires_at"] = state.now() - 1
                    changed += 1

        if not changed:
            raise MockError(404, "Mock session was not found.")
        return jsonify({"ok": True, "expired": changed})

    @app.post("/mock/advance-time")
    def advance_time():
        affected = state.advance_time(body().get("seconds"))
        publish(affected)
        return jsonify({"ok": True, "time_offset": state.time_offset})

    @app.post("/mock/lock-expire")
    def lock_expire():
        with state.lock:
            for item in state.withdrawals.values():
                if item["status"] == "LOCKED":
                    item["lock_expires_at_timestamp"] = state.now() - 1
        affected = state.expire_locks()
        publish(affected)
        return jsonify({"ok": True, "payment_records": sorted(affected)})

    @app.get("/mock")
    def mock_ui():
        return render_template_string(
            _ADMIN_HTML,
            scenarios=sorted(SCENARIOS),
            snapshot=state.snapshot(),
        )

    return app


_ADMIN_HTML = """<!doctype html>
<meta charset="utf-8"><title>Mock Withdraw API v2</title>
<style>body{font:14px system-ui;max-width:1000px;margin:30px auto;padding:0 20px}table{border-collapse:collapse;width:100%;margin:12px 0}td,th{border:1px solid #ccc;padding:7px;text-align:left}button,select,input{padding:7px;margin:3px}pre{background:#f5f5f5;padding:12px;overflow:auto}</style>
<h1>Mock Withdraw API v2</h1>
<p>HTTP 127.0.0.1:9100 · WebSocket 127.0.0.1:9101 · scenario <strong>{{ snapshot.scenario }}</strong></p>
<form method="post" action="/mock/scenario" onsubmit="return send(this)"><select name="scenario">{% for value in scenarios %}<option>{{ value }}</option>{% endfor %}</select><button>Set scenario</button></form>
<button onclick="post('/mock/reset',{})">Reset to Seed Data</button><button onclick="post('/mock/withdrawals/reset',{})">Clear Withdrawals</button><button onclick="post('/mock/emit-queue-changed',{payment_record:'5'})">Emit queue.changed</button><button onclick="post('/mock/disconnect',{})">Disconnect WebSocket</button>
<form onsubmit="return addWithdrawal(this)"><strong>Add pending:</strong> <label>Payment Record <select name="payment_record" required><option>5</option><option>2b</option><option>9</option></select></label> <label>Amount <input name="amount" type="number" value="10.00" min="0.01" step="0.01" required></label> <label>Priority <input name="priority" type="number" value="0" min="0" step="1" required></label> <label>Bank Name <input name="bank_name" placeholder="Mock Bank" required></label> <label>Account Number <input name="account_number" placeholder="0000000000" required></label> <label>Account Name <input name="account_name" placeholder="Mock Test User" required></label><button>Add Pending Withdrawal</button></form>
<h2>Seeded users</h2><table><tr><th>User</th><th>ID</th><th>Payment records</th></tr>{% for name,user in snapshot.operators.items() %}<tr><td>{{ name }}</td><td>{{ user.id }}</td><td>{{ user.payment_records|join(', ') }}</td></tr>{% endfor %}</table>
<h2>Pending withdrawals</h2><table><tr><th>ID</th><th>TX</th><th>Record</th><th>Amount</th><th>Priority</th><th>Bank Name</th><th>Account Number</th><th>Account Name</th></tr>{% for w in snapshot.pending_withdrawals %}<tr><td>{{ w.id }}</td><td>{{ w.tx_id }}</td><td>{{ w.payment_record }}</td><td>RM {{ w.final_amount }}</td><td>{{ w.priority }}</td><td>{{ w.bank_name }}</td><td>{{ w.account_number }}</td><td>{{ w.account_name }}</td></tr>{% endfor %}</table>
<h2>History</h2><pre>{{ snapshot.confirmed_withdrawals + snapshot.cancelled_withdrawals }}</pre>
<script>async function post(url,data){const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(!response.ok){const result=await response.json();alert(result.message||result.error||'Mock request failed');return}location.reload()} function send(form){post(form.action,{scenario:form.scenario.value});return false} function addWithdrawal(form){post('/mock/withdrawals',{payment_record:form.payment_record.value,final_amount:form.amount.value,priority:Number(form.priority.value),bank_name:form.bank_name.value,account_number:form.account_number.value,account_name:form.account_name.value});return false}</script>"""


if __name__ == "__main__":
    mock_state = MockState()
    realtime = RealtimeServer(mock_state)
    realtime.start()
    application = create_app(mock_state, realtime)
    application.run(host=HOST, port=HTTP_PORT, threaded=True, use_reloader=False)
