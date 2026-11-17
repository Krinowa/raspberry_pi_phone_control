"""Compile proprietary Phone Controller modules into an isolated Cython stage."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.machinery
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STAGE = APP_ROOT / "build" / "protected"
DEFAULT_TEMP = APP_ROOT / "build" / "protected-temp"

# The executable entry point must remain a Python script for PyInstaller. Package
# initializers remain as source because CPython/PyInstaller package discovery is
# substantially more reliable with normal __init__ modules. Everything carrying
# application behavior is compiled.
BOOTSTRAP_PATHS = (
    "windows_launcher.py",
    "automation/__init__.py",
    "automation/input/__init__.py",
    "automation/integrations/__init__.py",
    "automation/integrations/withdraw_v2/__init__.py",
    "automation/jobs/__init__.py",
    "automation/screen/__init__.py",
    "automation/scripts/__init__.py",
    "automation/workflows/__init__.py",
    "hardware/__init__.py",
)

PROPRIETARY_PACKAGES = ("automation", "hardware")


class ProtectedBuildError(RuntimeError):
    pass


def module_name(path: Path) -> str:
    relative = path.relative_to(APP_ROOT)
    if relative.name == "__init__.py":
        return ".".join(relative.parent.parts)
    return ".".join(relative.with_suffix("").parts)


def discover_sources():
    # Root runtime modules are discovered rather than enumerated so a newly
    # added proprietary module cannot silently bypass the protected stage.
    sources = sorted(APP_ROOT.glob("*.py"))
    for package in PROPRIETARY_PACKAGES:
        sources.extend(sorted((APP_ROOT / package).rglob("*.py")))
    sources = sorted(set(sources))
    missing = [path for path in sources if not path.is_file()]
    if missing:
        raise ProtectedBuildError(
            "Required proprietary source is missing: "
            + ", ".join(str(path) for path in missing)
        )
    return sources


def _resolve_import(module: str, node: ast.ImportFrom, *, is_package: bool):
    if not node.level:
        return node.module or ""
    package = module.split(".") if is_package else module.split(".")[:-1]
    if node.level > len(package) + 1:
        return ""
    base = package[: len(package) - node.level + 1]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def imported_modules(path: Path, module: str):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_import(
                module, node, is_package=path.name == "__init__.py"
            )
            if resolved:
                imports.add(resolved)
            if node.module is None and resolved:
                imports.update(f"{resolved}.{alias.name}" for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            imports.add(node.args[0].value)
    return imports


def reachable_sources(sources):
    by_module = {module_name(path): path for path in sources}
    pending = ["windows_launcher"]
    reached = set()
    while pending:
        module = pending.pop()
        if module in reached or module not in by_module:
            continue
        reached.add(module)
        for imported in imported_modules(by_module[module], module):
            parts = imported.split(".")
            candidates = [".".join(parts[:index]) for index in range(1, len(parts) + 1)]
            pending.extend(
                candidate for candidate in candidates if candidate in by_module
            )
    return {by_module[module] for module in reached}


def build_plan():
    bootstrap_sources = [APP_ROOT / path for path in BOOTSTRAP_PATHS]
    all_sources = set(discover_sources())
    source_set = reachable_sources(all_sources)
    bootstrap_set = set(bootstrap_sources)
    if not bootstrap_set <= source_set:
        missing = bootstrap_set - source_set
        raise ProtectedBuildError(
            "Bootstrap allowlist contains unreachable or unknown source files: "
            + ", ".join(str(path) for path in sorted(missing))
        )
    compiled_sources = sorted(source_set - bootstrap_set)
    compiled_modules = [module_name(path) for path in compiled_sources]
    bootstrap_modules = [module_name(path) for path in bootstrap_sources]
    hiddenimports = set(bootstrap_modules)
    for path in source_set:
        hiddenimports.update(imported_modules(path, module_name(path)))
    hiddenimports.discard("__future__")
    # Native modules are added as explicit binaries. Asking ModuleGraph to find
    # them as hidden imports can make it resolve the original source tree and
    # place a second bytecode copy in PYZ.
    hiddenimports.difference_update(compiled_modules)
    return {
        "format": 1,
        "app_root": str(APP_ROOT),
        "compiled_modules": compiled_modules,
        "compiled_sources": [
            str(path.relative_to(APP_ROOT)) for path in compiled_sources
        ],
        "bootstrap_modules": bootstrap_modules,
        "bootstrap_sources": list(BOOTSTRAP_PATHS),
        "excluded_modules": sorted(
            module_name(path) for path in all_sources - source_set
        ),
        "hiddenimports": sorted(hiddenimports),
    }


def _safe_clean(path: Path):
    path = path.resolve()
    build_root = (APP_ROOT / "build").resolve()
    if path == build_root or build_root not in path.parents:
        raise ProtectedBuildError(f"Refusing to clean unsafe staging path: {path}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def compile_plan(plan, stage: Path, temp: Path):
    try:
        from Cython.Build import cythonize
        from setuptools import Extension, setup
    except ImportError as exc:
        raise ProtectedBuildError(
            "Cython and setuptools are required for the protected Windows build."
        ) from exc

    _safe_clean(stage)
    _safe_clean(temp)
    for relative in plan["bootstrap_sources"]:
        source = APP_ROOT / relative
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    extensions = [
        Extension(module, [relative])
        for module, relative in zip(
            plan["compiled_modules"], plan["compiled_sources"], strict=True
        )
    ]
    generated = temp / "generated"
    previous_directory = Path.cwd()
    os.chdir(APP_ROOT)
    try:
        extension_modules = cythonize(
            extensions,
            build_dir=str(generated),
            compiler_directives={
                "language_level": 3,
                "binding": True,
                "embedsignature": False,
            },
            annotate=False,
            force=True,
            nthreads=max(1, min(4, os.cpu_count() or 1)),
        )
        setup(
            name="phone-controller-protected-modules",
            ext_modules=extension_modules,
            script_args=[
                "build_ext",
                "--build-lib",
                str(stage),
                "--build-temp",
                str(temp / "objects"),
            ],
        )
    finally:
        os.chdir(previous_directory)

    suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    extensions_by_module = {}
    missing = []
    for module in plan["compiled_modules"]:
        parent = stage.joinpath(*module.split(".")[:-1])
        stem = module.rsplit(".", 1)[-1]
        candidates = [
            path for path in parent.glob(stem + ".*") if path.name.endswith(suffixes)
        ]
        if len(candidates) != 1:
            missing.append(module)
        else:
            extensions_by_module[module] = str(candidates[0].relative_to(stage))
    if missing:
        raise ProtectedBuildError(
            "Cython did not produce exactly one native extension for: "
            + ", ".join(missing)
        )
    plan["extensions"] = extensions_by_module
    plan["extension_sha256"] = {
        module: hashlib.sha256((stage / relative).read_bytes()).hexdigest()
        for module, relative in extensions_by_module.items()
    }
    plan["build_id"] = uuid.uuid4().hex
    plan["stage"] = str(stage)
    manifest = stage / "protected-manifest.json"
    manifest.write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
    attestation = {
        "format": plan["format"],
        "build_id": plan["build_id"],
        "compiled_modules": plan["compiled_modules"],
        "bootstrap_modules": plan["bootstrap_modules"],
        "excluded_modules": plan["excluded_modules"],
        "extensions": plan["extensions"],
        "extension_sha256": plan["extension_sha256"],
    }
    (stage / "protected-attestation.json").write_text(
        json.dumps(attestation, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    parser.add_argument("--temp", type=Path, default=DEFAULT_TEMP)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = build_plan()
        if args.plan_only:
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        manifest = compile_plan(plan, args.stage.resolve(), args.temp.resolve())
    except ProtectedBuildError as exc:
        parser.error(str(exc))
    print(f"Protected modules ready: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
