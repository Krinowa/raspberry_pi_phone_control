"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const source =
  readFileSync(join(__dirname, "../static/phone_gestures.js"), "utf8") +
  "\n" +
  readFileSync(join(__dirname, "../static/app.js"), "utf8");

// Exercise the dashboard script with no browser/network dependencies.
function dashboard() {
  const nodes = new Map(),
    events = {},
    requests = [],
    intervals = [],
    timeouts = [],
    windowEvents = [],
    statusRequests = [];
  let createdNodes = 0;

  let fail = false,
    statusResolve,
    statusSnapshot,
    frameResolve;
  let controlResult = { enabled: true, pending: false },
    heartbeatResolve;
  let delayedHeartbeat = false;
  let delayedActionResolve = null;
  const networkFailures = new Map();

  const node = (id) => {
    if (!nodes.has(id))
      nodes.set(id, {
        textContent: "",
        children: [],
        dataset: {},
        style: {},
        classList: { toggle() {}, add() {}, remove() {} },
        listeners: {},
        addEventListener(name, fn) {
          this.listeners[name] = fn;
        },
        append(...children) {
          this.children.push(...children);
        },
        replaceChildren(...children) {
          this.children = children;
        },
        setAttribute() {},
        getContext: () => ({
          drawImage() {}, save() {}, restore() {}, beginPath() {},
          moveTo() {}, lineTo() {}, closePath() {}, fill() {}, stroke() {},
          arc() {}, fillText() {},
        }),
        focus() {
          context.document.activeElement = this;
          this.listeners.focus?.();
        },
        blur() {
          context.document.activeElement = null;
          this.listeners.blur?.();
        },
        getBoundingClientRect: () => ({
          left: 0,
          top: 0,
          width: 100,
          height: 100,
        }),
        setPointerCapture() {},
      });
    return nodes.get(id);
  };

  const context = vm.createContext({
    document: {
      body: { dataset: {} },
      hidden: false,
      hasFocus: () => true,
      getElementById: node,
      querySelector: () => ({ content: "test-token" }),
      createElement: (tag) => node(`created-${tag}-${++createdNodes}`),
      querySelectorAll: (selector) =>
        selector === "[data-action]"
          ? ["home", "back", "swipe_up", "screen_on"].map((action) => {
              const button = node("action-" + action);
              button.dataset = { action };
              return button;
            })
          : [],
      addEventListener: (name, fn) => {
        events[name] = fn;
      },
    },
    window: {
      addEventListener(name, fn) {
        windowEvents.push({ name, fn });
      },
    },
    crypto: { randomUUID: () => "browser-test-123" },
    performance: { now: () => 1000 },
    AbortSignal: { timeout() {} },
    setTimeout(fn, ms) {
      const timer = { fn, ms, cancelled: false };
      timeouts.push(timer);
      return timer;
    },
    clearTimeout(timer) {
      if (timer) timer.cancelled = true;
    },
    clearInterval() {},
    setInterval(fn, ms) {
      intervals.push(fn);
      fn.intervalMs = ms;
      return intervals.length;
    },
    fetch: (path, options) => {
      const failures = networkFailures.get(path) || 0;
      if (failures) {
        networkFailures.set(path, failures - 1);
        if (path !== "/api/status" && path !== "/api/pi/status")
          requests.push({
            path,
            body: options?.body ? JSON.parse(options.body) : undefined,
            headers: options?.headers,
          });
        return Promise.reject(new TypeError("Failed to fetch"));
      }
      if (path === "/api/pi/status") {
        statusRequests.push(path);
        return Promise.resolve({
          ok: true,
          json: async () => ({ api: "online", phone: "connected" }),
        });
      }
      if (path === "/api/status") {
        statusRequests.push(path);
        if (statusSnapshot)
          return Promise.resolve({
            ok: true,
            json: async () => statusSnapshot,
          });
        return new Promise((resolve) => {
          statusResolve = resolve;
        });
      }
      if (path === "/api/devices")
        return Promise.resolve({
          ok: true,
          json: async () => ({
            ok: true,
            default_device: "phone_A",
            selected_device: "phone_A",
            devices: [
              {
                id: "phone_A",
                name: "Phone A",
                connected: true,
                bluetooth_address: "AA:BB:CC:DD:EE:FF",
                default: true,
              },
            ],
            enrollment: { active: false },
          }),
        });
      if (path === "/api/frame")
        return new Promise((resolve) => {
          frameResolve = resolve;
        });
      requests.push({
        path,
        body: JSON.parse(options.body),
        headers: options.headers,
      });
      if (path === "/api/action" && delayedActionResolve !== false)
        if (delayedActionResolve === true)
          return new Promise((resolve) => {
            delayedActionResolve = () =>
              resolve({ ok: true, json: async () => ({ sent: true }) });
          });
      if (path === "/api/heartbeat" && delayedHeartbeat)
        return new Promise((resolve) => {
          heartbeatResolve = () =>
            resolve({ ok: true, json: async () => controlResult });
        });
      if (
        path === "/api/control" ||
        path === "/api/heartbeat" ||
        path === "/api/takeover"
      ) {
        return Promise.resolve({
          ok: !controlResult.error,
          json: async () => controlResult,
        });
      }
      const rejected = fail && (path === "/api/key" || path === "/api/action");
      return Promise.resolve({
        ok: !rejected,
        json: async () =>
          rejected
            ? { ok: false, error: "device_busy", owner: "automation" }
            : { sent: true },
      });
    },
    createImageBitmap: async () => ({ width: 1280, height: 720, close() {} }),
  });
  vm.runInContext(source, context);
  const run = (code) => vm.runInContext(code, context);
  run(`state = { owned: true, enabled: true, calibrated: true,
    device: { serial: 'usb-test', width: 1080, height: 2400, rotation: 0 },
    camera: { ready: true }, corners: [] };
    manual = true; frameId = 123; frameAt = 1000; render();`);
  const arm = () => node("video").focus();
  arm();
  const key = (key, overrides = {}) => {
    const event = {
      key,
      target: {},
      defaultPrevented: false,
      getModifierState: () => false,
      preventDefault() {
        this.defaultPrevented = true;
      },
      ...overrides,
    };
    events.keydown(event);
    return event;
  };
  return {
    key,
    arm,
    run,
    node,
    events,
    requests,
    intervals,
    timeouts,
    windowEvents,
    statusRequests,
    fail: () => {
      fail = true;
    },
    finishStatus: (value) => {
      statusSnapshot = value;
      statusResolve({ ok: true, json: async () => value });
    },
    result: (value) => {
      controlResult = value;
    },
    delayHeartbeat: () => {
      delayedHeartbeat = true;
    },
    failNetwork: (path, count = 1) => networkFailures.set(path, count),
    delayAction: () => {
      delayedActionResolve = true;
    },
    finishAction: () => delayedActionResolve(),
    finishFrame: (frame = {}) => {
      const resolve = frameResolve;
      frameResolve = null;
      resolve({
        ok: frame.ok !== false,
        headers: { get: () => String(frame.id ?? 123) },
        blob: async () => ({}),
        json: async () => ({ error: frame.error || "Camera unavailable" }),
      });
    },
    runNextTimeout: async () => {
      const candidates = timeouts.filter((item) => !item.cancelled);
      const timeout = candidates.sort((a, b) => a.ms - b.ms)[0];
      timeouts.splice(timeouts.indexOf(timeout), 1);
      await timeout.fn();
      await settle();
    },
    finishHeartbeat: () => heartbeatResolve(),
  };
}
const settle = async () => {
  await new Promise(setImmediate);
  await new Promise(setImmediate);
};

