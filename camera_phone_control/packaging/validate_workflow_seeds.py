"""Fail closed when curated installer workflow seeds contain development data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


class WorkflowSeedError(RuntimeError):
    pass


def _walk(value, path="$", *, findings):
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key == "recording_screenshot":
                findings.append(f"{child}: recording screenshots are not distributable")
            _walk(item, child, findings=findings)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk(item, f"{path}[{index}]", findings=findings)


def validate_seed_file(path):
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowSeedError(f"{path}: invalid workflow JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise WorkflowSeedError(f"{path}: workflow root must be an object")
    for field in ("id", "name", "purpose", "steps"):
        if not payload.get(field):
            raise WorkflowSeedError(f"{path}: missing required field {field!r}")
    if not isinstance(payload["steps"], list):
        raise WorkflowSeedError(f"{path}: steps must be a list")

    findings = []
    _walk(payload, findings=findings)
    if payload.get("purpose") == "payment":
        for index, variable in enumerate(payload.get("variables") or []):
            if not isinstance(variable, dict):
                continue
            name = str(variable.get("name") or "").lower()
            if name in {"account_number", "amount", "bank_name"} and variable.get(
                "default"
            ) not in (None, ""):
                findings.append(
                    f"$.variables[{index}].default: payment sample/default values "
                    "must be empty in installer seeds"
                )
    if findings:
        raise WorkflowSeedError(f"{path}: " + "; ".join(findings))


def validate_seed_directory(directory):
    directory = Path(directory)
    if not directory.is_dir():
        raise WorkflowSeedError(
            f"Curated workflow seed directory is missing: {directory}"
        )
    paths = sorted(directory.glob("*.json"))
    for path in paths:
        validate_seed_file(path)
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    paths = validate_seed_directory(args.directory)
    print(f"Validated {len(paths)} curated workflow seed(s) in {args.directory}")


if __name__ == "__main__":
    main()
