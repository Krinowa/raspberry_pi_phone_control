"use strict";

const $ = (id) => document.getElementById(id);
const canvas = $("video");
const ctx = canvas.getContext("2d");
const clientId = crypto.randomUUID();
const token = document.querySelector('meta[name="control-token"]').content;
const names = ["top-left", "top-right", "bottom-right", "bottom-left"];
let state = null,
  frameId = null,
  frameAt = 0,
  bitmap = null;
let calibrating = false,
  corners = [],
  manual = false,
  busy = false;
let controlVersion = 0;

function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").classList.toggle("error", error);
}

async function api(path, data, method = "POST") {
  const response = await fetch(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-Control-Token": token,
      "X-Control-Client": clientId,
    },
    body: method === "GET" ? undefined : JSON.stringify(data ?? {}),
    cache: "no-store",
    signal: AbortSignal.timeout(12000),
  });

  const body = await response.json();

  if (!response.ok)
    throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function fresh() {
  return frameId !== null && performance.now() - frameAt < 1800;
}
function active() {
  return (
    manual &&
    state?.owned &&
    fresh() &&
    !calibrating &&
    state?.control_status !== "automation" &&
    !state?.pending
  );
}

function keyboardActive() {
  return (
    active() &&
    state?.enabled &&
    state?.calibrated &&
    !!state?.device &&
    state?.camera?.ready &&
    !document.hidden &&
    document.hasFocus()
  );
}

function renderKeyboard() {
  const enabled = keyboardActive();
  $("keyboard-state").textContent = enabled
    ? busy
      ? "Keyboard control paused · command in progress"
      : "Keyboard control active"
    : "Keyboard control inactive";
  $("keyboard-state").classList.toggle("active", enabled && !busy);
}

function render() {
  $("serial").textContent = state?.device?.serial || "No authorized USB phone";

  const d = state?.device;

  $("resolution").textContent = d
    ? `${d.width} × ${d.height} · rotation ${d.rotation * 90}°`
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
    button.disabled = !active() || busy;
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

async function refreshStatus() {
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
    if (!state.owned && !state.pending_owned) manual = false;
    if (!state.camera.ready || !state.device) notice(state.message, true);
    else if (state.message !== previousMessage) notice(state.message);
    render();
    draw();
  } catch (error) {
    if (version !== controlVersion) return;
    manual = false;
    notice(`Backend unavailable: ${error.message}`, true);
    render();
  } finally {
    setTimeout(refreshStatus, 1800);
  }
}

async function videoLoop() {
  try {
    const response = await fetch("/api/frame", {
      cache: "no-store",
      signal: AbortSignal.timeout(4000),
    });

    if (!response.ok)
      throw new Error((await response.json()).error || "Camera unavailable");
    const nextId = Number(response.headers.get("X-Frame-Id"));
    const next = await createImageBitmap(await response.blob());

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
  } catch (error) {
    frameAt = 0;
    const wasManual = manual;
    manual = false;
    if (wasManual) api("/api/control", { enabled: false }).catch(() => {});
    $("video-placeholder").textContent = error.message;
    $("video-placeholder").classList.remove("hidden");
    render();
  }
  setTimeout(videoLoop, fresh() ? 65 : 600);
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
    const result = await api("/api/control", {
      enabled: true,
      frame_id: frameId,
    });
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

async function sendAction(action, point) {
  if (!active() || busy) return;

  await report(async () => {
    const data = { action, frame_id: frameId };
    if (point) [data.x, data.y] = point;

    try {
      const result = await api("/api/action", data);
      if (result.pixel) {
        $("coordinates").textContent =
          `${result.normalized.map((v) => v.toFixed(3)).join(", ")} → ${result.pixel.join(", ")} px`;
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

document.addEventListener("keydown", (event) => {
  if (
    !keyboardActive() ||
    busy ||
    event.defaultPrevented ||
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
  } else if (
    !event.shiftKey &&
    ["Enter", "Backspace", "Tab"].includes(event.key)
  ) {
    type = "key";
    value = event.key.toLowerCase();
  } else return;

  event.preventDefault();
  // Send immediately using the existing busy gate. No buffer or keyboard history.
  report(async () => {
    try {
      await api("/api/key", { type, value, frame_id: frameId });
    } catch (error) {
      // An uncertain input is never retried, even after a network timeout.
      manual = false;
      api("/api/control", { enabled: false }).catch(() => {});
      throw error;
    }
  });
});

window.addEventListener("focus", renderKeyboard);
window.addEventListener("blur", renderKeyboard);

canvas.addEventListener("click", (event) => {
  if (!fresh() || busy) return;
  canvas.focus({ preventScroll: true });
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
  if (!manual) return;
  if (!fresh() || document.hidden) {
    disable().catch(() => {});
    return;
  }

  const version = controlVersion;
  try {
    const result = await api("/api/heartbeat", { frame_id: frameId });
    if (version !== controlVersion || !manual) return;
    applyControlResult(result);
  } catch (error) {
    if (version !== controlVersion) return;
    manual = false;
    notice(error.message, true);
    render();
  }
}, 1500);

document.addEventListener("visibilitychange", () => {
  if (document.hidden && manual) disable().catch(() => {});
});

window.addEventListener("pagehide", () => {
  // Best effort on close; the server's six-second lease is the fallback.
  if (manual)
    fetch("/api/control", {
      method: "POST",
      keepalive: true,
      headers: {
        "Content-Type": "application/json",
        "X-Control-Token": token,
        "X-Control-Client": clientId,
      },
      body: JSON.stringify({ enabled: false }),
    }).catch(() => {});
});

render();
refreshStatus();
videoLoop();