test("successful stale preview frames keep the normal polling cadence", async () => {
  const d = dashboard();
  d.finishFrame({ id: 123 });
  await settle();
  const timer = d.timeouts.find((candidate) => !candidate.cancelled);
  assert.equal(timer.ms, 83);
});

test("preview request errors alone use the slower retry cadence", async () => {
  const d = dashboard();
  d.finishFrame({ ok: false, error: "Camera unavailable" });
  await settle();
  const timer = d.timeouts.find((candidate) => !candidate.cancelled);
  assert.equal(timer.ms, 600);
});

test("preview scheduling recovers immediately when a tab becomes visible", async () => {
  const d = dashboard();
  d.finishFrame({ id: 124 });
  await settle();
  d.run("document.hidden = true");
  d.events.visibilitychange();
  let timer = d.timeouts.findLast((candidate) => !candidate.cancelled);
  assert.equal(timer.ms, 1000);
  d.run("document.hidden = false");
  d.events.visibilitychange();
  timer = d.timeouts.findLast((candidate) => !candidate.cancelled);
  assert.equal(timer.ms, 0);
});

test("printable ASCII and named keys reach the existing keyboard API without echo", async () => {
  const d = dashboard();
  assert.equal(d.node("keyboard-state").textContent, "Keyboard control active");
  for (let code = 32; code < 127; code++) {
    const value = String.fromCharCode(code);
    assert.equal(d.key(value).defaultPrevented, true);
    await settle();
    assert.deepEqual(d.requests.at(-1).body, {
      type: "text",
      value,
      frame_id: 123,
    });
    assert.equal(
      d.requests.at(-1).headers["X-Control-Client"],
      "browser-test-123",
    );
    assert.equal(d.node("notice").textContent, "");
  }
  for (const [key, value] of Object.entries({
    Enter: "enter",
    Backspace: "backspace",
    Tab: "tab",
    ArrowLeft: "left",
    ArrowRight: "right",
    ArrowUp: "up",
    ArrowDown: "down",
    Delete: "delete",
    Home: "home",
    End: "end",
    PageUp: "pageup",
    PageDown: "pagedown",
  })) {
    assert.equal(d.key(key).defaultPrevented, true);
    await settle();
    assert.deepEqual(d.requests.at(-1).body, {
      type: "key",
      value,
      frame_id: 123,
    });
  }
});

