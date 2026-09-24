"use strict";

const $ = (id) => document.getElementById(id);
const canvas = $("video");
const ctx = canvas.getContext("2d");
const clientId = crypto.randomUUID();
const token = document.querySelector('meta[name="control-token"]').content;
const previewDebug =
  document.querySelector('meta[name="preview-debug"]')?.content === "1";
const names = ["top-left", "top-right", "bottom-right", "bottom-left"];
let state = null,
  frameId = null,
  frameAt = 0,
  bitmap = null;
let calibrating = false,
  corners = [],
  manual = false,
  busy = false;
let statusRefreshInFlight = false;
let legacyStatusTimer = null;
let controlVersion = 0;
let keyboardArmed = false;
let heartbeatInFlight = false;
let piHealthInFlight = false;
let physicalBusy = false;
let physicalBusyLabel = "";
let connectionState = "connected";
let recoveryTimer = null;
let recoveryInFlight = false;
let recoveryAttempt = 0;
let recoveryHadManualControl = false;
const RECOVERY_DELAYS_MS = [500, 1000, 2000, 3000, 5000];
// The browser waits longer than the Pi client's 2s connect + 5s read limits,
// allowing the server to return its controlled uncertainty message first.
const API_TIMEOUT_MS = 9000;
let previewDebugWindow = null;
let previewTimer = null;
let previewInFlight = false;
let deviceCatalog = null;
let deviceRefreshInFlight = false;
let deviceRefreshTimer = null;
let deviceRenderSignature = null;
let devicePollingStopped = false;
const deviceDeletesInFlight = new Set();
const DEVICE_REFRESH_MS = 1000;
const PREVIEW_INTERVAL_MS = 65;
const PREVIEW_ERROR_RETRY_MS = 600;
const PREVIEW_BACKGROUND_MS = 1000;

function recordPreviewTiming(fetchMs, bodyMs, decodeMs, totalMs) {
  if (!previewDebug) return;
  const now = performance.now();
  previewDebugWindow ||= {
    started: now,
    last: null,
    count: 0,
    fetch: 0,
    body: 0,
    decode: 0,
    total: 0,
    interval: 0,
    maxInterval: 0,
  };
  const metrics = previewDebugWindow;
  if (metrics.last !== null) {
    const interval = now - metrics.last;
    metrics.interval += interval;
    metrics.maxInterval = Math.max(metrics.maxInterval, interval);
  }
  metrics.last = now;
  metrics.count++;
  metrics.fetch += fetchMs;
  metrics.body += bodyMs;
  metrics.decode += decodeMs;
  metrics.total += totalMs;
  const elapsed = now - metrics.started;
  if (elapsed >= 1000) {
    console.info(
      `Browser preview FPS=${((metrics.count * 1000) / elapsed).toFixed(1)} ` +
        `render interval avg=${(
          metrics.interval / Math.max(1, metrics.count - 1)
        ).toFixed(1)}ms max=${metrics.maxInterval.toFixed(1)}ms ` +
        `fetch=${(metrics.fetch / metrics.count).toFixed(1)}ms ` +
        `body=${(metrics.body / metrics.count).toFixed(1)}ms ` +
        `decode=${(metrics.decode / metrics.count).toFixed(1)}ms ` +
        `total=${(metrics.total / metrics.count).toFixed(1)}ms`,
    );
    previewDebugWindow = null;
  }
}

function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").classList.toggle("error", error);
}

function transientFailure(error, status = 0) {
  return (
    [502, 503, 504].includes(status) ||
    /timeout|timed out|network|fetch|connection|reset|temporarily unavailable|pi api.*(?:offline|unavailable)/i.test(
      error?.message || "",
    )
  );
}

function scheduleRecovery() {
  if (recoveryTimer !== null || recoveryInFlight) return;
  const delay =
    RECOVERY_DELAYS_MS[
      Math.min(recoveryAttempt, RECOVERY_DELAYS_MS.length - 1)
    ];
  recoveryAttempt++;
  recoveryTimer = setTimeout(() => {
    recoveryTimer = null;
    return recoverConnection();
  }, delay);
}

function beginReconnect(error) {
  recoveryHadManualControl ||= manual;
  connectionState = "reconnecting";
  notice("Connection interrupted. Retrying automatically...", true);
  render();
  scheduleRecovery();
}

