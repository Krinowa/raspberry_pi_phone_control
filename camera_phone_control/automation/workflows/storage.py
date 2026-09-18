"""Validated JSON storage with private files, atomic replacement and safe IDs."""

import json
import os
import tempfile
import threading
from pathlib import Path
from .models import now
from .validation import identifier, validate_workflow


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

    def delete(self, workflow_id):
        with self.lock:
            self.path(workflow_id).unlink()

    def list(self):
        with self.lock:
            result = []

            for path in sorted(self.directory.glob("*.json")):
                workflow = self.read(path.stem)
                result.append(
                    {
                        key: workflow.get(key)
                        for key in ("id", "name", "created_at", "updated_at")
                    }
                    | {
                        "step_count": len(workflow["steps"]),
                        "variable_count": len(workflow["variables"]),
                    }
                )

            return result
