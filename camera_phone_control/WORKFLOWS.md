# Workflows, recorder and backend interfaces

The existing camera dashboard, ADB controls, script registry, cancellation and
cross-process device locks remain in use. `appium_phone_poc` is unchanged.
Here, **appium** is the configuration name for the current **ADB-backed**
implementation; it wraps `AndroidControl` and does not start an Appium server.

## Run the project

Backend and frontend are one Flask application; no npm build or separate frontend
server is needed. From this workspace:

```sh
cd /Users/weizhe/Documents/phone_controller
source appium_phone_poc/venv/bin/activate
python -m pip install -r camera_phone_control/requirements.txt
cd camera_phone_control
PHONE_INPUT_BACKEND=appium PHONE_SCREEN_BACKEND=appium python app.py --camera 0
```

Open **http://127.0.0.1:8765**. Both backend variables default to `appium`. Existing
camera options (`--camera`, `--camera-url`, dimensions, rotation, port) still work.
The server continues to bind to loopback and uses the existing same-origin,
Host, control-token and browser-ownership checks. Do not publish or tunnel it.

Existing scripts still run from the same directory:

```sh
python -m automation.runner --list
python -m automation.runner settings_demo
python -m automation.runner open_mae
```

Scripts and workflows share `DeviceLocks`; neither creates a competing lock
system. The current MAE script is unchanged by this refactor, including its
existing startup handling and lack of a verified landing-page selector.

## Mock playback without a phone

Stop the first server with Ctrl+C, then run:

```sh
PHONE_INPUT_BACKEND=mock PHONE_SCREEN_BACKEND=mock python app.py --camera 0
```

`PHONE_INPUT_BACKEND=mock` alone is also sufficient. Mock input never discovers
or controls a USB phone, and screen methods are explicitly unsupported in mock
input mode even if the configured screen name remains `appium`. The dashboard
may still attempt external-camera capture; a missing camera does not block the
workflow list, normalized mock preview, recorder or playback. No Raspberry Pi,
ESP32, ADB, Appium server or camera is required for mock workflows.

1. In **Automations → Saved Flows**, select **Run…** on the included demo.
2. Enter an ordinary search value and press **Run Selected Flow**.
3. At the checkpoint, press **Resume after manual action**.
4. Watch step status, redacted `MOCK TAP`, `MOCK SWIPE`, `MOCK INPUT`, and `MOCK KEY`
   logs. The normalized phone preview displays the current tap, swipe arrow or
   input label. Fast steps may pass between status polls; all commands remain in
   the run log. This preview is a diagram, not a phone screenshot.
5. Resume the final checkpoint to finish. **Stop Run** cancels a run or a checkpoint.
6. To record without hardware, choose **Start Recording**,
   then click, drag and type using the small mock phone preview. Stop to review.

Mock text logs include only character counts. Typed values are not retained in
mock command history, status or logs. Run history is in memory, bounded to 100 runs
and 500 log entries per run; restarting Flask clears it. Saved flows persist.

## Record first, configure after

1. Connect the phone using your configured manual controls and unlock it
   manually. Open an ordinary screen such as Settings search. Calibrate the lit
   display using the existing four-corner controls.
2. Open **Start Recording**. Recording enables the
   phone's manual control through its existing ownership/frame checks.
   You do not have to choose action types or variables first.
3. Click inside the live phone preview; type and use phone keys after focusing
   it. Use the swipe buttons for Pi recording; legacy and mock recording also support dragging. Normal navigation buttons such as Home and Back also
   become recorded actions. In mock mode, use the small phone preview without a
   camera or phone. These actions execute once as they are recorded.
4. Active recording shows **● Recording • N steps**, a live **Recorded Steps** timeline, **Undo Last** and
   **Stop Recording**. Configuration and technical details stay hidden. Undo
   removes the recorded step; it does not reverse the physical phone's action.
5. Press **Stop Recording**. Pending typed text is flushed before stopping. The
   **Review Recording** timeline opens and manual keyboard forwarding is disabled.
6. Rename the flow and individual steps. Tap labels use accessibility text when
   available; otherwise they display “Tap”. Swipes show their direction.
   Tap coordinates are rounded percentages; full precision remains in the workflow.
