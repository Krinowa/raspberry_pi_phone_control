"use strict";

const $ = (id) => document.getElementById(id);
const ui = (key, fallback, params) =>
  window.PhoneI18n?.t(key, params) ?? fallback;
const uiText = (element, key, fallback, params) => {
  if (window.PhoneI18n?.bindText)
    return window.PhoneI18n.bindText(element, key, params);
  element.textContent = fallback;
  return element;
};
var uiLiteral = (text) => window.PhoneI18n?.translateEnglish?.(text) ?? text;
var uiLiteralText = (element, text) => {
  if (window.PhoneI18n?.bindComputedText)
    return window.PhoneI18n.bindComputedText(element, () =>
      window.PhoneI18n.translateEnglish(text),
    );
  element.textContent = text;
  return element;
};
var uiError = (message) =>
  window.PhoneI18n?.translateError?.(message) ?? message;
var uiBackendError = (code, params, fallback) =>
  window.PhoneI18n?.translateBackendError?.(code, params, fallback) ?? fallback;
var uiErrorText = (element, message) => {
  if (window.PhoneI18n?.bindComputedText)
    return window.PhoneI18n.bindComputedText(
      element,
      () => window.PhoneI18n.translateError?.(message) ?? message,
    );
  element.textContent = message;
  return element;
};
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
  bitmap = null,
  bitmapCoordinateSpace = "camera_normalized";
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
let renderedDeviceManagementBlocked = false;
let devicePollingStopped = false;
const deviceDeletesInFlight = new Set();
const devicePreviewTimers = new Map();
const devicePreviewLastFrames = new Map();
const devicePreviewGenerations = new Map();
let cameraSettingsDeviceId = null;
let deviceRefreshLastSuccessAt = null;
let deviceRefreshError = "";
let deviceStatusStale = false;
let addPhonePanelOpen = false;
let addPhoneEnrollmentTarget = null;
let addPhoneInitialDeviceIds = new Set();
let deviceWithdrawDetails = {};
let jobCatalog = [];
let remoteControlHistory = [];
const jobCancelsInFlight = new Set();
const jobRetriesInFlight = new Set();
const withdrawReconnectsInFlight = new Set();
const balanceRefreshesInFlight = new Set();
const workReadinessRequestsInFlight = new Set();
let workflowSettingsDeviceId = null;
let workflowSettingsData = null;
let workflowSettingsMode = "payment";
let withdrawSettingsDeviceId = null;
let withdrawSettingsAssigned = false;
let withdrawSettingsReauthentication = false;
let withdrawCancelDeviceId = null;
let withdrawCancelPendingReason = null;
const withdrawCancelsInFlight = new Set();
let ocrSettingsDirty = false;
let ocrStatusTimer = null;
let ocrStatusInFlight = false;
let ocrPollingStopped = false;
let ocrRecommendedWorkerCount = null;
const DEVICE_REFRESH_MS = 1000;
const DEVICE_STATUS_STALE_MS = 5000;
const RUNTIME_STATUS_MS = 2000;
const PREVIEW_INTERVAL_MS = 83;
const PREVIEW_ERROR_RETRY_MS = 600;
const PREVIEW_BACKGROUND_MS = 1000;
let dashboardMode = "operate";
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
let returnedDeviceId = null;
let previewGesture = null;
let suppressPreviewClick = false;
let ocrOperationalHealthy = null;
let workflowRuntime = null;
let diagnosticsLogs = [];
let previewConsecutiveFailures = 0;
let previewCalibrationRequired = false;
let noticeRevision = 0;
const pendingFrontendDiagnostics = [];
let frontendDiagnosticsInFlight = false;

async function flushFrontendDiagnostics() {
  if (frontendDiagnosticsInFlight || !pendingFrontendDiagnostics.length) return;
  frontendDiagnosticsInFlight = true;
  try {
    while (pendingFrontendDiagnostics.length) {
      const response = await fetch("/api/diagnostics/frontend", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Control-Token": token,
          "X-Control-Client": clientId,
        },
        body: JSON.stringify(pendingFrontendDiagnostics[0]),
        cache: "no-store",
        signal: AbortSignal.timeout(3000),
      });
      if (!response.ok) break;
      pendingFrontendDiagnostics.shift();
    }
  } catch {
    // The event remains queued and is retried after a later successful request.
  } finally {
    frontendDiagnosticsInFlight = false;
  }
}

function reportFrontendDiagnostic(event, params = {}) {
  if (pendingFrontendDiagnostics.length >= 10)
    pendingFrontendDiagnostics.shift();
  pendingFrontendDiagnostics.push({ event, params });
  void flushFrontendDiagnostics();
}

window.phoneReportDiagnostic = reportFrontendDiagnostic;

function setDashboardMode(mode) {
  dashboardMode = mode === "admin" ? "admin" : "operate";
  if (document.body) document.body.dataset.dashboardMode = dashboardMode;
  $("dashboard-operate").ariaSelected = String(dashboardMode === "operate");
  $("dashboard-admin").ariaSelected = String(dashboardMode === "admin");
  $("enable").textContent =
    dashboardMode === "operate"
      ? "Start Manual Control"
      : "Enable Manual Control";
  window.PhoneI18n?.bindText?.(
    $("enable"),
    dashboardMode === "operate" ? "device.startManual" : "device.enableManual",
  );
  window.phoneRecorder?.dashboardModeChanged?.(dashboardMode);
  return dashboardMode;
}

async function switchDashboardMode(mode) {
  const nextMode = mode === "admin" ? "admin" : "operate";

  if (nextMode === dashboardMode) return;

  if (manualAssistanceOpen) {
    await closeManualAssistance();
  } else if (manual) {
    await disable();
  }

  setDashboardMode(nextMode);
}

function formatDiagnosticLog(entry) {
  return `${entry.timestamp || "—"} ${entry.level || "INFO"} [${entry.logger || "app"}] ${entry.message || ""}`;
}

async function loadDiagnostics() {
  const output = $("diagnostics-output");
  const status = $("diagnostics-state");
  const refresh = $("diagnostics-refresh");
  refresh.disabled = true;
  uiLiteralText(status, "Loading recent logs…");

  try {
    const filter = $("diagnostics-filter").value.trim();
    const query = filter ? `?query=${encodeURIComponent(filter)}` : "";
    const result = await api(
      `/api/diagnostics/logs${query}`,
      undefined,
      "GET",
      { monitor: false },
    );

    diagnosticsLogs = Array.isArray(result.logs) ? result.logs : [];
    output.textContent = diagnosticsLogs.length
      ? diagnosticsLogs.map(formatDiagnosticLog).join("\n")
      : uiLiteral("No matching application logs.");

    deviceUiText(
      status,
      "diagnostics.logCount",
      `${diagnosticsLogs.length} recent log ${diagnosticsLogs.length === 1 ? "entry" : "entries"}.`,
      {
        count: diagnosticsLogs.length,
        entry: deviceUi(
          diagnosticsLogs.length === 1
            ? "diagnostics.entry.one"
            : "diagnostics.entry.many",
          diagnosticsLogs.length === 1 ? "entry" : "entries",
        ),
      },
    );
    output.scrollTop = output.scrollHeight;
  } catch (error) {
    output.textContent = uiLiteral(
      "Logs are available only in developer mode.",
    );
    status.textContent = error.message;
  } finally {
    refresh.disabled = false;
  }
}

async function copyDiagnostics() {
  const value = $("diagnostics-output").textContent;
  if (!navigator.clipboard?.writeText) {
    notice(uiLiteral("Copy is not available in this browser."), true);
    return;
  }

  await navigator.clipboard.writeText(value);
  uiLiteralText($("diagnostics-state"), "Logs copied.");
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
  const revision = ++noticeRevision;
  const node = $("notice");
  if (window.PhoneI18n?.bindComputedText)
    window.PhoneI18n.bindComputedText(
      node,
      () => window.PhoneI18n.translateError?.(text) ?? text,
    );
  else node.textContent = text;
  node.classList.toggle("error", error);
  return revision;
}

function renderControllerTag() {
  const tag = $("controller-tag");
  if (!tag) return;

  if (connectionState !== "connected") {
    uiText(tag, "controller.reconnecting", "● Controller Reconnecting");
    tag.classList.add("error");
    return;
  }

  if (!state) {
    uiText(tag, "controller.checking", "● Checking controller…");
    tag.classList.remove("error");
    return;
  }

  uiText(tag, "controller.online", "● Controller Online");
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
  notice(
    ui(
      "controller.interrupted",
      "Connection interrupted. Retrying automatically...",
    ),
    true,
  );
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
      if (
        response.status === 403 &&
        (body.error_code === "authentication_required" ||
          body.error === "Admin login required.")
      )
        lockAdminState();
      const rawMessage = body.error || null;
      const error = new Error(
        (rawMessage &&
          uiBackendError(body.error_code, body.error_params, rawMessage)) ||
          ui("error.requestFailed", `Request failed (${response.status})`, {
            status: response.status,
          }),
      );
      error.status = response.status;
      error.rawMessage = rawMessage;
      error.errorCode = body.error_code;
      error.errorParams = body.error_params || {};
      error.transient = transientFailure(error, response.status);
      throw error;
    }
    void flushFrontendDiagnostics();
    return body;
  } catch (error) {
    error.transient ??= transientFailure(error);
    if (!error.status && path !== "/api/diagnostics/frontend")
      reportFrontendDiagnostic("frontend_api_network_failed", {
        operation: String(path).split("?", 1)[0].slice(0, 120),
        error_code:
          error.name === "TimeoutError" ? "frontend_timeout" : "network_failed",
      });
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
  endRemoteControl: (deviceId) =>
    api(`/api/devices/${encodeURIComponent(deviceId)}/remote-control/end`, {}),
  remoteControl: () =>
    api("/api/remote-control", undefined, "GET", { monitor: false }),
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
  workflows: (deviceId) =>
    api(
      `/api/devices/${encodeURIComponent(deviceId)}/workflows`,
      undefined,
      "GET",
      { monitor: false },
    ),
  updateWorkflows: (deviceId, workflowIds, balanceWorkflowId) =>
    api(
      `/api/devices/${encodeURIComponent(deviceId)}/workflows`,
      {
        workflow_ids: workflowIds,
        balance_workflow_id: balanceWorkflowId,
      },
      "PUT",
    ),
  setWorkReadiness: (deviceId, ready) =>
    api(
      `/api/devices/${encodeURIComponent(deviceId)}/work-readiness`,
      { ready },
      "PUT",
    ),
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
  withdraw: (deviceId) =>
    api(
      `/api/devices/${encodeURIComponent(deviceId)}/withdraw`,
      undefined,
      "GET",
      { monitor: false },
    ),
  loginWithdraw: (deviceId, credentials) =>
    api(
      `/api/devices/${encodeURIComponent(deviceId)}/withdraw/login-assign`,
      credentials,
    ),
  reconnectWithdraw: (deviceId) =>
    api(`/api/devices/${encodeURIComponent(deviceId)}/withdraw/reconcile`, {}),
  refreshBalance: (deviceId) =>
    api(
      `/api/devices/${encodeURIComponent(deviceId)}/withdraw/balance/refresh`,
      {},
    ),
  cancelWithdrawal: (deviceId, reason) =>
    api(`/api/devices/${encodeURIComponent(deviceId)}/withdraw/cancel`, {
      reason,
    }),
};

const jobService = {
  list: () => api("/api/jobs", undefined, "GET", { monitor: false }),
  cancel: (jobId) => api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {}),
  recover: (jobId) => api(`/api/jobs/${encodeURIComponent(jobId)}/recover`, {}),
  verifyReady: (jobId) =>
    api(`/api/jobs/${encodeURIComponent(jobId)}/verify-ready`, {}),
  markReady: (jobId) =>
    api(`/api/jobs/${encodeURIComponent(jobId)}/mark-ready`, {}),
  retry: (jobId, mode) =>
    api(`/api/jobs/${encodeURIComponent(jobId)}/retry`, { mode }),
};

// Kept inside the device-service slice used by the focused UI tests.
const deviceUi = (key, fallback, params) =>
  window.PhoneI18n?.t(key, params) ?? fallback;
const deviceUiText = (element, key, fallback, params) => {
  if (window.PhoneI18n?.bindText)
    return window.PhoneI18n.bindText(element, key, params);
  element.textContent = fallback;
  return element;
};
// Re-declared with var so focused device harnesses that evaluate this slice
// retain the same translation fallback as the complete browser bundle.
var uiLiteral =
  uiLiteral || ((text) => window.PhoneI18n?.translateEnglish?.(text) ?? text);
var uiLiteralText =
  uiLiteralText ||
  ((element, text) => {
    if (window.PhoneI18n?.bindComputedText)
      return window.PhoneI18n.bindComputedText(element, () =>
        window.PhoneI18n.translateEnglish(text),
      );
    element.textContent = text;
    return element;
  });
var uiError =
  uiError ||
  ((message) => window.PhoneI18n?.translateError?.(message) ?? message);
var uiBackendError =
  uiBackendError ||
  ((code, params, fallback) =>
    window.PhoneI18n?.translateBackendError?.(code, params, fallback) ??
    fallback);
var uiErrorText =
  uiErrorText ||
  ((element, message) => {
    if (window.PhoneI18n?.bindComputedText)
      return window.PhoneI18n.bindComputedText(
        element,
        () => window.PhoneI18n.translateError?.(message) ?? message,
      );
    element.textContent = message;
    return element;
  });
const deviceStatusLabel = (status) =>
  window.PhoneI18n?.translateStatus?.(status) ?? status ?? "—";

const TERMINAL_WITHDRAW_PHASES = new Set([
  "CONFIRMED",
  "RELEASED",
  "CANCELLED",
  "LOCK_REJECTED",
]);

function latestRelevantJobForDevice(deviceId, withdrawStatus = null) {
  const jobs = Array.isArray(jobCatalog) ? jobCatalog : [];
  const deviceJobs = [...jobs]
    .reverse()
    .filter((job) => job.device_id === deviceId);
  const phase = withdrawStatus?.phase;
  const currentWithdrawal =
    typeof phase === "string" && !TERMINAL_WITHDRAW_PHASES.has(phase);
  if (!currentWithdrawal) return deviceJobs[0] || null;

  const matchesWithdrawal = (job) =>
    job.action === "withdraw_v2" &&
    (!withdrawStatus?.tx_id || job.external_ref === withdrawStatus.tx_id);

  if (withdrawStatus?.job_id) {
    return (
      deviceJobs.find(
        (job) => job.job_id === withdrawStatus.job_id && matchesWithdrawal(job),
      ) || null
    );
  }
  if (withdrawStatus?.tx_id) {
    return deviceJobs.find(matchesWithdrawal) || null;
  }
  return null;
}

function availableJobActions(job) {
  if (!job) return [];

  const withdrawalRetryBlocked =
    job.action === "withdraw_v2" &&
    (job.withdrawal_terminal === true || job.withdrawal_retryable === false);

  if (["QUEUED", "WAITING_FOR_DEVICE", "RUNNING"].includes(job.status))
    return job.action === "withdraw_v2" ? [] : ["cancel"];

  if (!["FAILED", "INTERRUPTED"].includes(job.status)) return [];

  if (job.recovery_status === "NEEDS_ATTENTION")
    return [
      "manual_assistance",
      ...(job.action === "withdraw_v2" &&
      !withdrawalRetryBlocked &&
      Number.isInteger(job.failed_step_index)
        ? ["retry_failed_step"]
        : []),
      ...(job.action === "withdraw_v2" && !withdrawalRetryBlocked
        ? ["restart"]
        : []),
    ];

  if (job.recovery_status !== "READY") return [];

  if (withdrawalRetryBlocked) return [];

  return [
    ...(Number.isInteger(job.failed_step_index) ? ["retry_failed_step"] : []),
    "restart",
  ];
}

async function cancelExternalJob(job) {
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
  renderDevices();
  renderJobs();
  try {
    const updated = await jobService.cancel(job.job_id);
    jobCatalog = jobCatalog.map((candidate) =>
      candidate.job_id === updated.job_id ? updated : candidate,
    );

    if (updated.status === "CANCELLED") {
      if (recoveryTargetJobId === updated.job_id) {
        recoveryTargetJobId = null;
        recoveryPanelOpen = false;
        manualAssistanceOpen = false;
        readyVerificationJobId = null;
        readyVerificationFailedJobId = null;
        readyVerificationMessage = "";
        readyVerificationRestoreManual = false;
      }

      if (workflowRuntime?.device_id === updated.device_id)
        workflowRuntime = null;
    }
  } catch (error) {
    reportFrontendDiagnostic("frontend_action_failed", {
      operation: "cancel_job",
      device_id: job.device_id,
      job_id: job.job_id,
      ...(error?.errorCode ? { error_code: error.errorCode } : {}),
      ...(error?.status ? { status: error.status } : {}),
    });
    notice(
      deviceUi("error.cancelJob", `Could not cancel job: ${error.message}`, {
        message: uiError(error.message),
      }),
      true,
    );
  } finally {
    jobCancelsInFlight.delete(job.job_id);
    renderDevices();
    renderJobs();
    renderOperationalRecovery();
  }
}

