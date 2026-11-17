"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const appSource = readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const workflowSource = readFileSync(
  path.join(__dirname, "../static/workflows.js"),
  "utf8",
);
const template = readFileSync(
  path.join(__dirname, "../templates/index.html"),
  "utf8",
);

function modeHarness() {
  const nodes = new Map();
  const start = appSource.indexOf('let dashboardMode = "operate"');
  const end = appSource.indexOf("function recordPreviewTiming", start);
  const context = vm.createContext({
    document: { body: { dataset: {} } },
    window: {},
    $: (id) => {
      if (!nodes.has(id)) nodes.set(id, { ariaSelected: "" });
      return nodes.get(id);
    },
  });
  vm.runInContext(
    appSource.slice(start, end) +
      "\nglobalThis.changeMode = setDashboardMode;" +
      " globalThis.currentMode = () => dashboardMode;",
    context,
  );
  return { context, nodes };
}

test("Operate is default and Admin switching only changes presentation state", () => {
  const { context, nodes } = modeHarness();
  const selectedDevice = { id: "phone_B" };
  const activeJob = { id: "job_1", status: "RUNNING" };
  const typedAdminValue = "camera-2";
  const poller = { timer: 17, listeners: 4 };

  context.changeMode("operate");
  assert.equal(context.currentMode(), "operate");
  assert.equal(context.document.body.dataset.dashboardMode, "operate");
  context.changeMode("admin");
  assert.equal(context.document.body.dataset.dashboardMode, "admin");
  assert.equal(nodes.get("dashboard-admin").ariaSelected, "true");
  assert.deepEqual(selectedDevice, { id: "phone_B" });
  assert.deepEqual(activeJob, { id: "job_1", status: "RUNNING" });
  assert.equal(typedAdminValue, "camera-2");
  assert.deepEqual(poller, { timer: 17, listeners: 4 });
  assert.doesNotMatch(
    appSource.slice(
      appSource.indexOf("function setDashboardMode"),
      appSource.indexOf("function recordPreviewTiming"),
    ),
    /setTimeout|setInterval|addEventListener|refreshStatus|loadDevices/,
  );
});