7. Recorded text appears as a single input step. Choose **Keep Fixed**, **Make
   Variable**, or **Manual**. The variable editor supplies Name, Label, Required,
   optional Default Value and **Ask When Running**. Conversion to a variable
   removes the captured value; converting back to fixed requires entering the
   desired fixed value (or using the variable default). Manual conversion removes
   the value and replaces the step with a checkpoint.
8. Use Delete, Move Up/Down, or insert a wait/manual checkpoint at the chosen
   timeline position. These editing actions are only available after Stop. A
   financial preparation flow must end in a manual authorization checkpoint.
9. **Save Workflow**, select **Run…**, fill the generated variable form, and **Start
   Run**. “Ask When Running” defaults to on for old and new definitions. When off,
   the variable uses its default; a required hidden variable needs a default.
10. At a manual checkpoint, automation releases ownership. Complete the action on
    the phone or enable normal Manual Control, then press Resume. Resume is tied
    to the displayed checkpoint and reacquires the same device lock and health
    checks. Take Over permanently cancels a running workflow; it does not resume it.

For legacy/mock recording, typing is buffered briefly in the browser, then sent in order with gestures/keys.
Consecutive chunks from the same focused field are combined into one recorded
text step, including a trailing Backspace. Local form editing and browser
shortcuts are not recorded. Supported text remains printable ASCII with the
current ADB backend; pasted unsupported text is rejected. Pending actions are
never retried after a failed or uncertain command. A stop waits for prior captured
input to finish; an ADB command already sent cannot be retracted.

Recording observes actions performed through this dashboard, not arbitrary touches
or typing performed directly on the physical phone. A draft survives refresh in
the same tab but not a server restart. The browser's normal manual-control
identifier and the draft recording identifier remain separate.

### Recording through Pi manual controls

Pi recording observes the existing `/api/action`, `/api/key`, and `/api/text`
requests after ownership, camera freshness, calibration, and device-lock checks.
It prepares a valid candidate before sending input, then commits only after the
Pi acknowledges success. Failed or uncertain input stops recording without
appending a successful step or retrying. The UI preserves the draft for review.
No status checks, screen inspection, or automation transport are added to input.

Printable keys go to the phone immediately, one existing request per key.
Consecutive acknowledged text is grouped in the draft, up to 4096 characters per
step. Every physical tap, swipe, navigation action, or special key breaks the
group. Backspace is an explicit key step, not an edit to previously captured text.
Stop closes the input gate immediately and waits for an already-sent input before
opening review. It does not save or discard automatically.

Version 1 is retained: tap `x/y` and swipe `start/end` contain full-precision
normalized phone coordinates. Text uses `input` with `source: fixed` and `value`;
keys use `key` with uppercase `key`. Navigation uses existing `HOME`, `BACK`, and
`SCREEN_ON` keys. The additive key names `LEFT`, `RIGHT`, `UP`, `DOWN`, `DELETE`,
`MOVE_HOME`, `END`, `PAGEUP`, and `PAGEDOWN` cover the manual keyboard. `MOVE_HOME`
means the text cursor Home key; existing `HOME` still means phone navigation.
Legacy playback accepts these keys through its existing keyboard transport.
No transport names, URLs, request payloads, or camera coordinates enter steps.

Pi recording has no UI hierarchy or secure-field detection. It records text sent
through the manual controls as entered. For private input, use the phone directly
and insert a manual checkpoint during review. Legacy input privacy checks remain
unchanged. Playback uses its independently configured input backend, including
Pi when `PHONE_INPUT_BACKEND=raspberry_pi`.

Review supports step names, fixed/variable/manual text, wait duration, deletion,
and reordering. **Save Workflow** uses the workflow name field; an empty name shows
“Enter a workflow name”. Recorder tests run without hardware:

```sh
python -m unittest discover -s tests -p 'test_pi_recording.py' -v
node --test tests/test_keyboard.js tests/test_workflow_ui.js
```

### Input privacy and target previews

Before capturing legacy Appium input, the recorder inspects the current UI
through `context.screen.inspect()`. Password flags and security-related UI labels
cause a manual checkpoint instead of a replayable tap/key/text action. Text also
requires one focused, non-password editable field with a recognized ordinary
label (such as search, customer name, reference or email). Unknown, unlabelled or
custom fields fall back to a manual checkpoint. The typed value is discarded and
is not sent, logged, shown in review or included in saved JSON. Complete that part
directly on the phone and continue recording on the next screen. Multiple attempts
on the same protected screen do not create a checkpoint for every keystroke.