async function retryExternalJob(job, mode) {
  if (jobRetriesInFlight.has(job.job_id)) return;
  jobRetriesInFlight.add(job.job_id);
  renderJobs();
  try {
    const retry = await jobService.retry(job.job_id, mode);
    jobCatalog = [...jobCatalog, retry];
    notice(
      mode === "failed_step"
        ? "Retry queued from the selected failed step."
        : "Workflow restart queued from step 0.",
    );
  } catch (error) {
    reportFrontendDiagnostic("frontend_action_failed", {
      operation: mode === "failed_step" ? "retry_failed_step" : "restart_job",
      device_id: job.device_id,
      job_id: job.job_id,
      ...(error?.errorCode ? { error_code: error.errorCode } : {}),
      ...(error?.status ? { status: error.status } : {}),
    });
    notice(
      deviceUi("error.retryJob", `Could not create retry: ${error.message}`, {
        message: uiError(error.message),
      }),
      true,
    );
  } finally {
    jobRetriesInFlight.delete(job.job_id);
    renderJobs();
    renderDevices();
  }
}

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
  const key = {
    "Checking…": "common.checking",
    Ready: "common.ready",
    Healthy: "common.healthy",
    Problem: "common.problem",
    Connected: "common.connected",
    Disconnected: "common.disconnected",
    "Camera unavailable": "camera.unavailable",
    "Calibration required": "camera.calibrationRequired",
    "Raspberry Pi unavailable": "error.piUnavailable",
    "OCR unavailable": "error.ocrUnavailable",
  }[label];
  if (key) deviceUiText(element, key, label);
  else uiLiteralText(element, label);
  element.className = healthy ? "health-ok" : "health-problem";
}