async function api(path, data, method = "POST", options = {}) {
  try {
    const response = await fetch(path, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-Control-Token": token,
        "X-Control-Client": clientId,
        "X-Recorder-Client": window.phoneRecorder?.ownerId || clientId,
      },
      body: method === "GET" ? undefined : JSON.stringify(data ?? {}),
      cache: "no-store",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });
    let body = {};
    try {
      body = await response.json();
    } catch {
      body = {};
    }
    if (!response.ok) {
      const error = new Error(
        body.error || `Request failed (${response.status})`,
      );
      error.status = response.status;
      error.transient = transientFailure(error, response.status);
      throw error;
    }
    return body;
  } catch (error) {
    error.transient ??= transientFailure(error);
    if (options.monitor !== false && error.transient) beginReconnect(error);
    throw error;
  }
}

// Keep all Pi device-management HTTP details beside the dashboard's existing
// API helper. Rendering and event handlers use this service rather than fetch.
const deviceService = {
  list: () => api("/api/devices", undefined, "GET", { monitor: false }),
  select: (deviceId) => api("/api/devices/select", { device_id: deviceId }),
  startEnrollment: (deviceId) =>
    api("/api/enrollment/start", { target_device_id: deviceId }),
  stopEnrollment: () => api("/api/enrollment/stop", {}),
  remove: (deviceId) =>
    api(`/api/devices/${encodeURIComponent(deviceId)}`, {}, "DELETE"),
};

function setDeviceError(message = "") {
  const node = $("device-error");
  node.textContent = message;
  node.hidden = !message;
}

function nextDeviceId(devices) {
  const ids = new Set(devices.map((device) => device.id));
  for (let code = "B".charCodeAt(0); code <= "Z".charCodeAt(0); code++) {
    const candidate = `phone_${String.fromCharCode(code)}`;
    if (!ids.has(candidate)) return candidate;
  }
  let number = 2;
  while (ids.has(`phone_${number}`)) number++;
  return `phone_${number}`;
}

function scheduleDeviceRefresh(active = true) {
  clearTimeout(deviceRefreshTimer);
  deviceRefreshTimer = null;
  if (!active) return;
  deviceRefreshTimer = setTimeout(() => loadDevices(), DEVICE_REFRESH_MS);
}

function selectedPiDevice() {
  const selected = deviceCatalog?.selected_device;
  return Array.isArray(deviceCatalog?.devices)
    ? deviceCatalog.devices.find((device) => device.id === selected)
    : null;
}

function selectedPiDeviceConnected() {
  if (state?.manual_backend !== "raspberry_pi") return true;
  return selectedPiDevice()?.connected === true;
}

window.selectedPiDeviceConnected = selectedPiDeviceConnected;

function deviceManagementBlocked() {
  return (
    calibrating ||
    state?.control_status === "automation" ||
    !!window.phoneRecorder?.deviceManagementBlocked?.()
  );
}

function renderDevices() {
  const devices = Array.isArray(deviceCatalog?.devices)
    ? deviceCatalog.devices
    : [];
  const enrollment = deviceCatalog?.enrollment || { active: false };
  const selected = deviceCatalog?.selected_device;
  $("device-state").textContent = devices.length
    ? `${devices.length} registered phone${devices.length === 1 ? "" : "s"}. Select which phone this dashboard controls.`
    : "No phones are registered with this Pi yet.";

  const cards = devices.map((device) => {
    const card = document.createElement("article");
    card.className =
      "device-item" + (device.id === selected ? " selected" : "");

    const heading = document.createElement("div");
    heading.className = "device-item-heading";
    const name = document.createElement("h3");
    name.textContent = device.name || device.id;
    heading.append(name);
    if (device.default || device.id === deviceCatalog.default_device) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "Default";
      heading.append(badge);
    }

    const details = document.createElement("dl");
    const addDetail = (label, value, className = "") => {
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = label;
      description.textContent = value;
      description.className = className;
      details.append(term, description);
    };
    addDetail("Device ID", device.id || "—");
    addDetail(
      "Status",
      device.connected ? "Connected" : "Disconnected",
      "device-status" + (device.connected ? " connected" : ""),
    );
    addDetail("Bluetooth MAC", device.bluetooth_address || "Not paired");

    const actions = document.createElement("div");
    actions.className = "device-item-actions";
    const select = document.createElement("button");
    select.type = "button";
    select.textContent = device.id === selected ? "Selected" : "Select";
    select.disabled = device.id === selected || deviceManagementBlocked();
    select.addEventListener("click", () => selectDashboardDevice(device.id));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "stop";
    remove.textContent = deviceDeletesInFlight.has(device.id)
      ? "Deleting…"
      : "Delete";
    remove.disabled =
      deviceDeletesInFlight.has(device.id) || deviceManagementBlocked();
    remove.addEventListener("click", () => deleteDevice(device, remove));
    actions.append(select, remove);
    card.append(heading, details, actions);
    return card;
  });
  $("device-list").replaceChildren(...cards);

  $("device-enrollment-active").hidden = !enrollment.active;
  $("device-enrollment-form").hidden = true;
  $("device-add").disabled = !!enrollment.active || deviceManagementBlocked();
  if (enrollment.active) {
    $("device-enrollment-target").textContent = enrollment.target_device_id
      ? `Registering ${enrollment.target_device_id}`
      : "Pair the phone from its Bluetooth settings";
    $("device-enrollment-time").textContent = Number.isFinite(
      enrollment.remaining_seconds,
    )
      ? `${Math.max(0, Math.ceil(enrollment.remaining_seconds))} seconds remaining`
      : "Enrollment window active";
  }
}

