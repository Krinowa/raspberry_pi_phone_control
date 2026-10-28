"use strict";

(() => {
  const workflowUi = (key, fallback, params) =>
    window.PhoneI18n?.t(key, params) ?? fallback;
  const workflowUiText = (element, key, fallback, params) => {
    if (window.PhoneI18n?.bindText)
      return window.PhoneI18n.bindText(element, key, params);
    element.textContent = fallback;
    return element;
  };
  const workflowUiComputed = (element, compute) => {
    if (window.PhoneI18n?.bindComputedText)
      return window.PhoneI18n.bindComputedText(element, compute);
    element.textContent = compute();
    return element;
  };
  const workflowLiteral = (text) =>
    window.PhoneI18n?.translateEnglish?.(text) ?? text;
  const workflowError = (message) =>
    window.PhoneI18n?.translateError?.(message) ?? message;
  const workflowBackendError = (code, params, fallback) =>
    window.PhoneI18n?.translateBackendError?.(code, params, fallback) ??
    fallback;
  const workflowStatus = (status) =>
    window.PhoneI18n?.translateStatus?.(status) ?? status;
  const workflowLiteralText = (element, text) =>
    workflowUiComputed(element, () => workflowLiteral(text));
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
  let availableFlows = [];
  let flowLoadGeneration = 0;
  let editingId = null,
    timelineSignature = "",
    timelineWasLive = false,
    stopping = false;
  let externalPhysicalBusy = false,
    reconnectRefreshInFlight = false;
  let runPollTimer = null,
    runPollInFlight = false,
    workflowPollingStopped = false,
    runStateSignature = "";
  let textBuffer = "",
    textTimer = null,
    captureQueue = Promise.resolve(),
    queued = 0;
  const preview = $("workflow-preview"),
    paint = preview.getContext("2d");
  const piManual = () => state?.manual_backend === "raspberry_pi";
  const previewCoordinateSpace = () =>
    window.phonePreviewCoordinateSpace?.() || "camera_normalized";
  const cameraPoint = (point) =>
    previewCoordinateSpace() === "screen_normalized"
      ? point
      : (window.phonePreviewToCameraPoint?.(point) ?? point);
  let pendingManual = Promise.resolve();
  const terminal = new Set(["passed", "failed", "cancelled"]);
  const show = (message, error = false) => {
    const node = $("workflow-notice");
    if (window.PhoneI18n?.bindComputedText)
      window.PhoneI18n.bindComputedText(
        node,
        () => window.PhoneI18n.translateError?.(message) ?? message,
      );
    else node.textContent = message;
    node.classList.toggle("error", error);
  };

  // Recording and review use one control pane so the phone surface remains
  // beside the timeline, temporary setup panels, and step editor.
  $("workflow-authoring-slot").append(
    $("review-flow"),
    $("expected-text-editor"),
  );

  async function task(fn) {
    if (working) return;
    working = true;
    renderRecording();
    try {
      await fn();
    } catch (error) {
      if (error?.transient || Number(error?.status) >= 500)
        window.phoneReportDiagnostic?.("frontend_workflow_command_failed", {
          operation: "workflow_command",
          ...(error?.errorCode ? { error_code: error.errorCode } : {}),
          ...(error?.status ? { status: error.status } : {}),
        });
      show(error.message, true);
    } finally {
      working = false;
      renderRecording();
    }
  }

  function describe(step, index = 0) {
    const suffix = step.label;
    if (step.type === "tap")
      return `${workflowUi("workflow.step.tap", "Tap")}${suffix ? " — " + suffix : ""}`;

    if (step.type === "swipe") {
      const dx = step.end[0] - step.start[0],
        dy = step.end[1] - step.start[1];
      const directionKey =
        Math.abs(dx) > Math.abs(dy)
          ? dx > 0
            ? "right"
            : "left"
          : dy > 0
            ? "down"
            : "up";
      const direction = workflowUi(
        `workflow.direction.${directionKey}`,
        directionKey.charAt(0).toUpperCase() + directionKey.slice(1),
      );
      return `${workflowUi("workflow.step.swipe", `Swipe ${direction}`, { direction })}${step.label ? " — " + step.label : ""}`;
    }

    if (step.type === "input") {
      const variable = recording.workflow?.variables.find(
        (v) => v.name === step.variable,
      );
      const format = workflowLiteral(
        step.variable === "amount"
          ? {
              implied_cents: "Implied cents",
              whole_number: "Whole amount only",
            }[step.amount_format]
          : null,
      );
      return `${workflowUi("workflow.step.typeText", "Type Text")}${variable?.label || suffix ? " — " + (variable?.label || suffix) : ""}${format ? " · " + format : ""}`;
    }

    if (step.type === "wait_until_stable")
      return workflowUi(
        "workflow.step.waitStable",
        `Wait for Screen to Settle — Stable for ${step.stable_ms} ms · Max ${step.timeout_seconds} sec`,
        { stable: step.stable_ms, seconds: step.timeout_seconds },
      );

    if (step.type === "wait_for_text")
      return workflowUi(
        "workflow.step.waitText",
        `Wait for Text — “${step.text}” · Wait up to ${step.timeout_seconds} sec`,
        { text: step.text, seconds: step.timeout_seconds },
      );

    if (step.type === "verify_text") {
      return workflowUi(
        "workflow.step.verify",
        `Verify Text — ${verifyFieldConfig(step.variable)?.label || "Configured field"}`,
        {
          field:
            workflowLiteral(verifyFieldConfig(step.variable)?.label) ||
            workflowLiteral("Configured field"),
        },
      );
    }

    if (step.type === "if_text")
      return workflowUi(
        "workflow.step.specialCase",
        `Special Case — “${step.text}”`,
        { text: step.text },
      );
    if (step.type === "ocr_value")
      return workflowUi(
        "workflow.step.readBalance",
        `Read Balance — ${step.label || step.output}`,
        { label: step.label || step.output },
      );

    if (step.type === "capture_screen") {
      const label = typeof suffix === "string" ? suffix.trim() : "";
      return `${workflowUi("workflow.step.screenshot", "Capture Screenshot")}${label && !["screenshot", "receipt screenshot"].includes(label.toLocaleLowerCase()) ? " — " + label : ""}`;
    }

    if (step.type === "wait")
      return workflowUi("workflow.step.wait", `Wait ${step.seconds} seconds`, {
        seconds: step.seconds,
      });

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
    return `${workflowUi("workflow.step.checkpoint", "Manual checkpoint")}${step.label ? " — " + step.label : ""}`;
  }

  function allWorkflowSteps(steps = recording.workflow?.steps || []) {
    const result = [];
    for (const step of steps) {
      result.push(step);
      if (step.type === "if_text") {
        result.push(...allWorkflowSteps(step.then_steps));
        result.push(...allWorkflowSteps(step.else_steps));
      }
    }
    return result;
  }

  function findWorkflowStep(stepId) {
    return allWorkflowSteps().find((step) => step.id === stepId);
  }

  function setSectionVisible(id, visible) {
    const section = $(id);

    section.hidden = !visible;

    section
      .querySelectorAll("input, select, textarea, button")
      .forEach((control) => {
        control.disabled = !visible;
      });
  }

  function button(label, fn) {
    const element = document.createElement("button");
    element.type = "button";
    workflowLiteralText(element, label);
    element.addEventListener("click", () => task(fn));
    return element;
  }

  function scrollToSavedFlows() {
    requestAnimationFrame(() => {
      $("saved-flow-panel")?.scrollIntoView({
        block: "start",
        behavior: "smooth",
      });
    });
  }

  let expectedActionId = null,
    expectedMode = null,
    expectedOriginalROI = null,
    keyboardSetupOpen = false,
    variableInputOpen = false,
    readValueOpen = false,
    liveWaitOpen = false,
    liveVerifyOpen = false,
    addStepType = "wait",
    returnToVariableInput = false,
    alternateSetupOpen = false,
    specialCaseAfterStepId = null,
    enabledManualForReviewSpecialCase = false;

  const annotationBusy = () =>
    !!expectedMode ||
    !!recording.expected_text_pending ||
    keyboardSetupOpen ||
    !!recording.keyboard_capture ||
    variableInputOpen ||
    readValueOpen ||
    liveWaitOpen ||
    liveVerifyOpen ||
    alternateSetupOpen;

  function keyboardOptions(select, captureOption = false) {
    const keyboards = Object.values(recording.workflow?.keyboards || {});
    const options = keyboards.map(
      (keyboard) =>
        new Option(
          `${keyboard.name} · ${keyboard.kind.charAt(0).toUpperCase()}${keyboard.kind.slice(1)}`,
          keyboard.id,
        ),
    );

    if (captureOption)
      options.push(
        new Option(workflowLiteral("+ Capture New Keyboard"), "__capture__"),
      );

    select.replaceChildren(...options);
    return keyboards;
  }

  function currentKeyboardKey() {
    const capture = recording.keyboard_capture;
    return capture?.requested_keys?.find((key) => !capture.keys?.[key]) || null;
  }

  function displayKeyboardKey(key) {
    if (key === " ") return workflowLiteral("SPACE");
    if (key.toLowerCase() === "backspace") return workflowLiteral("BACKSPACE");
    return key;
  }

  function keyboardCaptureKindFields() {
    const kind = $("keyboard-capture-kind").value || "numeric";
    $("keyboard-numeric-options").hidden = kind !== "numeric";
    $("keyboard-alphabetic-options").hidden = kind !== "alphabetic";
    $("keyboard-custom-options").hidden = kind !== "custom";
  }

  function keyboardCaptureKeys(kind) {
    if (kind === "numeric") {
      const keys = Array.from("0123456789");
      if ($("keyboard-include-decimal").checked) keys.push(".");
      if ($("keyboard-numeric-backspace").checked) keys.push("BACKSPACE");
      return keys;
    }

    if (kind === "alphabetic") {
      const keys = Array.from("qwertyuiopasdfghjklzxcvbnm");

      if ($("keyboard-include-space").checked) keys.push(" ");
      if ($("keyboard-alphabetic-backspace").checked) keys.push("backspace");

      return keys;
    }

    return $("keyboard-custom-keys")
      .value.split(",")
      .map((label) => label.trim());
  }

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

  function isUsefulExpectedText(text) {
    const value = String(text || "").trim();

    if (value.length < 2) return false;

    const compact = value.toLowerCase().replace(/[^a-z0-9]/g, "");

    const keyboardPatterns = [
      "qwertyuiop",
      "asdfghjkl",
      "zxcvbnm",
      "1234567890",
    ];

    if (
      keyboardPatterns.some(
        (pattern) => pattern.includes(compact) || compact.includes(pattern),
      )
    ) {
      return false;
    }

    return true;
  }

  async function openExpectedText(step, wait, mode = "review") {
    expectedActionId = step?.id || null;
    expectedMode = mode;

    $("expected-text-editor").hidden = false;

    requestAnimationFrame(() => {
      $("expected-text-editor").scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    });

    workflowUiComputed($("expected-text-title"), () =>
      step
        ? workflowUi(
            "workflow.chooseExpectedAfter",
            `Choose expected text after ${describe(step)}`,
            { step: describe(step) },
          )
        : workflowUi(
            "workflow.chooseExpectedCurrent",
            "Choose expected text for the current screen",
          ),
    );

    $("expected-text-value").value = wait?.text || "";

    $("expected-text-timeout").value = wait?.timeout_seconds ?? 10;

    expectedOriginalROI = wait?.roi || null;
    expectedROIPicker.reset(expectedOriginalROI);
    $("expected-text-advanced").open = !!expectedOriginalROI;
    $("expected-suggestions").replaceChildren();
    workflowLiteralText(
      $("expected-text-status"),
      "Reading current phone screen…",
    );
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

      const choices = (result.suggestions || [])
        .map((text) => String(text || "").trim())
        .filter(isUsefulExpectedText)
        .filter(
          (text, index, list) =>
            list.findIndex(
              (item) => item.toLowerCase() === text.toLowerCase(),
            ) === index,
        )
        .slice(0, 5);

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
      workflowLiteralText(
        $("expected-text-status"),
        choices.length
          ? "Suggestions from the current screen. Select one or enter text manually."
          : "No reliable text suggestions found. Enter expected text manually.",
      );
    } catch (error) {
      if (expectedMode === mode && expectedActionId === (step?.id || null))
        workflowLiteralText(
          $("expected-text-status"),
          "No reliable text suggestions found. Enter expected text manually.",
        );
    }
    renderRecording();
  }

  function closeExpectedText(scrollBack = false) {
    expectedActionId = null;
    expectedMode = null;
    expectedOriginalROI = null;

    expectedROIPicker.reset(null);
    $("expected-text-advanced").open = false;
    $("expected-text-editor").hidden = true;

    render();

    if (scrollBack) {
      requestAnimationFrame(() => {
        const returnTarget = recording.active
          ? $("record-live")
          : $("review-flow");

        returnTarget?.scrollIntoView({
          behavior: "smooth",
          block: "start",
        });
      });
    }
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

      closeExpectedText(true);
    });
  });

  $("expected-text-cancel").addEventListener("click", () =>
    task(async () => {
      if (expectedMode && expectedMode !== "review")
        recording = await api("/api/recorder/expected-text/cancel", {});

      closeExpectedText(true);
    }),
  );

  $("expected-text-manual").addEventListener("click", () => {
    for (const radio of $("expected-suggestions").querySelectorAll("input"))
      radio.checked = false;
    $("expected-text-value").focus();
  });

  function renderInsertPositions(steps = recording.workflow?.steps || []) {
    const positions = [new Option(workflowLiteral("At the beginning"), "0")];
    steps.forEach((step, index) =>
      positions.push(
        new Option(
          workflowUi(
            "workflow.afterStep",
            `After ${index + 1}. ${describe(step, index)}`,
            { number: index + 1, step: describe(step, index) },
          ),
          String(index + 1),
        ),
      ),
    );
    $("insert-position").replaceChildren(...positions);
    $("insert-position").value = String(steps.length);
  }

  function insertionPayload(step) {
    return {
      operation: "insert",
      index: Number($("insert-position").value),
      step,
    };
  }

  const addStepSections = {
    wait: "add-step-wait",
    wait_for_text: "add-step-text",
    verify_text: "add-step-verify",
    capture_screen: "add-step-screenshot",
    ocr_value: "add-step-value",
  };

  function renderAddStepType() {
    for (const [type, id] of Object.entries(addStepSections)) {
      $(id).hidden = addStepType !== type;
    }

    document.querySelectorAll("[data-add-step-type]").forEach((button) => {
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.addStepType === addStepType),
      );
    });
  }

  function closeAddStep() {
    $("review-insert").open = false;
    addStepType = "wait";
    renderAddStepType();
  }

  document.querySelectorAll("[data-add-step-type]").forEach((button) => {
    button.addEventListener("click", () => {
      addStepType = button.dataset.addStepType;
      renderAddStepType();

      const focusTarget = {
        wait: "record-wait-seconds",
        wait_for_text: "record-text-value",
        verify_text: "verify-field",
        ocr_value: "record-value-preset",
      }[addStepType];

      if (focusTarget) requestAnimationFrame(() => $(focusTarget)?.focus());
    });
  });

  document.querySelectorAll("[data-add-step-cancel]").forEach((button) => {
    button.addEventListener("click", closeAddStep);
  });

  $("review-insert").addEventListener("toggle", () => {
    if ($("review-insert").open) renderAddStepType();
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

    $("flow-name").value = draft.name === "New Recording" ? "" : draft.name;
    $("flow-purpose").value = draft.purpose || "payment";
    const renderRows = (steps, nested = false) =>
      steps.map((step, index) => {
        const row = document.createElement("li"),
          title = document.createElement("strong");
        row.dataset ||= {};
        row.dataset.stepId = step.id;
        workflowUiComputed(title, () => describe(step, index));
        row.append(title);

        if (step.critical === true) {
          const warning = document.createElement("p");
          warning.className = "small";
          workflowLiteralText(warning, "Irreversible payment submission");
          row.append(warning);
        }

        if (step.type === "tap") {
          const detail = document.createElement("p");
          detail.textContent = `X ${Math.round(step.x * 100)}% • Y ${Math.round(step.y * 100)}%`;
          row.append(detail);
        }

        if (step.preview) {
          const image = document.createElement("img");
          image.src = "data:image/png;base64," + step.preview;
          image.alt = workflowLiteral("Target preview before the tap");
          image.width = 96;
          image.height = 96;
          row.append(image);
        }

        if (!recording.active && step.recording_screenshot?.image) {
          const wrap = document.createElement("div");
          wrap.className = "recording-step-preview";

          const image = document.createElement("img");

          const format =
            step.recording_screenshot.format === "png" ? "png" : "jpeg";

          image.src =
            `data:image/${format};base64,` + step.recording_screenshot.image;

          image.alt = `Screen before ${describe(step, index)}`;

          wrap.append(image);

          if (step.type === "tap") {
            const marker = document.createElement("span");
            marker.className = "recording-tap-marker";

            marker.style.left = `${step.x * 100}%`;
            marker.style.top = `${step.y * 100}%`;

            wrap.append(marker);
          }

          if (step.type === "swipe") {
            const start = document.createElement("span");
            start.className = "recording-swipe-start";
            start.style.left = `${step.start[0] * 100}%`;
            start.style.top = `${step.start[1] * 100}%`;

            const end = document.createElement("span");
            end.className = "recording-swipe-end";
            end.style.left = `${step.end[0] * 100}%`;
            end.style.top = `${step.end[1] * 100}%`;

            wrap.append(start, end);
          }

          row.append(wrap);
        }

        const actions = document.createElement("div");
        actions.className = "flow-buttons";
        const editButton = button("Edit", () => openEditor(step));
        workflowUiText(editButton, "common.edit", "Edit");
        actions.append(editButton);

        if (step.type === "input") {
          const value = document.createElement("p");
          value.textContent =
            step.source === "fixed"
              ? "“" + step.value + "”"
              : workflowUi("workflow.variableValue", "Variable: {label}", {
                  label:
                    draft.variables.find((v) => v.name === step.variable)
                      ?.label || workflowLiteral("Input"),
                });
          row.append(value);
        }
        const deleteButton = button("Delete", () =>
          review({ operation: "delete", step_id: step.id }),
        );
        workflowUiText(deleteButton, "common.delete", "Delete");
        actions.append(deleteButton);

        if (index > 0)
          actions.append(
            button("Move Up", () =>
              review({ operation: "move", step_id: step.id, index: index - 1 }),
            ),
          );

        if (index < steps.length - 1)
          actions.append(
            button("Move Down", () =>
              review({ operation: "move", step_id: step.id, index: index + 1 }),
            ),
          );

        if (!nested && !recording.active && canExpectText(step)) {
          const next = draft.steps[index + 1];
          const wait = next?.type === "wait_for_text" ? next : null;
          const expected = document.createElement("p");
          workflowUiText(
            expected,
            "workflow.expectedText",
            `Expected text: ${wait?.text || "Not configured"}`,
            {
              text:
                wait?.text ||
                workflowUi("common.notConfigured", "Not configured"),
            },
          );
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

        if (!nested && !recording.active)
          actions.append(
            button("+ Add Special Case", () => openSpecialCaseSetup(step.id)),
          );

        if (step.type === "if_text") {
          const branches = document.createElement("div");
          branches.className = "condition-branches";
          const summary = document.createElement("p");
          workflowUiText(
            summary,
            "workflow.specialSummary",
            `Looking for: “${step.text}” · ${step.then_steps.length} recorded ${step.then_steps.length === 1 ? "action" : "actions"} · If not shown: Continue normally`,
            () => ({
              text: step.text,
              count: step.then_steps.length,
              action: workflowUi(
                step.then_steps.length === 1
                  ? "workflow.action.one"
                  : "workflow.action.many",
                step.then_steps.length === 1 ? "action" : "actions",
              ),
            }),
          );
          branches.append(summary);
          const configuredBranches = [
            ["Actions when shown", "then_steps"],
            ...(step.else_steps.length
              ? [["Actions when not shown", "else_steps"]]
              : []),
          ];
          for (const [label, branch] of configuredBranches) {
            const section = document.createElement("section"),
              heading = document.createElement("h5"),
              list = document.createElement("ol");
            workflowLiteralText(heading, label);
            list.className = "condition-step-list";
            const childRows = renderRows(step[branch], true);
            list.replaceChildren(...childRows);
            if (!childRows.length) {
              const empty = document.createElement("li");
              empty.className = "condition-empty";
              workflowUiText(empty, "workflow.noSteps", "No steps recorded");
              list.append(empty);
            }
            section.append(heading, list);
            branches.append(section);
          }
          row.append(branches);
        }

        if (!recording.active) row.append(actions);
        return row;
      });

    const rows = renderRows(draft.steps);

    $("record-steps").replaceChildren(...rows);
    if (enteringReview) $("record-steps").scrollTop = 0;
    if (recording.active) rows.at(-1)?.scrollIntoView?.({ block: "nearest" });
    renderInsertPositions(draft.steps);
    $("workflow-debug-data").textContent = JSON.stringify(
      draft,
      (key, value) =>
        ["value", "default", "preview", "image"].includes(key)
          ? "(hidden)"
          : value,
      2,
    );
  }

  function renderCapturedKeyboards() {
    const keyboards = Object.values(recording.workflow?.keyboards || {});

    $("captured-keyboards").hidden = !recording.workflow || !keyboards.length;
    const referenced = new Set(
      allWorkflowSteps()
        .filter((step) => step.input_method === "screen_keyboard")
        .map((step) => step.keyboard_id),
    );

    $("captured-keyboard-list").replaceChildren(
      ...keyboards.map((keyboard) => {
        const row = document.createElement("li"),
          info = document.createElement("div"),
          name = document.createElement("strong"),
          detail = document.createElement("span"),
          usage = document.createElement("span"),
          remove = button("Delete", async () => {
            recording = await api("/api/recorder/keyboard-delete", {
              keyboard_id: keyboard.id,
            });
            show("Captured keyboard deleted.");
            renderRecording();
          });
        row.className = "keyboard-map-row";
        info.className = "keyboard-map-info";
        name.textContent = keyboard.name;
        const kind =
          keyboard.kind.charAt(0).toUpperCase() + keyboard.kind.slice(1);
        workflowUiText(
          detail,
          "workflow.keyboardSummary",
          `${kind} · ${Object.keys(keyboard.keys).length} keys`,
          {
            kind: workflowLiteral(kind),
            count: Object.keys(keyboard.keys).length,
          },
        );
        const uses = allWorkflowSteps().filter(
          (step) =>
            step.input_method === "screen_keyboard" &&
            step.keyboard_id === keyboard.id,
        );
        if (uses.length) {
          const usageItems = uses
            .map((step) =>
              step.source === "variable" && step.variable
                ? `${workflowLiteral("Input")} {{${step.variable}}}`
                : workflowLiteral("input step"),
            )
            .join(", ");
          workflowUiText(
            usage,
            "workflow.keyboardUsedBy",
            `Used by: ${usageItems}`,
            { uses: usageItems },
          );
        } else workflowLiteralText(usage, "Unused");
        remove.disabled =
          referenced.has(keyboard.id) || annotationBusy() || working;
        if (referenced.has(keyboard.id)) {
          workflowLiteralText(remove, "In Use");
          remove.title = usage.textContent;
        }
        info.append(name, detail, usage);
        row.append(info, remove);
        return row;
      }),
    );
  }

  function renderDeviceAvailability() {
    $("flow-run").disabled =
      working ||
      !selected ||
      !!recording.workflow ||
      (piManual() && !window.selectedPiDeviceConnected?.()) ||
      (running && !terminal.has(running.status));
  }

  function renderRecording() {
    const draft = recording.workflow,
      live = recording.active,
      editingExisting = !!recording.editing_existing,
      reviewSpecialCase = recording.alternate_screen?.return_mode === "review";

    $("control-workspace").classList.toggle("is-workflow-authoring", !!draft);
    $("workflow-phone-pane").classList.toggle(
      "uses-workflow-preview",
      backend === "mock",
    );
    $("workflow-mock-preview-slot").hidden = backend !== "mock";

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

    const waitControls = $("record-wait-controls");
    const waitSlot = live
      ? $("record-wait-live-slot")
      : $("record-wait-review-slot");

    if (waitControls.parentElement !== waitSlot) {
      waitSlot.append(waitControls);
    }

    $("record-wait-live-slot").hidden = !live || !liveWaitOpen;
    waitControls.hidden = live && !liveWaitOpen;
    workflowLiteralText($("record-wait"), live ? "Add Wait" : "Insert Wait");

    const verifyControls = $("record-verify-controls");
    const verifySlot = live
      ? $("record-verify-live-slot")
      : $("record-verify-review-slot");
    if (verifyControls.parentElement !== verifySlot)
      verifySlot.append(verifyControls);
    $("record-verify-live-slot").hidden = !live || !liveVerifyOpen;
    verifyControls.hidden = live && !liveVerifyOpen;
    workflowLiteralText(
      $("verify-add"),
      live ? "Add Verification" : "Insert Step",
    );

    if (!live) renderAddStepType();

    $("review-flow").hidden = !draft || live;

    $("saved-flow-panel").hidden = !!draft;

    $("workflow-debug").hidden = live;

    const timelineSlot = live
      ? $("live-timeline-slot")
      : $("review-timeline-slot");

    if ($("record-timeline").parentElement !== timelineSlot) {
      timelineSlot.append($("record-timeline"));
    }

    if (stopping)
      workflowLiteralText($("record-status"), "Finishing recording…");
    else if (recording.alternate_screen)
      workflowLiteralText($("record-status"), "● Recording Special Case");
    else
      workflowUiText(
        $("record-status"),
        "workflow.recordingCount",
        `● Recording • ${draft?.steps.length || 0} ${draft?.steps.length === 1 ? "step" : "steps"}`,
        {
          count: draft?.steps.length || 0,
          step: workflowUi(
            draft?.steps.length === 1
              ? "workflow.step.one"
              : "workflow.step.many",
            draft?.steps.length === 1 ? "step" : "steps",
          ),
        },
      );

    if (recording.alternate_screen)
      workflowUiText(
        $("record-count"),
        "workflow.lookingFor",
        `Looking for: “${recording.alternate_screen.text}”`,
        { text: recording.alternate_screen.text },
      );
    else
      workflowLiteralText(
        $("record-count"),
        "Taps, swipes and typing appear below.",
      );

    $("record-start").disabled = working || !!draft;

    $("record-stop").disabled = stopping;
    $("record-stop").hidden = reviewSpecialCase;

    $("record-expect-current").disabled = stopping || annotationBusy();
    $("record-read-value").disabled = stopping || annotationBusy();
    $("record-wait-here").disabled = stopping || annotationBusy();
    $("record-verify-text-here").disabled = stopping || annotationBusy();
    $("record-capture-keyboard").disabled = stopping || annotationBusy();
    $("review-capture-keyboard").disabled = annotationBusy();
    $("record-input-variable").disabled = stopping || annotationBusy();
    $("record-alternate-screen").hidden =
      alternateSetupOpen || !!recording.alternate_screen;
    $("record-alternate-screen").disabled = stopping || annotationBusy();
    $("alternate-screen-setup").hidden = !alternateSetupOpen;
    $("alternate-screen-active").hidden = !recording.alternate_screen;
    $("alternate-screen-finish").disabled = stopping || annotationBusy();
    $("alternate-screen-cancel").disabled = stopping || annotationBusy();
    if (recording.alternate_screen)
      workflowUiText(
        $("alternate-screen-status"),
        "workflow.specialActive",
        `Looking for: “${recording.alternate_screen.text}” · ${recording.alternate_screen.step_count} recorded ${recording.alternate_screen.step_count === 1 ? "action" : "actions"}`,
        {
          text: recording.alternate_screen.text,
          count: recording.alternate_screen.step_count,
          action: workflowUi(
            recording.alternate_screen.step_count === 1
              ? "workflow.action.one"
              : "workflow.action.many",
            recording.alternate_screen.step_count === 1 ? "action" : "actions",
          ),
        },
      );
    else $("alternate-screen-status").textContent = "";
    $("record-screenshot-here").disabled =
      working || stopping || annotationBusy();
    $("record-variable-input").hidden = !variableInputOpen;
    $("record-read-value-form").hidden = !readValueOpen;

    const sharedPhysicalBusy =
      externalPhysicalBusy || !!window.phoneConnection?.isPhysicalBusy?.();

    $("record-variable-type").disabled =
      working ||
      sharedPhysicalBusy ||
      !!window.phoneConnection?.isReconnecting?.();
    workflowLiteralText(
      $("record-variable-type"),
      sharedPhysicalBusy ? "Typing now..." : "Add & Type Now",
    );
    $("keyboard-capture").hidden =
      !keyboardSetupOpen && !recording.keyboard_capture;

    const keyboardCaptureSlot = live ? $("record-live") : $("review-flow");
    if ($("keyboard-capture").parentElement !== keyboardCaptureSlot)
      keyboardCaptureSlot.append($("keyboard-capture"));

    const captureKey = currentKeyboardKey();
    const overlay = $("keyboard-capture-overlay");
    const overlayKey = $("keyboard-capture-overlay-key");

    if (recording.keyboard_capture && captureKey) {
      const label = displayKeyboardKey(captureKey);

      overlay.hidden = false;
      overlayKey.textContent = label;

      workflowUiText(
        $("keyboard-capture-instruction"),
        "workflow.captureInstruction",
        `Tap ${label} on the phone preview.`,
        { key: label },
      );
    } else {
      overlay.hidden = true;

      workflowLiteralText(
        $("keyboard-capture-instruction"),
        recording.keyboard_capture
          ? "All selected keys are captured. Save the keyboard or cancel."
          : "Choose keys, then start capture.",
      );
    }

    $("keyboard-capture-start").hidden = !!recording.keyboard_capture;
    $("keyboard-capture-save").hidden =
      !recording.keyboard_capture || !!captureKey;

    $("record-undo").disabled =
      stopping ||
      annotationBusy() ||
      !(recording.alternate_screen
        ? recording.alternate_screen.step_count
        : draft?.steps.length) ||
      queued > 0 ||
      !!textBuffer;

    $("record-save").disabled = working || !draft?.steps.length || live;

    workflowLiteralText(
      $("record-save"),
      editingExisting ? "Save Changes" : "Save Workflow",
    );

    workflowLiteralText(
      $("record-cancel"),
      editingExisting ? "Cancel Editing" : "Discard Recording",
    );

    renderDeviceAvailability();

    $("mock-recording-preview").hidden =
      piManual() || backend !== "mock" || !live;

    // Real backends use the dashboard's live camera; this canvas is only the
    // mock phone surface used for simulated recording and playback.
    preview.hidden = backend !== "mock";
    const reviewTitle = $("review-flow")?.querySelector("h3");

    if (reviewTitle) {
      workflowLiteralText(
        reviewTitle,
        editingExisting ? "Edit Workflow" : "Review Recording",
      );
    }

    if (draft && !live && editingExisting) {
      workflowLiteralText(
        $("record-empty"),
        "This workflow has no steps. Add or edit steps, then save your changes.",
      );
    } else {
      workflowLiteralText(
        $("record-empty"),
        "No steps recorded yet. Use the phone preview to record taps, swipes, typing and navigation.",
      );
    }

    renderTimeline();
    renderCapturedKeyboards();
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
    $("input-method-fields").hidden = source === "manual";
    $("input-keyboard-field").hidden =
      source === "manual" || $("input-method").value !== "screen_keyboard";
    $("amount-format-field").hidden =
      source !== "variable" || $("variable-name").value !== "amount";
  }

  function recordAmountFormatFields() {
    $("record-amount-format-field").hidden =
      $("record-variable-name").value !== "amount";
  }

  function variableName(selectId, customId) {
    const choice = $(selectId).value;
    return choice === "__custom__" ? $(customId).value.trim() : choice;
  }

  function variableChoiceFields(selectId, customFieldId, customId, name) {
    const predefined = [
      "account_number",
      "account_name",
      "amount",
      "bank_name",
    ];
    if (name === "__custom__") {
      $(selectId).value = name;
      $(customFieldId).hidden = false;
      return;
    }
    $(selectId).value = predefined.includes(name) ? name : "__custom__";
    $(customId).value = predefined.includes(name) ? "" : name || "";
    $(customFieldId).hidden = $(selectId).value !== "__custom__";
  }

  function openEditor(step, source) {
    editingId = step.id;

    $("step-editor").hidden = false;
    $("step-editor-title").textContent = describe(
      step,
      allWorkflowSteps(recording.workflow.steps).indexOf(step),
    );

    $("step-name").value = step.label || "";
    $("step-critical").checked = step.critical === true;

    // Show/hide step-specific controls.
    setSectionVisible("step-tap-options", step.type === "tap");

    setSectionVisible("step-swipe-options", step.type === "swipe");

    setSectionVisible("step-key-options", step.type === "key");

    setSectionVisible("step-input-options", step.type === "input");

    setSectionVisible("step-wait-options", step.type === "wait");

    setSectionVisible("step-stable-options", step.type === "wait_until_stable");
    setSectionVisible("step-value-options", step.type === "ocr_value");
    $("step-value-parser").value = step.parser || "currency";
    $("step-value-output").value = step.output || "payer_balance";
    valueROIPicker.reset(step.type === "ocr_value" ? step.roi : null);

    setSectionVisible(
      "step-text-options",
      ["wait_for_text", "if_text"].includes(step.type),
    );
    setSectionVisible("step-verify-options", step.type === "verify_text");

    $("step-stable-ms").value = step.stable_ms ?? 700;
    $("step-stable-timeout").value = step.timeout_seconds ?? 10;
    $("step-text-value").value = step.text ?? "";
    $("step-text-timeout").value = step.timeout_seconds ?? 10;
    $("step-text-timeout").min = step.type === "if_text" ? "0.1" : "0.5";

    stepROIPicker.reset(step.roi);
    $("step-text-advanced").open = !!step.roi;

    if (step.type === "verify_text") {
      $("step-verify-field").value = verifyFieldConfig(step.variable)
        ? step.variable
        : "account_name";
      stepVerifyROIPicker.reset(step.roi);
    }

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
      $("input-value").placeholder = workflowLiteral(
        "Leave blank to keep existing value",
      );
    } else {
      $("input-value").value = step.value ?? definition?.default ?? "";
      $("input-value").placeholder = "";
    }

    variableChoiceFields(
      "variable-name",
      "variable-custom-field",
      "variable-custom-name",
      definition?.name,
    );
    $("variable-default").value = definition?.default ?? "";
    $("amount-format").value = step.amount_format || "raw";
    $("input-method").value = step.input_method || "hid";

    const keyboards = keyboardOptions($("input-keyboard"));
    $("input-keyboard").value = step.keyboard_id || keyboards[0]?.id || "";

    sourceFields();

    $("step-editor").scrollIntoView?.({
      block: "nearest",
      behavior: "smooth",
    });
  }

  $("input-source").addEventListener("change", sourceFields);
  $("variable-name").addEventListener("change", () => {
    $("variable-custom-field").hidden =
      $("variable-name").value !== "__custom__";
    sourceFields();
  });
  $("input-method").addEventListener("change", sourceFields);

  $("step-close").addEventListener("click", () => {
    editingId = null;
    $("step-editor").hidden = true;
  });

  $("step-editor").addEventListener("submit", (event) => {
    event.preventDefault();

    const step_id = editingId;
    const label = $("step-name").value;

    const old = findWorkflowStep(step_id);

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
    if (source !== "manual" && $("input-method").value === "screen_keyboard") {
      if (!$("input-keyboard").value) {
        show(
          "Capture a keyboard before selecting on-screen keyboard input.",
          true,
        );
        return;
      }
      input.input_method = "screen_keyboard";
      input.keyboard_id = $("input-keyboard").value;
    }

    if (source === "fixed") {
      const value = $("input-value").value;

      // When editing an existing saved workflow:
      // blank = preserve the existing server-side value.
      if (!recording.editing_existing || value !== "") {
        input.value = value;
      }
    }

    if (source === "variable") {
      const previousDefinition = recording.workflow.variables.find(
        (definition) => definition.name === old.variable,
      );
      const name = variableName("variable-name", "variable-custom-name");
      const enteredDefault = $("variable-default").value;
      const defaultValue =
        enteredDefault !== "" || previousDefinition?.default === ""
          ? enteredDefault
          : null;
      const variable = previousDefinition
        ? { name, default: defaultValue }
        : {
            name,
            label: name.replaceAll("_", " "),
            type: "text",
            required: true,
            default: defaultValue,
            ask_when_running: true,
          };
      input.variable = variable;
      if (name === "amount")
        input.amount_format = $("amount-format").value || "raw";
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

      if (old.type === "ocr_value") {
        await review({
          operation: "fields",
          step_id,
          fields: valueFields("step", valueROIPicker),
        });
      }
      if (["wait_for_text", "if_text"].includes(old.type)) {
        await review({
          operation: "fields",
          step_id,
          fields: textWaitFields("step"),
        });
      }

      if (old.type === "verify_text") {
        await review({
          operation: "fields",
          step_id,
          fields: verifyTextFields("step-verify", stepVerifyROIPicker, old.roi),
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
        operation: "set_critical",
        step_id,
        critical: $("step-critical").checked,
      });

      await review({
        operation: "rename_step",
        step_id,
        label,
      });

      $("input-value").value = "";

      editingId = null;
      $("step-editor").hidden = true;

      show("Step updated.");
    });
  });

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

  const roiKeys = ["x", "y", "width", "height"];

  function mountRegionOCR(prefix) {
    const template = $("region-ocr-template");
    if (!template?.content) return;
    const fragment = template.content.cloneNode(true);
    for (const label of fragment.querySelectorAll("[data-roi-i18n]")) {
      workflowUiText(
        label,
        label.dataset.roiI18n,
        label.textContent.trim().replace(/\s+/g, " "),
      );
      label.removeAttribute("data-roi-i18n");
    }
    for (const control of fragment.querySelectorAll("[data-roi-control]")) {
      control.id = `${prefix}-roi-${control.dataset.roiControl}`;
      control.removeAttribute("data-roi-control");
    }
    $(`${prefix}-roi-picker`).append(fragment);
  }

  function createRegionOCRPicker(prefix, textPrefix, parser = null) {
    mountRegionOCR(prefix);
    const canvas = $(`${prefix}-roi-canvas`);
    let image = null,
      token = null,
      start = null,
      generation = 0;

    function hasValues() {
      return roiKeys.every((key) => $(`${prefix}-roi-${key}`).value !== "");
    }

    function read(validate = true) {
      const roi = Object.fromEntries(
        roiKeys.map((key) => [key, Number($(`${prefix}-roi-${key}`).value)]),
      );
      if (
        validate &&
        (roiKeys.some(
          (key) =>
            $(`${prefix}-roi-${key}`).value === "" ||
            !Number.isFinite(roi[key]) ||
            roi[key] < 0 ||
            roi[key] > 1,
        ) ||
          roi.width <= 0 ||
          roi.height <= 0 ||
          roi.x + roi.width > 1 + 1e-12 ||
          roi.y + roi.height > 1 + 1e-12)
      )
        throw new Error(
          "ROI must have positive size and fit inside the calibrated screen.",
        );
      return roi;
    }

    function setValues(roi) {
      roiKeys.forEach((key, index) => {
        $(`${prefix}-roi-${key}`).value =
          (Array.isArray(roi) ? roi[index] : roi?.[key]) ?? "";
      });
    }

    function valueSummary() {
      if (!hasValues()) return workflowLiteral("No OCR region selected.");
      return roiKeys
        .map((key) => {
          const label =
            key === "width"
              ? workflowUi("workflow.roi.width", "Width")
              : key === "height"
                ? workflowUi("workflow.roi.height", "Height")
                : key.toUpperCase();
          return `${label}: ${read(false)[key].toFixed(4)}`;
        })
        .join(" · ");
    }

    function draw() {
      $(`${prefix}-roi-values`).textContent = valueSummary();
      $(`${prefix}-roi-result`).textContent = "";
      if (!image) return;
      const context = canvas.getContext("2d");
      context.drawImage(image, 0, 0);
      if (!hasValues()) return;
      const roi = read(false);
      context.strokeStyle = "#ff3b30";
      context.lineWidth = Math.max(2, canvas.width / 200);
      context.strokeRect(
        roi.x * canvas.width,
        roi.y * canvas.height,
        roi.width * canvas.width,
        roi.height * canvas.height,
      );
    }

    workflowUiComputed($(`${prefix}-roi-values`), valueSummary);

    function reset(roi) {
      generation++;
      image = token = start = null;
      canvas.hidden = true;
      $(`${prefix}-roi-enabled`).checked = !!roi;
      setValues(roi);
      draw();
    }

    function clear() {
      generation++;
      start = null;
      $(`${prefix}-roi-enabled`).checked = false;
      setValues(null);
      draw();
    }

    function fields(existingROI = null) {
      if ($(`${prefix}-roi-enabled`).checked) return { roi: read() };
      return existingROI ? { roi: null } : {};
    }

    function point(event) {
      const bounds = canvas.getBoundingClientRect();
      return [
        Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)),
        Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height)),
      ];
    }

    roiKeys.forEach((key) =>
      $(`${prefix}-roi-${key}`).addEventListener("input", draw),
    );
    $(`${prefix}-roi-refresh`).addEventListener("click", () =>
      task(async () => {
        const selectedGeneration = ++generation;
        const result = await api("/api/ocr/roi-preview", {});
        const nextImage = new Image();
        await new Promise((resolve, reject) => {
          nextImage.onload = resolve;
          nextImage.onerror = reject;
          nextImage.src = result.image;
        });
        if (selectedGeneration !== generation) return;
        image = nextImage;
        token = result.token;
        canvas.width = result.width;
        canvas.height = result.height;
        canvas.hidden = false;
        draw();
      }),
    );
    $(`${prefix}-roi-clear`).addEventListener("click", clear);
    canvas.addEventListener("pointerdown", (event) => {
      if (!image || event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      start = point(event);
      canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener("pointermove", (event) => {
      if (!start) return;
      const [x, y] = point(event);
      setValues(
        [
          Math.min(x, start[0]),
          Math.min(y, start[1]),
          Math.abs(x - start[0]),
          Math.abs(y - start[1]),
        ].map((value) => Number(value.toFixed(6))),
      );
      $(`${prefix}-roi-enabled`).checked = true;
      draw();
    });
    for (const name of ["pointerup", "pointercancel", "lostpointercapture"])
      canvas.addEventListener(name, () => {
        start = null;
      });
    const testButton = $(`${prefix}-roi-test`);
    if (!parser && !textPrefix) {
      testButton.hidden = true;
      testButton.disabled = true;
    }
    testButton.addEventListener("click", () =>
      task(async () => {
        if (!parser && !textPrefix) return;
        if (!token) throw new Error("Select an OCR region snapshot first.");
        const testedGeneration = generation;
        const text = parser ? "" : $(`${textPrefix}-text-value`).value.trim();
        const result = await api("/api/ocr/roi-test", {
          token,
          roi: read(),
          text,
          ...(parser ? { parser } : {}),
        });
        if (testedGeneration !== generation) return;
        $(`${prefix}-roi-result`).textContent = parser
          ? workflowUi("workflow.readResult", `Read Balance: ${result.value}`, {
              value: result.value,
            })
          : result.found
            ? workflowUi(
                "workflow.targetDetected",
                `Target detected in selected snapshot crop · ${result.texts.join(" | ")}`,
                { texts: result.texts.join(" | ") },
              )
            : workflowUi(
                "workflow.targetNotDetected",
                `Target not detected in selected snapshot crop · ${result.texts.join(" | ") || "No text read"}`,
                {
                  texts:
                    result.texts.join(" | ") || workflowLiteral("No text read"),
                },
              );
      }),
    );

    return { fields, reset };
  }

  const stepROIPicker = createRegionOCRPicker("step", "step");
  const stepVerifyROIPicker = createRegionOCRPicker("step-verify", null);
  const recordROIPicker = createRegionOCRPicker("record", "record");
  const verifyROIPicker = createRegionOCRPicker("verify", null);
  const conditionROIPicker = createRegionOCRPicker("condition", "condition");
  const expectedROIPicker = createRegionOCRPicker("expected", "expected");
  const valueROIPicker = createRegionOCRPicker("value", null, "currency");
  const recordValueROIPicker = createRegionOCRPicker(
    "record-value",
    null,
    "currency",
  );
  const liveValueROIPicker = createRegionOCRPicker(
    "live-value",
    null,
    "currency",
  );
  liveValueROIPicker.reset(null);
  recordValueROIPicker.reset(null);
  recordROIPicker.reset(null);
  verifyROIPicker.reset(null);
  conditionROIPicker.reset(null);

  const VERIFY_FIELDS = Object.freeze({
    account_name: Object.freeze({
      label: "Account Name",
      match_mode: "account_name",
    }),
    account_number: Object.freeze({
      label: "Account Number",
      match_mode: "account_number",
    }),
    bank_name: Object.freeze({ label: "Bank Name", match_mode: "contains" }),
    amount: Object.freeze({ label: "Amount", match_mode: "amount" }),
  });

  function verifyFieldConfig(variable) {
    return VERIFY_FIELDS[variable] || null;
  }

  function verifyTextFields(prefix, picker, existingROI = null) {
    const variable = $(`${prefix}-field`).value;
    const config = verifyFieldConfig(variable);
    if (!config) throw new Error("Choose a field to verify.");
    const fields = {
      source: "variable",
      variable,
      match_mode: config.match_mode,
      timeout_seconds: 5,
    };
    Object.assign(fields, picker.fields(existingROI));
    return fields;
  }

  function textWaitFields(prefix) {
    const text = $(`${prefix}-text-value`).value.trim();
    const timeout_seconds = Number($(`${prefix}-text-timeout`).value);
    const minimumTimeout =
      prefix === "condition" ||
      (prefix === "step" && findWorkflowStep(editingId)?.type === "if_text")
        ? 0.1
        : 0.5;
    if (!text || text.length > 500)
      throw new Error("Text to detect must be between 1 and 500 characters.");
    if (
      !Number.isFinite(timeout_seconds) ||
      timeout_seconds < minimumTimeout ||
      timeout_seconds > 300
    )
      throw new Error(
        `Wait time must be between ${minimumTimeout} and 300 seconds.`,
      );
    const fields = { text, timeout_seconds };
    if (prefix === "step")
      Object.assign(
        fields,
        stepROIPicker.fields(findWorkflowStep(editingId)?.roi),
      );
    else if (prefix === "record")
      Object.assign(fields, recordROIPicker.fields());
    else if (prefix === "condition")
      Object.assign(fields, conditionROIPicker.fields());
    else if (prefix === "expected")
      Object.assign(fields, expectedROIPicker.fields(expectedOriginalROI));
    return fields;
  }

  $("record-text").addEventListener("click", () =>
    task(async () => {
      await review(
        insertionPayload({
          type: "wait_for_text",
          ...textWaitFields("record"),
        }),
      );

      closeAddStep();
      recordROIPicker.reset(null);
      $("record-text-advanced").open = false;
    }),
  );

  $("verify-add").addEventListener("click", () =>
    task(async () => {
      const step = {
        type: "verify_text",
        ...verifyTextFields("verify", verifyROIPicker),
      };
      step.label = `Verify ${verifyFieldConfig(step.variable).label}`;
      if (recording.active) {
        await pendingManual;
        await captureQueue;
        recording = await api("/api/recorder/step", {
          capture: false,
          step,
        });
        liveVerifyOpen = false;
        show("Text verification added. Recording continues.");
      } else {
        await review(insertionPayload(step));
        closeAddStep();
      }
      verifyROIPicker.reset(null);
    }),
  );

  $("verify-cancel").addEventListener("click", () => {
    if (recording.active) {
      liveVerifyOpen = false;
      renderRecording();
    } else closeAddStep();
  });

  $("record-wait").addEventListener("click", () =>
    task(async () => {
      const seconds = Number($("record-wait-seconds").value);

      if (!Number.isFinite(seconds) || seconds < 0 || seconds > 3600) {
        throw new Error("Wait duration must be between 0 and 3600 seconds.");
      }

      if (recording.active) {
        await pendingManual;
        await captureQueue;

        liveWaitOpen = false;

        recording = await api("/api/recorder/step", {
          capture: false,
          step: {
            type: "wait",
            seconds,
          },
        });

        show(`Wait ${seconds}s added. Recording continues.`);
        return;
      }

      await review(
        insertionPayload({
          type: "wait",
          seconds,
        }),
      );

      closeAddStep();
    }),
  );

  $("record-wait-cancel").addEventListener("click", () => {
    if (recording.active) {
      liveWaitOpen = false;
      renderRecording();
      return;
    }

    closeAddStep();
  });

  function valueFields(prefix, picker) {
    const fields = picker.fields();
    if (!fields.roi) throw new Error("Select a calibrated OCR region first.");
    return {
      ...fields,
      parser: "currency",
      output: $(`${prefix}-value-output`).value.trim(),
    };
  }

  function applyValuePreset(prefix) {
    const balance = $(`${prefix}-value-preset`).value === "balance";
    const name = $(`${prefix}-value-name`);
    const output = $(`${prefix}-value-output`);
    $(`${prefix}-value-parser`).value = "currency";
    name.readOnly = balance;
    output.readOnly = balance;
    if (balance) {
      name.value = "Available Balance";
      output.value = "payer_balance";
    } else {
      if (name.value === "Available Balance") name.value = "";
      if (output.value === "payer_balance") output.value = "";
    }
  }

  $("record-value-preset").addEventListener("change", () =>
    applyValuePreset("record"),
  );

  $("record-value").addEventListener("click", () =>
    task(async () => {
      await review(
        insertionPayload({
          type: "ocr_value",
          label: $("record-value-name").value.trim() || "Read Balance",
          ...valueFields("record", recordValueROIPicker),
        }),
      );

      closeAddStep();
    }),
  );

  $("record-read-value").addEventListener("click", () =>
    task(async () => {
      await pendingManual;
      await flushText();
      readValueOpen = true;
      $("live-value-preset").value = "balance";
      applyValuePreset("live");
      liveValueROIPicker.reset(null);
    }),
  );

  $("live-value-preset").addEventListener("change", () =>
    applyValuePreset("live"),
  );

  $("live-value-save").addEventListener("click", () =>
    task(async () => {
      if (!recording.active || stopping) return;
      await pendingManual;
      await flushText();
      const step = {
        type: "ocr_value",
        label: $("live-value-name").value.trim() || "Read Balance",
        ...valueFields("live", liveValueROIPicker),
      };
      recording = await api("/api/recorder/step", {
        capture: false,
        step,
      });
      readValueOpen = false;
      liveValueROIPicker.reset(null);
      show("Read Balance step added. Recording continues.");
    }),
  );

  $("live-value-cancel").addEventListener("click", () => {
    readValueOpen = false;
    liveValueROIPicker.reset(null);
    renderRecording();
  });

  $("record-screenshot").addEventListener("click", () =>
    task(async () => {
      await review(
        insertionPayload({
          type: "capture_screen",
          label: "Screenshot",
          output_key: "receipt_path",
        }),
      );

      closeAddStep();
    }),
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
      enabledManualForReviewSpecialCase = false;

      try {
        await api("/api/recorder/stop", {});
        recording = await api("/api/recorder", undefined, "GET");
      } catch {
        /* Keep the local input gate closed if the server is unavailable. */
      }

      recording.active = false;
      show(
        `${error.message} ${workflowLiteral(
          "Recording stopped; inspect the phone before continuing.",
        )}`,
        true,
      );
    }
    renderRecording();
  }

  function enqueueRecording(operation) {
    if (!recording.active || annotationBusy()) return captureQueue;
    if (queued >= 32) {
      textBuffer = "";
      clearTimeout(textTimer);
      recording.active = false;

      if (backend !== "mock") disable().catch(() => {});
      enabledManualForReviewSpecialCase = false;

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
    const queuedOperation = captureQueue.then(async () => {
      try {
        if (recording.active) await operation();
      } finally {
        queued--;
        renderRecording();
      }
    });
    captureQueue = queuedOperation.catch(() => {});
    return queuedOperation;
  }

  function enqueue(step, extra = {}) {
    return enqueueRecording(() => capture(step, extra));
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

  function naturalAction(
    action,
    point,
    coordinateSpace = previewCoordinateSpace(),
  ) {
    if (!recording.active || stopping || annotationBusy()) return;
    flushText();

    if (action === "tap")
      return enqueue(
        { type: "tap" },
        { camera_start: point, coordinate_space: coordinateSpace },
      );

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

  async function openSpecialCaseSetup(afterStepId = null) {
    if (alternateSetupOpen) {
      show("Finish or cancel the current Special Case setup first.", true);
      return;
    }

    if (recording.alternate_screen) {
      show("Finish the current Special Case before adding another one.", true);
      return;
    }

    const startedFromReview = !!afterStepId;

    enabledManualForReviewSpecialCase = false;

    if (startedFromReview && (piManual() || backend !== "mock") && !active()) {
      const enabled = await enableManualControl();

      if (!enabled) {
        throw new Error(
          "Manual Control could not be enabled. Special Case setup was not opened.",
        );
      }

      enabledManualForReviewSpecialCase = true;
    }

    specialCaseAfterStepId = afterStepId;
    alternateSetupOpen = true;

    conditionROIPicker.reset(null);
    $("condition-text-advanced").open = false;

    renderRecording();
    $("condition-text-value").focus();

    if (startedFromReview) {
      show(
        "Special Case setup opened. Manual Control is enabled so you can position the phone.",
      );
    }
  }

  $("record-alternate-screen").addEventListener("click", () =>
    task(() => openSpecialCaseSetup()),
  );

  $("alternate-screen-setup-cancel").addEventListener("click", () =>
    task(async () => {
      if (enabledManualForReviewSpecialCase) {
        try {
          await disable();
        } finally {
          enabledManualForReviewSpecialCase = false;
        }
      }

      alternateSetupOpen = false;
      specialCaseAfterStepId = null;

      conditionROIPicker.reset(null);
      $("condition-text-advanced").open = false;

      renderRecording();
    }),
  );

  $("alternate-screen-start").addEventListener("click", () =>
    task(async () => {
      await pendingManual;
      await flushText();

      recording = await api("/api/recorder/alternate-screen/start", {
        ...textWaitFields("condition"),
        ...(specialCaseAfterStepId
          ? { after_step_id: specialCaseAfterStepId }
          : {}),
      });

      specialCaseAfterStepId = null;
      alternateSetupOpen = false;
      conditionROIPicker.reset(null);
      $("condition-text-advanced").open = false;

      show("Recording Special Case actions.");
    }),
  );

  $("alternate-screen-finish").addEventListener("click", () =>
    task(async () => {
      await pendingManual;
      await flushText();
      await captureQueue;
      const conditionId = recording.alternate_screen?.condition_id;
      const returnMode = recording.alternate_screen?.return_mode;
      recording = await api("/api/recorder/alternate-screen/finish", {});

      if (returnMode === "review" && enabledManualForReviewSpecialCase) {
        try {
          await disable();
        } finally {
          enabledManualForReviewSpecialCase = false;
        }
      }

      renderRecording();

      show(
        returnMode === "review"
          ? "Special Case saved. Review continues."
          : "Special Case saved. Main recording continues.",
      );
      if (returnMode === "review" && conditionId)
        document
          .querySelector?.(`[data-step-id="${conditionId}"]`)
          ?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
    }),
  );

  $("alternate-screen-cancel").addEventListener("click", () =>
    task(async () => {
      textBuffer = "";
      clearTimeout(textTimer);
      await pendingManual;
      await captureQueue;
      const returnMode = recording.alternate_screen?.return_mode;
      recording = await api("/api/recorder/alternate-screen/cancel", {});

      if (returnMode === "review" && enabledManualForReviewSpecialCase) {
        try {
          await disable();
        } finally {
          enabledManualForReviewSpecialCase = false;
        }
      }

      show(
        returnMode === "review"
          ? "Special Case cancelled. Actions already performed on the phone were not reversed."
          : "Special Case cancelled. Main recording continues; actions already performed on the phone were not reversed.",
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

  $("record-wait-here").addEventListener("click", () =>
    task(async () => {
      if (!recording.active || stopping || annotationBusy()) return;

      await pendingManual;
      await flushText();
      await captureQueue;

      liveWaitOpen = true;
      renderRecording();

      requestAnimationFrame(() => {
        $("record-wait-seconds").focus();
        $("record-wait-seconds").select();
      });
    }),
  );

  $("record-verify-text-here").addEventListener("click", () =>
    task(async () => {
      if (!recording.active || stopping || annotationBusy()) return;
      await pendingManual;
      await flushText();
      await captureQueue;
      liveVerifyOpen = true;
      renderRecording();
      requestAnimationFrame(() => $("verify-field").focus());
    }),
  );

  $("record-screenshot-here").addEventListener("click", () =>
    task(async () => {
      if (!recording.active || stopping || annotationBusy()) return;
      await pendingManual;
      flushText();
      await enqueueRecording(async () => {
        recording = await api("/api/recorder/step", {
          capture: false,
          step: {
            type: "capture_screen",
            label: "Screenshot",
            output_key: "receipt_path",
          },
        });
        show("Screenshot added. Recording continues.");
      });
    }),
  );

  function variableMethodFields() {
    $("record-variable-keyboard-field").hidden =
      $("record-variable-method").value !== "screen_keyboard";
  }

  $("record-input-variable").addEventListener("click", () => {
    variableInputOpen = true;
    if (
      !["raw", "implied_cents", "whole_number"].includes(
        $("record-amount-format").value,
      )
    )
      $("record-amount-format").value = "raw";

    if (!["hid", "screen_keyboard"].includes($("record-variable-method").value))
      $("record-variable-method").value = "screen_keyboard";
    const current = $("record-variable-keyboard").value;
    const keyboards = keyboardOptions($("record-variable-keyboard"), true);
    $("record-variable-keyboard").value = keyboards.some(
      (keyboard) => keyboard.id === current,
    )
      ? current
      : keyboards[0]?.id || "__capture__";
    variableMethodFields();
    variableChoiceFields(
      "record-variable-name",
      "record-variable-custom-field",
      "record-variable-custom-name",
      $("record-variable-name").value,
    );
    recordAmountFormatFields();
    renderRecording();
  });

  $("record-variable-method").addEventListener("change", variableMethodFields);
  $("record-variable-name").addEventListener("change", () => {
    $("record-variable-custom-field").hidden =
      $("record-variable-name").value !== "__custom__";
    recordAmountFormatFields();
  });
  $("record-variable-keyboard").addEventListener("change", () => {
    if ($("record-variable-keyboard").value !== "__capture__") return;
    returnToVariableInput = true;
    variableInputOpen = false;
    keyboardSetupOpen = true;
    workflowLiteralText(
      $("keyboard-capture-instruction"),
      "Choose keys, then start capture.",
    );
    keyboardCaptureKindFields();
    renderRecording();
  });

  async function addVariableInput(typeNow) {
    await pendingManual;
    await flushText();
    const name = variableName(
      "record-variable-name",
      "record-variable-custom-name",
    );
    if (!name) throw new Error("Enter a variable name.");
    const defaultValue = $("record-variable-default").value;
    if (typeNow && defaultValue === "")
      throw new Error("Enter a Default value before using Add & Type Now.");
    const method = $("record-variable-method").value;
    const keyboardId = $("record-variable-keyboard").value;
    if (
      method === "screen_keyboard" &&
      (!keyboardId || keyboardId === "__capture__")
    )
      throw new Error("Capture or select a keyboard first.");
    const request = () =>
      api("/api/recorder/variable-input", {
        variable: {
          name,
          label:
            {
              account_number: "Account Number",
              amount: "Amount",
              bank_name: "Bank Name",
            }[name] || name.replaceAll("_", " "),
          type: "text",
          required: true,
          default: defaultValue === "" ? null : defaultValue,
          ask_when_running: true,
        },
        input_method: method,
        ...(name === "amount"
          ? { amount_format: $("record-amount-format").value || "raw" }
          : {}),
        ...(method === "screen_keyboard" ? { keyboard_id: keyboardId } : {}),
        sample: defaultValue,
        type_sample: typeNow,
        frame_id: frameId,
      });
    const runPhysical = window.phoneConnection?.runPhysical
      ? (label, operation) =>
          window.phoneConnection.runPhysical(label, operation)
      : (_label, operation) => operation();
    try {
      recording = typeNow
        ? await runPhysical("Typing default value...", request)
        : await request();
    } catch (error) {
      if (typeNow && error.transient)
        error.message = `${error.message} ${workflowLiteral(
          "Typing result uncertain; it was not retried automatically.",
        )}`;
      throw error;
    }
    variableInputOpen = false;
    $("record-variable-default").value = "";
    show(
      typeNow
        ? "Variable step added and its default value typed once."
        : "Variable step added without sending input.",
    );
  }

  $("record-variable-add").addEventListener("click", () =>
    task(() => addVariableInput(false)),
  );
  $("record-variable-type").addEventListener("click", () =>
    task(() => addVariableInput(true)),
  );
  $("record-variable-cancel").addEventListener("click", () => {
    variableInputOpen = false;
    renderRecording();
  });

  function openKeyboardCapture() {
    keyboardSetupOpen = true;
    workflowLiteralText(
      $("keyboard-capture-instruction"),
      "Choose keys, then start capture.",
    );
    keyboardCaptureKindFields();
    renderRecording();
    render();
  }

  $("record-capture-keyboard").addEventListener("click", openKeyboardCapture);
  $("review-capture-keyboard").addEventListener("click", openKeyboardCapture);

  $("keyboard-capture-kind").addEventListener(
    "change",
    keyboardCaptureKindFields,
  );

  $("keyboard-capture-start").addEventListener("click", () =>
    task(async () => {
      await pendingManual;
      await flushText();
      const kind = $("keyboard-capture-kind").value || "numeric";
      const keys = keyboardCaptureKeys(kind);
      recording = await api("/api/recorder/keyboard-capture/start", {
        name: $("keyboard-capture-name").value.trim(),
        kind,
        keys,
      });
      keyboardSetupOpen = false;
      show(
        "Teaching clicks are captured only; they are not sent to the phone.",
      );
      render();
    }),
  );

  $("keyboard-capture-save").addEventListener("click", () =>
    task(async () => {
      const capturedId = recording.keyboard_capture?.id;
      recording = await api("/api/recorder/keyboard-capture/save", {});
      keyboardSetupOpen = false;
      if (returnToVariableInput) {
        returnToVariableInput = false;
        variableInputOpen = true;
        keyboardOptions($("record-variable-keyboard"), true);
        $("record-variable-keyboard").value = capturedId;
      }
      show("Keyboard saved. Use it only while its layout remains stable.");
      render();
    }),
  );

  $("keyboard-capture-cancel").addEventListener("click", () =>
    task(async () => {
      if (recording.keyboard_capture)
        recording = await api("/api/recorder/keyboard-capture/cancel", {});
      keyboardSetupOpen = false;
      if (returnToVariableInput) {
        returnToVariableInput = false;
        variableInputOpen = true;
        keyboardOptions($("record-variable-keyboard"), true);
      }
      show("Keyboard capture cancelled; the workflow was not changed.");
      render();
    }),
  );

  $("record-start").addEventListener("click", () =>
    task(async () => {
      let enabledManualForRecording = false;

      if (piManual() || backend !== "mock") {
        if (!active()) {
          const enabled = await enableManualControl();

          if (!enabled) {
            throw new Error(
              "Manual Control could not be enabled. Recording was not started.",
            );
          }

          enabledManualForRecording = true;
        }
      }

      try {
        keyboardSetupOpen = false;
        variableInputOpen = false;
        readValueOpen = false;
        liveWaitOpen = false;
        addStepType = "wait";
        $("review-insert").open = false;
        returnToVariableInput = false;
        alternateSetupOpen = false;
        specialCaseAfterStepId = null;

        recording = await api("/api/recorder/start", {
          name: "New Recording",
          execute: true,
        });
      } catch (error) {
        if (enabledManualForRecording) {
          await disable();
        }

        throw error;
      }

      timelineSignature = "";
      textBuffer = "";
      stopping = false;

      show(
        "Recording started. Manual Control is enabled — use the phone preview normally.",
      );

      const livePreview = !piManual() && backend === "mock" ? preview : canvas;

      livePreview.focus({ preventScroll: true });

      livePreview.scrollIntoView?.({
        block: "center",
        behavior: "smooth",
      });
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
        keyboardSetupOpen = false;
        variableInputOpen = false;
        readValueOpen = false;
        liveWaitOpen = false;
        returnToVariableInput = false;
        alternateSetupOpen = false;
        specialCaseAfterStepId = null;
        closeExpectedText();
        if (piManual() || backend !== "mock") await disable();
        show(
          "Recording stopped. Review your steps, enter the bank name, then save.",
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

      keyboardSetupOpen = false;
      variableInputOpen = false;
      readValueOpen = false;
      liveWaitOpen = false;
      addStepType = "wait";
      returnToVariableInput = false;
      alternateSetupOpen = false;
      specialCaseAfterStepId = null;

      closeExpectedText();

      await loadFlows();
      renderRecording();

      show(editingExisting ? "Changes discarded." : "Recording discarded.");

      if (editingExisting) {
        scrollToSavedFlows();
      }
    }),
  );

  $("record-save").addEventListener("click", () =>
    task(async () => {
      if (!$("flow-name").value.trim()) {
        $("flow-name").focus();
        return show("Enter bank name", true);
      }

      const editingExisting = !!recording.editing_existing;

      await review({
        operation: "rename_flow",
        name: $("flow-name").value.trim(),
        purpose: $("flow-purpose").value,
      });

      const saved = await api("/api/recorder/save", {});

      recording = {
        active: false,
        workflow: null,
        editing_existing: false,
      };
      liveWaitOpen = false;
      alternateSetupOpen = false;
      specialCaseAfterStepId = null;

      show(editingExisting ? "Workflow updated" : "Workflow saved");

      await loadFlows();
      renderRecording();
      await window.refreshDeviceReadiness?.();

      scrollToSavedFlows();
    }),
  );

  // function localTarget(event) {
  //   return (
  //     event.target?.isContentEditable ||
  //     event.target?.closest?.(
  //       "input, textarea, select, button, a, [role='button']",
  //     )
  //   );
  // }
  // document.addEventListener("keydown", (event) => {
  //   if (
  //     piManual() ||
  //     !recording.active ||
  //     stopping ||
  //     annotationBusy() ||
  //     event.defaultPrevented ||
  //     localTarget(event) ||
  //     document.hidden ||
  //     !document.hasFocus()
  //   )
  //     return;
  //   if (
  //     event.ctrlKey ||
  //     event.metaKey ||
  //     event.altKey ||
  //     event.isComposing ||
  //     event.keyCode === 229 ||
  //     event.getModifierState?.("AltGraph")
  //   )
  //     return;
  //   if (backend !== "mock" && (!active() || !fresh())) return;
  //   if (event.key.length === 1 && event.key >= " " && event.key <= "~") {
  //     event.preventDefault();
  //     addText(event.key);
  //   } else if (
  //     !event.shiftKey &&
  //     ["Enter", "Backspace", "Tab"].includes(event.key)
  //   ) {
  //     event.preventDefault();
  //     if (event.key === "Backspace" && textBuffer)
  //       textBuffer = textBuffer.slice(0, -1);
  //     else {
  //       flushText();
  //       enqueue({ type: "key", key: event.key.toUpperCase() });
  //     }
  //   }
  // });
  // document.addEventListener("paste", (event) => {
  //   if (
  //     piManual() ||
  //     !recording.active ||
  //     stopping ||
  //     annotationBusy() ||
  //     localTarget(event) ||
  //     document.hidden ||
  //     !document.hasFocus()
  //   )
  //     return;
  //   if (backend !== "mock" && (!active() || !fresh())) return;
  //   const value = event.clipboardData?.getData("text/plain") || "";
  //   event.preventDefault();
  //   if (!/^[ -~]*$/.test(value))
  //     return show(
  //       "This phone input currently supports printable English characters only.",
  //       true,
  //     );
  //   addText(value);
  // });

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
        start: window.phonePreviewGestures.point(event, element),
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

      const end = window.phonePreviewGestures.point(event, element);
      if ([...saved.start, ...end].some((v) => v < 0 || v > 1))
        return show("Keep the gesture inside the preview.", true);
      const classified = window.phonePreviewGestures.classify(
        saved.start,
        end,
        element,
        performance.now() - saved.at,
      );
      const swipe = classified.type === "swipe";
      const duration_ms = classified.duration_ms;
      const step = swipe ? { type: "swipe", duration_ms } : { type: "tap" };
      const extra = camera
        ? {
            camera_start: cameraPoint(swipe ? saved.start : end),
            ...(swipe ? { camera_end: cameraPoint(end) } : {}),
            coordinate_space: previewCoordinateSpace(),
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

  function installKeyboardCapture(element, camera) {
    element.addEventListener("click", (event) => {
      const key = currentKeyboardKey();
      if (!recording.workflow || !key || stopping || calibrating) return;
      if (camera && (!fresh() || !state?.calibrated))
        return show(
          "Calibrate a live camera view before capturing keys.",
          true,
        );
      if (!camera && backend !== "mock") return;
      const point = window.phonePreviewGestures.point(event, element);
      if (point.some((value) => value < 0 || value > 1))
        return show("Click inside the phone preview.", true);
      event.preventDefault();
      task(async () => {
        recording = await api("/api/recorder/keyboard-capture/key", {
          key,
          ...(camera
            ? {
                camera_point: cameraPoint(point),
                coordinate_space: previewCoordinateSpace(),
                frame_id: frameId,
              }
            : { point }),
        });
      });
    });
  }

  installKeyboardCapture(canvas, true);
  installKeyboardCapture(preview, false);
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

    blocksInput: () => {
      const reviewSpecialCaseSetup =
        alternateSetupOpen && !!specialCaseAfterStepId && !recording.active;

      return (
        !!recording.workflow &&
        (stopping || (annotationBusy() && !reviewSpecialCaseSetup))
      );
    },

    physicalBusyChanged: (value) => {
      externalPhysicalBusy = value;
      renderRecording();
    },

    refreshAfterReconnect: async () => {
      if (reconnectRefreshInFlight) return;
      reconnectRefreshInFlight = true;
      try {
        if (recording.workflow)
          recording = await api("/api/recorder", undefined, "GET");
        if (running && !terminal.has(running.status))
          running = await api(
            `/api/workflow-runs/${running.run_id}`,
            undefined,
            "GET",
          );
        renderRecording();
        if (running) renderRun();
      } finally {
        reconnectRefreshInFlight = false;
      }
    },

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
      keyboardSetupOpen = false;
      closeExpectedText();
      show(
        `${error.message} ${workflowLiteral(
          "Recording stopped. Input may already have occurred; inspect the phone before continuing. No retry attempted.",
        )}`,
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
        `${error.message} ${workflowLiteral(
          "Recording stopped. Input may already have occurred; inspect the phone before continuing. No retry attempted.",
        )}`,
        true,
      );
      renderRecording();
    },

    ownerId: recorderOwner,
    action: naturalAction,
    deviceStateChanged: () => {
      renderDeviceAvailability();
      loadFlows().catch((error) => show(error.message, true));
    },
    workflowAssignmentsChanged: () => loadFlows(),
    dashboardModeChanged: () => loadFlows(),
    pausePolling: () => {
      workflowPollingStopped = true;
      clearTimeout(runPollTimer);
      runPollTimer = null;
    },

    resumePolling: () => {
      workflowPollingStopped = false;
      scheduleRunPoll(0);
    },

    shutdown: () => {
      workflowPollingStopped = true;
      clearTimeout(runPollTimer);
      runPollTimer = null;
      clearTimeout(textTimer);
    },

    deviceManagementBlocked: () =>
      recording.active || (running && !terminal.has(running.status)),
  };

  async function loadFlows() {
    const generation = ++flowLoadGeneration;
    const result = await api("/api/workflows", undefined, "GET");
    if (generation !== flowLoadGeneration) return;

    backend = result.input_backend;
    availableFlows = result.workflows;
    const unrestricted = result.allowed_workflow_ids == null;
    const allowedIds = new Set(result.allowed_workflow_ids || []);
    const adminMode = document.body?.dataset?.dashboardMode === "admin";
    const visibleFlows =
      adminMode || unrestricted
        ? result.workflows
        : result.workflows.filter((flow) => allowedIds.has(flow.id));

    if (selected && !unrestricted && !allowedIds.has(selected.id)) {
      selected = null;
      $("run-variables").replaceChildren();
      workflowUiText($("run-title"), "workflow.run", "Run Flow");
    }

    $("workflow-backend").textContent =
      workflowUi(
        "workflow.backendSummary",
        `Input: ${backend} · Screen: ${result.screen_backend}`,
        {
          input: backend,
          screen: result.screen_backend,
        },
      ) +
      (result.selected_device_id
        ? unrestricted
          ? ` · ${result.selected_device_id}: legacy unrestricted`
          : ` · ${result.selected_device_id}: ${allowedIds.size} assigned`
        : "");

    const rows = visibleFlows.map((flow) => {
      const row = document.createElement("li");
      const label = document.createElement("span");

      workflowUiComputed(label, () => {
        const purpose =
          flow.purpose === "balance_check"
            ? workflowUi("balance.check", "Balance Check")
            : workflowUi("ui.payment", "Payment");
        const created = flow.created_at
          ? new Date(flow.created_at).toLocaleString()
          : "—";
        const updated = flow.updated_at
          ? new Date(flow.updated_at).toLocaleString()
          : "—";
        return workflowUi(
          "workflow.flowSummary",
          `${flow.name} · ${purpose} · ${flow.step_count} steps · ${flow.variable_count} variables · Created ${created} · Updated ${updated}`,
          {
            name: flow.name,
            purpose,
            steps: flow.step_count,
            variables: flow.variable_count,
            created,
            updated,
          },
        );
      });

      // Run workflow
      // Visible in BOTH Operate and Admin.
      // Reuses the existing workflow selection / execution flow.
      const select = document.createElement("button");
      select.type = "button";
      workflowUiText(select, "workflow.runAction", "Run…");
      select.disabled = !unrestricted && !allowedIds.has(flow.id);
      if (select.disabled)
        select.title = workflowUi(
          "workflow.notAssignedTo",
          `Not assigned to ${result.selected_device_id || "the selected phone"}`,
          {
            device:
              result.selected_device_id ||
              workflowLiteral("the selected phone"),
          },
        );

      select.addEventListener("click", () => task(() => selectFlow(flow.id)));

      // Edit workflow
      // Admin only.
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "admin-only";
      workflowUiText(edit, "common.edit", "Edit");

      edit.addEventListener("click", () =>
        task(async () => {
          recording = await api("/api/recorder/edit", {
            workflow_id: flow.id,
          });

          selected = null;
          timelineSignature = "";
          editingId = null;

          show(
            workflowUi(
              "workflow.editing",
              `Editing “${recording.workflow.name}”`,
              { name: recording.workflow.name },
            ),
          );

          renderRecording();

          $("review-flow").scrollIntoView?.({
            block: "start",
            behavior: "smooth",
          });
        }),
      );

      // Delete workflow
      // Admin only.
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "admin-only stop";
      workflowUiText(remove, "common.delete", "Delete");

      remove.addEventListener("click", () =>
        task(async () => {
          if (
            !window.confirm(
              workflowUi(
                "workflow.deleteConfirm",
                `Delete workflow “${flow.name}”?`,
                { name: flow.name },
              ),
            )
          ) {
            return;
          }

          await api("/api/workflows/" + flow.id, {}, "DELETE");

          // If the deleted workflow is currently selected for running,
          // clear the run panel.
          if (selected?.id === flow.id) {
            selected = null;

            $("run-variables").replaceChildren();
            workflowUiText($("run-title"), "workflow.run", "Run Flow");
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
    workflowUiText(
      $("run-title"),
      "workflow.runNamed",
      "Run: " + selected.name,
      { name: selected.name },
    );
    const fields = selected.variables
      .filter((variable) => variable.required && variable.default == null)
      .map((variable) => {
        const label = document.createElement("label"),
          input = document.createElement("input");
        label.textContent = variable.label;
        input.name = variable.name;
        input.type = variable.type === "number" ? "number" : "text";
        if (input.type === "number") input.step = "any";
        input.required = true;
        input.value = "";
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
        if (!definition.required || definition.default != null) continue;
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
      scheduleRunPoll(0);
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
        scheduleRunPoll();
      }),
    );

  function drawPreview(step) {
    paint.fillStyle = "#142b24";
    paint.fillRect(0, 0, preview.width, preview.height);
    paint.strokeStyle = "#6ef0ab";
    paint.fillStyle = "#e0ffed";
    paint.lineWidth = 4;
    paint.font = "14px sans-serif";
    paint.fillText(workflowLiteral("Phone preview"), 16, 28);
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
    const nextRunStateSignature = `${running.run_id}:${running.status}`;
    if (nextRunStateSignature !== runStateSignature) {
      runStateSignature = nextRunStateSignature;
      window.phoneRuntimeStateChanged?.(running);
      if (terminal.has(running.status)) void window.refreshDeviceReadiness?.();
    }
    workflowUiComputed($("flow-run-state"), () => {
      const detail = `${
        running.preview
          ? describe(running.preview, (running.step_index || 1) - 1)
          : workflowUi("common.starting", "Starting")
      }${running.checkpoint_message ? " — " + workflowError(running.checkpoint_message) : ""}${
        terminal.has(running.status)
          ? " — " +
            workflowBackendError(
              running.error_code,
              running.error_params,
              running.message || "",
            )
          : ""
      }`;
      return workflowUi(
        "workflow.runState",
        `${workflowStatus(running.status)} · Step ${running.step_index || 0}/${running.step_count || 0} · ${detail}`,
        {
          status: workflowStatus(running.status),
          current: running.step_index || 0,
          total: running.step_count || 0,
          detail,
        },
      );
    });
    $("flow-resume").hidden = running.status !== "waiting_manual";
    $("flow-cancel").disabled = terminal.has(running.status);
    $("workflow-logs").textContent = (running.logs || []).join("\n");
    $("operate-workflow-log").textContent = (running.logs || [])
      .slice(-6)
      .join("\n");
    drawPreview(running.preview);
    renderRecording();
  }

  window.addEventListener?.("phonecontroller:languagechange", () => {
    renderRecording();
    if (running) renderRun();
  });

  function scheduleRunPoll(delay = 500) {
    if (
      workflowPollingStopped ||
      runPollTimer !== null ||
      !running ||
      terminal.has(running.status)
    )
      return;
    runPollTimer = setTimeout(pollRun, delay);
  }

  async function pollRun() {
    runPollTimer = null;
    if (
      workflowPollingStopped ||
      runPollInFlight ||
      !running ||
      terminal.has(running.status)
    )
      return;
    runPollInFlight = true;
    try {
      running = await api(
        `/api/workflow-runs/${running.run_id}`,
        undefined,
        "GET",
      );
      renderRun();
    } catch (error) {
      show(error.message, true);
    } finally {
      runPollInFlight = false;
      scheduleRunPoll();
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
        scheduleRunPoll();
      } catch {
        localStorage.removeItem("phone-workflow-run");
      }
    }
    drawPreview(running?.preview);
  });
})();