This is conservative UI metadata classification, not a guarantee that every app
marks secrets correctly. Keep PINs, OTPs and credentials on the physical phone;
custom controls can lack reliable metadata. The mock preview is a synthetic
ordinary input surface and has no real field classification.

When a non-sensitive tap has a usable UI hierarchy, the recorder optionally takes
a pre-action screenshot through the screen provider. All recognized editable
regions are masked before a 96×96 PNG target crop is saved with the step. Secure
or unknown screens are skipped, as are unavailable captures. The review timeline
shows the crop. Full screenshots are not retained. Images are reference material
only; playback still uses normalized coordinates, with no visual matching.

Saved flows retain Run/Delete; review supports editing the current recorded draft.
There is no saved-flow graphical editor in this increment. The JSON update API
remains available. Existing workflows remain readable without the optional new
metadata or `ask_when_running` field.

## Interfaces and ownership

`InputBridge` defines normalized `tap`, `swipe`, relative `move`, pointer `click`,
`key` and `type_text`. `AppiumInputBridge` delegates to existing `AndroidControl`
methods. A small general-coordinate swipe method was added there. Text uses the
existing safely quoted single-character ADB input path; it checks cancellation
and device health between characters. The current physical backend supports
printable ASCII text, Enter, Backspace, Tab, Home, Back, Recents and screen on/off.
It raises `UnsupportedOperation` for relative mouse movement and pointer click.

`ScreenProvider.capture()` returns an image/frame if supported; `inspect()` returns
structured state if supported. The current provider returns PNG screenshot bytes
from `adb exec-out screencap -p`, and the existing UIAutomator XML string from
`AndroidControl.dump_ui`. Screenshot capture is on demand and not written to disk.
The live external-camera feed remains independent. Mock screen operations raise a
clear unsupported-operation exception; they do not return fabricated UI state.

`AutomationContext.input` and `.screen` are guarded facades. Every operation goes
through `_step`, cancellation/takeover and the selected backend's health check.
Existing `send`, `send_key`, `launch_app`, `dump_ui`, `wait`, `device` and `controller`
remain compatible. `send("tap", pixel)` converts legacy pixels to ratios before
calling the bridge; `dump_ui()` delegates to `screen.inspect()`. Existing scripts
need no JSON conversion. As before, direct use of the raw legacy `controller` is
for trusted legacy code only; workflows exclusively use the guarded facades.

The generic `WorkflowRunner(context).run(workflow, variables)` validates the full
document and resolves all required/default variables before issuing input. It
executes steps sequentially using `context.input`, `context.wait` or
`context.manual_checkpoint`, with cancellation checks and start/completion/failure
logs. Unsupported steps fail before playback; there are no automatic input
retries. `Runner.run_task` supplies the same lock lifecycle as `Runner.run` and
supports explicit manual pause/resume. Local API background threads call that
entry point rather than taking an independent lock.

Backend selection, construction, device discovery and health checks live in
`automation/backends.py`. Input supports `appium`, `raspberry_pi`, and `mock`.
Screen selection remains `appium` or `mock`; camera/vision and ESP32 input are not
implemented. The workflow schema and recorder are independent of this selection.

### Pi workflow playback

Start the dashboard with the playback input explicitly selected:

```sh
export PHONE_INPUT_BACKEND=raspberry_pi
export PI_API_BASE_URL=http://192.168.68.61:8766
export PHONE_SCREEN_BACKEND=appium
python app.py --camera 0
```

The advanced panel will show `Input: raspberry_pi · Screen: appium`. Restart the
server after changing its environment. `PHONE_MANUAL_INPUT_BACKEND` independently
selects manual controls; it does not choose workflow playback. Leave
`PHONE_INPUT_BACKEND=appium` to retain legacy playback, or use `mock` for no input.

Saved version-1 workflows go through `WorkflowRunner`, the guarded input contract,
`RaspberryPiWorkflowInput`, the existing `RaspberryPiInputBridge`, and `PiApiClient`.
Pi input discovers no USB device and performs no `/status` or `/health` checks.
Each physical action is sent once; failures stop later steps, preserve useful Pi
errors, and warn when input may have occurred. No retries or status polling occur.

