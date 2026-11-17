"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const styles = readFileSync(path.join(__dirname, "../static/style.css"), "utf8");
const start = source.indexOf("const deviceService =");
const end = source.indexOf("function fresh()", start);
const deviceSource = source.slice(start, end).replace(
  "const deviceService =",
  "globalThis.deviceService =",
);

function element(tag = "div") {
  return {
    tag,
    hidden: false,
    disabled: false,
    textContent: "",
    value: "",
    className: "",
    dataset: {},
    style: {},
    children: [],
    listeners: {},
    classList: {
      add() {},
      remove() {},
      toggle() {},
    },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    focus() { this.focused = true; },
    scrollIntoView() { this.scrolled = true; },
  };
}

function setup() {
  const nodes = new Map();
  const calls = [];
  const timers = [];
  const notices = [];
  const context = vm.createContext({
    deviceCatalog: null,
    deviceCameraDetails: null,
    deviceRefreshInFlight: false,
    deviceRefreshTimer: null,
    runtimeStatusTimer: null,
    deviceRenderSignature: null,
    devicePollingStopped: false,
    deviceDeletesInFlight: new Set(),
    devicePreviewTimers: new Map(),
    devicePreviewGenerations: new Map(),
    devicePreviewLastFrames: new Map(),
    cameraSettingsDeviceId: null,
    deviceRefreshLastSuccessAt: null,
    deviceRefreshError: "",
    deviceStatusStale: false,
    addPhonePanelOpen: false,
    addPhoneEnrollmentTarget: null,
    addPhoneInitialDeviceIds: new Set(),
    deviceWithdrawDetails: {},
    jobCatalog: [],
    remoteControlHistory: [],
    jobCancelsInFlight: new Set(),
    jobRetriesInFlight: new Set(),
    withdrawReconnectsInFlight: new Set(),
    balanceRefreshesInFlight: new Set(),
    workReadinessRequestsInFlight: new Set(),
    returnedDeviceId: null,
    workflowSettingsDeviceId: null,
    workflowSettingsData: null,
    workflowSettingsMode: "payment",
    withdrawSettingsDeviceId: null,
    withdrawSettingsAssigned: false,
    withdrawSettingsReauthentication: false,
    withdrawCancelDeviceId: null,
    withdrawCancelPendingReason: null,
    withdrawCancelsInFlight: new Set(),
    ocrSettingsDirty: false,
    ocrStatusTimer: null,
    ocrStatusInFlight: false,
    ocrPollingStopped: false,
    ocrRecommendedWorkerCount: null,
    recoveryPanelOpen: false,
    recoveryRequestInFlight: false,
    recoveryTargetJobId: null,
    manualAssistanceOpen: false,
    readyVerificationJobId: null,
    readyVerificationFailedJobId: null,
    readyVerificationMessage: "",
    readyVerificationRestoreManual: false,
    readyVerificationRestoreInFlight: false,
    readyVerificationRestorePromise: null,
    readyVerificationFinishInFlight: false,
    ocrOperationalHealthy: true,
    workflowRuntime: null,
    connectionState: "connected",
    DEVICE_REFRESH_MS: 1000,
    DEVICE_STATUS_STALE_MS: 5000,
    RUNTIME_STATUS_MS: 2000,
    state: {
      manual_backend: "raspberry_pi",
      pi: { api: "online", phone: "connected" },
      device: { serial: "pi-test" },
      camera: { ready: true },
      calibrated: true,
    },
    calibrating: false,
    manual: false,
    controlVersion: 0,
    api: async (url, data, method) => {
      calls.push({ url, data, method });
      if (url.endsWith("/withdraw/login-assign"))
        return {
          assigned: true,
          operator_id: 7,
          username: "bot-operator01",
          payment_records: ["5", "2b"],
          credentials_saved: false,
        };
      if (url.endsWith("/work-readiness"))
        return { ok: true, ready_for_work: data.ready };
      return { ok: true };
    },
    $: (id) => {
      if (!nodes.has(id)) nodes.set(id, element());
      return nodes.get(id);
    },
    document: {
      body: { dataset: {} },
      createElement: element,
      hidden: false,
      activeElement: null,
    },
    window: { confirm: () => true },
    encodeURIComponent,
    clearTimeout(id) {
      if (id && timers[id - 1]) timers[id - 1].cancelled = true;
    },
    setTimeout(callback, delay) {
      timers.push({ callback, delay });
      return timers.length;
    },
    refreshStatus: async () => {},
    render() {},
    notice(message) { notices.push(message); },
    reportFrontendDiagnostic() {},
  });
  vm.runInContext(deviceSource, context);
  return { context, nodes, calls, timers, notices };
}

function phoneCardActionLabels(card) {
  const group = card.children.at(-1).children.find(
    (child) => child.className === "device-job-actions",
  );
  return group ? group.children.map((button) => button.textContent) : [];
}

test("device service keeps management calls in the shared API layer", async () => {
  const { context, calls } = setup();
  await context.deviceService.startEnrollment("phone_C");
  await context.deviceService.remove("branch-phone/1");
  await context.deviceService.updateCamera("phone_B", {
    camera_source: 1,
    rotation: 90,
    enabled: true,
  });
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    {
      url: "/api/enrollment/start",
      data: { target_device_id: "phone_C" },
    },
    {
      url: "/api/devices/branch-phone%2F1",
      data: {},
      method: "DELETE",
    },
    {
      url: "/api/devices/phone_B/camera",
      data: { camera_source: 1, rotation: 90, enabled: true },
      method: "PUT",
    },
  ]);
});

test("manual balance refresh uses the per-device Withdraw endpoint", async () => {
  const { context, calls } = setup();
  await context.deviceService.refreshBalance("phone A");
  assert.equal(
    calls.at(-1).url,
    "/api/devices/phone%20A/withdraw/balance/refresh",
  );
  assert.deepEqual(Object.keys(calls.at(-1).data), []);
  assert.equal(calls.at(-1).method, undefined);
});

test("work readiness uses the per-device persistent gate endpoint", async () => {
  const { context, calls } = setup();
  await context.deviceService.setWorkReadiness("phone A", false);
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1))), {
    url: "/api/devices/phone%20A/work-readiness",
    data: { ready: false },
    method: "PUT",
  });
});

test("device card exposes normal operator work status and toggle", async () => {
  const { context, nodes, calls } = setup();
  context.deviceCatalog = {
    default_device: "phone_A",
    selected_device: "phone_A",
    devices: [{
      id: "phone_A",
      name: "Main phone",
      connected: true,
      default: true,
      ready_for_work: false,
    }],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = {
    phone_A: {
      config: { camera_source: 0, rotation: 0, enabled: true },
      status: { ready: true },
    },
  };
  context.loadDevices = async () => {};

  context.renderDevices();
  const card = nodes.get("device-list").children[0];
  const status = card.children.find(
    (child) => child.className.includes("device-work-readiness"),
  );
  assert.equal(status.children[0].textContent, "Work status");
  assert.equal(status.children[1].textContent, "● Not Ready");
  assert.equal(status.children[2].textContent, "Mark Ready");

  await status.children[2].listeners.click();
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1))), {
    url: "/api/devices/phone_A/work-readiness",
    data: { ready: true },
    method: "PUT",
  });
  assert.equal(context.deviceCatalog.devices[0].ready_for_work, true);
});

test("Add Phone starts enrollment and never calls Replace", async () => {
  const { context, calls } = setup();
  await context.deviceService.startEnrollment("phone_B");
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    {
      url: "/api/enrollment/start",
      data: { target_device_id: "phone_B" },
    },
  ]);
});

