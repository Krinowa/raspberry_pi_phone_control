# Local camera phone controller

A macOS dashboard for controlling a phone through the existing **Raspberry Pi
Bluetooth HID API**, while viewing it through an **external camera**. Manual
controls now default to the Pi; camera calibration maps clicks to normalized
phone coordinates.

**[Pi manual-control setup and verification](PI_MANUAL_CONTROL.md)** covers the
configuration, startup command, status checks, and each supported control. Pi
manual mode needs no Appium server, ADB, USB debugging, or Android device serial.

Recorder, workflow playback, and automation scripts retain their existing
backends in this incremental integration. For those features, explicitly select
`PHONE_MANUAL_INPUT_BACKEND=appium` and follow the legacy USB instructions below.
Do not run legacy automation and Pi manual control concurrently on the same phone:
their lock identities are separate until those features are migrated.

**[Recorder, workflows, backend interfaces and complete implementation guide](WORKFLOWS.md)**
includes the **record first → Stop → review/edit → save** workflow, mock mode,
recording/playback instructions, JSON/API examples,
file inventory, tests and future hardware integration points. Automation defaults remain
`PHONE_INPUT_BACKEND=appium` and `PHONE_SCREEN_BACKEND=appium`. For a hardware-free
workflow test, run `PHONE_INPUT_BACKEND=mock python app.py --camera 0` and use the
Automations section's normalized mock preview.

## Source layout

```text
camera_phone_control/
├── app.py                 # Flask routes, control state, ownership, frame checks
├── android_control.py     # USB device discovery, resolution/rotation, ADB inputs
├── device_lock.py         # Per-device locks shared across local processes
├── automation/
│   ├── __init__.py
│   ├── runner.py          # Callable runner, run state, and CLI entry point
│   ├── context.py         # Controller helpers, cancellation, explicit waits
│   ├── backends.py        # Selected input/screen backends and guarded facades
│   ├── coordinates.py     # Shared normalized/pixel conversion
│   ├── input/             # Generic, current ADB-backed, and mock input
│   ├── screen/            # Generic/current screen providers; camera placeholder
│   ├── workflows/         # Schema, storage, recorder, execution, local API
│   ├── workflow_data/     # Saved JSON workflows
│   └── scripts/
│       ├── __init__.py    # Explicit registry of predefined scripts
│       ├── settings_demo.py
│       └── open_mae.py
├── camera.py              # External camera capture and in-memory JPEG frames
├── calibration.py         # Four-point perspective mapping using NumPy
├── requirements.txt
├── templates/index.html
├── static/workflows.js
├── static/app.js
├── static/style.css
└── tests/
    ├── test_control.py    # Shared simulated camera fixture
    ├── test_workflows.py  # Backend, workflows, storage, API, ownership
    ├── test_record_first.py # Automatic capture, privacy and timeline editing
    ├── test_workflow_ui.js # Recorder pointer/input UI tests
    ├── test_automation.py # Runner, discovery, cancellation, cross-process locking
    ├── test_takeover.py   # Manual preemption, safe handoff, lease expiry
    └── test_keyboard.js   # Dashboard keyboard behavior tests
```

## Legacy USB setup on this Mac

From `/Users/weizhe/Documents/phone_controller`, inspect the existing tools:

```zsh
python3 --version
adb version
adb devices -l
```

Only if ADB is missing:

```zsh
brew install android-platform-tools
```

Use the existing Python virtual environment:

```zsh
cd /Users/weizhe/Documents/phone_controller
source appium_phone_poc/venv/bin/activate
pip install -r camera_phone_control/requirements.txt
cd camera_phone_control
PHONE_MANUAL_INPUT_BACKEND=appium python app.py --camera 0
```

Open **http://127.0.0.1:8765** in Chrome or Safari on this Mac. Input starts
**Disabled**. Stop the server with Ctrl+C. There is no background service.
The dependencies have been installed in the existing environment during development.

For a standalone copy, Python 3.10+ is required. Create an environment with
`python3 -m venv venv`, then `source venv/bin/activate` and
`pip install -r requirements.txt` before starting `python app.py`.