async function loadDevices() {
  if (deviceRefreshInFlight || state?.manual_backend !== "raspberry_pi") return;
  deviceRefreshInFlight = true;
  try {
    const nextCatalog = await deviceService.list();
    const nextSignature = JSON.stringify({
      devices: nextCatalog.devices,
      enrollment: nextCatalog.enrollment,
      default_device: nextCatalog.default_device,
      selected_device: nextCatalog.selected_device,
    });
    deviceCatalog = nextCatalog;
    setDeviceError();
    if (nextSignature !== deviceRenderSignature) {
      deviceRenderSignature = nextSignature;
      renderDevices();
      render();
      window.phoneRecorder?.deviceStateChanged?.();
    }
  } catch (error) {
    setDeviceError(error.message);
  } finally {
    deviceRefreshInFlight = false;
    scheduleDeviceRefresh(
      !devicePollingStopped &&
        state?.manual_backend === "raspberry_pi" &&
        !document.hidden,
    );
  }
}

async function selectDashboardDevice(deviceId) {
  setDeviceError();
  if (calibrating) {
    setDeviceError("Finish or cancel calibration before switching phones.");
    return;
  }
  try {
    await deviceService.select(deviceId);
    manual = false;
    controlVersion++;
    await refreshStatus();
    await loadDevices();
    notice(`Selected ${deviceId}. Calibrate this phone if required.`);
  } catch (error) {
    setDeviceError(error.message);
  }
}

async function deleteDevice(device, button) {
  setDeviceError();
  if (calibrating) {
    setDeviceError("Finish or cancel calibration before deleting a phone.");
    return;
  }
  if (
    !window.confirm(
      `Delete ${device.id}? This will unregister and unpair this phone from the Raspberry Pi.`,
    )
  )
    return;
  deviceDeletesInFlight.add(device.id);
  button.disabled = true;
  button.textContent = "Deleting…";
  try {
    const result = await deviceService.remove(device.id);
    if (Array.isArray(result.devices)) deviceCatalog = result;
    notice(`Deleted ${device.id} from the Raspberry Pi.`);
    deviceRenderSignature = null;
    renderDevices();
    await loadDevices();
  } catch (error) {
    setDeviceError(error.message);
    // The Pi may report that a device vanished between the poll and delete.
    // Refresh in every failure case while preserving the server's selection.
    deviceRenderSignature = null;
    await loadDevices();
  } finally {
    deviceDeletesInFlight.delete(device.id);
    deviceRenderSignature = null;
    renderDevices();
  }
}

function fresh() {
  return frameId !== null && performance.now() - frameAt < 1800;
}
function active() {
  return (
    manual &&
    connectionState === "connected" &&
    !window.phoneRecorder?.blocksInput?.() &&
    state?.owned &&
    fresh() &&
    !calibrating &&
    state?.control_status !== "automation" &&
    !state?.pending &&
    selectedPiDeviceConnected()
  );
}

function keyboardEligible() {
  return (
    active() &&
    !window.phoneRecorder?.capturesPreview() &&
    state?.enabled &&
    state?.calibrated &&
    !!state?.device &&
    state?.camera?.ready &&
    !document.hidden &&
    document.hasFocus()
  );
}

function keyboardActive() {
  return (
    keyboardArmed && document.activeElement === canvas && keyboardEligible()
  );
}

function releaseKeyboard() {
  keyboardArmed = false;
  renderKeyboard();
}

