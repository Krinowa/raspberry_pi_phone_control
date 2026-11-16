"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = readFileSync(
  path.join(__dirname, "../static/i18n.js"),
  "utf8",
);

function loadI18n(storedLocale = null) {
  const values = new Map();
  if (storedLocale !== null)
    values.set("phone-controller.locale", storedLocale);
  const events = [];
  const window = {
    localStorage: {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
    },
    CustomEvent: class CustomEvent {
      constructor(type, options) {
        this.type = type;
        this.detail = options?.detail;
      }
    },
    dispatchEvent: (event) => events.push(event),
  };
  const context = vm.createContext({ window });
  vm.runInContext(source, context);
  return { i18n: window.PhoneI18n, values, events };
}

test("defaults to English when no valid preference exists", () => {
  const { i18n } = loadI18n();
  assert.equal(i18n.getLocale(), "en");
  assert.equal(i18n.t("device.ready"), "Ready");
  assert.equal(i18n.t("withdraw.loginAgain"), "Login Again");
});

test("switches immediately to Hong Kong Traditional Chinese and persists it", () => {
  const { i18n, values, events } = loadI18n();
  const node = { textContent: "", isConnected: true };
  i18n.bindText(node, "withdraw.loginAgain");
  assert.equal(node.textContent, "Login Again");

  i18n.setLocale("zh-HK");
  assert.equal(node.textContent, "重新登入");
  assert.equal(values.get("phone-controller.locale"), "zh-HK");
  assert.equal(events.at(-1).detail.locale, "zh-HK");

  i18n.setLocale("en");
  assert.equal(node.textContent, "Login Again");
  assert.equal(values.get("phone-controller.locale"), "en");
});

test("loads the persisted locale on the next launch", () => {
  const { i18n } = loadI18n("zh-HK");
  assert.equal(i18n.getLocale(), "zh-HK");
  assert.equal(i18n.t("balance.refresh"), "更新餘額");
});

test("missing zh-HK entries fall back to English", () => {
  const { i18n } = loadI18n("zh-HK");
  i18n.dictionaries.en["test.englishOnly"] = "English fallback";
  assert.equal(i18n.t("test.englishOnly"), "English fallback");
});

test("dynamic status text changes language without changing workflow or OCR data", () => {
  const { i18n } = loadI18n();
  const status = { textContent: "", isConnected: true };
  const workflow = {
    id: "flow_zh_test",
    name: "Pay & Transfer",
    expected_text: "確認付款",
    username: "operator_01",
  };
  const original = structuredClone(workflow);

  i18n.bindText(status, "controller.reconnecting");
  i18n.setLocale("zh-HK");

  assert.equal(status.textContent, "● 控制器重新連線中");
  assert.deepEqual(workflow, original);
});

test("runtime machine statuses render in zh-HK without leaking translation keys", () => {
  const { i18n } = loadI18n("zh-HK");
  const expected = {
    COMPLETED: "已完成",
    RUNNING: "執行中",
    FAILED: "失敗",
    QUEUED: "排隊中",
    WAITING_FOR_DEVICE: "等待裝置",
    CANCELLED: "已取消",
    INTERRUPTED: "已中斷",
    NEEDS_ATTENTION: "需要處理",
    IDLE: "閒置",
    CONNECTING: "連線中",
    DISCONNECTED: "已中斷連線",
  };

  for (const [status, translation] of Object.entries(expected)) {
    assert.equal(i18n.translateStatus(status), translation);
    assert.notEqual(i18n.translateStatus(status), `status.${status}`);
  }
  assert.equal(i18n.translateStatus("VENDOR_STATE"), "VENDOR_STATE");
});

test("missed runtime messages and OCR worker text translate in zh-HK", () => {
  const { i18n } = loadI18n("zh-HK");
  assert.equal(i18n.t("withdraw.authRequired"), "需要重新驗證");
  assert.equal(i18n.t("common.disconnected"), "已中斷連線");
  assert.equal(
    i18n.translateEnglish("Workflow completed."),
    "工作流程已完成。",
  );
  assert.equal(
    i18n.translateEnglish("Collecting recent OCR performance samples."),
    "正在收集近期 OCR 效能樣本。",
  );
  assert.equal(
    i18n.t("ocr.workerStatus", {
      number: 2,
      state: i18n.translateStatus("IDLE"),
    }),
    "工作程序 2 — 閒置",
  );
  assert.equal(
    i18n.t("ocr.poolClamped", { configured: 12, ceiling: 4, started: 4 }),
    "設定的 OCR 工作程序池大小 12 超出偵測到的安全上限 4；已啟動 4 個工作程序。已儲存的設定未有更改。",
  );
});