| Saved action                | Pi operation                                    |
| --------------------------- | ----------------------------------------------- |
| `tap` (`x`, `y`)            | `/tap`, unchanged normalized coordinates        |
| `swipe` (`start`, `end`)    | `/swipe`, normalized endpoints                  |
| Fixed or variable `input`   | `/text`, resolved value; empty input is a no-op |
| Keyboard `key`              | `/key`; `MOVE_HOME` maps to keyboard `HOME`     |
| `HOME`, `BACK`, `SCREEN_ON` | `/home`, `/back`, `/wake`                       |
| `wait`, `manual_checkpoint` | Existing local wait and checkpoint behavior     |

The Pi swipe API has no duration field. Saved durations remain intact for legacy
playback, but Pi playback uses the HID server's swipe timing. Recent Apps, Screen
Off, relative motion, pointer click, and application launch are unsupported and
fail without inventing an endpoint or falling back to ADB.

Pi runs use the same `pi-api:<base URL>` lock identity as Pi manual input.
Cancellation, explicit takeover, and checkpoint release/resume retain their
existing semantics. Different transport identities (Pi versus USB) still cannot
coordinate ownership of the same physical phone.

Screen acquisition has not migrated. Appium screenshot/UI inspection still uses
the existing USB screen provider, resolving its USB device only when explicitly
requested. Input-only workflows require no USB even with `PHONE_SCREEN_BACKEND=appium`.
A screen operation still requires USB; no camera or vision fallback is introduced.
Legacy scripts that require pixel geometry or direct Android controller operations
are not automatically made Pi-compatible.

```sh
python -m unittest discover -s tests -p 'test_pi_playback.py' -v
```

## Coordinates and JSON

All persisted tap and swipe coordinates are normalized. Shared
`normalized_to_device(x, y, width, height)` maps inclusive edges to `0..width-1`
and `0..height-1`; `device_to_normalized` provides the inverse. Values must be
finite and in range; booleans are rejected. The existing calibration also uses
this conversion, preserving its nearest-pixel rounding tolerance.

The camera canvas uses its displayed browser rectangle to get camera-relative
ratios. The recorder sends those camera points to Flask, where the existing
four-corner homography converts them into **phone** ratios before saving. This
preserves perspective correction and rejects bezel/outside clicks. It never
mistakes camera-frame ratios for phone ratios. The small mock preview already
represents the full normalized phone display and requires no calibration.

Example version 1 document:

```json
{
  "id": "search_flow",
  "name": "Search Flow",
  "version": 1,
  "financial": false,
  "variables": [
    {
      "name": "search_text",
      "label": "Search Text",
      "type": "text",
      "required": true,
      "default": null
    }
  ],
  "steps": [
    { "id": "step_001", "type": "tap", "x": 0.51, "y": 0.73 },
    {
      "id": "step_002",
      "type": "input",
      "source": "variable",
      "variable": "search_text"
    },
    {
      "id": "step_003",
      "type": "swipe",
      "start": [0.5, 0.8],
      "end": [0.5, 0.2],
      "duration_ms": 350
    },
    { "id": "step_004", "type": "wait", "seconds": 1.0 },
    {
      "id": "step_005",
      "type": "manual_checkpoint",
      "message": "Inspect the result, then Resume."
    }
  ]
}
```

Steps may include optional human-readable `label`, `recorded_at` and PNG-base64
`preview` metadata. These are presentation hints, not backend-specific selectors.
Steps have unique IDs and support `tap`, `swipe`, `input`, `key`, `wait` and
`manual_checkpoint`. Inputs accept `source: fixed` with `value`, or `source:
variable` with a defined variable name. `source: manual` is also accepted without
a value, and behaves as a checkpoint. The recorder and review UI write explicit manual checkpoints.
Variables support text or finite numbers, required flags, defaults and the optional
`ask_when_running` flag (default true). Optional
empty values resolve to an empty string. Values are not substituted into arbitrary
JSON or code: resolution occurs only for declared variable input steps.

