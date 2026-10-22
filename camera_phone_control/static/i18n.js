(function (root) {
  "use strict";

  const STORAGE_KEY = "phone-controller.locale";
  const DEFAULT_LOCALE = "en";
  const SUPPORTED_LOCALES = new Set(["en", "zh-HK"]);

  const en = {
    "language.label": "Language",
    "language.en": "English",
    "language.zhHK": "繁體中文（香港）",
    "app.title": "Phone · Local control",
    "app.eyebrow": "PHONE CONTROL / EXTERNAL CAMERA",
    "app.localControl": "Local control",
    "nav.dashboardMode": "Dashboard mode",
    "nav.operate": "Operate",
    "nav.admin": "Admin",
    "common.checking": "Checking…",
    "common.starting": "Starting…",
    "common.loading": "Loading…",
    "common.available": "Available",
    "common.unavailable": "Unavailable",
    "common.disabled": "Disabled",
    "common.unassigned": "Unassigned",
    "common.connected": "Connected",
    "common.disconnected": "Disconnected",
    "common.authenticated": "Authenticated",
    "common.authenticating": "Authenticating",
    "common.reconnecting": "Reconnecting",
    "common.ready": "Ready",
    "common.problem": "Problem",
    "common.healthy": "Healthy",
    "common.yes": "Yes",
    "common.no": "No",
    "common.none": "None",
    "common.save": "Save",
    "common.cancel": "Cancel",
    "common.delete": "Delete",
    "common.deleting": "Deleting…",
    "common.edit": "Edit",
    "common.change": "Change",
    "common.continue": "Continue",
    "common.close": "Close",
    "common.select": "Select",
    "common.controlling": "Controlling",
    "common.notConfigured": "Not configured",
    "controller.checking": "● Checking controller…",
    "controller.online": "● Controller Online",
    "controller.reconnecting": "● Controller Reconnecting",
    "controller.interrupted":
      "Connection interrupted. Retrying automatically...",
    "health.systemStatus": "SYSTEM STATUS",
    "health.title": "Operational health",
    "health.controller": "Controller",
    "health.raspberryPi": "Raspberry Pi",
    "health.hid": "HID",
    "health.camera": "Camera",
    "health.ocr": "OCR",
    "health.needsAttention": "Needs Attention",
    "health.needsAttentionWarning": "⚠ Needs Attention",
    "health.resolveIssue": "Resolve Issue",
    "health.issue": "Issue",
    "health.recommendedAction": "Recommended action",
    "health.reviewRecovery": "Review the available recovery options.",
    "health.recoverPhone": "Recover Phone",
    "health.viewDetails": "View Details",
    "health.resolvePhone": "Resolve Phone Issue",
    "device.title": "Devices",
    "device.ready": "Ready",
    "device.description":
      "Monitor every phone camera and choose one control target.",
    "device.add": "Add Phone",
    "device.loading": "Loading registered phones…",
    "device.logicalId": "Logical device ID",
    "device.startEnrollment": "Start Enrollment",
    "device.stopEnrollment": "Stop Enrollment",
    "device.waitingPair": "Waiting for phone to pair",
    "device.noRegistered": "No phones are registered.",
    "device.statusUnavailable": "Status unavailable",
    "device.phoneDisconnected": "Phone disconnected",
    "device.busy": "Device busy",
    "device.cameraSettings": "Camera Settings",
    "device.manualAssistance": "Manual Assistance",
    "device.startManual": "Start Manual Control",
    "device.enableManual": "Enable Manual Control",
    "device.phoneState.ready": "Phone state: Ready",
    "device.phoneState.recovering": "Phone state: Recovering",
    "device.phoneState.attention": "Phone state: Needs Attention",
    "device.currentJob.none": "Current job: —",
    "device.currentJob": "Current job: {status}",
    "device.lastJob": "Last job: {status}",
    "device.error": "Error: {message}",
    "device.result": "Result: {result}",
    "device.usernameAccount": "Username: {username} · Account ID: {accountId}",
    "device.paymentRecords": "Payment Records: {records}",
    "device.failedStep": "Failed step: Step {number}{type}",
    "device.enrolling": "Registering {deviceId}",
    "device.enrollmentRemaining": "{seconds} seconds remaining",
    "device.enrollmentActive": "Enrollment window active",
    "device.pairBluetooth": "Pair the phone from its Bluetooth settings",
    "device.multipleAttention": "{count} devices need attention",
    "device.controllingNamed": "Controlling: {name}",
    "device.deleteConfirm":
      "Delete {deviceId}? This will unregister and unpair this phone from the Raspberry Pi.",
    "device.deleted": "Deleted {deviceId} from the Raspberry Pi.",
    "device.count":
      "{count} registered phone{suffix}. All cameras remain visible; one phone receives control.",
    "device.statusRetrying":
      "Device status unavailable. Retrying automatically.",
    "device.noneOnPi": "No phones are registered with this Pi yet.",
    "device.default": "Default",
    "device.controlTarget": "Control target",
    "device.running": "Running",
    "device.offline": "Offline",
    "device.bluetoothStatus": "Bluetooth: ● {status}",
    "device.bluetoothUnavailable": "Bluetooth: ? Status unavailable",
    "device.cameraStatus": "Camera: ● {status}",
    "device.cameraUnavailableStatus": "Camera: ? Status unavailable",
    "device.workStatus": "Work status",
    "device.readyMark": "● Ready",
    "device.notReadyMark": "● Not Ready",
    "device.setNotReady": "Set Not Ready",
    "device.markReady": "Mark Ready",
    "device.updating": "Updating…",
    "camera.title": "Camera view",
    "camera.waiting": "Waiting for camera access…",
    "camera.unavailable": "Camera unavailable",
    "camera.source": "Camera source",
    "camera.refresh": "Refresh Cameras",
    "camera.test": "Test Camera",
    "camera.save": "Save Camera",
    "camera.settings": "Camera Settings",
    "camera.calibrationRequired": "Calibration required",
    "camera.saveCalibration": "Save calibration",
    "camera.loading": "Loading camera…",
    "camera.noneAssigned": "No camera assigned",
    "camera.named": "Camera {id}",
    "camera.sourceOption": "{name} — {status}",
    "camera.savedUnavailable": "Saved source {source} — Unavailable",
    "camera.settingsNamed": "Camera Settings · {name}",
    "camera.savedFor": "Camera settings saved for {deviceId}.",
    "withdraw.title": "Withdraw Account",
    "withdraw.loginAssign": "Login & Assign",
    "withdraw.loginAgain": "Login Again",
    "withdraw.changeAccount": "Change Account",
    "withdraw.reconnect": "Reconnect",
    "withdraw.reconnecting": "Reconnecting…",
    "withdraw.authRequired": "Authentication required",
    "withdraw.configRequired": "Configuration required",
    "withdraw.cancel": "Cancel Withdrawal",
    "withdraw.cancelling": "Cancelling Withdrawal…",
    "withdraw.confirmCancel": "Confirm Cancel",
    "withdraw.confirmTitle": "Cancel this withdrawal?",
    "withdraw.confirmYes": "Yes, Cancel Withdrawal",
    "withdraw.assigned": "Assigned",
    "withdraw.notAssigned": "Not Assigned",
    "withdraw.paymentReady": "Payment readiness: Ready",
    "withdraw.paymentNotReady": "Payment readiness: Not ready",
    "withdraw.accountStatus": "Account Status: {status}",
    "withdraw.sessionStatus": "Session: {session} · Realtime: {realtime}",
    "withdraw.usernameIdentity":
      "Username: {username} · Account ID: {accountId} · Payment Records: {records}",
    "withdraw.titleNamed": "Withdraw Account · {name}",
    "withdraw.cancelNamed": "Cancel Withdrawal · {name}",
    "balance.title": "Balance",
    "balance.available": "Available Balance",
    "balance.check": "Balance Check",
    "balance.refresh": "Refresh Balance",
    "balance.refreshing": "Refreshing…",
    "balance.requesting": "Requesting…",
    "balance.checking": "Checking…",
    "balance.stale": "Stale",
    "balance.failed": "Balance check failed",
    "balance.assignWorkflow": "Assign a Balance Check Workflow first.",
    "balance.lastChecked": "Last checked: {time}",
    "workflow.assignment": "Workflow Assignment",
    "workflow.payment": "Payment Workflow",
    "workflow.balance": "Balance Check Workflow",
    "workflow.save": "Save Workflow",
    "workflow.saved": "Workflow saved",
    "workflow.updated": "Workflow updated",
    "workflow.startRecording": "● Start Recording",
    "workflow.stopRecording": "■ Stop Recording",
    "workflow.noSteps": "No steps recorded",
    "workflow.savedFlows": "Saved Flows",
    "workflow.startRun": "Start Run",
    "workflow.stopRun": "Stop Run",
    "workflow.run": "Run Flow",
    "workflow.edit": "Edit Workflow",
    "workflow.addStep": "Add Step",
    "workflow.editStep": "Edit Step",
    "workflow.addCheck": "Add Check",
    "workflow.readBalance": "Read Balance",
    "workflow.addSpecialCase": "Add Special Case",
    "workflow.addExpectedText": "Add Expected Text",
    "workflow.expectedText": "Expected text: {text}",
    "workflow.notStarted": "Not started",
    "workflow.technicalDetails": "Technical details",
    "workflow.runAction": "Run…",
    "workflow.runNamed": "Run: {name}",
    "workflow.editing": "Editing “{name}”",
    "workflow.deleteConfirm": "Delete workflow “{name}”?",
    "workflow.flowSummary":
      "{name} · {purpose} · {steps} steps · {variables} variables · Created {created} · Updated {updated}",
    "workflow.settingsNamed": "{type} · {name}",
    "workflow.step.tap": "Tap",
    "workflow.step.swipe": "Swipe {direction}",
    "workflow.step.typeText": "Type Text",
    "workflow.step.wait": "Wait {seconds} seconds",
    "workflow.step.waitText":
      "Wait for Text — “{text}” · Wait up to {seconds} sec",
    "workflow.step.readBalance": "Read Balance — {label}",
    "workflow.step.screenshot": "Capture Screenshot",
    "workflow.step.checkpoint": "Manual checkpoint",
    "workflow.direction.right": "Right",
    "workflow.direction.left": "Left",
    "workflow.direction.down": "Down",
    "workflow.direction.up": "Up",
    "workflow.assign": "Assign",
    "workflow.noAssigned": "No workflow assigned",
    "workflow.notAssigned": "Not assigned",
    "workflow.chooseOne": "Choose one workflow",
    "workflow.executionDisabled": "Execution disabled",
    "workflow.missing": "Missing",
    "workflow.retryFailed": "Retry from Failed Step",
    "workflow.restart": "Restart Workflow",
    "workflow.cancellationRequested": "Cancellation requested",
    "workflow.cancelling": "Cancelling…",
    "settings.title": "Settings",
    "settings.ocr": "OCR Settings",
    "status.loadingJobs": "Loading jobs…",
    "status.noJobs": "No external jobs have been submitted.",
    "status.noLogs": "No matching application logs.",
    "status.inUse": "In Use",
    "status.QUEUED": "QUEUED",
    "status.WAITING_FOR_DEVICE": "WAITING_FOR_DEVICE",
    "status.RUNNING": "RUNNING",
    "status.FAILED": "FAILED",
    "status.INTERRUPTED": "INTERRUPTED",
    "status.CANCELLED": "CANCELLED",
    "status.passed": "passed",
    "status.failed": "failed",
    "status.cancelled": "cancelled",
    "status.waiting_manual": "waiting for manual input",
    "jobs.summary": "Showing latest {shown} of {count} jobs.",
    "jobs.account": "Account: {account}{bank}",
    "jobs.device": "Device: {deviceId}",
    "jobs.workflow": "Workflow: {workflow}{action}",
    "jobs.queued": "Queued: {time}",
    "jobs.finished": "Finished: {time}",
    "jobs.started": "Started: {time}",
    "jobs.attempts": "Attempts: {count}",
    "error.requestFailed": "Request failed ({status})",
    "error.cancelJob": "Could not cancel job: {message}",
    "error.retryJob": "Could not create retry: {message}",
    "error.ocrUnavailable": "OCR unavailable",
    "error.piUnavailable": "Raspberry Pi unavailable",
    "error.jobInterrupted": "Job interrupted",
    "error.piApiTimeout": "Pi API {path} timed out.",
    "error.piApiNoSocket": "Pi API {path} connected without a socket.",
    "error.piApiHttp": "Pi API {path} returned HTTP {status}.",
    "error.piApiTooLarge": "Pi API response exceeded the size limit.",
    "error.piApiInvalidJson": "Pi API {path} returned invalid JSON.",
    "error.piApiInvalidResponse":
      "Pi API {path} returned an invalid response (expected boolean ok).",
    "error.piRejectedService":
      "Pi rejected {path}. Check the Pi service and phone connection.",
    "error.piRejectedHid":
      "Pi rejected {path}. Check the Bluetooth HID connection to the phone.",
    "error.piApiUnreachable":
      "Cannot reach Pi API for {path}. Check the Pi address and private Wi-Fi.",
    "error.piStatusInvalid":
      "Pi /status response is missing boolean connected.",
    "error.physicalMayHaveOccurred":
      " Input may have occurred; no retry attempted.",
    "error.browserTimeout": "The request timed out.",
    "error.networkRequestFailed": "The network request failed.",
    "auth.title": "Phone Controller · Authentication",
    "auth.controller": "PHONE CONTROLLER",
    "auth.setupAdmin": "Set up administrator",
    "auth.createAdmin":
      "Create the local administrator account for this computer.",
    "auth.username": "Username",
    "auth.password": "Password",
    "auth.confirmPassword": "Confirm password",
    "auth.setupAuthenticator": "Set up authenticator",
    "auth.scanQr":
      "Scan this QR code with Google Authenticator, Microsoft Authenticator, or another compatible app.",
    "auth.qrAlt": "Authenticator setup QR code",
    "auth.manualKey": "Manual key:",
    "auth.sixDigit": "6-digit code",
    "auth.verifyFinish": "Verify and finish setup",
    "auth.storeCodes":
      "Store these one-time codes securely. They will not be shown again.",
    "auth.continueAdmin": "Continue to Admin",
    "auth.adminLogin": "Administrator login",
    "auth.twoFactor": "Two-factor verification",
    "auth.enterAuthenticator": "Enter the current authenticator code for",
    "auth.login": "Login",
    "auth.useRecovery": "Use recovery code",
    "auth.enterRecovery": "Enter one unused recovery code for",
    "auth.recoveryCode": "Recovery code",
    "auth.useAuthenticator": "Use authenticator code",
  };

  const zhHK = {
    "language.label": "語言",
    "language.en": "English",
    "language.zhHK": "繁體中文（香港）",
    "app.title": "手機控制器 · 本機控制",
    "app.eyebrow": "手機控制／外置相機",
    "app.localControl": "本機控制",
    "nav.dashboardMode": "控制台模式",
    "nav.operate": "操作",
    "nav.admin": "管理",
    "common.checking": "檢查中…",
    "common.starting": "啟動中…",
    "common.loading": "載入中…",
    "common.available": "可用",
    "common.unavailable": "無法使用",
    "common.disabled": "已停用",
    "common.unassigned": "未指派",
    "common.connected": "已連線",
    "common.disconnected": "已中斷連線",
    "common.authenticated": "已驗證",
    "common.authenticating": "驗證中",
    "common.reconnecting": "重新連線中",
    "common.ready": "就緒",
    "common.problem": "異常",
    "common.healthy": "正常",
    "common.yes": "是",
    "common.no": "否",
    "common.none": "沒有",
    "common.save": "儲存",
    "common.cancel": "取消",
    "common.delete": "刪除",
    "common.deleting": "刪除中…",
    "common.edit": "編輯",
    "common.change": "更改",
    "common.continue": "繼續",
    "common.close": "關閉",
    "common.select": "選擇",
    "common.controlling": "控制中",
    "common.notConfigured": "未設定",
    "controller.checking": "● 正在檢查控制器…",
    "controller.online": "● 控制器已連線",
    "controller.reconnecting": "● 控制器重新連線中",
    "controller.interrupted": "連線中斷，正在自動重試…",
    "health.systemStatus": "系統狀態",
    "health.title": "運作狀況",
    "health.controller": "控制器",
    "health.raspberryPi": "Raspberry Pi",
    "health.hid": "HID",
    "health.camera": "相機",
    "health.ocr": "OCR",
    "health.needsAttention": "需要處理",
    "health.needsAttentionWarning": "⚠ 需要處理",
    "health.resolveIssue": "處理問題",
    "health.issue": "問題",
    "health.recommendedAction": "建議操作",
    "health.reviewRecovery": "請查看可用的復原選項。",
    "health.recoverPhone": "復原手機",
    "health.viewDetails": "查看詳情",
    "health.resolvePhone": "處理手機問題",
    "device.title": "裝置",
    "device.ready": "就緒",
    "device.description": "監察所有手機相機，並選擇要控制的手機。",
    "device.add": "新增手機",
    "device.loading": "正在載入已登記的手機…",
    "device.logicalId": "邏輯裝置 ID",
    "device.startEnrollment": "開始配對",
    "device.stopEnrollment": "停止配對",
    "device.waitingPair": "等待手機配對",
    "device.noRegistered": "尚未登記任何手機。",
    "device.statusUnavailable": "無法取得狀態",
    "device.phoneDisconnected": "手機已中斷連線",
    "device.busy": "裝置忙碌中",
    "device.cameraSettings": "相機設定",
    "device.manualAssistance": "手動協助",
    "device.startManual": "開始手動控制",
    "device.enableManual": "啟用手動控制",
    "device.phoneState.ready": "手機狀態：就緒",
    "device.phoneState.recovering": "手機狀態：復原中",
    "device.phoneState.attention": "手機狀態：需要處理",
    "device.currentJob.none": "目前工作：—",
    "device.currentJob": "目前工作：{status}",
    "device.lastJob": "上一項工作：{status}",
    "device.error": "錯誤：{message}",
    "device.result": "結果：{result}",
    "device.usernameAccount": "用戶名稱：{username} · 帳戶 ID：{accountId}",
    "device.paymentRecords": "付款記錄：{records}",
    "device.failedStep": "失敗步驟：第 {number} 步{type}",
    "device.enrolling": "正在登記 {deviceId}",
    "device.enrollmentRemaining": "尚餘 {seconds} 秒",
    "device.enrollmentActive": "配對時段已開啟",
    "device.pairBluetooth": "從手機的藍牙設定進行配對",
    "device.multipleAttention": "{count} 部裝置需要處理",
    "device.controllingNamed": "正在控制：{name}",
    "device.deleteConfirm":
      "要刪除 {deviceId} 嗎？這會從 Raspberry Pi 取消登記並解除配對此手機。",
    "device.deleted": "已從 Raspberry Pi 刪除 {deviceId}。",
    "device.count":
      "已登記 {count} 部手機。所有相機畫面會保持顯示；只有一部手機會接收控制。",
    "device.statusRetrying": "無法取得裝置狀態，正在自動重試。",
    "device.noneOnPi": "此 Pi 尚未登記任何手機。",
    "device.default": "預設",
    "device.controlTarget": "控制目標",
    "device.running": "執行中",
    "device.offline": "離線",
    "device.bluetoothStatus": "藍牙：● {status}",
    "device.bluetoothUnavailable": "藍牙：? 無法取得狀態",
    "device.cameraStatus": "相機：● {status}",
    "device.cameraUnavailableStatus": "相機：? 無法取得狀態",
    "device.workStatus": "工作狀態",
    "device.readyMark": "● 就緒",
    "device.notReadyMark": "● 未就緒",
    "device.setNotReady": "設為未就緒",
    "device.markReady": "標記為就緒",
    "device.updating": "更新中…",
    "camera.title": "相機畫面",
    "camera.waiting": "等待取得相機權限…",
    "camera.unavailable": "相機無法使用",
    "camera.source": "相機來源",
    "camera.refresh": "更新相機清單",
    "camera.test": "測試相機",
    "camera.save": "儲存相機設定",
    "camera.settings": "相機設定",
    "camera.calibrationRequired": "需要校正",
    "camera.saveCalibration": "儲存校正設定",
    "camera.loading": "正在載入相機…",
    "camera.noneAssigned": "未指派相機",
    "camera.named": "相機 {id}",
    "camera.sourceOption": "{name} — {status}",
    "camera.savedUnavailable": "已儲存的來源 {source} — 無法使用",
    "camera.settingsNamed": "相機設定 · {name}",
    "camera.savedFor": "已儲存 {deviceId} 的相機設定。",
    "withdraw.title": "提款帳戶",
    "withdraw.loginAssign": "登入並指派",
    "withdraw.loginAgain": "重新登入",
    "withdraw.changeAccount": "更改帳戶",
    "withdraw.reconnect": "重新連線",
    "withdraw.reconnecting": "重新連線中…",
    "withdraw.authRequired": "需要驗證",
    "withdraw.configRequired": "需要設定",
    "withdraw.cancel": "取消提款",
    "withdraw.cancelling": "正在取消提款…",
    "withdraw.confirmCancel": "確認取消",
    "withdraw.confirmTitle": "要取消這項提款嗎？",
    "withdraw.confirmYes": "是，取消提款",
    "withdraw.assigned": "已指派",
    "withdraw.notAssigned": "未指派",
    "withdraw.paymentReady": "付款狀態：就緒",
    "withdraw.paymentNotReady": "付款狀態：未就緒",
    "withdraw.accountStatus": "帳戶狀態：{status}",
    "withdraw.sessionStatus": "工作階段：{session} · 即時連線：{realtime}",
    "withdraw.usernameIdentity":
      "用戶名稱：{username} · 帳戶 ID：{accountId} · 付款記錄：{records}",
    "withdraw.titleNamed": "提款帳戶 · {name}",
    "withdraw.cancelNamed": "取消提款 · {name}",
    "balance.title": "餘額",
    "balance.available": "可用餘額",
    "balance.check": "餘額檢查",
    "balance.refresh": "更新餘額",
    "balance.refreshing": "更新中…",
    "balance.requesting": "要求中…",
    "balance.checking": "檢查中…",
    "balance.stale": "資料已過時",
    "balance.failed": "餘額檢查失敗",
    "balance.assignWorkflow": "請先指派餘額檢查工作流程。",
    "balance.lastChecked": "上次檢查：{time}",
    "workflow.assignment": "工作流程指派",
    "workflow.payment": "付款工作流程",
    "workflow.balance": "餘額檢查工作流程",
    "workflow.save": "儲存工作流程",
    "workflow.saved": "工作流程已儲存",
    "workflow.updated": "工作流程已更新",
    "workflow.startRecording": "● 開始錄製",
    "workflow.stopRecording": "■ 停止錄製",
    "workflow.noSteps": "尚未錄製任何步驟",
    "workflow.savedFlows": "已儲存的流程",
    "workflow.startRun": "開始執行",
    "workflow.stopRun": "停止執行",
    "workflow.run": "執行流程",
    "workflow.edit": "編輯工作流程",
    "workflow.addStep": "新增步驟",
    "workflow.editStep": "編輯步驟",
    "workflow.addCheck": "新增檢查",
    "workflow.readBalance": "讀取餘額",
    "workflow.addSpecialCase": "新增特殊情況",
    "workflow.addExpectedText": "新增預期文字",
    "workflow.expectedText": "預期文字：{text}",
    "workflow.notStarted": "尚未開始",
    "workflow.technicalDetails": "技術詳情",
    "workflow.runAction": "執行…",
    "workflow.runNamed": "執行：{name}",
    "workflow.editing": "正在編輯「{name}」",
    "workflow.deleteConfirm": "要刪除工作流程「{name}」嗎？",
    "workflow.flowSummary":
      "{name} · {purpose} · {steps} 個步驟 · {variables} 個變數 · 建立於 {created} · 更新於 {updated}",
    "workflow.settingsNamed": "{type} · {name}",
    "workflow.step.tap": "點按",
    "workflow.step.swipe": "向{direction}滑動",
    "workflow.step.typeText": "輸入文字",
    "workflow.step.wait": "等待 {seconds} 秒",
    "workflow.step.waitText": "等待文字 —「{text}」· 最多等待 {seconds} 秒",
    "workflow.step.readBalance": "讀取餘額 — {label}",
    "workflow.step.screenshot": "擷取螢幕截圖",
    "workflow.step.checkpoint": "手動檢查點",
    "workflow.direction.right": "右",
    "workflow.direction.left": "左",
    "workflow.direction.down": "下",
    "workflow.direction.up": "上",
    "workflow.assign": "指派",
    "workflow.noAssigned": "未指派工作流程",
    "workflow.notAssigned": "未指派",
    "workflow.chooseOne": "選擇一個工作流程",
    "workflow.executionDisabled": "已停用執行",
    "workflow.missing": "遺失",
    "workflow.retryFailed": "從失敗步驟重試",
    "workflow.restart": "重新開始工作流程",
    "workflow.cancellationRequested": "已要求取消",
    "workflow.cancelling": "取消中…",
    "settings.title": "設定",
    "settings.ocr": "OCR 設定",
    "status.loadingJobs": "正在載入工作…",
    "status.noJobs": "尚未提交任何外部工作。",
    "status.noLogs": "找不到相符的應用程式記錄。",
    "status.inUse": "使用中",
    "status.QUEUED": "排隊中",
    "status.WAITING_FOR_DEVICE": "等待裝置",
    "status.RUNNING": "執行中",
    "status.FAILED": "失敗",
    "status.INTERRUPTED": "已中斷",
    "status.CANCELLED": "已取消",
    "status.passed": "已通過",
    "status.failed": "失敗",
    "status.cancelled": "已取消",
    "status.waiting_manual": "等待手動操作",
    "jobs.summary": "顯示最近 {shown} 項，共 {count} 項工作。",
    "jobs.account": "帳戶：{account}{bank}",
    "jobs.device": "裝置：{deviceId}",
    "jobs.workflow": "工作流程：{workflow}{action}",
    "jobs.queued": "排隊時間：{time}",
    "jobs.finished": "完成時間：{time}",
    "jobs.started": "開始時間：{time}",
    "jobs.attempts": "嘗試次數：{count}",
    "error.requestFailed": "要求失敗（{status}）",
    "error.cancelJob": "無法取消工作：{message}",
    "error.retryJob": "無法建立重試：{message}",
    "error.ocrUnavailable": "OCR 無法使用",
    "error.piUnavailable": "Raspberry Pi 無法使用",
    "error.jobInterrupted": "工作已中斷",
    "error.piApiTimeout": "Pi API {path} 請求逾時。",
    "error.piApiNoSocket": "Pi API {path} 已連線，但沒有可用的通訊端。",
    "error.piApiHttp": "Pi API {path} 傳回 HTTP {status}。",
    "error.piApiTooLarge": "Pi API 回應超出大小限制。",
    "error.piApiInvalidJson": "Pi API {path} 傳回無效的 JSON。",
    "error.piApiInvalidResponse":
      "Pi API {path} 傳回無效回應（預期 ok 為布林值）。",
    "error.piRejectedService": "Pi 拒絕 {path}。請檢查 Pi 服務及手機連線。",
    "error.piRejectedHid": "Pi 拒絕 {path}。請檢查手機的 Bluetooth HID 連線。",
    "error.piApiUnreachable":
      "無法透過 Pi API 連接 {path}。請檢查 Pi 位址及私人 Wi-Fi。",
    "error.piStatusInvalid": "Pi /status 回應缺少布林值 connected。",
    "error.physicalMayHaveOccurred": " 輸入可能已經發生；系統沒有嘗試重試。",
    "error.browserTimeout": "請求逾時。",
    "error.networkRequestFailed": "網絡請求失敗。",
    "auth.title": "手機控制器 · 身分驗證",
    "auth.controller": "手機控制器",
    "auth.setupAdmin": "設定管理員",
    "auth.createAdmin": "為此電腦建立本機管理員帳戶。",
    "auth.username": "用戶名稱",
    "auth.password": "密碼",
    "auth.confirmPassword": "確認密碼",
    "auth.setupAuthenticator": "設定驗證器",
    "auth.scanQr":
      "請使用 Google Authenticator、Microsoft Authenticator 或其他兼容應用程式掃描此二維碼。",
    "auth.qrAlt": "驗證器設定二維碼",
    "auth.manualKey": "手動輸入密鑰：",
    "auth.sixDigit": "6 位數驗證碼",
    "auth.verifyFinish": "驗證並完成設定",
    "auth.storeCodes": "請妥善保存這些一次性代碼；系統不會再次顯示。",
    "auth.continueAdmin": "繼續前往管理頁面",
    "auth.adminLogin": "管理員登入",
    "auth.twoFactor": "雙重認證",
    "auth.enterAuthenticator": "請輸入以下用戶目前的驗證器代碼：",
    "auth.login": "登入",
    "auth.useRecovery": "使用復原代碼",
    "auth.enterRecovery": "請輸入以下用戶一個未使用的復原代碼：",
    "auth.recoveryCode": "復原代碼",
    "auth.useAuthenticator": "使用驗證器代碼",
  };

  Object.assign(en, {
    "ui.automations": "Automations",
    "ui.jobHistory": "Job History",
    "ui.diagnostics": "Diagnostics / Logs",
    "ui.refreshLogs": "Refresh Logs",
    "ui.filterLogs": "Filter logs",
    "ui.copy": "Copy",
    "ui.ocrPool": "OCR Worker Pool",
    "ui.workerPoolSize": "Worker pool size",
    "ui.restartOcr": "Restart OCR Workers",
    "ui.applyRecommendation": "Apply Recommendation",
    "ui.currentWithdrawal": "CURRENT WITHDRAWAL",
    "ui.reason": "Reason",
    "ui.accountName": "Account Name",
    "ui.accountNumber": "Account Number",
    "ui.bankName": "Bank Name",
    "ui.amount": "Amount",
    "ui.name": "Name",
    "ui.purpose": "Purpose",
    "ui.payment": "Payment",
    "ui.recordWhat": "Record What To Do",
    "ui.recordedSteps": "Recorded Steps",
    "ui.reviewRecording": "Review Recording",
    "ui.discardRecording": "Discard Recording",
    "ui.captureKeyboard": "Capture Keyboard",
    "ui.inputVariable": "Input Variable",
    "ui.captureScreenshot": "Capture Screenshot",
    "ui.readValue": "Read Value",
    "ui.verifyText": "Verify Text",
    "ui.wait": "Wait",
    "ui.waitForText": "Wait for Text",
    "ui.text": "Text",
    "ui.duration": "Duration",
    "ui.seconds": "seconds",
    "ui.insertStep": "Insert Step",
    "ui.advancedOptions": "Advanced options",
    "ui.searchWorkflows": "Search workflows",
    "ui.refreshFlows": "Refresh Flows",
    "ui.captureKey": "Capture key",
    "ui.saveKeyboard": "Save Keyboard",
    "ui.startCapture": "Start Capture",
    "ui.capturedKeyboards": "Captured Keyboards",
    "ui.calibration": "Calibration",
    "ui.calibrateDisplay": "Calibrate display",
    "ui.undoCorner": "Undo corner",
    "ui.back": "Back",
    "ui.home": "Home",
    "ui.takeOver": "Take Over",
    "ui.disableInput": "Disable input",
    "ui.reloadDashboard": "Reload Dashboard",
    "ui.applyChanges": "Apply Changes",
    "ui.close": "Close",
    "ui.username": "Username",
    "ui.password": "Password",
    "ui.twoFactorCode": "Google 2FA Code",
    "ui.rotation": "Rotation",
    "ui.enabled": "Enabled",
    "ui.invalidAccount": "Invalid account number",
    "ui.bankRejected": "Bank rejected payment",
    "ui.paymentIncomplete": "Unable to complete payment",
    "ui.searchWorkflowPlaceholder": "Search by workflow name or ID",
    "ui.cameraPlaceholder": "Select a camera, index, or camera URL",
    "ui.logPlaceholder": "camera, OCR, workflow…",
    "ui.bankPlaceholder": "e.g. Hong Leong",
    "ui.variablePlaceholder": "Custom workflow variable…",
    "ui.dashboardModeLabel": "Dashboard mode",
  });
  Object.assign(zhHK, {
    "ui.automations": "自動化",
    "ui.jobHistory": "工作記錄",
    "ui.diagnostics": "診斷／記錄",
    "ui.refreshLogs": "更新記錄",
    "ui.filterLogs": "篩選記錄",
    "ui.copy": "複製",
    "ui.ocrPool": "OCR 工作程序集區",
    "ui.workerPoolSize": "工作程序數量",
    "ui.restartOcr": "重新啟動 OCR 工作程序",
    "ui.applyRecommendation": "套用建議",
    "ui.currentWithdrawal": "目前提款",
    "ui.reason": "原因",
    "ui.accountName": "帳戶名稱",
    "ui.accountNumber": "帳戶號碼",
    "ui.bankName": "銀行名稱",
    "ui.amount": "金額",
    "ui.name": "名稱",
    "ui.purpose": "用途",
    "ui.payment": "付款",
    "ui.recordWhat": "錄製操作",
    "ui.recordedSteps": "已錄製步驟",
    "ui.reviewRecording": "檢視錄製內容",
    "ui.discardRecording": "放棄錄製",
    "ui.captureKeyboard": "擷取鍵盤",
    "ui.inputVariable": "輸入變數",
    "ui.captureScreenshot": "擷取螢幕截圖",
    "ui.readValue": "讀取數值",
    "ui.verifyText": "驗證文字",
    "ui.wait": "等待",
    "ui.waitForText": "等待文字",
    "ui.text": "文字",
    "ui.duration": "持續時間",
    "ui.seconds": "秒",
    "ui.insertStep": "插入步驟",
    "ui.advancedOptions": "進階選項",
    "ui.searchWorkflows": "搜尋工作流程",
    "ui.refreshFlows": "更新流程",
    "ui.captureKey": "擷取按鍵",
    "ui.saveKeyboard": "儲存鍵盤",
    "ui.startCapture": "開始擷取",
    "ui.capturedKeyboards": "已擷取的鍵盤",
    "ui.calibration": "校正",
    "ui.calibrateDisplay": "校正顯示範圍",
    "ui.undoCorner": "復原上一個角點",
    "ui.back": "返回",
    "ui.home": "主畫面",
    "ui.takeOver": "接管控制",
    "ui.disableInput": "停用輸入",
    "ui.reloadDashboard": "重新載入控制台",
    "ui.applyChanges": "套用變更",
    "ui.close": "關閉",
    "ui.username": "用戶名稱",
    "ui.password": "密碼",
    "ui.twoFactorCode": "Google 雙重認證碼",
    "ui.rotation": "旋轉",
    "ui.enabled": "已啟用",
    "ui.invalidAccount": "帳戶號碼無效",
    "ui.bankRejected": "銀行拒絕付款",
    "ui.paymentIncomplete": "無法完成付款",
    "ui.searchWorkflowPlaceholder": "按工作流程名稱或 ID 搜尋",
    "ui.cameraPlaceholder": "選擇相機、索引或相機網址",
    "ui.logPlaceholder": "相機、OCR、工作流程…",
    "ui.bankPlaceholder": "例如：Hong Leong",
    "ui.variablePlaceholder": "自訂工作流程變數…",
    "ui.dashboardModeLabel": "控制台模式",
  });

  const dictionaries = { en, "zh-HK": zhHK };
  const staticTextKeys = new Map(
    Object.entries({
      "PHONE CONTROL / EXTERNAL CAMERA": "app.eyebrow",
      "Local control": "app.localControl",
      Operate: "nav.operate",
      Admin: "nav.admin",
      "SYSTEM STATUS": "health.systemStatus",
      "Operational health": "health.title",
      Controller: "health.controller",
      "Raspberry Pi": "health.raspberryPi",
      HID: "health.hid",
      Camera: "health.camera",
      OCR: "health.ocr",
      "Resolve Issue": "health.resolveIssue",
      "NEEDS ATTENTION": "health.needsAttention",
      "Needs Attention": "health.needsAttention",
      Issue: "health.issue",
      "Recommended action": "health.recommendedAction",
      "Review the available recovery options.": "health.reviewRecovery",
      "Recover Phone": "health.recoverPhone",
      "View Details": "health.viewDetails",
      "Resolve Phone Issue": "health.resolvePhone",
      Devices: "device.title",
      "Monitor every phone camera and choose one control target.":
        "device.description",
      "Add Phone": "device.add",
      "Loading registered phones…": "device.loading",
      "Logical device ID": "device.logicalId",
      "Start Enrollment": "device.startEnrollment",
      Cancel: "common.cancel",
      "Waiting for phone to pair": "device.waitingPair",
      "Stop Enrollment": "device.stopEnrollment",
      "Camera Settings": "camera.settings",
      "Camera source": "camera.source",
      "Refresh Cameras": "camera.refresh",
      "Test Camera": "camera.test",
      Save: "common.save",
      "Withdraw Account": "withdraw.title",
      "Login & Assign": "withdraw.loginAssign",
      "Cancel Withdrawal": "withdraw.cancel",
      "Confirm Cancel": "withdraw.confirmCancel",
      "Cancel this withdrawal?": "withdraw.confirmTitle",
      "Yes, Cancel Withdrawal": "withdraw.confirmYes",
      "Workflow Assignment": "workflow.assignment",
      "Payment Workflow": "workflow.payment",
      "Balance Check Workflow": "workflow.balance",
      "Save Workflow": "workflow.save",
      "Camera view": "camera.title",
      "Waiting for camera access…": "camera.waiting",
      "● Start Recording": "workflow.startRecording",
      "■ Stop Recording": "workflow.stopRecording",
      "Read Balance": "workflow.readBalance",
      "Add Special Case": "workflow.addSpecialCase",
      "Add Step": "workflow.addStep",
      Balance: "balance.title",
      "Available Balance": "balance.available",
      "Balance Check": "balance.check",
      "Start Manual Control": "device.startManual",
      "Save calibration": "camera.saveCalibration",
      "Edit Step": "workflow.editStep",
      Settings: "settings.title",
      "Add Check": "workflow.addCheck",
      "Saved Flows": "workflow.savedFlows",
      "Start Run": "workflow.startRun",
      "Stop Run": "workflow.stopRun",
      "Set up administrator": "auth.setupAdmin",
      "Create the local administrator account for this computer.":
        "auth.createAdmin",
      Username: "auth.username",
      Password: "auth.password",
      "Confirm password": "auth.confirmPassword",
      Continue: "common.continue",
      "Set up authenticator": "auth.setupAuthenticator",
      "Scan this QR code with Google Authenticator, Microsoft Authenticator, or another compatible app.":
        "auth.scanQr",
      "Manual key:": "auth.manualKey",
      "6-digit code": "auth.sixDigit",
      "Verify and finish setup": "auth.verifyFinish",
      "Store these one-time codes securely. They will not be shown again.":
        "auth.storeCodes",
      "Continue to Admin": "auth.continueAdmin",
      "Administrator login": "auth.adminLogin",
      "Two-factor verification": "auth.twoFactor",
      "Enter the current authenticator code for": "auth.enterAuthenticator",
      Login: "auth.login",
      "Use recovery code": "auth.useRecovery",
      "Use recovery code": "auth.useRecovery",
      "Enter one unused recovery code for": "auth.enterRecovery",
      "Recovery code": "auth.recoveryCode",
      "Use authenticator code": "auth.useAuthenticator",
      Automations: "ui.automations",
      "Job History": "ui.jobHistory",
      "Diagnostics / Logs": "ui.diagnostics",
      "Refresh Logs": "ui.refreshLogs",
      "Filter logs": "ui.filterLogs",
      Copy: "ui.copy",
      "OCR Worker Pool": "ui.ocrPool",
      "Worker pool size": "ui.workerPoolSize",
      "Restart OCR Workers": "ui.restartOcr",
      "Apply Recommendation": "ui.applyRecommendation",
      "CURRENT WITHDRAWAL": "ui.currentWithdrawal",
      Reason: "ui.reason",
      "Account Name": "ui.accountName",
      "Account Number": "ui.accountNumber",
      "Bank Name": "ui.bankName",
      Amount: "ui.amount",
      Name: "ui.name",
      Purpose: "ui.purpose",
      Payment: "ui.payment",
      "Record What To Do": "ui.recordWhat",
      "Recorded Steps": "ui.recordedSteps",
      "Review Recording": "ui.reviewRecording",
      "Discard Recording": "ui.discardRecording",
      "Capture Keyboard": "ui.captureKeyboard",
      "Input Variable": "ui.inputVariable",
      "Capture Screenshot": "ui.captureScreenshot",
      "Read Value": "ui.readValue",
      "Verify Text": "ui.verifyText",
      Wait: "ui.wait",
      "Wait for Text": "ui.waitForText",
      Text: "ui.text",
      Duration: "ui.duration",
      seconds: "ui.seconds",
      "Insert Step": "ui.insertStep",
      "Advanced options": "ui.advancedOptions",
      "Search workflows": "ui.searchWorkflows",
      "Refresh Flows": "ui.refreshFlows",
      "Capture key": "ui.captureKey",
      "Save Keyboard": "ui.saveKeyboard",
      "Start Capture": "ui.startCapture",
      "Captured Keyboards": "ui.capturedKeyboards",
      Calibration: "ui.calibration",
      "Calibrate display": "ui.calibrateDisplay",
      "Undo corner": "ui.undoCorner",
      Back: "ui.back",
      Home: "ui.home",
      "Take Over": "ui.takeOver",
      "Disable input": "ui.disableInput",
      "Reload Dashboard": "ui.reloadDashboard",
      "Apply Changes": "ui.applyChanges",
      Close: "ui.close",
      "Google 2FA Code": "ui.twoFactorCode",
      Rotation: "ui.rotation",
      Enabled: "ui.enabled",
      "Invalid account number": "ui.invalidAccount",
      "Bank rejected payment": "ui.bankRejected",
      "Unable to complete payment": "ui.paymentIncomplete",
    }),
  );
  const additionalStaticZhHK = {
    "1. Position": "1. 位置",
    "2. Step type": "2. 步驟類型",
    "3. Settings": "3. 設定",
    "Account Status: Not Assigned": "帳戶狀態：未指派",
    "Arrow Down": "向下鍵",
    "Arrow Left": "向左鍵",
    "Arrow Right": "向右鍵",
    "Arrow Up": "向上鍵",
    Backspace: "退格鍵",
    "Bottom-left": "左下角",
    "Bottom-right": "右下角",
    "Calibration required": "需要校正",
    "Checking…": "檢查中…",
    "Click the four corners of the lit phone display, in this order. Leave the bezel out.":
      "依次點按亮起的手機顯示範圍四個角；不要包括邊框。",
    "Collecting metrics…": "正在收集數據…",
    Custom: "自訂",
    Delete: "刪除",
    Disabled: "已停用",
    "Duration (ms)": "持續時間（毫秒）",
    End: "結束位置",
    "End X (%)": "結束 X（%）",
    "End Y (%)": "結束 Y（%）",
    Enter: "Enter 鍵",
    "Enter manually": "手動輸入",
    "Input steps select one of these maps explicitly.":
      "輸入步驟會明確選用其中一個鍵盤配置。",
    "Insert after": "插入於此項之後",
    "Interact with this phone preview to try recording without hardware.":
      "在此手機預覽中操作，即可在沒有硬件的情況下試用錄製功能。",
    "Keep the camera and phone fixed. Recalibrate after moving either one or rotating the phone.":
      "保持相機及手機位置固定；移動任何一方或旋轉手機後，請重新校正。",
    Key: "按鍵",
    "Local dashboard. Camera viewing, manual control, scripts and saved workflows share device ownership.":
      "本機控制台。相機畫面、手動控制、指令碼及已儲存的工作流程共用裝置控制權。",
    "Lowercase QWERTY keys are included.": "包括小寫 QWERTY 按鍵。",
    "Mark only the final action after which the bank may have sent money. Withdraw retries use this boundary to prevent duplicate payments.":
      "只標記銀行可能已匯款後的最後一項操作。提款重試會以此界線避免重複付款。",
    "No steps recorded yet. Click Start Recording, then control the phone through the preview. Your taps, swipes, typing and navigation will appear here. Typed text is saved in the workflow.":
      "尚未錄製任何步驟。按「開始錄製」，然後透過預覽控制手機；點按、滑動、輸入及導覽操作會顯示於此。輸入的文字會儲存於工作流程。",
    "No steps recorded yet. Use the phone preview to record taps, swipes, typing and navigation.":
      "尚未錄製任何步驟。使用手機預覽錄製點按、滑動、輸入及導覽操作。",
    Output: "輸出",
    "Page Down": "下一頁",
    "Page Up": "上一頁",
    Read: "讀取",
    "Reconnecting...": "重新連線中…",
    "Recorded value": "已錄製數值",
    "Run Flow": "執行流程",
    Screenshot: "螢幕截圖",
    "Secure input belongs on the phone. Avoid typing sensitive values while recording.":
      "敏感資料應在手機上安全輸入；錄製期間請避免輸入敏感資料。",
    "Select issue": "選擇問題",
    "Select on Current Screen": "在目前畫面選擇",
    "Select on the calibrated current screen snapshot; refresh after the phone screen changes.":
      "在已校正的目前畫面快照上選擇；手機畫面變更後請更新快照。",
    "Sometimes something extra appears here. Record what to do when it does.":
      "如這裡有時會出現額外內容，請錄製出現時要執行的操作。",
    "Start Recording": "開始錄製",
    "Start X (%)": "起始 X（%）",
    "Start Y (%)": "起始 Y（%）",
    Tab: "Tab 鍵",
    "Test the camera to preview it here.": "測試相機後，預覽會顯示於此。",
    "The recorded value will be removed. Playback will pause for you to complete this step on the phone.":
      "已錄製的值會被移除。播放時會暫停，讓你在手機上完成此步驟。",
    "Top-left": "左上角",
    "Top-right": "右上角",
    "Use Region OCR": "使用區域 OCR",
    "Use only for keyboards whose key positions stay the same between runs. Do not use this for PIN, OTP, password, randomized, or authorization keypads.":
      "只適用於每次執行時按鍵位置不變的鍵盤。請勿用於 PIN、OTP、密碼、隨機排列或授權鍵盤。",
    "Use this input as": "將此輸入用作",
    "Used when no value is provided when the workflow starts.":
      "工作流程開始時如未提供值，便會使用此值。",
    "Waiting for live video": "正在等待即時影像",
    "Wake Screen": "喚醒螢幕",
    width: "寬度",
    height: "高度",
    "lit phone display": "亮起的手機顯示範圍",
    "← Back": "← 返回",
    "← Swipe left": "← 向左滑動",
    "↑ Swipe up": "↑ 向上滑動",
    "→ Swipe right": "→ 向右滑動",
    "↓ Swipe down": "↓ 向下滑動",
    "⌂ Home": "⌂ 主畫面",
    "⏻ Screen off": "⏻ 關閉螢幕",
    "▣ Recent apps": "▣ 最近使用的應用程式",
    "● Checking controller…": "● 正在檢查控制器…",
    "● Recording": "● 錄製中",
    "☀ Wake screen": "☀ 喚醒螢幕",
    "Phone control": "手機控制",
    "Phone display": "手機顯示",
    "External camera": "外置相機",
    "Pi API": "Pi API",
    "Phone HID": "手機 HID",
    "Checking connection…": "正在檢查連線…",
    "Checking device status…": "正在檢查裝置狀態…",
    "Checking the camera and phone…": "正在檢查相機及手機…",
    "Loading camera status…": "正在載入相機狀態…",
    "Loading jobs…": "正在載入工作…",
    "Persistent job audit and diagnostic history.":
      "持續保存的工作稽核及診斷記錄。",
    "Recent redacted application activity for support.":
      "供支援用途的近期已遮蓋敏感資料應用程式活動。",
    "Select Refresh Logs to load recent activity.":
      "選擇「更新記錄」以載入近期活動。",
    "Physical CPU cores:": "實體 CPU 核心：",
    "Logical CPU threads:": "邏輯 CPU 執行緒：",
    "OCR CPU budget:": "OCR CPU 配額：",
    "CPU worker ceiling:": "CPU 工作程序上限：",
    "RAM worker ceiling:": "RAM 工作程序上限：",
    "Hardware worker ceiling:": "硬件工作程序上限：",
    "Current workers:": "目前工作程序：",
    "Recommended workers:": "建議工作程序：",
    "Busy workers:": "忙碌中的工作程序：",
    "Queued OCR requests:": "等候中的 OCR 要求：",
    "Restart required:": "需要重新啟動：",
    "CPU usage:": "CPU 使用量：",
    "Memory usage:": "記憶體使用量：",
    "Available RAM:": "可用 RAM：",
    "Average queue wait:": "平均等候時間：",
    "Average inference:": "平均推理時間：",
    "Recommendation:": "建議：",
    "Higher values allow simultaneous OCR requests but use more CPU and memory.":
      "較高數值可同時處理更多 OCR 要求，但會使用更多 CPU 及記憶體。",
    "DEVICE CAMERA": "裝置相機",
    "DEVICE AUTOMATION": "裝置自動化",
    "EXTERNAL WITHDRAW BOT": "外部提款機械人",
    "MANUAL RECOVERY": "手動復原",
    Username: "用戶名稱",
    Password: "密碼",
    "Enter the current 6-digit code shown in Google Authenticator.":
      "輸入 Google Authenticator 目前顯示的 6 位數驗證碼。",
    "This will cancel the withdrawal externally and cannot be undone.":
      "這會在外部系統取消提款，且無法復原。",
    "Workflow Assignment": "工作流程指派",
    Record: "錄製",
    Manual: "手動",
    "Recording controls": "錄製控制",
    "No steps recorded yet. Click Start Recording, then control the phone through the preview. Your taps, swipes, typing and navigation will appear here.":
      "尚未錄製任何步驟。按「開始錄製」，然後透過預覽控制手機；點按、滑動、輸入及導覽操作會顯示於此。",
    "Use the phone preview as usual. We’ll record your taps, swipes and typing, then you can review and name the flow.":
      "如常使用手機預覽；系統會錄製點按、滑動及輸入，之後可檢視內容並命名流程。",
    "Review recording and save workflow": "檢視錄製內容並儲存工作流程",
    "Actions recorded: 0": "已錄製操作：0",
    "Undo Last": "復原上一步",
    "Expect Current Screen": "預期目前畫面",
    "Finish Special Case": "完成特殊情況",
    "Add & Type Now": "新增並立即輸入",
    "Custom Value": "自訂數值",
    Currency: "貨幣",
    "Output variable": "輸出變數",
    "Step name": "步驟名稱",
    Parser: "解析器",
    Input: "輸入",
    "Input method": "輸入方式",
    "Default value": "預設值",
    Required: "必填",
    Variable: "變數",
    "Keep Fixed": "保持固定值",
    "Make Variable": "設為變數",
    "Custom variable name": "自訂變數名稱",
    "Amount Input Format": "金額輸入格式",
    "As received": "按收到的格式",
    "Implied cents (100.00 → 10000)": "隱含仙位（100.00 → 10000）",
    "Whole amount only (100.00 → 100)": "只輸入整數金額（100.00 → 100）",
    "Captured on-screen keyboard": "已擷取的螢幕鍵盤",
    Keyboard: "鍵盤",
    "Keyboard type": "鍵盤類型",
    Numeric: "數字",
    Alphabetic: "英文字母",
    "Keys to capture, separated by commas": "要擷取的按鍵（以逗號分隔）",
    "Choose keys, then start capture.": "選擇按鍵，然後開始擷取。",
    "Capture this key": "擷取此按鍵",
    "Tap this key on the phone": "在手機上點按此鍵",
    "Digits 0-9 are included.": "包括數字 0 至 9。",
    "Include decimal": "包括小數點",
    "Include space": "包括空格",
    "Include backspace": "包括退格鍵",
    "Verification must pass before the workflow continues.":
      "驗證通過後工作流程才會繼續。",
    "Verification never resumes or retries the failed workflow.":
      "驗證不會恢復或重試失敗的工作流程。",
    "Verify field": "驗證欄位",
    "Expected text": "預期文字",
    "Choose expected text": "選擇預期文字",
    "Use Region": "使用區域",
    "Clear OCR Region": "清除 OCR 區域",
    "Test ROI OCR": "測試區域 OCR",
    "Wait duration (seconds)": "等待時間（秒）",
    "Wait up to": "最多等待",
    "Check for up to": "最多檢查",
    "Stable for (ms)": "穩定時間（毫秒）",
    "Maximum wait (sec)": "最長等待時間（秒）",
    "Special Case": "特殊情況",
    "When this appears": "當此內容出現時",
    "If not found: Continue normally": "如找不到：如常繼續",
    "Recording Special Case": "正在錄製特殊情況",
    "Irreversible payment submission": "不可逆轉的付款提交",
    "Capture the current phone screen and save it as workflow evidence.":
      "擷取目前手機畫面並儲存為工作流程證明。",
    "Calibrate display": "校正顯示範圍",
    "Input is disabled. Calibrate the display to begin.":
      "輸入已停用。請先校正顯示範圍。",
    "Start manual control to use the phone preview, Back, and Home.":
      "開始手動控制，以使用手機預覽、返回及主畫面。",
    "Use the phone preview to return the phone to the expected starting screen.":
      "使用手機預覽將手機返回預期的起始畫面。",
    "Verify Phone Ready": "驗證手機已就緒",
    "Keyboard control inactive": "鍵盤控制未啟用",
    "Click or focus the phone view to enable keyboard control.":
      "點按手機畫面或將焦點移至該處，以啟用鍵盤控制。",
    "Physical controls are paused while status checks continue.":
      "實體控制已暫停，但狀態檢查會繼續。",
    "Primary actions": "主要操作",
    "Advanced / Debug": "進階／除錯",
    "No active run": "沒有執行中的流程",
    "Resume Checkpoint": "從檢查點繼續",
    "Withdraw session reconnect requested.": "已要求重新連接提款工作階段。",
    "Balance refresh requested.": "已要求更新餘額。",
    "Phone marked ready.": "已將手機標記為就緒。",
    "Phone restored and ready.": "手機已復原並就緒。",
    "Withdrawal cancelled successfully.": "已成功取消提款。",
    "Withdraw account authenticated successfully.": "提款帳戶驗證成功。",
    "Connection restored.": "連線已恢復。",
    "Pi temporarily unavailable. Retrying automatically...":
      "Pi 暫時無法使用，正在自動重試…",
    "Camera resolution changed. Recalibrate the display.":
      "相機解像度已變更，請重新校正顯示範圍。",
    "Takeover requested. Waiting for automation to yield.":
      "已要求接管控制，正在等待自動化程序釋放控制。",
    "Manual control active.": "手動控制已啟用。",
    "Input disabled.": "輸入已停用。",
    "Sent swipe.": "已傳送滑動操作。",
    "Keep the gesture inside the phone preview.":
      "請在手機預覽範圍內完成手勢。",
    "Captured keyboard deleted.": "已刪除擷取的鍵盤。",
    "Flow updated.": "流程已更新。",
    "Step no longer exists.": "步驟已不存在。",
    "Tap coordinates must be between 0% and 100%.":
      "點按座標必須介乎 0% 至 100%。",
    "Swipe coordinates must be between 0% and 100%.":
      "滑動座標必須介乎 0% 至 100%。",
    "Swipe duration must be between 50 and 60000 ms.":
      "滑動時間必須介乎 50 至 60000 毫秒。",
    "Wait duration must be between 0 and 3600 seconds.":
      "等待時間必須介乎 0 至 3600 秒。",
    "Step updated.": "步驟已更新。",
    "Stable for must be between 100 and 10000 ms.":
      "穩定時間必須介乎 100 至 10000 毫秒。",
    "Maximum wait must be between 1 and 300 seconds.":
      "最長等待時間必須介乎 1 至 300 秒。",
    "Select an OCR region snapshot first.": "請先選擇 OCR 區域快照。",
    "Choose a field to verify.": "請選擇要驗證的欄位。",
    "Text to detect must be between 1 and 500 characters.":
      "要偵測的文字必須為 1 至 500 個字元。",
    "Text verification added. Recording continues.":
      "已新增文字驗證，錄製會繼續。",
    "Select a calibrated OCR region first.": "請先選擇已校正的 OCR 區域。",
    "Read Balance step added. Recording continues.":
      "已新增讀取餘額步驟，錄製會繼續。",
    "Text is too long for one input.": "文字太長，無法一次輸入。",
    "Finish or cancel the current Special Case setup first.":
      "請先完成或取消目前的特殊情況設定。",
    "Finish the current Special Case before adding another one.":
      "請先完成目前的特殊情況，然後再新增另一個。",
    "Recording Special Case actions.": "正在錄製特殊情況操作。",
    "Screenshot added. Recording continues.": "已新增螢幕截圖，錄製會繼續。",
    "Enter a variable name.": "請輸入變數名稱。",
    "Enter a Default value before using Add & Type Now.":
      "使用「新增並立即輸入」前，請先輸入預設值。",
    "Capture or select a keyboard first.": "請先擷取或選擇鍵盤。",
    "Keyboard saved. Use it only while its layout remains stable.":
      "鍵盤已儲存；只應在版面保持不變時使用。",
    "Keyboard capture cancelled; the workflow was not changed.":
      "已取消鍵盤擷取；工作流程沒有變更。",
    "Last recorded step removed. The phone itself was not changed.":
      "已移除最後錄製的步驟；手機本身沒有變更。",
    "Enter bank name": "請輸入銀行名稱",
    "Calibrate a live camera view before recording.":
      "錄製前請先校正即時相機畫面。",
    "The camera frame expired; gesture discarded.":
      "相機畫面已過期，手勢已捨棄。",
    "Keep the gesture inside the preview.": "請在預覽範圍內完成手勢。",
    "Click inside the phone preview.": "請點按手機預覽範圍。",
    "Bluetooth MAC not paired": "尚未配對藍牙 MAC",
    "Technical details": "技術詳情",
    "Phone recovered successfully.": "手機已成功復原。",
    "The configured ready-screen condition was detected. Review the failed job before starting new business work.":
      "已偵測到設定的就緒畫面條件。開始新的業務工作前，請先檢視失敗的工作。",
    "Recovery workflow is running. The failed workflow will not be retried.":
      "復原工作流程正在執行；失敗的工作流程不會重試。",
    "Expected starting screen was not detected.": "未偵測到預期的起始畫面。",
    "Automatic recovery failed. Resolve the phone issue manually.":
      "自動復原失敗，請手動處理手機問題。",
    "Phone recovered successfully. Status: Ready.":
      "手機已成功復原。狀態：就緒。",
    "Starting recovery workflow…": "正在啟動復原工作流程…",
    "Manual control could not be stopped safely. Verification was not started.":
      "無法安全停止手動控制，因此尚未開始驗證。",
    "Marking phone ready…": "正在將手機標記為就緒…",
    "Verifying the configured ready screen…": "正在驗證已設定的就緒畫面…",
    "External cancellation failed. This withdrawal remains under manual attention.":
      "外部取消失敗，此提款仍需手動處理。",
    "Account Status: Assigned": "帳戶狀態：已指派",
    "Login with the external operator account assigned to this phone.":
      "使用指派給此手機的外部操作員帳戶登入。",
    "Enter password to authenticate": "輸入密碼以進行驗證",
    Reauthenticate: "重新驗證",
    "Google 2FA code must be exactly 6 digits.":
      "Google 雙重認證碼必須為 6 位數字。",
    "Missing workflow": "遺失的工作流程",
    "Remove this assignment": "移除此指派",
    "No Balance Check Workflow assigned": "未指派餘額檢查工作流程",
    "Disable automatic balance checks for this phone":
      "停用此手機的自動餘額檢查",
    "Missing Balance Check Workflow": "遺失餘額檢查工作流程",
    "Invalid Balance Check Workflow": "餘額檢查工作流程無效",
    "Loading workflows…": "正在載入工作流程…",
    "Save Balance Check Workflow": "儲存餘額檢查工作流程",
    "Save Payment Workflow": "儲存付款工作流程",
    "Choose the Balance Check Workflow used to read this payer's balance.":
      "選擇用來讀取此付款人餘額的餘額檢查工作流程。",
    "Choose the Payment Workflow that may run on this phone.":
      "選擇可在此手機上執行的付款工作流程。",
    "Camera test failed": "相機測試失敗",
    Recalibrate: "重新校正",
    "Waiting for the assigned camera…": "正在等待已指派的相機…",
    "Camera configuration changed. Existing screen calibration may no longer match. Recalibrate before control.":
      "相機設定已變更，現有螢幕校正可能不再適用。控制前請重新校正。",
    "Finish or cancel calibration before deleting a phone.":
      "刪除手機前，請先完成或取消校正。",
    "Keyboard control paused · command in progress":
      "鍵盤控制已暫停 · 指令執行中",
    "Keyboard control active": "鍵盤控制已啟用",
    "Keyboard control inactive": "鍵盤控制未啟用",
    "Type on your laptop to control the phone. Press Esc to release keyboard control.":
      "在手提電腦上輸入以控制手機；按 Esc 解除鍵盤控制。",
    "Bluetooth phone unavailable": "藍牙手機無法使用",
    "No authorized USB phone": "沒有已授權的 USB 手機",
    "Status unavailable": "無法取得狀態",
    "Pi Reconnecting...": "Pi 重新連線中…",
    Online: "在線",
    "Pi Offline": "Pi 離線",
    "Phone Waiting": "等待手機",
    Unknown: "未知",
    "Camera-calibrated coordinates": "相機校正座標",
    "Unavailable / starting": "無法使用／啟動中",
    "Not in use": "未使用",
    "● Live camera": "● 相機即時畫面",
    "Camera feed unavailable": "相機影像無法使用",
    "Automation running": "自動化程序執行中",
    "Takeover requested": "已要求接管控制",
    "Control active": "控制已啟用",
    "Manual control active": "手動控制已啟用",
    "Other tab": "其他分頁",
    Offline: "離線",
    "Phone controls are unavailable while automation owns the phone.":
      "自動化程序控制手機期間，手機控制功能無法使用。",
    "Manual control disabled. Camera monitoring remains available. Use Take Over to interrupt automation.":
      "手動控制已停用；相機監察仍可使用。如要中斷自動化程序，請使用「接管控制」。",
    "Takeover requested. Manual input stays disabled until automation yields.":
      "已要求接管控制；自動化程序釋放控制前，手動輸入會保持停用。",
    "Click the phone preview to tap or drag to swipe.":
      "在手機預覽上點按，或拖曳以滑動。",
    "Enable control when you are ready. Each click sends one tap to the phone.":
      "準備好後啟用控制；每次點按都會向手機傳送一次點按操作。",
    Saved: "已儲存",
    Required: "需要",
    "This action is not provided by the Pi API.": "Pi API 不支援此操作。",
    "Check the outline, then save calibration.": "檢查外框，然後儲存校正設定。",
    "Click to tap or drag to swipe inside the outlined display.":
      "在外框內點按，或拖曳以滑動。",
    "Click inside the outlined display to send one tap.":
      "在外框內點按以傳送一次點按操作。",
    "Start manual control when you are ready.": "準備好後開始手動控制。",
    "Input disabled. Calibrate, then enable Manual Control.":
      "輸入已停用。請先校正，然後啟用手動控制。",
    selected: "已選擇",
    "Click the four corners of the": "依次點按",
    ", in this order. Leave the bezel out.": "的四個角；不要包括邊框。",
    "Connection details": "連線詳情",
    "Pi connection status": "Pi 連線狀態",
    "Recent application logs": "近期應用程式記錄",
    "Test camera preview": "測試相機預覽",
    "Live external camera. Click to tap or drag to swipe.":
      "外置相機即時畫面。點按以操作，或拖曳以滑動。",
    "Phone preview. Click, drag and type to record in mock mode.":
      "手機預覽。在模擬模式中點按、拖曳及輸入以錄製操作。",
    "Recorded steps": "已錄製步驟",
    "Step type": "步驟類型",
    "Wait seconds": "等待秒數",
    "Recent workflow activity": "近期工作流程活動",
    "Workflow execution logs": "工作流程執行記錄",
    "Leave blank to keep existing value": "留空以保留現有值",
    "Move Up": "上移",
    "Move Down": "下移",
    Change: "更改",
    "Add Expected Text": "新增預期文字",
    Remove: "移除",
    "+ Add Special Case": "+ 新增特殊情況",
    "Actions when shown": "顯示時的操作",
    "Actions when not shown": "未顯示時的操作",
    "Add Wait": "新增等待",
    "Insert Wait": "插入等待",
    "Add Verification": "新增驗證",
    "Insert Step": "插入步驟",
    "Finishing recording…": "正在完成錄製…",
    "● Recording Special Case": "● 正在錄製特殊情況",
    "Taps, swipes and typing appear below.":
      "點按、滑動及輸入操作會顯示於下方。",
    "Typing now...": "正在輸入…",
    "All selected keys are captured. Save the keyboard or cancel.":
      "已擷取所有選定按鍵。請儲存鍵盤或取消。",
    "Save Changes": "儲存變更",
    "Save Workflow": "儲存工作流程",
    "Cancel Editing": "取消編輯",
    "Discard Recording": "捨棄錄製內容",
    "Edit Workflow": "編輯工作流程",
    "Review Recording": "檢視錄製內容",
    "This workflow has no steps. Add or edit steps, then save your changes.":
      "此工作流程沒有步驟。新增或編輯步驟後，請儲存變更。",
    "Reading current phone screen…": "正在讀取目前手機畫面…",
    "Suggestions from the current screen. Select one or enter text manually.":
      "以下是目前畫面的建議。請選擇一項或手動輸入文字。",
    "No reliable text suggestions found. Enter expected text manually.":
      "找不到可靠的文字建議，請手動輸入預期文字。",
    "Controller connection interrupted": "控制器連線中斷",
    "Recheck the controller and Raspberry Pi connection.":
      "請重新檢查控制器及 Raspberry Pi 連線。",
    "Raspberry Pi unavailable": "Raspberry Pi 無法使用",
    "Recheck the Raspberry Pi connection.": "請重新檢查 Raspberry Pi 連線。",
    "Reconnect the phone, then recheck its status.":
      "請重新連接手機，然後再次檢查狀態。",
    "Open Admin and configure or reconnect the selected camera.":
      "請開啟管理頁面，設定或重新連接所選相機。",
    "Open Admin and calibrate the selected phone.":
      "請開啟管理頁面並校正所選手機。",
    "Phone recovery in progress": "手機復原進行中",
    "The configured recovery workflow is returning the phone to its verified ready screen.":
      "已設定的復原工作流程正將手機返回已驗證的就緒畫面。",
    "OCR unavailable": "OCR 無法使用",
    "Open Admin to inspect the OCR worker pool.":
      "請開啟管理頁面檢查 OCR 工作程序池。",
    "Ready screen detected. Releasing manual control…":
      "已偵測到就緒畫面，正在解除手動控制…",
    "Ready screen was detected, but manual control could not be released safely.":
      "已偵測到就緒畫面，但無法安全解除手動控制。",
    "Cancel this running job? The physical phone may already have changed.":
      "取消這項執行中的工作？實體手機可能已經發生變更。",
    "Manual Control could not be enabled. Recording was not started.":
      "無法啟用手動控制，因此錄製尚未開始。",
    "Variable step added and its default value typed once.":
      "已新增變數步驟，並輸入了一次預設值。",
    "Variable step added without sending input.":
      "已新增變數步驟，但沒有傳送輸入。",
    "Teaching clicks are captured only; they are not sent to the phone.":
      "教學點按只會被擷取，不會傳送至手機。",
    Transfer: "轉帳",
    "Target preview before the tap": "點按前的目標預覽",
    "In Use": "使用中",
    Unused: "未使用",
    Home: "主畫面",
    Back: "返回",
    "Screen Off": "關閉螢幕",
    "Recent Apps": "最近使用的應用程式",
    "Press Home": "按主畫面鍵",
    "Press Page Up": "按上一頁鍵",
    "Press Page Down": "按下一頁鍵",
    "Configured field": "已設定欄位",
    "the selected phone": "所選手機",
    "Check the camera connection or open Admin for camera settings.":
      "請檢查相機連線，或開啟管理頁面的相機設定。",
    "Automatic recovery failed": "自動復原失敗",
    "Resolve the phone issue and verify the expected starting screen before further work.":
      "繼續操作前，請先處理手機問題並驗證預期的起始畫面。",
    "Use Manual Assistance to restore the phone, then verify its expected starting screen.":
      "使用手動協助復原手機，然後驗證預期的起始畫面。",
    "Withdrawal lock status is uncertain. Reconcile before retrying.":
      "提款鎖定狀態不確定；重試前請先核對。",
    "Manual control was not restored": "未能恢復手動控制",
    "NEEDS ATTENTION": "需要處理",
    "No OCR region selected.": "未選擇 OCR 區域。",
    "ROI must have positive size and fit inside the calibrated screen.":
      "OCR 區域必須有正數大小，並位於已校正畫面範圍內。",
    "input step": "輸入步驟",
    "No text read": "未讀取到文字",
    SPACE: "空格鍵",
    BACKSPACE: "退格鍵",
    "Implied cents": "隱含仙位",
    "Whole amount only": "只輸入整數金額",
    "Loading recent logs…": "正在載入近期記錄…",
    "No matching application logs.": "沒有相符的應用程式記錄。",
    "Logs are available only in developer mode.": "記錄只可在開發者模式使用。",
    "Logs copied.": "已複製記錄。",
    "Copy is not available in this browser.": "此瀏覽器不支援複製功能。",
    "Collecting recent OCR performance samples.": "正在收集近期 OCR 效能樣本。",
    "Withdrawal recovery requires attention.": "提款復原需要處理。",
    "This phone needs attention.": "此手機需要處理。",
    "Retry queued from the selected failed step.": "已從所選失敗步驟排入重試。",
    "Workflow restart queued from step 0.": "已從第 0 步排入重新開始工作流程。",
    "logical fallback": "邏輯核心後備值",
    "Calibration required for this phone.": "此手機需要校正。",
    "Phone marked Ready for Work.": "已將手機標記為可執行工作。",
    "Phone set Not Ready.": "已將手機設為未就緒。",
    "Wait Until Stable": "等待畫面穩定",
    "Manual Checkpoint": "手動檢查點",
    "If Text": "如有文字",
    "Job needs attention": "工作需要處理",
    "Complete the requested phone action, then use Resume Checkpoint.":
      "完成所要求的手機操作，然後使用「從檢查點繼續」。",
    "Workflow needs attention": "工作流程需要處理",
    "Inspect the phone before deciding whether to restart the workflow.":
      "決定是否重新開始工作流程前，請先檢查手機。",
    "Balance Check Workflow saved.": "已儲存餘額檢查工作流程。",
    "Payment Workflow saved.": "已儲存付款工作流程。",
    "Phone Connected": "手機已連線",
    "Pi Online / Phone Waiting": "Pi 在線／等待手機",
    "Finish or cancel calibration before switching phones.":
      "切換手機前，請先完成或取消校正。",
    "Controller busy. Wait for the current action to finish.":
      "控制器忙碌中，請等待目前操作完成。",
    "Pi temporarily unavailable.": "Pi 暫時無法使用。",
    "Phone geometry changed during calibration. Start calibration again.":
      "校正期間手機幾何位置已變更，請重新開始校正。",
    "Calibration saved. Input remains disabled until you enable Manual Control.":
      "校正已儲存；啟用手動控制前，輸入仍會停用。",
    "Sending...": "傳送中…",
    "+ Capture New Keyboard": "+ 擷取新鍵盤",
    "At the beginning": "在開頭",
    "Maximum wait must allow the requested stability window.":
      "最長等待時間必須足以涵蓋所要求的穩定時間。",
    "The phone could not keep up. Recording stopped; review the captured steps.":
      "手機未能及時處理。錄製已停止，請檢視已擷取的步驟。",
    "Manual Control could not be enabled. Special Case setup was not opened.":
      "無法啟用手動控制，因此未開啟特殊情況設定。",
    "Special Case setup opened. Manual Control is enabled so you can position the phone.":
      "已開啟特殊情況設定；手動控制已啟用，讓你調整手機畫面。",
    "Special Case saved. Review continues.": "特殊情況已儲存，繼續檢視。",
    "Special Case saved. Main recording continues.":
      "特殊情況已儲存，主錄製會繼續。",
    "Special Case cancelled. Actions already performed on the phone were not reversed.":
      "已取消特殊情況；手機上已執行的操作不會復原。",
    "Special Case cancelled. Main recording continues; actions already performed on the phone were not reversed.":
      "已取消特殊情況；主錄製會繼續，手機上已執行的操作不會復原。",
    "Typing default value...": "正在輸入預設值…",
    "Recording started. Manual Control is enabled — use the phone preview normally.":
      "錄製已開始。手動控制已啟用，請如常使用手機預覽。",
    "Recording stopped. Review your steps, enter the bank name, then save.":
      "錄製已停止。請檢視步驟、輸入銀行名稱，然後儲存。",
    "Changes discarded.": "已捨棄變更。",
    "Recording discarded.": "已捨棄錄製內容。",
    "Calibrate a live camera view before capturing keys.":
      "擷取按鍵前，請先校正相機即時畫面。",
    "Phone preview": "手機預覽",
    "Recording stopped; inspect the phone before continuing.":
      "錄製已停止；繼續前請先檢查手機。",
    "Recording stopped. Input may already have occurred; inspect the phone before continuing. No retry attempted.":
      "錄製已停止。輸入可能已經發生；繼續前請先檢查手機。系統沒有嘗試重試。",
    "Typing result uncertain; it was not retried automatically.":
      "輸入結果不確定；系統沒有自動重試。",
    "Action result uncertain; it was not retried automatically.":
      "操作結果不確定；系統沒有自動重試。",
  };
  for (const [source, translation] of Object.entries(additionalStaticZhHK)) {
    const key = `static.${source}`;
    en[key] = source;
    zhHK[key] = translation;
    if (!staticTextKeys.has(source)) staticTextKeys.set(source, key);
  }
  Object.assign(en, {
    "camera.current": "Camera {index} · {width} × {height}",
    "camera.rotation": "{name} · {rotation}°",
    "camera.clickCorner": "Click the phone display’s {corner} corner.",
    "camera.corner.top-left": "top-left",
    "camera.corner.top-right": "top-right",
    "camera.corner.bottom-right": "bottom-right",
    "camera.corner.bottom-left": "bottom-left",
    "device.selectForControl": "Select {name} for control",
    "device.livePreview": "{name} live camera preview",
    "error.recoveryStart": "Recovery could not start: {message}",
    "error.manualEnable": "Manual control could not be enabled: {message}",
    "error.markReady": "Phone could not be marked ready: {message}",
    "error.verifyReady": "Ready state could not be verified: {message}",
    "workflow.chooseExpectedAfter": "Choose expected text after {step}",
    "workflow.chooseExpectedCurrent":
      "Choose expected text for the current screen",
    "workflow.variableValue": "Variable: {label}",
    "workflow.specialSummary":
      "Looking for: “{text}” · {count} recorded {action} · If not shown: Continue normally",
    "workflow.action.one": "action",
    "workflow.action.many": "actions",
    "workflow.recordingCount": "● Recording • {count} {step}",
    "workflow.step.one": "step",
    "workflow.step.many": "steps",
    "workflow.lookingFor": "Looking for: “{text}”",
    "workflow.captureInstruction": "Tap {key} on the phone preview.",
    "workflow.keyboardSummary": "{kind} · {count} keys",
    "workflow.keyboardUsage": "Used by {count} workflow{suffix}",
    "workflow.notAssignedTo": "Not assigned to {device}",
    "workflow.backendSummary": "Input: {input} · Screen: {screen}",
    "workflow.step.waitStable":
      "Wait for Screen to Settle — Stable for {stable} ms · Max {seconds} sec",
    "workflow.step.verify": "Verify Text — {field}",
    "workflow.step.specialCase": "Special Case — “{text}”",
    "workflow.specialActive":
      "Looking for: “{text}” · {count} recorded {action}",
    "workflow.runState": "{status} · Step {current}/{total} · {detail}",
    "workflow.keyboardUsedBy": "Used by: {uses}",
    "workflow.readResult": "Read Balance: {value}",
    "workflow.targetDetected":
      "Target detected in selected snapshot crop · {texts}",
    "workflow.targetNotDetected":
      "Target not detected in selected snapshot crop · {texts}",
    "diagnostics.logCount": "{count} recent log {entry}.",
    "diagnostics.entry.one": "entry",
    "diagnostics.entry.many": "entries",
    "ocr.health": "Healthy · {count} {worker} · Queue {queue}",
    "ocr.worker.one": "worker",
    "ocr.worker.many": "workers",
    "jobs.queuePosition": "{status} · queue {position}",
    "workflow.afterStep": "After {number}. {step}",
    "error.ocrStatus": "OCR status unavailable: {message}",
    "error.ocrSettings": "OCR settings unavailable: {message}",
    "error.backendUnavailable": "Backend unavailable: {message}",
  });
  Object.assign(zhHK, {
    "camera.current": "相機 {index} · {width} × {height}",
    "camera.rotation": "{name} · 旋轉 {rotation}°",
    "camera.clickCorner": "點按手機顯示範圍的{corner}。",
    "camera.corner.top-left": "左上角",
    "camera.corner.top-right": "右上角",
    "camera.corner.bottom-right": "右下角",
    "camera.corner.bottom-left": "左下角",
    "device.selectForControl": "選擇 {name} 作為控制目標",
    "device.livePreview": "{name} 的相機即時預覽",
    "error.recoveryStart": "無法開始復原：{message}",
    "error.manualEnable": "無法啟用手動控制：{message}",
    "error.markReady": "無法將手機標記為就緒：{message}",
    "error.verifyReady": "無法驗證就緒狀態：{message}",
    "workflow.chooseExpectedAfter": "選擇「{step}」之後的預期文字",
    "workflow.chooseExpectedCurrent": "選擇目前畫面的預期文字",
    "workflow.variableValue": "變數：{label}",
    "workflow.specialSummary":
      "正在尋找：「{text}」· 已錄製 {count} 項操作 · 如未顯示：如常繼續",
    "workflow.action.one": "項操作",
    "workflow.action.many": "項操作",
    "workflow.recordingCount": "● 錄製中 • {count} 個步驟",
    "workflow.step.one": "個步驟",
    "workflow.step.many": "個步驟",
    "workflow.lookingFor": "正在尋找：「{text}」",
    "workflow.captureInstruction": "在手機預覽上點按 {key}。",
    "workflow.keyboardSummary": "{kind} · {count} 個按鍵",
    "workflow.keyboardUsage": "由 {count} 個工作流程使用",
    "workflow.notAssignedTo": "未指派給 {device}",
    "workflow.backendSummary": "輸入：{input} · 畫面：{screen}",
    "workflow.step.waitStable":
      "等待畫面穩定 — 穩定 {stable} 毫秒 · 最長 {seconds} 秒",
    "workflow.step.verify": "驗證文字 — {field}",
    "workflow.step.specialCase": "特殊情況 —「{text}」",
    "workflow.specialActive": "正在尋找：「{text}」· 已錄製 {count} 項操作",
    "workflow.runState": "{status} · 步驟 {current}/{total} · {detail}",
    "workflow.keyboardUsedBy": "使用者：{uses}",
    "workflow.readResult": "讀取餘額：{value}",
    "workflow.targetDetected": "在所選快照裁剪範圍內偵測到目標 · {texts}",
    "workflow.targetNotDetected": "在所選快照裁剪範圍內未偵測到目標 · {texts}",
    "diagnostics.logCount": "近期有 {count} 項記錄。",
    "diagnostics.entry.one": "項記錄",
    "diagnostics.entry.many": "項記錄",
    "ocr.health": "正常 · {count} 個工作程序 · 佇列 {queue}",
    "ocr.worker.one": "個工作程序",
    "ocr.worker.many": "個工作程序",
    "jobs.queuePosition": "{status} · 佇列位置 {position}",
    "workflow.afterStep": "在第 {number} 步「{step}」之後",
    "error.ocrStatus": "無法取得 OCR 狀態：{message}",
    "error.ocrSettings": "無法取得 OCR 設定：{message}",
    "error.backendUnavailable": "後端無法使用：{message}",
  });
  const textBindings = new Set();
  const attributeBindings = new Set();
  const staticAttributeKeys = new Map(
    Object.entries({
      Language: "language.label",
      "Dashboard mode": "ui.dashboardModeLabel",
      "Search by workflow name or ID": "ui.searchWorkflowPlaceholder",
      "Select a camera, index, or camera URL": "ui.cameraPlaceholder",
      "camera, OCR, workflow…": "ui.logPlaceholder",
      "e.g. Hong Leong": "ui.bankPlaceholder",
      "Custom workflow variable…": "ui.variablePlaceholder",
      "Authenticator setup QR code": "auth.qrAlt",
      "Connection details": "static.Connection details",
      "Pi connection status": "static.Pi connection status",
      "Recent application logs": "static.Recent application logs",
      Close: "ui.close",
      "Test camera preview": "static.Test camera preview",
      "Live external camera. Click to tap or drag to swipe.":
        "static.Live external camera. Click to tap or drag to swipe.",
      "Phone preview. Click, drag and type to record in mock mode.":
        "static.Phone preview. Click, drag and type to record in mock mode.",
      "Recorded steps": "static.Recorded steps",
      "Step type": "static.Step type",
      "Wait seconds": "static.Wait seconds",
      Transfer: "static.Transfer",
      "Recent workflow activity": "static.Recent workflow activity",
      "Workflow execution logs": "static.Workflow execution logs",
    }),
  );

  function normalizeLocale(value) {
    return SUPPORTED_LOCALES.has(value) ? value : DEFAULT_LOCALE;
  }

  function readStoredLocale() {
    try {
      return normalizeLocale(root.localStorage?.getItem(STORAGE_KEY));
    } catch {
      return DEFAULT_LOCALE;
    }
  }

  let locale = readStoredLocale();

  function interpolate(value, params) {
    return value.replace(/\{([^}]+)\}/g, (match, name) =>
      Object.prototype.hasOwnProperty.call(params || {}, name)
        ? String(params[name])
        : match,
    );
  }

  function t(key, params) {
    const value = dictionaries[locale]?.[key] ?? en[key] ?? key;
    return interpolate(value, params);
  }

  function translateEnglish(source) {
    for (const [key, value] of Object.entries(en)) {
      if (value === source) return t(key);
    }
    return source;
  }

  function translateError(source) {
    if (typeof source !== "string" || !source) return source;
    const uncertainSuffix = " Input may have occurred; no retry attempted.";
    const hasUncertainSuffix = source.endsWith(uncertainSuffix);
    const message = hasUncertainSuffix
      ? source.slice(0, -uncertainSuffix.length)
      : source;
    const patterns = [
      [
        /^Pi API (\S+) timed out\.$/,
        "error.piApiTimeout",
        ([, path]) => ({ path }),
      ],
      [
        /^Pi API (\S+) connected without a socket\.$/,
        "error.piApiNoSocket",
        ([, path]) => ({ path }),
      ],
      [
        /^Pi API (\S+) returned HTTP (\d{3})\.$/,
        "error.piApiHttp",
        ([, path, status]) => ({ path, status }),
      ],
      [
        /^Pi API response exceeded the size limit\.$/,
        "error.piApiTooLarge",
        () => ({}),
      ],
      [
        /^Pi API (\S+) returned invalid JSON\.$/,
        "error.piApiInvalidJson",
        ([, path]) => ({ path }),
      ],
      [
        /^Pi API (\S+) returned an invalid response \(expected boolean ok\)\.$/,
        "error.piApiInvalidResponse",
        ([, path]) => ({ path }),
      ],
      [
        /^Pi rejected (\S+)\. Check the Pi service and phone connection\.$/,
        "error.piRejectedService",
        ([, path]) => ({ path }),
      ],
      [
        /^Pi rejected (\S+)\. Check the Bluetooth HID connection to the phone\.$/,
        "error.piRejectedHid",
        ([, path]) => ({ path }),
      ],
      [
        /^Cannot reach Pi API for (\S+)\. Check the Pi address and private Wi-Fi\.$/,
        "error.piApiUnreachable",
        ([, path]) => ({ path }),
      ],
      [
        /^Pi \/status response is missing boolean connected\.$/,
        "error.piStatusInvalid",
        () => ({}),
      ],
    ];
    for (const [pattern, key, params] of patterns) {
      const match = message.match(pattern);
      if (!match) continue;
      return (
        t(key, params(match)) +
        (hasUncertainSuffix ? t("error.physicalMayHaveOccurred") : "")
      );
    }
    if (/^The operation was aborted due to timeout\.?$/i.test(message))
      return t("error.browserTimeout");
    if (
      /^(?:Failed to fetch|Load failed|Network request failed|NetworkError when attempting to fetch resource\.)$/i.test(
        message,
      )
    )
      return t("error.networkRequestFailed");
    return translateEnglish(source);
  }

  function bindText(node, key, params) {
    if (!node) return node;
    node.__phoneI18n = { key, params };
    textBindings.add(node);
    node.textContent = t(key, typeof params === "function" ? params() : params);
    return node;
  }

  function bindComputedText(node, compute) {
    if (!node) return node;
    node.__phoneI18n = { compute };
    textBindings.add(node);
    node.textContent = compute();
    return node;
  }

  function bindAttribute(node, attribute, key, params) {
    if (!node) return node;
    const binding = { node, attribute, key, params };
    attributeBindings.add(binding);
    node.setAttribute(
      attribute,
      t(key, typeof params === "function" ? params() : params),
    );
    return node;
  }

  function captureStaticText(doc) {
    if (!doc?.createTreeWalker || !root.NodeFilter) return;
    const walker = doc.createTreeWalker(doc.body, root.NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const trimmed = node.nodeValue.trim().replace(/\s+/g, " ");
      const key = staticTextKeys.get(trimmed);
      if (!key) continue;
      const leading = node.nodeValue.match(/^\s*/)?.[0] || "";
      const trailing = node.nodeValue.match(/\s*$/)?.[0] || "";
      node.__phoneI18nWhitespace = { leading, trailing };
      node.__phoneI18n = { key };
      textBindings.add(node);
    }
  }

  function captureStaticAttributes(doc) {
    if (!doc?.querySelectorAll) return;
    for (const node of doc.querySelectorAll(
      "[placeholder], [aria-label], [title], img[alt]",
    )) {
      for (const attribute of ["placeholder", "aria-label", "title", "alt"]) {
        const value = node.getAttribute?.(attribute);
        const key = staticAttributeKeys.get(value);
        if (key) bindAttribute(node, attribute, key);
      }
    }
  }

  function translateBoundContent() {
    for (const node of [...textBindings]) {
      if (node.isConnected === false) {
        textBindings.delete(node);
        continue;
      }
      const binding = node.__phoneI18n;
      if (!binding) continue;
      const value = binding.compute
        ? binding.compute()
        : t(
            binding.key,
            typeof binding.params === "function"
              ? binding.params()
              : binding.params,
          );
      if (node.nodeType === 3 && node.__phoneI18nWhitespace) {
        const { leading, trailing } = node.__phoneI18nWhitespace;
        node.nodeValue = `${leading}${value}${trailing}`;
      } else {
        node.textContent = value;
      }
    }
    for (const binding of [...attributeBindings]) {
      if (binding.node.isConnected === false) {
        attributeBindings.delete(binding);
        continue;
      }
      binding.node.setAttribute(
        binding.attribute,
        t(
          binding.key,
          typeof binding.params === "function"
            ? binding.params()
            : binding.params,
        ),
      );
    }
    if (root.document?.documentElement)
      root.document.documentElement.lang = locale;
    if (root.document?.title)
      root.document.title = t(
        root.document.body?.classList?.contains("auth-page")
          ? "auth.title"
          : "app.title",
      );
    const selector = root.document?.getElementById?.("language-selector");
    if (selector) selector.value = locale;
  }

  function setLocale(nextLocale) {
    locale = normalizeLocale(nextLocale);
    try {
      root.localStorage?.setItem(STORAGE_KEY, locale);
    } catch {
      /* The selected language still applies for this page session. */
    }
    translateBoundContent();
    if (typeof root.CustomEvent === "function" && root.dispatchEvent)
      root.dispatchEvent(
        new root.CustomEvent("phonecontroller:languagechange", {
          detail: { locale },
        }),
      );
    return locale;
  }

  function init(doc = root.document) {
    if (!doc) return;
    captureStaticText(doc);
    captureStaticAttributes(doc);
    const selector = doc.getElementById?.("language-selector");
    if (selector && !selector.__phoneI18nListener) {
      selector.__phoneI18nListener = true;
      selector.addEventListener("change", () => setLocale(selector.value));
    }
    const label = doc.querySelector?.('[for="language-selector"]');
    if (label) bindText(label, "language.label");
    translateBoundContent();
  }

  const api = {
    DEFAULT_LOCALE,
    STORAGE_KEY,
    dictionaries,
    getLocale: () => locale,
    setLocale,
    t,
    translateEnglish,
    translateError,
    bindText,
    bindComputedText,
    bindAttribute,
    init,
  };
  root.PhoneI18n = api;
  root.t = t;

  if (root.document) {
    if (root.document.readyState === "loading")
      root.document.addEventListener("DOMContentLoaded", () => init(), {
        once: true,
      });
    else init();
  }

  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
