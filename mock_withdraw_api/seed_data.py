"""Deterministic users and withdrawals restored by POST /mock/reset."""

from datetime import datetime, timedelta, timezone


def iso(offset_minutes=0):
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)).isoformat()


def users():
    return {
        "operator_a": {"id": 101, "payment_records": ["5"]},
        "operator_b": {"id": 102, "payment_records": ["5", "2b"]},
        "operator_c": {"id": 103, "payment_records": ["2b"]},
        "operator_d": {"id": 104, "payment_records": ["9"]},
    }


def _withdrawal(
    ident,
    order_id,
    tx_id,
    payment_record,
    amount,
    account_number,
    account_name,
    bank_name,
    mobile_no,
    *,
    priority=0,
    created_offset=0,
):
    return {
        "id": ident,
        "order_id": order_id,
        "tx_id": tx_id,
        "payment_record": payment_record,
        "type": "withdraw",
        "withdraw_amount": amount,
        "final_amount": amount,
        "priority": priority,
        "account_number": account_number,
        "account_name": account_name,
        "holder_name": account_name,
        "bank_name": bank_name,
        "mobile_no": mobile_no,
        "created_at": iso(created_offset),
        "status": "PENDING",
        "locked_by": None,
        "lock_expires_at": None,
        "cancel_reason": None,
        "confirmed_by_operator": None,
        "completed_at": None,
        "proof_files": [],
    }


def withdrawals():
    values = [
        _withdrawal(
            935402,
            "WT20261005A1",
            "TX9F3K2",
            "5",
            "50.00",
            "1234567890",
            "Test User One",
            "Mock Bank A",
            "0123456789",
            created_offset=-4,
        ),
        _withdrawal(
            935403,
            "WT20261005A2",
            "TX9F3K3",
            "2b",
            "120.00",
            "2234567890",
            "Test User Two",
            "Mock Bank B",
            "0133456789",
            created_offset=-3,
        ),
        _withdrawal(
            935404,
            "WT20261005A3",
            "TX9F3K4",
            "5",
            "9999.00",
            "3234567890",
            "High Amount User",
            "Mock Bank A",
            "0143456789",
            created_offset=-2,
        ),
        _withdrawal(
            935405,
            "WT20261005A4",
            "TX9F3K5",
            "5",
            "25.00",
            "4234567890",
            "Priority User",
            "Mock Bank C",
            "0153456789",
            priority=1,
            created_offset=-10,
        ),
        _withdrawal(
            935406,
            "WT20261005A5",
            "TX9F3K6",
            "9",
            "80.00",
            "5234567890",
            "Isolated Queue User",
            "Mock Bank D",
            "0163456789",
            created_offset=-1,
        ),
    ]
    confirmed = _withdrawal(
        935450,
        "WT20261005H1",
        "TX-CONFIRMED-001",
        "5",
        "30.00",
        "6234567890",
        "Confirmed User",
        "Mock Bank A",
        "0173456789",
        created_offset=-60,
    )
    confirmed.update(
        status="PROOF_PENDING",
        confirmed_by_operator=101,
        completed_at=iso(-30),
        proof_rejected=False,
    )
    cancelled = _withdrawal(
        935451,
        "WT20261005H2",
        "TX-CANCELLED-001",
        "5",
        "45.00",
        "7234567890",
        "Cancelled User",
        "Mock Bank A",
        "0183456789",
        created_offset=-50,
    )
    cancelled.update(status="CANCELLED", cancel_reason="invalid account number")
    return {item["id"]: item for item in [*values, confirmed, cancelled]}
