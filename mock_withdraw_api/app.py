"""Standalone localhost HTTP API and realtime launcher."""

from __future__ import annotations

import os
import logging
import time
from ipaddress import ip_address
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request
from werkzeug.utils import secure_filename

from config import API_PREFIX, HOST, HTTP_PORT, PROOF_ROOT
from realtime import RealtimeServer
from scenarios import SCENARIOS
from state import MockError, MockState

LOG = logging.getLogger("mock_withdraw_api")


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

    @app.before_request
    def local_mock_controls_only():
        if not request.path.startswith("/mock"):
            return None
        try:
            local = ip_address(request.remote_addr or "").is_loopback
        except ValueError:
            local = False
        if not local:
            return jsonify(envelope({}, "Mock controls are local-only.", 0)), 403
        return None

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
        with state.lock:
            before = [
                {"id": item["id"], "status": item["status"]}
                for item in state.withdrawals.values()
            ]
        LOG.warning(
            "mock_state_reset timestamp=%s route_action=POST_/mock/reset "
            "caller=%s reason=restore_seed_data count=%d withdrawals=%s",
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            request.remote_addr or "unknown",
            len(before),
            before,
        )
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
        if set(data) - {"confirm_locked"}:
            raise MockError(422, "Only confirm_locked may be provided.")
        confirm_locked = data.get("confirm_locked", False)
        if type(confirm_locked) is not bool:
            raise MockError(422, "confirm_locked must be a boolean.")
        affected = state.clear_withdrawals(
            action_name="POST /mock/withdrawals/reset",
            caller=request.remote_addr or "unknown",
            reason=(
                "admin_confirmed_locked_clear"
                if confirm_locked
                else "admin_confirmed_clear"
            ),
            confirm_locked=confirm_locked,
        )
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
            future = hub.disconnect_all()
            if future:
                future.result(timeout=5)
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
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or set(data) - {"withdraw_id"}:
            raise MockError(422, "Expected optional withdraw_id only.")
        affected = state.expire_lock(data.get("withdraw_id"))
        publish(affected)
        return jsonify({"ok": True, "payment_records": sorted(affected)})

    @app.post("/mock/withdrawals/<int:withdraw_id>/approve")
    def approve_withdrawal(withdraw_id):
        data = body()
        if set(data) != {"operator_id"}:
            raise MockError(422, "operator_id is required.")
        value, record = state.approve(withdraw_id, data["operator_id"])
        publish({record})
        return jsonify({"ok": True, "withdrawal": value})

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
<form method="post" action="/mock/scenario" onsubmit="return send(this)"><select name="scenario">{% for value in scenarios %}<option>{{ value }}</option>{% endfor %}</select><button type="submit">Set scenario</button></form>
<button type="button" onclick="post('/mock/reset',{})">Reset to Seed Data</button><button type="button" onclick="clearWithdrawals({{ 'true' if snapshot.locked_withdrawals else 'false' }})">Clear Withdrawals</button><button type="button" onclick="post('/mock/emit-queue-changed',{payment_record:'5'})">Emit queue.changed</button><button type="button" onclick="post('/mock/disconnect',{})">Disconnect WebSocket</button>
<form onsubmit="return expireLock(this)"><strong>Expire lock:</strong> <label>Withdrawal ID <input name="withdraw_id" type="number" min="1" required></label><button type="submit">Expire Exact Lock</button></form>
<form onsubmit="return approveWithdrawal(this)"><strong>Simulate approval:</strong> <label>Withdrawal ID <input name="withdraw_id" type="number" min="1" required></label> <label>Operator ID <input name="operator_id" type="number" min="1" required></label><button type="submit">Approve Exact Withdrawal</button></form>
<form onsubmit="return addWithdrawal(this)"><strong>Add pending:</strong> <label>Payment Record <select name="payment_record" required><option>5</option><option>2b</option><option>9</option></select></label> <label>Amount <input name="amount" type="number" value="10.00" min="0.01" step="0.01" required></label> <label>Priority <input name="priority" type="number" value="0" min="0" step="1" required></label> <label>Bank Name <input name="bank_name" placeholder="Mock Bank" required></label> <label>Account Number <input name="account_number" placeholder="0000000000" required></label> <label>Account Name <input name="account_name" placeholder="Mock Test User" required></label><button type="submit">Add Pending Withdrawal</button></form>
<h2>Seeded users</h2><table><tr><th>User</th><th>ID</th><th>Payment records</th></tr>{% for name,user in snapshot.operators.items() %}<tr><td>{{ name }}</td><td>{{ user.id }}</td><td>{{ user.payment_records|join(', ') }}</td></tr>{% endfor %}</table>
<h2>Pending withdrawals</h2><table><tr><th>ID</th><th>TX</th><th>Record</th><th>Amount</th><th>Priority</th><th>Bank Name</th><th>Account Number</th><th>Account Name</th></tr>{% for w in snapshot.pending_withdrawals %}<tr><td>{{ w.id }}</td><td>{{ w.tx_id }}</td><td>{{ w.payment_record }}</td><td>RM {{ w.final_amount }}</td><td>{{ w.priority }}</td><td>{{ w.bank_name }}</td><td>{{ w.account_number }}</td><td>{{ w.account_name }}</td></tr>{% endfor %}</table>
<h2>History</h2><pre>{{ snapshot.confirmed_withdrawals + snapshot.cancelled_withdrawals }}</pre>
<script>async function post(url,data){const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(!response.ok){const result=await response.json();alert(result.message||result.error||'Mock request failed');return}location.reload()} function clearWithdrawals(hasLocked){const message=hasLocked?'This will clear all withdrawals, including active LOCKED withdrawals. This can invalidate cancellation and recovery tests. Continue?':'This will clear all pending and historical mock withdrawals. Continue?';if(!window.confirm(message)){return}post('/mock/withdrawals/reset',{confirm_locked:hasLocked})} function send(form){post(form.action,{scenario:form.scenario.value});return false} function addWithdrawal(form){post('/mock/withdrawals',{payment_record:form.payment_record.value,final_amount:form.amount.value,priority:Number(form.priority.value),bank_name:form.bank_name.value,account_number:form.account_number.value,account_name:form.account_name.value});return false} function expireLock(form){post('/mock/lock-expire',{withdraw_id:Number(form.withdraw_id.value)});return false} function approveWithdrawal(form){post('/mock/withdrawals/'+Number(form.withdraw_id.value)+'/approve',{operator_id:Number(form.operator_id.value)});return false}</script>"""


if __name__ == "__main__":
    mock_state = MockState()
    realtime = RealtimeServer(mock_state)
    realtime.start()
    application = create_app(mock_state, realtime)
    application.run(host=HOST, port=HTTP_PORT, threaded=True, use_reloader=False)