test("shortcuts, system keys, local editing and composition remain in the browser", () => {
  const d = dashboard();
  for (const modifier of ["ctrlKey", "metaKey", "altKey"]) {
    for (const key of [
      "r",
      "l",
      "t",
      "w",
      "c",
      "v",
      "x",
      "a",
      "Enter",
      "Tab",
      "Backspace",
    ]) {
      assert.equal(d.key(key, { [modifier]: true }).defaultPrevented, false);
    }
  }
  for (const key of ["F1", "F5", "F12", "Dead", "é", "😀"]) {
    assert.equal(d.key(key).defaultPrevented, false);
  }
  for (const extra of [
    { isComposing: true },
    { keyCode: 229 },
    { getModifierState: () => true },
    { target: { isContentEditable: true } },
    { target: { closest: () => ({}) } },
  ]) {
    assert.equal(d.key("a", extra).defaultPrevented, false);
  }
  assert.equal(d.key("Tab", { shiftKey: true }).defaultPrevented, false);
  const button = {
    closest: (selector) => (selector.includes("button") ? {} : null),
  };
  assert.equal(d.key("Enter", { target: button }).defaultPrevented, false);
  assert.equal(d.key(" ", { target: button }).defaultPrevented, false);
  assert.equal(d.requests.length, 0);
});

test("manual ownership, health, frame, calibration, visibility, and busy gates still apply", () => {
  for (const change of [
    "manual = false",
    "state.owned = false",
    "state.enabled = false",
    "state.device = null",
    "state.camera.ready = false",
    "state.calibrated = false",
    "calibrating = true",
    "frameId = null",
    "frameAt = -2000",
    "document.hidden = true",
    "busy = true",
  ]) {
    const d = dashboard();
    d.run(change + "; render()");
    assert.equal(d.key("a").defaultPrevented, false, change);
    assert.equal(d.requests.length, 0);
  }
});

