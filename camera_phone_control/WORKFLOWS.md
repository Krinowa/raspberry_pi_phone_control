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
6. To record without hardware, choose **Record New Flow → Start Recording**,
   then click, drag and type using the small mock phone preview. Stop to review.

Mock text logs include only character counts. Typed values are not retained in
mock command history, status or logs. Run history is in memory, bounded to 100 runs
and 500 log entries per run; restarting Flask clears it. Saved flows persist.

## Record first, configure after

1. Start the current backend, connect and authorize the USB phone, and unlock it
   manually. Open an ordinary screen such as Settings search. Calibrate the lit
   display using the existing four-corner controls.
2. Open **Automations → Record New Flow → Start Recording**. Recording enables the
   current backend's manual control through its existing ownership/frame checks.
   You do not have to choose action types or variables first.
3. Click or drag inside the live phone preview; type and use Enter, Backspace or
   Tab after focusing it. Normal navigation buttons such as Home and Back also
   become recorded actions. In mock mode, use the small phone preview without a
   camera or phone. These actions execute once as they are recorded.
4. Active recording shows **● Recording**, **Actions recorded**, **Undo Last** and
   **Stop Recording**. Configuration and technical details stay hidden. Undo
   removes the recorded step; it does not reverse the physical phone's action.
5. Press **Stop Recording**. Pending typed text is flushed before stopping. The
   **Review Flow** timeline opens and manual keyboard forwarding is disabled.
6. Rename the flow and individual steps. Tap labels use accessibility text when
   available; otherwise they display “Tap — Step 1”. Swipes show their direction.
   Coordinates, durations and IDs are only in **Advanced / Debug**.
7. Recorded text appears as a single input step. Choose **Keep Fixed**, **Make
   Variable**, or **Manual**. The variable editor supplies Name, Label, Required,
   optional Default Value and **Ask When Running**. Conversion to a variable
   removes the captured value; converting back to fixed requires entering the
   desired fixed value (or using the variable default). Manual conversion removes
   the value and replaces the step with a checkpoint.
8. Use Delete, Move Up/Down, or insert a wait/manual checkpoint at the chosen
   timeline position. These editing actions are only available after Stop. A
   financial preparation flow must end in a manual authorization checkpoint.
9. **Save Flow**, select **Run…**, fill the generated variable form, and **Start
   Run**. “Ask When Running” defaults to on for old and new definitions. When off,
   the variable uses its default; a required hidden variable needs a default.
10. At a manual checkpoint, automation releases ownership. Complete the action on
    the phone or enable normal Manual Control, then press Resume. Resume is tied
    to the displayed checkpoint and reacquires the same device lock and health
    checks. Take Over permanently cancels a running workflow; it does not resume it.

Typing is buffered briefly in the browser, then sent in order with gestures/keys.
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

### Input privacy and target previews

Before capturing physical-backend input, the recorder inspects the current UI
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
`automation/backends.py`. To add Raspberry Pi or ESP32-S3 later, implement the
`InputBridge` contract, then register its construction/discovery/health handling
in that module and allow its configuration name. Provide a ScreenProvider for
camera/vision independently. The recorder, JSON, variable forms, generic runner
and workflow actions remain unchanged. No Pi/ESP32 protocol has been selected.
`screen/camera.py` is only a reserved module; requesting `camera`, `raspberry_pi`
or `esp32_s3` now produces an explicit configuration error.

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
    {"name": "search_text", "label": "Search Text", "type": "text", "required": true, "default": null}
  ],
  "steps": [
    {"id": "step_001", "type": "tap", "x": 0.51, "y": 0.73},
    {"id": "step_002", "type": "input", "source": "variable", "variable": "search_text"},
    {"id": "step_003", "type": "swipe", "start": [0.5, 0.8], "end": [0.5, 0.2], "duration_ms": 350},
    {"id": "step_004", "type": "wait", "seconds": 1.0},
    {"id": "step_005", "type": "manual_checkpoint", "message": "Inspect the result, then Resume."}
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

| Method and path | Purpose |
| --- | --- |
| GET /api/workflows | List summaries and backend configuration |
| POST /api/workflows | Create a validated workflow JSON document |
| GET /api/workflows/ID | Read full definition |
| PUT /api/workflows/ID | Update definition with matching ID |
| DELETE /api/workflows/ID | Delete saved definition |
| POST /api/workflows/ID/run | Start with `{"variables":{"search_text":"hello"}}` |
| GET /api/workflow-runs/RUN_ID | Status, step counters, safe preview and logs |
| POST /api/workflow-runs/RUN_ID/cancel | Stop, with `{}` |
| POST /api/workflow-runs/RUN_ID/resume | Resume the displayed checkpoint, with `{"step_index":1}` |
| GET /api/recorder | Current browser-owned draft |
| POST /api/recorder/start | Name, execute flag and financial flag |
| POST /api/recorder/step | Capture performed actions using `capture:true`, with optional camera points; legacy definition-only requests remain supported |
| POST /api/recorder/review | After Stop: rename_flow, rename_step, delete, move, input conversion, or insert |
| POST /api/recorder/undo, /stop, /save, /cancel | Draft lifecycle, with `{}` |

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