test("Delete is shown for returned devices and Replace is not exposed", () => {
  const { context, nodes } = setup();
  context.deviceCatalog = {
    default_device: "phone_A",
    selected_device: "phone_A",
    devices: [
      { id: "phone_A", name: "Main phone", connected: true,
        bluetooth_address: "AA:BB:CC:DD:EE:FF", default: true },
    ],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = {
    phone_A: {
      config: { camera_source: 0, rotation: 90, enabled: true },
      status: { ready: true },
    },
  };
  context.renderDevices();
  const cards = nodes.get("device-list").children;
  assert.equal(cards.length, 1);
  const actionButtons = cards[0].children.at(-1).children;
  assert.deepEqual(
    actionButtons.map((button) => button.textContent),
    ["Controlling", "Camera Settings", "Delete"],
  );
  assert.equal(
    actionButtons.some((button) => button.textContent === "Replace"),
    false,
  );
  assert.equal(
    cards[0].children.some((child) => child.className === "device-camera-settings"),
    false,
  );
  assert.equal(
    actionButtons.some((button) => button.textContent === "Camera Settings"),
    true,
  );
  assert.equal(cards[0].children[3].className, "device-preview");
});

test("device card shows Withdraw Account assignment and Login & Assign safely", async () => {
  const { context, nodes, calls } = setup();
  context.deviceCatalog = {
    default_device: "phone_A",
    selected_device: "phone_A",
    devices: [
      {
        id: "phone_A",
        name: "Main phone",
        connected: true,
        bluetooth_address: "AA:BB:CC:DD:EE:FF",
        default: true,
      },
    ],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = {
    phone_A: {
      config: { camera_source: 0, rotation: 0, enabled: true },
      status: { ready: true },
    },
  };
  context.deviceWithdrawDetails = {
    phone_A: {
      assigned: false,
      credentials_saved: false,
      withdraw_api: {
        operator_id: null,
        username: null,
        payment_records: [],
      },
    },
  };
  context.renderDevices();
  let card = nodes.get("device-list").children[0];
  let summary = card.children.find(
    (child) => child.className.includes("withdraw-account-summary"),
  );
  assert.equal(summary.children[0].textContent, "Withdraw Account");
  assert.equal(summary.children[1].textContent, "Account Status: Not Assigned");
  assert.equal(summary.children.at(-1).textContent, "Login & Assign");
  summary.children.at(-1).listeners.click();
  assert.equal(nodes.get("withdraw-settings-dialog").hidden, false);

  nodes.get("withdraw-settings-username").value = "typed-username";
  nodes.get("withdraw-settings-password").value = "private-password";
  nodes.get("withdraw-settings-totp").value = "123456";
  context.state.manual_backend = "appium";
  await context.saveWithdrawSettings({ preventDefault() {} });
  const request = calls.find((call) => call.url.endsWith("/withdraw/login-assign"));
  assert.deepEqual(JSON.parse(JSON.stringify(request.data)), {
    username: "typed-username",
    password: "private-password",
    g2f_code: "123456",
  });
  assert.equal(nodes.get("withdraw-settings-password").value, "");
  assert.equal(nodes.get("withdraw-settings-totp").value, "");

  card = nodes.get("device-list").children[0];
  summary = card.children.find(
    (child) => child.className.includes("withdraw-account-summary"),
  );
  assert.equal(summary.children[1].textContent, "Account Status: Assigned");
  assert.match(summary.children[2].textContent, /bot-operator01.*Account ID: 7/);
  assert.equal(summary.children[3].textContent, "Payment Records: 5, 2b");
  assert.equal(
    summary.children[4].textContent,
    "Session: Authentication required · Realtime: Disconnected",
  );
  assert.equal(summary.children[5].textContent, "Login Again");
  assert.equal(summary.children.at(-1).textContent, "Change Account");
  assert.doesNotMatch(summary.textContent || "", /private-password|123456/);
  const balanceSummary = card.children.find((child) =>
    child.className.includes("device-balance-summary"),
  );
  assert.equal(balanceSummary.children.at(-2).textContent, "Refresh Balance");
  assert.equal(balanceSummary.children.at(-2).disabled, true);
  assert.equal(
    balanceSummary.children.at(-1).textContent,
    "Assign a Balance Check Workflow first.",
  );

  context.deviceWithdrawDetails.phone_A.status = {
    state: "IDLE",
    session_state: "READY",
    authentication_available: true,
    reconciliation_complete: true,
    physical_socket_connected: true,
    payment_ready: false,
    payment_readiness_error:
      "Add a Capture Screenshot step after the success check.",
  };
  context.renderDevices();
  summary = nodes
    .get("device-list")
    .children[0].children.find((child) =>
      child.className.includes("withdraw-account-summary"),
    );
  assert.match(summary.children[4].textContent, /Session: Connected/);
  assert.match(
    summary.children[5].textContent,
    /Payment readiness: Not ready/,
  );
  assert.equal(
    summary.children[6].textContent,
    "Add a Capture Screenshot step after the success check.",
  );
  assert.equal(
    summary.children.some((child) => child.textContent === "Reconnect"),
    false,
  );

  context.deviceWithdrawDetails.phone_A.status.payment_ready = true;
  context.deviceWithdrawDetails.phone_A.status.payment_readiness_error = null;
  context.renderDevices();
  summary = nodes
    .get("device-list")
    .children[0].children.find((child) =>
      child.className.includes("withdraw-account-summary"),
    );
  assert.equal(summary.children[5].textContent, "Payment readiness: Ready");

  context.deviceWithdrawDetails.phone_A.status = {
    state: "NEEDS_ATTENTION",
    session_state: "READY",
    authentication_available: true,
    reconciliation_complete: true,
    physical_socket_connected: true,
    phase: "NEEDS_ATTENTION",
    job_id: "failed-payment-job",
  };
  context.renderDevices();
  summary = nodes
    .get("device-list")
    .children[0].children.find((child) =>
      child.className.includes("withdraw-account-summary"),
    );
  assert.equal(
    summary.children[4].textContent,
    "Session: Connected · Realtime: Connected",
  );
  assert.equal(
    summary.children.some((child) => child.textContent === "Reconnect"),
    false,
  );

  context.deviceWithdrawDetails.phone_A.status = {
    state: "WAITING_FOR_CONNECTION",
    session_state: "DISCONNECTED",
    authentication_available: true,
    reconciliation_complete: false,
    physical_socket_connected: false,
  };
  context.renderDevices();
  summary = nodes
    .get("device-list")
    .children[0].children.find((child) =>
      child.className.includes("withdraw-account-summary"),
    );
  assert.equal(
    summary.children[4].textContent,
    "Session: Reconnecting · Realtime: Disconnected",
  );
  assert.equal(
    summary.children.some((child) => child.textContent === "Reconnect"),
    true,
  );

  context.deviceWithdrawDetails.phone_A.status = {
    state: "AUTH_FAILED",
    session_state: "DISCONNECTED",
    authentication_available: false,
    reconciliation_complete: false,
    physical_socket_connected: false,
  };
  context.renderDevices();
  summary = nodes
    .get("device-list")
    .children[0].children.find((child) =>
      child.className.includes("withdraw-account-summary"),
    );
  assert.match(summary.children[4].textContent, /Authentication required/);
  summary.children.find((child) => child.textContent === "Login Again").listeners.click();
  assert.equal(nodes.get("withdraw-settings-username").value, "bot-operator01");
  assert.equal(nodes.get("withdraw-settings-username").readOnly, true);
  assert.equal(nodes.get("withdraw-settings-submit").textContent, "Login Again");
  nodes.get("withdraw-settings-password").value = "fresh-password";
  nodes.get("withdraw-settings-totp").value = "654321";
  await context.saveWithdrawSettings({ preventDefault() {} });
  const relogin = calls.filter((call) =>
    call.url.endsWith("/withdraw/login-assign"),
  ).at(-1);
  assert.deepEqual(JSON.parse(JSON.stringify(relogin.data)), {
    username: "bot-operator01",
    password: "fresh-password",
    g2f_code: "654321",
    reauthenticate: true,
  });
  assert.equal(
    calls.some((call) => call.url.endsWith("/withdraw/reconcile")),
    false,
  );
});

test("Withdraw runtime actions distinguish live transport from missing or expired authentication", () => {
  const { context } = setup();

  assert.equal(
    context.withdrawRuntimeView({
      status: {
        state: "IDLE",
        session_state: "READY",
        authentication_available: true,
        physical_socket_connected: true,
      },
    }).action,
    null,
  );
  assert.equal(
    context.withdrawRuntimeView({
      status: {
        state: "WAITING_FOR_CONNECTION",
        session_state: "DISCONNECTED",
        authentication_available: true,
        physical_socket_connected: false,
      },
    }).action,
    "reconnect",
  );
  assert.equal(
    context.withdrawRuntimeView({
      status: {
        state: "DISABLED",
        session_state: "DISABLED",
        authentication_available: false,
        physical_socket_connected: false,
      },
    }).action,
    "login",
  );
  assert.equal(
    context.withdrawRuntimeView({
      status: {
        state: "WAITING_FOR_CONNECTION",
        session_state: "DISCONNECTED",
        authentication_available: false,
        physical_socket_connected: false,
      },
    }).action,
    "login",
  );
  assert.equal(
    context.withdrawRuntimeView({
      status: {
        state: "AUTH_FAILED",
        session_state: "DISCONNECTED",
        authentication_available: false,
        physical_socket_connected: false,
      },
    }).action,
    "login",
  );
});

test("worker recovery state overrides a waiting job's misleading Ready state", () => {
  const { context, nodes } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [{ id: "phone_A", name: "Phone A", connected: true }],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = {
    phone_A: {
      config: { camera_source: 0, enabled: true },
      status: { ready: true },
    },
  };
  context.jobCatalog = [{
    job_id: "retry_waiting",
    device_id: "phone_A",
    action: "withdraw_v2",
    status: "WAITING_FOR_DEVICE",
    recovery_status: null,
  }];
  context.deviceWithdrawDetails = {
    phone_A: {
      status: {
        state: "NEEDS_ATTENTION",
        phase: "NEEDS_ATTENTION",
        job_id: "retry_waiting",
        session_state: "READY",
        reconciliation_complete: true,
        physical_socket_connected: true,
      },
    },
  };

  context.renderDevices();

  const card = nodes.get("device-list").children[0];
  assert.equal(card.dataset.state, "needs-attention");
  const jobSummary = card.children.find((child) =>
    child.className.includes("device-job-summary"),
  );
  assert.equal(jobSummary.children[0].textContent, "Phone state: Needs Attention");
  assert.equal(jobSummary.children[1].textContent, "Current job: WAITING_FOR_DEVICE");
});

test("active withdrawal exposes a reason-coded Cancel Withdrawal confirmation", async () => {
  const { context, nodes, calls, notices } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [
      { id: "phone_A", name: "Phone A", connected: true },
      { id: "phone_B", name: "Phone B", connected: true },
    ],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = {
    phone_A: { config: {}, status: {} },
    phone_B: { config: {}, status: {} },
  };
  context.deviceWithdrawDetails = {
    phone_A: {
      assigned: true,
      balance_workflow_id: "balance-a",
      withdraw_api: { operator_id: 7, username: "operator-a" },
      status: {
        phase: "LOCKED",
        withdraw_id: 935402,
        tx_id: "TX9F3K2",
        job_id: null,
        cancel_allowed: true,
      },
    },
    phone_B: { status: { phase: null, withdraw_id: null } },
  };
  context.loadDevices = async () => {};
  context.renderDevices();

  const [phoneA, phoneB] = nodes.get("device-list").children;
  assert.deepEqual(phoneCardActionLabels(phoneA), ["Cancel Withdrawal"]);
  assert.deepEqual(phoneCardActionLabels(phoneB), []);

  const cancel = phoneA.children.at(-1).children
    .find((child) => child.className === "device-job-actions")
    .children[0];
  cancel.listeners.click();
  assert.equal(nodes.get("withdraw-cancel-dialog").hidden, false);
  assert.equal(nodes.get("withdraw-cancel-confirm").disabled, true);

  nodes.get("withdraw-cancel-reason").value = "invalid_account";
  context.updateWithdrawCancelConfirm();
  assert.equal(nodes.get("withdraw-cancel-confirm").disabled, false);
  await context.confirmWithdrawCancel({ preventDefault() {} });

  assert.equal(nodes.get("withdraw-cancel-dialog").hidden, true);
  assert.equal(nodes.get("withdraw-cancel-final-dialog").hidden, false);
  assert.equal(
    calls.filter((call) => call.url.endsWith("/withdraw/cancel")).length,
    0,
  );
  await context.confirmWithdrawCancelFinal();

  const request = calls.find((call) => call.url.endsWith("/withdraw/cancel"));
  assert.deepEqual(JSON.parse(JSON.stringify(request)), {
    url: "/api/devices/phone_A/withdraw/cancel",
    data: { reason: "invalid_account" },
  });
  assert.equal(nodes.get("withdraw-cancel-dialog").hidden, true);
  assert.equal(notices.at(-1), "Withdrawal cancelled successfully.");
});

test("Cancel Withdrawal modal has only predefined issue reasons and no free text", () => {
  const template = readFileSync(
    path.join(__dirname, "../templates/index.html"),
    "utf8",
  );
  const start = template.indexOf('id="withdraw-cancel-dialog"');
  const end = template.indexOf('id="workflow-settings-dialog"', start);
  const modal = template.slice(start, end);
  const reasons = [...modal.matchAll(/<option value="([^"]+)">([^<]+)<\/option>/g)]
    .map((match) => [match[1], match[2].trim()]);
  assert.deepEqual(reasons, [
    ["invalid_account", "Invalid account number"],
    ["bank_rejected", "Bank rejected payment"],
    ["unable_to_complete", "Unable to complete payment"],
  ]);
  assert.doesNotMatch(modal, /<input|<textarea/);
  assert.match(modal, /id="withdraw-cancel-confirm"[\s\S]*?disabled/);
  assert.match(modal, /Cancel this withdrawal\?/);
  assert.doesNotMatch(modal, /payment was NOT submitted/);
  assert.match(modal, /cancel the withdrawal externally/);
  assert.match(modal, /Yes, Cancel Withdrawal/);
});

test("all eligible withdrawals require final UI confirmation and Back sends no request", async () => {
  const { context, nodes, calls } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [{ id: "phone_A", name: "Phone A", connected: true }],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = { phone_A: { config: {}, status: {} } };
  context.deviceWithdrawDetails = {
    phone_A: {
      status: {
        phase: "NEEDS_ATTENTION",
        withdraw_id: 935402,
        tx_id: "TX9F3K2",
        job_id: "job_failed",
        cancel_allowed: true,
      },
    },
  };
  context.loadDevices = async () => {};
  context.openWithdrawCancel({ id: "phone_A", name: "Phone A" });
  nodes.get("withdraw-cancel-reason").value = "unable_to_complete";

  await context.confirmWithdrawCancel({ preventDefault() {} });
  assert.equal(nodes.get("withdraw-cancel-dialog").hidden, true);
  assert.equal(nodes.get("withdraw-cancel-final-dialog").hidden, false);
  assert.equal(
    calls.filter((call) => call.url.endsWith("/withdraw/cancel")).length,
    0,
  );

  context.closeWithdrawCancelFinal({ back: true });
  assert.equal(nodes.get("withdraw-cancel-dialog").hidden, false);
  assert.equal(nodes.get("withdraw-cancel-final-dialog").hidden, true);
  assert.equal(
    calls.filter((call) => call.url.endsWith("/withdraw/cancel")).length,
    0,
  );
});

test("failed withdrawal confirmation submits reason exactly once", async () => {
  const { context, nodes, calls } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [{ id: "phone_A", name: "Phone A", connected: true }],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = { phone_A: { config: {}, status: {} } };
  context.deviceWithdrawDetails = {
    phone_A: {
      status: {
        phase: "NEEDS_ATTENTION",
        withdraw_id: 935402,
        tx_id: "TX9F3K2",
        job_id: "job_failed",
        cancel_allowed: true,
      },
    },
  };
  context.loadDevices = async () => {};
  context.openWithdrawCancel({ id: "phone_A", name: "Phone A" });
  nodes.get("withdraw-cancel-reason").value = "unable_to_complete";
  await context.confirmWithdrawCancel({ preventDefault() {} });

  const first = context.confirmWithdrawCancelFinal();
  const duplicate = context.confirmWithdrawCancelFinal();
  await Promise.all([first, duplicate]);

  const requests = calls.filter((call) => call.url.endsWith("/withdraw/cancel"));
  assert.equal(requests.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(requests[0].data)), {
    reason: "unable_to_complete",
  });
});

test("Cancel Withdrawal displays the backend external cancellation error", async () => {
  const { context, nodes } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [{ id: "phone_A", name: "Phone A", connected: true }],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = { phone_A: { config: {}, status: {} } };
  context.deviceWithdrawDetails = {
    phone_A: { status: { cancel_allowed: true } },
  };
  context.loadDevices = async () => {};
  context.api = async () => {
    throw new Error(
      "External cancellation failed (APIError code=409: external lock was lost).",
    );
  };
  context.deviceService.cancelWithdrawal = (deviceId, reason) =>
    context.api(
      `/api/devices/${encodeURIComponent(deviceId)}/withdraw/cancel`,
      { reason },
    );

  context.openWithdrawCancel({ id: "phone_A", name: "Phone A" });
  nodes.get("withdraw-cancel-reason").value = "bank_rejected";
  await context.confirmWithdrawCancel({ preventDefault() {} });
  await context.confirmWithdrawCancelFinal();

  assert.equal(
    nodes.get("withdraw-cancel-error").textContent,
    "External cancellation failed (APIError code=409: external lock was lost).",
  );
  assert.equal(nodes.get("withdraw-cancel-dialog").hidden, false);
});

test("device cards retain stale balances and distinguish balance states", () => {
  const { context, nodes } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: ["phone_A", "phone_B", "phone_C", "phone_D", "phone_E"].map((id) => ({
      id,
      name: id,
      connected: true,
    })),
    enrollment: { active: false },
  };
  context.deviceCameraDetails = Object.fromEntries(
    context.deviceCatalog.devices.map(({ id }, index) => [
      id,
      {
        config: { camera_source: index, rotation: 0, enabled: true },
        status: { ready: true },
      },
    ]),
  );
  context.deviceWithdrawDetails = {
    phone_A: {
      assigned: true,
      balance_workflow_id: "balance-a",
      withdraw_api: { operator_id: 7, username: "operator-a" },
      status: {
        balance_workflow_ready: true,
        balance_state: "VALID",
        balance_valid: true,
        last_balance: "8200",
        balance_observed_at: "2026-10-06T08:32:00Z",
      },
    },
    phone_B: {
      status: {
        balance_state: "VALID",
        balance_valid: true,
        last_balance: "1550.2",
        balance_observed_at: "2026-10-06T08:33:00Z",
      },
    },
    phone_C: {
      status: {
        balance_state: "UNKNOWN",
        balance_valid: false,
        last_balance: null,
        balance_observed_at: null,
      },
    },
    phone_D: {
      status: {
        balance_state: "STALE",
        balance_valid: false,
        last_balance: "9999.99",
        balance_observed_at: "2026-10-06T08:00:00Z",
      },
    },
    phone_E: {
      status: {
        balance_state: "VALID",
        balance_valid: true,
        last_balance: "0.00",
        balance_observed_at: "2026-10-06T08:34:00Z",
      },
    },
  };

  context.renderDevices();
  const balances = nodes.get("device-list").children.map((card) =>
    card.children.find((child) => child.className.includes("device-balance-summary")),
  );
  assert.doesNotMatch(balances[0].className, /admin-only/);
  assert.equal(balances[0].children[1].textContent, "8,200.00");
  assert.match(balances[0].children[2].textContent, /^Last checked: /);
  assert.equal(balances[0].children[3].textContent, "Refresh Balance");
  assert.equal(balances[1].children[1].textContent, "1,550.20");
  assert.equal(balances[2].children[1].textContent, "Unavailable");
  assert.equal(balances[2].children.length, 2);
  assert.equal(balances[3].children[1].textContent, "9,999.99");
  assert.equal(balances[3].children[2].textContent, "Stale");
  assert.match(balances[3].children[3].textContent, /^Last checked: /);
  assert.equal(balances[4].children[1].textContent, "0.00");
});

