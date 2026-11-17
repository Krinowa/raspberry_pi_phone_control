"""JSON APIs for account routing and persistent background job queues."""

from __future__ import annotations

import logging

from flask import g, jsonify, request

from automation.error_codes import error_fields, exception_error_fields
from automation.workflows.validation import resolve_variables
from diagnostic_logging import EVENT_DEDUPLICATOR, log_event

from .executor import BackgroundJobExecutor
from .service import DeviceEligibility, JobScheduler
from .storage import AccountBindingStore, JobStore, RecordConflict, RecordNotFound

LOG = logging.getLogger("jobs_api")

_OPERATIONAL_CONFLICT_EVENTS = {
    "authentication_required": (
        "authentication",
        "authentication_required",
        logging.WARNING,
    ),
    "session_expired": ("authentication", "session_expired", logging.WARNING),
    "device_not_ready": ("device", "device_not_ready", logging.WARNING),
    "balance_workflow_failed": (
        "balance",
        "balance_refresh_failed",
        logging.WARNING,
    ),
    "withdrawal_state_unresolved": (
        "withdrawal",
        "reconciliation_required",
        logging.ERROR,
    ),
    "withdrawal_cancel_uncertain": (
        "withdrawal",
        "cancel_failed",
        logging.ERROR,
    ),
}


