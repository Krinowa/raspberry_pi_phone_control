# Raspberry Pi manual control

The dashboard sends manual input through `RaspberryPiInputBridge` and a single
`PiApiClient`. The client calls the existing Pi HTTP API; the Pi firmware and HID
server are unchanged. Viewing and calibration still use the external camera.

## Start

Use the existing Python environment (its directory name does not mean an Appium
server is required):

```zsh
cd /Users/weizhe/Documents/phone_controller/camera_phone_control
source ../appium_phone_poc/venv/bin/activate
export PHONE_MANUAL_INPUT_BACKEND=raspberry_pi
export PI_API_BASE_URL=http://192.168.68.61:8766
export PI_API_CONNECT_TIMEOUT=2
export PI_API_READ_TIMEOUT=5
python app.py --camera 0
```

Open http://127.0.0.1:8765. Choose the external camera index and allow macOS camera
access as needed. These environment settings are also the normal manual-control
defaults. They are read from the process environment, not automatically from a
`.env` file. The client uses Python's standard HTTP library; no new dependency is
needed. Connection and read timeouts must be positive, finite seconds.

No Appium or ADB connection, USB debugging, Developer Options, or Android serial
is needed to start or use Pi manual control. The Mac and Pi must share the private
network and the phone must already be paired with the proven Pi HID setup.

## Check status

The dashboard displays **Pi API: Online** and **Phone HID: Connected** using the
Pi's `/status` alone, once on dashboard load and once when you explicitly enable
Manual Control. Enabling requires a fresh response reporting the phone HID as
connected; a failed check or waiting phone leaves input disabled, without retrying.
The badges retain the last-known status. There is no background Pi polling.
Actions, lease heartbeats, and ordinary dashboard status requests never check the
Pi. `/health` remains available in the client for explicit diagnostics.
Reload the dashboard to refresh its badges, or request a fresh summary without
sending phone input:

```zsh
curl http://127.0.0.1:8765/api/pi/status
```

Expected: `api` is `online` and `phone` is `connected`. The live Pi returned
`ok: true` from both endpoints during integration, including `connected: true`
and `OK connected mode=mt keyboard=yes` from `/status`.

## Verify manual controls

First light the display manually, keep the phone and camera fixed, and calibrate
the four display corners: top-left, top-right, bottom-right, bottom-left. Save
calibration and enable Manual Control. Existing fresh-frame, browser ownership,
and input-lock checks still apply to every manual input, including Wake.

| Dashboard action         | Verification on phone                                                                                                                         |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| Wake screen              | After the calibrated phone sleeps, click Wake; HID activity should wake it. It does not unlock the phone.                                     |
| Home                     | Home screen appears.                                                                                                                          |
| Back                     | Open a harmless Settings subpage first; Back returns to the previous page.                                                                    |
| Camera preview click     | Click a visible Settings item inside the calibration outline; that item opens. The backend sends normalized coordinates, never camera pixels. |
| Swipe up/down/left/right | Open suitable scrollable content and check movement in each direction.                                                                        |
| Laptop typing            | Click a harmless Android text field through the preview. With Keyboard control active, type `hello123`; verify the phone receives it.         |
| Laptop keys              | Press Backspace and Enter; verify deletion and submission. Tab, arrows, Delete, Home, End, PageUp and PageDown are also supported.            |
| Escape                   | Keyboard control becomes inactive; subsequent typing sends nothing. Click a dashboard form and verify normal local typing.                    |

Keyboard forwarding requires intentional focus on the camera surface and enabled
manual ownership. Click the preview or focus it with keyboard navigation to arm;
Escape, an outside click, form focus, window blur, or loss of manual ownership
releases capture. After Take Over, focus the preview again. Cmd/Ctrl/Alt shortcuts,
composition, and repeated keydown events stay local. Shift+Tab leaves the surface.
Printable ASCII uses the existing `/api/key` text path; named keys use that same
endpoint and the bridge's `key` method. No visible Send Text/Send Key panel remains.
The existing single-command gate ignores additional input while a request runs;
type individual keys at a pace the connection can handle. No key queue or retry
is introduced.

Pi action endpoints may acknowledge input with any successful HTTP `2xx` status.
The current asynchronous Pi service normally returns `202 Accepted` with
`accepted: true`, `status: "queued"`, and a `command_id`. The laptop client keeps
the most recent command ID and queue status for diagnostics, then returns without
polling for completion. Legacy synchronous `200` responses remain supported.

Recent Apps and Screen Off are disabled in Pi mode because
the supplied API has no corresponding endpoints.

Input failures display a Pi error and disable manual control. Check the phone
before enabling control again: a timeout can occur after input was delivered.
There are no automatic physical-input retries or ADB fallbacks. Typed text is
not echoed in responses or errors.

## Incremental scope and tests

Manual input and workflow playback are independently configured. Use
`PHONE_MANUAL_INPUT_BACKEND=appium` for legacy USB manual controls, or
`PHONE_INPUT_BACKEND=raspberry_pi` for Pi workflow input playback. Screen selection
remains `appium` or `mock`; it is not migrated to the camera. See
[workflow playback configuration](WORKFLOWS.md#pi-workflow-playback).
Existing global mock mode stays hardware-free unless Pi manual mode is explicitly
selected.

Pi manual input and Pi workflow playback share an endpoint-based lock identity,
including Take Over. Legacy automation uses Android serial-based locks, so
Take Over does not coordinate across these two transports.
Do not run both against the same phone concurrently. Within each existing lock
identity, ownership, cancellation, and takeover behavior are preserved.

Dormant Android control code remains for legacy features, but does not block Pi
dashboard startup or discover Android devices in the Pi manual path.

```zsh
python -m unittest discover -s tests -p 'test_pi_manual.py' -v
python -m unittest discover -s tests -p 'test_automation.py' -q
python -m unittest discover -s tests -p 'test_takeover.py' -q
node --test tests/test_keyboard.js
```

Validation passed: 19 focused Pi tests, 24 automation tests, 15 takeover tests,
and 10 keyboard tests. HTTP/input tests are mocked and require no Pi. Live checks
were read-only health/status checks; physical phone actions remain to be verified
using the table above.
