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
let runtimeStatusTimer = null;
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
let deviceCameraDetails = {};
let deviceRefreshInFlight = false;
let deviceRefreshTimer = null;
let deviceRenderSignature = null;
let devicePollingStopped = false;
const deviceDeletesInFlight = new Set();
const devicePreviewTimers = new Map();
let devicePreviewGeneration = 0;
let cameraSettingsDeviceId = null;
let deviceRefreshLastSuccessAt = null;
let deviceRefreshError = "";
let deviceStatusStale = false;
let addPhonePanelOpen = false;
let addPhoneEnrollmentTarget = null;
let addPhoneInitialDeviceIds = new Set();
let accountBindings = [];
let jobCatalog = [];
const jobCancelsInFlight = new Set();
let accountSettingsDeviceId = null;
let accountSettingsAccountId = null;
let ocrSettingsDirty = false;
let ocrStatusTimer = null;
let ocrStatusInFlight = false;
let ocrPollingStopped = false;
let ocrRecommendedWorkerCount = null;
const DEVICE_REFRESH_MS = 1000;
const DEVICE_STATUS_STALE_MS = 5000;
const RUNTIME_STATUS_MS = 2000;
const PREVIEW_INTERVAL_MS = 65;
const PREVIEW_ERROR_RETRY_MS = 600;
const PREVIEW_BACKGROUND_MS = 1000;
let dashboardMode = "operate";
let adminSession = { authenticated: false, username: null, role: null };
let recoveryPanelOpen = false;
let recoveryRequestInFlight = false;
let recoveryTargetJobId = null;
let manualAssistanceOpen = false;
let readyVerificationJobId = null;
let readyVerificationFailedJobId = null;
let readyVerificationMessage = "";
let readyVerificationRestoreManual = false;
let readyVerificationRestoreInFlight = false;
let readyVerificationRestorePromise = null;
let readyVerificationFinishInFlight = false;
let previewGesture = null;
let suppressPreviewClick = false;
let ocrOperationalHealthy = null;
let workflowRuntime = null;

function setDashboardMode(mode) {
  dashboardMode =
    mode === "admin" && adminSession.authenticated ? "admin" : "operate";
  if (document.body) document.body.dataset.dashboardMode = dashboardMode;
  $("dashboard-operate").ariaSelected = String(dashboardMode === "operate");
  $("dashboard-admin").ariaSelected = String(dashboardMode === "admin");
  $("enable").textContent =
    dashboardMode === "operate"
      ? "Start Manual Control"
      : "Enable Manual Control";
  return dashboardMode;
}

function renderAdminSession() {
  const authenticated =
    adminSession.authenticated && adminSession.role === "admin";
  $("dashboard-admin").textContent = authenticated ? "Admin" : "Admin 🔒";
  $("admin-session").hidden = !authenticated;
  $("admin-session-username").textContent = authenticated
    ? adminSession.username
    : "";
}

function closeAdminLogin() {
  $("admin-login-dialog").hidden = true;
  $("admin-login-password").value = "";
  $("admin-login-error").hidden = true;
  $("admin-login-error").textContent = "";
}

function openAdminLogin() {
  $("admin-login-dialog").hidden = false;
  $("admin-login-error").hidden = true;
  $("admin-login-username").focus();
}

function lockAdminState() {
  adminSession = { authenticated: false, username: null, role: null };
  setDashboardMode("operate");
  renderAdminSession();
}

async function loadAdminSession() {
  try {
    const result = await api("/api/auth/session", undefined, "GET", {
      monitor: false,
    });
    adminSession = result.authenticated
      ? result
      : { authenticated: false, username: null, role: null };
  } catch {
    adminSession = { authenticated: false, username: null, role: null };
  }
  if (!adminSession.authenticated) setDashboardMode("operate");
  renderAdminSession();
}

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

function renderControllerTag() {
  const tag = $("controller-tag");
  if (!tag) return;

  if (connectionState !== "connected") {
    tag.textContent = "● Controller Reconnecting";
    tag.classList.add("error");
    return;
  }

  if (!state) {
    tag.textContent = "● Checking controller…";
    tag.classList.remove("error");
    return;
  }

  tag.textContent = "● Controller Online";
  tag.classList.remove("error");
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
      if (response.status === 403 && body.error === "Admin login required.")
        lockAdminState();
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
  camera: (deviceId) =>
    api(
      `/api/devices/${encodeURIComponent(deviceId)}/camera`,
      undefined,
      "GET",
      {
        monitor: false,
      },
    ),
  updateCamera: (deviceId, settings) =>
    api(`/api/devices/${encodeURIComponent(deviceId)}/camera`, settings, "PUT"),
  discoverCameras: (refresh = false) =>
    api(
      refresh ? "/api/cameras?refresh=true" : "/api/cameras",
      undefined,
      "GET",
      { monitor: false },
    ),
  testCamera: (cameraSource, rotation) =>
    api(
      "/api/cameras/test",
      { camera_source: cameraSource, rotation },
      "POST",
      { monitor: false },
    ),
};

const accountService = {
  list: () => api("/api/accounts", undefined, "GET", { monitor: false }),
  create: (binding) => api("/api/accounts", binding),
  update: (accountId, changes) =>
    api(`/api/accounts/${encodeURIComponent(accountId)}`, changes, "PATCH"),
  remove: (accountId) =>
    api(`/api/accounts/${encodeURIComponent(accountId)}`, {}, "DELETE"),
};

const jobService = {
  list: () => api("/api/jobs", undefined, "GET", { monitor: false }),
  cancel: (jobId) => api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {}),
  recover: (jobId) => api(`/api/jobs/${encodeURIComponent(jobId)}/recover`, {}),
  verifyReady: (jobId) =>
    api(`/api/jobs/${encodeURIComponent(jobId)}/verify-ready`, {}),
};

const ocrService = {
  settings: () =>
    api("/api/ocr/settings", undefined, "GET", { monitor: false }),
  save: (workerPoolSize) =>
    api("/api/ocr/settings", { worker_pool_size: workerPoolSize }, "PUT"),
  status: () => api("/api/ocr/status", undefined, "GET", { monitor: false }),
  restart: () => api("/api/ocr/restart", {}),
};

function setOperationalHealth(id, label, healthy) {
  const element = $(id);
  element.textContent = label;
  element.className = healthy ? "health-ok" : "health-problem";
}

function setOCRSettingsError(message = "") {
  $("ocr-settings-error").textContent = message;
  $("ocr-settings-error").hidden = !message;
}

function formatOCRBytes(value) {
  if (!Number.isFinite(value) || value < 0) return "—";
  return `${(value / 1024 ** 3).toFixed(1)} GB`;
}

function formatOCRMilliseconds(value) {
  if (!Number.isFinite(value)) return "—";
  return value >= 1000
    ? `${(value / 1000).toFixed(2)} s`
    : `${Math.round(value)} ms`;
}

function renderOCRStatus(status) {
  const input = $("ocr-worker-pool-size");
  const maximum = status.hardware_worker_ceiling;
  if (Number.isInteger(maximum) && maximum >= 1) input.max = String(maximum);
  ocrRecommendedWorkerCount = Number.isInteger(status.recommended_worker_count)
    ? status.recommended_worker_count
    : null;

  $("ocr-physical-cores").textContent =
    status.physical_cpu_cores == null
      ? "—"
      : `${status.physical_cpu_cores}${
          status.physical_cpu_fallback ? " (logical fallback)" : ""
        }`;
  $("ocr-logical-threads").textContent = status.logical_cpu_threads ?? "—";
  $("ocr-cpu-budget").textContent = Number.isFinite(status.cpu_capacity_percent)
    ? `${status.cpu_capacity_percent}%`
    : "—";
  $("ocr-cpu-ceiling").textContent = status.cpu_worker_ceiling ?? "—";
  $("ocr-ram-ceiling").textContent = status.ram_worker_ceiling ?? "—";
  $("ocr-hardware-ceiling").textContent = maximum ?? "—";
  $("ocr-pool-size").textContent = status.pool_size ?? "—";
  $("ocr-recommended-workers").textContent = ocrRecommendedWorkerCount ?? "—";
  $("ocr-busy-workers").textContent = status.busy_workers ?? "—";
  $("ocr-queued-requests").textContent = status.queued_requests ?? "—";
  const ocrHealthy = Number.isInteger(status.pool_size) && status.pool_size > 0;
  ocrOperationalHealthy = ocrHealthy;
  setOperationalHealth(
    "operate-ocr-state",
    ocrHealthy
      ? `Healthy · ${status.pool_size} worker${status.pool_size === 1 ? "" : "s"} · Queue ${status.queued_requests ?? 0}`
      : "Problem",
    ocrHealthy,
  );
  renderOperationalRecovery();
  $("ocr-restart-required").textContent = status.restart_required
    ? "Yes"
    : "No";
  $("ocr-cpu-usage").textContent = Number.isFinite(status.current_cpu_percent)
    ? `${status.current_cpu_percent.toFixed(1)}%`
    : "—";
  $("ocr-memory-usage").textContent = Number.isFinite(
    status.current_ram_percent,
  )
    ? `${status.current_ram_percent.toFixed(1)}%`
    : "—";
  $("ocr-available-ram").textContent = formatOCRBytes(
    status.available_ram_bytes,
  );
  $("ocr-average-queue").textContent = formatOCRMilliseconds(
    status.average_queue_wait_ms,
  );
  $("ocr-average-inference").textContent = formatOCRMilliseconds(
    status.average_inference_ms,
  );
  $("ocr-recommendation-reason").textContent =
    status.recommendation_reason ||
    "Collecting recent OCR performance samples.";
  const warning = $("ocr-settings-warning");
  warning.textContent = status.warning || "";
  warning.hidden = !status.warning;
  $("ocr-apply-recommendation").disabled =
    ocrRecommendedWorkerCount === null ||
    ocrRecommendedWorkerCount === status.configured_pool_size;

  const workers = Array.isArray(status.workers) ? status.workers : [];
  $("ocr-workers").replaceChildren(
    ...workers.map((worker) => {
      const item = document.createElement("span");
      item.textContent = `Worker ${worker.worker_id} — ${worker.state}`;
      return item;
    }),
  );
}