### Camera OCR setup

Camera text detection uses PaddleOCR's PP-OCRv5 mobile detector and its
English-specific PP-OCRv5 mobile recognizer on the CPU. The document-orientation
and document-unwarping models are disabled; text line orientation is also
disabled by default. Paddle and its models are created lazily, exactly once in a
persistent spawned worker process, then reused for the life of the server. This
keeps Paddle's native inference work and Python-runtime contention out of the
camera/Flask process. The first request can take longer while the models are
downloaded and initialized.

On an Apple Silicon Mac (`uname -m` prints `arm64`), install the CPU build in the
active virtual environment with:

```zsh
python -m pip install --upgrade pip
python -m pip install paddlepaddle==3.3.0 \
  -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
python -m pip install "paddleocr>=3.3,<4"
```

The same dependencies are pinned in `requirements.txt`, so a fresh environment
can instead use `python -m pip install -r requirements.txt`. Current PaddlePaddle
macOS wheels support Apple Silicon; use Linux or another supported platform for
an Intel Mac that cannot install the pinned wheel.

The default input is the calibrated, high-resolution display crop converted to
BGR without thresholding. Set `CAMERA_OCR_SCALE=1.25` or `1.5` only when the
camera image makes very small text benefit from upscaling. Enable text-line
orientation only for screens that regularly rotate individual lines:

```zsh
CAMERA_OCR_TEXTLINE_ORIENTATION=1 python app.py --camera 0
```

OCR uses four CPU inference threads in the worker by default. Tune this between
1 and 16 only after measuring on the target machine:

```zsh
CAMERA_OCR_CPU_THREADS=2 python app.py --camera 0
```

Successful OCR results are reused only when a downsampled screen signature is
effectively unchanged. `CAMERA_OCR_CACHE_THRESHOLD` controls both the normalized
mean difference and changed-pixel ratio; its conservative default is `0.001`,
`0` disables caching, and the accepted range is 0 through 0.1:

```zsh
CAMERA_OCR_CACHE_THRESHOLD=0.002 python app.py --camera 0
```

For one-request diagnostics, save the exact NumPy image passed to PaddleOCR and
log its dimensions, detected text, region count, confidence, and separate
capture, decode, warp, copy, preprocessing, worker startup, IPC send/wait,
child-process inference, total timing, cache status, worker PID, and main PID:

```zsh
CAMERA_OCR_DEBUG=1 \
CAMERA_OCR_DEBUG_DIR=/tmp/camera-ocr-debug \
python app.py --camera 0
curl http://127.0.0.1:8765/api/ocr/texts
```

To profile camera delivery around an OCR request, restart with preview debugging
enabled:

```zsh
CAMERA_PREVIEW_DEBUG=1 CAMERA_OCR_DEBUG=1 python app.py --camera 0
```

The server logs physical capture FPS, latest-frame publication FPS, preview
response FPS, camera-sequence FPS, response intervals, route time, active request
count, thread count, and OCR start/finish timing. The browser console separately
logs frame-fetch, response-body, bitmap-decode, canvas-render, and total preview
timings. This instrumentation is disabled unless `CAMERA_PREVIEW_DEBUG` is set.

PaddleOCR is the normal implementation and does not invoke Tesseract. The prior
provider remains available for a temporary same-camera comparison when
Tesseract is installed. Stop the server between runs, keep the phone and camera
fixed, and call the same endpoint after each start:

```zsh
# PaddleOCR
python app.py --camera 0
curl http://127.0.0.1:8765/api/ocr/texts

# Optional legacy comparison
CAMERA_OCR_ENGINE=tesseract python app.py --camera 0
curl http://127.0.0.1:8765/api/ocr/texts
```

### Camera selection and permission

Attach your external USB camera and aim it at the phone. Camera index `0` is only
a default; on a MacBook it may be the built-in camera. If needed, stop the server
and try `python app.py --camera 1`, then `--camera 2`. Confirm visually that the
feed comes from the intended external camera. You can inspect detected hardware:

