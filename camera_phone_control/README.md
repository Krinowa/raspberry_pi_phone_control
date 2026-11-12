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

Camera text detection uses PaddleOCR's PP-OCRv5 mobile detector on the CPU.
English uses `en_PP-OCRv5_mobile_rec`; set `CAMERA_OCR_LANGUAGE=zh-HK` (or
`chinese_cht`) for Chinese with `PP-OCRv5_mobile_rec`, or
`CAMERA_OCR_LANGUAGE=ms` for Malay with `latin_PP-OCRv5_mobile_rec`. English is
the default, and unsupported languages fail explicitly. The document-orientation
and document-unwarping models are disabled; text line orientation is also
disabled by default. Paddle and its selected models are created exactly once in a
persistent spawned worker process, then reused for the life of the server. A
background warmup starts the worker before Flask begins serving so model
initialization normally finishes before the first workflow OCR request. This
keeps Paddle's native inference work and Python-runtime contention out of the
camera/Flask process.

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

OCR uses one CPU inference thread in the worker by default. On the current Mac,
one and four configured threads had effectively identical medians while one
thread had the tighter p95. Tune this between 1 and 16 only after measuring on
the target machine:

```zsh
CAMERA_OCR_CPU_THREADS=2 python app.py --camera 0
```

Text regions are recognized one at a time by default. On the current Apple
Silicon/Paddle 3.3 setup this is faster than PaddleOCR's implicit batch default
for phone screens with many differently sized lines. It remains configurable:

```zsh
CAMERA_OCR_RECOGNITION_BATCH_SIZE=1 python app.py --camera 0
```

Successful OCR results are reused only when a downsampled screen signature is
effectively unchanged. `CAMERA_OCR_CACHE_THRESHOLD` controls both the normalized
mean difference and changed-pixel ratio; its conservative default is `0.001`,
`0` disables caching, and the accepted range is 0 through 0.1:

```zsh
CAMERA_OCR_CACHE_THRESHOLD=0.002 python app.py --camera 0
```

During `wait_for_text`, the camera provider captures a cheap 96×160 signature
before the preceding input action. It polls that signature every 75 ms, requires
120 ms of visual stability after a change, and invokes PaddleOCR after settling
or every 450 ms if no change is detected. The timings are configurable with
`CAMERA_OCR_POLL_INTERVAL_SECONDS`, `CAMERA_OCR_SETTLE_SECONDS`, and
`CAMERA_OCR_FORCED_INTERVAL_SECONDS`. Change detection uses a 3-level mean pixel
difference and a 0.002 changed-pixel ratio by default; advanced deployments can
tune `CAMERA_OCR_CHANGE_THRESHOLD` and `CAMERA_OCR_CHANGE_RATIO_THRESHOLD` after
measuring camera noise.

Paddle inference uses a global pool of persistent child processes. The default
pool size is two. The selectable maximum is calculated from physical CPU cores,
RAM headroom, and an absolute safety cap of eight:

```bash
CAMERA_OCR_WORKER_POOL_SIZE=2 python app.py --camera 0
```

`OCR_CPU_CAPACITY_PERCENT` controls the CPU capacity budget and defaults to 80;
accepted values are 25 through 90. For example, eight physical cores at 80%
produce a CPU ceiling of six workers. The RAM ceiling keeps at least 25% or 2 GB
of total RAM for the OS/backend and at least 1 GB of currently available RAM for
non-OCR work. It uses observed Paddle process RSS when available, otherwise a
conservative 900 MB estimate. The final hardware ceiling is the minimum of the
CPU ceiling, RAM ceiling, and eight-worker safety cap.

The dashboard OCR Worker Pool setting is persisted privately in
`automation/camera_data/ocr_settings.json` and overrides the environment default.
Saving a different size marks the pool as requiring restart; **Restart OCR Workers**
is accepted only while no OCR requests are active or queued. Each process owns one
Paddle model, while per-device providers retain independent frame signatures and OCR
caches. Pool status is available from `GET /api/ocr/status`; settings use
`GET/PUT /api/ocr/settings` and `POST /api/ocr/restart`.

The status API also reports live CPU/RAM data and 30 seconds of bounded OCR queue
and inference history. A conservative recommendation may suggest one more worker
for sustained queue pressure with CPU/RAM headroom, or one fewer worker during
sustained CPU saturation and slow inference. Recommendations never resize or
restart the pool automatically; **Apply Recommendation** saves the proposed size,
then the existing explicit restart action applies it safely. If a stored setting
exceeds the detected startup ceiling, the backend starts the safe ceiling, keeps
the stored value intact, and exposes a warning in the dashboard.