Storage validates JSON before writing, uses bounded safe identifiers, private
files, atomic creation/replacement and rejects symlink reads/writes and path
traversal. Files live in `automation/workflow_data/*.json` relative to the package,
not the shell's current working directory. Create/read/update/delete/list are
supported. Create/update add UTC `created_at` and `updated_at` metadata.

The versioned, typed schema can be extended with `target`/`fallback` fields later;
the present validator rejects unsupported visual selectors instead of silently
executing fallback coordinates. No vision, branches, loops, generated variables
or external webhooks are implemented.

## Sensitive input and financial boundaries

Non-sensitive input steps are captured automatically during recording and reviewed before saving. Variables marked
`sensitive: true`, or named/labeled as PIN, OTP, password, passcode, secret,
biometric or approval code, are rejected; manually supplied sensitive fixed steps
are also rejected when marked sensitive. Ordinary browser keystrokes outside recording are never retained; active recording
only captures preview-directed input classified as non-sensitive. Authentication and approval values belong in manual checkpoints and
must be entered directly by the operator, not saved in fixed values/defaults.
No runtime input values are added to logs or status responses; execution values
are cleared from the service's runtime copy after completion.

For a financial preparation flow, set `financial: true` (the recorder checkbox).
Validation requires its **last** step to be a manual checkpoint. The engine does
not supply a transfer/cash-out final-confirm action. Humans must complete final
authorization on the phone at that checkpoint, then resume only to finish the run.

Coordinate taps and arbitrary strings have no app semantics. The engine cannot
recognize that a screen coordinate is a banking confirmation, or that an innocuous
variable name contains a secret. Flow authors must mark those boundaries and omit
such taps/values; keyword checks are not a security classifier. Camera viewing
may show what is on the phone, as before, but frames are not saved by the recorder.

## Local API

All mutations require existing `X-Control-Token` and a valid `X-Control-Client`
header. The token is in the dashboard HTML meta tag; this is a trusted local
application, not a public webhook authentication system.

| Method and path                                | Purpose                                                                                                                       |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| GET /api/workflows                             | List summaries and backend configuration                                                                                      |
| POST /api/workflows                            | Create a validated workflow JSON document                                                                                     |
| GET /api/workflows/ID                          | Read full definition                                                                                                          |
| PUT /api/workflows/ID                          | Update definition with matching ID                                                                                            |
| DELETE /api/workflows/ID                       | Delete saved definition                                                                                                       |
| POST /api/workflows/ID/run                     | Start with `{"variables":{"search_text":"hello"}}`                                                                            |
| GET /api/workflow-runs/RUN_ID                  | Status, step counters, safe preview and logs                                                                                  |
| POST /api/workflow-runs/RUN_ID/cancel          | Stop, with `{}`                                                                                                               |
| POST /api/workflow-runs/RUN_ID/resume          | Resume the displayed checkpoint, with `{"step_index":1}`                                                                      |
| GET /api/recorder                              | Current browser-owned draft                                                                                                   |
| POST /api/recorder/start                       | Name, execute flag and financial flag                                                                                         |
| POST /api/recorder/step                        | Capture performed actions using `capture:true`, with optional camera points; legacy definition-only requests remain supported |
| POST /api/recorder/review                      | After Stop: rename_flow, rename_step, delete, move, input conversion, or insert                                               |
| POST /api/recorder/undo, /stop, /save, /cancel | Draft lifecycle, with `{}`                                                                                                    |

Recorder requests can carry `X-Recorder-Client`, a separate per-tab draft identity
kept in session storage. Live gesture execution still checks the current manual
control client and frame; the recorder identity cannot grant manual ownership.

Run creation returns HTTP 202 with `run_id`, `workflow_id` and status. Poll for
`pending`, `running`, `waiting_manual`, `passed`, `failed` or `cancelled`. A busy
phone produces a failed run with no new input; an already-running local workflow
is rejected. One local workflow runs at a time. Step logs intentionally exclude
input payloads. Run cancellation does not require a camera or active manual lease.

## Validation and hardware TODOs

Run the new tests from `camera_phone_control`, with the virtual environment active:

```sh
python -m unittest discover -s tests -p 'test_workflows.py' -v
python -m unittest discover -s tests -p 'test_record_first.py' -v
node --check static/app.js
node --check static/workflows.js
node --test tests/test_workflow_ui.js
```

Existing local regression files, when present:

