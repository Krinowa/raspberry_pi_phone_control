"use strict";

(() => {
  let recording = { active: false, workflow: null },
    selected = null;
  let backend = "appium",
    running = null,
    working = false,
    gesture = null;
  let editingId = null,
    timelineSignature = "",
    stopping = false;
  let textBuffer = "",
    textTimer = null,
    captureQueue = Promise.resolve(),
    queued = 0;
  const preview = $("workflow-preview"),
    paint = preview.getContext("2d");
  const terminal = new Set(["passed", "failed", "cancelled"]);
  const show = (message, error = false) => {
    $("workflow-notice").textContent = message;
    $("workflow-notice").classList.toggle("error", error);
  };
  async function task(fn) {
    if (working) return;
    working = true;
    renderRecording();
    try {
      await fn();
    } catch (error) {
      show(error.message, true);
    } finally {
      working = false;
      renderRecording();
    }
  }
  function describe(step, index = 0) {
    const suffix = step.label || `Step ${index + 1}`;
    if (step.type === "tap") return `Tap — ${suffix}`;
    if (step.type === "swipe") {
      const dx = step.end[0] - step.start[0],
        dy = step.end[1] - step.start[1];
      return `Swipe ${Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? "Right" : "Left") : dy > 0 ? "Down" : "Up"}${step.label ? " — " + step.label : ""}`;
    }
    if (step.type === "input") {
      const variable = recording.workflow?.variables.find(
        (v) => v.name === step.variable,
      );
      return `Text Input — ${variable?.label || suffix}`;
    }
    if (step.type === "wait") return `Wait ${step.seconds} seconds`;
    if (step.type === "key")
      return (
        step.label ||
        `Press ${step.key.charAt(0).toUpperCase()}${step.key.slice(1).toLowerCase().replaceAll("_", " ")}`
      );
    return `Manual checkpoint${step.label ? " — " + step.label : ""}`;
  }
  function button(label, fn) {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = label;
    element.addEventListener("click", () => task(fn));
    return element;
  }
  function renderTimeline() {
    const draft = recording.workflow;
    const signature = JSON.stringify(draft);
    if (signature === timelineSignature) return;
    timelineSignature = signature;
    $("step-editor").hidden = true;
    editingId = null;
    if (!draft) {
      $("record-steps").replaceChildren();
      return;
    }
    $("flow-name").value = draft.name;
    $("flow-financial").checked = !!draft.financial;
    const positions = [new Option("At the beginning", "0")];
    const rows = draft.steps.map((step, index) => {
      const row = document.createElement("li"),
        title = document.createElement("strong");
      title.textContent = describe(step, index);
      row.append(title);
      if (step.preview) {
        const image = document.createElement("img");
        image.src = "data:image/png;base64," + step.preview;
        image.alt = "Target preview before the tap";
        image.width = 96;
        image.height = 96;
        row.append(image);
      }
      const actions = document.createElement("div");
      actions.className = "flow-buttons";
      actions.append(button("Rename", () => openEditor(step)));
      if (step.type === "input") {
        const value = document.createElement("p");
        value.textContent =
          step.source === "fixed"
            ? "Recorded value: " + step.value
            : "Ask for: " +
              (draft.variables.find((v) => v.name === step.variable)?.label ||
                "Input");
        row.append(value);
        actions.append(
          button("Keep Fixed", () => openEditor(step, "fixed")),
          button("Make Variable", () => openEditor(step, "variable")),
          button("Manual", () =>
            review({ operation: "input", step_id: step.id, source: "manual" }),
          ),
        );
      }
      actions.append(
        button("Delete", () =>
          review({ operation: "delete", step_id: step.id }),
        ),
      );
      if (index > 0)
        actions.append(
          button("Move Up", () =>
            review({ operation: "move", step_id: step.id, index: index - 1 }),
          ),
        );
      if (index < draft.steps.length - 1)
        actions.append(
          button("Move Down", () =>
            review({ operation: "move", step_id: step.id, index: index + 1 }),
          ),
        );
      row.append(actions);
      positions.push(
        new Option(
          `After ${index + 1}. ${describe(step, index)}`,
          String(index + 1),
        ),
      );
      return row;
    });
    $("record-steps").replaceChildren(...rows);
    $("insert-position").replaceChildren(...positions);
    $("insert-position").value = String(draft.steps.length);
    $("workflow-debug-data").textContent = JSON.stringify(
      draft,
      (key, value) =>
        ["value", "default", "preview"].includes(key) ? "(hidden)" : value,
      2,
    );
  }
  function renderRecording() {
    const draft = recording.workflow,
      live = recording.active;
    $("record-new").hidden = !!draft;
    $("record-new").disabled =
      working || (running && !terminal.has(running.status));
    if (draft) $("record-ready").hidden = true;
    const controlSlot =
      live && backend !== "mock"
        ? $("camera-record-controls")
        : $("record-controls-slot");
    if ($("record-live").parentElement !== controlSlot)
      controlSlot.append($("record-live"));
    $("record-live").hidden = !live;
    $("review-flow").hidden = !draft || live;
    $("saved-flow-panel").hidden = !!draft;
    $("workflow-debug").hidden = live;
    $("record-status").textContent = stopping
      ? "Finishing recording…"
      : "● Recording";
    $("record-count").textContent =
      `Actions recorded: ${draft?.steps.length || 0}`;
    $("record-start").disabled = working || !!draft;
    $("record-stop").disabled = stopping;
    $("record-undo").disabled =
      stopping || !draft?.steps.length || queued > 0 || !!textBuffer;
    $("record-save").disabled = working || !draft?.steps.length || live;
    $("flow-run").disabled =
      working ||
      !selected ||
      !!draft ||
      (running && !terminal.has(running.status));
    $("mock-recording-preview").hidden = backend !== "mock" || !live;
    preview.hidden = backend !== "mock" && !running;
    if (!live) renderTimeline();
  }
  async function review(data) {
    recording = await api("/api/recorder/review", data);
    show("Flow updated.");
    renderRecording();
  }
  function sourceFields() {
    const source = $("input-source").value;
    $("fixed-fields").hidden = source !== "fixed";
    $("variable-fields").hidden = source !== "variable";
    $("manual-input-help").hidden = source !== "manual";
  }
  function openEditor(step, source) {
    editingId = step.id;
    $("step-editor").hidden = false;
    $("step-editor-title").textContent = describe(
      step,
      recording.workflow.steps.indexOf(step),
    );
    $("step-name").value = step.label || "";
    $("step-input-options").hidden = step.type !== "input";
    const definition = recording.workflow.variables.find(
      (v) => v.name === step.variable,
    );
    $("input-source").value = source || step.source || "fixed";
    $("input-value").value = step.value ?? definition?.default ?? "";
    $("variable-name").value = definition?.name || "";
    $("variable-label").value = definition?.label || step.label || "";
    $("variable-required").checked = definition?.required ?? true;
    $("variable-default").value = definition?.default ?? "";
    $("variable-ask").checked = definition?.ask_when_running ?? true;
    sourceFields();
    $("step-editor").scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  }
  $("input-source").addEventListener("change", sourceFields);

  $("step-close").addEventListener("click", () => {
    editingId = null;
    $("step-editor").hidden = true;
  });

  $("step-editor").addEventListener("submit", (event) => {
    event.preventDefault();
    // Snapshot edits before the first response replaces the timeline/editor.
    const step_id = editingId,
      label = $("step-name").value;
    const old = recording.workflow.steps.find((s) => s.id === step_id);
    const source = $("input-source").value;
    const input = { operation: "input", step_id, source };
    if (source === "fixed") input.value = $("input-value").value;
    if (source === "variable")
      input.variable = {
        name: $("variable-name").value,
        label: $("variable-label").value,
        type: "text",
        required: $("variable-required").checked,
        default: $("variable-default").value || null,
        ask_when_running: $("variable-ask").checked,
      };

    task(async () => {
      if (old.type === "input") await review(input);
      await review({ operation: "rename_step", step_id, label });
      $("input-value").value = "";
      $("variable-default").value = "";
    });
  });

  $("flow-rename").addEventListener("click", () =>
    task(() =>
      review({ operation: "rename_flow", name: $("flow-name").value }),
    ),
  );

  $("flow-financial").addEventListener("change", () =>
    task(() =>
      review({
        operation: "rename_flow",
        name: $("flow-name").value,
        financial: $("flow-financial").checked,
      }),
    ),
  );

  $("record-wait").addEventListener("click", () =>
    task(() =>
      review({
        operation: "insert",
        index: Number($("insert-position").value),
        step: { type: "wait", seconds: Number($("record-wait-seconds").value) },
      }),
    ),
  );

  $("record-checkpoint").addEventListener("click", () =>
    task(() =>
      review({
        operation: "insert",
        index: Number($("insert-position").value),
        step: {
          type: "manual_checkpoint",
          message: $("checkpoint-message").value,
        },
      }),
    ),
  );

  async function capture(step, extra) {
    try {
      recording = await api("/api/recorder/step", {
        step,
        capture: true,
        frame_id: frameId,
        ...extra,
      });
      if (recording.notice) show(recording.notice);
      else show("");
    } catch (error) {
      textBuffer = "";
      clearTimeout(textTimer);
      // Stop after any uncertain command. Pending commands are discarded, never retried.
      recording.active = false;

      if (backend !== "mock") disable().catch(() => {});
      try {
        await api("/api/recorder/stop", {});
        recording = await api("/api/recorder", undefined, "GET");
      } catch {
        /* Keep the local input gate closed if the server is unavailable. */
      }

      recording.active = false;
      show(
        error.message +
          " Recording stopped; inspect the phone before continuing.",
        true,
      );
    }
    renderRecording();
  }
  function enqueue(step, extra = {}) {
    if (!recording.active) return captureQueue;
    if (queued >= 32) {
      textBuffer = "";
      clearTimeout(textTimer);
      recording.active = false;

      if (backend !== "mock") disable().catch(() => {});
      captureQueue = captureQueue.then(async () => {
        try {
          recording = await api("/api/recorder/stop", {});
        } catch {
          /* Keep input closed. */
        }
        recording.active = false;
        renderRecording();
      });

      show(
        "The phone could not keep up. Recording stopped; review the captured steps.",
        true,
      );
      return captureQueue;
    }
    queued++;
    captureQueue = captureQueue.then(async () => {
      try {
        if (recording.active) await capture(step, extra);
      } finally {
        queued--;
        renderRecording();
      }
    });
    return captureQueue;
  }

  function flushText() {
    clearTimeout(textTimer);
    if (textBuffer) {
      const value = textBuffer;
      textBuffer = "";
      enqueue({ type: "input", source: "fixed", value });
    }
    return captureQueue;
  }

  function addText(value) {
    if (textBuffer.length + value.length > 4096) {
      show("Text is too long for one input.", true);
      return;
    }
    textBuffer += value;
    clearTimeout(textTimer);
    textTimer = setTimeout(flushText, 400);
  }

  function naturalAction(action, point) {
    if (!recording.active || stopping) return;
    flushText();

    if (action === "tap")
      return enqueue({ type: "tap" }, { camera_start: point });

    const swipes = {
      swipe_up: [0.5, 0.75, 0.5, 0.25],
      swipe_down: [0.5, 0.25, 0.5, 0.75],
      swipe_left: [0.75, 0.5, 0.25, 0.5],
      swipe_right: [0.25, 0.5, 0.75, 0.5],
    };

    if (swipes[action])
      return enqueue({
        type: "swipe",
        start: swipes[action].slice(0, 2),
        end: swipes[action].slice(2),
        duration_ms: 300,
      });
    return enqueue({ type: "key", key: action.toUpperCase() });
  }

  $("record-new").addEventListener("click", () => {
    $("record-ready").hidden = false;
  });

  $("record-start").addEventListener("click", () =>
    task(async () => {
      if (backend !== "mock") {
        const result = await api("/api/control", {
          enabled: true,
          frame_id: frameId,
        });
        applyControlResult(result);
      }

      try {
        recording = await api("/api/recorder/start", {
          name: "New Recording",
          execute: true,
        });
      } catch (error) {
        if (backend !== "mock") await disable();
        throw error;
      }

      timelineSignature = "";
      textBuffer = "";
      stopping = false;
      show("Use the phone preview as usual. Stop when you’re ready to review.");
      const livePreview = backend === "mock" ? preview : canvas;
      livePreview.focus({ preventScroll: true });
      livePreview.scrollIntoView?.({ block: "center", behavior: "smooth" });
    }),
  );

  $("record-stop").addEventListener("click", () =>
    task(async () => {
      stopping = true;
      renderRecording();
      try {
        await flushText();
        recording = await api("/api/recorder/stop", {});
        if (backend !== "mock") await disable();
        show("Review your recording, then save the flow.");
        renderRecording();
        $("review-flow").scrollIntoView?.({
          block: "start",
          behavior: "smooth",
        });
      } finally {
        stopping = false;
      }
    }),
  );

  $("record-undo").addEventListener("click", () =>
    task(async () => {
      await flushText();
      recording = await api("/api/recorder/undo", {});
      show("Last recorded step removed. The phone itself was not changed.");
    }),
  );

  $("record-cancel").addEventListener("click", () =>
    task(async () => {
      recording = await api("/api/recorder/cancel", {});
      show("Recording discarded.");
    }),
  );

  $("record-save").addEventListener("click", () =>
    task(async () => {
      await review({
        operation: "rename_flow",
        name: $("flow-name").value,
        financial: $("flow-financial").checked,
      });
      const saved = await api("/api/recorder/save", {});
      recording = { active: false, workflow: null };
      show("Flow saved.");
      await loadFlows();
      await selectFlow(saved.id);
    }),
  );

  function localTarget(event) {
    return (
      event.target?.isContentEditable ||
      event.target?.closest?.(
        "input, textarea, select, button, a, [role='button']",
      )
    );
  }
  document.addEventListener("keydown", (event) => {
    if (
      !recording.active ||
      stopping ||
      event.defaultPrevented ||
      localTarget(event) ||
      document.hidden ||
      !document.hasFocus()
    )
      return;
    if (
      event.ctrlKey ||
      event.metaKey ||
      event.altKey ||
      event.isComposing ||
      event.keyCode === 229 ||
      event.getModifierState?.("AltGraph")
    )
      return;
    if (backend !== "mock" && (!active() || !fresh())) return;
    if (event.key.length === 1 && event.key >= " " && event.key <= "~") {
      event.preventDefault();
      addText(event.key);
    } else if (
      !event.shiftKey &&
      ["Enter", "Backspace", "Tab"].includes(event.key)
    ) {
      event.preventDefault();
      if (event.key === "Backspace" && textBuffer)
        textBuffer = textBuffer.slice(0, -1);
      else {
        flushText();
        enqueue({ type: "key", key: event.key.toUpperCase() });
      }
    }
  });
  document.addEventListener("paste", (event) => {
    if (
      !recording.active ||
      stopping ||
      localTarget(event) ||
      document.hidden ||
      !document.hasFocus()
    )
      return;
    if (backend !== "mock" && (!active() || !fresh())) return;
    const value = event.clipboardData?.getData("text/plain") || "";
    event.preventDefault();
    if (!/^[ -~]*$/.test(value))
      return show(
        "This phone input currently supports printable English characters only.",
        true,
      );
    addText(value);
  });

  function location(event, element) {
    const box = element.getBoundingClientRect();
    return [
      (event.clientX - box.left) / box.width,
      (event.clientY - box.top) / box.height,
    ];
  }

  function installGestures(element, camera) {
    element.addEventListener("pointerdown", (event) => {
      if (
        !recording.active ||
        stopping ||
        event.button !== 0 ||
        !event.isPrimary ||
        calibrating
      )
        return;
      if (camera && (!fresh() || !state?.calibrated))
        return show("Calibrate a live camera view before recording.", true);
      if (!camera && backend !== "mock") return;
      event.preventDefault();
      element.focus({ preventScroll: true });
      element.setPointerCapture(event.pointerId);
      gesture = {
        id: event.pointerId,
        start: location(event, element),
        at: performance.now(),
        element,
      };
    });
    element.addEventListener("pointerup", (event) => {
      if (
        !gesture ||
        gesture.id !== event.pointerId ||
        gesture.element !== element
      )
        return;
      const saved = gesture;
      gesture = null;

      if (!recording.active || stopping || calibrating) return;
      if (camera && !fresh())
        return show("The camera frame expired; gesture discarded.", true);

      const end = location(event, element);
      if ([...saved.start, ...end].some((v) => v < 0 || v > 1))
        return show("Keep the gesture inside the preview.", true);
      const box = element.getBoundingClientRect();
      const swipe =
        Math.hypot(
          (end[0] - saved.start[0]) * box.width,
          (end[1] - saved.start[1]) * box.height,
        ) > 8;
      const duration_ms = Math.max(
        50,
        Math.min(60000, Math.round(performance.now() - saved.at)),
      );
      const step = swipe ? { type: "swipe", duration_ms } : { type: "tap" };
      const extra = camera
        ? {
            camera_start: saved.start,
            ...(swipe ? { camera_end: end } : {}),
            frame_id: frameId,
          }
        : {};
      if (!camera)
        Object.assign(
          step,
          swipe ? { start: saved.start, end } : { x: end[0], y: end[1] },
        );
      flushText();
      enqueue(step, extra);
    });

    for (const name of ["pointercancel", "lostpointercapture"])
      element.addEventListener(name, () => {
        gesture = null;
      });
  }

  installGestures(canvas, true);
  installGestures(preview, false);
  let recorderOwner = clientId;
  try {
    recorderOwner =
      window.sessionStorage?.getItem("phone-recorder-owner") || clientId;
    window.sessionStorage?.setItem("phone-recorder-owner", recorderOwner);
  } catch {
    /* Recording works without session storage. */
  }

  window.phoneRecorder = {
    capturesPreview: () => recording.active,
    ownerId: recorderOwner,
    action: naturalAction,
  };

  async function loadFlows() {
    const result = await api("/api/workflows", undefined, "GET");
    backend = result.input_backend;

    $("workflow-backend").textContent =
      `Input: ${backend} · Screen: ${result.screen_backend}`;

    const rows = result.workflows.map((flow) => {
      const row = document.createElement("li"),
        label = document.createElement("span");

      label.textContent = `${flow.name} · ${flow.step_count} steps · ${flow.variable_count} variables · Created ${flow.created_at ? new Date(flow.created_at).toLocaleString() : "—"} · Updated ${flow.updated_at ? new Date(flow.updated_at).toLocaleString() : "—"}`;

      const select = document.createElement("button");
      select.textContent = "Run…";
      select.addEventListener("click", () => task(() => selectFlow(flow.id)));

      const remove = document.createElement("button");
      remove.textContent = "Delete";
      remove.addEventListener("click", () =>
        task(async () => {
          if (!window.confirm(`Delete workflow “${flow.name}”?`)) return;
          await api("/api/workflows/" + flow.id, {}, "DELETE");
          if (selected?.id === flow.id) {
            selected = null;
            $("run-variables").replaceChildren();
            $("run-title").textContent = "Run Flow";
          }
          await loadFlows();
        }),
      );

      row.append(label, select, remove);
      return row;
    });
    $("saved-flows").replaceChildren(...rows);
  }

  async function selectFlow(id) {
    selected = await api("/api/workflows/" + id, undefined, "GET");
    $("run-title").textContent = "Run: " + selected.name;
    const fields = selected.variables
      .filter((variable) => variable.ask_when_running !== false)
      .map((variable) => {
        const label = document.createElement("label"),
          input = document.createElement("input");
        label.textContent = variable.label;
        input.name = variable.name;
        input.type = variable.type === "number" ? "number" : "text";
        if (input.type === "number") input.step = "any";
        input.required = variable.required;
        input.value = variable.default ?? "";
        input.autocomplete = "off";
        label.append(input);
        return label;
      });

    $("run-variables").replaceChildren(...fields);
    renderRecording();
  }

  $("flows-refresh").addEventListener("click", () => task(loadFlows));
  $("flow-run-form").addEventListener("submit", (event) => {
    event.preventDefault();
    task(async () => {
      const variables = {};
      for (const definition of selected.variables) {
        if (definition.ask_when_running === false) continue;
        const field = $("flow-run-form").elements.namedItem(definition.name);
        variables[definition.name] =
          field.value === ""
            ? null
            : definition.type === "number"
              ? Number(field.value)
              : field.value;
      }
      running = await api(`/api/workflows/${selected.id}/run`, { variables });
      for (const field of $("run-variables").querySelectorAll("input"))
        field.value = "";
      manual = false;
      render();
      renderRun();
      localStorage.setItem("phone-workflow-run", running.run_id);
    });
  });

  for (const op of ["cancel", "resume"])
    $("flow-" + op).addEventListener("click", () =>
      task(async () => {
        running = await api(
          `/api/workflow-runs/${running.run_id}/${op}`,
          op === "resume" ? { step_index: running.step_index } : {},
        );
        if (op === "resume") {
          manual = false;
          render();
        }
        renderRun();
      }),
    );

  function drawPreview(step) {
    paint.fillStyle = "#142b24";
    paint.fillRect(0, 0, preview.width, preview.height);
    paint.strokeStyle = "#6ef0ab";
    paint.fillStyle = "#e0ffed";
    paint.lineWidth = 4;
    paint.font = "14px sans-serif";
    paint.fillText("Phone preview", 16, 28);
    if (!step) return;
    if (step.type === "tap") {
      paint.beginPath();
      paint.arc(
        step.x * preview.width,
        step.y * preview.height,
        12,
        0,
        Math.PI * 2,
      );
      paint.stroke();
    } else if (step.type === "swipe") {
      const [x1, y1] = [
        step.start[0] * preview.width,
        step.start[1] * preview.height,
      ];
      const [x2, y2] = [
        step.end[0] * preview.width,
        step.end[1] * preview.height,
      ];
      const angle = Math.atan2(y2 - y1, x2 - x1);
      paint.beginPath();
      paint.moveTo(x1, y1);
      paint.lineTo(x2, y2);
      paint.moveTo(
        x2 - 14 * Math.cos(angle - 0.5),
        y2 - 14 * Math.sin(angle - 0.5),
      );
      paint.lineTo(x2, y2);
      paint.lineTo(
        x2 - 14 * Math.cos(angle + 0.5),
        y2 - 14 * Math.sin(angle + 0.5),
      );
      paint.stroke();
    } else
      paint.fillText(
        step.label || step.type.toUpperCase(),
        16,
        70,
        preview.width - 32,
      );
  }

  function renderRun() {
    if (!running) return;
    $("flow-run-state").textContent =
      `${running.status} · Step ${running.step_index || 0}/${running.step_count || 0} · ${running.preview ? describe(running.preview, (running.step_index || 1) - 1) : "Starting"}${running.checkpoint_message ? " — " + running.checkpoint_message : ""}${terminal.has(running.status) ? " — " + (running.message || "") : ""}`;
    $("flow-resume").hidden = running.status !== "waiting_manual";
    $("flow-cancel").disabled = terminal.has(running.status);
    $("workflow-logs").textContent = (running.logs || []).join("\n");
    drawPreview(running.preview);
    renderRecording();
  }

  async function pollRun() {
    try {
      if (running && !terminal.has(running.status)) {
        running = await api(
          `/api/workflow-runs/${running.run_id}`,
          undefined,
          "GET",
        );
        renderRun();
      }
    } catch (error) {
      show(error.message, true);
    } finally {
      setTimeout(pollRun, 500);
    }
  }

  task(async () => {
    await loadFlows();
    recording = await api("/api/recorder", undefined, "GET");
    const id = localStorage.getItem("phone-workflow-run");
    if (id) {
      try {
        running = await api(`/api/workflow-runs/${id}`, undefined, "GET");
        renderRun();
      } catch {
        localStorage.removeItem("phone-workflow-run");
      }
    }
    drawPreview(running?.preview);
  });
  pollRun();
})();
