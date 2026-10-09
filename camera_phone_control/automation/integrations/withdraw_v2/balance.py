"""Per-device balance acquisition through the existing workflow service."""

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from automation.jobs.storage import RecordConflict
from automation.workflows.values import parse_currency


def _workflow_steps(steps):
    for step in steps:
        yield step
        if step.get("type") == "if_text":
            for branch in ("then_steps", "else_steps"):
                yield from _workflow_steps(step.get(branch, []))


def balance_check_workflow(workflow):
    """Validate purpose and safety invariants for a dedicated balance route."""
    if workflow.get("purpose") != "balance_check":
        raise ValueError(
            "Only a Balance Check purpose workflow can be assigned as a "
            "Balance Check Workflow."
        )
    steps = list(_workflow_steps(workflow.get("steps", [])))
    if any(step.get("critical") is True for step in steps):
        raise ValueError(
            "This workflow cannot be used as a Balance Check Workflow because "
            "it contains an irreversible payment step."
        )
    if not any(
        step.get("type") == "ocr_value"
        and step.get("parser") == "currency"
        and step.get("output") == "payer_balance"
        for step in steps
    ):
        raise ValueError(
            "This workflow cannot be used as a Balance Check Workflow because "
            "it does not contain Read Balance."
        )
    return workflow


class BalanceProvider(Protocol):
    def get_available_balance(self, device_id: str) -> Decimal | None: ...


class CameraOCRBalanceProvider:
    """Reserve scheduler eligibility while the ordinary runner owns the phone.

    ``workflow_for_device`` is a local configuration lookup, never API payload.
    This provider does not inspect camera frames or implement navigation.
    """

    def __init__(self, scheduler, workflows, service, workflow_for_device):
        self.scheduler = scheduler
        self.workflows = workflows
        self.service = service
        self.workflow_for_device = workflow_for_device

    def get_available_balance(self, device_id):
        workflow_id = self.workflow_for_device(device_id)
        if not workflow_id:
            raise ValueError("No balance-check workflow is assigned to this device.")
        workflow = balance_check_workflow(self.workflows.read(workflow_id))
        reservation = object()
        with self.scheduler.lock:
            if not self.scheduler.is_device_ready(device_id)["ready"]:
                raise RecordConflict("Device is not ready for a balance check.")
            if self.scheduler.next_job_for_device(device_id) is not None:
                raise RecordConflict(
                    "Queued device work takes priority over a balance check."
                )
            self.scheduler.reservations[device_id] = reservation
        try:
            started = self.service.start(workflow, {}, device_id=device_id)
            completed = self.service.wait(started["run_id"])
            if not completed:
                raise ValueError(
                    "Balance workflow did not complete; balance is unknown."
                )
            # A Read Balance step can publish a valid value before a later
            # cleanup/navigation step fails or is cancelled. Keep that value,
            # matching the workflow-completion listener's semantics.
            try:
                return parse_currency(completed.get("outputs", {}).get("payer_balance"))
            except (AttributeError, ValueError):
                raise ValueError(
                    "Balance workflow did not produce a valid payer balance."
                ) from None
        finally:
            with self.scheduler.lock:
                if self.scheduler.reservations.get(device_id) is reservation:
                    del self.scheduler.reservations[device_id]


@dataclass
class BalanceCache:
    """Ephemeral cache owned by one worker. Restart always creates an empty cache."""

    device_id: str
    refresh_seconds: float = 60.0
    available_balance: Decimal | None = None
    observed_at: str | None = None
    observed_monotonic: float = 0.0
    invalidated: bool = True
    context: tuple | None = None

    def belongs_to(self, context):
        return context is None or self.context == context

    def valid(self, *, now=None, context=None):
        stamp = time.monotonic() if now is None else now
        return (
            self.belongs_to(context)
            and not self.invalidated
            and self.available_balance is not None
            and 0 <= stamp - self.observed_monotonic < self.refresh_seconds
        )

    def observe(self, amount, *, now=None, context=None):
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
            self.invalidate()
            raise ValueError("Balance must be a finite nonnegative Decimal.")
        self.available_balance = amount
        self.observed_monotonic = time.monotonic() if now is None else now
        self.observed_at = datetime.now(timezone.utc).isoformat()
        self.invalidated = False
        self.context = context

    def invalidate(self):
        """Expire trust while retaining the last observation for display."""
        self.invalidated = True

    def clear(self):
        """Discard an observation that belongs to a different payer context."""
        self.available_balance = None
        self.observed_at = None
        self.observed_monotonic = 0.0
        self.invalidated = True
        self.context = None

    def confirmed_payment(self, amount):
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
            self.invalidate()
            raise ValueError("Payment amount must be a positive Decimal.")
        if not self.valid() or self.available_balance < amount:
            self.invalidate()
            return
        self.available_balance -= amount


def final_amount(withdrawal):
    """Only strings/integers from the protocol are accepted; no binary floats."""
    value = withdrawal.get("final_amount")
    if type(value) not in (str, int) or not re.fullmatch(
        r"[0-9]{1,16}(?:\.[0-9]{1,2})?", str(value)
    ):
        raise ValueError("Withdrawal final_amount must be a decimal string.")
    # API money has no OCR labels/separators; enforce cents and positive value.
    amount = Decimal(str(value))
    if amount <= 0:
        raise ValueError("Withdrawal final_amount must be positive.")
    return amount


def select_pending(pending, balance):
    if not isinstance(balance, Decimal) or not balance.is_finite() or balance < 0:
        raise ValueError("A valid own-device balance is required before selection.")
    for withdrawal in pending:
        if final_amount(withdrawal) <= balance:
            return withdrawal
    return None