test("refreshing balance retains the previous amount or shows Checking", () => {
  const { context } = setup();
  const retained = context.payerBalanceView({
    status: {
      balance_state: "REFRESHING",
      last_balance: "616.50",
      balance_observed_at: "2026-10-06T08:32:00Z",
    },
  });
  assert.equal(retained.value, "616.50");
  assert.equal(retained.stateLabel, "Refreshing…");
  assert.ok(retained.checkedAt);
  assert.deepEqual(
    JSON.parse(JSON.stringify(context.payerBalanceView({
      status: { balance_state: "REFRESHING", last_balance: null },
    }))),
    { value: "Checking…", checkedAt: null, stateLabel: null },
  );
});

test("device polling refreshes balance status without triggering balance work", async () => {
  const { context, nodes, calls } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [{ id: "phone_A", name: "Phone A", connected: true }],
    enrollment: { active: false },
  };
  let poll = 0;
  context.api = async (url, data, method) => {
    calls.push({ url, data, method });
    if (url === "/api/devices") return context.deviceCatalog;
    if (url.endsWith("/camera")) return {
      config: { camera_source: 0, rotation: 0, enabled: true },
      status: { ready: true },
    };
    if (url.endsWith("/withdraw")) {
      poll += 1;
      return {
        status: {
          balance_state: "VALID",
          balance_valid: true,
          last_balance: poll === 1 ? "100.00" : "250.50",
          balance_observed_at: `2026-10-06T08:3${poll}:00Z`,
        },
      };
    }
    if (url === "/api/jobs") return { jobs: [] };
    throw new Error(`Unexpected request: ${url}`);
  };

  await context.loadDevices();
  let balance = nodes.get("device-list").children[0].children.find(
    (child) => child.className.includes("device-balance-summary"),
  );
  assert.equal(balance.children[1].textContent, "100.00");
  await context.loadDevices();
  balance = nodes.get("device-list").children[0].children.find(
    (child) => child.className.includes("device-balance-summary"),
  );
  assert.equal(balance.children[1].textContent, "250.50");
  assert.equal(calls.filter((call) => call.url.endsWith("/withdraw")).length, 2);
  assert.equal(
    calls.some((call) => /balance|workflow-runs/.test(call.url)),
    false,
  );
});

test("workflow assignment UI displays names while API saves workflow IDs", async () => {
  const { context, nodes, calls } = setup();
  const persisted = {
    device_id: "phone_A",
    unrestricted: false,
    configured: true,
    allowed_workflow_ids: ["flow_testing"],
    balance_workflow_id: "flow_uob",
    balance_workflow: {
      id: "flow_uob",
      name: "UOB",
      purpose: "balance_check",
      missing: false,
      balance_check_eligible: true,
    },
    missing_workflow_ids: [],
    available_workflows: [
      { id: "flow_testing", name: "Testing", purpose: "payment", balance_check_eligible: false },
      { id: "flow_maybank", name: "Maybank", purpose: "payment", balance_check_eligible: false },
      { id: "flow_uob", name: "UOB", purpose: "balance_check", balance_check_eligible: true },
      { id: "flow_balance_2", name: "Balance 2", purpose: "balance_check", balance_check_eligible: true },
      { id: "flow_transfer", name: "Unsafe Balance", purpose: "balance_check", balance_check_eligible: false },
    ],
  };
  context.api = async (url, data, method) => {
    calls.push({ url, data, method });
    if (method === "GET") return structuredClone(persisted);
    persisted.allowed_workflow_ids = [...data.workflow_ids];
    persisted.balance_workflow_id = data.balance_workflow_id;
    persisted.balance_workflow = persisted.available_workflows.find(
      (workflow) => workflow.id === data.balance_workflow_id,
    );
    return structuredClone(persisted);
  };

  await context.openWorkflowSettings({ id: "phone_A", name: "Phone A" });
  const options = nodes.get("workflow-settings-list").children;
  assert.equal(options.length, 2);
  assert.equal(options[0].children[1].children[0].textContent, "Testing");
  assert.equal(options[0].dataset.workflowId, "flow_testing");
  assert.equal(options[0].children[0].checked, true);
  assert.equal(options[1].children[1].children[0].textContent, "Maybank");
  assert.equal(nodes.get("workflow-settings-payment-section").hidden, false);
  assert.equal(nodes.get("workflow-settings-balance-section").hidden, true);
  const balanceOptions = nodes.get("workflow-settings-balance-list").children;
  assert.deepEqual(
    balanceOptions.map((option) => option.dataset.workflowId),
    ["", "flow_uob", "flow_balance_2"],
  );
  assert.equal(balanceOptions[1].children[0].checked, true);

  options[0].children[0].checked = false;
  options[1].children[0].checked = true;
  context.loadDevices = async () => {};
  context.window.phoneRecorder = {
    workflowAssignmentsChanged: async () => {},
  };
  await context.saveWorkflowSettings({ preventDefault() {} });
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1))), {
    url: "/api/devices/phone_A/workflows",
    data: {
      workflow_ids: ["flow_maybank"],
      balance_workflow_id: "flow_uob",
    },
    method: "PUT",
  });

  await context.openWorkflowSettings(
    { id: "phone_A", name: "Phone A" },
    "balance",
  );
  assert.equal(nodes.get("workflow-settings-payment-section").hidden, true);
  assert.equal(nodes.get("workflow-settings-balance-section").hidden, false);
  const reopenedBalanceOptions = nodes.get("workflow-settings-balance-list").children;
  reopenedBalanceOptions[1].children[0].checked = false;
  reopenedBalanceOptions[2].children[0].checked = true;
  await context.saveWorkflowSettings({ preventDefault() {} });
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1))), {
    url: "/api/devices/phone_A/workflows",
    data: {
      workflow_ids: ["flow_maybank"],
      balance_workflow_id: "flow_balance_2",
    },
    method: "PUT",
  });

  // Opening the modal again represents a page/modal reload: selections must
  // be reconstructed from the persisted API response, not retained DOM state.
  await context.openWorkflowSettings(
    { id: "phone_A", name: "Phone A" },
    "balance",
  );
  assert.equal(
    nodes.get("workflow-settings-balance-list").children[2].children[0].checked,
    true,
  );
  assert.match(
    readFileSync(path.join(__dirname, "../templates/index.html"), "utf8"),
    /id="workflow-settings-dialog"[\s\S]*id="workflow-settings-search"/,
  );
  assert.match(
    readFileSync(path.join(__dirname, "../templates/index.html"), "utf8"),
    /Payment Workflow[\s\S]*Balance Check Workflow[\s\S]*id="workflow-settings-balance-list"/,
  );
});

test("device card exposes independent Payment and Balance Check assignments", () => {
  const { context, nodes } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [
      {
        id: "phone_A",
        name: "Phone A",
        connected: true,
        workflow_assignment: {
          unrestricted: false,
          assigned_workflows: [
            { id: "flow_payment", name: "Hong Leong Transfer", missing: false },
          ],
          balance_workflow: {
            id: "flow_balance",
            name: "Hong Leong Check Balance",
            missing: false,
          },
        },
      },
    ],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = {
    phone_A: { config: {}, status: { ready: true } },
  };

  context.renderDevices();
  const card = nodes.get("device-list").children[0];
  const summary = card.children.find((child) =>
    child.children.some((item) => item.textContent === "Payment Workflow"),
  );
  assert.ok(summary);
  assert.equal(summary.children[0].textContent, "Payment Workflow");
  assert.equal(summary.children[1].textContent, "Hong Leong Transfer");
  assert.equal(summary.children[4].textContent, "Balance Check Workflow");
  assert.equal(summary.children[5].textContent, "Hong Leong Check Balance");
  assert.equal(summary.children[6].textContent, "Change");

  context.deviceCatalog.devices[0].workflow_assignment.balance_workflow = null;
  context.renderDevices();
  const unassigned = nodes
    .get("device-list")
    .children[0].children.find((child) =>
      child.children.some((item) => item.textContent === "Payment Workflow"),
    );
  assert.equal(unassigned.children[5].textContent, "Not assigned");
  assert.equal(unassigned.children[6].textContent, "Assign");
});

