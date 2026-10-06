"""Per-device balance acquisition through the existing workflow service."""

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from automation.jobs.storage import RecordConflict
from automation.workflows.values import parse_currency


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
        workflow = self.workflows.read(workflow_id)
        if not any(
            step.get("type") == "ocr_value"
            and step.get("parser") == "currency"
            and step.get("output") == "payer_balance"
            for step in workflow["steps"]
        ):
            raise ValueError(
                "Balance workflow must produce payer_balance with Read Balance."
            )
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
            if not completed or completed.get("status") != "passed":
                raise ValueError(
                    "Balance workflow did not complete; balance is unknown."
                )
            return parse_currency(completed.get("outputs", {}).get("payer_balance"))
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

    def valid(self, *, now=None):
        stamp = time.monotonic() if now is None else now
        return (
            not self.invalidated
            and self.available_balance is not None
            and 0 <= stamp - self.observed_monotonic < self.refresh_seconds
        )

    def observe(self, amount, *, now=None):
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
            self.invalidate()
            raise ValueError("Balance must be a finite nonnegative Decimal.")
        self.available_balance = amount
        self.observed_monotonic = time.monotonic() if now is None else now
        self.observed_at = datetime.now(timezone.utc).isoformat()
        self.invalidated = False

    def invalidate(self):
        self.invalidated = True

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
