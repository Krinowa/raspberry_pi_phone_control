"""JSON APIs for account routing and persistent background job queues."""

from __future__ import annotations

from flask import jsonify, request

from automation.workflows.validation import resolve_variables

from .executor import BackgroundJobExecutor
from .service import DeviceEligibility, JobScheduler
from .storage import AccountBindingStore, JobStore, RecordConflict, RecordNotFound


def register_jobs(
    app,
    state,
    payload,
    *,
    account_store=None,
    job_store=None,
    start_executor=False,
):
    account_store = account_store or AccountBindingStore()
    job_store = job_store or JobStore()
    workflow_storage = app.extensions["workflow_storage"]
    workflow_service = app.extensions["workflow_service"]
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

    @app.errorhandler(RecordNotFound)
    def missing_record(exc):
        return jsonify(ok=False, error="Record not found."), 404

    @app.errorhandler(RecordConflict)
    def conflicting_record(exc):
        return jsonify(ok=False, error=str(exc)), 409

    def ensure_registered_device(device_id):
        if state.pi_bridge is None:
            if device_id not in {workflow_service.device_id, "phone_A"}:
                raise RecordConflict("Device is not registered.")
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
            raise RecordConflict("Device is not registered.")

    @app.get("/api/accounts")
    def list_accounts():
        return jsonify(accounts=[item.public() for item in account_store.list()])

    @app.post("/api/accounts")
    def create_account():
        data = payload()
        ensure_registered_device(data.get("device_id"))
        return jsonify(account_store.create(data).public()), 201

    @app.route("/api/accounts/<account_id>", methods=["GET", "PATCH", "DELETE"])
    def account(account_id):
        if request.method == "GET":
            return jsonify(account_store.get(account_id).public())
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
        return jsonify(jobs=[scheduler.public(job) for job in scheduler.list_jobs()])

    @app.post("/api/jobs")
    def create_job():
        data = payload()

        if set(data) != {"account_id", "workflow", "payload"}:
            raise ValueError("Expected account_id, workflow, and payload only.")
        binding = account_store.get(data["account_id"])

        if not binding.enabled:
            raise RecordConflict("Account binding is disabled.")
        workflow = workflow_storage.read(data["workflow"])
        resolve_variables(workflow, data["payload"])
        job = scheduler.enqueue(
            binding,
            workflow["id"],
            data["payload"],
            recovery_workflow_id=workflow.get("recovery_workflow_id"),
        )
        return jsonify(scheduler.public(job)), 202

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id):
        return jsonify(scheduler.public(scheduler.get_job(job_id)))

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id):
        if payload():
            raise ValueError("Cancel Job expects an empty JSON object.")
        return jsonify(scheduler.public(executor.cancel(job_id)))

    @app.post("/api/jobs/<job_id>/recover")
    def recover_job_phone(job_id):
        if payload():
            raise ValueError("Recover Phone expects an empty JSON object.")
        return jsonify(scheduler.public(executor.recover(job_id))), 202

    @app.post("/api/jobs/<job_id>/verify-ready")
    def verify_job_phone_ready(job_id):
        if payload():
            raise ValueError("Ready-state verification expects an empty JSON object.")
        return (
            jsonify(scheduler.public(executor.recover(job_id, verify_only=True))),
            202,
        )

    return scheduler