test("all camera previews remain visible when control selection changes", () => {
  const { context, nodes } = setup();
  context.deviceCatalog = {
    default_device: "phone_A",
    selected_device: "phone_A",
    devices: [
      { id: "phone_A", name: "Phone A", connected: true },
      { id: "phone_B", name: "Phone B", connected: false },
    ],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = {
    phone_A: { config: { camera_source: 0, rotation: 90, enabled: true }, status: { ready: false } },
    phone_B: { config: { camera_source: 1, rotation: 0, enabled: true }, status: { ready: true } },
  };
  context.renderDevices();
  assert.equal(nodes.get("device-list").children.length, 2);
  assert.equal(nodes.get("device-list").children[0].children[3].className, "device-preview");
  assert.equal(nodes.get("device-list").children[1].children[3].className, "device-preview");
  assert.match(nodes.get("device-list").children[0].children[1].children[0].textContent, /Connected/);
  assert.match(nodes.get("device-list").children[0].children[1].children[1].textContent, /Unavailable/);
  assert.match(nodes.get("device-list").children[1].children[1].children[0].textContent, /Disconnected/);
  assert.match(nodes.get("device-list").children[1].children[1].children[1].textContent, /Available/);

  context.deviceCatalog.selected_device = "phone_B";
  context.renderDevices();
  assert.equal(nodes.get("device-list").children.length, 2);
  assert.match(nodes.get("device-list").children[1].className, /selected/);
  assert.equal(nodes.get("device-list").children[0].children[3].className, "device-preview");
});

test("device preview URLs and unavailable state remain isolated per device", async () => {
  const { context, nodes } = setup();
  const fetched = [];
  context.document.createElement = (tag) => {
    const node = element(tag);
    if (tag === "img") node.decode = async () => {};
    node.append = function (...children) {
      for (const child of children) child.parentElement = this;
      this.children.push(...children);
    };
    node.removeAttribute = function (name) { delete this[name]; };
    return node;
  };
  context.URL = {
    createObjectURL: (blob) => `blob:${blob.deviceId}`,
    revokeObjectURL() {},
  };
  context.AbortSignal = { timeout: () => ({}) };
  context.Image = class {
    constructor() {
      this.naturalWidth = 438;
      this.naturalHeight = 996;
    }
    async decode() {}
  };
  context.requestAnimationFrame = (callback) => callback();
  context.fetch = async (url) => {
    fetched.push(url);
    if (url.includes("phone_A")) {
      return {
        ok: false,
        async json() { return { error: "Camera unavailable" }; },
      };
    }
    return {
      ok: true,
      async blob() { return { deviceId: "phone_B" }; },
    };
  };
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [
      { id: "phone_A", name: "Phone A", connected: true },
      { id: "phone_B", name: "Phone B", connected: true },
    ],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = {
    phone_A: {
      config: { camera_id: "avfoundation:rog", rotation: 90, enabled: true },
      status: { ready: true },
    },
    phone_B: {
      config: { camera_id: "avfoundation:macbook", rotation: 0, enabled: true },
      status: { ready: true },
    },
  };

  context.renderDevices();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));

  const cards = nodes.get("device-list").children;
  const previewA = cards[0].children[3];
  const previewB = cards[1].children[3];
  const imageA = previewA.children[0];
  const imageB = previewB.children[0];
  assert.equal(fetched.some((url) =>
    /\/api\/devices\/phone_A\/camera\/preview\?t=/.test(url)), true);
  assert.equal(fetched.some((url) =>
    /\/api\/devices\/phone_B\/camera\/preview\?t=/.test(url)), true);

  assert.equal(imageA.hidden, true);
  assert.equal(previewA.children[1].hidden, false);
  assert.equal(previewA.children[1].textContent, "Camera unavailable");
  assert.equal(imageB.hidden, false);
  assert.equal(imageB.src, "blob:phone_B");
  assert.equal(previewB.children[1].hidden, true);
});

test("device cards expose independent ready, running, recovering, attention, and offline states", () => {
  const { context, nodes } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [
      { id: "phone_A", name: "Phone A", connected: true },
      { id: "phone_B", name: "Phone B", connected: true },
      { id: "phone_C", name: "Phone C", connected: true },
      { id: "phone_D", name: "Phone D", connected: true },
      { id: "phone_E", name: "Phone E", connected: false },
    ],
    enrollment: { active: false },
  };
  context.jobCatalog = [
    {
      job_id: "failed_A",
      device_id: "phone_A",
      status: "FAILED",
      recovery_status: "NEEDS_ATTENTION",
      error: 'Text "Terms of Use" was not detected within 10 seconds.',
    },
    {
      job_id: "failed_B",
      device_id: "phone_B",
      status: "INTERRUPTED",
      recovery_status: "NEEDS_ATTENTION",
    },
    { job_id: "running_C", device_id: "phone_C", status: "RUNNING" },
    {
      job_id: "recovery_D",
      device_id: "phone_D",
      status: "FAILED",
      recovery_status: "RECOVERING",
    },
  ];
  context.renderDevices();

  const cards = nodes.get("device-list").children;
  assert.match(cards[0].className, /device-card--needs-attention/);
  assert.match(cards[1].className, /device-card--needs-attention/);
  assert.match(cards[2].className, /device-card--running/);
  assert.match(cards[3].className, /device-card--recovering/);
  assert.match(cards[4].className, /device-card--offline/);
  assert.match(
    cards[0].children[0].children[2].children.at(-1).textContent,
    /⚠ Needs Attention/,
  );
  assert.match(
    phoneCardActionLabels(cards[0]).join(" "),
    /Manual Assistance/,
  );
  const attentionActions = cards[0].children.at(-1).children.find(
    (child) => child.className === "device-job-actions",
  );
  assert.match(attentionActions.children[0].className, /danger-action/);
  assert.match(styles, /device-card--needs-attention[\s\S]*?border: 3px solid/);
  assert.equal(cards[0].children[0].children[2].children.at(-1).textContent,
    "⚠ Needs Attention");
  assert.match(
    cards[0].children.find((child) => child.className === "device-job-summary")
      .children[1].textContent,
    /FAILED/,
  );
  assert.equal(
    cards[4].children[0].children[2].children.at(-1).textContent,
    "Offline",
  );
  for (const state of ["running", "recovering", "needs-attention", "offline"])
    assert.match(styles, new RegExp(`device-card--${state}`));
});

test("phone cards expose only the next valid job actions", () => {
  const { context, nodes } = setup();
  const cases = [
    ["attention", "FAILED", "NEEDS_ATTENTION", 4, "withdraw_v2", ["Manual Assistance", "Retry from Failed Step", "Restart Workflow"]],
    ["normal_attention", "FAILED", "NEEDS_ATTENTION", 4, null, ["Manual Assistance"]],
    ["ready_step", "FAILED", "READY", 4, "withdraw_v2", ["Retry from Failed Step", "Restart Workflow"]],
    ["ready_restart", "FAILED", "READY", null, "withdraw_v2", ["Restart Workflow"]],
    ["interrupted", "INTERRUPTED", "READY", 2, "withdraw_v2", ["Retry from Failed Step", "Restart Workflow"]],
    ["running", "RUNNING", null, null, null, ["Cancel"]],
    ["queued", "QUEUED", null, null, null, ["Cancel"]],
    ["waiting", "WAITING_FOR_DEVICE", null, null, null, ["Cancel"]],
    ["completed", "COMPLETED", null, null, null, []],
    ["cancelled", "CANCELLED", null, null, null, []],
  ];
  context.deviceCatalog = {
    selected_device: "phone_attention",
    devices: cases.map(([id]) => ({ id: `phone_${id}`, connected: true })),
    enrollment: { active: false },
  };
  context.jobCatalog = cases.map(
    ([id, status, recovery_status, failed_step_index, action]) => ({
      job_id: `job_${id}`,
      device_id: `phone_${id}`,
      status,
      recovery_status,
      action,
      ...(Number.isInteger(failed_step_index) ? { failed_step_index } : {}),
    }),
  );
  context.renderDevices();
  nodes.get("device-list").children.forEach((card, index) => {
    assert.deepEqual(phoneCardActionLabels(card), cases[index][5]);
  });
});

test("terminal Withdraw jobs do not expose retry or restart after reload", () => {
  const { context, nodes } = setup();
  const cases = [
    ["cancelled_current", "CANCELLED"],
    ["cancelled_history", "CANCELLED"],
    ["confirmed", "CONFIRMED"],
    ["unresolved", "NEEDS_ATTENTION"],
    ["ordinary", null],
  ];
  context.deviceCatalog = {
    selected_device: "phone_cancelled_current",
    devices: cases.map(([id]) => ({ id: `phone_${id}`, connected: true })),
    enrollment: { active: false },
  };
  context.jobCatalog = cases.map(([id, phase]) => ({
    job_id: `job_${id}`,
    device_id: `phone_${id}`,
    action: id === "ordinary" ? null : "withdraw_v2",
    status: "FAILED",
    recovery_status: "READY",
    failed_step_index: 4,
    ...(phase
      ? {
          withdrawal_phase: phase,
          withdrawal_terminal: ["CANCELLED", "CONFIRMED"].includes(phase),
          withdrawal_retryable: !["CANCELLED", "CONFIRMED"].includes(phase),
        }
      : {}),
  }));

  context.renderDevices();

  const labels = nodes.get("device-list").children.map(phoneCardActionLabels);
  assert.deepEqual(labels[0], []);
  assert.deepEqual(labels[1], []);
  assert.deepEqual(labels[2], []);
  assert.deepEqual(labels[3], ["Retry from Failed Step", "Restart Workflow"]);
  assert.deepEqual(labels[4], ["Retry from Failed Step", "Restart Workflow"]);
});

test("failed Withdraw job can expose Cancel Withdrawal in Needs Attention", () => {
  const { context, nodes } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [{ id: "phone_A", name: "Phone A", connected: true }],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = { phone_A: { config: {}, status: {} } };
  context.jobCatalog = [{
    job_id: "job_failed",
    device_id: "phone_A",
    action: "withdraw_v2",
    external_ref: "TX9F3K2",
    status: "FAILED",
    recovery_status: "NEEDS_ATTENTION",
    failed_step_index: 2,
  }];
  context.deviceWithdrawDetails = {
    phone_A: {
      status: {
        phase: "NEEDS_ATTENTION",
        withdraw_id: 935402,
        tx_id: "TX9F3K2",
        job_id: "job_failed",
        cancel_allowed: true,
      },
    },
  };

  context.renderDevices();

  assert.deepEqual(phoneCardActionLabels(nodes.get("device-list").children[0]), [
    "Manual Assistance",
    "Retry from Failed Step",
    "Restart Workflow",
    "Cancel Withdrawal",
  ]);
});

test("phone card uses the newest job for its fixed device", () => {
  const { context, nodes } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [
      { id: "phone_A", connected: true },
      { id: "phone_B", connected: true },
    ],
    enrollment: { active: false },
  };
  context.jobCatalog = [
    {
      job_id: "old_failed_A",
      device_id: "phone_A",
      status: "FAILED",
      recovery_status: "READY",
      failed_step_index: 3,
    },
    { job_id: "running_B", device_id: "phone_B", status: "RUNNING" },
    { job_id: "new_queued_A", device_id: "phone_A", status: "QUEUED" },
  ];
  context.renderDevices();
  const [phoneA, phoneB] = nodes.get("device-list").children;
  assert.deepEqual(phoneCardActionLabels(phoneA), ["Cancel"]);
  assert.deepEqual(phoneCardActionLabels(phoneB), ["Cancel"]);
  assert.match(
    phoneA.children.find((child) => child.className === "device-job-summary")
      .children[1].textContent,
    /QUEUED/,
  );
});

test("zh-HK completed job status and result never expose an i18n key", () => {
  const { context, nodes } = setup();
  context.window.PhoneI18n = {
    t(key, params = {}) {
      const values = {
        "device.lastJob": "上一項工作：{status}",
        "device.result": "結果：{result}",
      };
      return (values[key] || key).replace(
        /\{([^}]+)\}/g,
        (match, name) => params[name] ?? match,
      );
    },
    translateStatus(status) {
      return status === "COMPLETED" ? "已完成" : status;
    },
    translateEnglish(text) {
      return text === "Workflow completed." ? "工作流程已完成。" : text;
    },
    bindText(node, key, params) {
      node.textContent = this.t(
        key,
        typeof params === "function" ? params() : params,
      );
      return node;
    },
    bindComputedText(node, compute) {
      node.textContent = compute();
      return node;
    },
  };
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [{ id: "phone_A", name: "Phone A", connected: true }],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = {
    phone_A: { config: { enabled: true }, status: { ready: true } },
  };
  context.jobCatalog = [
    {
      job_id: "completed_A",
      device_id: "phone_A",
      status: "COMPLETED",
      result: "Workflow completed.",
    },
  ];

  context.renderDevices();
  const summary = nodes.get("device-list").children[0].children.find((child) =>
    child.className.includes("device-job-summary"),
  );
  const text = summary.children.map((child) => child.textContent).join(" ");
  assert.match(text, /上一項工作：已完成/);
  assert.match(text, /結果：工作流程已完成。/);
  assert.doesNotMatch(text, /status\.COMPLETED/);
});