function scheduleOCRStatus(delay = 1500) {
  clearTimeout(ocrStatusTimer);
  if (ocrPollingStopped) return;
  ocrStatusTimer = setTimeout(refreshOCRStatus, delay);
}

async function refreshOCRStatus() {
  if (ocrStatusInFlight) return;
  ocrStatusInFlight = true;

  try {
    renderOCRStatus(await ocrService.status());
  } catch (error) {
    setOCRSettingsError(`OCR status unavailable: ${error.message}`);
    ocrOperationalHealthy = false;
    setOperationalHealth("operate-ocr-state", "Problem", false);
    renderOperationalRecovery();
  } finally {
    ocrStatusInFlight = false;
    scheduleOCRStatus();
  }
}

async function loadOCRSettings() {
  try {
    const [settings, status] = await Promise.all([
      ocrService.settings(),
      ocrService.status(),
    ]);
    const input = $("ocr-worker-pool-size");

    if (!ocrSettingsDirty && document.activeElement !== input) {
      input.value = String(settings.worker_pool_size);
    }
    renderOCRStatus(status);
    setOCRSettingsError();
  } catch (error) {
    setOCRSettingsError(`OCR settings unavailable: ${error.message}`);
    ocrOperationalHealthy = false;
    setOperationalHealth("operate-ocr-state", "Problem", false);
    renderOperationalRecovery();
  } finally {
    scheduleOCRStatus();
  }
}

async function saveOCRSettings(event) {
  event.preventDefault();
  const button = $("ocr-settings-save");
  button.disabled = true;
  setOCRSettingsError();

  try {
    const result = await ocrService.save(
      Number($("ocr-worker-pool-size").value),
    );
    ocrSettingsDirty = false;
    $("ocr-worker-pool-size").value = String(result.worker_pool_size);
    await refreshOCRStatus();
  } catch (error) {
    setOCRSettingsError(error.message);
  } finally {
    button.disabled = false;
  }
}

async function restartOCRWorkers() {
  const button = $("ocr-workers-restart");
  button.disabled = true;
  setOCRSettingsError();

  try {
    renderOCRStatus(await ocrService.restart());
  } catch (error) {
    setOCRSettingsError(error.message);
  } finally {
    button.disabled = false;
  }
}

async function applyOCRRecommendation() {
  if (!Number.isInteger(ocrRecommendedWorkerCount)) return;
  const button = $("ocr-apply-recommendation");
  button.disabled = true;
  setOCRSettingsError();

  try {
    const result = await ocrService.save(ocrRecommendedWorkerCount);
    ocrSettingsDirty = false;
    $("ocr-worker-pool-size").value = String(result.worker_pool_size);
    await refreshOCRStatus();
  } catch (error) {
    setOCRSettingsError(error.message);
  } finally {
    button.disabled =
      ocrRecommendedWorkerCount === null ||
      Number($("ocr-worker-pool-size").value) === ocrRecommendedWorkerCount;
  }
}

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

function scheduleRuntimeStatusRefresh(delay = RUNTIME_STATUS_MS) {
  clearTimeout(runtimeStatusTimer);
  runtimeStatusTimer = null;

  if (
    devicePollingStopped ||
    document.hidden ||
    state?.manual_backend !== "raspberry_pi"
  )
    return;
  runtimeStatusTimer = setTimeout(async () => {
    runtimeStatusTimer = null;
    await refreshStatus();
    scheduleRuntimeStatusRefresh();
  }, delay);
}

function selectedPiDevice() {
  const selected = deviceCatalog?.selected_device;
  return Array.isArray(deviceCatalog?.devices)
    ? deviceCatalog.devices.find((device) => device.id === selected)
    : null;
}