test("Admin mode opens directly without local login or logout UI", () => {
  assert.match(template, /id="dashboard-admin"[\s\S]*?>\s*Admin\s*<\/button>/);
  assert.doesNotMatch(template, /Admin 🔒|admin-login|admin-logout|>Logout</);
  assert.doesNotMatch(
    appSource,
    /adminSession|api\/auth|api\/admin\/(?:login|logout|setup)/,
  );
  assert.match(
    appSource,
    /\$\("dashboard-admin"\)\.addEventListener\("click", \(\) =>[\s\S]*?switchDashboardMode\("admin"\)/,
  );
  assert.doesNotMatch(appSource, /localStorage/);
});

test("dashboard has one DOM, unique IDs, and correct mode ownership", () => {
  assert.match(template, /data-dashboard-mode="operate"/);
  assert.match(template, /id="record-card" class="card admin-only"/);
  assert.match(template, /id="ocr-settings" class="card ocr-settings-card admin-only"/);
  assert.match(template, /id="camera-settings-dialog"[\s\S]*?class="modal-backdrop admin-only"/);
  assert.match(
    template,
    /id="withdraw-settings-dialog"[\s\S]*?class="modal-backdrop admin-only"/,
  );
  for (const id of [
    "withdraw-settings-username",
    "withdraw-settings-password",
    "withdraw-settings-totp",
    "withdraw-settings-submit",
  ])
    assert.match(template, new RegExp(`id="${id}"`));
  assert.doesNotMatch(
    template,
    /account-settings-dialog|Payment Routing|Incoming Account ID/,
  );
  assert.match(
    template,
    /id="jobs"[\s\S]*?class="card jobs-card admin-only developer-only"/,
  );
  assert.match(
    template,
    /id="diagnostics"[\s\S]*?class="card diagnostics-card admin-only developer-only"/,
  );
  assert.match(template, /id="diagnostics-output"[^>]*Recent application logs/);
  assert.match(template, /id="flow-run-form"/);
  assert.match(
    template,
    /id="workflow-debug" class="admin-only developer-only"/,
  );

  const ids = [...template.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(new Set(ids).size, ids.length);
  assert.equal((appSource.match(/dashboard-operate.*addEventListener/g) || []).length, 1);
  assert.equal((appSource.match(/dashboard-admin.*addEventListener/g) || []).length, 1);
});

test("client-facing Operator and Admin hide developer history and diagnostics", () => {
  const css = readFileSync(
    path.join(__dirname, "../static/style.css"),
    "utf8",
  );
  assert.match(css, /body\[data-dashboard-mode="operate"\] \.admin-only/);
  assert.match(template, /id="jobs"[\s\S]*?developer-only/);
  assert.match(
    template,
    /id="workflow-debug" class="admin-only developer-only"/,
  );
  assert.match(template, /id="diagnostics"[\s\S]*?developer-only/);
  assert.match(css, /\.developer-only\s*\{\s*display: none !important/);
  assert.match(
    css,
    /data-dashboard-mode="admin"\]\[data-developer-tools="enabled"\][\s\S]*?\.developer-only[\s\S]*?display: block !important/,
  );
  assert.match(
    template,
    /data-developer-tools="\{\{ 'enabled' if developer_mode else 'disabled' \}\}"/,
  );
  assert.doesNotMatch(template, /Developer Mode|developer-mode|Enable Developer/i);
  assert.match(template, /meta name="preview-debug"/);
  assert.match(appSource, /if \(!previewDebug\) return;/);
  assert.match(appSource, /\/api\/diagnostics\/logs\$\{query\}/);
  assert.match(appSource, /output\.textContent = diagnosticsLogs\.length/);
});

test("workflow Resume remains restricted to manual checkpoints", () => {
  assert.match(
    workflowSource,
    /\$\("flow-resume"\)\.hidden = running\.status !== "waiting_manual"/,
  );
  assert.doesNotMatch(template, />\s*(?:Continue|Resume failure)\s*</i);
});

test("Operate hides raw controls until guided recovery exposes shared Manual Assistance", () => {
  const css = readFileSync(
    path.join(__dirname, "../static/style.css"),
    "utf8",
  );
  assert.match(template, /id="operate-resolve-issue"[\s\S]*?Resolve Issue/);
  assert.match(template, /id="operate-recover-phone"[\s\S]*?Recover Phone/);
  assert.match(template, /id="operate-open-assistance"[^>]*hidden/);
  assert.match(template, /id="manual-control-card" class="card manual-control-surface"/);
  assert.match(template, /Resolve Phone Issue/);
  assert.match(appSource, /\? "Start Manual Control"/);
  assert.match(
    appSource,
    /async function enableManualControl\(\)[\s\S]*?api\("\/api\/control", \{[\s\S]*?enabled: true/,
  );
  assert.match(
    appSource,
    /\$\("enable"\)\.addEventListener\("click", \(\) => report\(enableManualControl\)\)/,
  );
  assert.match(
    template,
    /Use the phone preview to return the phone to the expected starting[\s\S]*?screen\./,
  );
  assert.match(
    template,
    /id="manual-assistance-finish"[\s\S]*?id="operate-verify-finish"[\s\S]*?Verify Phone Ready/,
  );
  assert.doesNotMatch(
    template,
    /operate-only manual-assistance-finish|manual-assistance-finish operate-only/,
  );
  assert.match(template, /data-action="back"/);
  assert.match(template, /data-action="home"/);
  for (const action of [
    "recents",
    "swipe_up",
    "swipe_down",
    "swipe_left",
    "swipe_right",
    "screen_on",
    "screen_off",
  ])
    assert.match(
      template,
      new RegExp(`class="admin-only" data-action="${action}"`),
    );
  assert.match(template, /id="keyboard-state"[\s\S]*?class="badge admin-only"/);
  assert.match(template, /id="disable" class="stop admin-only"/);
  assert.match(template, /id="takeover" class="stop admin-only"/);
  assert.match(
    css,
    /body\[data-dashboard-mode="operate"\] \.control-workspace\s*\{\s*display: none/,
  );
  assert.match(
    css,
    /data-manual-assistance="open"[\s\S]*?\.control-workspace\s*\{\s*display: grid/,
  );
  assert.match(
    appSource,
    /\$\("operate-open-assistance"\)\.hidden = !issue\?\.manualAvailable/,
  );
  assert.doesNotMatch(template, /Try Recovery|Recheck Status/);
});

test("recovery and ready verification use job APIs without retrying business work", () => {
  const start = appSource.indexOf("async function startPhoneRecovery");
  const end = appSource.indexOf("function setAccountSettingsError", start);
  const recoverySource = appSource.slice(start, end);
  const verifyStart = appSource.indexOf("async function verifyPhoneReady");
  const verifyEnd = appSource.indexOf(
    "async function closeManualAssistance",
    verifyStart,
  );
  const verifySource = appSource.slice(verifyStart, verifyEnd);
  assert.match(recoverySource, /jobService\.recover\(jobId\)/);
  assert.match(
    verifySource,
    /if \(!job\?\.recovery_workflow_id\)[\s\S]*?jobService\.markReady\(job\.job_id\)[\s\S]*?jobService\.verifyReady\(job\.job_id\)/,
  );
  assert.doesNotMatch(verifySource, /dashboardMode|admin/);
  assert.ok(
    recoverySource.indexOf("await disable()") <
      recoverySource.indexOf("jobService.verifyReady(job.job_id)"),
  );
  assert.match(recoverySource, /readyVerificationJobId = job\.job_id/);
  assert.match(
    appSource,
    /Expected starting screen was not detected\./,
  );
  assert.doesNotMatch(
    recoverySource,
    /flow-resume|\/run["'`]|start workflow|retry.*workflow/i,
  );
  assert.match(
    template,
    /Verify Phone Ready[\s\S]*?never resumes or retries the failed workflow/i,
  );
  assert.match(workflowSource, /phoneRuntimeStateChanged\?\.\(running\)/);
});

test("Verify Phone Ready is disabled only while a recovery request is active", () => {
  const renderStart = appSource.indexOf("function renderOperationalRecovery");
  const renderEnd = appSource.indexOf("async function startPhoneRecovery", renderStart);
  const renderSource = appSource.slice(renderStart, renderEnd);

  assert.match(
    renderSource,
    /\$\("operate-verify-finish"\)\.disabled =\s*recoveryRequestInFlight \|\|\s*readyVerificationRestoreInFlight \|\|\s*readyVerificationFinishInFlight;/,
  );
  assert.doesNotMatch(renderSource, /dashboardMode/);
});

test("phone cards use shared state-driven job actions", () => {
  assert.match(
    appSource,
    /function availableJobActions\(job\)[\s\S]*?manual_assistance[\s\S]*?retry_failed_step[\s\S]*?restart/,
  );
  assert.match(
    appSource,
    /retryExternalJob\(job, "failed_step"\)/,
  );
  assert.match(appSource, /retryExternalJob\(job, "restart"\)/);
  assert.match(appSource, /Retry from Failed Step/);
  assert.match(appSource, /Retry queued from the selected failed step\./);
  assert.match(
    appSource,
    /function returnToDeviceCard\(deviceId\)[\s\S]*?renderDevices\(\)[\s\S]*?scrollIntoView[\s\S]*?void loadDevices\(\)/,
  );
  assert.match(
    appSource,
    /api\(`\/api\/jobs\/\$\{encodeURIComponent\(jobId\)\}\/retry`, \{ mode \}\)/,
  );
  const markReadyStart = appSource.indexOf("async function verifyPhoneReady");
  const markReadyEnd = appSource.indexOf(
    "async function closeManualAssistance",
    markReadyStart,
  );
  assert.doesNotMatch(
    appSource.slice(markReadyStart, markReadyEnd),
    /retryExternalJob|jobService\.retry/,
  );
});

test("workflow editor exposes the structured irreversible payment boundary", () => {
  assert.match(template, /id="step-critical"[\s\S]*?Irreversible payment submission/);
  assert.match(
    workflowSource,
    /operation: "set_critical"[\s\S]*?critical: \$\("step-critical"\)\.checked/,
  );
});

test("Operate and recorder use the same preview gesture classifier", () => {
  const gestures = readFileSync(
    path.join(__dirname, "../static/phone_gestures.js"),
    "utf8",
  );
  assert.match(template, /static', filename='phone_gestures\.js'/);
  assert.match(appSource, /window\.phonePreviewGestures\.classify/);
  assert.match(workflowSource, /window\.phonePreviewGestures\.classify/);
  assert.match(gestures, /SWIPE_THRESHOLD_PX = 8/);
  assert.doesNotMatch(workflowSource, /Math\.hypot/);
});

test("ready verification keeps assistance open on failure and closes it on success", () => {
  const renderStart = appSource.indexOf("function renderOperationalRecovery");
  const renderEnd = appSource.indexOf("async function startPhoneRecovery", renderStart);
  const renderSource = appSource.slice(renderStart, renderEnd);
  assert.match(
    renderSource,
    /recovery_status === "READY"[\s\S]*?manualAssistanceOpen = false;[\s\S]*?recoveryPanelOpen = false/,
  );
  assert.match(
    renderSource,
    /recovery_status === "NEEDS_ATTENTION"[\s\S]*?manualAssistanceOpen = true;[\s\S]*?recoveryPanelOpen = true/,
  );
  assert.match(
    renderSource,
    /Expected starting screen was not detected\./,
  );
  assert.match(
    renderSource,
    /readyVerificationRestoreManual[\s\S]*?restoreManualControlAfterVerificationFailure\(\)/,
  );
});

test("failed ready verification restores only the manual control it stopped", () => {
  const verifyStart = appSource.indexOf("async function verifyPhoneReady");
  const verifyEnd = appSource.indexOf("async function closeManualAssistance", verifyStart);
  const verifySource = appSource.slice(verifyStart, verifyEnd);
  const restoreStart = appSource.indexOf(
    "async function restoreManualControlAfterVerificationFailure",
  );
  const restoreEnd = appSource.indexOf(
    '$("enable").addEventListener',
    restoreStart,
  );
  const restoreSource = appSource.slice(restoreStart, restoreEnd);

  assert.match(
    verifySource,
    /const restoreManualOnFailure = manual;[\s\S]*?await disable\(\)/,
  );
  assert.match(
    verifySource,
    /readyVerificationRestoreManual = restoreManualOnFailure/,
  );
  assert.match(restoreSource, /!readyVerificationRestoreManual/);
  assert.match(restoreSource, /await enableManualControl\(\)/);
  assert.doesNotMatch(
    restoreSource,
    /flow-resume|\/run["'`]|start workflow|retry.*workflow/i,
  );
  assert.match(template, /id="takeover" class="stop admin-only"/);
  assert.match(template, /id="disable" class="stop admin-only"/);
});
