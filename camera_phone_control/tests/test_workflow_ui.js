"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source =
  readFileSync(path.join(__dirname, "../static/phone_gestures.js"), "utf8") +
  "\n" +
  readFileSync(path.join(__dirname, "../static/workflows.js"), "utf8");
const templateSource = readFileSync(
  path.join(__dirname, "../templates/index.html"),
  "utf8",
);
const styleSource = readFileSync(
  path.join(__dirname, "../static/style.css"),
  "utf8",
);

function extractFunctionSource(sourceText, pattern, trailingPattern) {
  const match = sourceText.replace(/\r\n?/g, "\n").match(pattern);
  assert.notEqual(match, null, `Expected function source to match ${pattern}`);
  return match[0].replace(trailingPattern, "");
}

function setup(pi = false, options = {}) {
  const nodes = new Map(),
    calls = [],
    events = {},
    timers = new Map();
  let draft = structuredClone(
      options.draft || { active: false, workflow: null },
    ),
    clock = 1000,
    timerId = 0;
  let rejectCapture = false;
  let rejectOCR = false;
  let delayVariableInput = false,
    finishVariableInput;
  let workflowRun = null;
  let savedWorkflow = null;
  function findStep(steps, id) {
    for (const step of steps || []) {
      if (step.id === id) return step;
      const nested =
        findStep(step.then_steps, id) ||
        findStep(step.else_steps, id) ||
        Object.values(step.branches || {}).reduce(
          (found, branch) => found || findStep(branch, id),
          null,
        );
      if (nested) return nested;
    }
    return null;
  }
  function element(id = "") {
    return {
      id,
      value: "",
      checked: false,
      children: [],
      listeners: {},
      classList: { toggle() {}, add() {} },
      addEventListener(name, fn) {
        this.listeners[name] = fn;
      },
      replaceChildren(...children) {
        this.children = children;
      },
      append(...children) {
        this.children.push(...children);
      },
      getBoundingClientRect() {
        return { left: 100, top: 50, width: 200, height: 400 };
      },
      setPointerCapture() {},
      click() { return this.listeners.click?.(); },
      focus() {},
      scrollIntoView() {},
      getContext() {
        return new Proxy({}, { get: () => () => {} });
      },
      querySelector() { return null; },
      querySelectorAll() {
        return [];
      },
    };
  }
  const $ = (id) => {
    if (!nodes.has(id)) nodes.set(id, element(id));
    return nodes.get(id);
  };
  $("flow-run-form").elements = {
    namedItem(name) {
      return $("run-variables").children
        .map((label) => label.children[0])
        .find((input) => input?.name === name);
    },
  };
  const api = async (url, data, method) => {
    calls.push({ url, data: structuredClone(data), method });
    if (url === "/api/workflow-options/transfer-methods")
      return {
        transfer_methods: options.transferMethods || [
          {
            value: "bank_account",
            label: "Bank Account",
            identifier_field: "account_number",
            workflow_variable: "account_number",
          },
          {
            value: "phone_no",
            label: "FPS Phone Number",
            identifier_field: "mobile_no",
            workflow_variable: "fps_phone_number",
          },
          {
            value: "email",
            label: "FPS Email Address",
            identifier_field: "mobile_no",
            workflow_variable: "fps_email",
          },
          {
            value: "fps_id",
            label: "FPS ID",
            identifier_field: "mobile_no",
            workflow_variable: "fps_id",
          },
        ],
      };
    if (url === "/api/ocr/roi-preview") return {token: "snapshot", image: "data:image/png;base64,test", width: 200, height: 400};
    if (url === "/api/ocr/roi-test")
      return { found: true, texts: ["RM 8,230.50"], value: "8230.50" };
    if (url === "/api/recorder/text-suggestions") {
      if (data.current) {
        draft.expected_text_pending = true;
        draft.pending_expected_text_step_id = null;
      }
      if (rejectOCR) throw new Error("OCR unavailable");
      return {
        suggestions: options.ocrSuggestions || ["Pay & Transfer", "New to Maybank?"],
      };
    }
    if (url === "/api/recorder/expected-text") {
      const wait = {
        id: `expected_${calls.length}`,
        type: "wait_for_text",
        text: data.text,
        timeout_seconds: data.timeout_seconds,
      };
      if (data.roi) wait.roi = structuredClone(data.roi);
      const target = draft.pending_expected_text_step_id;
      const index = target
        ? draft.workflow.steps.findIndex((step) => step.id === target) + 1
        : draft.workflow.steps.length;
      draft.workflow.steps.splice(index, 0, wait);
      draft.expected_text_pending = false;
      draft.pending_expected_text_step_id = null;
      return structuredClone(draft);
    }
    if (url === "/api/recorder/expected-text/cancel") {
      draft.expected_text_pending = false;
      draft.pending_expected_text_step_id = null;
      return structuredClone(draft);
    }
    if (url === "/api/recorder/keyboard-capture/start") {
      const keyboardCount = Object.keys(draft.workflow.keyboards).length;
      draft.keyboard_capture = {
        id: keyboardCount ? `keyboard_test_${keyboardCount + 1}` : "keyboard_test",
        name: data.name,
        kind: data.kind || "numeric",
        requested_keys: data.keys,
        keys: {},
      };
      return structuredClone(draft);
    }
    if (url === "/api/recorder/keyboard-capture/key") {
      draft.keyboard_capture.keys[data.key] = {
        x: (data.point || data.camera_point)[0],
        y: (data.point || data.camera_point)[1],
      };
      return structuredClone(draft);
    }
    if (url === "/api/recorder/keyboard-capture/save") {
      const captured = structuredClone(draft.keyboard_capture);
      delete captured.requested_keys;
      draft.workflow.keyboards[captured.id] = captured;
      draft.keyboard_capture = null;
      return structuredClone(draft);
    }
    if (url === "/api/recorder/keyboard-capture/cancel") {
      draft.keyboard_capture = null;
      return structuredClone(draft);
    }
    if (url === "/api/recorder/keyboard-delete") {
      delete draft.workflow.keyboards[data.keyboard_id];
      return structuredClone(draft);
    }
    if (url === "/api/recorder/variable-input") {
      if (delayVariableInput)
        await new Promise((resolve) => {
          finishVariableInput = resolve;
        });
      draft.workflow.variables.push(data.variable);
      const inputStep = {
        id: `step_${draft.workflow.steps.length + 1}`,
        type: "input",
        source: "variable",
        variable: data.variable.name,
        ...(data.variable.name === "amount"
          ? { amount_format: data.amount_format || "raw" }
          : {}),
        ...(data.input_method === "screen_keyboard"
          ? { input_method: "screen_keyboard", keyboard_id: data.keyboard_id }
          : {}),
      };
      const alternate = findStep(
        draft.workflow.steps,
        draft.alternate_screen?.condition_id,
      );
      const destination =
        alternate?.type === "switch_transfer_method"
          ? alternate.branches[draft.alternate_screen.branch]
          : alternate?.then_steps || draft.workflow.steps;
      destination.push(inputStep);
      if (alternate) draft.alternate_screen.step_count = destination.length;
      return structuredClone(draft);
    }
    if (url === "/api/recorder/alternate-screen/start") {
      let condition;
      if (data.source === "withdrawal_api") {
        condition = data.condition_id
          ? findStep(draft.workflow.steps, data.condition_id)
          : null;
        if (!condition) {
          condition = {
            id: `alternate_${calls.length}`,
            type: "switch_transfer_method",
            branches: {},
          };
          draft.workflow.steps.push(condition);
        }
        condition.branches[data.transfer_method] ||= [];
      } else {
        condition = {
          id: `alternate_${calls.length}`,
          type: "if_text",
          text: data.text,
          timeout_seconds: data.timeout_seconds,
          then_steps: [],
          else_steps: [],
          ...(data.roi ? { roi: structuredClone(data.roi) } : {}),
        };
        const afterIndex = data.after_step_id
          ? draft.workflow.steps.findIndex((step) => step.id === data.after_step_id)
          : draft.workflow.steps.length - 1;
        draft.workflow.steps.splice(afterIndex + 1, 0, condition);
      }
      const returnMode = draft.active ? "recording" : "review";
      draft.active = true;
      draft.alternate_screen = {
        condition_id: condition.id,
        source: data.source || "screen_text",
        ...(data.source === "withdrawal_api"
          ? { branch: data.transfer_method }
          : { text: condition.text }),
        step_count: 0,
        return_mode: returnMode,
        after_step_id: data.after_step_id || null,
      };
      return structuredClone(draft);
    }
    if (url === "/api/recorder/alternate-screen/finish") {
      draft.active = draft.alternate_screen?.return_mode === "recording";
      draft.alternate_screen = null;
      return structuredClone(draft);
    }
    if (url === "/api/recorder/alternate-screen/cancel") {
      const condition = findStep(
        draft.workflow.steps,
        draft.alternate_screen?.condition_id,
      );
      if (condition)
        draft.workflow.steps.splice(draft.workflow.steps.indexOf(condition), 1);
      draft.active = draft.alternate_screen?.return_mode === "recording";
      draft.alternate_screen = null;
      return structuredClone(draft);
    }
    if (url === "/api/workflows")
      return {
        workflows: options.workflows || [],
        input_backend: "mock",
        screen_backend: "appium",
        selected_device_id: options.selectedDeviceId || "phone_A",
        allowed_workflow_ids:
          options.allowedWorkflowIds === undefined
            ? null
            : options.allowedWorkflowIds,
        workflow_access_unrestricted:
          options.allowedWorkflowIds === undefined,
      };
    if (url.startsWith("/api/workflows/") && method === "GET")
      return structuredClone(options.workflow || savedWorkflow);
    if (url.endsWith("/run")) {
      workflowRun = structuredClone(options.runStart);
      return structuredClone(workflowRun);
    }
    if (url.startsWith("/api/workflow-runs/") && method === "GET") {
      workflowRun = structuredClone(options.runNext || workflowRun);
      return structuredClone(workflowRun);
    }
    if (url === "/api/recorder/start")
      draft = {
        active: true,
        execute: true,
        expected_text_pending: false,
        pending_expected_text_step_id: null,
        alternate_screen: null,
        workflow: { name: "New Recording", variables: [], keyboards: {}, steps: [] },
      };
    if (url === "/api/recorder/save") {
      savedWorkflow = {
        id: draft.workflow.id || "flow_saved",
        ...structuredClone(draft.workflow),
      };
      return structuredClone(savedWorkflow);
    }
    if (url === "/api/recorder/step") {
      if (rejectCapture) throw new Error("Uncertain input");
      const recordedStep = {
        id: `step_${draft.workflow.steps.length + 1}`,
        ...data.step,
        ...(data.camera_start
          ? data.step.type === "tap"
            ? { x: 0.5, y: 0.5 }
            : { start: data.camera_start, end: data.camera_end }
          : {}),
      };
      const alternate = findStep(
        draft.workflow.steps,
        draft.alternate_screen?.condition_id,
      );
      const destination =
        alternate?.type === "switch_transfer_method"
          ? alternate.branches[draft.alternate_screen.branch]
          : alternate?.then_steps || draft.workflow.steps;
      destination.push(recordedStep);
      if (alternate) draft.alternate_screen.step_count = destination.length;
    }
    if (url === "/api/recorder/stop") {
      draft.active = false;
      draft.expected_text_pending = false;
      draft.pending_expected_text_step_id = null;
      draft.alternate_screen = null;
    }
    if (url === "/api/recorder/undo") {
      const condition = findStep(
        draft.workflow.steps,
        draft.alternate_screen?.condition_id,
      );
      const destination = condition?.then_steps || draft.workflow.steps;
      destination.pop();
      if (condition) draft.alternate_screen.step_count = destination.length;
    }
    if (url === "/api/recorder/review") {
      const step = findStep(draft.workflow.steps, data.step_id);
      if (data.operation === "rename_flow") {
        draft.workflow.name = data.name;
        draft.workflow.purpose = data.purpose;
      }
      if (["expected_text", "remove_expected_text"].includes(data.operation)) {
        const index = draft.workflow.steps.indexOf(step) + 1;
        const wait = draft.workflow.steps[index]?.type === "wait_for_text" ? draft.workflow.steps[index] : null;
        if (data.operation === "remove_expected_text") {
          if (wait) draft.workflow.steps.splice(index, 1);
        } else if (wait) {
          Object.assign(wait, { text: data.text, timeout_seconds: data.timeout_seconds });
          if (data.roi === null) delete wait.roi;
          else if (data.roi) wait.roi = structuredClone(data.roi);
        } else {
          const next = { id: `expected_${calls.length}`, type: "wait_for_text", text: data.text, timeout_seconds: data.timeout_seconds };
          if (data.roi) next.roi = structuredClone(data.roi);
          draft.workflow.steps.splice(index, 0, next);
        }
      }
      if (data.operation === "insert") {
        const parent = findStep(draft.workflow.steps, data.parent_step_id);
        const destination = parent ? parent[data.branch] : draft.workflow.steps;
        destination.splice(data.index, 0, {
          id: `inserted_${calls.length}`,
          ...data.step,
        });
      }
      if (data.operation === "delete") draft.workflow.steps.splice(draft.workflow.steps.indexOf(step), 1);
      if (data.operation === "move") {
        draft.workflow.steps.splice(draft.workflow.steps.indexOf(step), 1);
        draft.workflow.steps.splice(data.index, 0, step);
      }
      if (data.operation === "fields") Object.assign(step, data.fields);
      if (data.operation === "set_critical") {
        if (data.critical) step.critical = true;
        else delete step.critical;
      }
      if (data.operation === "rename_step") step.label = data.label;
      if (data.operation === "input") {
        if (data.source === "variable") {
          delete step.value;
          step.source = "variable";
          step.variable = data.variable.name;
          if (data.variable.name === "amount")
            step.amount_format = data.amount_format || "raw";
          else delete step.amount_format;
          draft.workflow.variables = [data.variable];
        }
        if (data.source === "fixed") {
          delete step.variable;
          step.source = "fixed";
          step.value = data.value;
          draft.workflow.variables = [];
        }
        if (data.source !== "manual" && data.input_method === "screen_keyboard") {
          step.input_method = data.input_method;
          step.keyboard_id = data.keyboard_id;
        } else {
          delete step.input_method;
          delete step.keyboard_id;
        }
      }
    }
    return structuredClone(draft);
  };
  const context = vm.createContext({
    $,
    api,
    clientId: "test-browser-123",
    canvas: $("video"),
    frameId: 123,
    fresh: () => true,
    state: { calibrated: true, manual_backend: pi ? "raspberry_pi" : "appium" },
    applyControlResult() {},
    refreshStatus: async () => {},
    calibrating: false,
    manual: false,
    active: () => true,
    render() {},
    disable: async () => {},
    window: {},
    document: {
      body: { dataset: { dashboardMode: options.dashboardMode || "operate" } },
      hidden: false,
      hasFocus: () => true,
      createElement: element,
      querySelectorAll: () => [],
      addEventListener: (name, fn) => {
        events[name] = fn;
      },
    },
    Image: class { set src(value) { this.onload(); } },
    Option: function (label, value) {
      this.textContent = label;
      this.value = value;
    },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    performance: { now: () => clock },
    requestAnimationFrame(callback) {
      callback();
      return 1;
    },
    setTimeout(fn) {
      timers.set(++timerId, fn);
      return timerId;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    console,
  });
  vm.runInContext(source, context);
  context.window.phoneConnection = {
    busy: false,
    isReconnecting: () => false,
    isPhysicalBusy() { return this.busy; },
    async runPhysical(label, operation) {
      this.busy = true;
      context.window.phoneRecorder.physicalBusyChanged(true, label);
      try {
        return await operation();
      } finally {
        this.busy = false;
        context.window.phoneRecorder.physicalBusyChanged(false, "");
      }
    },
  };
  const fire = async (id, name, event = {}) => {
    // Real disabled controls do not dispatch clicks. Wait for queued recorder
    // work before emulating the operator's next action.
    for (let attempt = 0; $(id).disabled && attempt < 20; attempt++)
      await new Promise((resolve) => setImmediate(resolve));
    await $(id).listeners[name]({
      button: 0,
      isPrimary: true,
      pointerId: 1,
      clientX: 200,
      clientY: 250,
      preventDefault() {},
      ...event,
    });
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
  };
  const key = (key, extra = {}) => {
    const event = {
      key,
      target: {},
      preventDefault() {
        this.defaultPrevented = true;
      },
      getModifierState: () => false,
      ...extra,
    };
    events.keydown(event);
    return event;
  };
  return {
    $,
    calls,
    draft: () => structuredClone(draft),
    context,
    events,
    key,
    fire,
    timers,
    setClock: (value) => {
      clock = value;
    },
    failOCR: () => { rejectOCR = true; },
    delayVariableInput: () => { delayVariableInput = true; },
    finishVariableInput: () => finishVariableInput(),
    fail: () => {
      rejectCapture = true;
    },
  };
}
async function start(ui) {
  await new Promise((resolve) => setImmediate(resolve));
  await ui.fire("record-start", "click");
}

test("workflow polling is idle without a run and cleanup is idempotent", async () => {
  const ui = setup();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.timers.size, 0);
  ui.context.window.phoneRecorder.pausePolling();
  ui.context.window.phoneRecorder.pausePolling();
  ui.context.window.phoneRecorder.resumePolling();
  assert.equal(ui.timers.size, 0);
  ui.context.window.phoneRecorder.shutdown();
  assert.equal(ui.timers.size, 0);
});