test("automation busy response disables manual input without retrying or queueing", async () => {
  for (const action of ["keyboard", "home"]) {
    const d = dashboard();
    d.fail();
    if (action === "keyboard") {
      d.key("a");
      assert.equal(d.key("b").defaultPrevented, false);
    } else await d.run('sendAction("home")');
    await settle();
    assert.equal(d.run("manual"), false);
    assert.equal(d.node("notice").textContent, "device_busy");
    assert.equal(
      d.node("keyboard-state").textContent,
      "Keyboard control inactive",
    );
    assert.deepEqual(
      d.requests
        .filter((request) => request.path !== "/api/diagnostics/frontend")
        .map((r) => r.path),
      [action === "keyboard" ? "/api/key" : "/api/action", "/api/control"],
    );
    assert.equal(d.key("c").defaultPrevented, false);
  }
});

test("explicit Take Over keeps heartbeats running until automation releases the phone", async () => {
  const d = dashboard();
  d.run(
    'manual = false; state.owned = false; state.enabled = false; state.control_status = "automation"; render()',
  );
  assert.equal(d.node("mode").textContent, "Automation running");
  d.result({ enabled: false, pending: true });
  assert.equal(d.node("enable").disabled, true);
  assert.equal(d.node("takeover").hidden, false);
  await d.node("takeover").listeners.click();
  assert.equal(d.requests.at(-1).path, "/api/takeover");
  assert.equal(d.run("manual"), true); // Request intent; input remains disabled.
  assert.equal(d.node("mode").textContent, "Takeover requested");
  assert.equal(
    d.node("keyboard-state").textContent,
    "Keyboard control inactive",
  );
  assert.equal(d.key("a").defaultPrevented, false);
  await d.intervals[0]();
  assert.equal(d.requests.at(-1).path, "/api/heartbeat");
  d.result({ enabled: true, pending: false });
  await d.intervals[0]();
  assert.equal(d.node("mode").textContent, "Control active");
  assert.equal(
    d.node("keyboard-state").textContent,
    "Keyboard control inactive",
  );
  d.arm();
  assert.equal(d.node("keyboard-state").textContent, "Keyboard control active");
  assert.equal(d.key("b").defaultPrevented, true);
  await settle();
  assert.equal(d.requests.filter((r) => r.path === "/api/key").length, 1);
  assert.equal(d.requests.at(-1).body.value, "b");
});

test("hiding a pending tab cancels takeover; late heartbeat cannot reactivate input", async () => {
  const d = dashboard();
  d.run("applyControlResult({ enabled: false, pending: true })");
  d.delayHeartbeat();
  const heartbeat = d.intervals[0]();
  d.run("document.hidden = true");
  d.events.visibilitychange();
  await settle();
  d.result({ enabled: true, pending: false });
  d.finishHeartbeat();
  await heartbeat;
  assert.equal(d.run("manual"), false);
  assert.equal(d.key("a").defaultPrevented, false);
  assert.ok(
    d.requests.some(
      (r) => r.path === "/api/control" && r.body.enabled === false,
    ),
  );
});

test("late heartbeat cannot rearm control after uncertain input failure", async () => {
  const d = dashboard();
  d.delayHeartbeat();
  const heartbeat = d.intervals[0]();
  d.fail();
  d.key("a");
  await settle();
  assert.equal(d.run("manual"), false);
  d.finishHeartbeat();
  await heartbeat;
  assert.equal(d.run("manual"), false);
  assert.equal(d.key("b").defaultPrevented, false);
});

