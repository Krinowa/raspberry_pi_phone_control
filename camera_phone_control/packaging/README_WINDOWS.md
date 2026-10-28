# Windows client package

The supported release target is **Windows 11 x64 with CPython 3.13.13 x64 on the
build machine**. PyInstaller bundles are platform-specific: build and test the
Windows application on Windows, not macOS and not through cross-compilation.

The installed client does not require Python, Node.js, pip, npm, Paddle model
downloads, or a separately installed FFmpeg.

## Release inputs

- `windows_launcher.py` is the executable entry point.
- `packaging/requirements-windows.txt` pins the direct Windows runtime and build
  inputs used for the frozen validation build.
- `packaging/workflow_seeds` is the only source of installer workflow seeds.
  The live `automation/workflow_data` directory is deliberately excluded.
- OCR models are prepared into `build\paddlex`, never taken from a developer's
  `%USERPROFILE%\.paddlex` cache.
- A reviewed Windows x64 `ffmpeg.exe` should be placed at
  `packaging\vendor\ffmpeg.exe` for production builds. Record its origin,
  license, version and SHA-256 digest. The build never downloads it.

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

The build command creates a Python 3.13.13 virtual environment, installs the pinned
build inputs, validates curated workflow seeds, downloads all three OCR models
into the dedicated build cache, validates their model/configuration files, and
then creates a PyInstaller `onedir` application. It embeds PaddleOCR,
PaddlePaddle, PaddleX, OpenCV, NumPy, certifi, Flask templates/static files, and
only the curated workflow seed directory.

To include local Windows tooling, place approved copies of these files in
`packaging\vendor` before building:

- `ffmpeg.exe` (recommended for stable DirectShow camera enumeration)
- `adb.exe`, `AdbWinApi.dll`, `AdbWinUsbApi.dll` (local Android control)

The application resolves bundled FFmpeg through its frozen resource directory
before PATH. If it is absent, source/development PATH lookup and integer camera
sources still work. The normal Raspberry Pi input path does not require these
tools.

Before creating a production installer, inspect `build\PhoneController` and the
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