```sh
python -m unittest discover -s tests -p 'test_automation.py' -v
python -m unittest discover -s tests -p 'test_takeover.py' -v
node --test tests/test_keyboard.js
```

The shared missing `tests/test_control.py` fake-camera fixture was restored. The
pre-existing `test_open_mae.py` is stale: it imports `button_center`/`click_button`
that are commented out in the current MAE script and assumes the old polling
behavior. Unfiltered test discovery still fails on that file; it has not been
silently skipped or changed as part of the backend/workflow refactor. A new test
checks the current MAE script's context compatibility using simulated detections.

Tests cover coordinate bounds, conversion, variable resolution, invalid and
unsupported workflows, storage, mocked physical backend calls, cancellation
between characters, mock no-hardware guarantees, recorder mapping and execution
gates, and checkpoint/takeover lock lifecycles. Node tests simulate DOM/pointer
input; they do not replace a real browser/touch-device test. No physical phone or
camera commands were executed during these checks.

Remaining hardware work: validate live recording and playback on the actual
phone/camera; check screenshot and keyboard behavior on that Android build;
implement Pi/ESP32 transport plus device health/geometry adapters; implement
camera/vision capture/inspection and eventual visual selectors. No HID hardware
protocol, firmware or computer vision has been implemented here.

## Complete file inventory for this change

Created (paths relative to `camera_phone_control`):

```text
WORKFLOWS.md
automation/backends.py
automation/coordinates.py
automation/input/__init__.py
automation/input/base.py
automation/input/appium.py
automation/input/mock.py
automation/screen/__init__.py
automation/screen/base.py
automation/screen/appium.py
automation/screen/camera.py
automation/workflows/__init__.py
automation/workflows/models.py
automation/workflows/validation.py
automation/workflows/storage.py
automation/workflows/recorder.py
automation/workflows/capture.py
automation/workflows/runner.py
automation/workflows/service.py
automation/workflows/api.py
automation/workflow_data/demo_flow.json
static/workflows.js
tests/test_control.py
tests/test_workflows.py
tests/test_record_first.py
tests/test_workflow_ui.js
```

Modified:

```text
.gitignore
README.md
android_control.py
app.py
automation/context.py
automation/runner.py
calibration.py
static/app.js
static/style.css
templates/index.html
```

Project structure:

```text
camera_phone_control/
├── app.py                       # Existing Flask app + workflow route registration
├── android_control.py           # Existing ADB commands + generic swipe/screenshot
├── device_lock.py               # Unchanged shared lock/takeover protocol
├── calibration.py               # Existing homography + shared pixel conversion
├── camera.py                    # Unchanged external camera feed
├── automation/
│   ├── context.py               # Guarded input/screen and compatibility helpers
│   ├── runner.py                # Existing script runner + generic task/checkpoints
│   ├── backends.py              # Selection, creation, health checks, guard facades
│   ├── coordinates.py
│   ├── input/                   # __init__, base, appium, mock
│   ├── screen/                  # __init__, base, appium, camera placeholder
│   ├── workflows/               # models, validation, storage, recorder, runner,
│   │                            # service, API registration, __init__
│   ├── workflow_data/           # Persisted JSON including demo_flow.json
│   └── scripts/                 # Existing registry, settings_demo.py, open_mae.py
├── templates/index.html         # Existing dashboard + Automations section
├── static/                      # app.js, workflows.js, style.css
├── tests/                       # Workflow/UI tests + existing local tests
├── requirements.txt             # Unchanged
├── README.md
└── WORKFLOWS.md
```

### Wait for Screen to Settle

During review (including editing a saved flow), open **Add Step** to insert
**Wait for Screen to Settle**. Defaults are **Stable for: 700 ms** and
**Maximum wait: 10 sec**. Both values can be edited with the existing step editor;
steps can also be moved or deleted. Recording does not insert these automatically.
Fixed Wait and Manual Checkpoint remain available.

The JSON step uses the existing step ID convention:

```json
{
  "id": "settle_1",
  "type": "wait_until_stable",
  "stable_ms": 700,
  "timeout_seconds": 10
}
```

`stable_ms` accepts finite numbers from 100–10000; `timeout_seconds` accepts 1–300
and must cover the stability window. Allow additional time for sampling and any
screen transition. Playback requires a live external camera and current display
calibration, regardless of the input backend. No Pi command or Appium screenshot
is sent during this step.

