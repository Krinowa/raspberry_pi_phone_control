"""Private durable reconciliation records, using the existing atomic JSON store."""

from copy import deepcopy
from automation.jobs.storage import RecordConflict, _JsonStore
from automation.workflows.models import now

TERMINAL = {"CONFIRMED", "RELEASED", "CANCELLED", "LOCK_REJECTED"}


class WithdrawJournal(_JsonStore):
    @staticmethod
    def _entries(data):
        for device_id, device in data.items():
            current = device.get("current", {})
            if current:
                yield device_id, current, True
            for item in device.get("history", []):
                yield device_id, item, False

    @classmethod
    def _identity_conflict(cls, data, withdraw_id, tx_id):
        for device_id, item, current in cls._entries(data):
            matches = (
                withdraw_id is not None and item.get("withdraw_id") == withdraw_id
            ) or (tx_id is not None and item.get("tx_id") == tx_id)
            if item.get("phase") == "CONFIRMED" and matches:
                return "confirmed", device_id
            if current and item.get("phase") not in TERMINAL and matches:
                return "unresolved", device_id
            if (
                item.get("phase") == "CANCELLED"
                and item.get("assigned_device_id")
                and matches
            ):
                return "previously_locked", device_id
        return None

    @staticmethod
    def _archive(device):
        current = device.get("current", {})
        if current:
            device.setdefault("history", []).append(
                {
                    key: current.get(key)
                    for key in (
                        "withdraw_id",
                        "tx_id",
                        "phase",
                        "job_id",
                        "assigned_device_id",
                        "operator_id",
                        "final_amount",
                        "updated_at",
                    )
                }
            )

    def current(self, device_id):
        with self.lock:
            return deepcopy(self._read().get(device_id, {}).get("current", {}))

    def update(self, device_id, **changes):
        with self.lock:
            data = self._read()
            device = data.setdefault(device_id, {"current": {}, "history": []})
            device["current"].update(deepcopy(changes), updated_at=now())
            self._write(data)
            return deepcopy(device["current"])

    def recover_missing(self, device_id, **fields):
        """Create one startup recovery record without treating update as create."""
        with self.lock:
            data = self._read()
            device = data.setdefault(device_id, {"current": {}, "history": []})
            if device.get("current"):
                raise RecordConflict(
                    "A journal record already exists for this device; recovery did not overwrite it."
                )

            conflict = self._identity_conflict(
                data, fields.get("withdraw_id"), fields.get("tx_id")
            )
            if conflict:
                kind, conflict_device = conflict
                raise RecordConflict(
                    f"Startup recovery identity conflicts with {kind} journal state on {conflict_device}."
                )

            device["current"] = {**deepcopy(fields), "updated_at": now()}
            self._write(data)
            return deepcopy(device["current"])

    def begin(self, device_id, **fields):
        with self.lock:
            data = self._read()
            device = data.setdefault(device_id, {"current": {}, "history": []})
            current = device["current"]
            if current and current.get("phase") not in TERMINAL:
                raise ValueError(
                    "Unresolved withdrawal must be reconciled before new work."
                )
            if current:
                self._archive(device)
            device["current"] = {**deepcopy(fields), "updated_at": now()}
            self._write(data)

    def identity_conflict(self, withdraw_id, tx_id):
        """Return a dashboard-wide duplicate identity conflict, if any."""
        with self.lock:
            return self._identity_conflict(self._read(), withdraw_id, tx_id)

    def claim(self, device_id, *, withdraw_id, tx_id, operator_id, final_amount):
        """Atomically persist a dashboard-wide LOCK_INTENT assigned to a phone."""
        with self.lock:
            data = self._read()
            conflict = self._identity_conflict(data, withdraw_id, tx_id)

            if conflict:
                return False, conflict

            device = data.setdefault(device_id, {"current": {}, "history": []})
            current = device.get("current", {})

            if current and current.get("phase") not in TERMINAL:
                return False, ("device_unresolved", device_id)

            self._archive(device)
            device["current"] = {
                "phase": "LOCK_INTENT",
                "withdraw_id": withdraw_id,
                "tx_id": tx_id,
                "assigned_device_id": device_id,
                "operator_id": operator_id,
                "final_amount": final_amount,
                "updated_at": now(),
            }
            self._write(data)
            return True, None

    def was_confirmed_anywhere(self, tx_id):
        with self.lock:
            return any(
                item.get("tx_id") == tx_id and item.get("phase") == "CONFIRMED"
                for _, item, _ in self._entries(self._read())
            )

    def was_confirmed(self, device_id, tx_id):
        with self.lock:
            device = self._read().get(device_id, {})
            return any(
                item.get("tx_id") == tx_id and item.get("phase") == "CONFIRMED"
                for item in [device.get("current", {}), *device.get("history", [])]
            )