test("active withdrawal does not present an unrelated historical job", () => {
  const { context, nodes } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [{ id: "phone_A", name: "Phone A", connected: true }],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = { phone_A: { config: {}, status: {} } };
  context.jobCatalog = [{
    job_id: "old_mock_retry",
    device_id: "phone_A",
    action: "withdraw_v2",
    status: "COMPLETED",
    external_ref: "MOCK-TX-OLD",
    result: "Workflow completed.",
  }];
  context.deviceWithdrawDetails = {
    phone_A: {
      status: {
        state: "NEEDS_ATTENTION",
        phase: "NEEDS_ATTENTION",
        withdraw_id: 1309,
        tx_id: "test-wd-aabb291",
        error:
          "External lock result is uncertain. Reconcile this withdrawal before retrying.",
      },
    },
  };

  context.renderDevices();

  const summary = nodes.get("device-list").children[0].children.find((child) =>
    child.className.includes("device-job-summary"),
  );
  assert.equal(summary.children[0].textContent, "Phone state: Needs Attention");
  assert.equal(summary.children[1].textContent, "Current job: —");
  assert.equal(
    summary.children[2].textContent,
    "Withdrawal lock status is uncertain. Reconcile before retrying.",
  );
  assert.equal(summary.children.some((child) => /COMPLETED/.test(child.textContent)), false);
});

test("active withdrawal associates only a job with the same transaction identity", () => {
  const { context } = setup();
  context.jobCatalog = [
    {
      job_id: "old_mock_retry",
      device_id: "phone_A",
      action: "withdraw_v2",
      status: "COMPLETED",
      external_ref: "MOCK-TX-OLD",
    },
    {
      job_id: "current_staging_job",
      device_id: "phone_A",
      action: "withdraw_v2",
      status: "WAITING_FOR_DEVICE",
      external_ref: "test-wd-aabb291",
    },
  ];

  assert.equal(
    context.latestRelevantJobForDevice("phone_A", {
      phase: "QUEUED",
      withdraw_id: 1309,
      tx_id: "test-wd-aabb291",
    }).job_id,
    "current_staging_job",
  );

  assert.equal(
    context.latestRelevantJobForDevice("phone_A", {
      phase: "QUEUED",
      withdraw_id: 1309,
      tx_id: "test-wd-aabb291",
      job_id: "old_mock_retry",
    }),
    null,
  );
});

test("external lock uncertainty is not formatted as device busy", () => {
  const { context } = setup();
  assert.equal(
    context.friendlyOperationalError(
      "External lock result is uncertain. Reconcile this withdrawal before retrying.",
    ),
    "Withdrawal lock status is uncertain. Reconcile before retrying.",
  );
  assert.equal(
    context.friendlyOperationalError("Device already has a running job."),
    "Device busy",
  );
});

test("Manual Assistance binds the selected recovery context to the clicked device", async () => {
  const { context, nodes } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [
      { id: "phone_A", name: "Phone A", connected: true },
      { id: "phone_B", name: "Phone B", connected: true },
    ],
    enrollment: { active: false },
  };
  context.jobCatalog = [
    {
      job_id: "failed_A",
      device_id: "phone_A",
      status: "FAILED",
      recovery_status: "NEEDS_ATTENTION",
    },
    {
      job_id: "failed_B",
      device_id: "phone_B",
      status: "FAILED",
      recovery_status: "NEEDS_ATTENTION",
      recovery_workflow_id: "phone_B_return_ready",
    },
  ];
  const selected = [];
  context.selectDashboardDevice = async (deviceId) => {
    selected.push(deviceId);
    context.deviceCatalog.selected_device = deviceId;
    return true;
  };
  context.renderOperationalRecovery = () => {};
  context.renderDevices();

  const phoneB = nodes.get("device-list").children[1];
  const jobActions = phoneB.children.at(-1).children.find(
    (child) => child.className === "device-job-actions",
  );
  const assistance = jobActions.children.find(
    (button) => button.textContent === "Manual Assistance",
  );
  await assistance.listeners.click();

  assert.deepEqual(selected, ["phone_B"]);
  assert.equal(context.deviceCatalog.selected_device, "phone_B");
  assert.equal(context.recoveryTargetJobId, "failed_B");
  assert.equal(context.manualAssistanceOpen, true);
});

test("Manual Assistance eligibility is invariant across locale switches", () => {
  const { context, nodes } = setup();
  let locale = "en";
  const bindings = new Set();
  const labels = {
    en: { "device.manualAssistance": "Manual Assistance" },
    "zh-HK": { "device.manualAssistance": "手動協助" },
  };
  const updateBinding = (node) => {
    const binding = node.__testI18n;
    if (!binding) return;
    node.textContent =
      labels[locale][binding.key] ||
      labels.en[binding.key] ||
      binding.fallback ||
      binding.key;
  };
  context.window.PhoneI18n = {
    getLocale: () => locale,
    t: (key) => labels[locale][key] || labels.en[key] || key,
    translateEnglish: (text) => text,
    translateStatus: (status) => status,
    bindText(node, key) {
      node.__testI18n = { key };
      bindings.add(node);
      updateBinding(node);
      return node;
    },
    bindComputedText(node, compute) {
      node.textContent = compute();
      return node;
    },
    setLocale(nextLocale) {
      locale = nextLocale;
      for (const node of bindings) updateBinding(node);
    },
  };
  let managementGateReads = 0;
  context.window.phoneRecorder = {
    deviceManagementBlocked() {
      managementGateReads += 1;
      return false;
    },
  };
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [{ id: "phone_A", name: "Phone A", connected: true }],
    enrollment: { active: false },
  };
  context.jobCatalog = [
    {
      job_id: "failed_A",
      device_id: "phone_A",
      status: "FAILED",
      recovery_status: "NEEDS_ATTENTION",
    },
  ];
  const originalState = JSON.parse(
    JSON.stringify({
      state: context.state,
      deviceCatalog: context.deviceCatalog,
      jobCatalog: context.jobCatalog,
    }),
  );

  context.renderDevices();
  const card = nodes.get("device-list").children[0];
  const actions = card.children.at(-1).children.find(
    (child) => child.className === "device-job-actions",
  );
  const assistance = actions.children[0];
  assert.equal(assistance.textContent, "Manual Assistance");
  assert.equal(assistance.disabled, false);
  const initialGateReads = managementGateReads;

  context.window.PhoneI18n.setLocale("zh-HK");
  context.renderDevices({ presentationOnly: true });
  let localizedCard = nodes.get("device-list").children[0];
  let localizedActions = localizedCard.children.at(-1).children.find(
    (child) => child.className === "device-job-actions",
  );
  let localizedAssistance = localizedActions.children[0];
  assert.equal(localizedAssistance.textContent, "手動協助");
  assert.equal(localizedAssistance.disabled, false);
  assert.equal(managementGateReads, initialGateReads);

  context.window.PhoneI18n.setLocale("en");
  context.renderDevices({ presentationOnly: true });
  localizedCard = nodes.get("device-list").children[0];
  localizedActions = localizedCard.children.at(-1).children.find(
    (child) => child.className === "device-job-actions",
  );
  localizedAssistance = localizedActions.children[0];
  assert.equal(localizedAssistance.textContent, "Manual Assistance");
  assert.equal(localizedAssistance.disabled, false);
  assert.equal(managementGateReads, initialGateReads);
  assert.deepEqual(
    JSON.parse(
      JSON.stringify({
        state: context.state,
        deviceCatalog: context.deviceCatalog,
        jobCatalog: context.jobCatalog,
      }),
    ),
    originalState,
  );
});

test("resolving one device removes only its attention styling", () => {
  const { context, nodes } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [
      { id: "phone_A", name: "Phone A", connected: true },
      { id: "phone_B", name: "Phone B", connected: true },
    ],
    enrollment: { active: false },
  };
  context.jobCatalog = [
    {
      job_id: "failed_A",
      device_id: "phone_A",
      status: "FAILED",
      recovery_status: "READY",
      recovered_at: "now",
    },
    {
      job_id: "failed_B",
      device_id: "phone_B",
      status: "FAILED",
      recovery_status: "NEEDS_ATTENTION",
    },
  ];
  context.renderDevices();

  const [phoneA, phoneB] = nodes.get("device-list").children;
  assert.match(phoneA.className, /device-card--ready/);
  assert.doesNotMatch(phoneA.className, /needs-attention/);
  assert.equal(
    phoneA.children.at(-1).children.some(
      (group) => group.children?.some(
        (button) => button.textContent === "Manual Assistance",
      ),
    ),
    false,
  );
  assert.match(phoneB.className, /device-card--needs-attention/);
  assert.equal(
    phoneB.children.at(-1).children.some(
      (group) => group.children?.some(
        (button) => button.textContent === "Manual Assistance",
      ),
    ),
    true,
  );
});

test("device previews preserve rotated portrait and landscape geometry", () => {
  const { context, nodes } = setup();
  context.deviceCatalog = {
    default_device: "phone_A",
    selected_device: "phone_A",
    devices: [
      { id: "phone_A", name: "Portrait phone", connected: true },
      { id: "phone_B", name: "Landscape phone", connected: true },
    ],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = {
    phone_A: {
      config: { camera_source: 0, rotation: 90, enabled: true },
      status: { ready: true, width: 1080, height: 1920 },
    },
    phone_B: {
      config: { camera_source: 1, rotation: 0, enabled: true },
      status: { ready: true, width: 1920, height: 1080 },
    },
  };

  context.renderDevices();
  const [portrait, landscape] = nodes.get("device-list").children
    .map((card) => card.children[3]);
  assert.match(portrait.className, /portrait/);
  assert.equal(portrait.style.aspectRatio, "1080 / 1920");
  assert.equal(portrait.style.maxWidth, "259px");
  assert.match(landscape.className, /landscape/);
  assert.equal(landscape.style.aspectRatio, "1920 / 1080");
  assert.equal(landscape.style.maxWidth, "818px");
});

test("legacy payment routing UI is absent while Withdraw Account remains", async () => {
  const { context, nodes, calls } = setup();
  context.deviceCatalog = {
    default_device: "phone_A",
    selected_device: "phone_A",
    devices: [
      { id: "phone_A", name: "Phone A", connected: true },
      { id: "phone_B", name: "Phone B", connected: true },
    ],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = {
    phone_A: { config: { camera_source: 0, rotation: 0, enabled: true }, status: { ready: true } },
    phone_B: { config: { camera_source: 1, rotation: 0, enabled: true }, status: { ready: true } },
  };
  context.deviceWithdrawDetails = {
    phone_A: {
      assigned: false,
      withdraw_api: { operator_id: null, username: null, payment_records: [] },
    },
    phone_B: {
      assigned: true,
      withdraw_api: {
        operator_id: 12,
        username: "operator-B",
        payment_records: ["2b"],
      },
    },
  };
  context.renderDevices();
  const cards = nodes.get("device-list").children;
  const summaries = cards.map((card) =>
    card.children.find((child) =>
      child.className.includes("withdraw-account-summary"),
    ),
  );
  for (const card of cards)
    assert.equal(
      card.children.at(-1).children.some((button) =>
        /Payment Rout|Account Settings|Assign Account/.test(button.textContent),
      ),
      false,
    );
  assert.equal(summaries[0].children[0].textContent, "Withdraw Account");
  assert.equal(summaries[0].children[1].textContent, "Account Status: Not Assigned");
  assert.equal(summaries[0].children.at(-1).textContent, "Login & Assign");
  assert.equal(summaries[1].children[0].textContent, "Withdraw Account");
  assert.equal(summaries[1].children[1].textContent, "Account Status: Assigned");
  assert.match(summaries[1].children[2].textContent, /operator-B.*Account ID: 12/);
  assert.equal(summaries[1].children[3].textContent, "Payment Records: 2b");
  assert.match(
    summaries[1].children[4].textContent,
    /Session: Authentication required/,
  );
  assert.equal(summaries[1].children.at(-1).textContent, "Change Account");
  assert.doesNotMatch(summaries[0].textContent || "", /operator-B|12|2b/);
  context.api = async (url, data, method) => {
    calls.push({ url, data, method });
    if (url === "/api/devices") return context.deviceCatalog;
    if (url === "/api/jobs") return { jobs: [] };
    if (url.endsWith("/withdraw"))
      return context.deviceWithdrawDetails[url.includes("phone_B") ? "phone_B" : "phone_A"];
    return {
      config: { camera_source: 0, rotation: 0, enabled: true },
      status: { ready: true },
    };
  };
  await context.loadDevices();
  assert.equal(calls.some((call) => call.url === "/api/accounts"), false);
});

test("jobs view shows fixed account device and queue position", () => {
  const { context, nodes } = setup();
  context.jobCatalog = [{
    job_id: "job_123",
    account_id: "uob_account_1",
    device_id: "phone_A",
    workflow: "flow_demo",
    status: "QUEUED",
    queue_position: 2,
  }];
  context.renderJobs();
  const item = nodes.get("jobs-list").children[0];
  assert.equal(item.children[0].textContent, "job_123");
  assert.equal(item.children[2].textContent, "Device: phone_A");
  assert.equal(item.children[3].textContent, "Workflow: flow_demo");
  assert.match(item.children[4].textContent, /queue 2/);
});

test("phone card cancels eligible work through the job service", async () => {
  const { context, nodes, calls } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [{ id: "phone_A", name: "Phone A", connected: true }],
    enrollment: { active: false },
  };
  context.jobCatalog = [{
    job_id: "job_cancel",
    account_id: "account_1",
    device_id: "phone_A",
    workflow: "flow_demo",
    action: "balance_check",
    status: "WAITING_FOR_DEVICE",
    queue_position: 1,
  }];
  context.api = async (url, data, method) => {
    calls.push({ url, data, method });
    return { ...context.jobCatalog[0], status: "CANCELLED", queue_position: null };
  };
  context.renderDevices();
  const card = nodes.get("device-list").children[0];
  const group = card.children.at(-1).children.find(
    (child) => child.className === "device-job-actions",
  );
  const button = group.children[0];
  assert.equal(button.textContent, "Cancel");
  await button.listeners.click();
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [{
    url: "/api/jobs/job_cancel/cancel",
    data: {},
  }]);
  assert.equal(context.jobCatalog[0].status, "CANCELLED");
});

