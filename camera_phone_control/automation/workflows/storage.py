"""Validated JSON storage with private files, atomic replacement and safe IDs."""

import json
import os
import tempfile
import threading
from pathlib import Path
from .models import now
from .validation import identifier, validate_recovery_workflow, validate_workflow


class WorkflowStorage:
    def __init__(self, directory=None):
        self.directory = Path(
            directory or Path(__file__).resolve().parents[1] / "workflow_data"
        )
        self.lock = threading.RLock()

    def path(self, workflow_id):
        return self.directory / (identifier(workflow_id) + ".json")

    def read(self, workflow_id):
        with self.lock:
            path = self.path(workflow_id)

            if path.is_symlink():
                raise ValueError("Workflow symlinks are not supported.")

            with path.open() as stream:
                data = json.load(stream)

            if not isinstance(data, dict) or data.get("id") != workflow_id:
                raise ValueError("Workflow ID does not match filename.")
            return validate_workflow(data)

    def save(self, workflow, *, create=False):
        data = validate_workflow(workflow)

        with self.lock:
            recovery_workflow_id = data.get("recovery_workflow_id")
            if recovery_workflow_id is not None:
                try:
                    recovery_workflow = self.read(recovery_workflow_id)
                except FileNotFoundError:
                    raise ValueError("Recovery workflow does not exist.") from None
                validate_recovery_workflow(recovery_workflow)
            if self._recovery_referrers(data["id"]):
                validate_recovery_workflow(data)
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.path(data["id"])

            if path.is_symlink():
                raise ValueError("Workflow symlinks are not supported.")

            if create and path.exists():
                raise ValueError("Workflow already exists.")

            existing = self.read(data["id"]) if path.exists() else None
            data["created_at"] = (
                existing["created_at"]
                if existing and "created_at" in existing
                else now()
            )
            data["updated_at"] = now()
            fd, temporary = tempfile.mkstemp(
                dir=self.directory, prefix=".workflow-", suffix=".tmp"
            )

            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(data, stream, indent=2, allow_nan=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                if create:
                    os.link(
                        temporary, path
                    )  # Atomic create; do not overwrite another process.
                else:
                    os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return data

    def migrate_legacy_purposes(self, balance_workflow_ids, balance_validator):
        """Persist deterministic purpose metadata on pre-purpose documents.

        Legacy workflows default to payment. A workflow already assigned as a
        balance route is promoted only when the existing safety validator says
        its structured steps are safe; names are deliberately ignored.
        """
        balance_workflow_ids = set(balance_workflow_ids)
        migrated = []
        with self.lock:
            for path in sorted(self.directory.glob("*.json")):
                if path.is_symlink():
                    continue
                with path.open(encoding="utf-8") as stream:
                    raw = json.load(stream)
                if not isinstance(raw, dict) or "purpose" in raw:
                    continue
                purpose = "payment"
                if raw.get("id") in balance_workflow_ids:
                    candidate = {**raw, "purpose": "balance_check"}
                    try:
                        balance_validator(candidate)
                    except ValueError:
                        pass
                    else:
                        purpose = "balance_check"
                candidate = {**raw, "purpose": purpose}
                validate_workflow(candidate)
                fd, temporary = tempfile.mkstemp(
                    dir=self.directory, prefix=".workflow-purpose-", suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w") as stream:
                        json.dump(candidate, stream, indent=2, allow_nan=False)
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                migrated.append((candidate["id"], purpose))
        return migrated

    def delete(self, workflow_id):
        with self.lock:
            if self._recovery_referrers(workflow_id):
                raise ValueError(
                    "Workflow is assigned as a recovery workflow and cannot be deleted."
                )
            self.path(workflow_id).unlink()

    def _recovery_referrers(self, workflow_id):
        if not self.directory.exists():
            return []
        result = []
        for path in self.directory.glob("*.json"):
            if path.stem == workflow_id or path.is_symlink():
                continue
            candidate = self.read(path.stem)
            if candidate.get("recovery_workflow_id") == workflow_id:
                result.append(candidate["id"])
        return result

    def list(self):
        with self.lock:
            result = []

            for path in sorted(self.directory.glob("*.json")):
                workflow = self.read(path.stem)
                result.append(
                    {
                        key: workflow.get(key)
                        for key in (
                            "id",
                            "name",
                            "purpose",
                            "created_at",
                            "updated_at",
                            "recovery_workflow_id",
                        )
                    }
                    | {
                        "step_count": len(workflow["steps"]),
                        "variable_count": len(workflow["variables"]),
                    }
                )

            return result