function setOCRSettingsError(message = "") {
  uiErrorText($("ocr-settings-error"), message);
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
          status.physical_cpu_fallback
            ? ` (${uiLiteral("logical fallback")})`
            : ""
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
      ? deviceUi(
          "ocr.health",
          `Healthy · ${status.pool_size} worker${status.pool_size === 1 ? "" : "s"} · Queue ${status.queued_requests ?? 0}`,
          {
            count: status.pool_size,
            worker: deviceUi(
              status.pool_size === 1 ? "ocr.worker.one" : "ocr.worker.many",
              status.pool_size === 1 ? "worker" : "workers",
            ),
            queue: status.queued_requests ?? 0,
          },
        )
      : "Problem",
    ocrHealthy,
  );
  renderOperationalRecovery();
  uiLiteralText(
    $("ocr-restart-required"),
    status.restart_required ? "Yes" : "No",
  );
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
  uiLiteralText(
    $("ocr-recommendation-reason"),
    status.recommendation_reason ||
      "Collecting recent OCR performance samples.",
  );
  const warning = $("ocr-settings-warning");
  const warningMatch = status.warning?.match(
    /^Configured OCR worker pool size (\d+) exceeds the detected safe ceiling (\d+); started (\d+) worker\(s\)\. The stored setting was not changed\.$/,
  );
  if (warningMatch)
    deviceUiText(warning, "ocr.poolClamped", status.warning, {
      configured: warningMatch[1],
      ceiling: warningMatch[2],
      started: warningMatch[3],
    });
  else uiLiteralText(warning, status.warning || "");
  warning.hidden = !status.warning;
  $("ocr-apply-recommendation").disabled =
    ocrRecommendedWorkerCount === null ||
    ocrRecommendedWorkerCount === status.configured_pool_size;

  const workers = Array.isArray(status.workers) ? status.workers : [];
  $("ocr-workers").replaceChildren(
    ...workers.map((worker) => {
      const item = document.createElement("span");
      deviceUiText(
        item,
        "ocr.workerStatus",
        `Worker ${worker.worker_id} — ${worker.state}`,
        () => ({
          number: worker.worker_id,
          state:
            window.PhoneI18n?.translateStatus?.(worker.state) ?? worker.state,
        }),
      );
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
    setOCRSettingsError(
      deviceUi("error.ocrStatus", `OCR status unavailable: ${error.message}`, {
        message: uiError(error.message),
      }),
    );
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
    setOCRSettingsError(
      deviceUi(
        "error.ocrSettings",
        `OCR settings unavailable: ${error.message}`,
        { message: uiError(error.message) },
      ),
    );
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
  uiErrorText(node, message);
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
    ((config.camera_source === null || config.camera_source === undefined) &&
      !config.camera_id)
  )
    return {
      label: deviceUi("common.unassigned", "Unassigned"),
      className: "",
    };
  if (!config.enabled)
    return {
      label: deviceUi("common.disabled", "Disabled"),
      className: "",
    };
  if (details?.status?.poll_failed)
    return {
      label: deviceUi("device.statusUnavailable", "Status unavailable"),
      className: "",
    };
  return details?.status?.ready
    ? {
        label: deviceUi("common.available", "Available"),
        className: "connected",
      }
    : { label: deviceUi("common.unavailable", "Unavailable"), className: "" };
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
  for (const deviceId of devicePreviewTimers.keys())
    stopDevicePreview(deviceId);
  devicePreviewTimers.clear();
}

function stopDevicePreview(deviceId) {
  const timer = devicePreviewTimers.get(deviceId);
  if (timer) clearTimeout(timer);
  devicePreviewTimers.delete(deviceId);
  devicePreviewGenerations.set(
    deviceId,
    (devicePreviewGenerations.get(deviceId) || 0) + 1,
  );
}

function startDevicePreview(deviceId, image, placeholder) {
  // Keep the last decoded frame visible while the next one is fetched/decoded.
  // This avoids flashing "Loading camera…" whenever the device card rerenders.
  if (
    typeof image.decode !== "function" ||
    typeof URL?.createObjectURL !== "function"
  )
    return;

  const generation = devicePreviewGenerations.get(deviceId) || 0;
  const cachedFrame = devicePreviewLastFrames.get(deviceId);

  if (cachedFrame) {
    image.src = cachedFrame;
    image.hidden = false;
    placeholder.hidden = true;
  }

  const schedule = (delay) => {
    if (
      generation !== (devicePreviewGenerations.get(deviceId) || 0) ||
      devicePollingStopped
    )
      return;
    const timer = setTimeout(() => {
      if (document.hidden) return schedule(PREVIEW_BACKGROUND_MS);
      void loadNextFrame();
    }, delay);
    devicePreviewTimers.set(deviceId, timer);
  };

  const loadNextFrame = async () => {
    if (
      generation !== (devicePreviewGenerations.get(deviceId) || 0) ||
      devicePollingStopped
    )
      return;

    let nextUrl = null;

    try {
      const response = await fetch(
        `/api/devices/${encodeURIComponent(deviceId)}/camera/preview?t=${Date.now()}`,
        {
          cache: "no-store",
          signal: AbortSignal.timeout(4000),
        },
      );

      if (!response.ok) {
        let message = "Camera unavailable";
        try {
          const body = await response.json();
          message = uiBackendError(
            body.error_code,
            body.error_params,
            body.error || message,
          );
        } catch {
          /* The generic message is safe for a non-JSON camera failure. */
        }
        throw new Error(message);
      }

      const blob = await response.blob();
      nextUrl = URL.createObjectURL(blob);

      // Decode off-DOM first. The currently visible frame remains untouched
      // until this replacement is fully ready to paint.
      const preloader = new Image();
      preloader.src = nextUrl;
      await preloader.decode();

      if (
        generation !== (devicePreviewGenerations.get(deviceId) || 0) ||
        devicePollingStopped
      ) {
        URL.revokeObjectURL(nextUrl);
        return;
      }

      const previousUrl = devicePreviewLastFrames.get(deviceId);
      devicePreviewLastFrames.set(deviceId, nextUrl);

      image.src = nextUrl;
      image.hidden = false;
      placeholder.hidden = true;
      applyCameraPreviewGeometry(
        image.parentElement,
        preloader.naturalWidth,
        preloader.naturalHeight,
      );

      // The new resource is already decoded. Revoke the older object URL only
      // after switching the visible image to avoid a blank frame in between.
      if (previousUrl && previousUrl !== nextUrl) {
        requestAnimationFrame(() => URL.revokeObjectURL(previousUrl));
      }

      nextUrl = null;
      schedule(200);
    } catch (error) {
      if (nextUrl) URL.revokeObjectURL(nextUrl);

      // If we already have a good frame, keep it visible and retry silently.
      if (error.message === "Calibration required for this phone.") {
        const previousUrl = devicePreviewLastFrames.get(deviceId);
        if (previousUrl) URL.revokeObjectURL(previousUrl);
        devicePreviewLastFrames.delete(deviceId);
        image.removeAttribute("src");
        image.hidden = true;
        placeholder.hidden = false;
        placeholder.textContent = uiError(error.message);
      } else if (!devicePreviewLastFrames.has(deviceId)) {
        image.hidden = true;
        placeholder.hidden = false;
        placeholder.textContent = uiError(error.message);
      }

      schedule(1000);
    }
  };

  void loadNextFrame();
}

function createDeviceJobActions(job, deviceId, managementBlocked) {
  const actions = document.createElement("div");
  actions.className = "device-job-actions";

  for (const action of availableJobActions(job)) {
    const button = document.createElement("button");
    button.type = "button";

    if (action === "manual_assistance") {
      button.className = "primary danger-action";
      deviceUiText(button, "device.manualAssistance", "Manual Assistance");
      button.disabled = managementBlocked;
      button.addEventListener("click", () =>
        resolveDeviceIssue(deviceId, job.job_id),
      );
    } else if (action === "retry_failed_step") {
      deviceUiText(button, "workflow.retryFailed", "Retry from Failed Step");
      button.disabled = jobRetriesInFlight.has(job.job_id);
      button.addEventListener("click", () =>
        retryExternalJob(job, "failed_step"),
      );
    } else if (action === "restart") {
      deviceUiText(button, "workflow.restart", "Restart Workflow");
      button.disabled = jobRetriesInFlight.has(job.job_id);
      button.addEventListener("click", () => retryExternalJob(job, "restart"));
    } else if (action === "cancel") {
      button.className = "stop";
      const cancelKey = job.cancel_requested
        ? "workflow.cancellationRequested"
        : jobCancelsInFlight.has(job.job_id)
          ? "workflow.cancelling"
          : "common.cancel";
      deviceUiText(
        button,
        cancelKey,
        job.cancel_requested
          ? "Cancellation requested"
          : jobCancelsInFlight.has(job.job_id)
            ? "Cancelling…"
            : "Cancel",
      );
      button.disabled =
        !!job.cancel_requested || jobCancelsInFlight.has(job.job_id);
      button.addEventListener("click", () => cancelExternalJob(job));
    }
    actions.append(button);
  }
  return actions;
}

function formatPayerBalance(value) {
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(String(value ?? ""));
  if (!match) return null;
  const integer = match[1].replace(/^0+(?=\d)/, "");
  const cents = (match[2] || "").padEnd(2, "0");
  return `${integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${cents}`;
}

function payerBalanceView(details) {
  const status = details?.status || {};
  const observed = new Date(status.balance_observed_at || "");
  const checkedAt = Number.isNaN(observed.getTime())
    ? null
    : observed.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

  if (status.balance_state === "VALID" && status.balance_valid === true) {
    const amount = formatPayerBalance(status.last_balance);
    return {
      value: amount || deviceUi("common.unavailable", "Unavailable"),
      checkedAt,
      stateLabel: null,
    };
  }

  if (status.balance_state === "REFRESHING") {
    const amount = formatPayerBalance(status.last_balance);
    return {
      value: amount || deviceUi("balance.checking", "Checking…"),
      checkedAt: amount ? checkedAt : null,
      stateLabel: amount ? deviceUi("balance.refreshing", "Refreshing…") : null,
    };
  }

  if (status.balance_state === "STALE" && status.last_balance != null) {
    const amount = formatPayerBalance(status.last_balance);
    return {
      value: amount || deviceUi("common.unavailable", "Unavailable"),
      checkedAt,
      stateLabel: amount ? deviceUi("balance.stale", "Stale") : null,
    };
  }
  if (status.balance_state === "ERROR") {
    return {
      value: deviceUi("common.unavailable", "Unavailable"),
      checkedAt: null,
      stateLabel:
        (status.balance_error &&
          uiBackendError(
            status.balance_error_code,
            status.balance_error_params,
            status.balance_error,
          )) ||
        deviceUi("balance.failed", "Balance check failed"),
    };
  }
  return {
    value: deviceUi("common.unavailable", "Unavailable"),
    checkedAt: null,
    stateLabel: null,
  };
}

function canManuallyCancelWithdrawal(details) {
  return details?.status?.cancel_allowed === true;
}

function withdrawRuntimeView(details) {
  const status = details?.status || {};
  const authenticationAvailable = status.authentication_available === true;
  const connected =
    authenticationAvailable &&
    status.session_state === "READY" &&
    status.physical_socket_connected === true;

  if (status.state === "AUTH_FAILED")
    return {
      session: deviceUi("withdraw.authRequired", "Authentication required"),
      realtime: deviceUi("common.disconnected", "Disconnected"),
      ready: false,
      action: "login",
    };
  if (!authenticationAvailable)
    return {
      session: deviceUi("withdraw.authRequired", "Authentication required"),
      realtime: deviceUi("common.disconnected", "Disconnected"),
      ready: false,
      action: "login",
    };
  if (status.state === "CONFIGURATION_ERROR")
    return {
      session: deviceUi("withdraw.configRequired", "Configuration required"),
      realtime: deviceUi("common.disconnected", "Disconnected"),
      ready: false,
      action: "reconnect",
    };
  // NEEDS_ATTENTION describes local withdrawal/payment recovery, not the
  // external authentication or realtime transport. During a failed workflow
  // the worker intentionally keeps this READY session alive to renew its lock.
  if (connected)
    return {
      session: deviceUi("common.connected", "Connected"),
      realtime: deviceUi("common.connected", "Connected"),
      ready: true,
      action: null,
    };
  if (status.state === "NEEDS_ATTENTION")
    return {
      session: deviceUi("common.authenticated", "Authenticated"),
      realtime: status.physical_socket_connected
        ? deviceUi("common.connected", "Connected")
        : deviceUi("common.disconnected", "Disconnected"),
      ready: false,
      action: "reconnect",
    };
  if (["CONNECTING", "WAITING_FOR_CONNECTION"].includes(status.state))
    return {
      session:
        status.state === "CONNECTING"
          ? deviceUi("common.authenticating", "Authenticating")
          : deviceUi("common.reconnecting", "Reconnecting"),
      realtime: status.physical_socket_connected
        ? deviceUi("common.connected", "Connected")
        : deviceUi("common.disconnected", "Disconnected"),
      ready: false,
      action: "reconnect",
    };
  return {
    session: deviceUi("common.authenticated", "Authenticated"),
    realtime: status.physical_socket_connected
      ? deviceUi("common.connected", "Connected")
      : deviceUi("common.disconnected", "Disconnected"),
    ready: false,
    action: "reconnect",
  };
}

async function reconnectWithdraw(deviceId) {
  if (withdrawReconnectsInFlight.has(deviceId)) return;
  withdrawReconnectsInFlight.add(deviceId);
  deviceRenderSignature = null;
  renderDevices();

  try {
    await deviceService.reconnectWithdraw(deviceId);
    notice("Withdraw session reconnect requested.");
    await loadDevices();
  } catch (error) {
    notice(error.message, true);
  } finally {
    withdrawReconnectsInFlight.delete(deviceId);
    deviceRenderSignature = null;
    renderDevices();
  }
}

async function refreshWithdrawBalance(deviceId) {
  if (balanceRefreshesInFlight.has(deviceId)) return;
  balanceRefreshesInFlight.add(deviceId);
  deviceRenderSignature = null;
  renderDevices();

  try {
    await deviceService.refreshBalance(deviceId);
    notice("Balance refresh requested.");
    await loadDevices();
  } catch (error) {
    notice(error.message, true);
  } finally {
    balanceRefreshesInFlight.delete(deviceId);
    deviceRenderSignature = null;
    renderDevices();
  }
}

async function setDeviceWorkReadiness(deviceId, ready) {
  if (workReadinessRequestsInFlight.has(deviceId)) return;
  workReadinessRequestsInFlight.add(deviceId);
  deviceRenderSignature = null;
  renderDevices();

  try {
    const result = await deviceService.setWorkReadiness(deviceId, ready);
    const device = deviceCatalog?.devices?.find((item) => item.id === deviceId);
    if (device) device.ready_for_work = result.ready_for_work;
    notice(ready ? "Phone marked Ready for Work." : "Phone set Not Ready.");
    await loadDevices();
  } catch (error) {
    notice(error.message, true);
  } finally {
    workReadinessRequestsInFlight.delete(deviceId);
    deviceRenderSignature = null;
    renderDevices();
  }
}

function friendlyWorkflowStepType(type) {
  const labels = {
    tap: "Tap",
    swipe: "Swipe",
    input: "Input",
    key: "Key",
    wait: "Wait",
    wait_until_stable: "Wait Until Stable",
    wait_for_text: "Wait for Text",
    verify_text: "Verify Text",
    manual_checkpoint: "Manual Checkpoint",
    ocr_value: "Read Value",
    capture_screen: "Capture Screenshot",
    if_text: "If Text",
  };

  return labels[type] ? uiLiteral(labels[type]) : null;
}

function deviceJobRenderSnapshot(job) {
  if (!job) return null;
  return {
    job_id: job.job_id,
    device_id: job.device_id,
    external_ref: job.external_ref,
    action: job.action,
    status: job.status,
    error: job.error,
    result: job.result,
    recovery_status: job.recovery_status,
    recovery_error: job.recovery_error,
    error_code: job.error_code,
    error_params: job.error_params,
    recovery_error_code: job.recovery_error_code,
    recovery_error_params: job.recovery_error_params,
    failed_step_index: job.failed_step_index,
    failed_step_type: job.failed_step_type,
    retry_of_job_id: job.retry_of_job_id,
    withdrawal_terminal: job.withdrawal_terminal,
    withdrawal_retryable: job.withdrawal_retryable,
  };
}

function deviceCardRenderSignature(
  device,
  details,
  withdraw,
  latestJob,
  managementBlocked,
) {
  const status = withdraw?.status || {};
  return JSON.stringify({
    locale: window.PhoneI18n?.getLocale?.() || "en",
    device: {
      id: device.id,
      name: device.name,
      connected: device.connected,
      default: device.default,
      bluetooth_address: device.bluetooth_address,
      ready_for_work: device.ready_for_work,
      workflow_assignment: device.workflow_assignment,
      remote_control: device.remote_control
        ? {
            session_id: device.remote_control.session_id,
            user_id: device.remote_control.user_id,
            reason: device.remote_control.reason,
            started_at: device.remote_control.started_at,
            waiting_for_job: device.remote_control.waiting_for_job,
          }
        : null,
    },
    default_device: deviceCatalog?.default_device,
    selected: device.id === deviceCatalog?.selected_device,
    stale: deviceStatusStale,
    returned: device.id === returnedDeviceId,
    management_blocked: managementBlocked,
    camera: {
      config: details?.config,
      ready: details?.status?.ready,
      poll_failed: details?.status?.poll_failed,
      width: details?.status?.width,
      height: details?.status?.height,
    },
    withdraw: {
      assigned: withdraw?.assigned,
      balance_workflow_id: withdraw?.balance_workflow_id,
      withdraw_api: withdraw?.withdraw_api,
      state: status.state,
      phase: status.phase,
      error: status.error,
      error_code: status.error_code,
      error_params: status.error_params,
      session_state: status.session_state,
      physical_socket_connected: status.physical_socket_connected,
      payment_ready: status.payment_ready,
      payment_readiness_error: status.payment_readiness_error,
      payment_readiness_error_code: status.payment_readiness_error_code,
      payment_readiness_error_params: status.payment_readiness_error_params,
      balance_state: status.balance_state,
      balance_valid: status.balance_valid,
      last_balance: status.last_balance,
      balance_observed_at: status.balance_observed_at,
      balance_error: status.balance_error,
      balance_error_code: status.balance_error_code,
      balance_error_params: status.balance_error_params,
      balance_workflow_ready: status.balance_workflow_ready,
      balance_workflow_error: status.balance_workflow_error,
      balance_workflow_error_code: status.balance_workflow_error_code,
      balance_workflow_error_params: status.balance_workflow_error_params,
      cancel_allowed: status.cancel_allowed,
      withdraw_id: status.withdraw_id,
      tx_id: status.tx_id,
      job_id: status.job_id,
    },
    job: deviceJobRenderSnapshot(latestJob),
    workflow_runtime:
      workflowRuntime?.device_id === device.id
        ? {
            status: workflowRuntime.status,
            message: workflowRuntime.message,
          }
        : null,
    requests: {
      deleting: deviceDeletesInFlight.has(device.id),
      cancelling_job: latestJob
        ? jobCancelsInFlight.has(latestJob.job_id)
        : false,
      retrying_job: latestJob
        ? jobRetriesInFlight.has(latestJob.job_id)
        : false,
      reconnecting: withdrawReconnectsInFlight.has(device.id),
      refreshing_balance: balanceRefreshesInFlight.has(device.id),
      changing_readiness: workReadinessRequestsInFlight.has(device.id),
      cancelling_withdrawal: withdrawCancelsInFlight.has(device.id),
    },
  });
}

function devicePreviewRenderSignature(device, details) {
  const config = details?.config || {};
  return JSON.stringify({
    device_id: device.id,
    name: device.name,
    enabled: config.enabled,
    camera_source: config.camera_source,
    camera_id: config.camera_id,
    rotation: config.rotation,
    width: details?.status?.width,
    height: details?.status?.height,
  });
}

function renderDevices({ presentationOnly = false } = {}) {
  const devices = Array.isArray(deviceCatalog?.devices)
    ? deviceCatalog.devices
    : [];
  // Eligibility is a behavior-only snapshot. Rendering translated labels must
  // not cause action gates to be read again partway through the same render.
  const managementBlocked = presentationOnly
    ? renderedDeviceManagementBlocked
    : deviceManagementBlocked();
  if (!presentationOnly) renderedDeviceManagementBlocked = managementBlocked;

  const deviceList = $("device-list");
  const existingCards = new Map(
    Array.from(deviceList.children || []).map((card) => [
      card.dataset?.deviceId,
      card,
    ]),
  );

  const currentDeviceIds = new Set(devices.map((device) => device.id));
  for (const deviceId of existingCards.keys()) {
    if (!currentDeviceIds.has(deviceId)) stopDevicePreview(deviceId);
  }
  for (const [deviceId, frameUrl] of devicePreviewLastFrames) {
    if (currentDeviceIds.has(deviceId)) continue;
    stopDevicePreview(deviceId);
    URL.revokeObjectURL(frameUrl);
    devicePreviewLastFrames.delete(deviceId);
  }

  const enrollment = deviceCatalog?.enrollment || { active: false };
  const selected = deviceCatalog?.selected_device;

  $("device-state").textContent = deviceStatusStale
    ? deviceUi(
        "device.statusRetrying",
        "Device status unavailable. Retrying automatically.",
      )
    : devices.length
      ? deviceUi(
          "device.count",
          `${devices.length} registered phone${devices.length === 1 ? "" : "s"}. All cameras remain visible; one phone receives control.`,
          { count: devices.length, suffix: devices.length === 1 ? "" : "s" },
        )
      : deviceUi(
          "device.noneOnPi",
          "No phones are registered with this Pi yet.",
        );

  const cards = devices.map((device) => {
    const details = deviceCameraDetails?.[device.id] || {};
    const withdraw = deviceWithdrawDetails?.[device.id] || {};
    const withdrawNeedsAttention =
      withdraw.status?.state === "NEEDS_ATTENTION" ||
      withdraw.status?.phase === "NEEDS_ATTENTION";
    const camera = details.config || {};
    const status = cameraState(details);
    const latestJob = latestRelevantJobForDevice(device.id, withdraw.status);
    const recoveryJob =
      latestJob &&
      ["FAILED", "INTERRUPTED"].includes(latestJob.status) &&
      latestJob.recovery_status !== "READY"
        ? latestJob
        : null;
    const activeJob =
      latestJob &&
      ["QUEUED", "WAITING_FOR_DEVICE", "RUNNING"].includes(latestJob.status)
        ? latestJob
        : null;
    const manualWorkflowNeedsAttention =
      workflowRuntime?.status === "failed" &&
      workflowRuntime?.device_id === device.id;

    const cardSignature = deviceCardRenderSignature(
      device,
      details,
      withdraw,
      latestJob,
      managementBlocked,
    );
    const existingCard = existingCards.get(device.id);
    if (existingCard?.dataset?.renderSignature === cardSignature)
      return existingCard;

    const operationalState = recoveryJob
      ? recoveryJob.recovery_status === "RECOVERING"
        ? "recovering"
        : "needs-attention"
      : withdrawNeedsAttention
        ? "needs-attention"
        : manualWorkflowNeedsAttention
          ? "needs-attention"
          : activeJob
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
    card.dataset.deviceId = device.id;
    card.dataset.renderSignature = cardSignature;
    if (device.id === returnedDeviceId)
      card.className += " device-card--returned";

    const heading = document.createElement("div");
    heading.className = "device-item-heading";
    const name = document.createElement("h3");
    name.textContent = device.name || device.id;
    const logicalId = document.createElement("span");
    logicalId.className = "device-logical-id admin-only";
    logicalId.textContent = device.id;
    const badges = document.createElement("div");
    badges.className = "device-badges";

    if (device.default || device.id === deviceCatalog.default_device) {
      const badge = document.createElement("span");
      badge.className = "badge";
      deviceUiText(badge, "device.default", "Default");
      badges.append(badge);
    }

    if (device.id === selected) {
      const badge = document.createElement("span");
      badge.className = "badge control-target";
      deviceUiText(badge, "device.controlTarget", "Control target");
      badges.append(badge);
    }
    const operationalBadge = document.createElement("span");
    operationalBadge.className = `badge device-state-badge device-state-badge--${operationalState}`;
    const operationalLabel = {
      "needs-attention": ["health.needsAttentionWarning", "⚠ Needs Attention"],
      recovering: ["status.RECOVERING", "Recovering"],
      running: ["device.running", "Running"],
      offline: ["device.offline", "Offline"],
      ready: ["common.ready", "Ready"],
    }[operationalState];
    deviceUiText(operationalBadge, ...operationalLabel);
    badges.append(operationalBadge);
    heading.append(name, logicalId, badges);

    const states = document.createElement("div");
    states.className = "device-states";
    const bluetooth = document.createElement("span");
    bluetooth.className =
      "device-status" +
      (!deviceStatusStale && device.connected ? " connected" : "");

    if (deviceStatusStale) {
      deviceUiText(
        bluetooth,
        "device.bluetoothUnavailable",
        "Bluetooth: ? Status unavailable",
      );
    } else {
      const bluetoothState = device.connected
        ? deviceUi("common.connected", "Connected")
        : deviceUi("common.disconnected", "Disconnected");
      deviceUiText(
        bluetooth,
        "device.bluetoothStatus",
        `Bluetooth: ● ${bluetoothState}`,
        () => ({
          status: device.connected
            ? deviceUi("common.connected", "Connected")
            : deviceUi("common.disconnected", "Disconnected"),
        }),
      );
    }

    const cameraStatus = document.createElement("span");
    cameraStatus.className = "device-status " + status.className;
    if (deviceStatusStale) {
      deviceUiText(
        cameraStatus,
        "device.cameraUnavailableStatus",
        "Camera: ? Status unavailable",
      );
    } else {
      deviceUiText(
        cameraStatus,
        "device.cameraStatus",
        `Camera: ● ${status.label}`,
        () => ({ status: cameraState(details).label }),
      );
    }
    states.append(bluetooth, cameraStatus);

    const mac = document.createElement("p");
    mac.className = "device-mac admin-only";
    mac.textContent =
      device.bluetooth_address || uiLiteral("Bluetooth MAC not paired");

    const previewSignature = devicePreviewRenderSignature(device, details);
    const existingPreview = Array.from(existingCard?.children || []).find(
      (child) => child.className === "device-preview",
    );
    let preview;
    if (existingPreview?.dataset?.renderSignature === previewSignature) {
      preview = existingPreview;
      preview.disabled = device.id === selected || managementBlocked;
      preview.setAttribute?.(
        "aria-label",
        deviceUi(
          "device.selectForControl",
          `Select ${device.name || device.id} for control`,
          {
            name: device.name || device.id,
          },
        ),
      );
      applyCameraPreviewGeometry(
        preview,
        details.status?.width,
        details.status?.height,
      );
    } else {
      stopDevicePreview(device.id);
      preview = document.createElement("button");
      preview.type = "button";
      preview.className = "device-preview";
      preview.dataset.renderSignature = previewSignature;
      applyCameraPreviewGeometry(
        preview,
        details.status?.width,
        details.status?.height,
      );

      preview.disabled = device.id === selected || managementBlocked;
      preview.setAttribute?.(
        "aria-label",
        deviceUi(
          "device.selectForControl",
          `Select ${device.name || device.id} for control`,
          {
            name: device.name || device.id,
          },
        ),
      );

      const previewImage = document.createElement("img");
      previewImage.alt = deviceUi(
        "device.livePreview",
        `${device.name || device.id} live camera preview`,
        { name: device.name || device.id },
      );
      previewImage.hidden = true;

      const previewPlaceholder = document.createElement("span");
      previewPlaceholder.textContent = details?.status?.ready
        ? deviceUi("camera.loading", "Loading camera…")
        : `${deviceUi("health.camera", "Camera")} ${status.label.toLowerCase()}`;
      preview.append(previewImage, previewPlaceholder);
      preview.addEventListener("click", () => selectDashboardDevice(device.id));

      if (
        camera.enabled &&
        ((camera.camera_source !== null &&
          camera.camera_source !== undefined) ||
          camera.camera_id)
      )
        startDevicePreview(device.id, previewImage, previewPlaceholder);
    }

    const summary = document.createElement("p");
    summary.className = "camera-summary admin-only";
    summary.textContent =
      (camera.camera_source === null || camera.camera_source === undefined) &&
      !camera.camera_id
        ? deviceUi("camera.noneAssigned", "No camera assigned")
        : deviceUi(
            "camera.rotation",
            `${camera.display_name || `Camera ${camera.camera_id || camera.camera_source}`} · ${camera.rotation ?? 0}°`,
            {
              name:
                camera.display_name ||
                deviceUi(
                  "camera.named",
                  `Camera ${camera.camera_id || camera.camera_source}`,
                  {
                    id: camera.camera_id || camera.camera_source,
                  },
                ),
              rotation: camera.rotation ?? 0,
            },
          );

    const workflowAssignment = device.workflow_assignment || {
      unrestricted: true,
      assigned_workflows: [],
      missing_workflow_ids: [],
    };

    const readyForWork = device.ready_for_work !== false;

    const workReadiness = document.createElement("div");
    workReadiness.className = "device-work-readiness";

    const workReadinessLabel = document.createElement("span");
    workReadinessLabel.className = "device-work-readiness-label";
    deviceUiText(workReadinessLabel, "device.workStatus", "Work status");

    const workReadinessState = document.createElement("span");
    workReadinessState.className = `device-work-readiness-state ${readyForWork ? "ready" : "not-ready"}`;
    deviceUiText(
      workReadinessState,
      readyForWork ? "device.readyMark" : "device.notReadyMark",
      readyForWork ? "● Ready" : "● Not Ready",
    );

    const workReadinessAction = document.createElement("button");
    workReadinessAction.type = "button";
    workReadinessAction.className = "device-work-readiness-action";
    const readinessKey = workReadinessRequestsInFlight.has(device.id)
      ? "device.updating"
      : readyForWork
        ? "device.setNotReady"
        : "device.markReady";
    deviceUiText(
      workReadinessAction,
      readinessKey,
      workReadinessRequestsInFlight.has(device.id)
        ? "Updating…"
        : readyForWork
          ? "Set Not Ready"
          : "Mark Ready",
    );

    workReadinessAction.disabled = workReadinessRequestsInFlight.has(device.id);

    workReadinessAction.addEventListener("click", () =>
      setDeviceWorkReadiness(device.id, !readyForWork),
    );

    workReadiness.append(
      workReadinessLabel,
      workReadinessState,
      workReadinessAction,
    );

    const workflowSummary = document.createElement("div");
    workflowSummary.className = "account-summary admin-only";
    const workflowHeading = document.createElement("strong");
    deviceUiText(workflowHeading, "workflow.payment", "Payment Workflow");
    const workflowNames = document.createElement("span");
    const assignedWorkflow = workflowAssignment.assigned_workflows?.[0];

    workflowNames.textContent = workflowAssignment.unrestricted
      ? deviceUi("common.notConfigured", "Not configured")
      : assignedWorkflow
        ? assignedWorkflow.missing
          ? `${assignedWorkflow.id} · ${deviceUi("workflow.missing", "Missing")}`
          : assignedWorkflow.name || assignedWorkflow.id
        : deviceUi("workflow.noAssigned", "No workflow assigned");

    const workflowCount = document.createElement("span");

    workflowCount.textContent = workflowAssignment.unrestricted
      ? deviceUi("workflow.chooseOne", "Choose one workflow")
      : assignedWorkflow
        ? assignedWorkflow.id
        : deviceUi("workflow.executionDisabled", "Execution disabled");

    const paymentWorkflowAction = document.createElement("button");
    paymentWorkflowAction.type = "button";
    deviceUiText(
      paymentWorkflowAction,
      assignedWorkflow ? "common.change" : "workflow.assign",
      assignedWorkflow ? "Change" : "Assign",
    );
    paymentWorkflowAction.disabled = managementBlocked;
    paymentWorkflowAction.addEventListener("click", () =>
      openWorkflowSettings(device, "payment"),
    );

    const balanceWorkflowHeading = document.createElement("strong");
    deviceUiText(
      balanceWorkflowHeading,
      "workflow.balance",
      "Balance Check Workflow",
    );

    const balanceWorkflow = document.createElement("span");
    balanceWorkflow.textContent = workflowAssignment.balance_workflow
      ? workflowAssignment.balance_workflow.missing
        ? `${workflowAssignment.balance_workflow.id} · ${deviceUi("workflow.missing", "Missing")}`
        : workflowAssignment.balance_workflow.name ||
          workflowAssignment.balance_workflow.id
      : deviceUi("workflow.notAssigned", "Not assigned");

    const balanceWorkflowAction = document.createElement("button");
    balanceWorkflowAction.type = "button";
    deviceUiText(
      balanceWorkflowAction,
      workflowAssignment.balance_workflow ? "common.change" : "workflow.assign",
      workflowAssignment.balance_workflow ? "Change" : "Assign",
    );

    balanceWorkflowAction.disabled = managementBlocked;
    balanceWorkflowAction.addEventListener("click", () =>
      openWorkflowSettings(device, "balance"),
    );

    workflowSummary.append(
      workflowHeading,
      workflowNames,
      workflowCount,
      paymentWorkflowAction,
      balanceWorkflowHeading,
      balanceWorkflow,
      balanceWorkflowAction,
    );

    const withdrawApi = withdraw.withdraw_api || {};
    const withdrawAssigned =
      withdraw.assigned === true ||
      (Number.isInteger(withdrawApi.operator_id) && !!withdrawApi.username);
    const balance = payerBalanceView(withdraw);
    const balanceSummary = document.createElement("div");
    balanceSummary.className = "account-summary device-balance-summary";
    const balanceHeading = document.createElement("strong");
    deviceUiText(balanceHeading, "balance.title", "Balance");
    const balanceValue = document.createElement("span");
    balanceValue.className = "device-balance-value";
    balanceValue.textContent = balance.value;
    balanceSummary.append(balanceHeading, balanceValue);

    if (balance.stateLabel) {
      const balanceState = document.createElement("span");
      balanceState.className = "device-balance-state";
      balanceState.textContent = balance.stateLabel;
      balanceSummary.append(balanceState);
    }

    if (balance.checkedAt) {
      const checked = document.createElement("span");
      checked.className = "device-balance-time";
      deviceUiText(
        checked,
        "balance.lastChecked",
        `Last checked: ${balance.checkedAt}`,
        { time: balance.checkedAt },
      );
      balanceSummary.append(checked);
    }
    if (withdrawAssigned) {
      const refreshBalance = document.createElement("button");
      refreshBalance.type = "button";
      deviceUiText(
        refreshBalance,
        balanceRefreshesInFlight.has(device.id)
          ? "balance.requesting"
          : "balance.refresh",
        balanceRefreshesInFlight.has(device.id)
          ? "Requesting…"
          : "Refresh Balance",
      );
      refreshBalance.disabled =
        withdraw.status?.balance_workflow_ready !== true ||
        balanceRefreshesInFlight.has(device.id);
      refreshBalance.addEventListener("click", () =>
        refreshWithdrawBalance(device.id),
      );
      balanceSummary.append(refreshBalance);

      if (withdraw.status?.balance_workflow_ready !== true) {
        const balanceHelp = document.createElement("span");
        balanceHelp.className = "device-balance-help";
        if (withdraw.status?.balance_workflow_error) {
          balanceHelp.textContent = uiBackendError(
            withdraw.status.balance_workflow_error_code,
            withdraw.status.balance_workflow_error_params,
            withdraw.status.balance_workflow_error,
          );
        } else {
          deviceUiText(
            balanceHelp,
            "balance.assignWorkflow",
            "Assign a Balance Check Workflow first.",
          );
        }
        balanceSummary.append(balanceHelp);
      }
    }
    const withdrawSummary = document.createElement("div");
    withdrawSummary.className =
      "account-summary withdraw-account-summary admin-only";
    const withdrawHeading = document.createElement("strong");
    deviceUiText(withdrawHeading, "withdraw.title", "Withdraw Account");
    const withdrawStatus = document.createElement("span");
    const assignedLabel = withdrawAssigned
      ? deviceUi("withdraw.assigned", "Assigned")
      : deviceUi("withdraw.notAssigned", "Not Assigned");
    deviceUiText(
      withdrawStatus,
      "withdraw.accountStatus",
      `Account Status: ${assignedLabel}`,
      () => ({
        status: withdrawAssigned
          ? deviceUi("withdraw.assigned", "Assigned")
          : deviceUi("withdraw.notAssigned", "Not Assigned"),
      }),
    );
    withdrawSummary.append(withdrawHeading, withdrawStatus);
    if (withdrawAssigned) {
      const runtime = withdrawRuntimeView(withdraw);
      const identity = document.createElement("span");
      deviceUiText(
        identity,
        "device.usernameAccount",
        `Username: ${withdrawApi.username} · Account ID: ${withdrawApi.operator_id}`,
        { username: withdrawApi.username, accountId: withdrawApi.operator_id },
      );
      const records = document.createElement("span");
      const paymentRecords = withdrawApi.payment_records?.length
        ? withdrawApi.payment_records.join(", ")
        : deviceUi("common.none", "None");
      deviceUiText(
        records,
        "device.paymentRecords",
        `Payment Records: ${paymentRecords}`,
        { records: paymentRecords },
      );
      const session = document.createElement("span");
      deviceUiText(
        session,
        "withdraw.sessionStatus",
        `Session: ${runtime.session} · Realtime: ${runtime.realtime}`,
        { session: runtime.session, realtime: runtime.realtime },
      );
      withdrawSummary.append(identity, records, session);

      if (typeof withdraw.status?.payment_ready === "boolean") {
        const readiness = document.createElement("span");
        readiness.className = withdraw.status.payment_ready
          ? "withdraw-payment-ready"
          : "withdraw-payment-warning";
        deviceUiText(
          readiness,
          withdraw.status.payment_ready
            ? "withdraw.paymentReady"
            : "withdraw.paymentNotReady",
          withdraw.status.payment_ready
            ? "Payment readiness: Ready"
            : "Payment readiness: Not ready",
        );
        withdrawSummary.append(readiness);
        if (
          !withdraw.status.payment_ready &&
          withdraw.status.payment_readiness_error
        ) {
          const readinessDetail = document.createElement("span");
          readinessDetail.className = "withdraw-payment-warning-detail";
          readinessDetail.textContent = uiBackendError(
            withdraw.status.payment_readiness_error_code,
            withdraw.status.payment_readiness_error_params,
            withdraw.status.payment_readiness_error,
          );
          withdrawSummary.append(readinessDetail);
        }
      }

      if (runtime.action === "reconnect") {
        const reconnect = document.createElement("button");
        reconnect.type = "button";
        deviceUiText(
          reconnect,
          withdrawReconnectsInFlight.has(device.id)
            ? "withdraw.reconnecting"
            : "withdraw.reconnect",
          withdrawReconnectsInFlight.has(device.id)
            ? "Reconnecting…"
            : "Reconnect",
        );
        reconnect.disabled =
          managementBlocked || withdrawReconnectsInFlight.has(device.id);
        reconnect.addEventListener("click", () => reconnectWithdraw(device.id));
        withdrawSummary.append(reconnect);
      } else if (runtime.action === "login") {
        const loginAgain = document.createElement("button");
        loginAgain.type = "button";
        deviceUiText(loginAgain, "withdraw.loginAgain", "Login Again");
        loginAgain.disabled = managementBlocked;
        loginAgain.addEventListener("click", () =>
          openWithdrawSettings(device, withdraw, { reauthenticate: true }),
        );
        withdrawSummary.append(loginAgain);
      }
    }
    const withdrawButton = document.createElement("button");
    withdrawButton.type = "button";
    deviceUiText(
      withdrawButton,
      withdrawAssigned ? "withdraw.changeAccount" : "withdraw.loginAssign",
      withdrawAssigned ? "Change Account" : "Login & Assign",
    );
    withdrawButton.disabled = managementBlocked;
    withdrawButton.addEventListener("click", () =>
      openWithdrawSettings(device, withdraw),
    );
    withdrawSummary.append(withdrawButton);

    let jobSummary = null;
    if (latestJob) {
      jobSummary = document.createElement("div");
      jobSummary.className = "device-job-summary";

      const phoneState = document.createElement("strong");
      const phoneStateKey =
        withdrawNeedsAttention ||
        latestJob.recovery_status === "NEEDS_ATTENTION"
          ? "device.phoneState.attention"
          : latestJob.recovery_status === "RECOVERING"
            ? "device.phoneState.recovering"
            : "device.phoneState.ready";
      deviceUiText(
        phoneState,
        phoneStateKey,
        phoneStateKey === "device.phoneState.attention"
          ? "Phone state: Needs Attention"
          : phoneStateKey === "device.phoneState.recovering"
            ? "Phone state: Recovering"
            : "Phone state: Ready",
      );

      const jobState = document.createElement("span");
      const displayedJobStatus = deviceStatusLabel(latestJob.status);
      deviceUiText(
        jobState,
        activeJob ? "device.currentJob" : "device.lastJob",
        `${activeJob ? "Current" : "Last"} job: ${displayedJobStatus}`,
        { status: displayedJobStatus },
      );
      jobSummary.append(phoneState, jobState);

      if (Number.isInteger(latestJob.failed_step_index)) {
        const failedStep = document.createElement("span");
        failedStep.className = "job-failed-step";

        const stepNumber = latestJob.failed_step_index + 1;
        const stepType = friendlyWorkflowStepType(latestJob.failed_step_type);

        deviceUiText(
          failedStep,
          "device.failedStep",
          stepType
            ? `Failed step: Step ${stepNumber} · ${stepType}`
            : `Failed step: Step ${stepNumber}`,
          { number: stepNumber, type: stepType ? ` · ${stepType}` : "" },
        );

        jobSummary.append(failedStep);
      }

      const withdrawReason =
        withdrawNeedsAttention && withdraw.status?.error
          ? withdraw.status.error
          : null;

      const error =
        latestJob.error || latestJob.recovery_error || withdrawReason;
      const errorCode = latestJob.error
        ? latestJob.error_code
        : latestJob.recovery_error
          ? latestJob.recovery_error_code
          : withdraw.status?.error_code;
      const errorParams = latestJob.error
        ? latestJob.error_params
        : latestJob.recovery_error
          ? latestJob.recovery_error_params
          : withdraw.status?.error_params;

      if (error) {
        const message = document.createElement("span");
        message.className = "job-error";
        const friendlyError = errorCode
          ? uiBackendError(errorCode, errorParams, String(error))
          : friendlyOperationalError(String(error));
        deviceUiText(message, "device.error", `Error: ${friendlyError}`, {
          message: friendlyError,
        });

        jobSummary.append(message);
      } else if (latestJob.result) {
        const message = document.createElement("span");
        message.className = "job-result";
        deviceUiText(message, "device.result", `Result: ${latestJob.result}`, {
          result: uiLiteral(latestJob.result),
        });
        jobSummary.append(message);
      }
    } else if (withdrawNeedsAttention) {
      jobSummary = document.createElement("div");
      jobSummary.className = "device-job-summary device-issue-summary";

      const issueLabel = document.createElement("strong");
      deviceUiText(
        issueLabel,
        "device.phoneState.attention",
        "Phone state: Needs Attention",
      );

      const issueMessage = document.createElement("span");
      issueMessage.textContent = withdraw.status?.error
        ? uiBackendError(
            withdraw.status.error_code,
            withdraw.status.error_params,
            withdraw.status.error,
          )
        : uiLiteral("Withdrawal recovery requires attention.");

      const jobState = document.createElement("span");
      deviceUiText(jobState, "device.currentJob.none", "Current job: —");

      jobSummary.append(issueLabel, jobState, issueMessage);
    } else if (manualWorkflowNeedsAttention) {
      jobSummary = document.createElement("div");
      jobSummary.className = "device-job-summary device-issue-summary";
      const issueLabel = document.createElement("strong");
      deviceUiText(
        issueLabel,
        "device.phoneState.attention",
        "Phone state: Needs Attention",
      );
      const issueMessage = document.createElement("span");
      issueMessage.textContent = friendlyOperationalError(
        workflowRuntime?.message || uiLiteral("This phone needs attention."),
      );
      jobSummary.append(issueLabel, issueMessage);
    }

    const actions = document.createElement("div");
    actions.className = "device-item-actions";

    const select = document.createElement("button");
    select.type = "button";
    deviceUiText(
      select,
      device.id === selected ? "common.controlling" : "common.select",
      device.id === selected ? "Controlling" : "Select",
    );
    select.disabled = device.id === selected || managementBlocked;
    select.addEventListener("click", () => selectDashboardDevice(device.id));

    const jobActions = createDeviceJobActions(
      latestJob,
      device.id,
      managementBlocked,
    );
    if (canManuallyCancelWithdrawal(withdraw)) {
      const cancelWithdrawal = document.createElement("button");
      cancelWithdrawal.type = "button";
      cancelWithdrawal.className = "stop danger-action";
      deviceUiText(
        cancelWithdrawal,
        withdrawCancelsInFlight.has(device.id)
          ? "withdraw.cancelling"
          : "withdraw.cancel",
        withdrawCancelsInFlight.has(device.id)
          ? "Cancelling Withdrawal…"
          : "Cancel Withdrawal",
      );
      cancelWithdrawal.disabled = withdrawCancelsInFlight.has(device.id);
      cancelWithdrawal.addEventListener("click", () =>
        openWithdrawCancel(device),
      );
      jobActions.append(cancelWithdrawal);
    }
    if (!latestJob && manualWorkflowNeedsAttention) {
      const assist = document.createElement("button");
      assist.type = "button";
      assist.className = "primary danger-action";
      deviceUiText(assist, "device.manualAssistance", "Manual Assistance");
      assist.disabled = managementBlocked;
      assist.addEventListener("click", () =>
        resolveDeviceIssue(device.id, null),
      );
      jobActions.append(assist);
    }

    const settings = document.createElement("button");
    settings.className = "admin-only";
    settings.type = "button";
    deviceUiText(settings, "device.cameraSettings", "Camera Settings");
    settings.disabled = managementBlocked;
    settings.addEventListener("click", () => openCameraSettings(device));

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "stop admin-only";
    deviceUiText(
      remove,
      deviceDeletesInFlight.has(device.id)
        ? "common.deleting"
        : "common.delete",
      deviceDeletesInFlight.has(device.id) ? "Deleting…" : "Delete",
    );
    remove.disabled = deviceDeletesInFlight.has(device.id) || managementBlocked;
    remove.addEventListener("click", () => deleteDevice(device, remove));
    actions.append(
      ...(jobActions.children.length ? [jobActions] : []),
      select,
      settings,
      remove,
    );
    card.append(
      heading,
      states,
      mac,
      preview,
      summary,
      workReadiness,
      balanceSummary,
      workflowSummary,
      withdrawSummary,
    );
    if (jobSummary) card.append(jobSummary);
    if (device.remote_control) card.append(remoteControlBanner(device));
    card.append(actions);
    return card;
  });

  const currentCards = Array.from(deviceList.children || []);
  if (
    currentCards.length !== cards.length ||
    cards.some((card, index) => currentCards[index] !== card)
  )
    deviceList.replaceChildren(...cards);
  $("device-enrollment-active").hidden = !enrollment.active;
  $("device-enrollment-form").hidden =
    !addPhonePanelOpen || !!enrollment.active;
  $("device-add").disabled = !!enrollment.active || managementBlocked;

  if (enrollment.active) {
    if (enrollment.target_device_id)
      deviceUiText(
        $("device-enrollment-target"),
        "device.enrolling",
        `Registering ${enrollment.target_device_id}`,
        { deviceId: enrollment.target_device_id },
      );
    else
      deviceUiText(
        $("device-enrollment-target"),
        "device.pairBluetooth",
        "Pair the phone from its Bluetooth settings",
      );
    if (Number.isFinite(enrollment.remaining_seconds)) {
      const seconds = Math.max(0, Math.ceil(enrollment.remaining_seconds));
      deviceUiText(
        $("device-enrollment-time"),
        "device.enrollmentRemaining",
        `${seconds} seconds remaining`,
        { seconds },
      );
    } else
      deviceUiText(
        $("device-enrollment-time"),
        "device.enrollmentActive",
        "Enrollment window active",
      );
  }
  renderJobs();
}

function getRootJobId(job, jobsById) {
  let current = job;

  while (current?.retry_of_job_id) {
    const parent = jobsById.get(current.retry_of_job_id);
    if (!parent) break;
    current = parent;
  }

  return current?.job_id || job.job_id;
}

function latestAttemptForJob(job) {
  if (!job) return null;

  const jobs = Array.isArray(jobCatalog) ? jobCatalog : [];
  const jobsById = new Map(jobs.map((item) => [item.job_id, item]));
  const rootId = getRootJobId(job, jobsById);

  let latest = null;

  for (const candidate of jobs) {
    if (getRootJobId(candidate, jobsById) === rootId) {
      latest = candidate;
    }
  }

  return latest || job;
}

function unresolvedRecoveryJob(job) {
  const latest = latestAttemptForJob(job);

  if (
    !latest ||
    !["FAILED", "INTERRUPTED"].includes(latest.status) ||
    latest.recovery_status === "READY"
  ) {
    return null;
  }

  return latest;
}

function unresolvedRecoveryJobForDevice(deviceId) {
  const jobs = Array.isArray(jobCatalog) ? jobCatalog : [];
  const jobsById = new Map(jobs.map((job) => [job.job_id, job]));
  const latestByRoot = new Map();

  for (const job of jobs) {
    const rootId = getRootJobId(job, jobsById);
    latestByRoot.set(rootId, job);
  }

  return (
    [...latestByRoot.values()]
      .reverse()
      .find(
        (job) =>
          job.device_id === deviceId &&
          ["FAILED", "INTERRUPTED"].includes(job.status) &&
          job.recovery_status !== "READY",
      ) || null
  );
}

function groupJobAttempts(jobs) {
  const jobsById = new Map(jobs.map((job) => [job.job_id, job]));
  const groups = new Map();

  for (const job of jobs) {
    const rootId = getRootJobId(job, jobsById);

    if (!groups.has(rootId)) {
      groups.set(rootId, []);
    }

    groups.get(rootId).push(job);
  }

  return [...groups.values()];
}

function renderJobs() {
  const jobs = Array.isArray(jobCatalog) ? jobCatalog : [];
  const groups = groupJobAttempts(jobs);

  if (groups.length)
    deviceUiText(
      $("jobs-state"),
      "jobs.summary",
      `Showing latest ${Math.min(groups.length, 10)} of ${groups.length} jobs.`,
      { shown: Math.min(groups.length, 10), count: groups.length },
    );
  else
    deviceUiText(
      $("jobs-state"),
      "status.noJobs",
      "No external jobs have been submitted.",
    );

  const cards = [...groups]
    .reverse()
    .slice(0, 10)
    .map((attempts) => {
      const rootJob = attempts[0];
      const job = attempts[attempts.length - 1];
      const item = document.createElement("article");
      item.className = "job-item";
      const id = document.createElement("strong");
      id.textContent = rootJob.job_id;
      const account = document.createElement("span");
      deviceUiText(
        account,
        "jobs.account",
        job.bank
          ? `Account: ${job.account_id} · ${job.bank}`
          : `Account: ${job.account_id}`,
        { account: job.account_id, bank: job.bank ? ` · ${job.bank}` : "" },
      );
      const device = document.createElement("span");
      deviceUiText(device, "jobs.device", `Device: ${job.device_id}`, {
        deviceId: job.device_id,
      });
      const workflow = document.createElement("span");
      deviceUiText(
        workflow,
        "jobs.workflow",
        job.action
          ? `Workflow: ${job.workflow} · ${job.action}`
          : `Workflow: ${job.workflow}`,
        {
          workflow: job.workflow,
          action: job.action ? ` · ${job.action}` : "",
        },
      );
      const status = document.createElement("span");
      status.textContent = job.queue_position
        ? deviceUi(
            "jobs.queuePosition",
            `${deviceStatusLabel(job.status)} · queue ${job.queue_position}`,
            {
              status: deviceStatusLabel(job.status),
              position: job.queue_position,
            },
          )
        : deviceStatusLabel(job.status);

      const created = document.createElement("span");
      deviceUiText(created, "jobs.queued", `Queued: ${job.created_at || "—"}`, {
        time: job.created_at || "—",
      });
      const activity = document.createElement("span");
      if (job.finished_at)
        deviceUiText(
          activity,
          "jobs.finished",
          `Finished: ${job.finished_at}`,
          { time: job.finished_at },
        );
      else if (job.started_at)
        deviceUiText(activity, "jobs.started", `Started: ${job.started_at}`, {
          time: job.started_at,
        });
      else deviceUiText(activity, "workflow.notStarted", "Not started");

      item.append(id, account, device, workflow, status, created, activity);

      if (attempts.length > 1) {
        const attemptInfo = document.createElement("span");
        deviceUiText(
          attemptInfo,
          "jobs.attempts",
          `Attempts: ${attempts.length}`,
          { count: attempts.length },
        );
        item.append(attemptInfo);
      }

      if (job.error) {
        const error = document.createElement("span");
        error.className = "job-error";
        const technical = String(job.error);
        error.textContent = job.error_code
          ? uiBackendError(job.error_code, job.error_params, technical)
          : friendlyOperationalError(technical);
        item.append(error);
        if (error.textContent !== technical) {
          const details = document.createElement("details");
          details.className = "job-error-details";
          const summary = document.createElement("summary");
          deviceUiText(
            summary,
            "workflow.technicalDetails",
            "Technical details",
          );
          const message = document.createElement("code");
          message.textContent = technical;
          details.append(summary, message);
          item.append(details);
        }
      }
      if (job.result) {
        const result = document.createElement("span");
        result.className = "job-result";
        uiLiteralText(result, job.result);
        item.append(result);
      }
      if (job.recovery_status) {
        const recovery = document.createElement("span");
        recovery.className = `job-recovery-state job-recovery-${job.recovery_status.toLowerCase()}`;
        uiLiteralText(
          recovery,
          job.recovery_status === "READY"
            ? "Phone state: Ready"
            : job.recovery_status === "RECOVERING"
              ? "Phone state: Recovering"
              : "Phone state: Needs Attention",
        );
        item.append(recovery);
      }
      return item;
    });

  $("jobs-list").replaceChildren(...cards);
  renderRemoteControlHistory();
}

function localTime(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? String(iso) : date.toLocaleString();
}

function remoteControlBanner(device) {
  const remote = device.remote_control;
  const banner = document.createElement("div");
  banner.className = "remote-control-banner";
  banner.setAttribute("role", "status");
  const heading = document.createElement("strong");
  deviceUiText(
    heading,
    "remote.heldBy",
    `Remote control by ${remote.user_id}`,
    { user: remote.user_id },
  );
  const reason = document.createElement("span");
  deviceUiText(reason, "remote.reason", `Reason: ${remote.reason}`, {
    reason: remote.reason,
  });
  const since = document.createElement("span");
  const started = localTime(remote.started_at);
  if (remote.waiting_for_job)
    deviceUiText(
      since,
      "remote.sinceWaiting",
      `Since ${started} · waiting for the job to stop`,
      { time: started },
    );
  else
    deviceUiText(since, "remote.since", `Since ${started}`, { time: started });
  const end = document.createElement("button");
  end.type = "button";
  end.className = "stop";
  deviceUiText(end, "remote.end", "End Remote Control");
  end.addEventListener("click", () => endRemoteControl(device.id, end));
  banner.append(heading, reason, since, end);
  return banner;
}

async function endRemoteControl(deviceId, button) {
  if (
    !window.confirm(
      deviceUi(
        "remote.confirmEnd",
        "Take this phone back from the remote user?",
      ),
    )
  )
    return;
  button.disabled = true;
  try {
    await deviceService.endRemoteControl(deviceId);
    const device = deviceCatalog?.devices?.find((item) => item.id === deviceId);
    if (device) device.remote_control = null;
    notice(
      deviceUi(
        "remote.ended",
        "Remote control ended. The phone is back under local control.",
      ),
    );
  } catch (error) {
    notice(error.message, true);
  } finally {
    deviceRenderSignature = null;
    renderDevices();
    await loadDevices();
  }
}

const REMOTE_ENDED_BY = {
  operator: ["remote.endedBy.operator", "operator"],
  remote: ["remote.endedBy.remote", "remote user"],
  lease_expired: ["remote.endedBy.leaseExpired", "lease expired"],
  shutdown: ["remote.endedBy.shutdown", "app stopped"],
};

function renderRemoteControlHistory() {
  const list = $("remote-history");
  if (!list) return;
  const items = remoteControlHistory.slice(0, 20).map((record) => {
    const item = document.createElement("article");
    item.className = "job-item";
    const what = document.createElement("strong");
    if (record.event === "started")
      deviceUiText(what, "remote.historyStarted", "Remote takeover started");
    else if (record.event === "calibrated")
      deviceUiText(
        what,
        "remote.historyCalibrated",
        "Phone screen recalibrated remotely",
      );
    else {
      const [key, fallback] = REMOTE_ENDED_BY[record.ended_by] || [
        null,
        record.ended_by,
      ];
      const endedBy = key ? deviceUi(key, fallback) : fallback;
      deviceUiText(
        what,
        "remote.historyEnded",
        `Remote takeover ended (${endedBy})`,
        { endedBy },
      );
    }
    const who = document.createElement("span");
    deviceUiText(
      who,
      "remote.historyUser",
      `User: ${record.user_id} · session ${record.session_id}`,
      { user: record.user_id, session: record.session_id },
    );
    const device = document.createElement("span");
    deviceUiText(device, "jobs.device", `Device: ${record.device_id}`, {
      deviceId: record.device_id,
    });
    const when = document.createElement("span");
    when.textContent = localTime(record.at);
    const reason = document.createElement("span");
    reason.className = "job-result";
    deviceUiText(reason, "remote.reason", `Reason: ${record.reason}`, {
      reason: record.reason,
    });
    item.append(what, who, device, when, reason);
    if (record.interrupted_job) {
      const job = document.createElement("span");
      job.className = "job-error";
      const interrupted = record.interrupted_job;
      deviceUiText(
        job,
        "remote.interruptedJob",
        `Interrupted job ${interrupted.id} (${interrupted.workflow}, step ${interrupted.step_index + 1})`,
        {
          id: interrupted.id,
          workflow: interrupted.workflow,
          step: interrupted.step_index + 1,
        },
      );
      item.append(job);
    }
    if (record.event === "ended") {
      const inputs = document.createElement("span");
      deviceUiText(inputs, "remote.inputs", `Inputs: ${record.inputs}`, {
        count: record.inputs,
      });
      item.append(inputs);
    }
    return item;
  });
  if (items.length)
    deviceUiText(
      $("remote-history-state"),
      "remote.historySummary",
      `Latest ${items.length} remote control events.`,
      { count: items.length },
    );
  else
    deviceUiText(
      $("remote-history-state"),
      "remote.historyEmpty",
      "No remote control sessions yet.",
    );
  list.replaceChildren(...items);
}

function friendlyOperationalError(message) {
  // Shown in full: it names who took over and why.
  if (/^interrupted by remote takeover/i.test(message)) return message;
  const value = message.toLowerCase();
  if (/calibrat/.test(value))
    return deviceUi("camera.calibrationRequired", "Calibration required");
  if (/camera/.test(value))
    return deviceUi("camera.unavailable", "Camera unavailable");
  if (/ocr|paddle|text detection/.test(value))
    return deviceUi("error.ocrUnavailable", "OCR unavailable");
  if (
    /device (?:is )?busy|already has a running job|active reservation|scheduler reservation|device ownership/.test(
      value,
    )
  )
    return deviceUi("device.busy", "Device busy");
  if (/external lock result is uncertain/.test(value))
    return uiLiteral(
      "Withdrawal lock status is uncertain. Reconcile before retrying.",
    );
  if (/raspberry|pi api|pi unavailable|cannot reach pi/.test(value))
    return deviceUi("error.piUnavailable", "Raspberry Pi unavailable");
  if (/disconnect|bluetooth|phone unavailable/.test(value))
    return deviceUi("device.phoneDisconnected", "Phone disconnected");
  if (/interrupt|restart/.test(value))
    return deviceUi("error.jobInterrupted", "Job interrupted");
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

  const targetedJob = recoveryTargetJobId
    ? jobCatalog.find((job) => job.job_id === recoveryTargetJobId)
    : null;

  const targetJob = targetedJob
    ? unresolvedRecoveryJob(targetedJob)
    : unresolvedRecoveryJobForDevice(selectedDeviceId);

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
          : "Use Manual Assistance to restore the phone, then verify its expected starting screen.",
      state: "Needs Attention",
      recoverAvailable: false,
      manualAvailable: true,
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
  const selectedDeviceId = deviceCatalog?.selected_device;

  const targetedJob = recoveryTargetJobId
    ? jobCatalog.find((job) => job.job_id === recoveryTargetJobId)
    : null;

  const recoveryJob = targetedJob
    ? latestAttemptForJob(targetedJob)
    : unresolvedRecoveryJobForDevice(selectedDeviceId);

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
      const recoveredDeviceId = recoveryJob.device_id;
      readyVerificationJobId = null;
      readyVerificationFailedJobId = null;
      readyVerificationMessage = "";
      readyVerificationRestoreManual = false;
      manualAssistanceOpen = false;
      recoveryPanelOpen = false;
      recoveryTargetJobId = null;
      returnToDeviceCard(recoveredDeviceId);
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
    const jobsById = new Map(jobCatalog.map((job) => [job.job_id, job]));

    const latestByRoot = new Map();

    for (const job of jobCatalog) {
      const rootId = getRootJobId(job, jobsById);
      latestByRoot.set(rootId, job);
    }

    const affectedDevices = new Set(
      [...latestByRoot.values()]
        .filter(
          (job) =>
            ["FAILED", "INTERRUPTED"].includes(job.status) &&
            job.recovery_status !== "READY",
        )
        .map((job) => job.device_id),
    );

    if (affectedDevices.size > 1)
      deviceUiText(
        $("operate-attention-message"),
        "device.multipleAttention",
        `${affectedDevices.size} devices need attention`,
        { count: affectedDevices.size },
      );
    else uiErrorText($("operate-attention-message"), issue.label);
  }

  $("operate-recovery").hidden = !recoveryPanelOpen || (!issue && !recovered);
  if (issue) {
    uiErrorText($("operate-recovery-issue"), issue.label);
    uiLiteralText($("operate-recovery-recommendation"), issue.recommendation);
    uiLiteralText(
      $("operate-recovery-state"),
      issue.state || "Needs Attention",
    );
  } else if (recovered) {
    uiLiteralText($("operate-recovery-issue"), "Phone recovered successfully.");
    uiLiteralText(
      $("operate-recovery-recommendation"),
      "The configured ready-screen condition was detected. Review the failed job before starting new business work.",
    );
    uiLiteralText($("operate-recovery-state"), "Ready");
  }
  $("operate-recover-phone").hidden = !issue?.recoverAvailable;
  $("operate-recover-phone").disabled = recoveryRequestInFlight;
  $("operate-open-assistance").hidden = !issue?.manualAvailable;
  $("manual-assistance-finish").hidden = !manualAssistanceOpen;
  $("operate-verify-finish").disabled =
    recoveryRequestInFlight ||
    readyVerificationRestoreInFlight ||
    readyVerificationFinishInFlight;
  if (recoveryJob?.recovery_status === "RECOVERING")
    uiLiteralText(
      $("operate-recovery-result"),
      "Recovery workflow is running. The failed workflow will not be retried.",
    );
  else if (recoveryJob?.job_id === readyVerificationFailedJobId)
    uiLiteralText(
      $("operate-recovery-result"),
      "Expected starting screen was not detected.",
    );
  else if (
    recoveryJob?.recovery_status === "NEEDS_ATTENTION" &&
    recoveryAttempted
  )
    uiErrorText(
      $("operate-recovery-result"),
      recoveryJob.recovery_error ||
        "Automatic recovery failed. Resolve the phone issue manually.",
    );
  else if (recovered)
    uiLiteralText(
      $("operate-recovery-result"),
      "Phone recovered successfully. Status: Ready.",
    );
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
  uiLiteralText($("operate-recovery-result"), "Starting recovery workflow…");
  renderOperationalRecovery();
  try {
    const updated = await jobService.recover(jobId);
    jobCatalog = jobCatalog.map((job) =>
      job.job_id === updated.job_id ? updated : job,
    );
  } catch (error) {
    deviceUiText(
      $("operate-recovery-result"),
      "error.recoveryStart",
      `Recovery could not start: ${friendlyOperationalError(error.message)}`,
      { message: friendlyOperationalError(error.message) },
    );
  } finally {
    recoveryRequestInFlight = false;
    renderJobs();
    renderOperationalRecovery();
  }
}

