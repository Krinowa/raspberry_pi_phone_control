# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir definition for the Windows Phone Controller client."""

import os
import subprocess
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files


APP_ROOT = Path(SPECPATH).parent
model_root = Path(
    os.environ.get(
        "PHONE_CONTROLLER_BUILD_MODEL_ROOT",
        Path.home() / ".paddlex" / "official_models",
    )
).resolve()
model_names = (
    "PP-OCRv5_mobile_det",
    "en_PP-OCRv5_mobile_rec",
    "PP-LCNet_x1_0_textline_ori",
)
required_model_files = (
    "config.json",
    "inference.json",
    "inference.pdiparams",
    "inference.yml",
)
workflow_seeds = APP_ROOT / "packaging" / "workflow_seeds"

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
binaries = []
hiddenimports = []

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

# These tools are optional because Raspberry Pi control is the application
# default. If supplied before building, stable Windows camera enumeration and
# local Android tooling remain available without installing anything globally.
vendor = APP_ROOT / "packaging" / "vendor"
for filename in ("ffmpeg.exe", "adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"):
    source = vendor / filename
    if source.is_file():
        binaries.append((str(source), "tools"))

app_icon = Path(
    os.environ.get("PHONE_CONTROLLER_BUILD_ICON", vendor / "PhoneController.ico")
)
version_file = Path(os.environ.get("PHONE_CONTROLLER_BUILD_VERSION_FILE", ""))

a = Analysis(
    [str(APP_ROOT / "windows_launcher.py")],
    pathex=[str(APP_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(APP_ROOT / "packaging" / "pyi_rth_phone_controller.py")],
    excludes=["tkinter", "matplotlib.tests", "numpy.tests"],
    noarchive=False,
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