function selectedPiDeviceConnected() {
  if (state?.manual_backend !== "raspberry_pi") return true;
  if (deviceStatusStale) return false;
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

function cameraState(details) {
  const config = details?.config;

  if (
    !config ||
    config.camera_source === null ||
    config.camera_source === undefined
  )
    return { label: "Unassigned", className: "" };
  if (!config.enabled) return { label: "Disabled", className: "" };
  if (details?.status?.poll_failed)
    return { label: "Status unavailable", className: "" };
  return details?.status?.ready
    ? { label: "Available", className: "connected" }
    : { label: "Unavailable", className: "" };
}

function applyCameraPreviewGeometry(preview, width, height) {
  preview.className = preview.className
    .split(/\s+/)
    .filter((name) => name && name !== "portrait" && name !== "landscape")
    .join(" ");
  preview.style.aspectRatio = "";
  preview.style.maxWidth = "";
  if (
    !Number.isFinite(width) ||
    !Number.isFinite(height) ||
    width <= 0 ||
    height <= 0
  )
    return;

  preview.className += height > width ? " portrait" : " landscape";
  preview.style.aspectRatio = `${width} / ${height}`;
  // Constrain the width that corresponds to a 460px-tall frame. Combined
  // with width: 100%, this preserves the source ratio in narrow cards too.
  preview.style.maxWidth = `${Math.round((460 * width) / height)}px`;
}

function stopDevicePreviews() {
  devicePreviewGeneration++;
  for (const timer of devicePreviewTimers.values()) clearTimeout(timer);
  devicePreviewTimers.clear();
}

function startDevicePreview(deviceId, image, placeholder) {
  // Unit-test DOM shims do not decode images. Avoid creating background timers there.
  if (typeof image.decode !== "function") return;

  const generation = devicePreviewGeneration;
  const schedule = (delay) => {
    if (generation !== devicePreviewGeneration || devicePollingStopped) return;
    const timer = setTimeout(() => {
      if (document.hidden) return schedule(PREVIEW_BACKGROUND_MS);
      image.src = `/api/devices/${encodeURIComponent(deviceId)}/camera/frame?t=${Date.now()}`;
    }, delay);
    devicePreviewTimers.set(deviceId, timer);
  };

  image.addEventListener("load", () => {
    placeholder.hidden = true;
    image.hidden = false;
    schedule(333);
  });

  image.addEventListener("error", () => {
    image.hidden = true;
    placeholder.hidden = false;
    placeholder.textContent = "Camera unavailable";
    schedule(1000);
  });

  image.src = `/api/devices/${encodeURIComponent(deviceId)}/camera/frame?t=${Date.now()}`;
}

function renderDevices() {
  stopDevicePreviews();
  const devices = Array.isArray(deviceCatalog?.devices)
    ? deviceCatalog.devices
    : [];
  const enrollment = deviceCatalog?.enrollment || { active: false };
  const selected = deviceCatalog?.selected_device;

  $("device-state").textContent = deviceStatusStale
    ? "Device status unavailable. Retrying automatically."
    : devices.length
      ? `${devices.length} registered phone${devices.length === 1 ? "" : "s"}. All cameras remain visible; one phone receives control.`
      : "No phones are registered with this Pi yet.";

  const cards = devices.map((device) => {
    const details = deviceCameraDetails?.[device.id] || {};
    const camera = details.config || {};
    const status = cameraState(details);
    const deviceJobs = [...jobCatalog]
      .reverse()
      .filter((job) => job.device_id === device.id);
    const recoveryJob = deviceJobs.find(
      (job) =>
        ["FAILED", "INTERRUPTED"].includes(job.status) &&
        job.recovery_status !== "READY",
    );
    const runningJob = deviceJobs.find((job) => job.status === "RUNNING");
    const manualWorkflowNeedsAttention =
      workflowRuntime?.status === "failed" &&
      workflowRuntime?.device_id === device.id;

    const operationalState = recoveryJob
      ? recoveryJob.recovery_status === "RECOVERING"
        ? "recovering"
        : "needs-attention"
      : manualWorkflowNeedsAttention
        ? "needs-attention"
        : runningJob
          ? "running"
          : device.connected
            ? "ready"
            : "offline";
    const card = document.createElement("article");
    card.className =
      `device-item device-card--${operationalState}` +
      (!device.connected && operationalState !== "offline"
        ? " device-card--offline"
        : "") +
      (device.id === selected ? " selected" : "");
    card.dataset.state = operationalState;

    const heading = document.createElement("div");
    heading.className = "device-item-heading";
    const name = document.createElement("h3");
    name.textContent = device.name || device.id;
    const logicalId = document.createElement("span");
    logicalId.className = "device-logical-id";
    logicalId.textContent = device.id;
    const badges = document.createElement("div");
    badges.className = "device-badges";

    if (device.default || device.id === deviceCatalog.default_device) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "Default";
      badges.append(badge);
    }

    if (device.id === selected) {
      const badge = document.createElement("span");
      badge.className = "badge control-target";
      badge.textContent = "Control target";
      badges.append(badge);
    }
    const operationalBadge = document.createElement("span");
    operationalBadge.className = `badge device-state-badge device-state-badge--${operationalState}`;
    operationalBadge.textContent =
      operationalState === "needs-attention"
        ? "Needs Attention"
        : operationalState === "recovering"
          ? "Recovering"
          : operationalState === "running"
            ? "Running"
            : operationalState === "offline"
              ? "Offline"
              : "Ready";
    badges.append(operationalBadge);
    heading.append(name, logicalId, badges);

    const states = document.createElement("div");
    states.className = "device-states";
    const bluetooth = document.createElement("span");
    bluetooth.className =
      "device-status" +
      (!deviceStatusStale && device.connected ? " connected" : "");

    bluetooth.textContent = deviceStatusStale
      ? "Bluetooth: ? Status unavailable"
      : `Bluetooth: ● ${device.connected ? "Connected" : "Disconnected"}`;

    const cameraStatus = document.createElement("span");
    cameraStatus.className = "device-status " + status.className;
    cameraStatus.textContent = deviceStatusStale
      ? "Camera: ? Status unavailable"
      : `Camera: ● ${status.label}`;
    states.append(bluetooth, cameraStatus);

    const mac = document.createElement("p");
    mac.className = "device-mac admin-only";
    mac.textContent = device.bluetooth_address || "Bluetooth MAC not paired";

    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "device-preview";
    applyCameraPreviewGeometry(
      preview,
      details.status?.width,
      details.status?.height,
    );

    preview.disabled = device.id === selected || deviceManagementBlocked();
    preview.setAttribute?.(
      "aria-label",
      `Select ${device.name || device.id} for control`,
    );

    const previewImage = document.createElement("img");
    previewImage.alt = `${device.name || device.id} live camera preview`;
    previewImage.hidden = true;

    const previewPlaceholder = document.createElement("span");
    previewPlaceholder.textContent =
      status.label === "Available"
        ? "Loading camera…"
        : `Camera ${status.label.toLowerCase()}`;
    preview.append(previewImage, previewPlaceholder);
    preview.addEventListener("click", () => selectDashboardDevice(device.id));

    if (
      camera.enabled &&
      camera.camera_source !== null &&
      camera.camera_source !== undefined
    )
      startDevicePreview(device.id, previewImage, previewPlaceholder);

    const summary = document.createElement("p");
    summary.className = "camera-summary admin-only";
    summary.textContent =
      camera.camera_source === null || camera.camera_source === undefined
        ? "No camera assigned"
        : `Camera ${camera.camera_source} · ${camera.rotation ?? 0}°`;

    const binding = accountBindings.find(
      (account) => account.device_id === device.id,
    );
    const accountSummary = document.createElement("div");
    accountSummary.className = "account-summary";
    const accountHeading = document.createElement("strong");
    accountHeading.textContent = "Assigned Account";
    const accountName = document.createElement("span");
    accountName.textContent = binding
      ? `${binding.provider ? `${binding.provider} · ` : ""}${binding.display_name}${
          binding.enabled ? "" : " · Disabled"
        }`
      : "No account assigned";
    const accountId = document.createElement("span");
    accountId.textContent = binding?.account_id || "—";
    accountSummary.append(accountHeading, accountName, accountId);

    let issueSummary = null;
    if (operationalState === "needs-attention") {
      issueSummary = document.createElement("p");
      issueSummary.className = "device-issue-summary";

      const issueLabel = document.createElement("strong");
      issueLabel.textContent = "Last issue:";

      const issueMessage = document.createElement("span");
      const manualWorkflowMessage =
        workflowRuntime?.device_id === device.id
          ? workflowRuntime?.message
          : null;

      issueMessage.textContent = friendlyOperationalError(
        recoveryJob?.recovery_error ||
          recoveryJob?.error ||
          manualWorkflowMessage ||
          "This phone needs attention.",
      );
      issueSummary.append(issueLabel, issueMessage);
    }

    const actions = document.createElement("div");
    actions.className = "device-item-actions";

    const select = document.createElement("button");
    select.type = "button";
    select.textContent = device.id === selected ? "Controlling" : "Select";
    select.disabled = device.id === selected || deviceManagementBlocked();
    select.addEventListener("click", () => selectDashboardDevice(device.id));

    let resolve = null;
    if (operationalState === "needs-attention") {
      resolve = document.createElement("button");
      resolve.type = "button";
      resolve.className = "primary device-resolve";
      resolve.textContent = "Resolve";
      resolve.disabled = deviceManagementBlocked();
      resolve.addEventListener("click", () =>
        resolveDeviceIssue(device.id, recoveryJob?.job_id || null),
      );
    }

    const settings = document.createElement("button");
    settings.className = "admin-only";
    settings.type = "button";
    settings.textContent = "Camera Settings";
    settings.disabled = deviceManagementBlocked();
    settings.addEventListener("click", () => openCameraSettings(device));

    const account = document.createElement("button");
    account.className = "admin-only";
    account.type = "button";
    account.textContent = binding ? "Account Settings" : "Assign Account";
    account.disabled = deviceManagementBlocked();
    account.addEventListener("click", () =>
      openAccountSettings(device, binding),
    );

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "stop admin-only";
    remove.textContent = deviceDeletesInFlight.has(device.id)
      ? "Deleting…"
      : "Delete";
    remove.disabled =
      deviceDeletesInFlight.has(device.id) || deviceManagementBlocked();
    remove.addEventListener("click", () => deleteDevice(device, remove));
    actions.append(
      ...(resolve ? [resolve] : []),
      select,
      settings,
      account,
      remove,
    );
    card.append(heading, states, mac, preview, summary, accountSummary);
    if (issueSummary) card.append(issueSummary);
    card.append(actions);
    return card;
  });

  $("device-list").replaceChildren(...cards);
  $("device-enrollment-active").hidden = !enrollment.active;
  $("device-enrollment-form").hidden =
    !addPhonePanelOpen || !!enrollment.active;
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
  renderJobs();
}

function renderJobs() {
  const jobs = Array.isArray(jobCatalog) ? jobCatalog : [];
  $("jobs-state").textContent = jobs.length
    ? `${jobs.length} persistent job${jobs.length === 1 ? "" : "s"}.`
    : "No external jobs have been submitted.";

  const cards = [...jobs]
    .reverse()
    .slice(0, 50)
    .map((job) => {
      const item = document.createElement("article");
      item.className = "job-item";
      const id = document.createElement("strong");
      id.textContent = job.job_id;
      const account = document.createElement("span");
      account.textContent = job.bank
        ? `Account: ${job.account_id} · ${job.bank}`
        : `Account: ${job.account_id}`;
      const device = document.createElement("span");
      device.textContent = `Device: ${job.device_id}`;
      const workflow = document.createElement("span");
      workflow.textContent = job.action
        ? `Workflow: ${job.workflow} · ${job.action}`
        : `Workflow: ${job.workflow}`;
      const status = document.createElement("span");
      status.textContent = job.queue_position
        ? `${job.status} · queue ${job.queue_position}`
        : job.status;

      const created = document.createElement("span");
      created.textContent = `Queued: ${job.created_at || "—"}`;
      const activity = document.createElement("span");
      activity.textContent = job.finished_at
        ? `Finished: ${job.finished_at}`
        : job.started_at
          ? `Started: ${job.started_at}`
          : "Not started";

      item.append(id, account, device, workflow, status, created, activity);

      if (job.error) {
        const error = document.createElement("span");
        error.className = "job-error";
        const technical = String(job.error);
        error.textContent = friendlyOperationalError(technical);
        item.append(error);
        if (error.textContent !== technical) {
          const details = document.createElement("details");
          details.className = "job-error-details";
          const summary = document.createElement("summary");
          summary.textContent = "Technical details";
          const message = document.createElement("code");
          message.textContent = technical;
          details.append(summary, message);
          item.append(details);
        }
      }
      if (job.result) {
        const result = document.createElement("span");
        result.className = "job-result";
        result.textContent = job.result;
        item.append(result);
      }
      if (job.recovery_status) {
        const recovery = document.createElement("span");
        recovery.className = `job-recovery-state job-recovery-${job.recovery_status.toLowerCase()}`;
        recovery.textContent =
          job.recovery_status === "READY"
            ? "Phone state: Ready"
            : job.recovery_status === "RECOVERING"
              ? "Phone state: Recovering"
              : "Phone state: Needs Attention";
        item.append(recovery);
      }
      if (["FAILED", "INTERRUPTED"].includes(job.status)) {
        const actions = document.createElement("div");
        actions.className = "job-recovery-actions";
        const recoveryAttempted =
          !!job.recovery_run_id || !!job.recovery_error || !!job.recovered_at;
        if (job.recovery_workflow_id && !recoveryAttempted) {
          const recover = document.createElement("button");
          recover.type = "button";
          recover.className = "primary";
          recover.textContent = "Recover Phone";
          recover.disabled = recoveryRequestInFlight;
          recover.addEventListener("click", () =>
            startPhoneRecovery(job.job_id),
          );
          actions.append(recover);
        }
        if (
          !job.recovery_workflow_id ||
          (job.recovery_status === "NEEDS_ATTENTION" && recoveryAttempted)
        ) {
          const assist = document.createElement("button");
          assist.type = "button";
          assist.textContent = "Manual Assistance";
          assist.addEventListener("click", () =>
            openManualAssistance(job.job_id),
          );
          actions.append(assist);
        }
        if (actions.children.length) item.append(actions);
      }
      if (["QUEUED", "WAITING_FOR_DEVICE", "RUNNING"].includes(job.status)) {
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "job-cancel stop";
        const cancellationPending =
          job.cancel_requested || jobCancelsInFlight.has(job.job_id);
        cancel.textContent = job.cancel_requested
          ? "Cancellation requested"
          : jobCancelsInFlight.has(job.job_id)
            ? "Cancelling…"
            : "Cancel";
        cancel.disabled = cancellationPending;
        cancel.addEventListener("click", async () => {
          if (jobCancelsInFlight.has(job.job_id)) return;
          if (
            job.status === "RUNNING" &&
            typeof window.confirm === "function" &&
            !window.confirm(
              "Cancel this running job? The physical phone may already have changed.",
            )
          )
            return;
          jobCancelsInFlight.add(job.job_id);
          renderJobs();
          try {
            const updated = await jobService.cancel(job.job_id);
            jobCatalog = jobCatalog.map((candidate) =>
              candidate.job_id === updated.job_id ? updated : candidate,
            );
          } catch (error) {
            notice(`Could not cancel job: ${error.message}`, true);
          } finally {
            jobCancelsInFlight.delete(job.job_id);
            renderJobs();
          }
        });
        item.append(cancel);
      }
      return item;
    });

  $("jobs-list").replaceChildren(...cards);
}