test("automation disables normal controls and only the explicit button requests takeover", async () => {
  const d = dashboard();
  // Even a stale local manual flag cannot bypass the observed automation owner.
  d.run('state.control_status = "automation"; render()');
  assert.equal(d.node("mode").textContent, "Automation running");
  assert.match(d.node("control-help").textContent, /controls are unavailable/);
  assert.equal(d.node("enable").disabled, true);
  assert.equal(d.node("takeover").disabled, false);
  assert.equal(d.node("takeover").hidden, false);
  for (const action of ["home", "back", "swipe_up", "screen_on"]) {
    assert.equal(d.node("action-" + action).disabled, true);
    d.node("action-" + action).listeners.click();
  }
  for (const key of ["a", "Enter", "Backspace", "Tab"]) {
    assert.equal(d.key(key).defaultPrevented, false);
  }
  d.node("video").listeners.click({ clientX: 50, clientY: 50 });
  await settle();
  assert.equal(d.requests.length, 0);
  // Cancellation can be requested even if normal-input calibration is unavailable.
  d.run("state.calibrated = false; state.camera.ready = false; render()");
  assert.equal(d.node("takeover").disabled, false);
  d.result({ enabled: false, pending: true });
  await d.node("takeover").listeners.click();
  assert.deepEqual(
    d.requests.map((r) => r.path),
    ["/api/takeover"],
  );
  assert.deepEqual(d.requests[0].body, {});
});

test("surface focus is required; Escape, outside clicks and form focus release it", async () => {
  const d = dashboard();
  d.run("releaseKeyboard()");
  assert.equal(d.key("a").defaultPrevented, false);
  d.arm();
  assert.equal(d.key("a").defaultPrevented, true);
  await settle();
  assert.equal(d.key("Escape").defaultPrevented, true);
  assert.equal(d.key("b").defaultPrevented, false);
  assert.equal(
    d.node("keyboard-state").textContent,
    "Keyboard control inactive",
  );
  d.arm();
  d.events.pointerdown({ target: {} });
  assert.equal(d.key("c").defaultPrevented, false);
  d.arm();
  d.events.focusin({ target: { tagName: "INPUT" } });
  assert.equal(d.key("d").defaultPrevented, false);
  d.arm();
  assert.equal(d.key("e", { repeat: true }).defaultPrevented, false);
  assert.equal(d.requests.length, 1);
});

test("a preview click still sends one calibrated tap and arms keyboard focus", async () => {
  const d = dashboard();
  d.run("releaseKeyboard()");
  d.node("video").listeners.click({ clientX: 50, clientY: 50 });
  await settle();
  assert.deepEqual(
    d.requests.map((r) => r.path),
    ["/api/action"],
  );
  assert.deepEqual(d.requests[0].body, {
    action: "tap",
    frame_id: 123,
    x: 0.5,
    y: 0.5,
    coordinate_space: "camera_normalized",
  });
  assert.equal(d.node("keyboard-state").textContent, "Keyboard control active");
});

test("preview pointer gestures share the recorder threshold for taps and swipes", async () => {
  const d = dashboard();
  const preview = d.node("video");
  const event = (overrides = {}) => ({
    button: 0,
    isPrimary: true,
    pointerId: 7,
    clientX: 20,
    clientY: 30,
    preventDefault() {},
    ...overrides,
  });

  preview.listeners.pointerdown(event());
  preview.listeners.pointerup(event({ clientX: 25, clientY: 34 }));
  await settle();
  assert.equal(d.requests.at(-1).body.action, "tap");
  assert.deepEqual(
    [d.requests.at(-1).body.x, d.requests.at(-1).body.y],
    [0.25, 0.34],
  );

  preview.listeners.pointerdown(event({ pointerId: 8, clientY: 80 }));
  preview.listeners.pointerup(
    event({ pointerId: 8, clientX: 80, clientY: 20 }),
  );
  await settle();
  assert.deepEqual(d.requests.at(-1).body, {
    action: "swipe",
    start: [0.2, 0.8],
    end: [0.8, 0.2],
    duration_ms: 50,
    frame_id: 123,
    coordinate_space: "camera_normalized",
  });
});

