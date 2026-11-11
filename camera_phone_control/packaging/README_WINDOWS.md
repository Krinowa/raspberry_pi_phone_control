# Windows client package

The supported release target is **Windows 11 x64 with CPython 3.13.13 x64 on the
build machine**. PyInstaller bundles are platform-specific: build and test the
Windows application on Windows, not macOS and not through cross-compilation.
The protected build also requires Microsoft Visual Studio 2022 Build Tools with
the **Desktop development with C++** workload and a Windows SDK. Cython invokes
that supported MSVC toolchain for CPython 3.13 extension modules.

The installed client does not require Python, Node.js, pip, npm, Paddle model
downloads, or a separately installed FFmpeg.

## Release inputs

- `windows_launcher.py` is the executable entry point.
- `packaging/build_protected_modules.py` builds proprietary native extensions
  into isolated `build\protected` and `build\protected-temp` directories. It
  never edits the development source tree.
- `packaging/requirements-windows.txt` pins the direct Windows runtime and build
  inputs used for the frozen validation build.
- `packaging/workflow_seeds` is the only source of installer workflow seeds.
  The live `automation/workflow_data` directory is deliberately excluded.
- OCR models are prepared into `build\paddlex`, never taken from a developer's
  `%USERPROFILE%\.paddlex` cache.
- A reviewed, self-contained Windows x64 `ffmpeg.exe` must be placed at
  `packaging\vendor\ffmpeg.exe` for production builds. Record its origin,
  license, version and SHA-256 digest. It must expose the DirectShow (`dshow`)
  input device. The build never downloads it and fails if it is absent, is not
  x64, cannot run without companion DLLs, or lacks DirectShow.

Do not add `.env`, credentials, OTPs, bearer tokens, `automation/job_data`,
Withdraw journals, calibration, logs, receipts, or developer Paddle caches to
the spec or workflow seed directory.

The checked-in requirements file pins direct dependencies. Once a clean Windows
acceptance build passes, archive a full environment record alongside the signed
release:

```bat
set APP_VERSION=1.0.0
python -m pip freeze --all > installer\PhoneController-%APP_VERSION%-windows-x64.freeze.txt
python -m pip check
```

That release-specific freeze file must be regenerated only after dependency,
OCR and frozen smoke tests; do not copy a macOS environment into it.

## Clean build

Build on a clean 64-bit Windows machine with Python 3.13.13 x64 available to the
developer (Python is not required on client machines):

```bat
cd C:\path\to\phone_controller\camera_phone_control
set APP_VERSION=1.0.0
packaging\build_windows.bat
```

The command creates the PyInstaller `onedir` application at:

```text
dist\PhoneController\PhoneController.exe
```

The release build needs internet access the first time it populates the
dedicated `build\paddlex` cache (and when installing pinned Python packages).
The resulting frozen application does not need internet access for these OCR
profiles. A release can instead start from a reviewed, pre-populated cache, but
the same model validator still runs before packaging.

The build command creates a Python 3.13.13 virtual environment, installs the pinned
build inputs, validates curated workflow seeds, downloads all five OCR models
into the dedicated build cache, validates their model/configuration files, and
then Cython-compiles proprietary modules before creating a PyInstaller `onedir`
application. It embeds PaddleOCR,
PaddlePaddle, PaddleX, OpenCV, NumPy, certifi, Flask templates/static files, and
only the curated workflow seed directory.

## Protected proprietary modules

The protected stage compiles application behavior under `app.py`, the camera
and logging modules, `automation`, and `hardware` into CPython 3.13 `.pyd`
extensions. Third-party libraries—including Flask, PaddleOCR, PaddleX,
PaddlePaddle, NumPy and OpenCV—remain their normal upstream distributions.

The explicit bootstrap allowlist is limited to `windows_launcher.py` and package
`__init__.py` files. These are retained because PyInstaller requires a Python
entry script and regular package initializers are the safest way to preserve
package discovery, re-exports, multiprocessing spawn imports, and relative
imports. The allowlist is recorded in the generated protected manifest.

`phone_controller.spec` requires that manifest and stage; invoking it without
them fails instead of reverting to ordinary source packaging. It also rejects
any compiled-module name that appears in PyInstaller's pure-Python table. The
post-build validator checks every required `.pyd`, inspects the executable and
nested PYZ archive with PyInstaller's archive viewer, rejects proprietary
source/bytecode or generated C/C++ files, and runs a frozen Flask UI/API smoke
test using an isolated temporary data directory.

The Cython stage improves the effort required to recover Python implementation
details; it is not encryption or DRM. Native extensions can still be
disassembled, runtime strings and constants remain observable, the HTML/CSS and
frontend JavaScript remain readable, and a user can copy the complete installed
application. This build does not add activation, licensing, or anti-debugging.

Supply the required FFmpeg executable and any optional local Android tools in
`packaging\vendor` before building:

- `ffmpeg.exe` (required for stable DirectShow camera enumeration)
- `adb.exe`, `AdbWinApi.dll`, `AdbWinUsbApi.dll` (local Android control)

