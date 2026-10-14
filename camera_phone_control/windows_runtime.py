"""Windows packaging paths and first-run seeding for Phone Controller."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIRECTORY_NAME = "Phone Controller"


def resource_root() -> Path:
    """Return the read-only bundled resource root or the source checkout root."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    return (
        Path(frozen_root).resolve() if frozen_root else Path(__file__).resolve().parent
    )


def default_data_root(environment=None) -> Path:
    environment = os.environ if environment is None else environment
    configured = environment.get("PHONE_CONTROLLER_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        local = environment.get("LOCALAPPDATA")
        if not local:
            raise RuntimeError(
                "LOCALAPPDATA is unavailable; cannot store application data."
            )
        return Path(local) / APP_DIRECTORY_NAME
    local = environment.get("XDG_DATA_HOME")
    return (
        Path(local) / "phone-controller"
        if local
        else Path.home() / ".local" / "share" / "phone-controller"
    )


@dataclass(frozen=True)
class RuntimePaths:
    resources: Path
    data: Path
    workflows: Path
    camera: Path
    jobs: Path
    receipts: Path
    logs: Path
    config: Path
    locks: Path
    cache: Path
    ocr_models: Path

    @classmethod
    def create(cls, data_root=None):
        resources = resource_root()
        data = Path(data_root or default_data_root()).resolve()
        result = cls(
            resources=resources,
            data=data,
            workflows=data / "workflows",
            camera=data / "camera",
            jobs=data / "jobs",
            receipts=data / "receipts",
            logs=data / "logs",
            config=data / "config",
            locks=data / "locks",
            cache=data / "cache",
            ocr_models=resources / "paddle_models",
        )
        result.initialize()
        return result

    def initialize(self):
        for directory in (
            self.data,
            self.workflows,
            self.camera,
            self.jobs,
            self.receipts,
            self.logs,
            self.config,
            self.locks,
            self.cache,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self._copy_missing(
            self.resources / "automation" / "workflow_data", self.workflows
        )
        self._copy_missing(
            self.resources / "packaging" / "defaults",
            self.camera,
            names=("cameras.json",),
        )

        packaged_environment = self.resources / "packaging" / "client.env"
        client_environment = self.config / ".env"
        if packaged_environment.is_file() and not client_environment.exists():
            shutil.copy2(packaged_environment, client_environment)

    @staticmethod
    def _copy_missing(source, destination, *, names=None):
        if not source.is_dir():
            return
        candidates = (
            [source / name for name in names]
            if names is not None
            else sorted(source.glob("*.json"))
        )
        for source_path in candidates:
            if not source_path.is_file():
                continue
            destination_path = destination / source_path.name
            if not destination_path.exists():
                shutil.copy2(source_path, destination_path)