test("failed ready verification keeps recovery open and restores prior manual control", async () => {
  const d = dashboard();
  d.requests.length = 0;
  d.run(`
    manual = false;
    state.enabled = false;
    state.owned = false;
    state.control_status = "free";
    manualAssistanceOpen = true;
    recoveryPanelOpen = true;
    recoveryTargetJobId = "job_failed";
    readyVerificationJobId = "job_failed";
    readyVerificationRestoreManual = true;
    jobCatalog = [{
      job_id: "job_failed",
      status: "FAILED",
      recovery_status: "NEEDS_ATTENTION",
      recovery_workflow_id: "return_ready",
      recovery_run_id: "verify_run",
    }];
    renderOperationalRecovery();
  `);
  await settle();

  assert.equal(d.run("manualAssistanceOpen"), true);
  assert.equal(d.run("recoveryPanelOpen"), true);
  assert.equal(d.run('jobCatalog[0].recovery_status'), "NEEDS_ATTENTION");
  assert.equal(d.run("document.body.dataset.manualAssistance"), "open");
  assert.equal(
    d.node("operate-recovery-result").textContent,
    "Expected starting screen was not detected.",
  );
  assert.deepEqual(
    d.requests.filter((request) => request.path === "/api/control").map(
      (request) => request.body,
    ),
    [{ enabled: true, frame_id: 123 }],
  );
  assert.equal(d.run("manual"), true);
});

test("job retry service sends the selected explicit retry mode", async () => {
  const d = dashboard();
  d.requests.length = 0;

  await d.run('jobService.retry("job_failed", "failed_step")');
  await d.run('jobService.retry("job_failed", "restart")');

  assert.deepEqual(
    d.requests.map(({ path, body }) => ({ path, body })),
    [
      {
        path: "/api/jobs/job_failed/retry",
        body: { mode: "failed_step" },
      },
      {
        path: "/api/jobs/job_failed/retry",
        body: { mode: "restart" },
      },
    ],
  );
});

test("top-level recovery warning summarizes multiple affected devices", () => {
  const d = dashboard();
  d.run(`
    manual = false;
    recoveryTargetJobId = null;
    deviceCatalog = { selected_device: "phone_A" };
    jobCatalog = [
      {
        job_id: "failed_A",
        device_id: "phone_A",
        status: "FAILED",
        recovery_status: "NEEDS_ATTENTION",
      },
      {
        job_id: "failed_B",
        device_id: "phone_B",
        status: "INTERRUPTED",
        recovery_status: "NEEDS_ATTENTION",
      },
    ];
    renderOperationalRecovery();
  `);
  assert.equal(
    d.node("operate-attention-message").textContent,
    "2 devices need attention",
  );
});

test("successful ready verification releases control, closes recovery, and updates the card", async () => {
  const d = dashboard();
  d.requests.length = 0;
  d.run(`
    manual = true;
    state.enabled = true;
    state.owned = true;
    state.control_status = "manual";
    deviceCatalog = {
      selected_device: "phone_A",
      devices: [{ id: "phone_A", name: "Phone A", connected: true }],
      enrollment: { active: false },
    };
    deviceCameraDetails = {
      phone_A: {
        config: { camera_source: 0, rotation: 0, enabled: true },
        status: { ready: true },
      },
    };
    manualAssistanceOpen = true;
    recoveryPanelOpen = true;
    recoveryTargetJobId = "job_ready";
    readyVerificationJobId = "job_ready";
    readyVerificationRestoreManual = true;
    jobCatalog = [{
      job_id: "job_ready",
      device_id: "phone_A",
      status: "FAILED",
      recovery_status: "READY",
      recovery_workflow_id: "return_ready",
      recovered_at: "now",
    }];
    renderOperationalRecovery();
  `);
  await settle();

  assert.equal(d.run("manualAssistanceOpen"), false);
  assert.equal(d.run("recoveryPanelOpen"), false);
  assert.equal(d.run("recoveryTargetJobId"), null);
  assert.equal(d.run("manual"), false);
  assert.equal(d.run("document.body.dataset.manualAssistance"), "closed");
  assert.equal(d.node("operate-recovery").hidden, true);
  assert.deepEqual(
    d.requests.filter((request) => request.path === "/api/control").map(
      (request) => request.body,
    ),
    [{ enabled: false }],
  );
  assert.match(
    d.node("device-list").children[0].className,
    /device-card--ready.*device-card--returned/,
  );
  assert.doesNotMatch(
    d.node("device-list").children[0].className,
    /needs-attention/,
  );
  const deviceActions = d.node("device-list").children[0].children.at(-1);
  const jobActions = deviceActions.children.find(
    (child) => child.className === "device-job-actions",
  );
  assert.deepEqual(
    Array.from(jobActions.children, (button) => button.textContent),
    ["Restart Workflow"],
  );
});