function friendlyOperationalError(message) {
  const value = message.toLowerCase();
  if (/calibrat/.test(value)) return "Calibration required";
  if (/camera/.test(value)) return "Camera unavailable";
  if (/ocr|paddle|text detection/.test(value)) return "OCR unavailable";
  if (/busy|lock|ownership/.test(value)) return "Device busy";
  if (/raspberry|pi api|pi unavailable|cannot reach pi/.test(value))
    return "Raspberry Pi unavailable";
  if (/disconnect|bluetooth|phone unavailable/.test(value))
    return "Phone disconnected";
  if (/interrupt|restart/.test(value)) return "Job interrupted";
  return message;
}

function currentOperationalIssue() {
  if (!state) return null;
  const piMode = state.manual_backend === "raspberry_pi";
  if (connectionState !== "connected")
    return {
      kind: "connection",
      label: "Controller connection interrupted",
      recommendation: "Recheck the controller and Raspberry Pi connection.",
      manualAvailable: false,
    };
  if (piMode && (deviceStatusStale || state.pi?.api !== "online"))
    return {
      kind: "connection",
      label: "Raspberry Pi unavailable",
      recommendation: "Recheck the Raspberry Pi connection.",
      manualAvailable: false,
    };
  if (!state.device || (piMode && state.pi?.phone !== "connected"))
    return {
      kind: "device",
      label: "Phone disconnected",
      recommendation: "Reconnect the phone, then recheck its status.",
      manualAvailable: false,
    };
  if (!state.camera?.ready)
    return {
      kind: "camera",
      label: "Camera unavailable",
      recommendation:
        "Check the camera connection or open Admin for camera settings.",
      manualAvailable: false,
    };
  if (!state.calibrated)
    return {
      kind: "calibration",
      label: "Calibration required",
      recommendation: "Open Admin and calibrate the selected phone.",
      manualAvailable: false,
    };

  const selectedDeviceId = deviceCatalog?.selected_device;
  const targetJob = recoveryTargetJobId
    ? jobCatalog.find((job) => job.job_id === recoveryTargetJobId)
    : [...jobCatalog]
        .reverse()
        .find(
          (job) =>
            (!selectedDeviceId || job.device_id === selectedDeviceId) &&
            ["FAILED", "INTERRUPTED"].includes(job.status) &&
            job.recovery_status !== "READY",
        );

  if (targetJob?.recovery_status === "READY") return null;
  if (targetJob?.recovery_status === "RECOVERING")
    return {
      kind: "job",
      job: targetJob,
      label: "Phone recovery in progress",
      recommendation:
        "The configured recovery workflow is returning the phone to its verified ready screen.",
      state: "Recovering",
      recoverAvailable: false,
      manualAvailable: false,
    };
  if (targetJob) {
    const recoveryAttempted =
      !!targetJob.recovery_run_id ||
      !!targetJob.recovery_error ||
      !!targetJob.recovered_at;
    return {
      kind: "job",
      job: targetJob,
      label:
        targetJob.recovery_status === "NEEDS_ATTENTION" && recoveryAttempted
          ? "Automatic recovery failed"
          : friendlyOperationalError(
              targetJob.error ||
                (targetJob.status === "INTERRUPTED"
                  ? "Job interrupted"
                  : "Job needs attention"),
            ),
      recommendation:
        targetJob.recovery_status === "NEEDS_ATTENTION" && recoveryAttempted
          ? "Resolve the phone issue and verify the expected starting screen before further work."
          : targetJob.recovery_workflow_id
            ? "Run the configured recovery workflow. The failed job will not be retried."
            : "No recovery workflow is configured. Resolve the phone issue manually or open Admin to configure one.",
      state: "Needs Attention",
      recoverAvailable: !!targetJob.recovery_workflow_id && !recoveryAttempted,
      manualAvailable: !targetJob.recovery_workflow_id || recoveryAttempted,
    };
  }
  if (ocrOperationalHealthy === false)
    return {
      kind: "ocr",
      label: "OCR unavailable",
      recommendation: "Open Admin to inspect the OCR worker pool.",
      manualAvailable: false,
    };
  if (workflowRuntime?.status === "waiting_manual")
    return {
      kind: "workflow",
      label: "Manual checkpoint",
      recommendation:
        workflowRuntime.checkpoint_message ||
        "Complete the requested phone action, then use Resume Checkpoint.",
      state: "Needs Attention",
      recoverAvailable: false,
      manualAvailable: true,
    };
  if (workflowRuntime?.status === "failed")
    return {
      kind: "workflow",
      label: friendlyOperationalError(
        workflowRuntime.message || "Workflow needs attention",
      ),
      recommendation:
        "Inspect the phone before deciding whether to restart the workflow.",
      state: "Needs Attention",
      recoverAvailable: false,
      manualAvailable: true,
    };
  return null;
}

function renderOperationalRecovery() {
  // Lightweight unit-test DOMs and embedded clients may omit the page body.
  if (!document.body) return;
  let issue = currentOperationalIssue();
  const recoveryJob = recoveryTargetJobId
    ? jobCatalog.find((job) => job.job_id === recoveryTargetJobId)
    : [...jobCatalog]
        .reverse()
        .find(
          (job) =>
            ["FAILED", "INTERRUPTED"].includes(job.status) &&
            job.recovery_status !== "READY",
        );
  const recovered = recoveryJob?.recovery_status === "READY";
  const recoveryAttempted =
    !!recoveryJob?.recovery_run_id ||
    !!recoveryJob?.recovery_error ||
    !!recoveryJob?.recovered_at;
  if (
    readyVerificationJobId &&
    recoveryJob?.job_id === readyVerificationJobId &&
    recoveryJob.recovery_status === "READY"
  ) {
    if (manual || readyVerificationRestoreInFlight) {
      readyVerificationMessage =
        "Ready screen detected. Releasing manual control…";
      void finishSuccessfulReadyVerification(recoveryJob.job_id);
    } else {
      readyVerificationJobId = null;
      readyVerificationFailedJobId = null;
      readyVerificationMessage = "";
      readyVerificationRestoreManual = false;
      manualAssistanceOpen = false;
      recoveryPanelOpen = false;
      recoveryTargetJobId = null;
      renderDevices();
      issue = currentOperationalIssue();
    }
  } else if (
    readyVerificationJobId &&
    recoveryJob?.job_id === readyVerificationJobId &&
    recoveryJob.recovery_status === "NEEDS_ATTENTION" &&
    recoveryAttempted
  ) {
    readyVerificationFailedJobId = readyVerificationJobId;
    readyVerificationJobId = null;
    readyVerificationMessage = "Expected starting screen was not detected.";
    manualAssistanceOpen = true;
    recoveryPanelOpen = true;
  }
  const attention = $("operate-attention");
  attention.hidden = !issue;
  if (issue) {
    const affectedDevices = new Set(
      jobCatalog
        .filter(
          (job) =>
            ["FAILED", "INTERRUPTED"].includes(job.status) &&
            job.recovery_status !== "READY",
        )
        .map((job) => job.device_id),
    );
    $("operate-attention-message").textContent =
      affectedDevices.size > 1
        ? `${affectedDevices.size} devices need attention`
        : issue.label;
  }

  $("operate-recovery").hidden = !recoveryPanelOpen || (!issue && !recovered);
  if (issue) {
    $("operate-recovery-issue").textContent = issue.label;
    $("operate-recovery-recommendation").textContent = issue.recommendation;
    $("operate-recovery-state").textContent = issue.state || "Needs Attention";
  } else if (recovered) {
    $("operate-recovery-issue").textContent = "Phone recovered successfully.";
    $("operate-recovery-recommendation").textContent =
      "The configured ready-screen condition was detected. Review the failed job before starting new business work.";
    $("operate-recovery-state").textContent = "Ready";
  }
  $("operate-recover-phone").hidden = !issue?.recoverAvailable;
  $("operate-recover-phone").disabled = recoveryRequestInFlight;
  $("operate-open-assistance").hidden = !issue?.manualAvailable;
  $("operate-verify-finish").disabled =
    recoveryRequestInFlight ||
    readyVerificationRestoreInFlight ||
    readyVerificationFinishInFlight;
  if (recoveryJob?.recovery_status === "RECOVERING")
    $("operate-recovery-result").textContent =
      "Recovery workflow is running. The failed workflow will not be retried.";
  else if (recoveryJob?.job_id === readyVerificationFailedJobId)
    $("operate-recovery-result").textContent =
      "Expected starting screen was not detected.";
  else if (
    recoveryJob?.recovery_status === "NEEDS_ATTENTION" &&
    recoveryAttempted
  )
    $("operate-recovery-result").textContent =
      recoveryJob.recovery_error ||
      "Automatic recovery failed. Resolve the phone issue manually.";
  else if (recovered)
    $("operate-recovery-result").textContent =
      "Phone recovered successfully. Status: Ready.";
  if (manualAssistanceOpen && readyVerificationMessage)
    $("operate-recovery-result").textContent = readyVerificationMessage;
  document.body.dataset.manualAssistance = manualAssistanceOpen
    ? "open"
    : "closed";
  if (
    recoveryJob?.job_id === readyVerificationFailedJobId &&
    readyVerificationRestoreManual &&
    !manual
  )
    void restoreManualControlAfterVerificationFailure();
}

