# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir definition for the Windows Phone Controller client."""

import os
import json
import subprocess
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files


APP_ROOT = Path(SPECPATH).parent
protected_stage_value = os.environ.get("PHONE_CONTROLLER_PROTECTED_STAGE", "").strip()
protected_manifest_value = os.environ.get(
    "PHONE_CONTROLLER_PROTECTED_MANIFEST", ""
).strip()
if not protected_stage_value or not protected_manifest_value:
    raise SystemExit(
        "Protected build inputs are required. Run packaging\\build_windows.bat; "
        "ordinary source/bytecode packaging is disabled."
    )
protected_stage = Path(protected_stage_value).resolve()
protected_manifest_path = Path(protected_manifest_value).resolve()
if not protected_manifest_path.is_file():
    raise SystemExit(f"Protected build manifest is missing: {protected_manifest_path}")
protected_manifest = json.loads(protected_manifest_path.read_text(encoding="utf-8"))
if Path(protected_manifest.get("stage", "")).resolve() != protected_stage:
    raise SystemExit("Protected build manifest does not match the configured stage.")
model_root = Path(
    os.environ.get(
        "PHONE_CONTROLLER_BUILD_MODEL_ROOT",
        Path.home() / ".paddlex" / "official_models",
    )
).resolve()
model_names = (
    "PP-OCRv5_mobile_det",
    "en_PP-OCRv5_mobile_rec",
    "PP-OCRv5_mobile_rec",
    "latin_PP-OCRv5_mobile_rec",
    "PP-LCNet_x1_0_textline_ori",
)
required_model_files = (
    "config.json",
    "inference.json",
    "inference.pdiparams",
    "inference.yml",
)
workflow_seeds = APP_ROOT / "packaging" / "workflow_seeds"
build_version = os.environ.get("PHONE_CONTROLLER_BUILD_VERSION", "").strip()
version_marker = APP_ROOT / "build" / "phone-controller-version.txt"
if build_version:
    version_marker.parent.mkdir(parents=True, exist_ok=True)
    version_marker.write_text(build_version[:80] + "\n", encoding="utf-8")

subprocess.run(
    [
        sys.executable,
        str(APP_ROOT / "packaging" / "validate_workflow_seeds.py"),
        str(workflow_seeds),
    ],
    check=True,
)

datas = [
    (str(APP_ROOT / "static"), "static"),
    (str(APP_ROOT / "templates"), "templates"),
    (str(workflow_seeds), "automation/workflow_data"),
    (str(APP_ROOT / "packaging" / "defaults"), "packaging/defaults"),
]
if build_version and version_marker.is_file():
    datas.append((str(version_marker), "."))
binaries = []
hiddenimports = list(protected_manifest["hiddenimports"])

for module, relative in protected_manifest["extensions"].items():
    extension = protected_stage / relative
    if not extension.is_file():
        raise SystemExit(f"Required protected extension is missing: {extension}")
    destination = str(Path(relative).parent)
    binaries.append((str(extension), destination if destination != "." else "."))

vendor = APP_ROOT / "packaging" / "vendor"
ffmpeg = vendor / "ffmpeg.exe"
subprocess.run(
    [
        sys.executable,
        str(APP_ROOT / "packaging" / "validate_ffmpeg.py"),
        str(ffmpeg),
    ],
    check=True,
)
binaries.append((str(ffmpeg), "tools"))

# websocket-client is given certifi.where() explicitly for WSS verification.
# Bundle certifi's cacert.pem so that path also exists in the installed onedir app.
datas += collect_data_files("certifi")

for model_name in model_names:
    model = model_root / model_name
    if not all(
        (model / filename).is_file() and (model / filename).stat().st_size > 0
        for filename in required_model_files
    ):
        raise SystemExit(f"Required offline OCR model is missing or incomplete: {model}")
    datas.append((str(model), f"paddle_models/{model_name}"))

for package in ("paddle", "paddleocr", "paddlex", "cv2", "numpy"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

# Local Android tooling remains optional because Raspberry Pi control is the
# application default. FFmpeg is required above for stable DirectShow identity
# enumeration and is never resolved from a global installation in releases.
for filename in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"):
    source = vendor / filename
    if source.is_file():
        binaries.append((str(source), "tools"))

app_icon = Path(
    os.environ.get("PHONE_CONTROLLER_BUILD_ICON", vendor / "PhoneController.ico")
)
version_file = Path(os.environ.get("PHONE_CONTROLLER_BUILD_VERSION_FILE", ""))

a = Analysis(
    [str(protected_stage / "windows_launcher.py")],
    pathex=[str(protected_stage)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(APP_ROOT / "packaging" / "pyi_rth_phone_controller.py")],
    excludes=[
        "tkinter",
        "matplotlib.tests",
        "numpy.tests",
        *protected_manifest.get("excluded_modules", []),
    ],
    noarchive=False,
)

compiled_modules = set(protected_manifest["compiled_modules"])
pure_proprietary = sorted(
    name for name, *_ in a.pure if name in compiled_modules
)
if pure_proprietary:
    raise SystemExit(
        "Protected modules unexpectedly fell back to Python bytecode: "
        + ", ".join(pure_proprietary)
    )
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PhoneController",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    icon=str(app_icon) if app_icon.is_file() else None,
    version=str(version_file) if version_file.is_file() else None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PhoneController",
)