test("Pi status endpoints check once while device metadata polling stays separate", async () => {
  const d = dashboard();
  await settle();
  assert.deepEqual(d.statusRequests, ["/api/pi/status", "/api/status"]);
  d.finishStatus({
    manual_backend: "raspberry_pi",
    pi: { api: "online", phone: "connected" },
    owned: true,
    enabled: true,
    calibrated: true,
    device: { serial: "pi-test" },
    camera: { ready: true },
    corners: [],
  });
  await settle();
  assert.equal(d.node("pi-api-state").textContent, "Online");
  assert.equal(d.node("pi-phone-state").textContent, "Connected");
  assert.deepEqual(
    d.intervals.map((fn) => fn.intervalMs),
    [1500],
  );
  for (let i = 0; i < 5; i++) await d.intervals[0]();
  d.arm();
  d.key("a");
  await settle();
  await d.run('sendAction("home")');
  d.fail();
  d.key("Enter");
  await settle();
  assert.deepEqual(d.statusRequests, ["/api/pi/status", "/api/status"]);
  assert.equal(d.requests.filter((r) => r.path === "/api/key").length, 2);
  assert.equal(d.run("manual"), false);
});

test("Pi runtime status polling refreshes ownership without duplicate timers", async () => {
  const d = dashboard();
  await settle();
  d.finishStatus({
    manual_backend: "raspberry_pi",
    pi: { api: "online", phone: "connected" },
    owned: false,
    pending_owned: false,
    enabled: false,
    calibrated: true,
    device: { serial: "pi-test" },
    camera: { ready: true },
    corners: [],
  });
  await settle();
  let runtime = d.timeouts.find(
    (timer) => !timer.cancelled && timer.ms === 2000,
  );
  assert.ok(runtime);
  runtime.cancelled = true;
  await runtime.fn();
  await settle();
  assert.equal(
    d.statusRequests.filter((path) => path === "/api/status").length,
    2,
  );
  assert.equal(
    d.timeouts.filter((timer) => !timer.cancelled && timer.ms === 2000).length,
    1,
  );

  d.windowEvents.find((event) => event.name === "pagehide").fn();
  assert.equal(
    d.timeouts.filter((timer) => !timer.cancelled && timer.ms === 2000).length,
    0,
  );
});

test("legacy Appium dashboard retains its status timer", async () => {
  const d = dashboard();
  await settle();
  d.finishStatus({
    manual_backend: "appium",
    camera: { ready: true },
    corners: [],
  });
  await settle();
  assert.deepEqual(
    d.intervals.map((fn) => fn.intervalMs),
    [1500, 5000],
  );
  await d.intervals[1]();
  assert.equal(
    d.statusRequests.filter((path) => path === "/api/pi/status").length,
    1,
  );
  d.windowEvents.find((e) => e.name === "pagehide").fn();
  assert.equal(d.run("legacyStatusTimer"), null);
});

test("Pi enable updates cached badges on success or failure without another Pi request", async () => {
  for (const connected of [true, false]) {
    const d = dashboard();
    await settle();
    const snapshot = {
      manual_backend: "raspberry_pi",
      pi: { api: "online", phone: "connected" },
      owned: false,
      enabled: false,
      calibrated: true,
      device: { serial: "pi-test" },
      camera: { ready: true },
      corners: [],
    };
    d.finishStatus(snapshot);
    await settle();
    if (connected) {
      snapshot.owned = true;
      snapshot.enabled = true;
    } else {
      snapshot.pi.phone = "waiting";
      snapshot.device = null;
      snapshot.calibrated = false;
      snapshot.message = "Pi Online / Phone Waiting";
      d.result({ error: snapshot.message });
    }
    await d.node("enable").listeners.click();
    assert.equal(d.run("manual"), connected);
    assert.equal(
      d.node("pi-phone-state").textContent,
      connected ? "Connected" : "Phone Waiting",
    );
    if (!connected)
      assert.equal(d.node("notice").textContent, snapshot.message);
    assert.deepEqual(d.statusRequests, [
      "/api/pi/status",
      "/api/status",
      "/api/status",
    ]);
    assert.deepEqual(
      d.requests
        .filter((request) => request.path !== "/api/diagnostics/frontend")
        .map((r) => r.path),
      ["/api/control"],
    );
  }
});