test("running job cancellation requires confirmation", async () => {
  const { context, nodes, calls } = setup();
  let confirmations = 0;
  context.window.confirm = () => {
    confirmations += 1;
    return false;
  };
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [{ id: "phone_A", name: "Phone A", connected: true }],
    enrollment: { active: false },
  };
  context.jobCatalog = [{
    job_id: "job_running",
    account_id: "account_1",
    device_id: "phone_A",
    workflow: "flow_demo",
    status: "RUNNING",
  }];
  context.renderDevices();
  const card = nodes.get("device-list").children[0];
  const group = card.children.at(-1).children.find(
    (child) => child.className === "device-job-actions",
  );
  const button = group.children[0];
  await button.listeners.click();
  assert.equal(confirmations, 1);
  assert.deepEqual(calls, []);
});

test("OCR status polling preserves an actively edited pool size", async () => {
  const { context, nodes, timers } = setup();
  const input = nodes.get("ocr-worker-pool-size") || context.$("ocr-worker-pool-size");
  input.value = "4";
  input.focused = true;
  context.document.activeElement = input;
  context.ocrSettingsDirty = true;
  context.api = async (url) => {
    if (url === "/api/ocr/settings") {
      return { worker_pool_size: 2, restart_required: false };
    }
    if (url === "/api/ocr/status") {
      return {
        pool_size: 2,
        busy_workers: 1,
        queued_requests: 0,
        restart_required: false,
        workers: [
          { worker_id: 1, state: "BUSY" },
          { worker_id: 2, state: "IDLE" },
        ],
      };
    }
    throw new Error(`Unexpected URL ${url}`);
  };

  await context.loadOCRSettings();

  assert.equal(input.value, "4");
  assert.equal(context.document.activeElement, input);
  assert.equal(nodes.get("ocr-pool-size").textContent, 2);
  assert.equal(nodes.get("ocr-busy-workers").textContent, 1);
  assert.equal(nodes.get("ocr-workers").children.length, 2);
  assert.equal(timers.at(-1).delay, 1500);
});

test("zh-HK runtime and OCR status render paths translate live values", () => {
  const { context, nodes } = setup();
  const translations = {
    "withdraw.authRequired": "需要重新驗證",
    "common.disconnected": "已中斷連線",
    "ocr.workerStatus": "工作程序 {number} — {state}",
  };
  const interpolate = (value, params = {}) =>
    value.replace(/\{([^}]+)\}/g, (match, name) => params[name] ?? match);
  context.window.PhoneI18n = {
    t(key, params) {
      return interpolate(translations[key] || key, params);
    },
    translateStatus(status) {
      return { IDLE: "閒置", BUSY: "忙碌中" }[status] || status;
    },
    translateEnglish(text) {
      return text === "Collecting recent OCR performance samples."
        ? "正在收集近期 OCR 效能樣本。"
        : text;
    },
    bindText(node, key, params) {
      node.textContent = this.t(
        key,
        typeof params === "function" ? params() : params,
      );
      return node;
    },
    bindComputedText(node, compute) {
      node.textContent = compute();
      return node;
    },
  };

  const runtime = context.withdrawRuntimeView({
    status: {
      state: "AUTH_FAILED",
      authentication_available: false,
      physical_socket_connected: false,
    },
  });
  assert.equal(runtime.session, "需要重新驗證");
  assert.equal(runtime.realtime, "已中斷連線");

  context.renderOCRStatus({
    pool_size: 2,
    busy_workers: 0,
    queued_requests: 0,
    recommendation_reason: "Collecting recent OCR performance samples.",
    workers: [
      { worker_id: 1, state: "IDLE" },
      { worker_id: 2, state: "IDLE" },
    ],
  });
  assert.equal(
    nodes.get("ocr-recommendation-reason").textContent,
    "正在收集近期 OCR 效能樣本。",
  );
  assert.deepEqual(
    nodes.get("ocr-workers").children.map((node) => node.textContent),
    ["工作程序 1 — 閒置", "工作程序 2 — 閒置"],
  );
});

test("runtime status scheduling does not stop OCR status polling", () => {
  const { context, timers } = setup();

  context.scheduleRuntimeStatusRefresh();

  assert.equal(context.ocrPollingStopped, false);
  assert.equal(timers.at(-1).delay, 2000);
});

test("Apply Recommendation saves through existing settings flow without restarting", async () => {
  const { context, nodes, calls } = setup();
  const input = nodes.get("ocr-worker-pool-size") || context.$("ocr-worker-pool-size");
  input.value = "2";
  context.api = async (url, data, method) => {
    calls.push({ url, data, method });
    if (url === "/api/ocr/settings") {
      return { worker_pool_size: 3, restart_required: true };
    }
    if (url === "/api/ocr/status") {
      return {
        pool_size: 2,
        configured_pool_size: 3,
        recommended_worker_count: 3,
        recommendation_reason: "Sustained queue wait.",
        hardware_worker_ceiling: 6,
        workers: [],
        restart_required: true,
      };
    }
    throw new Error(`Unexpected URL ${url}`);
  };
  context.renderOCRStatus({
    pool_size: 2,
    configured_pool_size: 2,
    recommended_worker_count: 3,
    recommendation_reason: "Sustained queue wait.",
    hardware_worker_ceiling: 6,
    workers: [],
    restart_required: false,
  });

  await context.applyOCRRecommendation();

  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    {
      url: "/api/ocr/settings",
      data: { worker_pool_size: 3 },
      method: "PUT",
    },
    { url: "/api/ocr/status", method: "GET" },
  ]);
  assert.equal(input.value, "3");
  assert.equal(nodes.get("ocr-restart-required").textContent, "Yes");
});

test("Camera Settings opens discovered sources for the correct device", async () => {
  const { context, nodes } = setup();
  context.deviceCameraDetails = {
    phone_B: {
      config: { camera_source: 1, rotation: 90, enabled: true },
      status: { ready: true },
    },
  };
  context.api = async (url) => {
    assert.equal(url, "/api/cameras?refresh=true");
    return { cameras: [{ source: 0, available: true }, { source: 1, available: true }] };
  };
  await context.openCameraSettings({ id: "phone_B", name: "Phone B" });
  assert.equal(context.$("camera-settings-dialog").hidden, false);
  assert.equal(nodes.get("camera-settings-title").textContent, "Camera Settings · Phone B");
  assert.equal(nodes.get("camera-settings-source").value, "1");
  assert.equal(nodes.get("camera-settings-rotation").value, "90");
  assert.equal(nodes.get("camera-settings-enabled").checked, true);
  assert.equal(nodes.get("camera-source-options").children.length, 2);
});

test("unavailable saved source is retained without adding invalid discovered indexes", async () => {
  const { context, nodes } = setup();
  context.deviceCameraDetails = {
    phone_C: {
      config: { camera_source: 7, rotation: 0, enabled: true },
      status: { ready: false, error: "Cannot open camera source 7" },
    },
  };
  context.api = async () => ({
    cameras: [
      { source: 0, available: true },
      { source: 1, available: true },
    ],
  });
  await context.openCameraSettings({ id: "phone_C", name: "Phone C" });
  const options = nodes.get("camera-source-options").children;
  assert.deepEqual(options.map((option) => option.value), ["7", "0", "1"]);
  assert.match(options[0].textContent, /Unavailable/);
  assert.equal(nodes.get("camera-settings-source").value, "7");
});

test("Refresh Cameras forces discovery instead of using the cache", async () => {
  const { context, calls } = setup();
  context.cameraSettingsDeviceId = "phone_A";
  context.api = async (url, data, method) => {
    calls.push({ url, data, method });
    return { cameras: [{ source: 0, available: true }] };
  };
  await context.refreshCameraSources(true, "phone_A", "0");
  assert.equal(calls[0].url, "/api/cameras?refresh=true");
  assert.equal(context.$("camera-source-options").children.length, 1);
});

test("Test Camera reports success and failure without saving", async () => {
  const { context, nodes, calls } = setup();
  nodes.set("camera-settings-source", { ...element("input"), value: "2" });
  nodes.set("camera-settings-rotation", { ...element("select"), value: "180" });
  context.api = async (url, data, method) => {
    calls.push({ url, data, method });
    return { ok: true, preview: "data:image/jpeg;base64,dGVzdA==" };
  };
  await context.testCameraSettings();
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0])), {
    url: "/api/cameras/test",
    data: { camera_source: 2, rotation: 180 },
    method: "POST",
  });
  assert.equal(nodes.get("camera-settings-status").textContent, "Available");
  assert.equal(nodes.get("camera-test-image").hidden, false);

  context.api = async () => { throw new Error("Cannot open camera"); };
  await context.testCameraSettings();
  assert.equal(nodes.get("camera-settings-status").textContent, "Unavailable");
  assert.match(nodes.get("camera-settings-error").textContent, /Cannot open/);
});

test("Camera Settings saves source rotation and enabled for its device", async () => {
  const { context, nodes, calls } = setup();
  context.cameraSettingsDeviceId = "phone_B";
  context.deviceCatalog = { selected_device: "phone_A", devices: [] };
  nodes.set("camera-settings-source", { ...element("input"), value: "3" });
  nodes.set("camera-settings-rotation", { ...element("select"), value: "270" });
  nodes.set("camera-settings-enabled", { ...element("input"), checked: false });
  context.api = async (url, data, method) => {
    calls.push({ url, data, method });
    return { ok: true };
  };
  context.refreshStatus = async () => {};
  context.loadDevices = async () => {};
  await context.saveDeviceCamera({ preventDefault() {} });
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0])), {
    url: "/api/devices/phone_B/camera",
    data: { camera_source: 3, rotation: 270, enabled: false },
    method: "PUT",
  });
  assert.equal(nodes.get("camera-settings-dialog").hidden, true);
});