The build installs FFmpeg at `_internal\tools\ffmpeg.exe`. A frozen application
uses that exact file and never falls back to a global FFmpeg or PATH. Source
development retains PATH lookup. DirectShow alternative monikers remain the
authoritative join to stable Windows PnP camera identities; packaging does not
add an arbitrary numeric-index fallback.

The embedded Paddle models and runtime selection are:

| OCR language        | `CAMERA_OCR_LANGUAGE`           | Recognition model           |
| ------------------- | ------------------------------- | --------------------------- |
| English             | `en` / `english` (default)      | `en_PP-OCRv5_mobile_rec`    |
| Simplified Chinese  | `ch`, `zh-CN`                   | `PP-OCRv5_mobile_rec`       |
| Traditional Chinese | `chinese_cht`, `zh-HK`, `zh-TW` | `PP-OCRv5_mobile_rec`       |
| Japanese            | `japan`, `ja`                   | `PP-OCRv5_mobile_rec`       |
| Malay               | `ms`, `malay`                   | `latin_PP-OCRv5_mobile_rec` |

All profiles reuse `PP-OCRv5_mobile_det` and the optional
`PP-LCNet_x1_0_textline_ori` model. Unsupported languages fail explicitly; they
do not trigger a runtime model download. The package does not include Tesseract
or traineddata, and this build process does not alter the existing optional
Tesseract code path.

FFmpeg redistribution obligations depend on the exact approved build and its
enabled codecs. Release engineering must retain its license/notices and satisfy
the supplier's LGPL/GPL source-offer or corresponding-source requirements as
applicable. Legal review, the recorded download URL, version and SHA-256 are
release prerequisites; do not substitute an unreviewed binary.

After PyInstaller completes, the build validates the protected-module manifest,
PyInstaller archives, frozen UI/API startup, bundled FFmpeg and all five model
directories, and rejects embedded `.env`, private-key containers or
`tesseract.exe` files. Before
creating a production installer, inspect `build\PhoneController` and the
PyInstaller warning file, then test the frozen EXE on a clean Windows machine
with the build user's Paddle caches unavailable. Confirm OCR performs inference
without internet access and spawned OCR workers do not recursively launch the
dashboard.

## Installer

After the onedir build succeeds, compile the installer with Inno Setup 6. Supply
the real release version and optional reviewed installer icon with preprocessor
defines:

```bat
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=1.0.0 /DMyAppIcon="C:\release\PhoneController.ico" packaging\PhoneController.iss
```

Output:

```text
installer\PhoneControllerSetup.exe
```

The validator also checks that `PhoneController.iss` still recursively installs
the complete `dist\PhoneController` directory, including `_internal` and all
compiled `.pyd` extensions. Inspect the completed installer on the Windows
release machine as part of acceptance testing.

The per-user installer creates Desktop, Start Menu and **Stop Phone Controller**
shortcuts. Application
binaries live below `%LOCALAPPDATA%\Programs\Phone Controller`; persistent data
lives separately below `%LOCALAPPDATA%\Phone Controller` and is intentionally
retained on uninstall:

- `workflows` — editable workflow JSON
- `camera` — camera assignment, calibration, and OCR settings
- `jobs` — accounts, jobs, Withdraw journal, and private credential data
- `receipts` — captured receipt images
- `config` — session secret and optional client `.env`
- `logs` — rotating startup/runtime support logs
- `locks` — device ownership lock files
- `cache` — writable library/runtime cache (models remain bundled read-only)

Bundled workflow JSON and default camera configuration are copied only when the
corresponding writable file is missing, so reinstalling/upgrading does not
overwrite client changes.

Uninstall intentionally removes application binaries but retains operational
data. A complete reset is a separate support action: stop Phone Controller,
export any required records, uninstall it, and then explicitly remove
`%LOCALAPPDATA%\Phone Controller`. Never make that deletion an unattended
uninstaller default.

Configuration is created in the writable `config` directory after installation.
The release spec never embeds `client.env` or a source `.env`; credentials and
environment-specific endpoints must be commissioned separately.

Developer tools are disabled by default. To enable them for an installed client,
set `PHONE_DEVELOPER_MODE_KEY` to a non-empty value in
`%LOCALAPPDATA%\Phone Controller\config\.env`.

Restart Phone Controller after provisioning or rotating the key. Never add this
external `.env` to the PyInstaller bundle or installer inputs.

## Signing

Do not store certificates, private keys or signing passwords in this repository.
For production, sign `dist\PhoneController\PhoneController.exe`, compile the
installer, and sign `installer\PhoneControllerSetup.exe` using the organization's
protected Authenticode process (for example, an Inno Setup `SignTool` configured
on the release machine or a post-build `signtool.exe` step). Verify both
signatures and timestamp chains before distribution.

## Clean-machine acceptance

At minimum verify: standard-user install; silent/no-console launch; duplicate
launch; automatic browser open after health readiness; `--shutdown`; offline OCR
with no model download; frozen spawn workers; camera discovery and fallback;
Raspberry Pi LAN access; Withdraw HTTPS/WSS with fresh OTP login; workflow and
receipt paths; upgrade data preservation; uninstall; Defender/SmartScreen; and
paths containing spaces and non-ASCII characters. Paddle DLL collection and
physical Windows camera behavior are not proven by macOS checks.