test("recording observes only acknowledged physical inputs; Escape and shortcuts remain local", async () => {
  const d = dashboard();
  // The recorder observer cannot change the keyboard eligibility or transport.
  d.run(
    "window.phoneRecorder = { capturesPreview: () => false, blocksInput: () => false, accepted: 0, acceptManual() { this.accepted++; } }",
  );
  for (const ch of "hello") {
    d.key(ch);
    await settle();
  }
  d.key("Enter");
  await settle();
  d.key("Backspace");
  await settle();
  assert.equal(d.run("window.phoneRecorder.accepted"), 7);
  for (const modifier of ["ctrlKey", "metaKey", "altKey"])
    d.key("a", { [modifier]: true });
  d.key("Escape");
  d.key("a");
  await settle();
  assert.equal(d.requests.filter((r) => r.path === "/api/key").length, 7);
  assert.equal(d.run("window.phoneRecorder.accepted"), 7);
  d.arm();
  d.fail();
  d.key("Enter");
  await settle();
  assert.equal(d.run("window.phoneRecorder.accepted"), 7);
});

test("transient heartbeat failure reconnects without overlap and refreshes recorder state", async () => {
  const d = dashboard();
  await settle();
  const snapshot = {
    manual_backend: "raspberry_pi",
    pi: { api: "online", phone: "connected" },
    owned: true,
    enabled: true,
    calibrated: true,
    device: { serial: "pi-test" },
    camera: { ready: true },
    corners: [],
  };
  d.finishStatus(snapshot);
  await settle();
  d.run(
    "window.phoneRecorder = { ownerId: 'browser-test-123', refreshed: 0, blocksInput: () => false, capturesPreview: () => false, async refreshAfterReconnect() { this.refreshed++; } }",
  );
  d.failNetwork("/api/heartbeat");
  await Promise.all([d.intervals[0](), d.intervals[0]()]);
  assert.equal(
    d.requests.filter((request) => request.path === "/api/heartbeat").length,
    1,
  );
  assert.equal(d.run("connectionState"), "reconnecting");
  assert.equal(d.node("connection-banner").hidden, false);
  await d.runNextTimeout();
  assert.equal(
    d.run("connectionState"),
    "connected",
    JSON.stringify({
      requests: d.requests.map((request) => request.path),
      statusRequests: d.statusRequests,
      notice: d.node("notice").textContent,
      attempt: d.run("recoveryAttempt"),
    }),
  );
  assert.equal(d.run("window.phoneRecorder.refreshed"), 1);
  assert.equal(d.node("connection-banner").hidden, true);
  assert.equal(d.node("notice").textContent, "Connection restored.");
});

test("timed-out physical action is never retried and duplicate clicks cannot overlap", async () => {
  const timeout = dashboard();
  timeout.failNetwork("/api/action");
  await timeout.run('sendAction("home")');
  await settle();
  assert.equal(
    timeout.requests.filter((request) => request.path === "/api/action").length,
    1,
  );
  assert.match(timeout.node("notice").textContent, /not retried automatically/i);
  assert.equal(timeout.run("connectionState"), "reconnecting");

  const busy = dashboard();
  busy.delayAction();
  const first = busy.run('sendAction("home")');
  const second = busy.run('sendAction("back")');
  await settle();
  assert.equal(
    busy.requests.filter((request) => request.path === "/api/action").length,
    1,
  );
  busy.finishAction();
  await Promise.all([first, second]);
});
