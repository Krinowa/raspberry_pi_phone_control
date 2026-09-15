# Local camera phone controller

A small macOS dashboard for manually controlling **one physical USB Android phone**
while viewing it through an **external webcam or camera**. Python captures that
camera; the browser displays its live feed. A calibrated click sends one ADB tap.
Back, Home, Swipe up, and Swipe down are explicit buttons.
Manual Control also forwards supported keyboard input from the browser document
to the phone, without a text box or Send button.

This is separate from `appium_phone_poc`. It does not import or execute that script,
launch banking apps, acknowledge warnings, enter credentials, or run a workflow.
The existing Appium server at `http://127.0.0.1:4723` is not required by this
dashboard: these controls use ADB directly. Stop other phone automation scripts
while using manual control so two controllers do not operate the device at once.

## Source layout

```text
camera_phone_control/
├── app.py                 # Flask routes, control state, ownership, frame checks
├── android_control.py     # USB device discovery, resolution/rotation, ADB inputs
├── camera.py              # External camera capture and in-memory JPEG frames
├── calibration.py         # Four-point perspective mapping using NumPy
├── requirements.txt
├── templates/index.html
├── static/app.js
├── static/style.css
└── tests/
    ├── test_control.py    # Backend, ADB, calibration, and camera tests
    └── test_keyboard.js   # Dashboard keyboard behavior tests
```

## Setup on this Mac

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
python app.py --camera 0
```

Open **http://127.0.0.1:8765** in Chrome or Safari on this Mac. Input starts
**Disabled**. Stop the server with Ctrl+C. There is no background service.
The dependencies have been installed in the existing environment during development.

For a standalone copy, Python 3.10+ is required. Create an environment with
`python3 -m venv venv`, then `source venv/bin/activate` and
`pip install -r requirements.txt` before starting `python app.py`.

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

## Calibrate and control

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

## Keyboard passthrough

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
{"type":"text","value":"a","frame_id":123}
```

```json
{"type":"key","value":"enter","frame_id":123}
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
- All actions require a valid calibration, the same phone geometry, and a camera
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
- OpenCV is used only to capture/encode frames from the external camera. No ADB
  screenshots, screen mirroring, Appium screenshots, OCR, recordings, PIN/OTP entry,
  biometric interaction, warning acknowledgement, or banking workflow is implemented.
  Manual taps are general inputs and cannot determine an app action's meaning;
  use harmless screens, not banking/security flows, for this POC.

## Errors and diagnostics

| Message / symptom | What to check |
| --- | --- |
| No physical phone / unauthorized / offline | USB data cable, one attached phone, debugging prompt |
| USB transport missing | Disconnect wireless ADB and reconnect by USB |
| Camera unavailable / permission denied | Correct `--camera` index, macOS Camera permission, other camera apps |
| Camera froze or disconnected | Control is disabled; reconnect and restart capture, then recalibrate |
| Cannot determine rotation | Inspect the two `dumpsys` commands above; add support for the device's rotation field |
| Click outside display | No tap sent; click within the saved outline or recalibrate |
| Invalid/crossed/tiny corners | Select the four display corners in the labelled order with a clearer view |
| Phone geometry changed | Recalibrate after rotation or a resolution/device change |
| Input disabled / heartbeat expired | Wait for live video, then explicitly enable control |
| ADB timeout / connection lost | Inspect the phone before another action; an uncertain input is not repeated |
| Port already in use | Stop the other local server or choose `--port 8766` |

The UI shows errors and connection details. Console logs record camera errors,
control-state changes, device serials, button action names, and tap coordinates.
Keyboard text and key values are excluded from logging and responses. Camera
images stay in memory and are not saved. General input errors disable the browser's
Manual Control; enable it again only after checking the reported condition.

## Tests

From `camera_phone_control` with the virtual environment active:

```zsh
python -m unittest discover -s tests -v
node --check static/app.js
node --test tests/test_keyboard.js
```

The Python suite uses simulated ADB/camera responses. It checks perspective mapping,
corner validation, coordinates, size/rotation parsing, single-device selection,
control gating, stale frames, lease expiry, local-request protection, fixed ADB
commands, and failure behavior. Keyboard tests also check strict payloads, every
printable ASCII character's shell quoting, shared command locking, and privacy
on success/failure. It never taps a real phone or opens a camera. The Node tests
run the dashboard script with simulated DOM/network objects to check direct
keydown forwarding, shortcut exclusions, focus/health gates, no buffering, and
no retries (Node 18+; no npm dependencies).

For a physical-device smoke test, use Settings search: type `Ab9 .`, use Backspace,
then try Enter/Tab and verify the result on the phone. Confirm Cmd/Ctrl+L and
Cmd/Ctrl+C remain browser shortcuts. Disable control and verify typing sends
nothing; repeat from a second tab that does not own the lease. Automated tests
do not verify the phone's actual keyboard mapping or browser-native behavior.

The implementation follows the [Flask security guidance](https://flask.palletsprojects.com/en/stable/web-security/)
for local request handling and uses OpenCV's
[AVFoundation capture backend](https://docs.opencv.org/4.x/d4/d15/group__videoio__flags__base.html)
on macOS.