async function startPhoneRecovery(jobId) {
  if (recoveryRequestInFlight) return;
  recoveryRequestInFlight = true;
  recoveryTargetJobId = jobId;
  readyVerificationJobId = null;
  readyVerificationFailedJobId = null;
  readyVerificationMessage = "";
  readyVerificationRestoreManual = false;
  recoveryPanelOpen = true;
  $("operate-recovery-result").textContent = "Starting recovery workflow…";
  renderOperationalRecovery();
  try {
    const updated = await jobService.recover(jobId);
    jobCatalog = jobCatalog.map((job) =>
      job.job_id === updated.job_id ? updated : job,
    );
  } catch (error) {
    $("operate-recovery-result").textContent =
      `Recovery could not start: ${friendlyOperationalError(error.message)}`;
  } finally {
    recoveryRequestInFlight = false;
    renderJobs();
    renderOperationalRecovery();
  }
}

function openManualAssistance(jobId = null) {
  if (jobId !== recoveryTargetJobId) {
    readyVerificationJobId = null;
    readyVerificationFailedJobId = null;
    readyVerificationMessage = "";
    readyVerificationRestoreManual = false;
  }
  recoveryTargetJobId = jobId;
  recoveryPanelOpen = true;
  manualAssistanceOpen = true;
  renderOperationalRecovery();
  $("manual-control-card").scrollIntoView?.({
    block: "start",
    behavior: "smooth",
  });
}

async function verifyPhoneReady() {
  const job = recoveryTargetJobId
    ? jobCatalog.find((candidate) => candidate.job_id === recoveryTargetJobId)
    : null;

  if (readyVerificationRestorePromise) await readyVerificationRestorePromise;

  const restoreManualOnFailure = manual;
  if (restoreManualOnFailure) {
    try {
      await disable();
    } catch (error) {
      notice(error.message, true);
      readyVerificationMessage =
        "Manual control could not be stopped safely. Verification was not started.";
      readyVerificationRestoreManual = false;
      recoveryPanelOpen = true;
      renderOperationalRecovery();
      return;
    }
  }

  const manualWorkflowIssue =
    !job && ["failed", "waiting_manual"].includes(workflowRuntime?.status);

  if (!job?.recovery_workflow_id) {
    if (manualWorkflowIssue) {
      // Operator confirms they have manually returned the phone
      // to the expected state.
      workflowRuntime = null;

      readyVerificationJobId = null;
      readyVerificationFailedJobId = null;
      readyVerificationMessage = "";
      readyVerificationRestoreManual = false;

      manualAssistanceOpen = false;
      recoveryPanelOpen = false;
      recoveryTargetJobId = null;

      renderDevices();
      renderOperationalRecovery();

      notice("Phone marked ready.");
      return;
    }

    readyVerificationMessage =
      "Ready state cannot be verified because no recovery workflow is configured.";
    readyVerificationRestoreManual = restoreManualOnFailure;
    recoveryPanelOpen = true;
    renderOperationalRecovery();

    if (readyVerificationRestoreManual)
      await restoreManualControlAfterVerificationFailure();

    return;
  }

  if (recoveryRequestInFlight) return;
  recoveryRequestInFlight = true;
  readyVerificationRestoreManual = restoreManualOnFailure;
  readyVerificationMessage = "Verifying the configured ready screen…";
  renderOperationalRecovery();
  try {
    const updated = await jobService.verifyReady(job.job_id);
    jobCatalog = jobCatalog.map((candidate) =>
      candidate.job_id === updated.job_id ? updated : candidate,
    );
    readyVerificationJobId = job.job_id;
    readyVerificationFailedJobId = null;
  } catch (error) {
    readyVerificationMessage = `Ready state could not be verified: ${friendlyOperationalError(error.message)}`;
    if (readyVerificationRestoreManual)
      await restoreManualControlAfterVerificationFailure();
  } finally {
    recoveryRequestInFlight = false;
    renderJobs();
    renderOperationalRecovery();
  }
}

async function closeManualAssistance() {
  if (manual) {
    try {
      await disable();
    } catch (error) {
      notice(error.message, true);
    }
  }
  manualAssistanceOpen = false;
  readyVerificationJobId = null;
  readyVerificationFailedJobId = null;
  readyVerificationMessage = "";
  readyVerificationRestoreManual = false;
  renderOperationalRecovery();
}

function viewOperationalDetails() {
  const issue = currentOperationalIssue();
  if (issue?.kind === "job") {
    const details = $("jobs-list").querySelector?.("details");
    if (details) details.open = true;
    $("jobs").scrollIntoView?.({ block: "start", behavior: "smooth" });
    return;
  }
  if (issue?.kind === "workflow") {
    $("flow-run-state").scrollIntoView?.({
      block: "center",
      behavior: "smooth",
    });
    return;
  }
  setDashboardMode("admin");
}

function setAccountSettingsError(message = "") {
  $("account-settings-error").textContent = message;
  $("account-settings-error").hidden = !message;
}

function closeAccountSettings() {
  accountSettingsDeviceId = null;
  accountSettingsAccountId = null;
  $("account-settings-dialog").hidden = true;
  setAccountSettingsError();
}

function openAccountSettings(device, binding = null) {
  accountSettingsDeviceId = device.id;
  accountSettingsAccountId = binding?.account_id || null;
  $("account-settings-title").textContent =
    `Assigned Account · ${device.name || device.id}`;
  $("account-settings-id").value = binding?.account_id || "";
  $("account-settings-id").readOnly = !!binding;
  $("account-settings-name").value = binding?.display_name || "";
  $("account-settings-provider").value = binding?.provider || "";
  $("account-settings-enabled").checked = binding?.enabled ?? true;
  const options = (deviceCatalog?.devices || []).map((candidate) => {
    const option = document.createElement("option");
    option.value = candidate.id;
    option.textContent = candidate.name || candidate.id;
    return option;
  });
  $("account-settings-device").replaceChildren(...options);
  $("account-settings-device").value = binding?.device_id || device.id;
  $("account-settings-remove").hidden = !binding;
  $("account-settings-dialog").hidden = false;
  setAccountSettingsError();
  $(binding ? "account-settings-name" : "account-settings-id").focus();
}

async function saveAccountSettings(event) {
  event.preventDefault();
  const binding = {
    display_name: $("account-settings-name").value.trim(),
    provider: $("account-settings-provider").value.trim() || null,
    device_id: $("account-settings-device").value,
    enabled: $("account-settings-enabled").checked,
  };
  const button = $("account-settings-save");
  button.disabled = true;
  setAccountSettingsError();
  try {
    if (accountSettingsAccountId) {
      await accountService.update(accountSettingsAccountId, binding);
    } else {
      await accountService.create({
        account_id: $("account-settings-id").value.trim(),
        ...binding,
      });
    }
    closeAccountSettings();
    deviceRenderSignature = null;
    await loadDevices();
    notice("Account assignment saved.");
  } catch (error) {
    setAccountSettingsError(error.message);
  } finally {
    button.disabled = false;
  }
}

async function removeAccountSettings() {
  if (!accountSettingsAccountId) return;
  const button = $("account-settings-remove");
  button.disabled = true;
  setAccountSettingsError();
  try {
    await accountService.remove(accountSettingsAccountId);
    closeAccountSettings();
    deviceRenderSignature = null;
    await loadDevices();
    notice("Account assignment removed.");
  } catch (error) {
    setAccountSettingsError(error.message);
  } finally {
    button.disabled = false;
  }
}

