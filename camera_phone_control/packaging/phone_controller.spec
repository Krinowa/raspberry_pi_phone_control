# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir definition for the Windows Phone Controller client."""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


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

datas = [
    (str(APP_ROOT / "static"), "static"),
    (str(APP_ROOT / "templates"), "templates"),
    (str(APP_ROOT / "automation" / "workflow_data"), "automation/workflow_data"),
    (str(APP_ROOT / "packaging" / "defaults"), "packaging/defaults"),
]
binaries = []
hiddenimports = []

for model_name in model_names:
    model = model_root / model_name
    if not all(
        (model / filename).is_file()
        for filename in ("inference.json", "inference.pdiparams")
    ):
        raise SystemExit(f"Required offline OCR model is missing: {model}")
    datas.append((str(model), f"paddle_models/{model_name}"))

for package in ("paddle", "paddleocr", "paddlex", "cv2"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

client_environment = APP_ROOT / "packaging" / "client.env"
if client_environment.is_file():
    datas.append((str(client_environment), "packaging"))

# These tools are optional because Raspberry Pi control is the application
# default. If supplied before building, stable Windows camera enumeration and
# local Android tooling remain available without installing anything globally.
vendor = APP_ROOT / "packaging" / "vendor"
for filename in ("ffmpeg.exe", "adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"):
    source = vendor / filename
    if source.is_file():
        binaries.append((str(source), "tools"))

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
