"""Private durable reconciliation records, using the existing atomic JSON store."""

from copy import deepcopy
from automation.jobs.storage import _JsonStore
from automation.workflows.models import now

TERMINAL = {"CONFIRMED", "RELEASED", "CANCELLED"}


class WithdrawJournal(_JsonStore):
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
                device["history"].append(
                    {
                        key: current.get(key)
                        for key in (
                            "withdraw_id",
                            "tx_id",
                            "phase",
                            "job_id",
                            "updated_at",
                        )
                    }
                )
            device["current"] = {**deepcopy(fields), "updated_at": now()}
            self._write(data)

    def was_confirmed(self, device_id, tx_id):
        with self.lock:
            device = self._read().get(device_id, {})
            return any(
                item.get("tx_id") == tx_id and item.get("phase") == "CONFIRMED"
                for item in [device.get("current", {}), *device.get("history", [])]
            )