test("workflow create and edit UI persists one of the two displayed purposes", async () => {
  const created = setup();
  await start(created);
  await created.fire("record-screenshot-here", "click");
  await created.fire("record-stop", "click");
  assert.equal(created.$("flow-purpose").value, "payment");
  created.$("flow-name").value = "Balance Reader";
  created.$("flow-purpose").value = "balance_check";
  await created.fire("record-save", "click");
  const createPurpose = created.calls.find(
    (call) =>
      call.url === "/api/recorder/review" &&
      call.data.operation === "rename_flow",
  );
  assert.equal(createPurpose.data.purpose, "balance_check");

  const edited = setup(false, {
    draft: {
      active: false,
      editing_existing: true,
      workflow: {
        id: "flow_existing",
        name: "Existing",
        purpose: "balance_check",
        variables: [],
        keyboards: {},
        steps: [{ id: "shot", type: "capture_screen", output_key: "receipt_path" }],
      },
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(edited.$("flow-purpose").value, "balance_check");
  edited.$("flow-purpose").value = "payment";
  await edited.fire("record-save", "click");
  const editPurpose = edited.calls.find(
    (call) =>
      call.url === "/api/recorder/review" &&
      call.data.operation === "rename_flow",
  );
  assert.equal(editPurpose.data.purpose, "payment");
  const purposeSelect = templateSource.match(
    /<select id="flow-purpose">([\s\S]*?)<\/select>/,
  )[1];
  assert.deepEqual(
    [...purposeSelect.matchAll(/value="([^"]+)"/g)].map((match) => match[1]),
    ["payment", "balance_check"],
  );
});

test("Operate lists only workflows assigned to the selected phone", async () => {
  const workflows = [
    { id: "flow_testing", name: "Testing", step_count: 1, variable_count: 0 },
    { id: "flow_maybank", name: "Maybank", step_count: 1, variable_count: 0 },
  ];
  const operate = setup(false, {
    workflows,
    allowedWorkflowIds: ["flow_testing"],
    selectedDeviceId: "phone_A",
    dashboardMode: "operate",
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(operate.$("saved-flows").children.length, 1);
  assert.match(
    operate.$("saved-flows").children[0].children[0].textContent,
    /Testing · Payment/,
  );
  assert.match(operate.$("workflow-backend").textContent, /phone_A: 1 assigned/);

  const admin = setup(false, {
    workflows,
    allowedWorkflowIds: ["flow_testing"],
    selectedDeviceId: "phone_A",
    dashboardMode: "admin",
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(admin.$("saved-flows").children.length, 2);
  assert.equal(admin.$("saved-flows").children[1].children[1].disabled, true);
});

test("active workflow polling updates to terminal state independently", async () => {
  const workflow = {
    id: "flow_1",
    name: "Status flow",
    step_count: 1,
    variable_count: 0,
    variables: [],
  };
  const ui = setup(false, {
    workflows: [workflow],
    workflow,
    runStart: {
      run_id: "run_1",
      status: "running",
      step_index: 1,
      step_count: 1,
      logs: ["Started"],
    },
    runNext: {
      run_id: "run_1",
      status: "passed",
      step_index: 1,
      step_count: 1,
      logs: ["Started", "Passed"],
      message: "Complete",
    },
  });
  let runtimeChanges = 0;
  let readinessRefreshes = 0;
  ui.context.window.phoneRuntimeStateChanged = () => runtimeChanges++;
  ui.context.window.refreshDeviceReadiness = async () => readinessRefreshes++;
  await new Promise((resolve) => setImmediate(resolve));

  await ui.$("saved-flows").children[0].children[1].listeners.click();
  await new Promise((resolve) => setImmediate(resolve));
  await ui.fire("flow-run-form", "submit", { preventDefault() {} });
  assert.match(ui.$("flow-run-state").textContent, /running/);
  assert.equal(ui.timers.size, 1);

  const [timerId, poll] = ui.timers.entries().next().value;
  ui.timers.delete(timerId);
  await poll();
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(ui.$("flow-run-state").textContent, /passed/);
  assert.equal(ui.timers.size, 0);
  assert.equal(runtimeChanges, 2);
  assert.equal(readinessRefreshes, 1);
  assert.equal(
    ui.calls.some((call) => call.url === "/api/devices"),
    false,
  );
});

test("manual run prompts only for unresolved required variables", async () => {
  const workflow = {
    id: "flow_defaults",
    name: "Defaults flow",
    step_count: 1,
    variable_count: 3,
    variables: [
      {
        name: "with_default",
        label: "With default",
        type: "text",
        required: true,
        default: "saved",
        ask_when_running: true,
      },
      {
        name: "legacy_hidden",
        label: "Legacy hidden",
        type: "text",
        required: true,
        default: null,
        ask_when_running: false,
      },
      {
        name: "optional",
        label: "Optional",
        type: "text",
        required: false,
        default: null,
      },
    ],
  };
  const ui = setup(false, {
    workflows: [workflow],
    workflow,
    runStart: {
      run_id: "run_defaults",
      status: "running",
      step_index: 0,
      step_count: 1,
      logs: [],
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  await ui.$("saved-flows").children[0].children[1].listeners.click();
  assert.equal(ui.$("run-variables").children.length, 1);
  const input = ui.$("run-variables").children[0].children[0];
  assert.equal(input.name, "legacy_hidden");
  input.value = "provided";
  await ui.fire("flow-run-form", "submit", { preventDefault() {} });
  const run = ui.calls.find((call) => call.url.endsWith("/run"));
  assert.deepEqual(run.data.variables, { legacy_hidden: "provided" });
});

test("manual transfer-method simulation uses backend registry options", async () => {
  const workflow = {
    id: "flow_transfer_method",
    name: "Transfer method flow",
    step_count: 1,
    variable_count: 2,
    variables: [
      {
        name: "account_number",
        label: "Account Number",
        type: "text",
        required: true,
        default: null,
      },
      {
        name: "fps_phone_number",
        label: "FPS Phone Number",
        type: "text",
        required: true,
        default: null,
      },
    ],
    steps: [
      {
        id: "method_switch",
        type: "switch_transfer_method",
        branches: { bank_account: [], phone_no: [], email: [], fps_id: [], instant_bank: [] },
      },
    ],
  };
  const ui = setup(false, {
    workflows: [workflow],
    workflow,
    transferMethods: [
      {
        value: "bank_account",
        label: "Bank Account",
        identifier_field: "account_number",
      },
      {
        value: "phone_no",
        label: "FPS Phone Number",
        identifier_field: "mobile_no",
        workflow_variable: "fps_phone_number",
      },
      { value: "email", label: "FPS Email Address", identifier_field: "mobile_no", workflow_variable: "fps_email" },
      { value: "fps_id", label: "FPS ID", identifier_field: "mobile_no", workflow_variable: "fps_id" },
      {
        value: "instant_bank",
        label: "Instant Bank",
        identifier_field: "account_number",
        workflow_variable: "account_number",
      },
    ],
    runStart: {
      run_id: "run_transfer_method",
      status: "running",
      step_index: 0,
      step_count: 1,
      logs: [],
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  await ui.$("saved-flows").children[0].children[1].listeners.click();
  assert.equal(ui.$("run-transfer-method-simulation").hidden, false);
  assert.deepEqual(
    ui.$("run-transfer-method").children.map((option) => option.value),
    ["bank_account", "phone_no", "email", "fps_id", "instant_bank"],
  );
  ui.$("run-transfer-method").value = "instant_bank";
  await ui.fire("run-transfer-method", "change");
  assert.equal(ui.$("run-variables").children.length, 1);
  const account = ui.$("run-variables").children[0].children[0];
  assert.equal(account.name, "account_number");
  account.value = "0012345678";
  await ui.fire("flow-run-form", "submit", { preventDefault() {} });
  const run = ui.calls.find((call) => call.url.endsWith("/run"));
  assert.deepEqual(run.data.simulation, { transfer_method: "instant_bank" });
  assert.deepEqual(run.data.variables, { account_number: "0012345678" });
});

test("active recording hides configuration and records normalized mock gestures", async () => {
  const ui = setup();
  await start(ui);
  assert.equal(ui.$("review-flow").hidden, true);
  assert.equal(ui.$("saved-flow-panel").hidden, true);
  await ui.fire("workflow-preview", "pointerdown", {
    clientX: 150,
    clientY: 350,
  });
  await ui.fire("workflow-preview", "pointerup", {
    clientX: 150,
    clientY: 350,
  });
  const call = ui.calls.find((x) => x.url === "/api/recorder/step");
  assert.deepEqual(call.data.step, { type: "tap", x: 0.25, y: 0.75 });
  assert.equal(call.data.capture, true);
  assert.equal(ui.$("record-status").textContent, "● Recording • 1 step");
  await ui.fire("record-stop", "click");
  assert.equal(ui.$("review-flow").hidden, false);
  assert.equal(ui.$("record-live").hidden, true);
  const title = ui.$("record-steps").children[0].children[0].textContent;
  assert.equal(title, "Tap");
  assert.equal(title.includes("25%"), false);
});

test("Read Balance reuses the calibrated ROI picker and preserves currency output fields", async () => {
  const ui = setup();
  await ui.fire("record-new", "click");
  await ui.fire("record-start", "click");
  await ui.fire("record-stop", "click");
  ui.$("record-value-name").value = "Balance";
  ui.$("record-value-output").value = "payer_balance";
  ui.$("record-value-roi-enabled").checked = true;
  for (const [key, value] of Object.entries({x: .1, y: .2, width: .7, height: .1}))
    ui.$(`record-value-roi-${key}`).value = String(value);
  await ui.fire("record-value", "click");
  const inserted = ui.calls.find(c => c.data?.operation === "insert" && c.data.step.type === "ocr_value");
  assert.deepEqual(inserted.data.step, {
    type: "ocr_value", label: "Balance", parser: "currency", output: "payer_balance",
    roi: {x: .1, y: .2, width: .7, height: .1},
  });
  await ui.fire("record-value-roi-refresh", "click");
  await ui.fire("record-value-roi-test", "click");
  assert.equal(ui.calls.at(-1).url, "/api/ocr/roi-test");
  assert.equal(ui.calls.at(-1).data.parser, "currency");
  await ui.fire("record-value-roi-clear", "click");
  const count = ui.calls.filter(c => c.data?.operation === "insert").length;
  await ui.fire("record-value", "click");
  assert.equal(ui.calls.filter(c => c.data?.operation === "insert").length, count);
});

test("live Read Balance defaults to Balance and records once without phone input", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("workflow-preview", "pointerdown");
  await ui.fire("workflow-preview", "pointerup");

  assert.match(
    templateSource,
    /<button id="record-read-value" type="button">\s*Read Balance\s*<\/button>/,
  );
  await ui.fire("record-read-value", "click");
  assert.equal(ui.$("record-read-value-form").hidden, false);
  assert.equal(ui.$("live-value-preset").value, "balance");
  assert.equal(ui.$("live-value-name").value, "Available Balance");
  assert.equal(ui.$("live-value-parser").value, "currency");
  assert.equal(ui.$("live-value-output").value, "payer_balance");

  await ui.fire("live-value-roi-refresh", "click");
  const canvas = ui.$("live-value-roi-canvas");
  canvas.listeners.pointerdown({
    button: 0,
    clientX: 120,
    clientY: 130,
    pointerId: 1,
    preventDefault() {},
    stopPropagation() {},
  });
  canvas.listeners.pointermove({ clientX: 260, clientY: 170 });
  canvas.listeners.pointerup({});
  await ui.fire("live-value-roi-test", "click");
  assert.equal(ui.$("live-value-roi-result").textContent, "Read Balance: 8230.50");
  await ui.fire("live-value-save", "click");

  const valueCall = ui.calls.find(
    (call) =>
      call.url === "/api/recorder/step" && call.data.step.type === "ocr_value",
  );
  assert.deepEqual(valueCall.data, {
    capture: false,
    step: {
      type: "ocr_value",
      label: "Available Balance",
      parser: "currency",
      output: "payer_balance",
      roi: { x: 0.1, y: 0.2, width: 0.7, height: 0.1 },
    },
  });
  assert.deepEqual(
    ui.$("record-steps").children.map((row) => row.children[0].textContent),
    ["Tap", "Read Balance — Available Balance"],
  );
  assert.equal(ui.$("record-read-value-form").hidden, true);
  assert.equal(
    ui.calls.filter((call) => call.url === "/api/control").length,
    0,
  );

  await ui.fire("workflow-preview", "pointerdown");
  await ui.fire("workflow-preview", "pointerup");
  assert.deepEqual(
    ui.$("record-steps").children.map((row) => row.children[0].textContent),
    ["Tap", "Read Balance — Available Balance", "Tap"],
  );
});

test("Read Balance supports Custom Value and editor preserves its calibrated ROI", async () => {
  const roi = { x: 0.12, y: 0.18, width: 0.55, height: 0.08 };
  const ui = setup(false, {
    draft: {
      active: false,
      editing_existing: true,
      workflow: {
        id: "flow_value",
        name: "Value flow",
        variables: [],
        keyboards: {},
        steps: [
          {
            id: "read_value",
            type: "ocr_value",
            label: "Daily Limit",
            parser: "currency",
            output: "daily_limit",
            roi,
          },
        ],
      },
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  const actions = ui.$("record-steps").children[0].children.at(-1);
  await actions.children
    .find((button) => button.textContent === "Edit")
    .listeners.click();
  assert.equal(ui.$("step-value-options").hidden, false);
  assert.equal(ui.$("step-name").value, "Daily Limit");
  assert.equal(ui.$("step-value-parser").value, "currency");
  assert.equal(ui.$("step-value-output").value, "daily_limit");
  for (const [key, value] of Object.entries(roi))
    assert.equal(Number(ui.$(`value-roi-${key}`).value), value);

  ui.$("step-name").value = "Daily Transfer Limit";
  await ui.fire("step-editor", "submit", { preventDefault() {} });
  const fields = ui.calls
    .filter((call) => call.data?.operation === "fields")
    .at(-1);
  assert.deepEqual(fields.data.fields, {
    parser: "currency",
    output: "daily_limit",
    roi,
  });

  ui.$("record-value-preset").value = "custom";
  await ui.fire("record-value-preset", "change");
  assert.equal(ui.$("record-value-name").readOnly, false);
  assert.equal(ui.$("record-value-output").readOnly, false);
});

test("Capture Screenshot assigns proof output without extra client configuration", async () => {
  const ui = setup();
  await ui.fire("record-new", "click");
  await ui.fire("record-start", "click");
  await ui.fire("record-stop", "click");
  await ui.fire("record-screenshot", "click");
  const inserted = ui.calls.find(c => c.data?.operation === "insert");
  assert.deepEqual(inserted.data.step, {
    type: "capture_screen",
    label: "Screenshot",
    output_key: "receipt_path",
  });
  assert.equal(
    ui.$("record-steps").children[0].children[0].textContent,
    "Capture Screenshot",
  );
  assert.match(
    templateSource,
    /<h4>Capture Screenshot<\/h4>[\s\S]*?id="record-screenshot"/,
  );
  assert.doesNotMatch(templateSource, /record-receipt-screenshot/);
  assert.doesNotMatch(templateSource, /step-receipt-screenshot/);
  assert.doesNotMatch(templateSource, /placeholder="receipt_path"/);
});

test("recorder exposes one screenshot action", async () => {
  const ui = setup();
  await ui.fire("record-new", "click");
  await ui.fire("record-start", "click");
  await ui.fire("record-stop", "click");
  await ui.fire("record-screenshot", "click");
  const inserted = ui.calls.find(c => c.data?.operation === "insert");
  assert.equal(inserted.data.step.output_key, "receipt_path");
  assert.doesNotMatch(templateSource, /Capture Receipt Screenshot|Mark Screenshot Here/);
});

test("screenshot proof output is preserved when its step is edited", async () => {
  const ui = setup();
  await ui.fire("record-new", "click");
  await ui.fire("record-start", "click");
  await ui.fire("record-stop", "click");
  await ui.fire("record-screenshot", "click");

  const actions = ui.$("record-steps").children[0].children[1];
  await actions.children[0].listeners.click();
  await ui.fire("step-editor", "submit", { preventDefault() {} });
  const screenshotFields = ui.calls.filter(
    (call) =>
      call.data?.operation === "fields" &&
      Object.hasOwn(call.data.fields || {}, "output_key"),
  );
  assert.equal(screenshotFields.length, 0);
});

test("screenshot steps use recorder insertion and render/edit like existing steps", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("record-stop", "click");

  ui.$("record-wait-seconds").value = "1";
  ui.$("insert-position").value = "0";
  await ui.fire("record-wait", "click");
  ui.$("insert-position").value = "1";
  await ui.fire("record-screenshot", "click");
  const insert = ui.calls.find(
    (call) =>
      call.url === "/api/recorder/review" &&
      call.data.operation === "insert" &&
      call.data.step.type === "capture_screen",
  );
  assert.equal(insert.data.index, 1);
  assert.deepEqual(insert.data.step, {
    type: "capture_screen",
    label: "Screenshot",
    output_key: "receipt_path",
  });
  assert.equal(
    ui.$("record-steps").children[1].children[0].textContent,
    "Capture Screenshot",
  );
  assert.equal(ui.$("record-steps").children[0].children[0].textContent, "Wait 1 seconds");

  await ui.$("record-steps").children[1].children[1].children[0].listeners.click();
  for (const id of [
    "step-tap-options",
    "step-swipe-options",
    "step-key-options",
    "step-input-options",
    "step-wait-options",
    "step-stable-options",
    "step-text-options",
  ]) {
    assert.equal(ui.$(id).hidden, true, `${id} should remain hidden`);
  }

  ui.$("step-name").value = "";
  await ui.fire("step-editor", "submit", { preventDefault() {} });
  assert.equal(
    ui.$("record-steps").children[1].children[0].textContent,
    "Capture Screenshot",
  );

  await ui.$("record-steps").children[1].children[1].children[0].listeners.click();
  ui.$("step-name").value = "Receipt";
  await ui.fire("step-editor", "submit", { preventDefault() {} });
  assert.equal(
    ui.$("record-steps").children[1].children[0].textContent,
    "Capture Screenshot — Receipt",
  );

  await ui.$("record-steps").children[1].children[1].children[2].listeners.click();
  assert.equal(
    ui.$("record-steps").children[0].children[0].textContent,
    "Capture Screenshot — Receipt",
  );
  await ui.$("record-steps").children[0].children[1].children[2].listeners.click();
  assert.equal(
    ui.$("record-steps").children[1].children[0].textContent,
    "Capture Screenshot — Receipt",
  );
  await ui.$("record-steps").children[1].children[1].children[1].listeners.click();
  assert.equal(ui.$("record-steps").children.length, 1);
  assert.equal(ui.$("record-steps").children[0].children[0].textContent, "Wait 1 seconds");
  assert.match(
    templateSource,
    /<h4>Capture Screenshot<\/h4>[\s\S]*?id="record-screenshot"/,
  );
});

test("step editor persists the irreversible payment marker as structured data", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("record-stop", "click");
  ui.$("record-wait-seconds").value = "1";
  await ui.fire("record-wait", "click");
  const actions = ui.$("record-steps").children[0].children.find(
    (child) => child.className === "flow-buttons",
  );
  await actions.children.find((child) => child.textContent === "Edit").listeners.click();
  assert.equal(ui.$("step-critical").checked, false);

  ui.$("step-critical").checked = true;
  await ui.fire("step-editor", "submit", { preventDefault() {} });

  const marker = ui.calls
    .filter((call) => call.data?.operation === "set_critical")
    .at(-1);
  assert.equal(marker.data.critical, true);
  assert.equal(
    ui.$("record-steps").children[0].children.some(
      (child) => child.textContent === "Irreversible payment submission",
    ),
    true,
  );
});

test("live Capture Screenshot records once and continues without physical input", async () => {
  const ui = setup();
  await start(ui);
  assert.match(
    templateSource,
    /<button id="record-screenshot-here" type="button">\s*Capture Screenshot\s*<\/button>/,
  );

  await ui.fire("record-screenshot-here", "click");

  const steps = ui.calls.filter((call) => call.url === "/api/recorder/step");
  assert.equal(steps.length, 1);
  assert.deepEqual(steps[0].data, {
    capture: false,
    step: {
      type: "capture_screen",
      label: "Screenshot",
      output_key: "receipt_path",
    },
  });
  assert.equal(
    ui.calls.filter((call) => call.url === "/api/recorder/stop").length,
    0,
  );
  assert.equal(
    ui.calls.filter((call) => call.url === "/api/control").length,
    0,
  );
  assert.equal(ui.$("record-live").hidden, false);
  assert.equal(ui.$("record-status").textContent, "● Recording • 1 step");
  assert.equal(
    ui.$("record-steps").children[0].children[0].textContent,
    "Capture Screenshot",
  );
});

test("legacy receipt screenshot control is no longer exposed", async () => {
  const ui = setup();
  await start(ui);
  assert.doesNotMatch(templateSource, /record-receipt-screenshot-here/);
  assert.doesNotMatch(templateSource, /Capture Receipt Screenshot/);
});

test("device polling does not overwrite unsaved workflow editor input", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("record-stop", "click");
  ui.$("flow-name").value = "Unsaved workflow name";
  ui.$("record-wait-seconds").value = "17";
  ui.context.window.phoneRecorder.deviceStateChanged();
  assert.equal(ui.$("flow-name").value, "Unsaved workflow name");
  assert.equal(ui.$("record-wait-seconds").value, "17");
});

test("camera drag sends points for backend perspective correction", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("video", "pointerdown", { clientX: 200, clientY: 370 });
  ui.setClock(1420);
  await ui.fire("video", "pointerup", { clientX: 200, clientY: 130 });
  const call = ui.calls.find((x) => x.url === "/api/recorder/step");
  assert.deepEqual(call.data, {
    step: { type: "swipe", duration_ms: 420 },
    capture: true,
    frame_id: 123,
    camera_start: [0.5, 0.8],
    camera_end: [0.5, 0.2],
    coordinate_space: "camera_normalized",
  });
});

test("cancelled, outside and secondary gestures are discarded", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("workflow-preview", "pointerdown");
  await ui.fire("workflow-preview", "pointercancel");
  await ui.fire("workflow-preview", "pointerup");
  await ui.fire("workflow-preview", "pointerdown");
  await ui.fire("workflow-preview", "pointerup", { clientX: 500 });
  await ui.fire("workflow-preview", "pointerdown", { isPrimary: false });
  await ui.fire("workflow-preview", "pointerup");
  assert.equal(
    ui.calls.filter((x) => x.url === "/api/recorder/step").length,
    0,
  );
});

test("ordinary typing is batched automatically and ordered before Enter and Stop", async () => {
  const ui = setup();
  await start(ui);
  for (const ch of "John Tanx") ui.key(ch);
  ui.key("Backspace");
  ui.key("Enter");
  await ui.fire("record-stop", "click");
  const steps = ui.calls
    .filter((x) => x.url === "/api/recorder/step")
    .map((x) => x.data.step);
  assert.deepEqual(steps, [
    { type: "input", source: "fixed", value: "John Tan" },
    { type: "key", key: "ENTER" },
  ]);
  assert.equal(
    ui.$("record-steps").children[0].children[1].textContent,
    "“John Tan”",
  );
});

test("review converts a captured input to a variable and back without JSON editing", async () => {
  const ui = setup();
  await start(ui);
  for (const ch of "John") ui.key(ch);
  await ui.fire("record-stop", "click");
  const actions = ui.$("record-steps").children[0].children.at(-1);
  await actions.children
    .find((b) => b.textContent === "Edit")
    .listeners.click();
  ui.$("input-source").value = "variable";
  ui.$("variable-name").value = "customer_name";
  ui.$("variable-default").value = "Guest";
  await ui.fire("step-editor", "submit");
  const call = ui.calls.find(
    (x) => x.url === "/api/recorder/review" && x.data.operation === "input",
  );
  assert.deepEqual(call.data.variable, {
    name: "customer_name",
    label: "customer name",
    type: "text",
    required: true,
    default: "Guest",
    ask_when_running: true,
  });
  assert.equal(Object.hasOwn(call.data, "value"), false);
  const nextActions = ui.$("record-steps").children[0].children.at(-1);
  await nextActions.children
    .find((b) => b.textContent === "Edit")
    .listeners.click();
  ui.$("input-source").value = "fixed";
  ui.$("input-value").value = "Alice";
  await ui.fire("step-editor", "submit");
  const latest = ui.calls
    .filter(
      (x) => x.url === "/api/recorder/review" && x.data.operation === "input",
    )
    .at(-1);
  assert.equal(latest.data.value, "Alice");
  assert.equal(latest.data.source, "fixed");
});

test("editing a variable shows and updates its default without replacing legacy metadata", async () => {
  const ui = setup(false, {
    draft: {
      active: false,
      editing_existing: true,
      workflow: {
        id: "flow_variable",
        name: "Variable flow",
        variables: [
          {
            name: "amount",
            label: "Amount",
            type: "text",
            required: true,
            default: "10",
            ask_when_running: false,
          },
        ],
        keyboards: {
          keyboard_test: {
            id: "keyboard_test",
            name: "Amount keypad",
            kind: "custom",
            keys: { 1: { x: 0.5, y: 0.5 } },
          },
        },
        steps: [
          {
            id: "step_1",
            type: "input",
            source: "variable",
            variable: "amount",
            amount_format: "implied_cents",
            input_method: "screen_keyboard",
            keyboard_id: "keyboard_test",
          },
        ],
      },
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  const actions = ui.$("record-steps").children[0].children.at(-1);
  await actions.children
    .find((button) => button.textContent === "Edit")
    .listeners.click();
  assert.equal(ui.$("variable-default").value, "10");
  assert.equal(ui.$("input-keyboard").value, "keyboard_test");
  assert.equal(ui.$("amount-format-field").hidden, false);
  assert.equal(ui.$("amount-format").value, "implied_cents");
  ui.$("variable-default").value = "20";
  await ui.fire("step-editor", "submit");
  const update = ui.calls
    .filter((call) => call.data?.operation === "input")
    .at(-1);
  assert.deepEqual(update.data.variable, { name: "amount", default: "20" });
  assert.equal(update.data.amount_format, "implied_cents");

  const updatedActions = ui.$("record-steps").children[0].children.at(-1);
  await updatedActions.children
    .find((button) => button.textContent === "Edit")
    .listeners.click();
  ui.$("variable-name").value = "bank_name";
  await ui.fire("variable-name", "change");
  assert.equal(ui.$("amount-format-field").hidden, true);
  await ui.fire("step-editor", "submit");
  const bankUpdate = ui.calls
    .filter((call) => call.data?.operation === "input")
    .at(-1);
  assert.equal(bankUpdate.data.variable.name, "bank_name");
  assert.equal(Object.hasOwn(bankUpdate.data, "amount_format"), false);
});

test("amount format is scoped to Amount and both selectors include Bank Name", async () => {
  const ui = setup();
  const recordSelector = templateSource.match(
    /<select id="record-variable-name">[\s\S]*?<\/select>/,
  )[0];
  const editorSelector = templateSource.match(
    /<select id="variable-name">[\s\S]*?<\/select>/,
  )[0];
  assert.match(recordSelector, /value="bank_name">Bank Name/);
  assert.match(editorSelector, /value="bank_name">Bank Name/);

  await start(ui);
  await ui.fire("record-input-variable", "click");
  ui.$("record-variable-name").value = "amount";
  await ui.fire("record-variable-name", "change");
  assert.equal(ui.$("record-amount-format-field").hidden, false);
  ui.$("record-amount-format").value = "implied_cents";
  ui.$("record-variable-default").value = "25.50";
  ui.$("record-variable-keyboard").value = "keyboard_test";
  await ui.fire("record-variable-add", "click");
  const amountRequest = ui.calls
    .filter((entry) => entry.url === "/api/recorder/variable-input")
    .at(-1);
  assert.equal(amountRequest.data.amount_format, "implied_cents");
  assert.equal(
    ui.$("record-steps").children[0].children[0].textContent,
    "Type Text — Amount · Implied cents",
  );

  await ui.fire("record-input-variable", "click");
  ui.$("record-variable-name").value = "bank_name";
  await ui.fire("record-variable-name", "change");
  assert.equal(ui.$("record-amount-format-field").hidden, true);
  ui.$("record-variable-default").value = "Example Bank";
  ui.$("record-variable-keyboard").value = "keyboard_test";
  await ui.fire("record-variable-add", "click");
  const bankRequest = ui.calls
    .filter((entry) => entry.url === "/api/recorder/variable-input")
    .at(-1);
  assert.equal(Object.hasOwn(bankRequest.data, "amount_format"), false);
});

test("shortcuts and dashboard editing stay local; uncertain capture stops later actions", async () => {
  const ui = setup();
  await start(ui);
  assert.equal(ui.key("a", { ctrlKey: true }).defaultPrevented, undefined);
  assert.equal(
    ui.key("a", { target: { closest: () => ({}) } }).defaultPrevented,
    undefined,
  );
  ui.fail();
  ui.key("x");
  ui.key("Enter");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(
    ui.calls.filter((x) => x.url === "/api/recorder/step").length,
    1,
  );
  assert.equal(ui.context.window.phoneRecorder.capturesPreview(), false);
});


test("Pi recording uses manual controls, shows live friendly steps and stops into review", async () => {
  const ui = setup(true);
  await new Promise(resolve => setImmediate(resolve));
  await ui.fire("record-new", "click");
  const recorder = ui.context.window.phoneRecorder;
  assert.equal(ui.$("record-live").hidden, false);
  assert.equal(ui.$("record-empty").hidden, false);
  assert.equal(recorder.capturesPreview(), false);
  assert.equal(ui.$("mock-recording-preview").hidden, true);
  for (const key of ["a", "Enter", "Backspace", "Escape"]) {
    assert.equal(ui.key(key).defaultPrevented, undefined);
  }
  await ui.fire("video", "pointerdown");
  await ui.fire("video", "pointerup");
  assert.equal(ui.calls.filter(call => call.url === "/api/recorder/step").length, 0);
  const snapshot = { active:true, workflow: { name:"New Recording", variables:[], steps:[
    { id:"s1", type:"tap", x:.52139, y:.7302 },
    { id:"s2", type:"swipe", start:[.5,.8], end:[.5,.2] },
    { id:"s3", type:"input", source:"fixed", value:"hello" },
    { id:"s4", type:"key", key:"ENTER" },
    { id:"s5", type:"key", key:"BACK" },
  ] } };
  recorder.acceptManual(snapshot);
  const rows = ui.$("record-steps").children;
  assert.equal(ui.$("record-status").textContent, "● Recording • 5 steps");
  assert.deepEqual(rows.map(row => row.children[0].textContent),
    ["Tap", "Swipe Up", "Type Text", "Press Enter", "Back"]);
  assert.equal(rows[0].children[1].textContent, "X 52% • Y 73%");
  assert.equal(rows[2].children[1].textContent, "“hello”");
  assert.equal(ui.$("record-empty").hidden, true);
  assert.equal(ui.$("review-flow").hidden, true);
  await ui.fire("record-stop", "click");
  assert.equal(ui.$("record-live").hidden, true);
  assert.equal(ui.$("review-flow").hidden, false);
  assert.match(ui.$("workflow-notice").textContent, /Recording stopped/);
});

test("Pi Stop blocks new input immediately and waits for the acknowledged action", async () => {
  const ui = setup(true);
  await start(ui);
  const recorder = ui.context.window.phoneRecorder;
  let finish;
  recorder.trackManual(new Promise(resolve => { finish = resolve; }));
  const stop = ui.fire("record-stop", "click");
  assert.equal(recorder.blocksInput(), true);
  assert.equal(ui.calls.filter(call => call.url === "/api/recorder/stop").length, 0);
  finish();
  await stop;
  assert.equal(ui.calls.filter(call => call.url === "/api/recorder/stop").length, 1);
  assert.equal(recorder.blocksInput(), false);
});

test("Pi failure stops capture and warns about uncertain input without retrying", async () => {
  const ui = setup(true);
  await start(ui);
  await ui.context.window.phoneRecorder.manualFailed(new Error("Input timed out."));
  assert.match(ui.$("workflow-notice").textContent, /Input may already have occurred/);
  assert.equal(ui.$("record-live").hidden, true);
  assert.equal(ui.$("review-flow").hidden, false);
  assert.equal(ui.calls.filter(call => call.url === "/api/recorder/step").length, 0);
});

test("empty workflow names get a friendly validation message without saving", async () => {
  const ui = setup(true);
  await start(ui);
  await ui.fire("record-stop", "click");
  ui.$("flow-name").value = "  ";
  await ui.fire("record-save", "click");
  assert.equal(ui.$("workflow-notice").textContent, "Enter bank name");
  assert.equal(ui.calls.filter(call => call.url === "/api/recorder/save").length, 0);
});


test("normal recorder UI omits low-level stability wait creation", () => {
  assert.doesNotMatch(templateSource, /id="record-stable"/);
  assert.doesNotMatch(templateSource, /id="record-stable-ms"/);
  assert.doesNotMatch(templateSource, /id="record-stable-timeout"/);
});

test("Verify Text is available in edit and recording authoring with shared controls", () => {
  assert.match(templateSource, /data-add-step-type="verify_text"/);
  assert.match(templateSource, /id="record-verify-text-here"/);
  assert.match(templateSource, /id="record-verify-controls"/);
  assert.match(templateSource, /id="record-verify-live-slot"/);
  assert.match(templateSource, /id="record-verify-review-slot"/);
  assert.match(templateSource, /id="verify-field"/);
  assert.match(templateSource, /id="step-verify-field"/);
  assert.match(source, /type: "verify_text"/);
  assert.match(source, /verifyTextFields\(\s*"step-verify"/);
});

test("Verify Text exposes current payment fields without the legacy mobile alias", () => {
  const editor = templateSource.match(
    /<select id="step-verify-field">([\s\S]*?)<\/select>/,
  );
  const recorder = templateSource.match(
    /<select id="verify-field">([\s\S]*?)<\/select>/,
  );
  assert.ok(editor);
  assert.ok(recorder);
  const expected = [
    ["account_name", "Account Name"],
    ["holder_name", "Holder Name"],
    ["account_number", "Account Number"],
    ["fps_phone_number", "FPS Phone Number"],
    ["fps_email", "FPS Email Address"],
    ["fps_id", "FPS ID"],
    ["bank_name", "Bank Name"],
    ["amount", "Amount"],
  ];
  for (const markup of [editor[1], recorder[1]]) {
    const options = [
      ...markup.matchAll(/<option value="([^"]+)">([^<]+)<\/option>/g),
    ].map((match) => [match[1], match[2].trim()]);
    assert.deepEqual(options, expected);
  }
  for (const removedId of [
    "verify-source",
    "verify-variable",
    "verify-text-value",
    "verify-match-mode",
    "verify-last-n",
    "step-verify-source",
    "step-verify-variable",
    "step-verify-text-value",
    "step-verify-match-mode",
    "step-verify-last-n",
  ]) {
    assert.doesNotMatch(templateSource, new RegExp(`id="${removedId}"`));
  }
  assert.match(source, /account_name:[\s\S]*match_mode: "account_name"/);
  assert.match(source, /account_number:[\s\S]*match_mode: "account_number"/);
  assert.match(source, /bank_name:[\s\S]*match_mode: "contains"/);
  assert.match(source, /amount:[\s\S]*match_mode: "amount"/);
});

test("Verify Text in a transfer branch uses the API-defined workflow variable", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("record-alternate-screen", "click");
  ui.$("condition-source").value = "withdrawal_api";
  await ui.fire("condition-source", "change");
  ui.$("condition-transfer-method").value = "phone_no";
  await ui.fire("alternate-screen-start", "click");

  await ui.fire("record-verify-text-here", "click");
  assert.deepEqual(
    ui.$("verify-field").children.map((option) => option.value),
    ["fps_phone_number"],
  );
  assert.equal(ui.$("verify-field").value, "fps_phone_number");

  await ui.fire("verify-add", "click");
  const added = ui.calls.find(
    (call) =>
      call.url === "/api/recorder/step" &&
      call.data.step.type === "verify_text",
  );
  assert.equal(added.data.step.variable, "fps_phone_number");
  assert.equal(added.data.step.match_mode, "fps_phone");
});

test("normal Add Step keeps Read Balance alongside the existing simple steps", () => {
  const start = templateSource.indexOf('<details id="review-insert">');
  const end = templateSource.indexOf(
    '<form id="expected-text-editor"',
    start,
  );
  const addStep = templateSource.slice(start, end);
  assert.match(addStep, /<h4>Wait for Text<\/h4>/);
  assert.match(addStep, /id="record-text"/);
  assert.match(addStep, /<h4>Capture Screenshot<\/h4>/);
  assert.match(addStep, /id="record-screenshot"/);
  assert.match(addStep, /<h4>Read Value<\/h4>/);
  assert.match(addStep, /id="record-value"/);
  assert.match(addStep, /<h4>Wait<\/h4>/);
  assert.match(addStep, /id="record-wait"/);
  assert.doesNotMatch(addStep, /Wait for Screen to Settle/);
  assert.doesNotMatch(addStep, /Manual Checkpoint|record-checkpoint/);
});

test("advanced authoring controls are hidden while defaults remain editable", () => {
  assert.doesNotMatch(templateSource, /id="flow-financial"/);
  assert.doesNotMatch(templateSource, /id="flow-recovery-workflow"/);
  assert.doesNotMatch(templateSource, /id="variable-ask"|Ask When Running/);
  assert.doesNotMatch(
    templateSource,
    /id="variable-label"|id="variable-required"/,
  );
  assert.match(templateSource, /id="variable-default"/);
  for (const id of [
    "record-text-advanced",
    "step-text-advanced",
    "condition-text-advanced",
    "expected-text-advanced",
  ]) {
    assert.match(
      templateSource,
      new RegExp(
        `<details\\s+id="${id}"\\s+class="advanced-options"\\s*>`,
      ),
    );
  }
  assert.match(source, /function setSectionVisible\(id, visible\)/);
  assert.match(source, /control\.disabled = !visible/);
  assert.doesNotMatch(source, /financial: \$\("flow-financial"\)/);
  assert.doesNotMatch(source, /ask_when_running: \$\("variable-ask"\)/);
});

test("recording actions omit Mark Next Screen and keep keyboard capture primary", () => {
  assert.doesNotMatch(templateSource, /id="record-mark-next"/);
  assert.doesNotMatch(templateSource, /<span>More<\/span>/);
  const primary = templateSource.match(
    /<div class="record-action-group record-primary-actions">([\s\S]*?)<\/div>/,
  );
  assert.ok(primary);
  assert.match(primary[1], /id="record-capture-keyboard"/);
  assert.match(
    templateSource,
    /<span>Recording controls<\/span>[\s\S]*id="record-undo"[\s\S]*id="record-stop"/,
  );
});

test("existing smart wait remains visible and editable", async () => {
  const ui = setup(true);
  await start(ui);
  await ui.fire("record-stop", "click");
  await ui.context.api("/api/recorder/review", {
    operation: "insert",
    index: 0,
    step: { type: "wait_until_stable", stable_ms: 700, timeout_seconds: 10 },
  });
  await ui.context.window.phoneRecorder.refreshAfterReconnect();
  const row = ui.$("record-steps").children[0];
  assert.match(row.children[0].textContent, /Wait for Screen to Settle.*700 ms.*10 sec/);
  await row.children.find(c => c.className === "flow-buttons").children.find(c => c.textContent === "Edit").listeners.click();
  assert.equal(ui.$("step-stable-options").hidden, false);
  assert.equal(ui.$("step-wait-options").hidden, true);
  assert.equal(ui.$("step-stable-ms").value, 700);
  ui.$("step-stable-ms").value = "900";
  ui.$("step-stable-timeout").value = "3";
  await ui.fire("step-editor", "submit");
  const edited = ui.calls.filter(c => c.data?.operation === "fields").at(-1);
  assert.deepEqual(edited.data.fields, { stable_ms: 900, timeout_seconds: 3 });
});

test("smart wait uses the existing move and delete controls alongside fixed waits", async () => {
  const ui = setup(true);
  await start(ui);
  await ui.fire("record-stop", "click");
  ui.$("insert-position").value = "0";
  ui.$("record-wait-seconds").value = "1";
  await ui.fire("record-wait", "click");
  await ui.context.api("/api/recorder/review", {
    operation: "insert",
    index: 1,
    step: { type: "wait_until_stable", stable_ms: 700, timeout_seconds: 10 },
  });
  await ui.context.window.phoneRecorder.refreshAfterReconnect();
  async function action(index, name) {
    const row = ui.$("record-steps").children[index];
    await row.children.find(c => c.className === "flow-buttons")
      .children.find(c => c.textContent === name).listeners.click();
    await new Promise(resolve => setImmediate(resolve));
  }
  await action(1, "Move Up");
  assert.match(ui.$("record-steps").children[0].children[0].textContent, /Wait for Screen to Settle/);
  await action(0, "Delete");
  assert.equal(ui.$("record-steps").children.length, 1);
  assert.equal(ui.$("record-steps").children[0].children[0].textContent, "Wait 1 seconds");
});

test("wait for text can be inserted, edited, moved and deleted", async () => {
  const ui = setup(true);
  await start(ui);
  await ui.fire("record-stop", "click");
  ui.$("insert-position").value = "0";
  ui.$("record-wait-seconds").value = "1";
  await ui.fire("record-wait", "click");
  ui.$("insert-position").value = "1";
  ui.$("record-text-value").value = "  Transfer  ";
  ui.$("record-text-timeout").value = "10";
  await ui.fire("record-text", "click");
  const inserted = ui.calls.filter(c => c.data?.operation === "insert").at(-1);
  assert.deepEqual(inserted.data.step, {
    type: "wait_for_text", text: "Transfer", timeout_seconds: 10,
  });
  let row = ui.$("record-steps").children[1];
  assert.equal(
    row.children[0].textContent,
    "Wait for Text — “Transfer” · Wait up to 10 sec",
  );
  await row.children.find(c => c.className === "flow-buttons")
    .children.find(c => c.textContent === "Edit").listeners.click();
  assert.equal(ui.$("step-text-options").hidden, false);
  assert.equal(ui.$("step-stable-options").hidden, true);
  ui.$("step-text-value").value = "Confirm";
  ui.$("step-text-timeout").value = "3";
  await ui.fire("step-editor", "submit");
  const edited = ui.calls.filter(c => c.data?.operation === "fields").at(-1);
  assert.deepEqual(edited.data.fields, { text: "Confirm", timeout_seconds: 3 });
  row = ui.$("record-steps").children[1];
  await row.children.find(c => c.className === "flow-buttons")
    .children.find(c => c.textContent === "Move Up").listeners.click();
  assert.match(ui.$("record-steps").children[0].children[0].textContent, /“Confirm”/);
  row = ui.$("record-steps").children[0];
  await row.children.find(c => c.className === "flow-buttons")
    .children.find(c => c.textContent === "Delete").listeners.click();
  assert.equal(ui.$("record-steps").children.length, 1);

  ui.$("record-text-value").value = "   ";
  await ui.fire("record-text", "click");
  assert.match(ui.$("workflow-notice").textContent, /between 1 and 500/);
});

async function reviewAction(ui, index, label) {
  const actions = ui.$("record-steps").children[index].children.find(c => c.className === "flow-buttons");
  await actions.children.find(c => c.textContent === label).listeners.click();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

test("expected text is requested only in review and inserts, changes and removes the following wait", async () => {
  const ui = setup();
  await start(ui);
  for (let i = 0; i < 2; i++) {
    await ui.fire("workflow-preview", "pointerdown");
    await ui.fire("workflow-preview", "pointerup");
  }
  assert.equal(ui.calls.filter(c => c.url.includes("text-suggestions")).length, 0);
  await ui.fire("record-stop", "click");
  await reviewAction(ui, 0, "Add Expected Text");
  assert.equal(ui.$("expected-text-editor").hidden, false);
  assert.equal(ui.$("expected-text-timeout").value, 10);
  assert.equal(ui.$("expected-suggestions").children.length, 2);
  ui.$("expected-suggestions").children[0].children[0].listeners.change();
  await ui.fire("expected-text-editor", "submit");
  assert.match(ui.$("record-steps").children[1].children[0].textContent, /Pay & Transfer/);
  assert.equal(ui.$("record-steps").children[2].children[0].textContent, "Tap");
  await reviewAction(ui, 0, "Change");
  ui.$("expected-text-value").value = "Manual entry";
  ui.$("expected-text-timeout").value = "4";
  await ui.fire("expected-text-editor", "submit");
  assert.equal(ui.$("record-steps").children.length, 3);
  assert.match(ui.$("record-steps").children[1].children[0].textContent, /Manual entry.*4 sec/);
  await reviewAction(ui, 0, "Remove");
  assert.equal(ui.$("record-steps").children.length, 2);
});

test("OCR failure permits manual expected text, and Cancel does not edit the workflow", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("workflow-preview", "pointerdown");
  await ui.fire("workflow-preview", "pointerup");
  await ui.fire("record-stop", "click");
  ui.failOCR();
  await reviewAction(ui, 0, "Add Expected Text");
  assert.match(ui.$("expected-text-status").textContent, /No reliable text suggestions/);
  await ui.fire("expected-text-cancel", "click");
  assert.equal(ui.calls.filter(c => c.url === "/api/recorder/review").length, 0);
  await reviewAction(ui, 0, "Add Expected Text");
  await ui.fire("expected-text-manual", "click");
  ui.$("expected-text-value").value = "Entered manually";
  await ui.fire("expected-text-editor", "submit");
  assert.match(ui.$("record-steps").children[1].children[0].textContent, /Entered manually.*10 sec/);
});

test("Expect Current Screen adds a standalone wait and keeps recording active", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("workflow-preview", "pointerdown");
  await ui.fire("workflow-preview", "pointerup");
  await ui.fire("record-expect-current", "click");
  assert.equal(ui.$("expected-text-editor").hidden, false);
  assert.equal(ui.$("expected-text-timeout").value, 10);
  assert.equal(ui.context.window.phoneRecorder.blocksInput(), true);
  assert.equal(ui.context.window.phoneRecorder.capturesPreview(), false);
  ui.$("expected-text-value").value = "Current screen";
  await ui.fire("expected-roi-refresh", "click");
  const canvas = ui.$("expected-roi-canvas");
  canvas.listeners.pointerdown({button: 0, clientX: 120, clientY: 90, pointerId: 1, preventDefault() {}, stopPropagation() {}});
  canvas.listeners.pointermove({clientX: 260, clientY: 130});
  canvas.listeners.pointerup({});
  await ui.fire("expected-roi-test", "click");
  assert.match(ui.$("expected-roi-result").textContent, /Target detected/);
  await ui.fire("expected-text-editor", "submit");
  const saved = ui.calls.filter((call) => call.url === "/api/recorder/expected-text").at(-1);
  assert.deepEqual(saved.data.roi, {x: .1, y: .1, width: .7, height: .1});
  const rows = ui.$("record-steps").children;
  assert.deepEqual(rows.map(row => row.children[0].textContent), [
    "Tap", 'Wait for Text — “Current screen” · Wait up to 10 sec',
  ]);
  assert.equal(ui.$("record-live").hidden, false);
  assert.equal(ui.context.window.phoneRecorder.blocksInput(), false);
});

test("Traditional Chinese expected-text suggestions are not keyboard noise", () => {
  const pattern =
    /function isUsefulExpectedText\(text\) \{[\s\S]*?\r?\n  \}\r?\n\r?\n  async function openExpectedText/;
  const trailingPattern =
    /\r?\n\r?\n  async function openExpectedText[\s\S]*$/;
  const functionSource = extractFunctionSource(source, pattern, trailingPattern);
  assert.equal(
    extractFunctionSource(
      source.replace(/\r?\n/g, "\r\n"),
      pattern,
      trailingPattern,
    ),
    functionSource,
  );
  const isUsefulExpectedText = vm.runInNewContext(`(${functionSource})`);
  for (const text of ["確認", "取消", "轉賬成功", "可用餘額", "收款人"])
    assert.equal(isUsefulExpectedText(text), true, text);
  for (const text of ["", "---", "qwerty", "ASDF", "123456"])
    assert.equal(isUsefulExpectedText(text), false, text);
});

test("keyboard teaching clicks are normalized, saved, and never recorded or sent", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("record-capture-keyboard", "click");
  ui.$("keyboard-capture-name").value = "Account keypad";
  ui.$("keyboard-capture-kind").value = "custom";
  ui.$("keyboard-custom-keys").value = "1,2";
  await ui.fire("keyboard-capture-start", "click");
  assert.equal(ui.context.window.phoneRecorder.blocksInput(), true);
  ui.key("9");
  await ui.fire("workflow-preview", "click", { clientX: 150, clientY: 150 });
  await ui.fire("workflow-preview", "click", { clientX: 250, clientY: 350 });
  assert.equal(
    ui.calls.filter((entry) => entry.url === "/api/recorder/step").length,
    0,
  );
  const captures = ui.calls.filter(
    (entry) => entry.url === "/api/recorder/keyboard-capture/key",
  );
  assert.deepEqual(captures.map((entry) => entry.data.key), ["1", "2"]);
  assert.deepEqual(captures[0].data.point, [0.25, 0.25]);
  assert.deepEqual(captures[1].data.point, [0.75, 0.75]);
  await ui.fire("keyboard-capture-save", "click");
  assert.equal(ui.context.window.phoneRecorder.blocksInput(), false);
  assert.match(ui.$("workflow-notice").textContent, /layout remains stable/);
  assert.equal(ui.$("record-live").hidden, false);
});

test("keyboard capture cancel keeps the active workflow unchanged", async () => {
  const ui = setup(true);
  await start(ui);
  await ui.fire("record-capture-keyboard", "click");
  ui.$("keyboard-capture-name").value = "Temporary keypad";
  ui.$("keyboard-capture-kind").value = "custom";
  ui.$("keyboard-custom-keys").value = "1";
  await ui.fire("keyboard-capture-start", "click");
  await ui.fire("video", "click", { clientX: 200, clientY: 250 });
  await ui.fire("keyboard-capture-cancel", "click");
  assert.equal(ui.$("record-steps").children.length, 0);
  assert.equal(ui.context.window.phoneRecorder.blocksInput(), false);
  assert.equal(ui.$("record-live").hidden, false);
});

test("alphabetic capture UI requests optional symbols, space, backspace, and case toggle", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("record-capture-keyboard", "click");
  ui.$("keyboard-capture-name").value = "App keyboard";
  ui.$("keyboard-capture-kind").value = "alphabetic";
  await ui.fire("keyboard-capture-kind", "change");
  ui.$("keyboard-alphabetic-include-symbols").checked = true;
  ui.$("keyboard-include-space").checked = true;
  ui.$("keyboard-alphabetic-backspace").checked = true;
  ui.$("keyboard-alphabetic-shift").checked = true;
  await ui.fire("keyboard-capture-start", "click");
  const request = ui.calls.find(
    (entry) => entry.url === "/api/recorder/keyboard-capture/start",
  );
  assert.equal(request.data.kind, "alphabetic");
  assert.deepEqual(request.data.keys.slice(0, 3), ["q", "w", "e"]);
  assert.deepEqual(
    request.data.keys.slice(26, 33),
    ["@", "#", "$", "+", "(", ";", '"'],
  );
  assert.deepEqual(request.data.keys.slice(-3), [" ", "backspace", "shift"]);
  assert.equal(new Set(request.data.keys).size, 36);
  assert.equal(ui.$("keyboard-alphabetic-options").hidden, false);
  assert.equal(ui.$("keyboard-numeric-options").hidden, true);
  assert.equal(ui.context.window.phoneRecorder.blocksInput(), true);
});

test("keyboard key generation preserves Numeric, Alphabetic options, and Custom", () => {
  const fields = new Map([
    ["keyboard-include-decimal", { checked: false }],
    ["keyboard-numeric-backspace", { checked: false }],
    ["keyboard-alphabetic-include-digits", { checked: false }],
    ["keyboard-alphabetic-include-symbols", { checked: false }],
    ["keyboard-include-space", { checked: false }],
    ["keyboard-alphabetic-backspace", { checked: false }],
    ["keyboard-alphabetic-shift", { checked: false }],
    ["keyboard-custom-keys", { value: "A,1,-" }],
  ]);
  const pattern =
    /function keyboardCaptureKeys\(kind\) \{[\s\S]*?\r?\n  \}\r?\n\r?\n  function canExpectText/;
  const trailingPattern = /\r?\n\r?\n  function canExpectText[\s\S]*$/;
  const functionSource = extractFunctionSource(source, pattern, trailingPattern);
  assert.equal(
    extractFunctionSource(
      source.replace(/\r?\n/g, "\r\n"),
      pattern,
      trailingPattern,
    ),
    functionSource,
  );
  const keyboardCaptureKeys = vm.runInNewContext(`(${functionSource})`, {
    $: (id) => fields.get(id),
  });

  assert.deepEqual(Array.from(keyboardCaptureKeys("numeric")), Array.from("0123456789"));
  assert.deepEqual(
    Array.from(keyboardCaptureKeys("alphabetic")),
    Array.from("qwertyuiopasdfghjklzxcvbnm"),
  );
  fields.get("keyboard-alphabetic-shift").checked = true;
  assert.equal(keyboardCaptureKeys("alphabetic").at(-1), "shift");
  fields.get("keyboard-alphabetic-shift").checked = false;
  fields.get("keyboard-alphabetic-include-digits").checked = true;
  assert.deepEqual(
    Array.from(keyboardCaptureKeys("alphabetic").slice(-10)),
    Array.from("0123456789"),
  );
  fields.get("keyboard-alphabetic-include-digits").checked = false;
  fields.get("keyboard-alphabetic-include-symbols").checked = true;
  assert.deepEqual(
    Array.from(keyboardCaptureKeys("alphabetic").slice(-7)),
    ["@", "#", "$", "+", "(", ";", '"'],
  );
  assert.deepEqual(Array.from(keyboardCaptureKeys("custom")), ["A", "1", "-"]);
  assert.equal(
    /id="keyboard-alphabetic-include-digits"[\s\S]*?type="checkbox"/.test(
      templateSource,
    ),
    true,
  );
  assert.equal(
    /id="keyboard-alphabetic-include-symbols"[\s\S]*?type="checkbox"/.test(
      templateSource,
    ),
    true,
  );
  assert.equal(
    /id="keyboard-alphabetic-shift"[\s\S]*?type="checkbox"/.test(templateSource),
    true,
  );
});

test("captured keyboard list and selector distinguish multiple maps", async () => {
  const ui = setup();
  await start(ui);
  for (const [name, key] of [
    ["Account Number Keypad", "1"],
    ["Amount Keypad", "."],
  ]) {
    await ui.fire("record-capture-keyboard", "click");
    ui.$("keyboard-capture-name").value = name;
    ui.$("keyboard-capture-kind").value = "custom";
    ui.$("keyboard-custom-keys").value = key;
    await ui.fire("keyboard-capture-start", "click");
    await ui.fire("workflow-preview", "click");
    await ui.fire("keyboard-capture-save", "click");
  }
  assert.equal(ui.$("captured-keyboards").hidden, false);
  assert.equal(ui.$("captured-keyboard-list").children.length, 2);
  assert.equal(
    ui.$("captured-keyboard-list").children[0].className,
    "keyboard-map-row",
  );
  assert.match(
    ui.$("captured-keyboard-list").children[0].children[0].children[0]
      .textContent,
    /Account Number Keypad|Amount Keypad/,
  );
  ui.key("1");
  await ui.fire("record-stop", "click");
  const actions = ui.$("record-steps").children[0].children.at(-1);
  await actions.children.find((button) => button.textContent === "Edit").listeners.click();
  assert.deepEqual(
    ui.$("input-keyboard").children.map((option) => option.textContent).sort(),
    ["Account Number Keypad · Custom", "Amount Keypad · Custom"],
  );
});

test("unused captured keyboard can be deleted from the map list", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("record-capture-keyboard", "click");
  ui.$("keyboard-capture-name").value = "Temporary keypad";
  ui.$("keyboard-capture-kind").value = "custom";
  ui.$("keyboard-custom-keys").value = "1";
  await ui.fire("keyboard-capture-start", "click");
  await ui.fire("workflow-preview", "click");
  await ui.fire("keyboard-capture-save", "click");
  const remove = ui.$("captured-keyboard-list").children[0].children[1];
  await remove.listeners.click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.$("captured-keyboards").hidden, true);
  assert.equal(
    ui.calls.filter((entry) => entry.url === "/api/recorder/keyboard-delete")
      .length,
    1,
  );
});

test("review explicitly selects a captured keyboard for variable input", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("record-capture-keyboard", "click");
  ui.$("keyboard-capture-name").value = "Account keypad";
  ui.$("keyboard-capture-kind").value = "custom";
  ui.$("keyboard-custom-keys").value = "1";
  await ui.fire("keyboard-capture-start", "click");
  await ui.fire("workflow-preview", "click");
  await ui.fire("keyboard-capture-save", "click");
  ui.key("1");
  await ui.fire("record-stop", "click");
  const actions = ui.$("record-steps").children[0].children.at(-1);
  await actions.children.find((button) => button.textContent === "Edit").listeners.click();
  assert.equal(
    ui.$("input-keyboard").children[0].textContent,
    "Account keypad · Custom",
  );
  ui.$("input-source").value = "variable";
  ui.$("variable-name").value = "account_no";
  ui.$("input-method").value = "screen_keyboard";
  ui.$("input-keyboard").value = "keyboard_test";
  await ui.fire("step-editor", "submit");
  const update = ui.calls
    .filter(
      (entry) =>
        entry.url === "/api/recorder/review" && entry.data.operation === "input",
    )
    .at(-1);
  assert.equal(update.data.source, "variable");
  assert.equal(update.data.input_method, "screen_keyboard");
  assert.equal(update.data.keyboard_id, "keyboard_test");
  const mapRow = ui.$("captured-keyboard-list").children[0];
  assert.equal(mapRow.children[1].disabled, true);
  assert.equal(mapRow.children[1].textContent, "In Use");
  assert.equal(
    mapRow.children[0].children[2].textContent,
    "Used by: Input {{account_no}}",
  );
});

test("live Input Variable saves a default without typing it", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("record-capture-keyboard", "click");
  ui.$("keyboard-capture-name").value = "Account keypad";
  ui.$("keyboard-capture-kind").value = "custom";
  ui.$("keyboard-custom-keys").value = "1";
  await ui.fire("keyboard-capture-start", "click");
  await ui.fire("workflow-preview", "click");
  await ui.fire("keyboard-capture-save", "click");
  await ui.fire("record-input-variable", "click");
  ui.$("record-variable-name").value = "account_no";
  ui.$("record-variable-default").value = "111";
  await ui.fire("record-variable-add", "click");
  const request = ui.calls.filter(
    (entry) => entry.url === "/api/recorder/variable-input",
  ).at(-1);
  assert.equal(request.data.type_sample, false);
  assert.equal(request.data.sample, "111");
  assert.equal(request.data.variable.default, "111");
  assert.equal(request.data.keyboard_id, "keyboard_test");
  assert.equal(ui.$("record-steps").children.length, 1);
  assert.match(
    ui.$("record-steps").children[0].children[0].textContent,
    /account no/i,
  );
  assert.equal(ui.$("record-live").hidden, false);
});

test("live Input Variable saves and types the default once while keyboard capture preserves fields", async () => {
  const ui = setup(true);
  await start(ui);
  await ui.fire("record-input-variable", "click");
  ui.$("record-variable-name").value = "amount";
  ui.$("record-variable-default").value = "88.50";
  ui.$("record-variable-keyboard").value = "__capture__";
  await ui.fire("record-variable-keyboard", "change");
  assert.equal(ui.$("record-variable-input").hidden, true);
  ui.$("keyboard-capture-name").value = "Amount keypad";
  ui.$("keyboard-capture-kind").value = "custom";
  ui.$("keyboard-custom-keys").value = "8,.,5,0";
  await ui.fire("keyboard-capture-start", "click");
  for (let index = 0; index < 4; index++)
    await ui.fire("video", "click", { clientX: 120 + index * 20 });
  await ui.fire("keyboard-capture-save", "click");
  assert.equal(ui.$("record-variable-input").hidden, false);
  assert.equal(ui.$("record-variable-name").value, "amount");
  assert.equal(ui.$("record-variable-default").value, "88.50");
  assert.equal(ui.$("record-variable-keyboard").value, "keyboard_test");
  ui.context.window.phoneRecorder.physicalBusyChanged(true, "Sending...");
  assert.equal(ui.$("record-variable-keyboard").value, "keyboard_test");
  ui.context.window.phoneRecorder.physicalBusyChanged(false, "");
  await ui.fire("record-variable-type", "click");
  const request = ui.calls.filter(
    (entry) => entry.url === "/api/recorder/variable-input",
  ).at(-1);
  assert.equal(request.data.type_sample, true);
  assert.equal(request.data.sample, "88.50");
  assert.equal(request.data.variable.default, "88.50");
  assert.equal(request.data.variable.name, "amount");
  assert.equal(request.data.amount_format, "raw");
  assert.equal(ui.$("record-live").hidden, false);
});

test("Add & Type Now shows a busy state and rejects a duplicate submission", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("record-capture-keyboard", "click");
  ui.$("keyboard-capture-name").value = "Account keypad";
  ui.$("keyboard-capture-kind").value = "custom";
  ui.$("keyboard-custom-keys").value = "1";
  await ui.fire("keyboard-capture-start", "click");
  await ui.fire("workflow-preview", "click");
  await ui.fire("keyboard-capture-save", "click");
  await ui.fire("record-input-variable", "click");
  ui.$("record-variable-name").value = "account_no";
  ui.$("record-variable-default").value = "1";
  ui.delayVariableInput();
  const first = ui.$("record-variable-type").listeners.click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.$("record-variable-type").disabled, true);
  assert.equal(ui.$("record-variable-type").textContent, "Typing now...");
  ui.$("record-variable-type").listeners.click();
  assert.equal(
    ui.calls.filter((call) => call.url === "/api/recorder/variable-input").length,
    1,
  );
  ui.finishVariableInput();
  await first;
});

test("Add & Type Now rejects an empty default before physical input", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("record-input-variable", "click");
  ui.$("record-variable-name").value = "account_no";
  ui.$("record-variable-default").value = "";
  await ui.fire("record-variable-type", "click");
  assert.match(ui.$("workflow-notice").textContent, /Enter a Default value/);
  assert.equal(
    ui.calls.filter((call) => call.url === "/api/recorder/variable-input")
      .length,
    0,
  );
});

test("review mode can add another keyboard without changing existing references", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("record-capture-keyboard", "click");
  ui.$("keyboard-capture-name").value = "Account keypad";
  ui.$("keyboard-capture-kind").value = "custom";
  ui.$("keyboard-custom-keys").value = "1";
  await ui.fire("keyboard-capture-start", "click");
  await ui.fire("workflow-preview", "click");
  await ui.fire("keyboard-capture-save", "click");
  ui.key("1");
  await ui.fire("record-stop", "click");
  await ui.fire("review-capture-keyboard", "click");
  ui.$("keyboard-capture-name").value = "Amount keypad";
  ui.$("keyboard-custom-keys").value = ".";
  await ui.fire("keyboard-capture-start", "click");
  await ui.fire("workflow-preview", "click");
  await ui.fire("keyboard-capture-save", "click");
  assert.equal(ui.$("captured-keyboard-list").children.length, 2);
  assert.equal(ui.$("review-flow").hidden, false);
});


test("ROI picker drags normalized calibrated rectangle, tests crop, and saves configuration", async () => {
  const ui = setup(true);
  await start(ui);
  await ui.fire("record-stop", "click");
  ui.$("insert-position").value = "0";
  ui.$("record-text-value").value = "Ready";
  ui.$("record-text-timeout").value = "10";
  await ui.fire("record-text", "click");
  const edit = async () => ui.$("record-steps").children[0].children.find(c => c.className === "flow-buttons")
    .children.find(c => c.textContent === "Edit").listeners.click();
  await edit();
  await ui.fire("step-roi-refresh", "click");
  const canvas = ui.$("step-roi-canvas");
  assert.equal(canvas.hidden, false);
  canvas.listeners.pointerdown({button: 0, clientX: 100, clientY: 250, pointerId: 1, preventDefault() {}, stopPropagation() {}});
  canvas.listeners.pointermove({clientX: 300, clientY: 450});
  canvas.listeners.pointerup({});
  assert.match(ui.$("step-roi-values").textContent, /Y: 0.5000/);
  await ui.fire("step-roi-test", "click");
  assert.match(ui.$("step-roi-result").textContent, /Target detected/);
  const tested = ui.calls.find(c => c.url === "/api/ocr/roi-test");
  assert.deepEqual(tested.data, {token: "snapshot", roi: {x: 0, y: .5, width: 1, height: .5}, text: "Ready"});
  await ui.fire("step-editor", "submit");
  assert.deepEqual(ui.calls.filter(c => c.data?.operation === "fields").at(-1).data.fields.roi,
    {x: 0, y: .5, width: 1, height: .5});
  await edit();
  assert.equal(ui.$("step-text-advanced").open, true);
  ui.$("step-text-advanced").open = false;
  await ui.fire("step-editor", "submit");
  assert.deepEqual(
    ui.calls.filter(c => c.data?.operation === "fields").at(-1).data.fields.roi,
    {x: 0, y: .5, width: 1, height: .5},
  );
  await edit();
  ui.$("step-roi-enabled").checked = false;
  await ui.fire("step-editor", "submit");
  assert.equal(ui.calls.filter(c => c.data?.operation === "fields").at(-1).data.fields.roi, null);
});

test("recording wait uses the shared Region OCR picker schema and clear behavior", async () => {
  const ui = setup(true);
  await start(ui);
  await ui.fire("record-stop", "click");
  ui.$("insert-position").value = "0";
  ui.$("record-text-value").value = "Ready";
  ui.$("record-text-timeout").value = "10";
  assert.notEqual(ui.$("record-text-advanced").open, true);
  ui.$("record-text-advanced").open = true;
  await ui.fire("record-roi-refresh", "click");
  const canvas = ui.$("record-roi-canvas");
  canvas.listeners.pointerdown({button: 0, clientX: 130, clientY: 334, pointerId: 1, preventDefault() {}, stopPropagation() {}});
  canvas.listeners.pointermove({clientX: 282, clientY: 350});
  canvas.listeners.pointerup({});
  await ui.fire("record-roi-test", "click");
  assert.match(ui.$("record-roi-result").textContent, /Target detected/);
  ui.$("record-text-advanced").open = false;
  await ui.fire("record-text", "click");
  assert.deepEqual(
    ui.calls.filter((call) => call.data?.operation === "insert").at(-1).data.step,
    {
      type: "wait_for_text",
      text: "Ready",
      timeout_seconds: 10,
      roi: {x: .15, y: .71, width: .76, height: .04},
    },
  );

  ui.$("insert-position").value = "1";
  ui.$("record-text-value").value = "Full screen";
  await ui.fire("record-roi-refresh", "click");
  canvas.listeners.pointerdown({button: 0, clientX: 100, clientY: 50, pointerId: 2, preventDefault() {}, stopPropagation() {}});
  canvas.listeners.pointermove({clientX: 200, clientY: 250});
  canvas.listeners.pointerup({});
  await ui.fire("record-roi-clear", "click");
  assert.equal(ui.$("record-roi-enabled").checked, false);
  await ui.fire("record-text", "click");
  assert.deepEqual(
    ui.calls.filter((call) => call.data?.operation === "insert").at(-1).data.step,
    {type: "wait_for_text", text: "Full screen", timeout_seconds: 10},
  );
});

test("Edit and Recorder mount one shared Region OCR component implementation", () => {
  assert.equal(source.match(/function createRegionOCRPicker\(/g)?.length, 1);
  assert.equal(templateSource.match(/id="region-ocr-template"/g)?.length, 1);
  assert.equal(templateSource.match(/data-roi-control="canvas"/g)?.length, 1);
  for (const key of [
    "enable",
    "instructions",
    "selectCurrent",
    "clear",
    "width",
    "height",
    "test",
  ])
    assert.match(templateSource, new RegExp(`data-roi-i18n="workflow\\.roi\\.${key}"`));
  assert.match(source, /workflowUiText\(\s*label,\s*label\.dataset\.roiI18n/);
  assert.match(source, /createRegionOCRPicker\("step", "step"\)/);
  assert.match(source, /createRegionOCRPicker\("record", "record"\)/);
  assert.match(source, /createRegionOCRPicker\("condition", "condition"\)/);
  assert.match(source, /createRegionOCRPicker\("expected", "expected"\)/);
});

test("recording and review share a responsive workspace with a sticky phone pane", () => {
  assert.match(
    templateSource,
    /id="control-workspace" class="workspace control-workspace"/,
  );
  assert.match(templateSource, /id="workflow-phone-pane"/);
  assert.match(templateSource, /id="workflow-authoring-slot"/);
  assert.ok(
    templateSource.indexOf('id="workflow-phone-pane"') <
      templateSource.indexOf('id="record-card"'),
  );
  assert.match(
    source,
    /\$\("workflow-authoring-slot"\)\.append\(\s*\$\("review-flow"\),\s*\$\("expected-text-editor"\)/,
  );
  assert.match(
    styleSource,
    /\.control-workspace\.is-workflow-authoring > \.workflow-phone-pane\s*\{[^}]*position: sticky;[^}]*top: 16px;/s,
  );
  assert.match(
    styleSource,
    /@media \(max-width: 950px\)[\s\S]*?\.control-workspace\.is-workflow-authoring > \.workflow-phone-pane\s*\{[^}]*position: static;/,
  );
  assert.match(
    styleSource,
    /\.control-workspace\.is-workflow-authoring > aside\s*\{[^}]*grid-template-columns: minmax\(0, 1fr\);/,
  );
});

test("Add Special Case during main recording routes actions and returns to main recording", async () => {
  const ui = setup();
  await start(ui);
  await ui.fire("record-alternate-screen", "click");
  assert.equal(ui.$("alternate-screen-setup").hidden, false);

  ui.$("condition-text-value").value = "Extra verification required";
  ui.$("condition-text-timeout").value = "0.8";
  ui.$("condition-roi-enabled").checked = true;
  ui.$("condition-roi-x").value = "0.1";
  ui.$("condition-roi-y").value = "0.2";
  ui.$("condition-roi-width").value = "0.7";
  ui.$("condition-roi-height").value = "0.3";
  await ui.fire("alternate-screen-start", "click");

  const startAlternate = ui.calls.find(
    (call) => call.url === "/api/recorder/alternate-screen/start",
  );
  assert.deepEqual(startAlternate.data, {
    text: "Extra verification required",
    timeout_seconds: 0.8,
    roi: { x: 0.1, y: 0.2, width: 0.7, height: 0.3 },
  });
  assert.equal(ui.$("alternate-screen-active").hidden, false);
  assert.equal(ui.$("record-alternate-screen").hidden, true);
  assert.match(ui.$("record-status").textContent, /Recording Special Case/);

  await ui.fire("workflow-preview", "pointerdown", {
    clientX: 150,
    clientY: 350,
  });
  await ui.fire("workflow-preview", "pointerup", {
    clientX: 150,
    clientY: 350,
  });
  assert.match(ui.$("alternate-screen-status").textContent, /1 recorded action/);
  assert.equal(
    ui.$("record-steps").children[0].children[0].textContent,
    'Special Case — “Extra verification required”',
  );
  assert.match(
    ui.$("record-steps").children[0].children[1].children[0].textContent,
    /If not shown: Continue normally/,
  );

  await ui.fire("alternate-screen-finish", "click");
  assert.equal(ui.$("alternate-screen-active").hidden, true);
  assert.equal(ui.$("record-alternate-screen").hidden, false);

  await ui.fire("workflow-preview", "pointerdown");
  await ui.fire("workflow-preview", "pointerup");
  assert.equal(ui.$("record-steps").children.length, 2);

  await ui.fire("record-alternate-screen", "click");
  ui.$("condition-text-value").value = "Temporary screen";
  ui.$("condition-text-timeout").value = "0.8";
  await ui.fire("alternate-screen-start", "click");
  assert.equal(ui.$("record-steps").children.length, 3);
  await ui.fire("alternate-screen-cancel", "click");
  assert.equal(ui.$("record-steps").children.length, 2);
  assert.equal(ui.$("record-alternate-screen").hidden, false);

  await ui.fire("record-stop", "click");
  const conditionActions = ui.$("record-steps").children[0].children.at(-1);
  await conditionActions.children
    .find((child) => child.textContent === "Edit")
    .listeners.click();
  assert.equal(ui.$("step-text-options").hidden, false);
  ui.$("step-text-value").value = "Changed alternate screen";
  await ui.fire("step-editor", "submit", { preventDefault() {} });
  assert.equal(
    ui.$("record-steps").children[0].children[0].textContent,
    'Special Case — “Changed alternate screen”',
  );

  assert.match(templateSource, /id="record-alternate-screen"[^>]*>\s*Add Special Case/);
  assert.doesNotMatch(templateSource, /Add Another Screen|>Another Screen</);
  assert.doesNotMatch(templateSource, /id="insert-target"/);
  assert.doesNotMatch(templateSource, />\s*(?:IF|THEN|ELSE)\s*</);
});

test("open screen-text setup can be replaced by transfer-method branch drafts", async () => {
  const existingBankStep = {
    id: "bank_existing",
    type: "tap",
    x: 0.25,
    y: 0.3,
  };
  const ui = setup(false, {
    draft: {
      active: false,
      editing_existing: true,
      workflow: {
        id: "flow_transfer_branches",
        name: "Transfer branches",
        purpose: "payment",
        variables: [],
        keyboards: {},
        steps: [
          { id: "main_tap", type: "tap", x: 0.5, y: 0.5 },
          {
            id: "method_switch",
            type: "switch_transfer_method",
            branches: {
              bank_account: [existingBankStep],
            },
          },
        ],
      },
    },
  });
  await new Promise((resolve) => setImmediate(resolve));

  const row = (id) =>
    ui.$("record-steps").children.find((item) => item.dataset.stepId === id);
  const branchButton = (methodIndex) => {
    const branches = row("method_switch").children.find(
      (item) => item.className === "condition-branches",
    );
    return branches.children[methodIndex].children.at(-1);
  };
  const clickButton = async (button) => {
    await button.listeners.click();
    await new Promise((resolve) => setImmediate(resolve));
  };

  const mainActions = row("main_tap").children.at(-1);
  await clickButton(
    mainActions.children.find(
      (button) => button.textContent === "+ Add Special Case",
    ),
  );
  ui.$("condition-text-value").value = "Temporary screen-text draft";
  const beforeReplacement = ui.draft().workflow.steps;

  await clickButton(branchButton(0));
  assert.equal(ui.$("condition-source").value, "withdrawal_api");
  assert.equal(ui.$("condition-transfer-method").value, "bank_account");
  assert.deepEqual(ui.draft().workflow.steps, beforeReplacement);
  assert.doesNotMatch(
    ui.$("workflow-notice").textContent,
    /Finish or cancel the current Special Case setup first/,
  );

  await ui.fire("alternate-screen-start", "click");
  let starts = ui.calls.filter(
    (call) => call.url === "/api/recorder/alternate-screen/start",
  );
  assert.deepEqual(starts[0].data, {
    source: "withdrawal_api",
    transfer_method: "bank_account",
    condition_id: "method_switch",
  });
  await ui.fire("workflow-preview", "pointerdown", {
    clientX: 150,
    clientY: 350,
  });
  await ui.fire("workflow-preview", "pointerup", {
    clientX: 150,
    clientY: 350,
  });
  await ui.fire("alternate-screen-finish", "click");

  await clickButton(branchButton(1));
  assert.equal(ui.$("condition-transfer-method").value, "phone_no");
  await ui.fire("alternate-screen-start", "click");

  const guardedEmailButton = branchButton(2);
  await clickButton(guardedEmailButton);
  assert.match(
    ui.$("workflow-notice").textContent,
    /Finish the current Special Case before adding another one/,
  );
  assert.equal(ui.$("condition-transfer-method").value, "phone_no");

  await ui.fire("workflow-preview", "pointerdown", {
    clientX: 250,
    clientY: 350,
  });
  await ui.fire("workflow-preview", "pointerup", {
    clientX: 250,
    clientY: 350,
  });
  await ui.fire("alternate-screen-finish", "click");

  starts = ui.calls.filter(
    (call) => call.url === "/api/recorder/alternate-screen/start",
  );
  assert.deepEqual(
    starts.map((call) => call.data.transfer_method),
    ["bank_account", "phone_no"],
  );
  const methodSwitch = ui
    .draft()
    .workflow.steps.find((step) => step.id === "method_switch");
  assert.deepEqual(
    methodSwitch.branches.bank_account.map((step) => step.id),
    ["bank_existing", "step_3"],
  );
  assert.deepEqual(
    methodSwitch.branches.phone_no.map((step) => step.id),
    ["step_3"],
  );
});

test("review Special Case records only nested actions and returns safely to review", async () => {
  const ui = setup();
  await start(ui);
  for (const x of [150, 250]) {
    await ui.fire("workflow-preview", "pointerdown", { clientX: x, clientY: 350 });
    await ui.fire("workflow-preview", "pointerup", { clientX: x, clientY: 350 });
  }
  await ui.fire("record-stop", "click");
  const originalIds = ui.$("record-steps").children.map(
    (row) => row.dataset.stepId,
  );
  const firstActions = ui.$("record-steps").children[0].children.at(-1);
  await firstActions.children
    .find((button) => button.textContent === "+ Add Special Case")
    .listeners.click();
  assert.equal(ui.$("alternate-screen-setup").hidden, false);

  ui.$("condition-text-value").value = "Increase limit";
  ui.$("condition-text-timeout").value = "0.8";
  await ui.fire("alternate-screen-start", "click");
  const started = ui.calls
    .filter((call) => call.url === "/api/recorder/alternate-screen/start")
    .at(-1);
  assert.equal(started.data.after_step_id, originalIds[0]);
  assert.equal(ui.$("review-flow").hidden, true);
  assert.equal(ui.$("record-stop").hidden, true);

  await ui.fire("workflow-preview", "pointerdown", {
    clientX: 150,
    clientY: 350,
  });
  await ui.fire("workflow-preview", "pointerup", {
    clientX: 150,
    clientY: 350,
  });
  await ui.fire("workflow-preview", "pointerdown", {
    clientX: 150,
    clientY: 350,
  });
  await ui.fire("workflow-preview", "pointerup", {
    clientX: 250,
    clientY: 150,
  });
  assert.match(ui.$("alternate-screen-status").textContent, /2 recorded actions/);
  await ui.fire("record-undo", "click");
  assert.match(ui.$("alternate-screen-status").textContent, /1 recorded action/);
  await ui.context.window.phoneRecorder.refreshAfterReconnect();
  assert.match(ui.$("record-status").textContent, /Recording Special Case/);

  const callsBeforeFinish = ui.calls.filter(
    (call) => call.url === "/api/recorder/step",
  ).length;
  await ui.fire("alternate-screen-finish", "click");
  assert.equal(ui.$("review-flow").hidden, false);
  assert.equal(ui.$("record-live").hidden, true);
  assert.deepEqual(
    ui.$("record-steps").children.map((row) => row.children[0].textContent),
    ["Tap", 'Special Case — “Increase limit”', "Tap"],
  );
  await ui.fire("workflow-preview", "pointerdown");
  await ui.fire("workflow-preview", "pointerup");
  assert.equal(
    ui.calls.filter((call) => call.url === "/api/recorder/step").length,
    callsBeforeFinish,
  );

  const lastActions = ui.$("record-steps").children[2].children.at(-1);
  await lastActions.children
    .find((button) => button.textContent === "+ Add Special Case")
    .listeners.click();
  ui.$("condition-text-value").value = "Temporary warning";
  await ui.fire("alternate-screen-start", "click");
  await ui.fire("workflow-preview", "pointerdown");
  await ui.fire("workflow-preview", "pointerup");
  await ui.fire("alternate-screen-cancel", "click");
  assert.equal(ui.$("review-flow").hidden, false);
  assert.deepEqual(
    ui.$("record-steps").children.map((row) => row.children[0].textContent),
    ["Tap", 'Special Case — “Increase limit”', "Tap"],
  );
  assert.match(
    ui.$("workflow-notice").textContent,
    /Actions already performed on the phone were not reversed/,
  );
});