test("Camera Settings saves AVFoundation identity with its display name", async () => {
  const { context, nodes, calls } = setup();
  const stableSource = "avfoundation:rog-unique-id";
  context.cameraSettingsDeviceId = "phone_A";
  context.deviceCatalog = { selected_device: "phone_B", devices: [] };
  context.populateCameraSources(
    [
      {
        source: stableSource,
        stable_id: stableSource,
        runtime_source: 0,
        backend: "avfoundation",
        index: 0,
        name: "ROG EYE S",
        available: true,
      },
    ],
    stableSource,
  );
  nodes.set("camera-settings-rotation", { ...element("select"), value: "90" });
  nodes.set("camera-settings-enabled", { ...element("input"), checked: true });
  context.api = async (url, data, method) => {
    calls.push({ url, data, method });
    return { ok: true };
  };
  context.refreshStatus = async () => {};
  context.loadDevices = async () => {};

  await context.saveDeviceCamera({ preventDefault() {} });

  assert.deepEqual(JSON.parse(JSON.stringify(calls[0])), {
    url: "/api/devices/phone_A/camera",
    data: {
      camera_source: stableSource,
      camera_id: stableSource,
      rotation: 90,
      enabled: true,
      display_name: "ROG EYE S",
    },
    method: "PUT",
  });
  assert.match(
    nodes.get("camera-source-options").children[0].textContent,
    /ROG EYE S.*Available/,
  );
});

test("failed camera save resets fields to the authoritative restored assignment", async () => {
  const { context, nodes } = setup();
  context.cameraSettingsDeviceId = "phone_A";
  nodes.set("camera-settings-source", { ...element("input"), value: "1" });
  nodes.set("camera-settings-rotation", { ...element("select"), value: "90" });
  nodes.set("camera-settings-enabled", { ...element("input"), checked: false });
  let request = 0;
  context.api = async (url, data, method) => {
    request++;
    if (request === 1) throw new Error(
      "Camera could not be reopened; previous assignments were restored.",
    );
    assert.equal(url, "/api/devices/phone_A/camera");
    assert.equal(method, "GET");
    return {
      config: { camera_source: 0, rotation: 0, enabled: true },
      status: { ready: true, width: 1280, height: 720 },
    };
  };

  await context.saveDeviceCamera({ preventDefault() {} });

  assert.equal(context.$("camera-settings-dialog").hidden, false);
  assert.equal(nodes.get("camera-settings-source").value, "0");
  assert.equal(nodes.get("camera-settings-rotation").value, "0");
  assert.equal(nodes.get("camera-settings-enabled").checked, true);
  assert.match(nodes.get("camera-settings-error").textContent, /restored/);
  assert.equal(context.deviceCameraDetails.phone_A.config.camera_source, 0);
});

test("camera swap resets preview when the selected device changes indirectly", async () => {
  const { context, nodes, notices } = setup();
  context.cameraSettingsDeviceId = "phone_A";
  context.deviceCatalog = { selected_device: "phone_B", devices: [] };
  context.manual = true;
  context.calibrating = true;
  nodes.set("camera-settings-source", { ...element("input"), value: "1" });
  nodes.set("camera-settings-rotation", { ...element("select"), value: "0" });
  nodes.set("camera-settings-enabled", { ...element("input"), checked: true });
  let previewMessage = null;
  context.clearPreview = (message) => { previewMessage = message; };
  context.api = async () => ({
    ok: true,
    selected_camera_changed: true,
    selected_calibration_revalidation_required: true,
  });
  context.refreshStatus = async () => {};
  context.loadDevices = async () => {};

  await context.saveDeviceCamera({ preventDefault() {} });

  assert.equal(context.manual, false);
  assert.equal(context.calibrating, false);
  assert.equal(previewMessage, "Waiting for the assigned camera…");
  assert.equal(nodes.get("calibrate").textContent, "Recalibrate");
  assert.match(notices.at(-1), /Recalibrate before control/);
});

test("confirmed delete uses DELETE, refreshes, and adopts server fallback", async () => {
  const { context, calls, nodes, notices } = setup();
  const remaining = {
    ok: true,
    default_device: "phone_A",
    selected_device: "phone_A",
    devices: [{ id: "phone_A", name: "Phone A", connected: true }],
    enrollment: { active: false },
  };
  context.deviceCatalog = {
    ...remaining,
    selected_device: "phone_B",
    devices: [
      ...remaining.devices,
      { id: "phone_B", name: "Phone B", connected: true },
    ],
  };
  context.api = async (url, data, method) => {
    calls.push({ url, data, method });
    return structuredClone(remaining);
  };
  const button = element("button");
  await context.deleteDevice(
    { id: "phone_B", name: "Phone B", connected: true },
    button,
  );
  assert.equal(calls[0].url, "/api/devices/phone_B");
  assert.equal(calls[0].method, "DELETE");
  assert.equal(calls.some((call) => call.url.includes("replace")), false);
  assert.equal(context.deviceCatalog.selected_device, "phone_A");
  assert.equal(nodes.get("device-list").children.length, 1);
  assert.match(notices.at(-1), /Deleted phone_B/);
});

test("delete requires confirmation", async () => {
  const { context, calls } = setup();
  context.window.confirm = () => false;
  await context.deleteDevice({ id: "phone_A" }, element("button"));
  assert.equal(calls.length, 0);
});

test("polling updates connection state and preserves selected device", async () => {
  const { context, nodes } = setup();
  let connected = true;
  context.api = async (url) => url.endsWith("/camera") ? ({
    config: { camera_source: 1, rotation: 90, enabled: true },
    status: { ready: connected, error: connected ? "" : "Unavailable" },
  }) : ({
    ok: true,
    default_device: "phone_A",
    selected_device: "phone_B",
    devices: [
      { id: "phone_A", name: "Phone A", connected: true },
      { id: "phone_B", name: "Phone B", connected },
    ],
    enrollment: { active: false },
  });
  await context.loadDevices();
  let selected = nodes.get("device-list").children[1];
  assert.match(selected.className, /selected/);
  assert.match(selected.children[1].children[0].textContent, /Connected/);

  connected = false;
  await context.loadDevices();
  selected = nodes.get("device-list").children[1];
  assert.match(selected.className, /selected/);
  assert.match(selected.children[1].children[0].textContent, /Disconnected/);

  connected = true;
  await context.loadDevices();
  selected = nodes.get("device-list").children[1];
  assert.match(selected.children[1].children[0].textContent, /Connected/);
  assert.equal(context.deviceCatalog.selected_device, "phone_B");
});

test("polling adds and removes device cards without resetting selection", async () => {
  const { context, nodes } = setup();
  let devices = [
    { id: "phone_A", connected: true },
    { id: "phone_B", connected: true },
  ];
  context.api = async (url) => url.endsWith("/camera") ? ({
    config: { camera_source: url.includes("phone_A") ? 0 : 1, rotation: 0, enabled: true },
    status: { ready: true, width: 1280, height: 720 },
  }) : ({
    selected_device: "phone_B",
    devices: structuredClone(devices),
    enrollment: { active: false },
  });

  await context.loadDevices();
  assert.equal(nodes.get("device-list").children.length, 2);
  assert.match(nodes.get("device-list").children[1].className, /selected/);

  devices.push({ id: "phone_C", connected: false });
  await context.loadDevices();
  assert.equal(nodes.get("device-list").children.length, 3);
  assert.match(nodes.get("device-list").children[1].className, /selected/);

  devices = devices.filter((device) => device.id !== "phone_A");
  await context.loadDevices();
  assert.deepEqual(
    nodes.get("device-list").children.map((card) => card.children[0].children[0].textContent),
    ["phone_B", "phone_C"],
  );
  assert.match(nodes.get("device-list").children[0].className, /selected/);
});

test("Add Phone draft, validation, panel, and focus survive background polling", async () => {
  const { context, nodes } = setup();
  let devices = [{ id: "phone_A", connected: true }];
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: structuredClone(devices),
    enrollment: { active: false },
  };
  context.deviceCameraDetails = {
    phone_A: {
      config: { camera_source: 0, rotation: 0, enabled: true },
      status: { ready: true, width: 1280, height: 720 },
    },
  };
  context.addPhoneInitialDeviceIds = new Set(["phone_A"]);
  context.addPhonePanelOpen = true;
  const input = nodes.get("device-enrollment-id") || context.$("device-enrollment-id");
  input.value = "phone_B";
  input.focus();
  const validation = context.$("device-error");
  validation.textContent = "Choose an unused device ID.";
  validation.hidden = false;
  context.renderDevices();

  context.api = async (url) => url.endsWith("/camera") ? ({
    config: { camera_source: 0, rotation: 0, enabled: true },
    status: { ready: true, width: 1280, height: 720 },
  }) : ({
    selected_device: "phone_A",
    devices: structuredClone(devices),
    enrollment: { active: false },
  });
  await context.loadDevices();
  await context.loadDevices();
  assert.equal(input.value, "phone_B");
  assert.equal(context.$("device-enrollment-form").hidden, false);
  assert.equal(input.focused, true);
  assert.equal(validation.textContent, "Choose an unused device ID.");
  assert.equal(validation.hidden, false);

  devices = [...devices, { id: "phone_B", connected: true }];
  await context.loadDevices();
  assert.equal(context.$("device-enrollment-form").hidden, true);
  assert.equal(input.value, "");
});

test("device polling does not overwrite open Camera Settings fields", async () => {
  const { context } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [{ id: "phone_A", connected: true }],
    enrollment: { active: false },
  };
  context.cameraSettingsDeviceId = "phone_A";
  context.$("camera-settings-dialog").hidden = false;
  context.$("camera-settings-source").value = "custom-camera-url";
  context.$("camera-settings-rotation").value = "270";
  context.$("camera-settings-enabled").checked = false;
  context.api = async (url) => url.endsWith("/camera") ? ({
    config: { camera_source: 0, rotation: 0, enabled: true },
    status: { ready: true, width: 1280, height: 720 },
  }) : ({
    selected_device: "phone_A",
    devices: [{ id: "phone_A", connected: false }],
    enrollment: { active: false },
  });

  await context.loadDevices();
  assert.equal(context.$("camera-settings-dialog").hidden, false);
  assert.equal(context.$("camera-settings-source").value, "custom-camera-url");
  assert.equal(context.$("camera-settings-rotation").value, "270");
  assert.equal(context.$("camera-settings-enabled").checked, false);
});

test("one failed poll preserves connection state and a later poll recovers", async () => {
  const { context, nodes } = setup();
  let fail = false;
  let connected = true;
  context.api = async (url) => {
    if (url === "/api/devices") {
      if (fail) throw new Error("Pi status request failed");
      return {
        selected_device: "phone_A",
        devices: [{ id: "phone_A", connected }],
        enrollment: { active: false },
      };
    }
    return {
      config: { camera_source: 0, rotation: 0, enabled: true },
      status: { ready: true, width: 1280, height: 720 },
    };
  };

  await context.loadDevices();
  fail = true;
  await context.loadDevices();
  assert.match(
    nodes.get("device-list").children[0].children[1].children[0].textContent,
    /Connected/,
  );
  assert.equal(context.selectedPiDeviceConnected(), true);

  fail = false;
  connected = false;
  await context.loadDevices();
  assert.match(
    nodes.get("device-list").children[0].children[1].children[0].textContent,
    /Disconnected/,
  );
});

test("prolonged poll failure shows status unavailable without claiming disconnect", async () => {
  const { context, nodes } = setup();
  context.deviceCatalog = {
    selected_device: "phone_A",
    devices: [{ id: "phone_A", connected: true }],
    enrollment: { active: false },
  };
  context.deviceCameraDetails = {
    phone_A: {
      config: { camera_source: 0, rotation: 0, enabled: true },
      status: { ready: true, width: 1280, height: 720 },
    },
  };
  context.deviceRefreshLastSuccessAt = Date.now() - 6000;
  context.api = async () => { throw new Error("Pi unavailable"); };
  await context.loadDevices();
  assert.match(nodes.get("device-state").textContent, /Status unavailable/i);
  assert.match(
    nodes.get("device-list").children[0].children[1].children[0].textContent,
    /Status unavailable/,
  );
  assert.equal(context.selectedPiDeviceConnected(), false);
});