function reconcileAddPhonePanel(devices, enrollment) {
  const registered = new Set(devices.map((device) => device.id));
  const typedDeviceId = String($("device-enrollment-id").value || "").trim();
  const completedTarget = addPhoneEnrollmentTarget || typedDeviceId;
  const targetWasAdded =
    completedTarget &&
    registered.has(completedTarget) &&
    (addPhoneEnrollmentTarget !== null ||
      !addPhoneInitialDeviceIds.has(completedTarget));
  if (targetWasAdded) {
    addPhonePanelOpen = false;
    addPhoneEnrollmentTarget = null;
    addPhoneInitialDeviceIds = new Set();
    $("device-enrollment-id").value = "";
    setDeviceError();
    return;
  }
  if (enrollment?.active) addPhonePanelOpen = false;
  if (
    addPhoneEnrollmentTarget &&
    !enrollment?.active &&
    !registered.has(addPhoneEnrollmentTarget)
  ) {
    // Enrollment ended without registering the requested phone. Keep the
    // typed ID available for a deliberate retry when the user reopens Add Phone.
    addPhoneEnrollmentTarget = null;
  }
}

async function loadDevices() {
  if (deviceRefreshInFlight || state?.manual_backend !== "raspberry_pi") return;
  deviceRefreshInFlight = true;
  try {
    const wasStale = deviceStatusStale;
    const previousPiState = JSON.stringify(state?.pi || null);
    const nextCatalog = await deviceService.list();
    deviceCatalog = nextCatalog;
    const devices = Array.isArray(nextCatalog.devices)
      ? nextCatalog.devices
      : [];
    reconcileAddPhonePanel(devices, nextCatalog.enrollment);
    const [cameraEntries, accountResult, jobResult] = await Promise.all([
      Promise.all(
        devices.map(async (device) => {
          try {
            return [device.id, await deviceService.camera(device.id)];
          } catch (error) {
            const previous = deviceCameraDetails?.[device.id] || {};
            return [
              device.id,
              {
                config: previous.config || null,
                status: {
                  ...(previous.status || {}),
                  poll_failed: true,
                  error: error.message,
                },
              },
            ];
          }
        }),
      ),
      accountService.list().catch(() => ({ accounts: accountBindings })),
      jobService.list().catch(() => ({ jobs: jobCatalog })),
    ]);
    const nextCameras = Object.fromEntries(cameraEntries);
    accountBindings = Array.isArray(accountResult.accounts)
      ? accountResult.accounts
      : accountBindings;
    jobCatalog = Array.isArray(jobResult.jobs) ? jobResult.jobs : jobCatalog;
    const nextSignature = JSON.stringify({
      devices: nextCatalog.devices,
      enrollment: nextCatalog.enrollment,
      default_device: nextCatalog.default_device,
      selected_device: nextCatalog.selected_device,
      cameras: nextCameras,
      accounts: accountBindings,
      jobs: jobCatalog,
      status_unavailable: false,
    });
    deviceCameraDetails = nextCameras;
    deviceRefreshLastSuccessAt = Date.now();
    deviceRefreshError = "";
    deviceStatusStale = false;
    if (state) {
      const selected = devices.find(
        (device) => device.id === nextCatalog.selected_device,
      );
      state.pi = {
        api: "online",
        phone: selected?.connected ? "connected" : "waiting",
        message: selected?.connected
          ? "Phone Connected"
          : "Pi Online / Phone Waiting",
      };
    }
    if (!addPhonePanelOpen) setDeviceError();
    if (
      nextSignature !== deviceRenderSignature ||
      wasStale ||
      previousPiState !== JSON.stringify(state?.pi || null)
    ) {
      deviceRenderSignature = nextSignature;
      renderDevices();
      render();
      window.phoneRecorder?.deviceStateChanged?.();
    }
  } catch (error) {
    deviceRefreshError = error.message;
    const stale =
      deviceRefreshLastSuccessAt === null ||
      Date.now() - deviceRefreshLastSuccessAt >= DEVICE_STATUS_STALE_MS;
    if (stale !== deviceStatusStale) {
      deviceStatusStale = stale;
      renderDevices();
      render();
      window.phoneRecorder?.deviceStateChanged?.();
    }
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
    return false;
  }
  try {
    clearPreview(`Switching to ${deviceId} camera…`);
    const result = await deviceService.select(deviceId);
    if (deviceCatalog && result.selected_device === deviceId)
      deviceCatalog = { ...deviceCatalog, selected_device: deviceId };
    manual = false;
    controlVersion++;
    await refreshStatus();
    await loadDevices();
    notice(`Selected ${deviceId}. Calibrate this phone if required.`);
    return deviceCatalog?.selected_device === deviceId;
  } catch (error) {
    setDeviceError(error.message);
    return false;
  }
}

async function resolveDeviceIssue(deviceId, jobId) {
  if (
    deviceId !== deviceCatalog?.selected_device &&
    !(await selectDashboardDevice(deviceId))
  )
    return;
  if (deviceCatalog?.selected_device !== deviceId) return;

  recoveryTargetJobId = jobId;
  recoveryPanelOpen = true;
  readyVerificationJobId = null;
  readyVerificationFailedJobId = null;
  readyVerificationMessage = "";
  readyVerificationRestoreManual = false;
  const issue = currentOperationalIssue();
  manualAssistanceOpen = !!issue?.manualAvailable;
  renderOperationalRecovery();
  $(
    manualAssistanceOpen ? "manual-control-card" : "operate-recovery",
  ).scrollIntoView?.({
    block: "start",
    behavior: "smooth",
  });
}

function setCameraSettingsError(message = "") {
  $("camera-settings-error").textContent = message;
  $("camera-settings-error").hidden = !message;
}

function closeCameraSettings() {
  cameraSettingsDeviceId = null;
  $("camera-settings-dialog").hidden = true;
  $("camera-test-image").hidden = true;
  $("camera-test-placeholder").hidden = false;
  applyCameraPreviewGeometry($("camera-test-preview"));
  setCameraSettingsError();
}

function cameraSourceValue(value) {
  return /^\d+$/.test(value) ? Number(value) : value;
}

function populateCameraSources(cameras, current) {
  const input = $("camera-settings-source");
  const list = $("camera-source-options");
  const values = new Set();
  const options = [];
  for (const camera of cameras || []) {
    values.add(String(camera.source));
    const option = document.createElement("option");
    option.value = String(camera.source);
    option.textContent = `Camera ${camera.source} — ${camera.available ? "Available" : "Unavailable"}`;
    options.push(option);
  }
  if (
    current !== null &&
    current !== undefined &&
    !values.has(String(current))
  ) {
    const option = document.createElement("option");
    option.value = String(current);
    option.textContent = `Saved source ${current} — Unavailable`;
    options.unshift(option);
  }
  list.replaceChildren(...options);
  input.value = String(current ?? options[0]?.value ?? "");
}

async function openCameraSettings(device) {
  cameraSettingsDeviceId = device.id;
  const details = deviceCameraDetails?.[device.id] || {};
  const config = details.config || {};
  $("camera-settings-title").textContent =
    `Camera Settings · ${device.name || device.id}`;
  $("camera-settings-rotation").value = String(config.rotation ?? 0);
  $("camera-settings-enabled").checked = config.enabled === true;
  $("camera-settings-status").textContent = cameraState(details).label;
  populateCameraSources([], config.camera_source);
  $("camera-settings-dialog").hidden = false;
  setCameraSettingsError();
  await refreshCameraSources(false, device.id, config.camera_source);
}

async function refreshCameraSources(
  force = true,
  deviceId = cameraSettingsDeviceId,
  current = $("camera-settings-source").value,
) {
  const button = $("camera-settings-refresh");
  button.disabled = true;
  try {
    const discovery = await deviceService.discoverCameras(force);
    if (cameraSettingsDeviceId !== deviceId) return;
    populateCameraSources(discovery.cameras, current);
  } catch (error) {
    if (cameraSettingsDeviceId === deviceId)
      setCameraSettingsError(error.message);
  } finally {
    button.disabled = false;
  }
}

async function testCameraSettings() {
  const button = $("camera-settings-test");
  const save = $("camera-settings-save");
  button.disabled = true;
  save.disabled = true;
  setCameraSettingsError();
  try {
    const result = await deviceService.testCamera(
      cameraSourceValue($("camera-settings-source").value),
      Number($("camera-settings-rotation").value),
    );
    $("camera-settings-status").textContent = "Available";
    applyCameraPreviewGeometry(
      $("camera-test-preview"),
      result.width,
      result.height,
    );
    $("camera-test-image").src = result.preview || "";
    $("camera-test-image").hidden = !result.preview;
    $("camera-test-placeholder").hidden = !!result.preview;
  } catch (error) {
    $("camera-settings-status").textContent = "Unavailable";
    applyCameraPreviewGeometry($("camera-test-preview"));
    $("camera-test-image").hidden = true;
    $("camera-test-placeholder").hidden = false;
    $("camera-test-placeholder").textContent = "Camera test failed";
    setCameraSettingsError(error.message);
  } finally {
    button.disabled = false;
    save.disabled = false;
  }
}