async function openManualAssistance(jobId = null) {
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

  if (!manual) {
    try {
      await enableManualControl();
    } catch (error) {
      const params = { operation: "enable_manual_assistance" };
      const deviceId = deviceCatalog?.selected_device;
      if (deviceId) params.device_id = deviceId;
      if (jobId) params.job_id = jobId;
      if (error?.errorCode) params.error_code = error.errorCode;
      if (error?.status) params.status = error.status;
      reportFrontendDiagnostic("frontend_manual_assistance_failed", params);
      notice(
        deviceUi(
          "error.manualEnable",
          `Manual control could not be enabled: ${friendlyOperationalError(error.message)}`,
          { message: friendlyOperationalError(error.message) },
        ),
        true,
      );
    }
  }
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

    if (job) {
      readyVerificationFinishInFlight = true;
      readyVerificationMessage = "Marking phone ready…";
      renderOperationalRecovery();

      try {
        const updated = await jobService.markReady(job.job_id);

        jobCatalog = jobCatalog.map((candidate) =>
          candidate.job_id === updated.job_id ? updated : candidate,
        );

        readyVerificationJobId = null;
        readyVerificationFailedJobId = null;
        readyVerificationMessage = "";
        readyVerificationRestoreManual = false;

        manualAssistanceOpen = false;
        recoveryPanelOpen = false;
        recoveryTargetJobId = null;

        renderJobs();
        returnToDeviceCard(updated.device_id);
        renderOperationalRecovery();
      } catch (error) {
        readyVerificationMessage = deviceUi(
          "error.markReady",
          `Phone could not be marked ready: ${friendlyOperationalError(error.message)}`,
          { message: friendlyOperationalError(error.message) },
        );

        if (restoreManualOnFailure)
          await restoreManualControlAfterVerificationFailure();
      } finally {
        readyVerificationFinishInFlight = false;
        renderOperationalRecovery();
      }

      return;
    }
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
    readyVerificationMessage = deviceUi(
      "error.verifyReady",
      `Ready state could not be verified: ${friendlyOperationalError(error.message)}`,
      { message: friendlyOperationalError(error.message) },
    );
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

function returnToDeviceCard(deviceId) {
  if (!deviceId) return;
  returnedDeviceId = deviceId;
  deviceRenderSignature = null;
  renderDevices();
  const card = Array.from($("device-list").children || []).find(
    (candidate) => candidate.dataset?.deviceId === deviceId,
  );
  card?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  notice("Phone restored and ready.");
  void loadDevices();
  setTimeout(() => {
    if (returnedDeviceId !== deviceId) return;
    returnedDeviceId = null;
    card?.classList?.remove?.("device-card--returned");
  }, 2400);
}

function viewOperationalDetails() {
  const issue = currentOperationalIssue();
  if (issue?.kind === "job") {
    const card = Array.from($("device-list").children || []).find(
      (candidate) => candidate.dataset?.deviceId === issue.job.device_id,
    );
    card?.scrollIntoView?.({ block: "center", behavior: "smooth" });
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

function setWorkflowSettingsError(message = "") {
  uiErrorText($("workflow-settings-error"), message);
  $("workflow-settings-error").hidden = !message;
}

function closeWorkflowSettings() {
  workflowSettingsDeviceId = null;
  workflowSettingsData = null;
  workflowSettingsMode = "payment";
  $("workflow-settings-dialog").hidden = true;
  $("workflow-settings-search").value = "";
  $("workflow-settings-balance-search").value = "";
  $("workflow-settings-payment-section").hidden = false;
  $("workflow-settings-balance-section").hidden = false;
  setWorkflowSettingsError();
}

function setWithdrawSettingsError(message = "") {
  uiErrorText($("withdraw-settings-error"), message);
  $("withdraw-settings-error").hidden = !message;
}

function setWithdrawCancelError(message = "") {
  uiErrorText($("withdraw-cancel-error"), message);
  $("withdraw-cancel-error").hidden = !message;
}

function closeWithdrawCancel() {
  if (
    withdrawCancelDeviceId &&
    withdrawCancelsInFlight.has(withdrawCancelDeviceId)
  )
    return;
  withdrawCancelDeviceId = null;
  withdrawCancelPendingReason = null;
  $("withdraw-cancel-reason").value = "";
  $("withdraw-cancel-confirm").disabled = true;
  $("withdraw-cancel-dialog").hidden = true;
  $("withdraw-cancel-final-dialog").hidden = true;
  $("withdraw-cancel-final-confirm").disabled = false;
  setWithdrawCancelError();
}

function openWithdrawCancel(device) {
  withdrawCancelDeviceId = device.id;
  deviceUiText(
    $("withdraw-cancel-title"),
    "withdraw.cancelNamed",
    `Cancel Withdrawal · ${device.name || device.id}`,
    { name: device.name || device.id },
  );
  $("withdraw-cancel-reason").value = "";
  $("withdraw-cancel-confirm").disabled = true;
  $("withdraw-cancel-dialog").hidden = false;
  setWithdrawCancelError();
  $("withdraw-cancel-reason").focus();
}

function updateWithdrawCancelConfirm() {
  $("withdraw-cancel-confirm").disabled =
    !withdrawCancelDeviceId ||
    !$("withdraw-cancel-reason").value ||
    withdrawCancelsInFlight.has(withdrawCancelDeviceId);
}

function closeWithdrawCancelFinal({ back = false } = {}) {
  if (
    withdrawCancelDeviceId &&
    withdrawCancelsInFlight.has(withdrawCancelDeviceId)
  )
    return;
  withdrawCancelPendingReason = null;
  $("withdraw-cancel-final-dialog").hidden = true;
  if (back) $("withdraw-cancel-dialog").hidden = false;
}

function openWithdrawCancelFinal(reasonCode) {
  withdrawCancelPendingReason = reasonCode;
  $("withdraw-cancel-dialog").hidden = true;
  $("withdraw-cancel-final-dialog").hidden = false;
  $("withdraw-cancel-final-confirm").disabled = false;
  $("withdraw-cancel-final-confirm").focus();
}

async function submitWithdrawCancel(deviceId, reasonCode) {
  if (!deviceId || !reasonCode || withdrawCancelsInFlight.has(deviceId)) return;

  withdrawCancelsInFlight.add(deviceId);
  updateWithdrawCancelConfirm();
  $("withdraw-cancel-final-confirm").disabled = true;
  setWithdrawCancelError();
  deviceRenderSignature = null;
  renderDevices();

  try {
    const status = await deviceService.cancelWithdrawal(deviceId, reasonCode);
    deviceWithdrawDetails[deviceId] = {
      ...(deviceWithdrawDetails[deviceId] || {}),
      status,
    };
    withdrawCancelsInFlight.delete(deviceId);
    closeWithdrawCancel();
    deviceRenderSignature = null;
    renderDevices();
    notice("Withdrawal cancelled successfully.");
    await loadDevices();
  } catch (error) {
    $("withdraw-cancel-final-dialog").hidden = true;
    $("withdraw-cancel-dialog").hidden = false;
    setWithdrawCancelError(
      error?.message ||
        "External cancellation failed. This withdrawal remains under manual attention.",
    );
    await loadDevices().catch(() => {});
  } finally {
    withdrawCancelsInFlight.delete(deviceId);
    updateWithdrawCancelConfirm();
    $("withdraw-cancel-final-confirm").disabled = false;
    deviceRenderSignature = null;
    renderDevices();
  }
}

async function confirmWithdrawCancel(event) {
  event.preventDefault();
  const deviceId = withdrawCancelDeviceId;
  const reasonCode = $("withdraw-cancel-reason").value;
  if (!deviceId || !reasonCode || withdrawCancelsInFlight.has(deviceId)) return;

  openWithdrawCancelFinal(reasonCode);
}

async function confirmWithdrawCancelFinal() {
  const deviceId = withdrawCancelDeviceId;
  const reasonCode = withdrawCancelPendingReason;
  if (!deviceId || !reasonCode || withdrawCancelsInFlight.has(deviceId)) return;
  await submitWithdrawCancel(deviceId, reasonCode);
}

function closeWithdrawSettings() {
  withdrawSettingsDeviceId = null;
  withdrawSettingsAssigned = false;
  withdrawSettingsReauthentication = false;
  $("withdraw-settings-password").value = "";
  $("withdraw-settings-totp").value = "";
  $("withdraw-settings-dialog").hidden = true;
  setWithdrawSettingsError();
}

function openWithdrawSettings(
  device,
  details = {},
  { reauthenticate = false } = {},
) {
  const api = details.withdraw_api || {};

  withdrawSettingsDeviceId = device.id;
  withdrawSettingsAssigned =
    details.assigned === true ||
    (Number.isInteger(api.operator_id) && !!api.username);
  withdrawSettingsReauthentication =
    reauthenticate === true && withdrawSettingsAssigned;

  deviceUiText(
    $("withdraw-settings-title"),
    "withdraw.titleNamed",
    `Withdraw Account · ${device.name || device.id}`,
    { name: device.name || device.id },
  );

  uiLiteralText(
    $("withdraw-settings-status"),
    withdrawSettingsAssigned
      ? "Account Status: Assigned"
      : "Account Status: Not Assigned",
  );

  if (withdrawSettingsAssigned) {
    const records = api.payment_records?.length
      ? api.payment_records.join(", ")
      : deviceUi("common.none", "None");
    deviceUiText(
      $("withdraw-settings-identity"),
      "withdraw.usernameIdentity",
      `Username: ${api.username} · Account ID: ${api.operator_id} · Payment Records: ${records}`,
      { username: api.username, accountId: api.operator_id, records },
    );
  } else
    uiLiteralText(
      $("withdraw-settings-identity"),
      "Login with the external operator account assigned to this phone.",
    );

  $("withdraw-settings-username").value = api.username || "";
  $("withdraw-settings-username").readOnly = withdrawSettingsReauthentication;

  $("withdraw-settings-password").value = "";
  $("withdraw-settings-password").placeholder = details.credentials_saved
    ? uiLiteral("Enter password to authenticate")
    : "";

  $("withdraw-settings-totp").value = "";
  $("withdraw-settings-totp").placeholder = "123456";

  uiLiteralText(
    $("withdraw-settings-submit"),
    withdrawSettingsReauthentication
      ? "Login Again"
      : withdrawSettingsAssigned
        ? "Reauthenticate"
        : "Login & Assign",
  );

  $("withdraw-settings-dialog").hidden = false;

  setWithdrawSettingsError();

  $("withdraw-settings-username").focus();
}

async function saveWithdrawSettings(event) {
  event.preventDefault();

  if (!withdrawSettingsDeviceId) return;

  const button = $("withdraw-settings-submit");
  const g2fCode = $("withdraw-settings-totp").value.trim();

  if (!/^\d{6}$/.test(g2fCode)) {
    setWithdrawSettingsError("Google 2FA code must be exactly 6 digits.");
    $("withdraw-settings-totp").focus();
    return;
  }

  button.disabled = true;
  setWithdrawSettingsError();

  try {
    const login = {
      username: $("withdraw-settings-username").value.trim(),
      password: $("withdraw-settings-password").value,
      g2f_code: g2fCode,
    };
    if (withdrawSettingsReauthentication) login.reauthenticate = true;

    const result = await deviceService.loginWithdraw(
      withdrawSettingsDeviceId,
      login,
    );

    const deviceId = withdrawSettingsDeviceId;

    deviceWithdrawDetails[deviceId] = {
      ...(deviceWithdrawDetails[deviceId] || {}),
      assigned: true,
      credentials_saved: result.credentials_saved === true,
      withdraw_api: {
        ...(deviceWithdrawDetails[deviceId]?.withdraw_api || {}),
        operator_id: result.operator_id,
        username: result.username,
        payment_records: result.payment_records || [],
      },
    };

    closeWithdrawSettings();

    deviceRenderSignature = null;
    renderDevices();

    notice("Withdraw account authenticated successfully.");

    await loadDevices();
  } catch (error) {
    setWithdrawSettingsError(error.message);
  } finally {
    $("withdraw-settings-totp").value = "";
    button.disabled = false;
  }
}

function renderWorkflowSettingsOptions() {
  if (!workflowSettingsData) return;
  const query = $("workflow-settings-search").value.trim().toLocaleLowerCase();
  const allowed = workflowSettingsData.unrestricted
    ? new Set(
        workflowSettingsData.available_workflows.map((workflow) => workflow.id),
      )
    : new Set(workflowSettingsData.allowed_workflow_ids || []);
  const available = workflowSettingsData.available_workflows.map(
    (workflow) => ({
      id: workflow.id,
      name: workflow.name,
      purpose: workflow.purpose,
      missing: false,
    }),
  );
  for (const workflowId of workflowSettingsData.missing_workflow_ids || [])
    available.push({ id: workflowId, name: "Missing workflow", missing: true });

  const options = available
    .filter((workflow) => workflow.purpose === "payment" || workflow.missing)
    .filter((workflow) =>
      `${workflow.name} ${workflow.id}`.toLocaleLowerCase().includes(query),
    )
    .map((workflow) => {
      const label = document.createElement("label");
      label.className =
        "workflow-assignment-option" + (workflow.missing ? " missing" : "");
      label.dataset.workflowId = workflow.id;
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "device-workflow";
      input.checked = allowed.has(workflow.id);
      const text = document.createElement("span");
      const name = document.createElement("strong");
      name.textContent = workflow.missing
        ? `${workflow.name} · ${uiLiteral("Remove this assignment")}`
        : workflow.name;
      const id = document.createElement("small");
      id.textContent = workflow.id;
      text.append(name, id);
      label.append(input, text);
      return label;
    });
  $("workflow-settings-list").replaceChildren(...options);
}

function renderBalanceWorkflowOptions() {
  if (!workflowSettingsData) return;
  const query = $("workflow-settings-balance-search")
    .value.trim()
    .toLocaleLowerCase();
  const assignedId = workflowSettingsData.balance_workflow_id || "";
  const assigned = workflowSettingsData.balance_workflow;

  const available = workflowSettingsData.available_workflows
    .filter(
      (workflow) =>
        workflow.purpose === "balance_check" &&
        workflow.balance_check_eligible === true,
    )
    .filter((workflow) =>
      `${workflow.name} ${workflow.id}`.toLocaleLowerCase().includes(query),
    );

  const options = [];

  const noneLabel = document.createElement("label");
  noneLabel.className = "workflow-assignment-option";
  noneLabel.dataset.workflowId = "";
  const noneInput = document.createElement("input");
  noneInput.type = "radio";
  noneInput.name = "device-balance-workflow";
  noneInput.checked = !assignedId;
  const noneText = document.createElement("span");
  const noneName = document.createElement("strong");
  uiLiteralText(noneName, "No Balance Check Workflow assigned");
  const noneHint = document.createElement("small");
  uiLiteralText(noneHint, "Disable automatic balance checks for this phone");
  noneText.append(noneName, noneHint);
  noneLabel.append(noneInput, noneText);
  options.push(noneLabel);

  for (const workflow of available) {
    const label = document.createElement("label");
    label.className = "workflow-assignment-option";
    label.dataset.workflowId = workflow.id;
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "device-balance-workflow";
    input.checked = workflow.id === assignedId;
    const text = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = workflow.name;
    const id = document.createElement("small");
    id.textContent = workflow.id;
    text.append(name, id);
    label.append(input, text);
    options.push(label);
  }

  // Keep a broken existing assignment visible so the operator understands why
  // balance refresh is unavailable and can deliberately replace or remove it.
  if (
    assignedId &&
    assigned &&
    (assigned.missing || assigned.balance_check_eligible === false) &&
    !available.some((workflow) => workflow.id === assignedId)
  ) {
    const label = document.createElement("label");
    label.className = "workflow-assignment-option missing";
    label.dataset.workflowId = assignedId;
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "device-balance-workflow";
    input.checked = true;
    input.disabled = true;
    const text = document.createElement("span");
    const name = document.createElement("strong");
    uiLiteralText(
      name,
      assigned.missing
        ? "Missing Balance Check Workflow"
        : "Invalid Balance Check Workflow",
    );
    const id = document.createElement("small");
    id.textContent = assigned.name
      ? `${assigned.name} · ${assigned.id}`
      : assigned.id;
    text.append(name, id);
    label.append(input, text);
    options.push(label);
  }

  $("workflow-settings-balance-list").replaceChildren(...options);
}

async function openWorkflowSettings(device, mode = "payment") {
  workflowSettingsDeviceId = device.id;
  workflowSettingsData = null;
  workflowSettingsMode = mode === "balance" ? "balance" : "payment";

  const balanceMode = workflowSettingsMode === "balance";
  const workflowType = balanceMode
    ? deviceUi("workflow.balance", "Balance Check Workflow")
    : deviceUi("workflow.payment", "Payment Workflow");
  deviceUiText(
    $("workflow-settings-title"),
    "workflow.settingsNamed",
    `${workflowType} · ${device.name || device.id}`,
    { type: workflowType, name: device.name || device.id },
  );
  uiLiteralText($("workflow-settings-help"), "Loading workflows…");
  $("workflow-settings-payment-section").hidden = balanceMode;
  $("workflow-settings-balance-section").hidden = !balanceMode;
  uiLiteralText(
    $("workflow-settings-save"),
    balanceMode ? "Save Balance Check Workflow" : "Save Payment Workflow",
  );
  $("workflow-settings-list").replaceChildren();
  $("workflow-settings-dialog").hidden = false;
  setWorkflowSettingsError();

  try {
    workflowSettingsData = await deviceService.workflows(device.id);
    uiLiteralText(
      $("workflow-settings-help"),
      balanceMode
        ? "Choose the Balance Check Workflow used to read this payer's balance."
        : "Choose the Payment Workflow that may run on this phone.",
    );
    renderWorkflowSettingsOptions();
    renderBalanceWorkflowOptions();
    if (balanceMode) $("workflow-settings-balance-search").focus();
    else $("workflow-settings-search").focus();
  } catch (error) {
    setWorkflowSettingsError(error.message);
  }
}

async function saveWorkflowSettings(event) {
  event.preventDefault();
  if (!workflowSettingsDeviceId || !workflowSettingsData) return;

  const savingMode = workflowSettingsMode;
  let workflowIds;
  let balanceWorkflowId;

  if (savingMode === "balance") {
    // Preserve the Payment Workflow exactly as it was while editing only the
    // Balance Check Workflow.
    workflowIds = workflowSettingsData.unrestricted
      ? []
      : [...(workflowSettingsData.allowed_workflow_ids || [])];
    const selectedBalance = Array.from(
      $("workflow-settings-balance-list").children,
    ).find(
      (option) => option.children[0]?.checked && !option.children[0]?.disabled,
    );
    balanceWorkflowId = selectedBalance?.dataset.workflowId || null;
  } else {
    const selected = Array.from($("workflow-settings-list").children).find(
      (option) => option.children[0]?.checked,
    );
    workflowIds = selected ? [selected.dataset.workflowId] : [];
    // Preserve the Balance Check Workflow while editing only Payment Workflow.
    balanceWorkflowId = workflowSettingsData.balance_workflow_id || null;
  }

  const button = $("workflow-settings-save");
  button.disabled = true;
  setWorkflowSettingsError();
  try {
    await deviceService.updateWorkflows(
      workflowSettingsDeviceId,
      workflowIds,
      balanceWorkflowId,
    );
    closeWorkflowSettings();
    deviceRenderSignature = null;
    await loadDevices();
    await window.phoneRecorder?.workflowAssignmentsChanged?.();
    notice(
      savingMode === "balance"
        ? "Balance Check Workflow saved."
        : "Payment Workflow saved.",
    );
  } catch (error) {
    setWorkflowSettingsError(error.message);
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

function deviceRenderSnapshot(catalog, cameras, withdrawDetails, jobs) {
  return {
    devices: (catalog.devices || []).map((device) => ({
      id: device.id,
      name: device.name,
      connected: device.connected,
      default: device.default,
      bluetooth_address: device.bluetooth_address,
      ready_for_work: device.ready_for_work,
      workflow_assignment: device.workflow_assignment,
      remote_control: device.remote_control
        ? {
            session_id: device.remote_control.session_id,
            user_id: device.remote_control.user_id,
            reason: device.remote_control.reason,
            started_at: device.remote_control.started_at,
            waiting_for_job: device.remote_control.waiting_for_job,
          }
        : null,
    })),

    enrollment: catalog.enrollment
      ? {
          active: catalog.enrollment.active,
          target_device_id: catalog.enrollment.target_device_id,
          remaining_seconds: catalog.enrollment.remaining_seconds,
        }
      : null,

    default_device: catalog.default_device,
    selected_device: catalog.selected_device,

    cameras: Object.fromEntries(
      Object.entries(cameras || {}).map(([deviceId, details]) => [
        deviceId,
        {
          config: details?.config,
          status: {
            ready: details?.status?.ready,
            poll_failed: details?.status?.poll_failed,
            width: details?.status?.width,
            height: details?.status?.height,
          },
        },
      ]),
    ),

    withdraw: Object.fromEntries(
      Object.entries(withdrawDetails || {}).map(([deviceId, details]) => [
        deviceId,
        {
          assigned: details?.assigned,
          withdraw_api: {
            operator_id: details?.withdraw_api?.operator_id,
            username: details?.withdraw_api?.username,
            payment_records: details?.withdraw_api?.payment_records,
          },
          status: {
            state: details?.status?.state,
            phase: details?.status?.phase,
            error: details?.status?.error,
            error_code: details?.status?.error_code,
            error_params: details?.status?.error_params,
            session_state: details?.status?.session_state,
            physical_socket_connected:
              details?.status?.physical_socket_connected,

            payment_ready: details?.status?.payment_ready,
            payment_readiness_error: details?.status?.payment_readiness_error,
            payment_readiness_error_code:
              details?.status?.payment_readiness_error_code,
            payment_readiness_error_params:
              details?.status?.payment_readiness_error_params,

            balance_state: details?.status?.balance_state,
            balance_valid: details?.status?.balance_valid,
            last_balance: details?.status?.last_balance,
            balance_observed_at: details?.status?.balance_observed_at,
            balance_error: details?.status?.balance_error,
            balance_error_code: details?.status?.balance_error_code,
            balance_error_params: details?.status?.balance_error_params,
            balance_workflow_ready: details?.status?.balance_workflow_ready,
            balance_workflow_error: details?.status?.balance_workflow_error,
            balance_workflow_error_code:
              details?.status?.balance_workflow_error_code,
            balance_workflow_error_params:
              details?.status?.balance_workflow_error_params,

            cancel_allowed: details?.status?.cancel_allowed,
            withdraw_id: details?.status?.withdraw_id,
            tx_id: details?.status?.tx_id,
            job_id: details?.status?.job_id,
          },
        },
      ]),
    ),

    // Device cards only consume operational/recovery fields. Excluding payloads,
    // timestamps, screenshots and queue-only metadata prevents unrelated job
    // polling changes from rebuilding every phone card.
    jobs: (jobs || []).map(deviceJobRenderSnapshot),

    status_unavailable: false,
  };
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
    const [cameraEntries, withdrawEntries, jobResult] = await Promise.all([
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
      Promise.all(
        devices.map(async (device) => {
          try {
            return [device.id, await deviceService.withdraw(device.id)];
          } catch {
            return [device.id, deviceWithdrawDetails?.[device.id] || null];
          }
        }),
      ),
      jobService.list().catch(() => ({ jobs: jobCatalog })),
    ]);
    const remoteResult = await deviceService
      .remoteControl()
      .catch(() => ({ history: remoteControlHistory }));
    const nextCameras = Object.fromEntries(cameraEntries);
    deviceWithdrawDetails = Object.fromEntries(
      withdrawEntries.filter(([, value]) => value),
    );
    jobCatalog = Array.isArray(jobResult.jobs) ? jobResult.jobs : jobCatalog;
    remoteControlHistory = Array.isArray(remoteResult.history)
      ? remoteResult.history
      : remoteControlHistory;
    const nextSignature = JSON.stringify(
      deviceRenderSnapshot(
        nextCatalog,
        nextCameras,
        deviceWithdrawDetails,
        jobCatalog,
      ),
    );
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
    const cardsChanged = nextSignature !== deviceRenderSignature || wasStale;
    if (cardsChanged) {
      deviceRenderSignature = nextSignature;
      renderDevices();
    }
    if (cardsChanged || previousPiState !== JSON.stringify(state?.pi || null)) {
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

window.refreshDeviceReadiness = () => loadDevices();

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

  await openManualAssistance(jobId);
}

const cameraSourceNames = new Map();
const cameraStableIds = new Map();

function setCameraSettingsError(message = "") {
  uiErrorText($("camera-settings-error"), message);
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
  cameraSourceNames.clear();
  cameraStableIds.clear();
  for (const camera of cameras || []) {
    values.add(String(camera.source));
    if (camera.name) cameraSourceNames.set(String(camera.source), camera.name);
    if (camera.stable_id)
      cameraStableIds.set(String(camera.source), camera.stable_id);
    const option = document.createElement("option");
    option.value = String(camera.source);
    const cameraName =
      camera.name ||
      deviceUi("camera.named", `Camera ${camera.index ?? camera.source}`, {
        id: camera.index ?? camera.source,
      });
    const availability = camera.available
      ? deviceUi("common.available", "Available")
      : deviceUi("common.unavailable", "Unavailable");
    deviceUiText(
      option,
      "camera.sourceOption",
      `${cameraName} — ${availability}`,
      { name: cameraName, status: availability },
    );
    options.push(option);
  }
  if (
    current !== null &&
    current !== undefined &&
    !values.has(String(current))
  ) {
    const option = document.createElement("option");
    option.value = String(current);
    deviceUiText(
      option,
      "camera.savedUnavailable",
      `Saved source ${current} — Unavailable`,
      { source: current },
    );
    options.unshift(option);
  }
  list.replaceChildren(...options);
  input.value = String(current ?? options[0]?.value ?? "");
}

async function openCameraSettings(device) {
  cameraSettingsDeviceId = device.id;
  const details = deviceCameraDetails?.[device.id] || {};
  const config = details.config || {};
  deviceUiText(
    $("camera-settings-title"),
    "camera.settingsNamed",
    `Camera Settings · ${device.name || device.id}`,
    { name: device.name || device.id },
  );
  $("camera-settings-rotation").value = String(config.rotation ?? 90);
  $("camera-settings-enabled").checked = config.enabled === true;
  $("camera-settings-status").textContent = cameraState(details).label;
  const configuredSource = config.camera_id || config.camera_source;
  populateCameraSources([], configuredSource);
  $("camera-settings-dialog").hidden = false;
  setCameraSettingsError();
  await refreshCameraSources(true, device.id, configuredSource);
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
    deviceUiText($("camera-settings-status"), "common.available", "Available");
    applyCameraPreviewGeometry(
      $("camera-test-preview"),
      result.width,
      result.height,
    );
    $("camera-test-image").src = result.preview || "";
    $("camera-test-image").hidden = !result.preview;
    $("camera-test-placeholder").hidden = !!result.preview;
  } catch (error) {
    deviceUiText(
      $("camera-settings-status"),
      "common.unavailable",
      "Unavailable",
    );
    applyCameraPreviewGeometry($("camera-test-preview"));
    $("camera-test-image").hidden = true;
    $("camera-test-placeholder").hidden = false;
    uiLiteralText($("camera-test-placeholder"), "Camera test failed");
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
    const selectedSource = $("camera-settings-source").value;
    const settings = {
      camera_source: cameraSourceValue(selectedSource),
      rotation: Number($("camera-settings-rotation").value),
      enabled: $("camera-settings-enabled").checked,
    };
    if (cameraSourceNames.has(selectedSource))
      settings.display_name = cameraSourceNames.get(selectedSource);
    if (cameraStableIds.has(selectedSource))
      settings.camera_id = cameraStableIds.get(selectedSource);
    const result = await deviceService.updateCamera(deviceId, settings);
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
      if (selectedCameraChanged) uiLiteralText($("calibrate"), "Recalibrate");
    } else {
      notice(
        deviceUi("camera.savedFor", `Camera settings saved for ${deviceId}.`, {
          deviceId,
        }),
      );
    }
  } catch (error) {
    try {
      const actual = await deviceService.camera(deviceId);
      deviceCameraDetails = {
        ...(deviceCameraDetails || {}),
        [deviceId]: actual,
      };
      if (cameraSettingsDeviceId === deviceId && actual?.config) {
        const config = actual.config;
        populateCameraSources([], config.camera_id || config.camera_source);
        $("camera-settings-rotation").value = String(config.rotation ?? 90);
        $("camera-settings-enabled").checked = config.enabled === true;
        $("camera-settings-status").textContent = cameraState(actual).label;
      }
    } catch {
      // Keep the original failure visible if authoritative state cannot load.
    }
    setCameraSettingsError(error.message);
  } finally {
    button.disabled = false;
    test.disabled = false;
  }
}

function clearPreview(message = "Waiting for camera access…") {
  bitmap?.close();
  bitmap = null;
  bitmapCoordinateSpace = "camera_normalized";
  previewCalibrationRequired = false;
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
      deviceUi(
        "device.deleteConfirm",
        `Delete ${device.id}? This will unregister and unpair this phone from the Raspberry Pi.`,
        { deviceId: device.id },
      ),
    )
  )
    return;
  deviceDeletesInFlight.add(device.id);
  button.disabled = true;
  deviceUiText(button, "common.deleting", "Deleting…");
  try {
    const result = await deviceService.remove(device.id);
    if (Array.isArray(result.devices)) deviceCatalog = result;
    if (deviceCameraDetails) delete deviceCameraDetails[device.id];
    notice(
      deviceUi(
        "device.deleted",
        `Deleted ${device.id} from the Raspberry Pi.`,
        { deviceId: device.id },
      ),
    );
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
  uiLiteralText(
    $("keyboard-state"),
    enabled
      ? busy
        ? "Keyboard control paused · command in progress"
        : "Keyboard control active"
      : "Keyboard control inactive",
  );
  $("keyboard-state").classList.toggle("active", enabled && !busy);
  canvas.classList.toggle("keyboard-active", enabled);
  uiLiteralText(
    $("keyboard-help"),
    enabled
      ? "Type on your laptop to control the phone. Press Esc to release keyboard control."
      : "Click or focus the phone view to enable keyboard control.",
  );
}

function render() {
  renderControllerTag();
  const piMode = state?.manual_backend === "raspberry_pi";
  const selectedDevice = selectedPiDevice();
  $("devices").hidden = !piMode;
  $("jobs").hidden = !piMode;
  if (piMode && selectedDevice)
    deviceUiText(
      $("serial"),
      "device.controllingNamed",
      `Controlling: ${selectedDevice.name || selectedDevice.id}`,
      { name: selectedDevice.name || selectedDevice.id },
    );
  else {
    const serialLabel =
      state?.device?.label ||
      state?.device?.serial ||
      (piMode ? "Bluetooth phone unavailable" : "No authorized USB phone");
    uiLiteralText($("serial"), serialLabel);
  }
  $("pi-connection").hidden = !piMode;
  uiLiteralText(
    $("pi-api-state"),
    deviceStatusStale
      ? "Status unavailable"
      : connectionState === "reconnecting"
        ? "Pi Reconnecting..."
        : state?.pi?.api === "online"
          ? "Online"
          : "Pi Offline",
  );
  uiLiteralText(
    $("pi-phone-state"),
    deviceStatusStale
      ? "Status unavailable"
      : state?.pi?.phone === "connected"
        ? "Connected"
        : state?.pi?.api === "online"
          ? "Phone Waiting"
          : "Unknown",
  );

  const d = state?.device;

  $("resolution").textContent = d
    ? d.normalized
      ? uiLiteral("Camera-calibrated coordinates")
      : deviceUi(
          "camera.rotation",
          `${d.width} × ${d.height} · rotation ${d.rotation * 90}°`,
          {
            name: `${d.width} × ${d.height}`,
            rotation: d.rotation * 90,
          },
        )
    : "—";

  $("camera-state").textContent = state?.camera?.ready
    ? deviceUi(
        "camera.current",
        `Camera ${state.camera.index} · ${state.camera.width} × ${state.camera.height}`,
        {
          index: state.camera.index,
          width: state.camera.width,
          height: state.camera.height,
        },
      )
    : uiLiteral("Unavailable / starting");

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

  uiLiteralText(
    $("frame-state"),
    fresh() ? "● Live camera" : "Camera feed unavailable",
  );

  uiLiteralText(
    $("mode"),
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
            : "Disabled",
  );

  $("mode").classList.toggle("active", active());
  $("connection-banner").hidden = connectionState === "connected";
  uiLiteralText(
    $("connection-state"),
    connectionState === "reconnecting" ? "Reconnecting..." : "Offline",
  );
  renderKeyboard();

  const automation = state?.control_status === "automation";
  $("takeover").hidden = !automation && !state?.pending;
  $("takeover").disabled = busy || !!state?.pending || !state?.device;
  uiLiteralText(
    $("control-help"),
    automation
      ? dashboardMode === "operate"
        ? "Phone controls are unavailable while automation owns the phone."
        : "Manual control disabled. Camera monitoring remains available. Use Take Over to interrupt automation."
      : state?.pending
        ? "Takeover requested. Manual input stays disabled until automation yields."
        : dashboardMode === "operate"
          ? active()
            ? "Click the phone preview to tap or drag to swipe."
            : "Start manual control to use the phone preview, Back, and Home."
          : "Enable control when you are ready. Each click sends one tap to the phone.",
  );

  $("enable").hidden = dashboardMode === "operate" && active();
  if (state?.control_status === "remote") {
    deviceUiText($("mode"), "remote.mode", "Remote control");
    deviceUiText(
      $("control-help"),
      "remote.help",
      "A remote user controls this phone. Use End Remote Control on its card to take it back.",
    );
  }

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
    ? `${corners.length}/4 ${uiLiteral("selected")}`
    : uiLiteral(state?.calibrated ? "Saved" : "Required");

  $("undo").disabled = !calibrating || !corners.length || busy;

  $("save").disabled = !calibrating || corners.length !== 4 || busy || !fresh();
  document.querySelectorAll("[data-action]").forEach((button) => {
    const supported =
      !state?.supported_actions ||
      state.supported_actions.includes(button.dataset.action);
    button.disabled = !active() || busy || physicalBusy || !supported;
    button.title = supported
      ? ""
      : uiLiteral("This action is not provided by the Pi API.");
  });

  document.querySelectorAll("#corner-list li").forEach((item, i) => {
    item.classList.toggle("done", calibrating && i < corners.length);
    item.classList.toggle("next", calibrating && i === corners.length);
  });

  canvas.classList.toggle("interactive", calibrating || active());

  uiLiteralText(
    $("instruction"),
    calibrating
      ? corners.length < 4
        ? deviceUi(
            "camera.clickCorner",
            `Click the phone display’s ${names[corners.length]} corner.`,
            {
              corner: deviceUi(
                `camera.corner.${names[corners.length]}`,
                names[corners.length],
              ),
            },
          )
        : "Check the outline, then save calibration."
      : active()
        ? dashboardMode === "operate"
          ? "Click to tap or drag to swipe inside the outlined display."
          : "Click inside the outlined display to send one tap."
        : dashboardMode === "operate"
          ? "Start manual control when you are ready."
          : "Input disabled. Calibrate, then enable Manual Control.",
  );
}

function activePreviewCrop() {
  if (
    bitmapCoordinateSpace !== "camera_normalized" ||
    previewCalibrationRequired ||
    calibrating ||
    !bitmap ||
    !state?.calibrated ||
    !Array.isArray(state?.corners) ||
    state.corners.length !== 4
  ) {
    return null;
  }

  const xs = state.corners.map((point) => point[0]);
  const ys = state.corners.map((point) => point[1]);

  const left = Math.max(0, Math.min(...xs));
  const top = Math.max(0, Math.min(...ys));
  const right = Math.min(1, Math.max(...xs));
  const bottom = Math.min(1, Math.max(...ys));

  const width = right - left;
  const height = bottom - top;

  if (width <= 0 || height <= 0) return null;

  return {
    left,
    top,
    right,
    bottom,
    width,
    height,
  };
}

function previewPointToCamera(point) {
  const crop = activePreviewCrop();

  if (!crop) return point;

  return [crop.left + point[0] * crop.width, crop.top + point[1] * crop.height];
}

window.phonePreviewToCameraPoint = previewPointToCamera;
window.phonePreviewCoordinateSpace = () => bitmapCoordinateSpace;

function draw() {
  if (!bitmap) return;

  const crop = activePreviewCrop();

  // Normal mode: show only the selected calibrated phone.
  if (crop) {
    const sx = Math.max(0, Math.floor(crop.left * bitmap.width));
    const sy = Math.max(0, Math.floor(crop.top * bitmap.height));
    const ex = Math.min(bitmap.width, Math.ceil(crop.right * bitmap.width));
    const ey = Math.min(bitmap.height, Math.ceil(crop.bottom * bitmap.height));

    const sourceWidth = Math.max(1, ex - sx);
    const sourceHeight = Math.max(1, ey - sy);

    if (canvas.width !== sourceWidth || canvas.height !== sourceHeight) {
      canvas.width = sourceWidth;
      canvas.height = sourceHeight;
    }

    ctx.drawImage(
      bitmap,
      sx,
      sy,
      sourceWidth,
      sourceHeight,
      0,
      0,
      canvas.width,
      canvas.height,
    );

    return;
  }

  // Calibration / uncalibrated mode: show the whole physical camera.
  if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
  }

  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);

  if (!calibrating || !corners.length) return;

  ctx.save();
  ctx.lineWidth = Math.max(2, canvas.width / 500);
  ctx.strokeStyle = "#6ef0ab";
  ctx.fillStyle = "rgba(61, 208, 128, .10)";
  ctx.beginPath();

  corners.forEach(([x, y], i) => {
    if (i === 0) {
      ctx.moveTo(x * canvas.width, y * canvas.height);
    } else {
      ctx.lineTo(x * canvas.width, y * canvas.height);
    }
  });

  if (corners.length === 4) {
    ctx.closePath();
    ctx.fill();
  }

  ctx.stroke();

  corners.forEach(([x, y], i) => {
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
        new Error(
          uiBackendError(
            pi.error_code,
            pi.error_params,
            pi.message || "Pi temporarily unavailable.",
          ),
        ),
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
    const restoredNoticeRevision = notice("Connection restored.");
    setTimeout(() => {
      if (noticeRevision === restoredNoticeRevision) notice("");
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
        new Error(
          uiBackendError(
            state.pi.error_code,
            state.pi.error_params,
            state.pi.message || "Pi temporarily unavailable.",
          ),
        ),
      );
    if (!state.owned && !state.pending_owned) manual = false;
    const statusMessage = uiBackendError(
      state.message_error_code,
      state.message_error_params,
      state.message,
    );
    if (!state.camera.ready || !state.device) notice(statusMessage, true);
    else if (state.message !== previousMessage) notice(statusMessage);
    render();
    draw();
  } catch (error) {
    if (version !== controlVersion) return;
    if (!error.transient) {
      manual = false;
      notice(
        deviceUi(
          "error.backendUnavailable",
          `Backend unavailable: ${error.message}`,
          { message: uiError(error.message) },
        ),
        true,
      );
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
    let requestedCoordinateSpace =
      requestedDevice && !calibrating && !previewCalibrationRequired
        ? "screen_normalized"
        : "camera_normalized";
    let framePath = requestedDevice
      ? `/api/devices/${encodeURIComponent(requestedDevice)}/camera/${
          requestedCoordinateSpace === "camera_normalized" ? "frame" : "preview"
        }`
      : "/api/frame";
    const previewStarted = performance.now();
    let response = await fetch(framePath, {
      cache: "no-store",
      signal: AbortSignal.timeout(4000),
    });

    if (!response.ok) {
      const status = response.status;
      const errorBody = await response.json();
      const error = uiBackendError(
        errorBody.error_code,
        errorBody.error_params,
        errorBody.error || "Camera unavailable",
      );
      if (status === 409 && requestedDevice && !calibrating) {
        // The calibrated endpoint fails closed. Fetch raw explicitly so the
        // operator can enter calibration mode, while retaining camera-space
        // interaction semantics for this frame.
        requestedCoordinateSpace = "camera_normalized";
        framePath = `/api/devices/${encodeURIComponent(requestedDevice)}/camera/frame`;
        response = await fetch(framePath, {
          cache: "no-store",
          signal: AbortSignal.timeout(4000),
        });
        if (!response.ok) {
          const fallbackBody = await response.json();
          throw new Error(
            uiBackendError(
              fallbackBody.error_code,
              fallbackBody.error_params,
              fallbackBody.error || "Camera unavailable",
            ),
          );
        }
        previewCalibrationRequired = true;
        notice(error, true);
      } else {
        throw new Error(error);
      }
    } else if (requestedCoordinateSpace === "screen_normalized") {
      previewCalibrationRequired = false;
    }
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
      requestedCoordinateSpace !==
      (previewCalibrationRequired ||
      !deviceCatalog?.selected_device ||
      calibrating
        ? "camera_normalized"
        : "screen_normalized")
    ) {
      next.close();
      succeeded = true;
      return;
    }

    if (
      bitmap &&
      bitmapCoordinateSpace === requestedCoordinateSpace &&
      (next.width !== bitmap.width || next.height !== bitmap.height)
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

    const previousBitmap = bitmap;

    bitmap = next;
    bitmapCoordinateSpace = requestedCoordinateSpace;

    if (frameId !== nextId) {
      frameAt = performance.now();
      frameId = nextId;
    }

    $("video-placeholder").classList.add("hidden");

    draw();

    // Only close the old frame AFTER the new frame has been drawn.
    previousBitmap?.close();

    if (previewConsecutiveFailures >= 5) {
      const params = {};
      const recoveredDevice = deviceCatalog?.selected_device;
      if (recoveredDevice) params.device_id = recoveredDevice;
      reportFrontendDiagnostic("frontend_preview_recovered", params);
    }
    previewConsecutiveFailures = 0;

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
    previewConsecutiveFailures++;

    if (previewConsecutiveFailures === 5) {
      const params = { consecutive_failures: previewConsecutiveFailures };
      const failedDevice = deviceCatalog?.selected_device;
      if (failedDevice) params.device_id = failedDevice;
      reportFrontendDiagnostic("frontend_preview_unavailable", params);
    }

    // If we already have a good frame, keep showing it.
    // One dropped frame should NOT flash the placeholder.
    if (bitmap && previewConsecutiveFailures < 5) {
      console.debug(
        `Camera frame missed (${previewConsecutiveFailures}/5):`,
        error.message,
      );
    } else {
      frameAt = 0;

      const wasManual = manual;
      manual = false;

      if (wasManual) {
        api("/api/control", { enabled: false }).catch(() => {});
      }

      $("video-placeholder").textContent = uiError(error.message);
      $("video-placeholder").classList.remove("hidden");

      render();
    }
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
  let recoveredDeviceId = null;
  try {
    if (readyVerificationRestorePromise) await readyVerificationRestorePromise;
    if (manual) await disable();
    const verified = jobCatalog.find((job) => job.job_id === jobId);
    if (verified?.recovery_status !== "READY") return;
    recoveredDeviceId = verified.device_id;

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
    if (recoveredDeviceId) returnToDeviceCard(recoveredDeviceId);
    else renderDevices();
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
    previewCalibrationRequired = true;
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
    previewCalibrationRequired = false;
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
        error.message = `${error.message} ${uiLiteral(
          "Action result uncertain; it was not retried automatically.",
        )}`;
      throw error;
    },
  );
  window.phoneRecorder?.trackManual?.(operation);
  return operation;
}

async function sendAction(
  action,
  point,
  coordinateSpace = bitmapCoordinateSpace,
) {
  if (window.phoneRecorder?.capturesPreview()) {
    return window.phoneRecorder.action(action, point, coordinateSpace);
  }
  if (!active() || busy || physicalBusy) return;

  await report(async () => {
    const data = { action, frame_id: frameId };
    if (point) [data.x, data.y] = point;
    if (point) data.coordinate_space = coordinateSpace;

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

async function sendPreviewSwipe(
  start,
  end,
  durationMs,
  coordinateSpace = bitmapCoordinateSpace,
) {
  if (!active() || busy || physicalBusy) return;
  await report(async () => {
    try {
      await manualApi("/api/action", {
        action: "swipe",
        start,
        end,
        duration_ms: durationMs,
        frame_id: frameId,
        coordinate_space: coordinateSpace,
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
  if (gesture.type === "swipe") {
    const coordinateSpace = bitmapCoordinateSpace;
    const start =
      coordinateSpace === "screen_normalized"
        ? gesture.start
        : previewPointToCamera(gesture.start);
    const end =
      coordinateSpace === "screen_normalized"
        ? gesture.end
        : previewPointToCamera(gesture.end);
    sendPreviewSwipe(start, end, gesture.duration_ms, coordinateSpace);
  } else {
    const coordinateSpace = bitmapCoordinateSpace;
    sendAction(
      "tap",
      coordinateSpace === "screen_normalized"
        ? gesture.end
        : previewPointToCamera(gesture.end),
      coordinateSpace,
    );
  }
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
  } else {
    const coordinateSpace = bitmapCoordinateSpace;
    sendAction(
      "tap",
      coordinateSpace === "screen_normalized"
        ? point
        : previewPointToCamera(point),
      coordinateSpace,
    );
  }
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

$("dashboard-operate").addEventListener("click", () => {
  void switchDashboardMode("operate");
});

$("dashboard-admin").addEventListener("click", () => {
  void switchDashboardMode("admin");
});

window.addEventListener?.("phonecontroller:languagechange", () => {
  renderControllerTag();
  renderDevices({ presentationOnly: true });
  renderOperationalRecovery();
  render();
});

$("diagnostics-refresh").addEventListener(
  "click",
  () => void loadDiagnostics(),
);
$("diagnostics-filter").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    void loadDiagnostics();
  }
});
$("diagnostics-copy").addEventListener(
  "click",
  () => void copyDiagnostics().catch((error) => notice(error.message, true)),
);
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

  void openManualAssistance(issue?.job?.job_id || null);
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
      beginReconnect(
        new Error(
          uiBackendError(
            pi.error_code,
            pi.error_params,
            pi.message || "Pi temporarily unavailable.",
          ),
        ),
      );
  } catch (error) {
    if (!error.transient)
      notice(
        deviceUi(
          "error.backendUnavailable",
          `Backend unavailable: ${error.message}`,
          { message: uiError(error.message) },
        ),
        true,
      );
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
$("workflow-settings-form").addEventListener("submit", saveWorkflowSettings);
$("workflow-settings-search").addEventListener(
  "input",
  renderWorkflowSettingsOptions,
);
$("workflow-settings-balance-search").addEventListener(
  "input",
  renderBalanceWorkflowOptions,
);
$("workflow-settings-cancel").addEventListener("click", closeWorkflowSettings);
$("workflow-settings-close").addEventListener("click", closeWorkflowSettings);
$("workflow-settings-dialog").addEventListener("click", (event) => {
  if (event.target === $("workflow-settings-dialog")) closeWorkflowSettings();
});
$("withdraw-settings-form").addEventListener("submit", saveWithdrawSettings);
$("withdraw-settings-cancel").addEventListener("click", closeWithdrawSettings);
$("withdraw-settings-close").addEventListener("click", closeWithdrawSettings);
$("withdraw-settings-dialog").addEventListener("click", (event) => {
  if (event.target === $("withdraw-settings-dialog")) closeWithdrawSettings();
});
$("withdraw-cancel-form").addEventListener("submit", confirmWithdrawCancel);
$("withdraw-cancel-reason").addEventListener(
  "change",
  updateWithdrawCancelConfirm,
);
$("withdraw-cancel-back").addEventListener("click", closeWithdrawCancel);
$("withdraw-cancel-close").addEventListener("click", closeWithdrawCancel);
$("withdraw-cancel-dialog").addEventListener("click", (event) => {
  if (event.target === $("withdraw-cancel-dialog")) closeWithdrawCancel();
});
$("withdraw-cancel-final-back").addEventListener("click", () =>
  closeWithdrawCancelFinal({ back: true }),
);
$("withdraw-cancel-final-confirm").addEventListener(
  "click",
  confirmWithdrawCancelFinal,
);
$("withdraw-cancel-final-dialog").addEventListener("click", (event) => {
  if (event.target === $("withdraw-cancel-final-dialog"))
    closeWithdrawCancelFinal({ back: true });
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
render();
loadStatus();
videoLoop();
