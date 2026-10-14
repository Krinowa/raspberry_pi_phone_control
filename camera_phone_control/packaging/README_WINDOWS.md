# Windows client package

Build on a clean 64-bit Windows machine with Python 3.11 available to the
developer (Python is not required on client machines):

```bat
camera_phone_control\packaging\build_windows.bat
```

The command creates the PyInstaller `onedir` application at:

```text
camera_phone_control\dist\PhoneController\PhoneController.exe
```

The build command installs build dependencies and downloads OCR models only on
the build machine. It embeds PaddleOCR, PaddlePaddle, OpenCV, the English OCR
models, Flask templates/static files, and initial workflows. The installed
client does not run pip/npm and does not download models.

To include optional local Windows tooling, place any of these files in
`camera_phone_control\packaging\vendor` before building:

- `ffmpeg.exe` (stable DirectShow camera enumeration)
- `adb.exe`, `AdbWinApi.dll`, `AdbWinUsbApi.dll` (local Android control)

The normal Raspberry Pi input path does not require those tools.

## Installer

After the onedir build succeeds, compile the installer with Inno Setup 6:

```bat
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" camera_phone_control\packaging\PhoneController.iss
```

Output:

```text
camera_phone_control\installer\PhoneControllerSetup.exe
```

The per-user installer creates Desktop and Start Menu shortcuts. Application
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

For a preconfigured deployment, add `packaging\client.env` before building.
It will be seeded to the writable `config\.env` only on first launch. Do not put
production secrets in this file unless the installer itself is distributed as
a protected secret.