function renderKeyboard() {
  if (!keyboardEligible()) keyboardArmed = false;
  const enabled = keyboardActive();
  $("keyboard-state").textContent = enabled
    ? busy
      ? "Keyboard control paused · command in progress"
      : "Keyboard control active"
    : "Keyboard control inactive";
  $("keyboard-state").classList.toggle("active", enabled && !busy);
  canvas.classList.toggle("keyboard-active", enabled);
  $("keyboard-help").textContent = enabled
    ? "Type on your laptop to control the phone. Press Esc to release keyboard control."
    : "Click or focus the phone view to enable keyboard control.";
}

function render() {
  const piMode = state?.manual_backend === "raspberry_pi";
  $("devices").hidden = !piMode;
  $("serial").textContent =
    state?.device?.label ||
    state?.device?.serial ||
    (piMode ? "Bluetooth phone unavailable" : "No authorized USB phone");
  $("pi-connection").hidden = !piMode;
  $("pi-api-state").textContent =
    connectionState === "reconnecting"
      ? "Pi Reconnecting..."
      : state?.pi?.api === "online"
        ? "Online"
        : "Pi Offline";
  $("pi-phone-state").textContent =
    state?.pi?.phone === "connected"
      ? "Connected"
      : state?.pi?.api === "online"
        ? "Phone Waiting"
        : "Unknown";

  const d = state?.device;

  $("resolution").textContent = d
    ? d.normalized
      ? "Camera-calibrated coordinates"
      : `${d.width} × ${d.height} · rotation ${d.rotation * 90}°`
    : "—";

  $("camera-state").textContent = state?.camera?.ready
    ? `Camera ${state.camera.index} · ${state.camera.width} × ${state.camera.height}`
    : "Unavailable / starting";

  $("frame-state").textContent = fresh()
    ? "● Live camera"
    : "Camera feed unavailable";

  $("mode").textContent =
    state?.control_status === "automation"
      ? "Automation running"
      : state?.pending
        ? "Takeover requested"
        : active()
          ? "Manual control active"
          : state?.enabled && !state?.owned
            ? "Other tab"
            : "Disabled";

  $("mode").classList.toggle("active", active());
  $("connection-banner").hidden = connectionState === "connected";
  $("connection-state").textContent =
    connectionState === "reconnecting" ? "Reconnecting..." : "Offline";
  renderKeyboard();

  const automation = state?.control_status === "automation";
  $("takeover").hidden = !automation && !state?.pending;
  $("takeover").disabled = busy || !!state?.pending || !state?.device;
  $("control-help").textContent = automation
    ? "Manual control disabled. Camera monitoring remains available. Use Take Over to interrupt automation."
    : state?.pending
      ? "Takeover requested. Manual input stays disabled until automation yields."
      : "Enable control when you are ready. Each click sends one tap to the phone.";

  $("enable").disabled =
    busy ||
    calibrating ||
    !fresh() ||
    !state?.device ||
    !state?.calibrated ||
    automation ||
    !selectedPiDeviceConnected() ||
    state?.enabled;

  $("calibrate").disabled = busy || !fresh() || !state?.device;

  $("cal-state").textContent = calibrating
    ? `${corners.length}/4 selected`
    : state?.calibrated
      ? "Saved"
      : "Required";

  $("undo").disabled = !calibrating || !corners.length || busy;

  $("save").disabled = !calibrating || corners.length !== 4 || busy || !fresh();
  document.querySelectorAll("[data-action]").forEach((button) => {
    const supported =
      !state?.supported_actions ||
      state.supported_actions.includes(button.dataset.action);
    button.disabled = !active() || busy || physicalBusy || !supported;
    button.title = supported
      ? ""
      : "This action is not provided by the Pi API.";
  });

  document.querySelectorAll("#corner-list li").forEach((item, i) => {
    item.classList.toggle("done", calibrating && i < corners.length);
    item.classList.toggle("next", calibrating && i === corners.length);
  });

  canvas.classList.toggle("interactive", calibrating || active());

  $("instruction").textContent = calibrating
    ? corners.length < 4
      ? `Click the phone display’s ${names[corners.length]} corner.`
      : "Check the outline, then save calibration."
    : active()
      ? "Click inside the outlined display to send one tap."
      : "Input disabled. Calibrate, then enable Manual Control.";
}

