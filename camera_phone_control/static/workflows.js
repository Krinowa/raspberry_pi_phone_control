"use strict";

(() => {
  let recording = {
      active: false,
      workflow: null,
      editing_existing: false,
    },
    selected = null;
  let backend = "appium",
    running = null,
    working = false,
    gesture = null;
  let editingId = null,
    timelineSignature = "",
    timelineWasLive = false,
    stopping = false;
  let textBuffer = "",
    textTimer = null,
    captureQueue = Promise.resolve(),
    queued = 0;
  const preview = $("workflow-preview"),
    paint = preview.getContext("2d");
  const piManual = () => state?.manual_backend === "raspberry_pi";
  let pendingManual = Promise.resolve();
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
    const suffix = step.label;
    if (step.type === "tap") return `Tap${suffix ? " — " + suffix : ""}`;
    if (step.type === "swipe") {
      const dx = step.end[0] - step.start[0],
        dy = step.end[1] - step.start[1];
      return `Swipe ${Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? "Right" : "Left") : dy > 0 ? "Down" : "Up"}${step.label ? " — " + step.label : ""}`;
    }
    if (step.type === "input") {
      const variable = recording.workflow?.variables.find(
        (v) => v.name === step.variable,
      );
      return `Type Text${variable?.label || suffix ? " — " + (variable?.label || suffix) : ""}`;
    }
    if (step.type === "wait_until_stable")
      return `Wait for Screen to Settle — Stable for ${step.stable_ms} ms · Max ${step.timeout_seconds} sec`;
    if (step.type === "wait_for_text")
      return `Wait for Text — “${step.text}” · Max ${step.timeout_seconds} sec`;
    if (step.type === "wait") return `Wait ${step.seconds} seconds`;
    if (step.type === "key")
      return (
        step.label ||
        {
          HOME: "Home",
          BACK: "Back",
          SCREEN_ON: "Wake Screen",
          SCREEN_OFF: "Screen Off",
          RECENTS: "Recent Apps",
          MOVE_HOME: "Press Home",
          PAGEUP: "Press Page Up",
          PAGEDOWN: "Press Page Down",
        }[step.key.toUpperCase()] ||
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

  let expectedActionId = null,
    expectedMode = null;

  const annotationBusy = () =>
    !!expectedMode || !!recording.expected_text_pending;

  function canExpectText(step) {
    return (
      (step.type === "tap" ||
        step.type === "swipe" ||
        (step.type === "key" &&
          ["HOME", "BACK"].includes(step.key.toUpperCase()))) &&
      !/(?:^|[^a-z])(?:pin|otp)(?:$|[^a-z])|password|passwd|passcode|secret|biometric|approval_code|authoriz|authoris|confirm.*(?:pay|transfer)|final.*(?:pay|transfer)/i.test(
        step.label || "",
      )
    );
  }

  async function openExpectedText(step, wait, mode = "review") {
    expectedActionId = step?.id || null;
    expectedMode = mode;
    $("expected-text-editor").hidden = false;
    $("expected-text-title").textContent = step
      ? `Choose expected text after ${describe(step)}`
      : "Choose expected text for the current screen";
    $("expected-text-value").value = wait?.text || "";
    $("expected-text-timeout").value = wait?.timeout_seconds ?? 10;
    $("expected-suggestions").replaceChildren();
    $("expected-text-status").textContent = "Reading current phone screen…";
    renderRecording();
    render();
    try {
      const targetMode = mode;
      const result = await api(
        "/api/recorder/text-suggestions",
        mode === "current" ? { current: true } : { step_id: step.id },
      );
      if (
        expectedMode !== targetMode ||
        expectedActionId !== (step?.id || null)
      )
        return;
      const choices = (result.suggestions || []).slice(0, 5);
      $("expected-suggestions").replaceChildren(
        ...choices.map((text) => {
          const label = document.createElement("label");
          const radio = document.createElement("input");
          radio.type = "radio";
          radio.name = "expected-suggestion";
          radio.value = text;
          radio.addEventListener("change", () => {
            $("expected-text-value").value = text;
          });
          const caption = document.createElement("span");
          caption.textContent = text;
          label.append(radio, caption);
          return label;
        }),
      );
      $("expected-text-status").textContent = choices.length
        ? "Suggestions from the current screen. Select one or enter text manually."
        : "No reliable text suggestions found. Enter expected text manually.";
    } catch (error) {
      if (expectedMode === mode && expectedActionId === (step?.id || null))
        $("expected-text-status").textContent =
          "No reliable text suggestions found. Enter expected text manually.";
    }
    renderRecording();
  }

  function closeExpectedText() {
    expectedActionId = null;
    expectedMode = null;
    $("expected-text-editor").hidden = true;
    render();
  }

  async function openPendingExpectedText() {
    const stepId = recording.pending_expected_text_step_id;

    if (!recording.active || !recording.expected_text_pending || expectedMode)
      return;

    if (!stepId) {
      await openExpectedText(null, null, "current");
      return;
    }

    const step = recording.workflow?.steps.find((item) => item.id === stepId);

    if (step) await openExpectedText(step, null, "recording");
  }

  $("expected-text-editor").addEventListener("submit", (event) => {
    event.preventDefault();
    task(async () => {
      if (expectedMode === "review")
        await review({
          operation: "expected_text",
          step_id: expectedActionId,
          ...textWaitFields("expected"),
        });
      else
        recording = await api("/api/recorder/expected-text", {
          ...textWaitFields("expected"),
        });
      closeExpectedText();
    });
  });
  $("expected-text-cancel").addEventListener("click", () =>
    task(async () => {
      if (expectedMode && expectedMode !== "review")
        recording = await api("/api/recorder/expected-text/cancel", {});
      closeExpectedText();
    }),
  );
  $("expected-text-manual").addEventListener("click", () => {
    for (const radio of $("expected-suggestions").querySelectorAll("input"))
      radio.checked = false;
    $("expected-text-value").focus();
  });

  function renderTimeline() {
    const draft = recording.workflow;
    const signature = JSON.stringify([draft, recording.active]);

    if (signature === timelineSignature) return;
    timelineSignature = signature;

    const enteringReview = timelineWasLive && !recording.active;
    timelineWasLive = recording.active;

    $("step-editor").hidden = true;
    editingId = null;
    $("record-empty").hidden = !!draft?.steps.length;

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

      if (step.type === "tap") {
        const detail = document.createElement("p");
        detail.textContent = `X ${Math.round(step.x * 100)}% • Y ${Math.round(step.y * 100)}%`;
        row.append(detail);
      }
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
      actions.append(button("Edit", () => openEditor(step)));
      if (step.type === "input") {
        const value = document.createElement("p");
        value.textContent =
          step.source === "fixed"
            ? "“" + step.value + "”"
            : "Ask for: " +
              (draft.variables.find((v) => v.name === step.variable)?.label ||
                "Input");
        row.append(value);
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
      if (!recording.active && canExpectText(step)) {
        const next = draft.steps[index + 1];
        const wait = next?.type === "wait_for_text" ? next : null;
        const expected = document.createElement("p");
        expected.textContent = `Expected text: ${wait?.text || "Not configured"}`;
        row.append(expected);
        actions.append(
          button(wait ? "Change" : "Add Expected Text", () =>
            openExpectedText(step, wait),
          ),
        );
        if (wait)
          actions.append(
            button("Remove", () =>
              review({
                operation: "remove_expected_text",
                step_id: step.id,
              }),
            ),
          );
      }
      if (!recording.active) row.append(actions);
      positions.push(
        new Option(
          `After ${index + 1}. ${describe(step, index)}`,
          String(index + 1),
        ),
      );
      return row;
    });

    $("record-steps").replaceChildren(...rows);
    if (enteringReview) $("record-steps").scrollTop = 0;
    if (recording.active) rows.at(-1)?.scrollIntoView?.({ block: "nearest" });
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
      live = recording.active,
      editingExisting = !!recording.editing_existing;

    $("record-new").hidden = !!draft;
    $("record-guidance").hidden = !!draft;
    $("record-review-link").hidden = !draft || live;

    $("record-card").classList.toggle("is-recording", live);

    $("record-new").disabled =
      working || (running && !terminal.has(running.status));

    if (draft) {
      $("record-ready").hidden = true;
    }

    const controlSlot =
      live && (piManual() || backend !== "mock")
        ? $("camera-record-controls")
        : $("record-controls-slot");

    if ($("record-live").parentElement !== controlSlot) {
      controlSlot.append($("record-live"));
    }

    $("record-live").hidden = !live;

    $("review-flow").hidden = !draft || live;

    $("saved-flow-panel").hidden = !!draft;

    $("workflow-debug").hidden = live;

    const timelineSlot = live
      ? $("live-timeline-slot")
      : $("review-timeline-slot");

    if ($("record-timeline").parentElement !== timelineSlot) {
      timelineSlot.append($("record-timeline"));
    }

    $("record-status").textContent = stopping
      ? "Finishing recording…"
      : `● Recording • ${draft?.steps.length || 0} ${
          draft?.steps.length === 1 ? "step" : "steps"
        }`;

    $("record-count").textContent = "Taps, swipes and typing appear below.";

    $("record-start").disabled = working || !!draft;

    $("record-stop").disabled = stopping;

    $("record-mark-next").textContent = recording.mark_next_screen
      ? "✓ Next Screen Will Be Checked"
      : "Mark Next Screen: OFF";
    $("record-mark-next").disabled = stopping || annotationBusy();
    $("record-expect-current").disabled = stopping || annotationBusy();

    $("record-undo").disabled =
      stopping ||
      annotationBusy() ||
      !draft?.steps.length ||
      queued > 0 ||
      !!textBuffer;

    $("record-save").disabled = working || !draft?.steps.length || live;

    $("record-save").textContent = editingExisting
      ? "Save Changes"
      : "Save Workflow";

    $("record-cancel").textContent = editingExisting
      ? "Cancel Editing"
      : "Discard Recording";

    $("flow-run").disabled =
      working ||
      !selected ||
      !!draft ||
      (running && !terminal.has(running.status));

    $("mock-recording-preview").hidden =
      piManual() || backend !== "mock" || !live;

    preview.hidden = (piManual() || backend !== "mock") && !running;
    const reviewTitle = $("review-flow")?.querySelector("h3");

    if (reviewTitle) {
      reviewTitle.textContent = editingExisting
        ? "Edit Workflow"
        : "Review Recording";
    }

    if (draft && !live && editingExisting) {
      $("record-empty").textContent =
        "This workflow has no steps. Add or edit steps, then save your changes.";
    } else {
      $("record-empty").textContent =
        "No steps recorded yet. Use the phone preview to record taps, swipes, typing and navigation.";
    }

    renderTimeline();
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

    // Show/hide step-specific controls.
    $("step-tap-options").hidden = step.type !== "tap";
    $("step-swipe-options").hidden = step.type !== "swipe";
    $("step-key-options").hidden = step.type !== "key";
    $("step-input-options").hidden = step.type !== "input";
    $("step-wait-options").hidden = step.type !== "wait";
    $("step-stable-options").hidden = step.type !== "wait_until_stable";
    $("step-text-options").hidden = step.type !== "wait_for_text";
    $("step-stable-ms").value = step.stable_ms ?? 700;
    $("step-stable-timeout").value = step.timeout_seconds ?? 10;
    $("step-text-value").value = step.text ?? "";
    $("step-text-timeout").value = step.timeout_seconds ?? 10;

    // Tap
    if (step.type === "tap") {
      $("step-tap-x").value = (step.x * 100).toFixed(1);
      $("step-tap-y").value = (step.y * 100).toFixed(1);
    }

    // Swipe
    if (step.type === "swipe") {
      $("step-swipe-start-x").value = (step.start[0] * 100).toFixed(1);
      $("step-swipe-start-y").value = (step.start[1] * 100).toFixed(1);
      $("step-swipe-end-x").value = (step.end[0] * 100).toFixed(1);
      $("step-swipe-end-y").value = (step.end[1] * 100).toFixed(1);
      $("step-swipe-duration").value = step.duration_ms ?? 300;
    }

    // Key
    if (step.type === "key") {
      $("step-key").value = step.key.toUpperCase();
    }

    // Wait
    $("step-wait-seconds").value = step.seconds ?? 1;

    // Input
    const definition = recording.workflow.variables.find(
      (v) => v.name === step.variable,
    );

    $("input-source").value = source || step.source || "fixed";

    if (
      recording.editing_existing &&
      step.type === "input" &&
      step.source === "fixed"
    ) {
      $("input-value").value = "";
      $("input-value").placeholder = "Leave blank to keep existing value";
    } else {
      $("input-value").value = step.value ?? definition?.default ?? "";
      $("input-value").placeholder = "";
    }

    $("variable-name").value = definition?.name || "";
    $("variable-label").value = definition?.label || step.label || "";
    $("variable-required").checked = definition?.required ?? true;
    $("variable-default").value = definition?.default ?? "";
    $("variable-ask").checked = definition?.ask_when_running ?? true;

    sourceFields();

    $("step-editor").scrollIntoView?.({
      block: "nearest",
      behavior: "smooth",
    });
  }

  $("input-source").addEventListener("change", sourceFields);

  $("step-close").addEventListener("click", () => {
    editingId = null;
    $("step-editor").hidden = true;
  });

  $("step-editor").addEventListener("submit", (event) => {
    event.preventDefault();

    const step_id = editingId;
    const label = $("step-name").value;

    const old = recording.workflow.steps.find((step) => step.id === step_id);

    if (!old) {
      show("Step no longer exists.", true);
      return;
    }

    const seconds = Number($("step-wait-seconds").value);
    const source = $("input-source").value;

    const input = {
      operation: "input",
      step_id,
      source,
    };

    if (source === "fixed") {
      const value = $("input-value").value;

      // When editing an existing saved workflow:
      // blank = preserve the existing server-side value.
      if (!recording.editing_existing || value !== "") {
        input.value = value;
      }
    }

    if (source === "variable") {
      input.variable = {
        name: $("variable-name").value,
        label: $("variable-label").value,
        type: "text",
        required: $("variable-required").checked,
        default: $("variable-default").value || null,
        ask_when_running: $("variable-ask").checked,
      };
    }

    task(async () => {
      if (old.type === "tap") {
        const x = Number($("step-tap-x").value) / 100;
        const y = Number($("step-tap-y").value) / 100;

        if (
          !Number.isFinite(x) ||
          !Number.isFinite(y) ||
          x < 0 ||
          x > 1 ||
          y < 0 ||
          y > 1
        ) {
          throw new Error("Tap coordinates must be between 0% and 100%.");
        }

        await review({
          operation: "fields",
          step_id,
          fields: {
            x,
            y,
          },
        });
      }

      if (old.type === "swipe") {
        const start = [
          Number($("step-swipe-start-x").value) / 100,
          Number($("step-swipe-start-y").value) / 100,
        ];

        const end = [
          Number($("step-swipe-end-x").value) / 100,
          Number($("step-swipe-end-y").value) / 100,
        ];

        const duration_ms = Number($("step-swipe-duration").value);

        if (
          [...start, ...end].some(
            (value) => !Number.isFinite(value) || value < 0 || value > 1,
          )
        ) {
          throw new Error("Swipe coordinates must be between 0% and 100%.");
        }

        if (
          !Number.isFinite(duration_ms) ||
          duration_ms < 50 ||
          duration_ms > 60000
        ) {
          throw new Error("Swipe duration must be between 50 and 60000 ms.");
        }

        await review({
          operation: "fields",
          step_id,
          fields: {
            start,
            end,
            duration_ms,
          },
        });
      }

      if (old.type === "key") {
        await review({
          operation: "fields",
          step_id,
          fields: {
            key: $("step-key").value,
          },
        });
      }

      if (old.type === "input") {
        await review(input);
      }

      if (old.type === "wait_until_stable") {
        await review({
          operation: "fields",
          step_id,
          fields: stabilityFields("step"),
        });
      }

      if (old.type === "wait_for_text") {
        await review({
          operation: "fields",
          step_id,
          fields: textWaitFields("step"),
        });
      }

      if (old.type === "wait") {
        if (!Number.isFinite(seconds) || seconds < 0 || seconds > 3600) {
          throw new Error("Wait duration must be between 0 and 3600 seconds.");
        }

        await review({
          operation: "wait",
          step_id,
          seconds,
        });
      }

      await review({
        operation: "rename_step",
        step_id,
        label,
      });

      $("input-value").value = "";
      $("variable-default").value = "";

      editingId = null;
      $("step-editor").hidden = true;

      show("Step updated.");
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

  function stabilityFields(prefix) {
    const stable_ms = Number($(`${prefix}-stable-ms`).value);
    const timeout_seconds = Number($(`${prefix}-stable-timeout`).value);
    if (!Number.isFinite(stable_ms) || stable_ms < 100 || stable_ms > 10000)
      throw new Error("Stable for must be between 100 and 10000 ms.");
    if (
      !Number.isFinite(timeout_seconds) ||
      timeout_seconds < 1 ||
      timeout_seconds > 300
    )
      throw new Error("Maximum wait must be between 1 and 300 seconds.");
    if (timeout_seconds < stable_ms / 1000)
      throw new Error(
        "Maximum wait must allow the requested stability window.",
      );
    return { stable_ms, timeout_seconds };
  }

  function textWaitFields(prefix) {
    const text = $(`${prefix}-text-value`).value.trim();
    const timeout_seconds = Number($(`${prefix}-text-timeout`).value);
    if (!text || text.length > 500)
      throw new Error("Text to detect must be between 1 and 500 characters.");
    if (
      !Number.isFinite(timeout_seconds) ||
      timeout_seconds < 0.5 ||
      timeout_seconds > 300
    )
      throw new Error("Maximum wait must be between 0.5 and 300 seconds.");
    return { text, timeout_seconds };
  }

  $("record-stable").addEventListener("click", () =>
    task(() =>
      review({
        operation: "insert",
        index: Number($("insert-position").value),
        step: { type: "wait_until_stable", ...stabilityFields("record") },
      }),
    ),
  );

  $("record-text").addEventListener("click", () =>
    task(() =>
      review({
        operation: "insert",
        index: Number($("insert-position").value),
        step: { type: "wait_for_text", ...textWaitFields("record") },
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
      await openPendingExpectedText();
      if (recording.notice) show(recording.notice);
      else show("");
    } catch (error) {
      textBuffer = "";
      clearTimeout(textTimer);
      // Stop after any uncertain command. Pending commands are discarded, never retried.
      recording.active = false;
      closeExpectedText();

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
    if (!recording.active || annotationBusy()) return captureQueue;
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
    if (annotationBusy()) return;
    if (textBuffer.length + value.length > 4096) {
      show("Text is too long for one input.", true);
      return;
    }
    textBuffer += value;
    clearTimeout(textTimer);
    textTimer = setTimeout(flushText, 400);
  }

  function naturalAction(action, point) {
    if (!recording.active || stopping || annotationBusy()) return;
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

  $("record-new").addEventListener("click", () => $("record-start").click());

  $("record-mark-next").addEventListener("click", () =>
    task(async () => {
      await pendingManual;
      await flushText();
      recording = await api("/api/recorder/mark-next-screen", {});
      show(
        recording.mark_next_screen
          ? "The next tap, swipe, Home or Back action will be checked."
          : "Next-screen check cancelled.",
      );
    }),
  );

  $("record-expect-current").addEventListener("click", () =>
    task(async () => {
      await pendingManual;
      await flushText();
      await openExpectedText(null, null, "current");
    }),
  );

  $("record-start").addEventListener("click", () =>
    task(async () => {
      if (piManual() || backend !== "mock") {
        let result;
        try {
          result = await api("/api/control", {
            enabled: true,
            frame_id: frameId,
          });
        } finally {
          if (piManual()) await refreshStatus();
        }
        applyControlResult(result);
      }

      try {
        recording = await api("/api/recorder/start", {
          name: "New Recording",
          execute: true,
        });
      } catch (error) {
        if (piManual() || backend !== "mock") await disable();
        throw error;
      }

      timelineSignature = "";
      textBuffer = "";
      stopping = false;
      show("Use the phone preview as usual. Stop when you’re ready to review.");
      const livePreview = !piManual() && backend === "mock" ? preview : canvas;
      livePreview.focus({ preventScroll: true });
      livePreview.scrollIntoView?.({ block: "center", behavior: "smooth" });
    }),
  );

  $("record-stop").addEventListener("click", () =>
    task(async () => {
      stopping = true;
      renderRecording();
      try {
        await pendingManual;
        await flushText();
        recording = await api("/api/recorder/stop", {});
        closeExpectedText();
        if (piManual() || backend !== "mock") await disable();
        show(
          "Recording stopped. Review your steps, name your workflow, then save.",
        );
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
      await pendingManual;
      await flushText();
      recording = await api("/api/recorder/undo", {});
      show("Last recorded step removed. The phone itself was not changed.");
    }),
  );

  $("record-cancel").addEventListener("click", () =>
    task(async () => {
      const editingExisting = !!recording.editing_existing;

      recording = await api("/api/recorder/cancel", {});
      closeExpectedText();

      show(editingExisting ? "Changes discarded." : "Recording discarded.");

      await loadFlows();
    }),
  );

  $("record-save").addEventListener("click", () =>
    task(async () => {
      if (!$("flow-name").value.trim()) {
        $("flow-name").focus();
        return show("Enter a workflow name", true);
      }

      const editingExisting = !!recording.editing_existing;

      await review({
        operation: "rename_flow",
        name: $("flow-name").value.trim(),
        financial: $("flow-financial").checked,
      });

      const saved = await api("/api/recorder/save", {});

      recording = {
        active: false,
        workflow: null,
        editing_existing: false,
      };

      show(editingExisting ? "Workflow updated" : "Workflow saved");

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
      piManual() ||
      !recording.active ||
      stopping ||
      annotationBusy() ||
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
      piManual() ||
      !recording.active ||
      stopping ||
      annotationBusy() ||
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
        piManual() ||
        !recording.active ||
        stopping ||
        annotationBusy() ||
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

      if (!recording.active || stopping || annotationBusy() || calibrating)
        return;
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
    capturesPreview: () => recording.active && !piManual() && !annotationBusy(),
    blocksInput: () => recording.active && (stopping || annotationBusy()),
    trackManual: (operation) => {
      if (piManual() && recording.active)
        pendingManual = operation.catch(() => {});
    },
    acceptManual: (snapshot) => {
      if (!snapshot || !piManual()) return;
      recording = snapshot;
      renderRecording();
      openPendingExpectedText().catch((error) => show(error.message, true));
    },
    manualFailed: async (error) => {
      if (!piManual() || !recording.active) return;
      recording.active = false;
      closeExpectedText();
      show(
        error.message +
          " Recording stopped. Input may already have occurred; inspect the phone before continuing. No retry attempted.",
        true,
      );
      renderRecording();
      try {
        await api("/api/recorder/stop", {});
        recording = await api("/api/recorder", undefined, "GET");
      } catch {
        /* Keep the local capture gate closed. */
      }
      recording.active = false;
      show(
        error.message +
          " Recording stopped. Input may already have occurred; inspect the phone before continuing. No retry attempted.",
        true,
      );
      renderRecording();
    },
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

      const edit = document.createElement("button");
      edit.textContent = "Edit";
      edit.addEventListener("click", () =>
        task(async () => {
          recording = await api("/api/recorder/edit", {
            workflow_id: flow.id,
          });

          selected = null;
          timelineSignature = "";
          editingId = null;

          show(`Editing “${recording.workflow.name}”`);

          renderRecording();

          $("review-flow").scrollIntoView?.({
            block: "start",
            behavior: "smooth",
          });
        }),
      );

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

      row.append(label, select, edit, remove);
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
    await openPendingExpectedText();
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