test("all OCR pool recommendation reasons have zh-HK display text", () => {
  const { i18n } = loadI18n("zh-HK");
  const reasons = [
    "Current pool exceeds the detected hardware safety ceiling.",
    "Sustained CPU saturation and slow OCR inference indicate contention.",
    "Current workers already match the hardware safety ceiling.",
    "CPU headroom is too low to add another OCR worker.",
    "Memory headroom is too low to add another OCR worker.",
    "Collecting recent OCR performance samples.",
    "OCR queue pressure is low.",
    "Workers are frequently idle despite recent queue delay.",
    "Inference slowed after the last increase without enough queue improvement.",
    "Sustained OCR queue wait has available CPU and memory headroom.",
    "CPU usage is elevated; keep the current worker count.",
  ];

  for (const reason of reasons)
    assert.notEqual(i18n.translateEnglish(reason), reason, reason);
});

test("numeric HTML entities are decoded once at the translation boundary", () => {
  const { i18n } = loadI18n("zh-HK");
  const cases = {
    "test.cpu": "&#x43;PU 工作程序上限",
    "test.memory": "&#x8A18;憶體使用量",
    "test.ram": "&#x53EF;用 RAM",
  };

  for (const [key, encoded] of Object.entries(cases)) {
    i18n.dictionaries.en[key] = encoded;
    i18n.dictionaries["zh-HK"][key] = encoded;
    const node = { textContent: "", isConnected: true };
    i18n.bindText(node, key);
    assert.equal(node.textContent.includes("&#x"), false);
  }
  assert.equal(i18n.t("test.cpu"), "CPU 工作程序上限");
  assert.equal(i18n.t("test.memory"), "記憶體使用量");
  assert.equal(i18n.t("test.ram"), "可用 RAM");
});

test("unsupported locales resolve to English", () => {
  const { i18n } = loadI18n("zh-CN");
  assert.equal(i18n.getLocale(), "en");
  assert.equal(i18n.setLocale("fr"), "en");
});

test("Hong Kong translations cover dashboard, recovery, camera and workflow UI", () => {
  const { i18n } = loadI18n("zh-HK");
  assert.equal(i18n.t("health.title"), "運作狀況");
  assert.equal(i18n.t("nav.operate"), "操作模式");
  assert.equal(i18n.t("nav.admin"), "管理模式");
  assert.equal(i18n.t("device.ready"), "就緒");
  assert.equal(i18n.t("camera.refresh"), "更新相機清單");
  assert.equal(i18n.t("withdraw.changeAccount"), "更改帳戶");
  assert.equal(i18n.t("workflow.step.screenshot"), "擷取螢幕截圖");
  assert.equal(i18n.t("settings.ocr"), "OCR 設定");
  assert.equal(i18n.t("auth.username"), "用戶名稱");
  assert.equal(i18n.translateEnglish("Capture this key"), "擷取此按鍵");
  assert.equal(i18n.translateEnglish("Save Changes"), "儲存變更");
  assert.equal(
    i18n.t("error.backendUnavailable", { message: "timeout" }),
    "後端無法使用：timeout",
  );
});

test("recent workflow features have complete Hong Kong translations", () => {
  const { i18n } = loadI18n("zh-HK");
  const expected = {
    "Special Case": "特殊情況",
    "Screen Text": "畫面文字",
    "Withdrawal API": "提款 API",
    "Transfer Method": "轉賬方式",
    "Bank Account": "銀行戶口",
    "Mobile Number": "手提電話號碼",
    "FPS Phone Number": "轉數快手提電話號碼",
    "FPS Email Address": "轉數快電郵地址",
    "FPS ID": "轉數快識別碼",
    "Holder Name": "持有人姓名",
    "Record Branch": "錄製分支",
    "Manual Simulation": "手動模擬",
    "Verify Text": "驗證文字",
    "Expect Current Screen": "驗證目前畫面",
    "Include digits (0–9)": "包含數字（0–9）",
    "Unsupported Transfer Method": "不支援的轉賬方式",
    "A Transfer Method branch must not be empty.":
      "轉賬方式分支不得為空。",
    "Enable only when digits remain visible on the same keyboard layout. Layout switching is not supported.":
      "只應在數字於同一鍵盤版面持續顯示時啟用。不支援切換鍵盤版面。",
    "Transfer-method condition contains an unsupported branch.":
      "轉賬方式條件包含不支援的分支。",
  };

  for (const [english, traditionalChinese] of Object.entries(expected))
    assert.equal(i18n.translateEnglish(english), traditionalChinese, english);
  assert.equal(
    i18n.t("workflow.recordingTransferBranch", { method: "銀行戶口" }),
    "正在錄製 銀行戶口 分支",
  );
});