def register_jobs(
    app,
    state,
    payload,
    *,
    account_store=None,
    job_store=None,
    start_executor=False,
    admin_required,
    require_admin,
):
    account_store = account_store or AccountBindingStore()
    job_store = job_store or JobStore()
    workflow_storage = app.extensions["workflow_storage"]
    workflow_service = app.extensions["workflow_service"]
    device_workflow_store = app.extensions["device_workflow_assignment_store"]
    camera_manager = app.extensions["camera_manager"]
    readiness = DeviceEligibility(state, camera_manager, workflow_service)
    scheduler = JobScheduler(job_store, readiness)
    executor = BackgroundJobExecutor(
        scheduler,
        workflow_storage,
        workflow_service,
    )
    if start_executor:
        executor.start()

    app.extensions.update(
        account_binding_store=account_store,
        job_store=job_store,
        job_scheduler=scheduler,
        job_executor=executor,
        device_eligibility=readiness,
    )

    def public_job(job):
        result = scheduler.public(job)
        if result.get("error"):
            result.setdefault("error_code", "workflow_failed")
            result.setdefault("error_params", {})
        if result.get("recovery_error"):
            result.setdefault("recovery_error_code", "workflow_recovery_failed")
            result.setdefault("recovery_error_params", {})
        manager = app.extensions.get("withdraw_manager")
        if manager is not None:
            result.update(manager.job_withdrawal_state(job))
        return result

    @app.errorhandler(RecordNotFound)
    def missing_record(exc):
        return (
            jsonify(**error_fields("Record not found.", "record_not_found", ok=False)),
            404,
        )

    @app.errorhandler(RecordConflict)
    def conflicting_record(exc):
        error_code = getattr(exc, "error_code", None)
        diagnostic = _OPERATIONAL_CONFLICT_EVENTS.get(error_code)
        if diagnostic is not None:
            component, event, level = diagnostic
            params = getattr(exc, "error_params", {}) or {}
            device_id = params.get("device_id") or (request.view_args or {}).get(
                "device_id"
            )
            route = (
                request.url_rule.rule if request.url_rule is not None else request.path
            )
            identity = (device_id, route, error_code)
            if EVENT_DEDUPLICATOR.should_log(component, event, identity):
                log_event(
                    LOG,
                    level,
                    component,
                    event,
                    request_id=getattr(g, "request_id", None),
                    device_id=device_id,
                    route=route,
                    error_code=error_code,
                    exception_type=type(exc).__name__,
                )
        return jsonify(**exception_error_fields(exc, ok=False)), 409

    def ensure_registered_device(device_id):
        if state.pi_bridge is None:
            if device_id not in {workflow_service.device_id, "phone_A"}:
                raise RecordConflict(
                    "Device is not registered.",
                    "device_unavailable",
                    {"device_id": device_id},
                )
            return
        with state.lock:
            registered = device_id in state.pi_devices
            initialized = state.pi_catalog_initialized
        if not initialized:
            catalog = state.pi_bridge.devices()
            with state.lock:
                state.update_pi_devices(catalog)
                state.pi_catalog_initialized = True
                registered = device_id in state.pi_devices
        if not registered:
            raise RecordConflict(
                "Device is not registered.",
                "device_unavailable",
                {"device_id": device_id},
            )

    app.extensions["ensure_registered_device"] = ensure_registered_device

    @app.get("/api/accounts")
    def list_accounts():
        return jsonify(accounts=[item.public() for item in account_store.list()])

    @app.post("/api/accounts")
    @admin_required
    def create_account():
        data = payload()
        ensure_registered_device(data.get("device_id"))
        return jsonify(account_store.create(data).public()), 201

    @app.route("/api/accounts/<account_id>", methods=["GET", "PATCH", "DELETE"])
    def account(account_id):
        if request.method == "GET":
            return jsonify(account_store.get(account_id).public())
        denied = require_admin()
        if denied is not None:
            return denied
        if request.method == "DELETE":
            data = payload()
            if data:
                raise ValueError("Delete Account expects an empty JSON object.")
            deleted = account_store.delete(account_id)
            return jsonify(deleted=True, account_id=deleted.account_id)
        changes = payload()
        if "device_id" in changes:
            ensure_registered_device(changes["device_id"])
        return jsonify(account_store.update(account_id, changes).public())

    @app.get("/api/accounts/<account_id>/device")
    def resolve_account_device(account_id):
        binding = account_store.get(account_id)
        ready = scheduler.is_device_ready(binding.device_id)
        return jsonify(
            account_id=binding.account_id,
            device_id=binding.device_id,
            enabled=binding.enabled,
            device_connected=ready["connected"],
            camera_available=ready["camera_available"],
            device_ready=ready["ready"],
            readiness=ready,
        )

    @app.get("/api/jobs")
    def list_jobs():
        return jsonify(jobs=[public_job(job) for job in scheduler.list_jobs()])

    @app.post("/api/jobs")
    def create_job():
        data = payload()

        if set(data) != {"account_id", "workflow", "payload"}:
            raise ValueError("Expected account_id, workflow, and payload only.")
        binding = account_store.get(data["account_id"])

        if not binding.enabled:
            raise RecordConflict("Account binding is disabled.")
        workflow = workflow_storage.read(data["workflow"])
        if workflow.get("purpose") != "payment":
            raise RecordConflict(
                "Only a Payment purpose workflow can be queued as device payment work."
            )
        if not device_workflow_store.allows(binding.device_id, workflow["id"]):
            raise RecordConflict(
                f"Workflow is not assigned to device {binding.device_id}."
            )
        resolve_variables(workflow, data["payload"])
        job = scheduler.enqueue(
            binding,
            workflow["id"],
            data["payload"],
            recovery_workflow_id=workflow.get("recovery_workflow_id"),
        )
        return jsonify(public_job(job)), 202

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id):
        return jsonify(public_job(scheduler.get_job(job_id)))

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id):
        if payload():
            raise ValueError("Cancel Job expects an empty JSON object.")
        return jsonify(public_job(executor.cancel(job_id)))

    @app.post("/api/jobs/<job_id>/recover")
    def recover_job_phone(job_id):
        if payload():
            raise ValueError("Recover Phone expects an empty JSON object.")
        return jsonify(public_job(executor.recover(job_id))), 202

    @app.post("/api/jobs/<job_id>/verify-ready")
    def verify_job_phone_ready(job_id):
        if payload():
            raise ValueError("Ready-state verification expects an empty JSON object.")
        return (
            jsonify(public_job(executor.recover(job_id, verify_only=True))),
            202,
        )

    @app.post("/api/jobs/<job_id>/mark-ready")
    def mark_job_phone_ready(job_id):
        if payload():
            raise ValueError("Mark Ready expects an empty JSON object.")

        return jsonify(public_job(scheduler.mark_ready(job_id)))

    @app.post("/api/jobs/<job_id>/retry")
    def retry_job(job_id):
        data = payload()
        if set(data) != {"mode"}:
            raise ValueError("Retry expects mode only.")
        original = scheduler.get_job(job_id)
        if original.action == "withdraw_v2":
            manager = app.extensions.get("withdraw_manager")
            if manager is None:
                raise RecordConflict(
                    "Withdraw Bot retry is unavailable because its session is not running."
                )
            retry = manager.retry_job(job_id, data["mode"])
        else:
            retry = scheduler.create_retry(job_id, data["mode"])
        executor.wake()
        return jsonify(public_job(retry)), 202

    return scheduler