```zsh
system_profiler SPCameraDataType
```

That output lists devices, not a guaranteed OpenCV index mapping. Allow macOS
camera access when prompted for the launching Terminal/Python/IDE. If denied,
check **System Settings → Privacy & Security → Camera**, enable access for the
launching application, and restart it. Close other applications that exclusively
hold the camera. This project does not request Screen Recording permission.

Optional startup parameters:

```zsh
python app.py --camera 1 --width 1280 --height 720 --port 8765
```

The requested frame size may be adjusted by the camera; the dashboard uses the
actual delivered dimensions. Frames are captured at a requested 15 FPS. The
browser fetches the latest JPEG sequentially and draws it on a canvas, typically
around 10–15 FPS depending on camera and decoding latency. HTTP is sufficient for
this POC; no WebSocket, cloud service, or video recording is used.

### Phone preparation

Enable Android Developer Options and USB debugging. Connect a data-capable USB
cable, manually unlock the phone, and accept the computer's debugging prompt.

```zsh
adb devices -l
adb shell wm size
```

Exactly one physical phone must be connected, with state `device` and a `usb:`
transport marker. Unauthorized/offline devices and wireless connections are
rejected. Emulators cannot be controlled. Multiple physical devices are rejected.
Keep the phone awake and manually unlocked; the server never unlocks it.
Open **Android Settings manually** for the first calibration and navigation test.

## Legacy USB calibration and control

1. Secure the camera and phone so neither moves. Prefer a straight view with all
   four display corners visible. Leave the phone's orientation fixed.
2. Click **Calibrate display**. This disables input and clears old calibration.
3. In the camera feed, click the lit display's **top-left**, **top-right**,
   **bottom-right**, then **bottom-left** corner. These are the phone screen's
   corners in its current orientation, even if the camera itself is tilted.
   Include the entire active display and exclude the bezel. Calibration clicks
   never send Android input.
4. Check the numbered outline. **Undo corner** removes the latest point;
   **Calibrate display** restarts selection. Click **Save calibration**.
5. Click **Enable Manual Control**. Click a harmless Settings navigation item
   inside the outlined display. The footer shows normalized phone coordinates
   and the resulting Android pixel coordinates.
6. Try **Back**, **Home**, **Swipe up**, or **Swipe down**. Swipes move the finger
   vertically through the center of the phone, between 25% and 75% of its height.
7. Click **Disable input** when finished.

Calibration is kept only in server memory. Restarting the server requires new
calibration. Changing the device, display resolution, rotation, or camera frame
size invalidates it. **Moving the phone/camera without a geometry change cannot
be detected automatically**: disable control and recalibrate after either moves.
One perspective transform assumes a flat screen; extreme lens distortion, glare,
occlusion, or a curved display can reduce accuracy. No automatic image analysis
or lens correction is performed.

## Legacy USB keyboard passthrough

After calibration, enable **Manual Control**, then click a harmless text field
on the phone through the camera view (for example, Android Settings search).
When **Keyboard control active** appears, type on the computer keyboard. The
phone's currently focused field receives the input; there is no dashboard text
box, Send button, clipboard access, or keyboard history.

- Supported text: `a-z`, `A-Z`, `0-9`, space, and printable ASCII punctuation
  (including quotes, percent, slash, backslash, and shell metacharacters).
  Unicode, emoji, dead keys, and IME composition are not forwarded. Rendering
  depends on Android's key mapping and the receiving app.
- Unmodified **Enter**, **Backspace**, and **Tab** send `KEYCODE_ENTER`,
  `KEYCODE_DEL`, and `KEYCODE_TAB`. **Shift+Tab** stays in the browser for dashboard
  navigation. Enter/Space on a focused dashboard button or link retain their
  normal activation behavior; click the camera view to return keyboard focus.
- All Cmd/Ctrl/Alt combinations stay local, including reload, address bar, new
  tab, close tab, and copy/paste/cut/select-all. Function/system keys and typing
  into local editable elements are not captured.