To compare one and two workers with an exact saved real OCR frame:

```bash
python tools/benchmark_ocr_pool.py /path/to/paddle-ocr-0001.png
```

For one-request diagnostics, save the exact NumPy image passed to PaddleOCR and
log its dimensions, region count, confidence, queue wait, child CPU/thread data,
and separate capture, decode, warp, copy, preprocessing, worker startup, IPC
send/wait, child-process inference, total timing, cache status, worker PID, and
main PID. Logs deliberately omit recognized text:

```zsh
CAMERA_OCR_DEBUG=1 \
CAMERA_OCR_DEBUG_DIR=/tmp/camera-ocr-debug \
python app.py --camera 0
curl http://127.0.0.1:8765/api/ocr/texts
```

Benchmark a saved real OCR input at the production size, 75%, 50%, and 960/720
long-side caps. The report checks only the expected strings and does not print
recognized screen text:

```zsh
python -m automation.screen.benchmark_paddle \
  /tmp/camera-ocr-debug/paddle-ocr-0001.png \
  --iterations 5 --recognition-batch-size 1 \
  --output /tmp/camera-ocr-benchmark.json
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
single-attached-phone selection. Raspberry Pi mode supports selecting registered
logical phones through the dashboard with separate lock and calibration
identities.

Logical phones can also use separate external cameras. Camera assignments are
stored in `automation/camera_data/cameras.json`; the selected phone controls the
main preview, calibration, OCR, recorder capture, and workflow screen observer.
Configure source `0` for `phone_A`, source `1` for `phone_B`, or any supported
OpenCV camera URL through **Camera Settings** on its device card. The Devices
grid shows every registered phone's monitoring preview and keeps Bluetooth and
camera availability as separate states. Selecting a card changes the control
target while the other previews remain visible. **Test Camera** checks a source
and displays a temporary frame without saving the assignment.

Changing source or rotation invalidates only that phone's in-memory calibration
and prompts for recalibration; its saved calibration file is retained.
`GET /api/cameras?probe=true` checks
local indexes 0–10 without retaining probe handles. On macOS, AVFoundation camera
indexes may differ from ffmpeg enumeration. Monitoring previews run at a modest
rate, the selected main preview runs at the normal rate, and OCR remains
request-driven through the single persistent OCR worker.
Camera discovery runs only when Camera Settings requests it. Results are cached
for 20 seconds; **Refresh Cameras** explicitly refreshes the probe. The one-second
device poll reads assigned-camera status and does not probe indexes 0–10. The
discovery list includes an index only after OpenCV opens it and reads a frame.
An unavailable saved assignment remains visible as unavailable but is never
silently replaced.

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

### Developer tools configuration

Developer tools are disabled unless a non-empty `PHONE_DEVELOPER_MODE_KEY` is
configured. Source execution loads the private `camera_phone_control/.env`
file. Copy `.env.example` to `.env` and set the key there.

Do not place the key in source code, frontend JavaScript, workflow data, or a
URL saved in documentation or browser bookmarks. If the setting is absent or
empty, developer authentication remains disabled. Restart the application after
changing the private environment file.

## Account-routed external jobs

External jobs use a persistent account binding rather than the dashboard's current
phone selection. Each account has a stable `account_id`, display name, optional
provider, one `device_id`, and an enabled flag. The current model permits one account
per device. It stores routing metadata only; credentials, passwords, PINs, OTPs, and
other banking secrets do not belong in account bindings.

Account routes:

```text
GET    /api/accounts
POST   /api/accounts
GET    /api/accounts/<account_id>
PATCH  /api/accounts/<account_id>
DELETE /api/accounts/<account_id>
GET    /api/accounts/<account_id>/device
```

Create a passive queued job with:

```json
POST /api/jobs
{
  "account_id": "uob_account_1",
  "workflow": "flow_example",
  "payload": {}
}
```

The server validates the enabled account and saved workflow, resolves the account's
device once, persists that `device_id`, and returns `202 Accepted`. Changing the
dashboard control target later does not move the job. `GET /api/jobs` and
`GET /api/jobs/<job_id>` expose safe status metadata without echoing the stored
payload. `POST /api/jobs/<job_id>/cancel` immediately cancels queued jobs. Running
jobs record `cancel_requested`, signal the active workflow cancellation event, and
remain assigned until workflow ownership has been released safely.

Future HTTP/WebSocket providers enter through
`automation.jobs.ExternalIntegrationService`, rather than calling workflow or
device code directly. An injected `IntegrationAdapter` converts an unknown provider
payload into `NormalizedJobRequest(external_ref, bank, action, payload)`. A separate
injected `JobRouteResolver` maps that normalized request to an existing account and
workflow. Only then does the service resolve and persist the account's `device_id`.
No provider field names or transport schema are part of this boundary. Jobs created
through it retain nullable `external_ref`, `bank`, and `action` metadata; legacy and
local jobs load with those fields set to null.

Job states are `QUEUED`, `WAITING_FOR_DEVICE`, `RUNNING`, `COMPLETED`, `FAILED`,
`CANCELLED`, and `INTERRUPTED`. Queues are ordered independently per device. The
background executor runs one workflow thread for each free device, so different
phones may progress concurrently while each phone remains strictly sequential. A
scheduler reservation is atomic within the backend process, permits at most one
running job for a device, and never substitutes another phone. On restart, an
interrupted `RUNNING` job becomes `INTERRUPTED`; physical HID input is never replayed
automatically. Queued and waiting jobs retain their original device and remain
eligible.

Bindings and jobs are stored as private, atomically replaced JSON files under
`automation/job_data/`. The dashboard shows the account assigned to each device and
recent jobs with queue position, timestamps, result/error, and valid cancellation
actions. These local APIs currently retain the dashboard's
same-origin and control-token protections; a production external service needs a
separate authenticated ingress policy before network exposure. The JSON stores and
scheduler serialize threads in the current single Flask process. A future
multi-process or multi-host deployment must move reservation and queue transitions
to a transactional database or another shared locking system.

The intended worker flow is:

```text
POST /api/jobs -> persistent job -> per-device scheduler -> background worker
-> CameraManager frame -> OCR / visual state -> workflow -> Pi HID
-> persist result -> release device
```

The normal server entry point starts one executor, which polls persisted eligibility
independently of browser requests and resolves each job's assigned camera and saved
calibration when execution starts. Application-factory users must opt in with
`start_job_executor=True`, preventing test factories or multiple WSGI workers from
accidentally starting competing schedulers. Per-device OCR providers keep separate
frame/cache state while sharing the one persistent Paddle worker. Closing the browser
therefore has no effect on a running job.

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

## RemoteViewer frame endpoint

`GET /agent/v1/devices/<device_id>/frames` gives the RemoteViewer device agent,
running on this same PC, a read-only stream of uncompressed camera frames. It
follows section 2 of RemoteViewer's `docs/pi-interface.md`. It carries pictures
only. The code is in `agent_frames.py`. Remote control of a phone is a separate
set of addresses, described in the next section.

**Secret file.** The agent and this app share one secret of at least 32 random
characters, kept in a file that only the operator's Windows user can read.
Create it once, in the folder that holds `agent.exe`:

```bat
python -c "import secrets, pathlib; pathlib.Path('pi.secret').write_text(secrets.token_urlsafe(48))"
icacls pi.secret /inheritance:r /grant:r "%USERNAME%:F"
```

Then tell this app where the file is, either in `camera_phone_control\.env` or as
an environment variable set before starting the app:

```text
REMOTEVIEWER_PI_SECRET_FILE=C:\full\path\to\pi.secret
```

The file is read once at start, so restart the app after creating or changing
it. The app refuses to start if the variable is set but the file is missing or
the secret is shorter than 32 characters. If the variable is not set, the
endpoint answers `401` to everything. Never put the secret itself in `.env`, in
a command line or in Git (`*.secret` is ignored).

**Port.** The endpoint is served by the dashboard's own server, on `127.0.0.1`
only, at the port given with `--port` (default `8765`). The stream is not
encrypted; do not forward or tunnel this port.

**Device ids** are the dashboard's phone ids: `phone_A`, plus every phone
enrolled on the Pi (the ids shown in the dashboard's phone list). An id that is
neither enrolled nor has a camera assignment answers `404`.

**Check it** on the Windows PC with the app running:

```bat
agent.exe check-frames -device phone_A -pi-secret-file pi.secret -pi http://127.0.0.1:8765
```

It must print `OK: the agent can use this endpoint`.

Frames come from the same capture loop as the dashboard preview, so a phone
that is not the one selected in the dashboard is captured at the lower
monitoring frame rate.

## RemoteViewer remote control

Four more addresses let the RemoteViewer agent operate a phone through the
existing input code (Pi or Appium). They follow sections 3 and 4 of
RemoteViewer's `docs/pi-interface.md`; the code is in `remote_control.py`.
They use the same secret file, port and device ids as the frame endpoint, and
the agent's requests need no dashboard control token.

| Address | Purpose |
|---|---|
| `POST /agent/v1/devices/<id>/takeover` | Take the phone. `200` with `interrupted_job` (or `null`); `409` with `local_operator`, `held_by_remote`, `critical_step` or `job_running`. |
| `POST /agent/v1/devices/<id>/takeover/renew` | Keep the lease. `409` once the session no longer holds the phone. |
| `DELETE /agent/v1/devices/<id>/takeover?session_id=…` | Release. Always `200`. |
| `POST /agent/v1/devices/<id>/input` | One tap, swipe, text, key, `home`, `back` or `wake`. `403` unless the session holds the phone; `200` after the input was carried out. |

**Lease.** Without a renew for 6 seconds the phone is released automatically.

**Running jobs.** With `allow_during_job` the job stops at its next input and
is recorded as `INTERRUPTED`, naming the user, session, reason and time. Its
phone is then marked *Needs Attention*, so it is retried only after the
operator verifies the phone, as for any other interrupted job. No new job
starts on a phone under remote control.

**Critical steps.** A workflow step ticked *Critical* in the workflow editor is
never interrupted: a takeover during it answers `409 critical_step`, whatever
`allow_during_job` says. Tick the step that confirms a payment.

**Coordinates** are fractions of the camera picture served by the frame
endpoint. Taps and swipes use the phone's saved calibration; a point outside
the phone is ignored. Taps and swipes are refused (`409 not_calibrated`) until
the phone is calibrated. Text is passed to the phone as data, never through a
shell. The Pi accepts printable ASCII text only; other text, and the `escape`
key, answer `422 unsupported_input` without sending anything.

**Dashboard.** A phone under remote control shows who holds it, why and since
when, with an *End Remote Control* button that takes it back (the agent's next
renew then gets `409`). Job History lists remote sessions and how each ended.
The audit is kept in `automation/job_data/remote_control_history.jsonl`; typed text is
never written there.

**Swipe duration on the Pi.** A swipe that starts and ends at the same point is
a long press. Appium holds it for `duration_ms`; the Pi API takes geometry only,
so the Pi's HID server decides how long it is held.

## RemoteViewer screen position

Section 5 of the same contract: viewers crop the camera picture to the phone
screen with this app's calibration, and a remote user holding the takeover can
correct it. There is one calibration per device, the same one the dashboard
saves (`automation/camera_data/calibration*.json`).

| Address | Purpose |
|---|---|
| `GET /agent/v1/devices/<id>/screen` | `{"corners": [[x, y], …]}`: top-left, top-right, bottom-right, bottom-left of the phone screen, in fractions of the frame picture; `{"corners": null}` when not calibrated. |
| `PUT /agent/v1/devices/<id>/screen` | `{"session_id", "corners"}`. `403` unless the session holds the takeover; `400 {"error": "bad_corners"}` unless the corners pass the dashboard's checks (four points in 0–1, convex, area at least 0.005). `200` returns the stored corners. |

A remote change is saved and used at once, exactly like a dashboard
calibration: the dashboard, the bot and the next remote input all use it. It
waits until a stopped job has handed over the phone. The calibration file
records who made the last change (`changed_by`: `dashboard`, or `remote` with
the session and user), and Job History lists remote recalibrations. If the
camera or phone is not ready the change is refused with `409
device_unavailable`.

## Tests

From `camera_phone_control` with the virtual environment active:

```zsh
python -m unittest discover -s tests -p 'test_workflows.py' -v
python -m unittest discover -s tests -p 'test_record_first.py' -v
python -m unittest discover -s tests -p 'test_automation.py' -v
python -m unittest discover -s tests -p 'test_takeover.py' -v
python -m pytest tests/test_agent_frames.py -v
python -m pytest tests/test_remote_control.py tests/test_remote_control_app.py -v
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