async function saveDeviceCamera(event) {
  event.preventDefault();
  const deviceId = cameraSettingsDeviceId;
  if (!deviceId) return;
  const button = $("camera-settings-save");
  const test = $("camera-settings-test");
  button.disabled = true;
  test.disabled = true;
  setCameraSettingsError();
  try {
    const result = await deviceService.updateCamera(deviceId, {
      camera_source: cameraSourceValue($("camera-settings-source").value),
      rotation: Number($("camera-settings-rotation").value),
      enabled: $("camera-settings-enabled").checked,
    });
    const selected = deviceId === deviceCatalog?.selected_device;
    const selectedCameraChanged =
      result.selected_camera_changed === true || selected;
    if (selectedCameraChanged) {
      manual = false;
      calibrating = false;
      corners = [];
      clearPreview("Waiting for the assigned camera…");
    }
    closeCameraSettings();
    deviceRenderSignature = null;
    await refreshStatus();
    await loadDevices();
    if (
      result.calibration_revalidation_required ||
      result.selected_calibration_revalidation_required
    ) {
      notice(
        "Camera configuration changed. Existing screen calibration may no longer match. Recalibrate before control.",
      );
      if (selectedCameraChanged) $("calibrate").textContent = "Recalibrate";
    } else {
      notice(`Camera settings saved for ${deviceId}.`);
    }
  } catch (error) {
    setCameraSettingsError(error.message);
  } finally {
    button.disabled = false;
    test.disabled = false;
  }
}