function draw() {
  if (!bitmap) return;
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  const points = calibrating ? corners : state?.corners || [];
  if (!points.length) return;

  ctx.save();
  ctx.lineWidth = Math.max(2, canvas.width / 500);
  ctx.strokeStyle = "#6ef0ab";
  ctx.fillStyle = "rgba(61, 208, 128, .10)";
  ctx.beginPath();

  points.forEach(([x, y], i) => {
    if (i === 0) ctx.moveTo(x * canvas.width, y * canvas.height);
    else ctx.lineTo(x * canvas.width, y * canvas.height);
  });

  if (points.length === 4) {
    ctx.closePath();
    ctx.fill();
  }

  ctx.stroke();
  points.forEach(([x, y], i) => {
    const radius = Math.max(12, canvas.width / 65);
    ctx.fillStyle = "#164d34";
    ctx.beginPath();
    ctx.arc(x * canvas.width, y * canvas.height, radius, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = "white";
    ctx.font = `600 ${radius}px sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(String(i + 1), x * canvas.width, y * canvas.height);
  });
  ctx.restore();
}

async function disable() {
  controlVersion++;
  manual = false;
  if (state) {
    state.pending = false;
    state.pending_owned = false;
  }
  render();
  await api("/api/control", { enabled: false });

  if (state) {
    state.enabled = false;
    state.owned = false;
    state.control_status = "free";
  }

  render();
}

async function report(task) {
  if (busy) return;
  busy = true;
  render();

  try {
    await task();
  } catch (error) {
    notice(error.message, true);
  } finally {
    busy = false;
    render();
  }
}

async function runPhysical(label, operation) {
  if (physicalBusy) {
    const error = new Error(
      "Controller busy. Wait for the current action to finish.",
    );
    error.clientBusy = true;
    throw error;
  }
  physicalBusy = true;
  physicalBusyLabel = label;
  window.phoneRecorder?.physicalBusyChanged?.(true, label);
  render();
  try {
    return await operation();
  } finally {
    physicalBusy = false;
    physicalBusyLabel = "";
    window.phoneRecorder?.physicalBusyChanged?.(false, "");
    render();
  }
}

async function recoverConnection() {
  if (
    recoveryInFlight ||
    statusRefreshInFlight ||
    piHealthInFlight ||
    heartbeatInFlight ||
    connectionState !== "reconnecting"
  ) {
    if (connectionState === "reconnecting") scheduleRecovery();
    return;
  }
  recoveryInFlight = true;
  try {
    if (recoveryHadManualControl && fresh() && !document.hidden) {
      heartbeatInFlight = true;
      try {
        await api("/api/heartbeat", { frame_id: frameId }, "POST", {
          monitor: false,
        });
      } finally {
        heartbeatInFlight = false;
      }
    }
    piHealthInFlight = true;
    let pi;
    try {
      pi = await api("/api/pi/status", undefined, "GET", {
        monitor: false,
      });
    } finally {
      piHealthInFlight = false;
    }
    if (state?.manual_backend === "raspberry_pi" && pi.api !== "online")
      throw Object.assign(
        new Error(pi.message || "Pi temporarily unavailable."),
        {
          transient: true,
        },
      );
    statusRefreshInFlight = true;
    let next;
    try {
      next = await api("/api/status", undefined, "GET", { monitor: false });
    } finally {
      statusRefreshInFlight = false;
    }
    state = next;
    manual = !!(next.owned || next.pending_owned);
    await window.phoneRecorder?.refreshAfterReconnect?.();
    connectionState = "connected";
    recoveryAttempt = 0;
    recoveryHadManualControl = false;
    notice("Connection restored.");
    setTimeout(() => {
      if ($("notice").textContent === "Connection restored.") notice("");
    }, 2500);
    render();
    draw();
  } catch (error) {
    connectionState = "reconnecting";
    notice("Pi temporarily unavailable. Retrying automatically...", true);
    scheduleRecovery();
  } finally {
    recoveryInFlight = false;
    if (connectionState === "reconnecting") scheduleRecovery();
  }
}

window.phoneConnection = {
  isReconnecting: () => connectionState !== "connected",
  isPhysicalBusy: () => physicalBusy,
  runPhysical,
};

async function refreshStatus() {
  if (statusRefreshInFlight) return;
  statusRefreshInFlight = true;
  const version = controlVersion;
  try {
    const next = await api("/api/status", undefined, "GET");
    if (version !== controlVersion) return;

    if (
      calibrating &&
      state?.device &&
      JSON.stringify(next.device) !== JSON.stringify(state.device)
    ) {
      calibrating = false;
      corners = [];
      notice(
        "Phone geometry changed during calibration. Start calibration again.",
        true,
      );
    }

    const previousMessage = state?.message;
    state = next;
    if (state.manual_backend === "raspberry_pi" && state.pi?.api === "offline")
      beginReconnect(
        new Error(state.pi.message || "Pi temporarily unavailable."),
      );
    if (!state.owned && !state.pending_owned) manual = false;
    if (!state.camera.ready || !state.device) notice(state.message, true);
    else if (state.message !== previousMessage) notice(state.message);
    render();
    draw();
  } catch (error) {
    if (version !== controlVersion) return;
    if (!error.transient) {
      manual = false;
      notice(`Backend unavailable: ${error.message}`, true);
    }
    render();
  } finally {
    statusRefreshInFlight = false;
  }
}

function schedulePreview(delay = PREVIEW_INTERVAL_MS) {
  if (previewTimer !== null) clearTimeout(previewTimer);
  const scheduledDelay = document.hidden
    ? Math.max(delay, PREVIEW_BACKGROUND_MS)
    : delay;
  previewTimer = setTimeout(() => {
    previewTimer = null;
    videoLoop();
  }, scheduledDelay);
}

async function videoLoop() {
  if (previewInFlight) return;
  previewInFlight = true;
  let succeeded = false;
  const cycleStarted = performance.now();
  try {
    const previewStarted = performance.now();
    const response = await fetch("/api/frame", {
      cache: "no-store",
      signal: AbortSignal.timeout(4000),
    });

    if (!response.ok)
      throw new Error((await response.json()).error || "Camera unavailable");
    const responseAt = performance.now();
    const nextId = Number(response.headers.get("X-Frame-Id"));
    const body = await response.blob();
    const bodyAt = performance.now();
    const next = await createImageBitmap(body);
    const decodedAt = performance.now();

    if (
      bitmap &&
      (next.width !== canvas.width || next.height !== canvas.height)
    ) {
      manual = false;
      calibrating = false;
      corners = [];
      if (state) {
        state.calibrated = false;
        state.corners = [];
      }
      api("/api/control", { enabled: false }).catch(() => {});
      notice("Camera resolution changed. Recalibrate the display.", true);
    }

    bitmap?.close();
    bitmap = next;
    canvas.width = next.width;
    canvas.height = next.height;
    if (frameId !== nextId) {
      frameAt = performance.now();
      frameId = nextId;
    }

    $("video-placeholder").classList.add("hidden");
    draw();
    render();
    const renderedAt = performance.now();
    recordPreviewTiming(
      responseAt - previewStarted,
      bodyAt - responseAt,
      decodedAt - bodyAt,
      renderedAt - previewStarted,
    );
    succeeded = true;
  } catch (error) {
    frameAt = 0;
    const wasManual = manual;
    manual = false;
    if (wasManual) api("/api/control", { enabled: false }).catch(() => {});
    $("video-placeholder").textContent = error.message;
    $("video-placeholder").classList.remove("hidden");
    render();
  } finally {
    previewInFlight = false;
    const elapsed = performance.now() - cycleStarted;
    schedulePreview(
      succeeded
        ? Math.max(0, PREVIEW_INTERVAL_MS - elapsed)
        : PREVIEW_ERROR_RETRY_MS,
    );
  }
}

function applyControlResult(result) {
  const wasPending = state?.pending;
  manual = !!(result.enabled || result.pending);
  if (state) {
    state.enabled = manual;
    state.owned = !!result.enabled;
    state.pending = !!result.pending;
    state.pending_owned = !!result.pending;
    state.control_status = result.pending
      ? "takeover_requested"
      : result.enabled
        ? "manual"
        : "free";
  }
  if (result.pending)
    notice("Takeover requested. Waiting for automation to yield.");
  else if (wasPending && result.enabled) notice("Manual control active.");
  render();
}

$("enable").addEventListener("click", () =>
  report(async () => {
    const version = ++controlVersion;
    let result;
    try {
      result = await api("/api/control", {
        enabled: true,
        frame_id: frameId,
      });
    } finally {
      // Enabling checks the Pi on the server; fetch the resulting cached badges.
      if (state?.manual_backend === "raspberry_pi") await refreshStatus();
    }
    if (version !== controlVersion) return;
    applyControlResult(result);
    if (result.enabled) notice("Manual control active.");
  }),
);

$("takeover").addEventListener("click", () =>
  report(async () => {
    const version = ++controlVersion;
    const result = await api("/api/takeover", {});
    if (version !== controlVersion) return;
    applyControlResult(result);
  }),
);

$("disable").addEventListener("click", () => {
  disable()
    .then(() => notice("Input disabled."))
    .catch((error) => notice(error.message, true));
});

$("calibrate").addEventListener("click", () =>
  report(async () => {
    await disable();
    await api("/api/calibration", {}, "DELETE");
    calibrating = true;
    corners = [];
    state.calibrated = false;
    state.corners = [];
    notice(
      "Select top-left, top-right, bottom-right, then bottom-left of the lit display. These clicks do not reach the phone.",
    );
    draw();
  }),
);

$("undo").addEventListener("click", () => {
  corners.pop();
  render();
  draw();
});

$("save").addEventListener("click", () =>
  report(async () => {
    const result = await api("/api/calibration", {
      corners,
      frame_id: frameId,
    });

    calibrating = false;
    state.calibrated = true;
    state.corners = result.corners;

    notice(
      "Calibration saved. Input remains disabled until you enable Manual Control.",
    );
    draw();
  }),
);

function manualApi(path, data) {
  const operation = runPhysical("Sending...", () => api(path, data)).then(
    (result) => {
      window.phoneRecorder?.acceptManual?.(result.recording);
      return result;
    },
    async (error) => {
      if (error.clientBusy) throw error;
      manual = false;
      render();
      await window.phoneRecorder?.manualFailed?.(error);
      if (error.transient)
        error.message = `${error.message} Action result uncertain; it was not retried automatically.`;
      throw error;
    },
  );
  window.phoneRecorder?.trackManual?.(operation);
  return operation;
}

async function sendAction(action, point) {
  if (window.phoneRecorder?.capturesPreview()) {
    return window.phoneRecorder.action(action, point);
  }
  if (!active() || busy || physicalBusy) return;

  await report(async () => {
    const data = { action, frame_id: frameId };
    if (point) [data.x, data.y] = point;

    try {
      const result = await manualApi("/api/action", data);
      if (result.normalized) {
        const position = result.normalized.map((v) => v.toFixed(3)).join(", ");
        $("coordinates").textContent = result.pixel
          ? `${position} → ${result.pixel.join(", ")} px`
          : position;
      }

      notice(`Sent ${action.replaceAll("_", " ")}.`);
    } catch (error) {
      // No automatic command retries, including after a network timeout.
      manual = false;
      api("/api/control", { enabled: false }).catch(() => {});
      throw error;
    }
  });
}

const phoneKeys = {
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
};

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && keyboardArmed) {
    event.preventDefault();
    releaseKeyboard();
    canvas.blur();
    return;
  }
  if (
    !keyboardActive() ||
    busy ||
    physicalBusy ||
    event.defaultPrevented ||
    event.repeat ||
    event.ctrlKey ||
    event.metaKey ||
    event.altKey ||
    event.isComposing ||
    event.keyCode === 229 ||
    event.getModifierState("AltGraph") ||
    event.getModifierState("Fn")
  )
    return;

  // Leave local editing and keyboard activation of dashboard controls intact.
  const target = event.target;
  if (target?.isContentEditable || target?.closest?.("input, textarea, select"))
    return;
  if (
    (event.key === "Enter" || event.key === " ") &&
    target?.closest?.("button, a, [role='button']")
  )
    return;

  let type, value;
  if (event.key.length === 1 && event.key >= " " && event.key <= "~") {
    type = "text";
    value = event.key;
  } else if (!event.shiftKey && Object.hasOwn(phoneKeys, event.key)) {
    type = "key";
    value = phoneKeys[event.key];
  } else return;

  event.preventDefault();
  // Send immediately using the existing busy gate. No buffer or keyboard history.
  report(async () => {
    try {
      await manualApi("/api/key", { type, value, frame_id: frameId });
    } catch (error) {
      // An uncertain input is never retried, even after a network timeout.
      manual = false;
      api("/api/control", { enabled: false }).catch(() => {});
      throw error;
    }
  });
});

window.addEventListener("focus", renderKeyboard);
window.addEventListener("blur", releaseKeyboard);

canvas.addEventListener("focus", () => {
  keyboardArmed = keyboardEligible();
  renderKeyboard();
});

canvas.addEventListener("blur", releaseKeyboard);

document.addEventListener("focusin", (event) => {
  if (event.target !== canvas) releaseKeyboard();
});

document.addEventListener("pointerdown", (event) => {
  if (event.target !== canvas) releaseKeyboard();
});

canvas.addEventListener("click", (event) => {
  if (!calibrating && window.phoneRecorder?.capturesPreview()) return;
  if (!fresh() || busy || physicalBusy) return;

  canvas.focus({ preventScroll: true });
  keyboardArmed = keyboardEligible();
  renderKeyboard();

  const box = canvas.getBoundingClientRect();
  const point = [
    (event.clientX - box.left) / box.width,
    (event.clientY - box.top) / box.height,
  ];

  if (point.some((value) => value < 0 || value > 1)) return;

  if (calibrating) {
    if (corners.length < 4) corners.push(point);
    render();
    draw();
  } else sendAction("tap", point);
});

document.querySelectorAll("[data-action]").forEach((button) => {
  button.addEventListener("click", () => sendAction(button.dataset.action));
});

setInterval(async () => {
  if (!manual || heartbeatInFlight || connectionState !== "connected") return;
  if (!fresh() || document.hidden) {
    disable().catch(() => {});
    return;
  }

  const version = controlVersion;
  heartbeatInFlight = true;
  try {
    const result = await api("/api/heartbeat", { frame_id: frameId });
    if (version !== controlVersion || !manual) return;
    applyControlResult(result);
  } catch (error) {
    if (version !== controlVersion) return;
    if (!error.transient) {
      manual = false;
      notice(error.message, true);
    }
    render();
  } finally {
    heartbeatInFlight = false;
  }
}, 1500);

document.addEventListener("visibilitychange", () => {
  if (document.hidden && manual) disable().catch(() => {});
  // Background tabs are intentionally low-rate. Restore the normal cadence
  // immediately when Chrome makes the page visible again.
  schedulePreview(document.hidden ? PREVIEW_BACKGROUND_MS : 0);
  if (document.hidden) scheduleDeviceRefresh(false);
  else if (state?.manual_backend === "raspberry_pi") loadDevices();
});

window.addEventListener("pagehide", () => {
  clearInterval(legacyStatusTimer);
  legacyStatusTimer = null;
  clearTimeout(deviceRefreshTimer);
  deviceRefreshTimer = null;
  devicePollingStopped = true;
  // Best effort on close; the server's six-second lease is the fallback.
  if (manual)
    fetch("/api/control", {
      method: "POST",
      keepalive: true,
      headers: {
        "Content-Type": "application/json",
        "X-Control-Token": token,
        "X-Control-Client": clientId,
        "X-Recorder-Client": window.phoneRecorder?.ownerId || clientId,
      },
      body: JSON.stringify({ enabled: false }),
    }).catch(() => {});
});

$("reload-dashboard").addEventListener("click", () => location.reload());

function startLegacyStatusPolling() {
  // Preserve the existing Appium dashboard/automation status updates.
  if (state?.manual_backend !== "appium" || legacyStatusTimer !== null) return;
  legacyStatusTimer = setInterval(refreshStatus, 5000);
}

async function loadStatus() {
  try {
    // This endpoint checks the Pi once (and is a no-op in Appium mode).
    const pi = await api("/api/pi/status", undefined, "GET");
    if (pi.api === "offline")
      beginReconnect(new Error(pi.message || "Pi temporarily unavailable."));
  } catch (error) {
    if (!error.transient) notice(`Backend unavailable: ${error.message}`, true);
  }
  await refreshStatus();
  if (state?.manual_backend === "raspberry_pi") await loadDevices();
  startLegacyStatusPolling();
}

$("device-add").addEventListener("click", () => {
  const devices = Array.isArray(deviceCatalog?.devices)
    ? deviceCatalog.devices
    : [];
  $("device-enrollment-id").value = nextDeviceId(devices);
  $("device-enrollment-form").hidden = false;
  $("device-enrollment-id").focus();
  setDeviceError();
});

$("device-enrollment-cancel").addEventListener("click", () => {
  $("device-enrollment-form").hidden = true;
});

$("device-enrollment-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setDeviceError();
  try {
    await deviceService.startEnrollment($("device-enrollment-id").value.trim());
    await loadDevices();
  } catch (error) {
    setDeviceError(error.message);
  }
});

$("device-enrollment-stop").addEventListener("click", async () => {
  setDeviceError();
  try {
    await deviceService.stopEnrollment();
    await loadDevices();
  } catch (error) {
    setDeviceError(error.message);
  }
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) {
    devicePollingStopped = false;
    refreshStatus();
    loadDevices();
    startLegacyStatusPolling();
  }
});
render();
loadStatus();
videoLoop();