- Keyboard input requires this tab's control lease, valid calibration, a healthy
  phone/camera, a recent displayed frame, and a visible, focused dashboard.
  Switching tabs disables Manual Control through the existing visibility and
  heartbeat handling. Returning requires explicitly enabling control again.
- The existing single-command gate also covers typing. While a command runs,
  the indicator says **Keyboard control paused · command in progress**; additional
  keys are not forwarded or buffered. Type slowly and check the phone: fast typing
  can lose characters. Only forwarded keys have their browser default prevented.
  Input errors disable control; uncertain input is never automatically retried.
- Typed values are used only transiently to issue the command. The controller
  does not log, echo in responses, persist, or keep a history of keyboard values.
  The receiving Android app handles text normally, and text visible on the phone
  can appear in the existing live camera feed.

### Keyboard API

`POST /api/key` uses the same local-request protection, `X-Control-Token`, and
`X-Control-Client` headers as the existing controls. The body must contain exactly
`type`, `value`, and `frame_id`; the frame must be recently displayed in this tab:

```json
{ "type": "text", "value": "a", "frame_id": 123 }
```

```json
{ "type": "key", "value": "enter", "frame_id": 123 }
```

Text must be exactly one printable ASCII character. Named keys are limited to
`enter`, `backspace`, and `tab`. Success returns `{"sent":true}` without echoing
input. Keyboard requests do not renew the lease; the existing heartbeat does.
The server chooses the validated USB serial, never a serial supplied in the body.