test("enrollment expiration is reflected by the next device poll", async () => {
  const { context, nodes } = setup();
  let active = true;
  context.api = async () => ({
    selected_device: null,
    devices: [],
    enrollment: active
      ? { active: true, target_device_id: "phone_B", remaining_seconds: 1 }
      : { active: false, target_device_id: null, remaining_seconds: null },
  });
  await context.loadDevices();
  assert.equal(nodes.get("device-enrollment-active").hidden, false);
  active = false;
  await context.loadDevices();
  assert.equal(nodes.get("device-enrollment-active").hidden, true);
  assert.equal(nodes.get("device-add").disabled, false);
});

test("device refresh scheduling keeps only one live timer", () => {
  const { context, timers } = setup();
  context.scheduleDeviceRefresh(true);
  context.scheduleDeviceRefresh(true);
  assert.equal(timers.filter((timer) => !timer.cancelled).length, 1);
  assert.equal(timers.filter((timer) => timer.cancelled).length, 1);
});

test("device polling never overlaps a slow request", async () => {
  const { context, calls } = setup();
  let finish;
  context.api = (url, data, method) => {
    calls.push({ url, data, method });
    if (url.endsWith("/camera")) return Promise.resolve({
      config: { camera_source: 0, rotation: 0, enabled: true },
      status: { ready: true },
    });
    if (url.endsWith("/withdraw")) return Promise.resolve({
      assigned: false,
      credentials_saved: false,
      withdraw_api: { operator_id: null, username: null, payment_records: [] },
    });
    if (url === "/api/jobs") return Promise.resolve({ jobs: [] });
    if (url === "/api/remote-control") return Promise.resolve({ history: [] });
    return new Promise((resolve) => { finish = resolve; });
  };
  const first = context.loadDevices();
  await context.loadDevices();
  assert.equal(calls.length, 1);
  finish({
    ok: true,
    default_device: "phone_A",
    selected_device: "phone_A",
    devices: [{ id: "phone_A", connected: true }],
    enrollment: { active: false },
  });
  await first;
});

test("device data remains visible while the next poll is in flight", async () => {
  const { context, nodes } = setup();
  let poll = 0;
  let finishDevicePoll;
  const catalog = {
    default_device: "phone_A",
    selected_device: "phone_A",
    devices: [{ id: "phone_A", name: "Phone A", connected: true }],
    enrollment: { active: false },
  };
  context.startDevicePreview = () => {};
  context.api = (url) => {
    if (url === "/api/devices") {
      poll += 1;
      if (poll === 1) return Promise.resolve(structuredClone(catalog));
      return new Promise((resolve) => { finishDevicePoll = resolve; });
    }
    if (url.endsWith("/camera")) return Promise.resolve({
      config: { camera_source: 0, rotation: 0, enabled: true },
      status: { ready: true, width: 438, height: 996 },
    });
    if (url.endsWith("/withdraw")) return Promise.resolve({
      assigned: false,
      withdraw_api: { operator_id: null, username: null, payment_records: [] },
      status: { state: "DISABLED" },
    });
    if (url === "/api/jobs") return Promise.resolve({ jobs: [] });
    if (url === "/api/remote-control") return Promise.resolve({ history: [] });
    throw new Error(`Unexpected request: ${url}`);
  };

  await context.loadDevices();
  const card = nodes.get("device-list").children[0];
  const bluetooth = card.children[1].children[0];
  const pending = context.loadDevices();

  assert.equal(nodes.get("device-list").children[0], card);
  assert.equal(card.children[1].children[0], bluetooth);
  assert.equal(bluetooth.textContent, "Bluetooth: ● Connected");
  assert.equal(context.deviceStatusStale, false);

  finishDevicePoll(structuredClone(catalog));
  await pending;
  assert.equal(nodes.get("device-list").children[0], card);
});

test("device polling checks assigned cameras without camera discovery", async () => {
  const { context, calls } = setup();
  context.api = async (url, data, method) => {
    calls.push({ url, data, method });
    if (url === "/api/devices") return {
      selected_device: "phone_A",
      devices: [
        { id: "phone_A", connected: true },
        { id: "phone_B", connected: false },
      ],
      enrollment: { active: false },
    };
    return {
      config: { camera_source: url.includes("phone_A") ? 0 : 1, rotation: 0, enabled: true },
      status: { ready: true },
    };
  };
  await context.loadDevices();
  assert.equal(calls.filter((call) => call.url === "/api/cameras").length, 0);
  assert.equal(
    calls.filter((call) => /\/api\/devices\/phone_[AB]\/camera$/.test(call.url)).length,
    2,
  );
});

test("arbitrary returned phones and active enrollment render correctly", () => {
  const { context, nodes, timers } = setup();
  context.deviceCatalog = {
    default_device: "phone_A",
    selected_device: "phone_C",
    devices: [
      { id: "phone_A", name: "Main phone", connected: true,
        bluetooth_address: "AA:BB:CC:DD:EE:FF", default: true },
      { id: "phone_C", name: "Branch phone", connected: false,
        bluetooth_address: null, default: false },
    ],
    enrollment: {
      active: true,
      target_device_id: "phone_D",
      remaining_seconds: 42.2,
    },
  };
  context.renderDevices();

  assert.equal(nodes.get("device-list").children.length, 2);
  assert.match(nodes.get("device-list").children[1].className, /selected/);
  assert.equal(nodes.get("device-enrollment-active").hidden, false);
  assert.equal(nodes.get("device-enrollment-target").textContent,
    "Registering phone_D");
  assert.equal(nodes.get("device-enrollment-time").textContent,
    "43 seconds remaining");
  assert.equal(nodes.get("device-add").disabled, true);
  context.scheduleDeviceRefresh(true);
  assert.equal(timers.at(-1).delay, 1000);
});

test("next enrollment id is derived from returned devices", () => {
  const { context } = setup();
  assert.equal(context.nextDeviceId([{ id: "phone_A" }, { id: "phone_B" }]),
    "phone_C");
});

function stablePollingApi(context, readState) {
  return async (url) => {
    const current = readState();
    if (url === "/api/devices")
      return {
        default_device: "phone_A",
        selected_device: current.selected,
        devices: current.devices.map((device) => ({ ...device })),
        enrollment: { active: false },
      };
    if (url.endsWith("/camera"))
      return {
        config: { camera_source: 0, rotation: 0, enabled: true },
        status: {
          ready: true,
          width: 438,
          height: 996,
          frame_sequence: current.frameSequence,
        },
      };
    if (url.endsWith("/withdraw"))
      return {
        assigned: false,
        withdraw_api: { operator_id: null, username: null, payment_records: [] },
        status: { state: "DISABLED", reconciliation_complete: current.frameSequence % 2 === 0 },
      };
    if (url === "/api/jobs")
      return {
        jobs: [{
          job_id: "old-job",
          device_id: "phone_A",
          status: "COMPLETED",
          screenshots: [`frame-${current.frameSequence}.jpg`],
        }],
      };
    throw new Error(`Unexpected request: ${url}`);
  };
}

test("identical visible polling preserves card, Bluetooth node, and preview", async () => {
  const { context, nodes } = setup();
  let frameSequence = 1;
  let previewStarts = 0;
  const devices = [{ id: "phone_A", name: "Phone A", connected: false }];
  context.startDevicePreview = () => { previewStarts += 1; };
  context.api = stablePollingApi(context, () => ({
    devices,
    selected: "phone_A",
    frameSequence,
  }));

  await context.loadDevices();
  const card = nodes.get("device-list").children[0];
  const bluetooth = card.children[1].children[0];
  const offlineBadge = card.children[0].children[2].children.at(-1);
  const preview = card.children[3];
  frameSequence += 1;
  await context.loadDevices();

  assert.equal(nodes.get("device-list").children[0], card);
  assert.equal(card.children[1].children[0], bluetooth);
  assert.equal(card.children[0].children[2].children.at(-1), offlineBadge);
  assert.equal(card.children[3], preview);
  assert.equal(bluetooth.textContent, "Bluetooth: ● Disconnected");
  assert.equal(offlineBadge.textContent, "Offline");
  assert.equal(previewStarts, 1);
});

test("one device update does not reset another device preview", async () => {
  const { context, nodes } = setup();
  const previewStarts = new Map();
  const devices = [
    { id: "phone_A", name: "Phone A", connected: false },
    { id: "phone_B", name: "Phone B", connected: true },
  ];
  context.startDevicePreview = (deviceId) => {
    previewStarts.set(deviceId, (previewStarts.get(deviceId) || 0) + 1);
  };
  context.api = stablePollingApi(context, () => ({
    devices,
    selected: "phone_A",
    frameSequence: 1,
  }));

  await context.loadDevices();
  const phoneBCard = nodes.get("device-list").children[1];
  const phoneBPreview = phoneBCard.children[3];
  devices[0].connected = true;
  await context.loadDevices();

  assert.equal(nodes.get("device-list").children[1], phoneBCard);
  assert.equal(phoneBCard.children[3], phoneBPreview);
  assert.equal(previewStarts.get("phone_B"), 1);
});

test("real Bluetooth and operational state changes still render promptly", async () => {
  const { context, nodes } = setup();
  const devices = [{ id: "phone_A", name: "Phone A", connected: false }];
  context.api = stablePollingApi(context, () => ({
    devices,
    selected: "phone_A",
    frameSequence: 1,
  }));

  await context.loadDevices();
  let card = nodes.get("device-list").children[0];
  assert.equal(card.dataset.state, "offline");
  assert.equal(card.children[1].children[0].textContent,
    "Bluetooth: ● Disconnected");

  devices[0].connected = true;
  await context.loadDevices();
  card = nodes.get("device-list").children[0];
  assert.equal(card.dataset.state, "ready");
  assert.match(card.className, /device-card--ready/);
  assert.match(card.children[1].children[0].className, /connected/);
  assert.equal(card.children[1].children[0].textContent,
    "Bluetooth: ● Connected");
});

test("control target changes update the relevant badge", async () => {
  const { context, nodes } = setup();
  let selected = "phone_A";
  let previewStarts = 0;
  const devices = [
    { id: "phone_A", name: "Phone A", connected: true },
    { id: "phone_B", name: "Phone B", connected: true },
  ];
  context.startDevicePreview = () => { previewStarts += 1; };
  context.api = stablePollingApi(context, () => ({
    devices,
    selected,
    frameSequence: 1,
  }));

  await context.loadDevices();
  selected = "phone_B";
  await context.loadDevices();
  const cards = nodes.get("device-list").children;

  assert.doesNotMatch(cards[0].className, /selected/);
  assert.match(cards[1].className, /selected/);
  assert.equal(
    cards[1].children[0].children[2].children.some(
      (badge) => badge.textContent === "Control target",
    ),
    true,
  );
  assert.ok(previewStarts >= 2);
});

test("device polling adds and removes cards without restarting retained preview", async () => {
  const { context, nodes } = setup();
  let previewStarts = 0;
  let devices = [{ id: "phone_A", name: "Phone A", connected: true }];
  context.startDevicePreview = () => { previewStarts += 1; };
  context.api = stablePollingApi(context, () => ({
    devices,
    selected: "phone_A",
    frameSequence: 1,
  }));

  await context.loadDevices();
  const phoneACard = nodes.get("device-list").children[0];
  const phoneAPreview = phoneACard.children[3];
  devices = [...devices, { id: "phone_B", name: "Phone B", connected: false }];
  await context.loadDevices();
  assert.equal(nodes.get("device-list").children[0], phoneACard);
  assert.equal(nodes.get("device-list").children[0].children[3], phoneAPreview);
  assert.equal(nodes.get("device-list").children.length, 2);

  devices = devices.slice(1);
  await context.loadDevices();
  assert.equal(nodes.get("device-list").children.length, 1);
  assert.equal(nodes.get("device-list").children[0].dataset.deviceId, "phone_B");
  assert.equal(previewStarts, 2);
});