The detector reuses the camera capture thread and calibration homography. Every
150 ms it samples a reduced grayscale JPEG, warps only the phone display to
96 × 160, trims the border, and applies a small blur. The mean absolute difference
must stay at or below 3 grayscale levels (out of 255), against both the previous
sample and the stability window's first sample. These are internal helper
parameters for future tuning. Frame IDs and monotonic capture timestamps must
advance; repeated frames cannot complete the window, feeds older than 2 seconds
fail, and gaps over 450 ms restart the stability window. Cancellation and takeover
are checked every sample; timeouts fail without retrying.

This detects visual stillness, not successful loading. Static error pages can
pass, small/local animations may be tolerated, and lighting changes or camera
movement may prevent settling. A camera device that repeats identical images
while delivering fresh IDs/timestamps cannot be distinguished from a still scene.
Keep the camera fixed and recalibrate when its position changes.

### Wait for Text

Review mode can insert an explicit semantic wait:

```json
{
  "id": "text_1",
  "type": "wait_for_text",
  "text": "Transfer",
  "timeout_seconds": 10,
  "roi": [0.0, 0.25, 1.0, 0.5]
}
```

The text is trimmed and must contain 1–500 characters. `timeout_seconds` accepts
finite numbers from 0.5–300. `roi` is optional normalized
`[x, y, width, height]`; camera OCR checks that region first and always falls back
to the full calibrated screen when the region misses. Generic providers poll about
every 200 ms; the camera provider currently uses a 75 ms visual poll. Playback
checks cancellation between samples and never repeats an earlier action. A timeout
fails the run.

When a camera text wait immediately follows an automated input action, playback
captures a low-resolution pre-action signature. It waits for a visible change to
settle before invoking PaddleOCR, while a periodic forced OCR check preserves the
same timeout and target-detection behavior when a change is too small to observe.
Other `ScreenStateProvider` implementations continue using `contains_text()` on
every poll.

Workflow execution depends only on `ScreenStateProvider.contains_text()` from
`automation/screen/state.py`. Providers perform case-insensitive literal substring
matching; fuzzy matching and regular expressions are outside the contract. This
allows an Android accessibility service, camera OCR, or deterministic test provider
to use the same runner path. Until a production provider is configured, the step
fails clearly instead of falling back to a fixed delay.

## Optional ROI-first text waits

Only `wait_for_text` uses ROI. The same Region OCR component is mounted in the
live recording expected-text dialog, the recording review **Insert Wait for
Text** panel, and the saved-step editor. Enter or select the target text, click
**Select OCR Region**, and drag a rectangle on the calibrated snapshot. The
displayed x/y/width/height values update while dragging and can also be entered
numerically. **Test ROI OCR** checks the selected crop of that exact snapshot
against the target text, without sending phone input or running full-screen
fallback. Select again after changing screens; snapshots expire after five
minutes. **Clear OCR Region** removes the optional ROI while retaining the text
wait. Save the check or step, then save the workflow after recording review.

The picker uses the same calibrated/warped, edge-trimmed screen image as OCR,
not the raw camera image. Coordinates describe that image from its top-left.
The editor stores `roi: {"x": ..., "y": ..., "width": ..., "height": ...}`.
Existing `[x, y, width, height]` configuration remains supported and retains its
array representation when validated. Values must be finite numbers in [0, 1],
with positive width/height, and the rectangle must fit inside the screen.
Booleans, strings, NaN, infinity, and out-of-bounds rectangles are rejected;
only floating-point roundoff at the far edge is clamped. Pixel crops round
outwards and always include at least one pixel.

Without ROI, polling and full-screen recognition follow the existing path.
With ROI, each eligible initial/changed-and-settled/forced check tries ROI
first. On a miss, one full-screen fallback runs when either threshold is met:

- `CAMERA_OCR_ROI_FALLBACK_MISSES=3` (positive count, maximum 100).
- `CAMERA_OCR_ROI_FALLBACK_SECONDS=3.0` (0.1–300 seconds).