Printable characters use `adb -s SERIAL shell input text VALUE`, with quoting
for the [device's shell](https://developer.android.com/tools/adb#shellcommands)
and `%s` for space. See Android's
[input text implementation](https://android.googlesource.com/platform/frameworks/base/+/8417c7bb1cbb/services/core/java/com/android/server/input/InputShellCommand.java).
Enter/Backspace/Tab use `adb -s SERIAL shell input keyevent KEYCODE` with the fixed
codes above. No PIN, OTP, password, banking credential, transaction confirmation,
biometric, or security-warning handling is implemented.

## Predefined scripts from the terminal

The browser supports manual control, Take Over and saved JSON workflows. Predefined
Python scripts remain terminal-based; there is no arbitrary script-launch API.
The runner uses the same `AndroidControl` implementation as manual input and needs
neither Flask, calibration, a browser lease, nor a running camera. The existing
dashboard can remain open to observe the camera feed.

With the existing virtual environment active, run from `camera_phone_control`:

```zsh
python -m automation.runner --list
python -m automation.runner settings_demo
python -m automation.runner settings_demo --serial N0AA003744K62800112
```

`settings_demo` wakes the phone, presses Home, launches Android Settings, and waits
one second. Manually unlock the phone before running it. It does not enter text,
tap screen coordinates, change settings, unlock the device, or handle security
prompts. The controller resolves the requested package's launcher activity, validates the
package/component arguments, and starts that activity through ADB.

Without `--serial`, automation selects a phone only when exactly one authorized
physical USB phone is available. Zero/multiple candidates fail clearly. With
`--serial`, it validates that exact phone; unauthorized/offline phones, emulators,
and wireless ADB remain unsupported. The manual dashboard retains its stricter
single-attached-phone selection. **One tested Android phone remains the current
hardware scope**; serial selection and lock isolation prepare for future devices,
not a multi-device dashboard.

### Exclusive device ownership

Each serial has an OS ownership lock shared by dashboard and runner processes
under `/tmp/camera-phone-control-<user-id>/`. Both must run as the same OS user.
The directory is private to that user. Filenames are hashes of serials. The
`.lock` file contains only a coarse owner label (`manual` or `automation`), and
`.takeover` contains a random request token. A `.guard` lock serializes short
metadata operations; it is never held during device commands or waits. No typed
input or script details are written to these files.

- **Automation has priority while it owns the device.** Normal manual controls,
  keyboard passthrough, and click-to-tap are disabled. The camera stays visible.
  The dashboard shows **Automation running** and **Manual control disabled**.
  Normal manual input and Enable Manual Control return HTTP 409 with
  `{"ok":false,"error":"device_busy","owner":"automation"}`; they never request
  cancellation or queue input.
- To interrupt a run, explicitly press **Take Over**. Saved workflows can be recorded and launched from Automations; predefined Python
  scripts still use the terminal runner. Take Over
  can request cancellation without calibration or a live camera, since it sends
  no Android input. Actual manual input retains all existing health/calibration
  checks.
- `POST /api/takeover` takes an empty JSON object and the existing local control
  token/client headers. It selects the configured USB phone and verifies that
  automation currently owns it. It then writes the per-device takeover signal
  and immediately returns HTTP 202:
  `{"enabled":false,"pending":true,"control_status":"takeover_requested"}`.
  A free device is rejected with HTTP 409 and `error:"no_automation"`.
  Another tab cannot steal an already-pending takeover request.
- During **Takeover requested**, automation retains the OS lock until its current
  ADB invocation finishes or times out. Cancellation is checked between steps,
  before new Android actions, and at most every 50 ms during `context.wait`.
  The runner records `takeover_requested` when detected, then `cancelled` after
  releasing ownership. It logs that cancellation was due to takeover. No ADB
  command is forcibly interrupted and no queued actions are replayed.
- The requesting browser continues heartbeats. After automation releases the
  lock, a fresh heartbeat completes the handoff, normally within 1.5 seconds.
  Manual input becomes active if camera health, calibration, and frame freshness
  still pass. Otherwise input remains disabled until those conditions are restored
  and Manual Control is enabled. The dashboard reports the coarse states `free`,
  `manual`, `automation`, and `takeover_requested`, without script details.
- The browser's six-second manual lease selects the controlling tab; manual
  commands take the OS lock individually. An externally triggered automation run
  can therefore start between manual actions, even when Manual Control was enabled.
  If a manual command is already in flight, a new run fails busy instead of
  issuing concurrent input. Explicit takeover reserves the handoff until it is
  completed or withdrawn; unrelated automation cannot steal it midway.
- Disable input, hide/close the tab, or lose its heartbeat lease to release a
  pending takeover reservation. A backend timer expires abandoned reservations
  even without more HTTP requests. A retained random request token ensures the
  old run still detects cancellation if a request is withdrawn before its next
  check. **Cancelled automation never resumes automatically**, including when
  manual control ends. A new run needs an explicit external trigger.
- Camera viewing and status remain available throughout takeover. Success,
  failure, and cancellation release automation's lock in cleanup; process exit
  also releases OS locks. A leftover file is not an active lock. Do not delete
  lock/signal files while either process is running. Different serials have
  independent ownership, takeover signals, and metadata locks. The code uses
  [Python's nonblocking `flock`](https://docs.python.org/3/library/fcntl.html#fcntl.flock)
  on macOS/Linux; Windows is not supported.
- These locks coordinate this project's entry points. Raw ADB, Appium, and other
  software do not automatically participate. Both dashboard and runner must use
  this version's takeover protocol.

### Cancellation, results, and future triggers

Press **Ctrl+C** to request cancellation. Explicit waits use a cancellation event
and finish promptly. An in-flight ADB invocation finishes or reaches its existing
timeout before cancellation stops subsequent steps; the runner does not send a
terminal interrupt to the ADB child. Uncertain inputs are never retried. A command
already delivered to Android cannot be retracted.

The CLI returns `0` on completion, `1` on failure, and `130` on cancellation
(`2` for invalid CLI syntax). Lifecycle logs include script/lock start and end,
fixed step labels, and failure categories. They exclude keyboard values and
runtime exception details that could contain input. Run state stays in memory:
script name, selected serial, running/takeover_requested/cancelled/completed/failed
status, a takeover flag, UTC start
and end timestamps, current step, and a short message. There is no run-history
database or keyboard history.

Future components can call the runner without invoking CLI code:

```python
from automation import runner

result = runner.run("settings_demo", serial="N0AA003744K62800112")
print(result.status, result.message)
```

For cancellation or inspection while running, create a `runner.Runner`, pass a
`threading.Event` as `cancel_event` to its `run` method, and inspect that instance's `state`.
Use separate Runner instances for concurrent devices. Scripts are trusted local
Python functions registered explicitly in `automation/scripts/__init__.py`, not
file paths or modules supplied by the CLI. A script receives `context.device`,
`context.controller`, `context.cancel_event`, `context.logger`, and `context.state`.
Use `context.send(action, pixel=None)`, `context.send_key(kind, value)`,
`context.launch_app(package)`, and `context.wait(seconds)` to get step logging,
device health/geometry checks, and cancellation between steps. During any custom
long-running work, call `context.check_cancelled()` regularly and use
`context.wait` for delays so manual takeover can be handled promptly. Those helpers
delegate to the existing Android controller; `send_key` retains its single ASCII
character/fixed keyevent limits. Use fixed diagnostic labels and never log typed
values or credentials in custom scripts.

Triggers, runner, and controller remain separate. The local workflow execution API
is documented in [WORKFLOWS.md](WORKFLOWS.md). Authentication and final financial
authorization belong in manual checkpoints. Pi workflow input is available with
`PHONE_INPUT_BACKEND=raspberry_pi`; screen handling remains separately configured.
See [Pi workflow playback](WORKFLOWS.md#pi-workflow-playback). External/public
webhooks, scheduling, ESP32 input and visual automation are not implemented.

## How clicks are mapped

The canvas displays the complete camera frame without cropping or letterboxing.
Browser coordinates are normalized against its actual displayed rectangle:

```text
camera_x = (clientX - canvas.left) / canvas.width
camera_y = (clientY - canvas.top)  / canvas.height
```

The four selected camera points map to phone coordinates `(0,0)`, `(1,0)`,
`(1,1)`, `(0,1)`. NumPy solves the eight coefficients of a projective transform
(homography). This handles perspective, rather than just scaling a rectangular
camera crop. The backend rejects clicks outside the selected quadrilateral.

```text
[u', v', w'] = homography × [camera_x, camera_y, 1]
phone_u = u' / w'
phone_v = v' / w'
tap_x = nearest_integer(phone_u × (screen_width  - 1))
tap_y = nearest_integer(phone_v × (screen_height - 1))
```

`adb shell wm size` supplies the dimensions; its Override size takes precedence
over Physical size. Rotation is read from `dumpsys input`, with a
`dumpsys window displays` fallback. Width/height are swapped for 90°/270° rotation.
If this Android build does not expose a recognized rotation field, control stops
with an actionable error rather than guessing. Rotation must stay fixed during
a command; hardware changes between the check and input cannot be made atomic.

The final command uses the selected serial explicitly:

```text
adb -s SERIAL shell input tap X Y
adb -s SERIAL shell input keyevent 4      # Back
adb -s SERIAL shell input keyevent 3      # Home
adb -s SERIAL shell input swipe X Y1 X Y2 300
```

## Control state and local access

- Input starts disabled and is disabled during calibration. Both backend and
  frontend enforce this; hidden or disabled buttons are not the only check.
- All manual actions require a valid calibration, the same phone geometry, and a camera
  frame displayed less than two seconds ago. Commands with stale frames are rejected.
- The controlling browser sends heartbeats. Control expires after six seconds
  without one. Hiding/closing the tab disables input immediately when possible;
  the lease is the fallback. A second tab cannot take control while the first owns it.
- Only one input command runs at once. Overlapping commands are rejected, and
  failed/uncertain commands are never retried automatically. Disabling input cannot
  retract an ADB command already sent to the phone.
- The server binds to **127.0.0.1 only**, accepts local Host names, rejects cross-site
  requests, and requires a per-server token for state changes. All frontend assets
  are local; no analytics or CDN requests occur. This is a trusted-single-user local
  POC, not a multi-user authentication system. Do not tunnel it or expose it to a LAN.
- OpenCV captures/encodes the external camera feed. Workflow recording stores
  actions, not camera video. On-demand screen capture is available through
  `context.screen.capture()` as PNG bytes; no screenshots are saved automatically.
  Use manual checkpoints for authentication and final financial authorization.
  Coordinate taps cannot determine an app action's meaning.

## Errors and diagnostics

| Message / symptom                          | What to check                                                                         |
| ------------------------------------------ | ------------------------------------------------------------------------------------- |
| No physical phone / unauthorized / offline | USB data cable, one attached phone, debugging prompt                                  |
| USB transport missing                      | Disconnect wireless ADB and reconnect by USB                                          |
| Camera unavailable / permission denied     | Correct `--camera` index, macOS Camera permission, other camera apps                  |
| Camera froze or disconnected               | Control is disabled; reconnect and restart capture, then recalibrate                  |
| Cannot determine rotation                  | Inspect the two `dumpsys` commands above; add support for the device's rotation field |
| Click outside display                      | No tap sent; click within the saved outline or recalibrate                            |
| Invalid/crossed/tiny corners               | Select the four display corners in the labelled order with a clearer view             |
| Phone geometry changed                     | Recalibrate after rotation or a resolution/device change                              |
| Input disabled / heartbeat expired         | Wait for live video, then explicitly enable control                                   |
| ADB timeout / connection lost              | Inspect the phone before another action; an uncertain input is not repeated           |
| Port already in use                        | Stop the other local server or choose `--port 8766`                                   |

The UI shows errors and connection details. Console logs record camera errors,
control-state changes, device serials, button action names, and tap coordinates.
Keyboard text and key values are excluded from logging and responses. Camera
images stay in memory and are not saved. General input errors disable the browser's
Manual Control; enable it again only after checking the reported condition.

## Tests

From `camera_phone_control` with the virtual environment active:

```zsh
python -m unittest discover -s tests -p 'test_workflows.py' -v
python -m unittest discover -s tests -p 'test_record_first.py' -v
python -m unittest discover -s tests -p 'test_automation.py' -v
python -m unittest discover -s tests -p 'test_takeover.py' -v
node --check static/app.js
node --check static/workflows.js
node --test tests/test_workflow_ui.js
node --test tests/test_keyboard.js
```

The pre-existing `test_open_mae.py` imports helpers commented out in the current
MAE script and still assumes the old startup behavior. Unfiltered discovery fails
on that stale file; the new suite checks current MAE/context compatibility using
mock detections without changing the script.

The Python regression suites use simulated ADB/camera responses. It checks perspective mapping,
corner validation, coordinates, size/rotation parsing, single-device selection,
control gating, stale frames, lease expiry, local-request protection, fixed ADB
commands, and failure behavior. Automation tests check script discovery, device
selection, state transitions, cancellation, privacy, and real cross-process lock
contention/release (including process death). Takeover tests cover an in-flight
command, a separate runner process waiting for 30 seconds, prompt cancellation,
manual ownership, no concurrent input/replay/resumption, independent devices,
and automatic expiry of abandoned leases. It never taps a real phone or opens
a camera. The Node tests
run the dashboard script with simulated DOM/network objects to check direct
keydown forwarding, shortcut exclusions, ownership/health gates, no buffering, and
takeover indicators/heartbeats, and rejection of late responses after disabling
input (Node 18+; no npm dependencies).

For a physical-device smoke test, use Settings search: type `Ab9 .`, use Backspace,
then try Enter/Tab and verify the result on the phone. Confirm Cmd/Ctrl+L and
Cmd/Ctrl+C remain browser shortcuts. Disable control and verify typing sends
nothing; repeat from a second tab that does not own the lease. Automated tests
do not verify the phone's actual keyboard mapping or browser-native behavior.

The implementation follows the [Flask security guidance](https://flask.palletsprojects.com/en/stable/web-security/)
for local request handling and uses OpenCV's
[AVFoundation capture backend](https://docs.opencv.org/4.x/d4/d15/group__videoio__flags__base.html)
on macOS.