function clearPreview(message = "Waiting for camera access…") {
  bitmap?.close();
  bitmap = null;
  frameId = null;
  frameAt = 0;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  $("video-placeholder").textContent = message;
  $("video-placeholder").classList.remove("hidden");
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
    if (deviceCameraDetails) delete deviceCameraDetails[device.id];
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
  renderControllerTag();
  const piMode = state?.manual_backend === "raspberry_pi";
  const selectedDevice = selectedPiDevice();
  $("devices").hidden = !piMode;
  $("jobs").hidden = !piMode;
  $("serial").textContent =
    (piMode && selectedDevice
      ? `Controlling: ${selectedDevice.name || selectedDevice.id}`
      : state?.device?.label) ||
    state?.device?.serial ||
    (piMode ? "Bluetooth phone unavailable" : "No authorized USB phone");
  $("pi-connection").hidden = !piMode;
  $("pi-api-state").textContent = deviceStatusStale
    ? "Status unavailable"
    : connectionState === "reconnecting"
      ? "Pi Reconnecting..."
      : state?.pi?.api === "online"
        ? "Online"
        : "Pi Offline";
  $("pi-phone-state").textContent = deviceStatusStale
    ? "Status unavailable"
    : state?.pi?.phone === "connected"
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

  const controllerReady = !!state && connectionState !== "reconnecting";
  const piReady =
    !piMode || (!deviceStatusStale && state?.pi?.api === "online");
  const hidReady =
    !!state?.device && (!piMode || state?.pi?.phone === "connected");
  const cameraReady = !!state?.camera?.ready;

  setOperationalHealth(
    "operate-controller-state",
    controllerReady ? "Ready" : "Problem",
    controllerReady,
  );
  setOperationalHealth(
    "operate-pi-state",
    piMode ? (piReady ? "Connected" : "Disconnected") : "Not in use",
    piReady,
  );
  setOperationalHealth(
    "operate-hid-state",
    hidReady ? "Ready" : "Problem",
    hidReady,
  );
  setOperationalHealth(
    "operate-camera-state",
    cameraReady ? "Ready" : "Problem",
    cameraReady,
  );
  renderOperationalRecovery();

  $("frame-state").textContent = fresh()
    ? "● Live camera"
    : "Camera feed unavailable";

  $("mode").textContent =
    state?.control_status === "automation"
      ? "Automation running"
      : state?.pending
        ? "Takeover requested"
        : active()
          ? dashboardMode === "operate"
            ? "Control active"
            : "Manual control active"
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
    ? dashboardMode === "operate"
      ? "Phone controls are unavailable while automation owns the phone."
      : "Manual control disabled. Camera monitoring remains available. Use Take Over to interrupt automation."
    : state?.pending
      ? "Takeover requested. Manual input stays disabled until automation yields."
      : dashboardMode === "operate"
        ? active()
          ? "Click the phone preview to tap or drag to swipe."
          : "Start manual control to use the phone preview, Back, and Home."
        : "Enable control when you are ready. Each click sends one tap to the phone.";

  $("enable").hidden = dashboardMode === "operate" && active();

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
      ? dashboardMode === "operate"
        ? "Click to tap or drag to swipe inside the outlined display."
        : "Click inside the outlined display to send one tap."
      : dashboardMode === "operate"
        ? "Start manual control when you are ready."
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

window.phoneRuntimeStateChanged = (runtime = null) => {
  if (runtime) {
    workflowRuntime = {
      ...runtime,
      device_id:
        runtime.device_id ||
        workflowRuntime?.device_id ||
        deviceCatalog?.selected_device ||
        null,
    };
  } else {
    workflowRuntime = null;
  }

  deviceRenderSignature = null;
  renderDevices();
  renderOperationalRecovery();
  refreshStatus();
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
    const requestedDevice = deviceCatalog?.selected_device || null;
    const framePath = requestedDevice
      ? `/api/devices/${encodeURIComponent(requestedDevice)}/camera/frame`
      : "/api/frame";
    const previewStarted = performance.now();
    const response = await fetch(framePath, {
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
    if (requestedDevice !== (deviceCatalog?.selected_device || null)) {
      next.close();
      succeeded = true;
      return;
    }

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

async function enableManualControl() {
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
  if (version !== controlVersion) return false;
  applyControlResult(result);
  if (result.enabled) notice("Manual control active.");
  return !!result.enabled;
}

async function restoreManualControlAfterVerificationFailure() {
  if (readyVerificationRestoreInFlight) return readyVerificationRestorePromise;
  if (!readyVerificationRestoreManual || manual || !manualAssistanceOpen)
    return;
  readyVerificationRestoreInFlight = true;
  readyVerificationRestorePromise = (async () => {
    try {
      await enableManualControl();
    } catch (error) {
      notice(
        `Manual control was not restored: ${friendlyOperationalError(error.message)}`,
        true,
      );
    } finally {
      readyVerificationRestoreManual = false;
      readyVerificationRestoreInFlight = false;
      readyVerificationRestorePromise = null;
      renderOperationalRecovery();
    }
  })();
  return readyVerificationRestorePromise;
}

async function finishSuccessfulReadyVerification(jobId) {
  if (readyVerificationFinishInFlight) return;
  readyVerificationFinishInFlight = true;
  try {
    if (readyVerificationRestorePromise) await readyVerificationRestorePromise;
    if (manual) await disable();
    const verified = jobCatalog.find((job) => job.job_id === jobId);
    if (verified?.recovery_status !== "READY") return;

    readyVerificationJobId = null;
    readyVerificationFailedJobId = null;
    readyVerificationMessage = "";
    readyVerificationRestoreManual = false;
    manualAssistanceOpen = false;
    recoveryPanelOpen = false;
    recoveryTargetJobId = null;
  } catch (error) {
    readyVerificationJobId = null;
    readyVerificationMessage =
      "Ready screen was detected, but manual control could not be released safely.";
    manualAssistanceOpen = true;
    recoveryPanelOpen = true;
    notice(error.message, true);
  } finally {
    readyVerificationFinishInFlight = false;
    renderDevices();
    renderOperationalRecovery();
  }
}

$("enable").addEventListener("click", () => report(enableManualControl));

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

async function sendPreviewSwipe(start, end, durationMs) {
  if (!active() || busy || physicalBusy) return;
  await report(async () => {
    try {
      await manualApi("/api/action", {
        action: "swipe",
        start,
        end,
        duration_ms: durationMs,
        frame_id: frameId,
      });
      notice("Sent swipe.");
    } catch (error) {
      // No automatic command retries, including after a network timeout.
      manual = false;
      api("/api/control", { enabled: false }).catch(() => {});
      throw error;
    }
  });
}

// const phoneKeys = {
//   Enter: "enter",
//   Backspace: "backspace",
//   Tab: "tab",
//   ArrowLeft: "left",
//   ArrowRight: "right",
//   ArrowUp: "up",
//   ArrowDown: "down",
//   Delete: "delete",
//   Home: "home",
//   End: "end",
//   PageUp: "pageup",
//   PageDown: "pagedown",
// };

// document.addEventListener("keydown", (event) => {
//   if (event.key === "Escape" && keyboardArmed) {
//     event.preventDefault();
//     releaseKeyboard();
//     canvas.blur();
//     return;
//   }
//   if (
//     !keyboardActive() ||
//     busy ||
//     physicalBusy ||
//     event.defaultPrevented ||
//     event.repeat ||
//     event.ctrlKey ||
//     event.metaKey ||
//     event.altKey ||
//     event.isComposing ||
//     event.keyCode === 229 ||
//     event.getModifierState("AltGraph") ||
//     event.getModifierState("Fn")
//   )
//     return;

//   // Leave local editing and keyboard activation of dashboard controls intact.
//   const target = event.target;
//   if (target?.isContentEditable || target?.closest?.("input, textarea, select"))
//     return;
//   if (
//     (event.key === "Enter" || event.key === " ") &&
//     target?.closest?.("button, a, [role='button']")
//   )
//     return;

//   let type, value;
//   if (event.key.length === 1 && event.key >= " " && event.key <= "~") {
//     type = "text";
//     value = event.key;
//   } else if (!event.shiftKey && Object.hasOwn(phoneKeys, event.key)) {
//     type = "key";
//     value = phoneKeys[event.key];
//   } else return;

//   event.preventDefault();
//   // Send immediately using the existing busy gate. No buffer or keyboard history.
//   report(async () => {
//     try {
//       await manualApi("/api/key", { type, value, frame_id: frameId });
//     } catch (error) {
//       // An uncertain input is never retried, even after a network timeout.
//       manual = false;
//       api("/api/control", { enabled: false }).catch(() => {});
//       throw error;
//     }
//   });
// });

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

canvas.addEventListener("pointerdown", (event) => {
  if (
    calibrating ||
    window.phoneRecorder?.capturesPreview() ||
    window.phoneRecorder?.blocksInput?.() ||
    !active() ||
    !fresh() ||
    busy ||
    physicalBusy ||
    event.button !== 0 ||
    !event.isPrimary
  )
    return;
  event.preventDefault();
  canvas.focus({ preventScroll: true });
  canvas.setPointerCapture(event.pointerId);
  previewGesture = {
    id: event.pointerId,
    start: window.phonePreviewGestures.point(event, canvas),
    at: performance.now(),
  };
});

canvas.addEventListener("pointerup", (event) => {
  if (!previewGesture || previewGesture.id !== event.pointerId) return;
  const saved = previewGesture;
  previewGesture = null;
  suppressPreviewClick = true;
  setTimeout(() => {
    suppressPreviewClick = false;
  }, 0);
  if (
    window.phoneRecorder?.blocksInput?.() ||
    !active() ||
    !fresh() ||
    busy ||
    physicalBusy
  )
    return;
  const end = window.phonePreviewGestures.point(event, canvas);
  if ([...saved.start, ...end].some((value) => value < 0 || value > 1)) {
    notice("Keep the gesture inside the phone preview.", true);
    return;
  }
  const gesture = window.phonePreviewGestures.classify(
    saved.start,
    end,
    canvas,
    performance.now() - saved.at,
  );
  if (gesture.type === "swipe")
    sendPreviewSwipe(gesture.start, gesture.end, gesture.duration_ms);
  else sendAction("tap", gesture.end);
});

for (const name of ["pointercancel", "lostpointercapture"])
  canvas.addEventListener(name, () => {
    previewGesture = null;
  });

canvas.addEventListener("click", (event) => {
  if (suppressPreviewClick) {
    suppressPreviewClick = false;
    return;
  }
  if (window.phoneRecorder?.blocksInput?.()) return;
  if (!calibrating && window.phoneRecorder?.capturesPreview()) return;
  if (!fresh() || busy || physicalBusy) return;

  canvas.focus({ preventScroll: true });
  keyboardArmed = keyboardEligible();
  renderKeyboard();

  const point = window.phonePreviewGestures.point(event, canvas);

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
  if (document.hidden) window.phoneRecorder?.pausePolling?.();
  else window.phoneRecorder?.resumePolling?.();
  // Background tabs are intentionally low-rate. Restore the normal cadence
  // immediately when Chrome makes the page visible again.
  schedulePreview(document.hidden ? PREVIEW_BACKGROUND_MS : 0);
  if (document.hidden) {
    scheduleDeviceRefresh(false);
    clearTimeout(runtimeStatusTimer);
    runtimeStatusTimer = null;
    clearTimeout(ocrStatusTimer);
    ocrStatusTimer = null;
  } else if (state?.manual_backend === "raspberry_pi") {
    loadDevices();
    scheduleRuntimeStatusRefresh(0);
    scheduleOCRStatus(0);
  } else {
    scheduleOCRStatus(0);
  }
});

window.addEventListener("pagehide", () => {
  stopDevicePreviews();
  window.phoneRecorder?.shutdown?.();
  clearInterval(legacyStatusTimer);
  legacyStatusTimer = null;
  clearTimeout(deviceRefreshTimer);
  deviceRefreshTimer = null;
  clearTimeout(runtimeStatusTimer);
  runtimeStatusTimer = null;
  clearTimeout(ocrStatusTimer);
  ocrStatusTimer = null;
  ocrPollingStopped = true;
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
$("dashboard-operate").addEventListener("click", () =>
  setDashboardMode("operate"),
);

$("dashboard-admin").addEventListener("click", () => {
  if (adminSession.authenticated) setDashboardMode("admin");
  else openAdminLogin();
});
$("admin-login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = $("admin-login-submit");
  const error = $("admin-login-error");
  submit.disabled = true;
  error.hidden = true;
  try {
    adminSession = await api(
      "/api/auth/login",
      {
        username: $("admin-login-username").value.trim(),
        password: $("admin-login-password").value,
      },
      "POST",
      { monitor: false },
    );
    closeAdminLogin();
    renderAdminSession();
    setDashboardMode("admin");
  } catch (loginError) {
    error.textContent = loginError.message;
    error.hidden = false;
    $("admin-login-password").select();
  } finally {
    submit.disabled = false;
  }
});
$("admin-login-cancel").addEventListener("click", closeAdminLogin);
$("admin-login-dialog").addEventListener("click", (event) => {
  if (event.target === $("admin-login-dialog")) closeAdminLogin();
});
$("admin-logout").addEventListener("click", async () => {
  setDashboardMode("operate");
  try {
    await api("/api/auth/logout", {}, "POST", { monitor: false });
  } catch (error) {
    notice(`Could not lock Admin: ${error.message}`, true);
    await loadAdminSession();
    return;
  }
  lockAdminState();
});
$("operate-resolve-issue").addEventListener("click", () => {
  const issue = currentOperationalIssue();
  if (issue?.job) recoveryTargetJobId = issue.job.job_id;
  recoveryPanelOpen = true;
  $("operate-recovery-result").textContent = "";
  renderOperationalRecovery();
  $("operate-recovery").scrollIntoView?.({
    block: "start",
    behavior: "smooth",
  });
});

$("operate-recover-phone").addEventListener("click", () => {
  const issue = currentOperationalIssue();
  if (issue?.job) startPhoneRecovery(issue.job.job_id);
});
$("operate-view-details").addEventListener("click", viewOperationalDetails);
$("operate-open-assistance").addEventListener("click", () => {
  const issue = currentOperationalIssue();
  openManualAssistance(issue?.job?.job_id || null);
});

$("operate-verify-finish").addEventListener("click", verifyPhoneReady);

$("operate-close-assistance").addEventListener("click", () =>
  closeManualAssistance(),
);

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
  scheduleRuntimeStatusRefresh();
  startLegacyStatusPolling();
  await loadOCRSettings();
}

$("device-add").addEventListener("click", () => {
  const devices = Array.isArray(deviceCatalog?.devices)
    ? deviceCatalog.devices
    : [];
  addPhoneInitialDeviceIds = new Set(devices.map((device) => device.id));
  $("device-enrollment-id").value = nextDeviceId(devices);
  addPhonePanelOpen = true;
  renderDevices();
  $("device-enrollment-id").focus();
  setDeviceError();
});

$("device-enrollment-cancel").addEventListener("click", () => {
  addPhonePanelOpen = false;
  addPhoneInitialDeviceIds = new Set();
  renderDevices();
});

$("device-enrollment-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setDeviceError();
  try {
    const target = $("device-enrollment-id").value.trim();
    await deviceService.startEnrollment(target);
    addPhoneEnrollmentTarget = target;
    addPhonePanelOpen = false;
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
$("account-settings-form").addEventListener("submit", saveAccountSettings);
$("account-settings-remove").addEventListener("click", removeAccountSettings);
$("account-settings-cancel").addEventListener("click", closeAccountSettings);
$("account-settings-close").addEventListener("click", closeAccountSettings);
$("account-settings-dialog").addEventListener("click", (event) => {
  if (event.target === $("account-settings-dialog")) closeAccountSettings();
});
$("camera-settings-form").addEventListener("submit", saveDeviceCamera);
$("camera-settings-test").addEventListener("click", testCameraSettings);
$("camera-settings-refresh").addEventListener("click", () =>
  refreshCameraSources(true),
);
$("camera-settings-cancel").addEventListener("click", closeCameraSettings);
$("camera-settings-close").addEventListener("click", closeCameraSettings);
$("camera-settings-dialog").addEventListener("click", (event) => {
  if (event.target === $("camera-settings-dialog")) closeCameraSettings();
});
$("ocr-worker-pool-size").addEventListener("input", () => {
  ocrSettingsDirty = true;
});
$("ocr-settings-form").addEventListener("submit", saveOCRSettings);
$("ocr-workers-restart").addEventListener("click", restartOCRWorkers);
$("ocr-apply-recommendation").addEventListener("click", applyOCRRecommendation);
window.addEventListener("pageshow", (event) => {
  if (event.persisted) {
    ocrPollingStopped = false;
    devicePollingStopped = false;
    window.phoneRecorder?.resumePolling?.();
    refreshStatus();
    loadDevices();
    scheduleRuntimeStatusRefresh();
    startLegacyStatusPolling();
    loadOCRSettings();
  }
});

setDashboardMode(dashboardMode);
renderAdminSession();
loadAdminSession();
render();
loadStatus();
videoLoop();