Both thresholds reset after a fallback, so subsequent checks return to ROI.
The interval starts at wait entry, then at completion of the last fallback.
It is evaluated on eligible OCR checks, not an independent timer; existing
forced checks ensure static screens eventually get a fallback while the wait
is still active. Existing timeout/cancellation, polling/settle intervals,
preprocessing, caches, model, and worker dispatch remain in use. Cache hits
count as OCR attempts but have zero inference time. Every workflow wait gets
fresh fallback state, separate from other waits and devices.

Run snapshots expose `timing.steps[].ocr_calls[]` with `scope` (`roi`/`full`),
normalized `roi` (null for ordinary full-screen calls), ROI attempt/hit/miss
counts, fallback count, inference/queue milliseconds, region count, and the
existing pool/worker/cache metadata. Counts in call records are increments;
step and summary counts are totals. `timing.summary` includes
`total_seconds`, `ocr_wall_seconds`, `roi_ocr_seconds`, `full_ocr_seconds`,
`roi_attempt_count`, `roi_hit_count`, `roi_miss_count`, `full_fallback_count`.
ROI/full OCR times use the existing OCR wall accounting (preprocessing, queue,
worker start/IPC including inference, postprocessing); camera capture/warp
remain separate. Step `region_count` sums detected regions across calls.

### PBe before/after procedure

No ROI coordinates have been guessed or added to existing workflows. Keep UOB
unchanged as the control. Measure only **Welcome** and **Login with PBe** in
**Public bank** initially.

1. Restart the dashboard with the changed code using your normal launch command
   and unchanged camera, model, pool and preprocessing settings. The optional
   environment variables above use their defaults if unset.
2. Leave ROI disabled on both PBe waits. Reset the phone to the workflow's normal
   starting screen, run PBe from the dashboard, and save the completed run JSON.
   In browser DevTools Network, copy the run ID from
   `/api/workflow-runs/<run_id>` (or evaluate
   `localStorage.getItem("phone-workflow-run")` in the dashboard console).
3. Export a completed run with the command below, replacing `BEFORE_RUN_ID`:

   ```sh
   curl --fail --silent --show-error \
     http://127.0.0.1:8765/api/workflow-runs/BEFORE_RUN_ID \
     -o /tmp/pbe-before.json
   ```

4. Manually display the real PBe Welcome screen. In its wait-step editor, refresh
   the calibrated preview, select the measured rectangle, and use **Test ROI
   OCR**. Save the step. Repeat on the actual Login with PBe screen, then save
   the workflow. Do not change UOB or other text waits.
5. Reset to the same starting screen, run PBe, and export its completed run:

   ```sh
   curl --fail --silent --show-error \
     http://127.0.0.1:8765/api/workflow-runs/AFTER_RUN_ID \
     -o /tmp/pbe-after.json
   python3 - /tmp/pbe-before.json /tmp/pbe-after.json <<'PY'
   import json, sys
   fields = ('total_seconds', 'ocr_wall_seconds', 'roi_ocr_seconds',
             'full_ocr_seconds', 'roi_attempt_count', 'roi_hit_count',
             'roi_miss_count', 'full_fallback_count')
   for filename in sys.argv[1:]:
       run = json.load(open(filename))
       print(filename, 'status=' + run['status'])
       for field in fields:
           print(f"  {field}: {run['timing']['summary'][field]}")
   PY
   ```

6. Repeat several paired runs (same device, starting screen, variables, pool
   warm-up and cache conditions). Export before restarting the server because
   run records are in memory. Compare successful runs and per-step timings as
   well as totals; run UOB under the same conditions as the unchanged control.
   To repeat a baseline, disable ROI on those two PBe waits, retaining your
   measured coordinates in an exported workflow copy.

Regression commands from the repository root:

```sh
cd camera_phone_control
PHONE_INPUT_BACKEND=appium PHONE_MANUAL_INPUT_BACKEND=appium PYTHONPATH=.:tests \
  ../appium_phone_poc/venv/bin/python -m unittest \
  test_text_suggestions test_screen_state test_screen_stability \
  test_workflow_timing test_ocr_pool test_camera_ocr_worker test_workflows test_multi_camera
node --test tests/test_workflow_ui.js
```

The backend overrides isolate these tests from the local `.env` Pi settings;
OCR tests use deterministic fake worker responses. Real PBe accuracy and
latency measurements require the measured rectangles and the connected phone.