test("configured Unicode labels and technical transfer values remain unchanged", () => {
  const { i18n } = loadI18n("zh-HK");
  const configured = {
    value: "fpx",
    label: "FPX 即時轉賬",
    identifier_field: "account_number",
  };
  const original = structuredClone(configured);

  assert.equal(i18n.translateEnglish(configured.label), configured.label);
  assert.deepEqual(configured, original);
  assert.equal(configured.value, "fpx");
  assert.equal(configured.identifier_field, "account_number");
});

test("transfer-method validation localizes around unchanged identifiers", () => {
  const { i18n } = loadI18n("zh-HK");
  assert.equal(
    i18n.translateError(
      "Transfer-method branch bank_account must verify account_number.",
    ),
    "轉賬方式分支 bank_account 必須驗證 account_number。",
  );
  assert.equal(
    i18n.translateError(
      "Payment workflow has no explicit branch for transfer_method fpx.",
    ),
    "付款工作流程未有為 transfer_method fpx 設定明確分支。",
  );
});

test("Hong Kong translations cover every Region OCR label", () => {
  const { i18n } = loadI18n("zh-HK");
  assert.equal(i18n.t("workflow.roi.enable"), "啟用區域 OCR");
  assert.equal(
    i18n.t("workflow.roi.instructions"),
    "請在目前已校正的螢幕截圖上選取區域；手機畫面變更後請重新整理。",
  );
  assert.equal(i18n.t("workflow.roi.selectCurrent"), "在目前畫面上選取");
  assert.equal(i18n.t("workflow.roi.clear"), "清除 OCR 區域");
  assert.equal(i18n.t("workflow.roi.width"), "寬度");
  assert.equal(i18n.t("workflow.roi.height"), "高度");
  assert.equal(i18n.t("workflow.roi.test"), "測試 ROI OCR");
  assert.equal(i18n.translateEnglish("No OCR region selected."), "未選擇 OCR 區域。");
});

test("computed dynamic UI re-renders while identifiers and OCR text stay exact", () => {
  const { i18n } = loadI18n();
  const node = { textContent: "", isConnected: true };
  const data = {
    device_id: "phone_B",
    workflow_id: "flow_transfer_01",
    expected_text: "Transfer HK$100.00",
  };
  const original = structuredClone(data);
  i18n.bindComputedText(node, () =>
    i18n.t("device.controllingNamed", { name: data.device_id }),
  );

  assert.equal(node.textContent, "Controlling: phone_B");
  i18n.setLocale("zh-HK");
  assert.equal(node.textContent, "正在控制：phone_B");
  assert.deepEqual(data, original);
});

test("translates known backend error codes without changing technical values", () => {
  const { i18n } = loadI18n();
  const path = "/devices/phone_A/status";
  const status = { textContent: "", isConnected: true };
  i18n.bindComputedText(status, () =>
    i18n.translateBackendError(
      "pi_api_timeout",
      { path },
      `Pi API ${path} timed out.`,
    ),
  );
  assert.equal(status.textContent, `Pi API ${path} timed out.`);

  i18n.setLocale("zh-HK");
  assert.equal(status.textContent, `Pi API ${path} 請求逾時。`);

  assert.equal(
    i18n.translateBackendError(
      "pi_api_timeout",
      { path },
      `Pi API ${path} timed out.`,
    ),
    `Pi API ${path} 請求逾時。`,
  );
  assert.equal(
    i18n.translateBackendError(
      "pi_api_http_error",
      { path, status: 504 },
      `Pi API ${path} returned HTTP 504.`,
    ),
    `Pi API ${path} 傳回 HTTP 504。`,
  );
  assert.equal(
    i18n.translateBackendError(
      "pi_api_unreachable",
      { path },
      `Cannot reach Pi API for ${path}. Check the Pi address and private Wi-Fi.`,
    ),
    `無法連接 ${path} 的 Pi API。請檢查 Pi 位址及私人 Wi-Fi。`,
  );
});

test("unknown or missing backend codes preserve the raw backend fallback", () => {
  const { i18n } = loadI18n("zh-HK");
  const raw = "Vendor-specific backend detail";
  assert.equal(
    i18n.translateBackendError("vendor_specific_failure", {}, raw),
    raw,
  );
  assert.equal(i18n.translateBackendError(null, null, raw), raw);
});

test("Pi API physical-action uncertainty is translated but the path is unchanged", () => {
  const { i18n } = loadI18n("zh-HK");
  assert.equal(
    i18n.translateBackendError(
      "pi_api_timeout",
      { path: "/devices/phone_A/tap", input_may_have_occurred: true },
      "Pi API /devices/phone_A/tap timed out. Input may have occurred; no retry attempted.",
    ),
    "Pi API /devices/phone_A/tap 請求逾時。 輸入可能已經發生；系統沒有嘗試重試。",
  );
});
