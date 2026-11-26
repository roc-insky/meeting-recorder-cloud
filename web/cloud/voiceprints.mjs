// 视觉草案，未获 G4 批准：本模块只保证真实录音、真实接口与可达性，尚未完成视觉签收。
import { createCloudSession } from "./session.mjs";
import { currentPath, renderLoginGate, setGuestMode } from "./gate.mjs";

const PROFILES_PATH = "/api/cloud/voiceprints";
const TASKS_PATH = "/api/cloud/voiceprints/tasks";
const CONSENT_TEXT = "我同意将本次录音用于提取本人声纹，仅匹配我自己的会议记录。";
const TASK_POLL_MS = 1000;
const MIME_CANDIDATES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];

const TASK_ERROR_TEXT = {
  voiceprint_extraction_failed:
    "未能从录音中提取声纹：请在安静环境照着提示朗读至少 15 秒（有效语音不足 10 秒会失败）后重试。",
  no_voiceprint_processor: "声纹处理服务未启动，请联系管理员后重试。",
  processing_failed: "声纹处理服务出错，请稍后重试。",
  voiceprint_sample_missing: "录音样本已丢失，请重新录音。",
  invalid_voiceprint_audio: "录音无效，请重新录制。",
  voiceprint_sample_too_large: "录音文件过大，请缩短录音后重试。",
  unsupported_voiceprint_media: "浏览器生成的录音格式不受支持，请更新浏览器后重试。",
  voiceprint_key_missing: "声纹服务暂时不可用，请稍后重试。",
  voiceprint_version_changed: "声纹版本已更新，请重新开始登记。",
  invalid_voiceprint_embedding: "声纹提取结果无效，请重新录音。",
  cancelled_by_user: "已取消本次处理。",
};

function element(doc, name, attributes = {}, text = "") {
  const node = doc.createElement(name);
  for (const [key, value] of Object.entries(attributes)) {
    if (value === undefined || value === null || value === false) continue;
    node.setAttribute(key === "className" ? "class" : key, value === true ? "" : String(value));
  }
  if (text) node.textContent = text;
  return node;
}

function normalizeName(value) {
  return String(value || "").normalize("NFC").trim();
}

function shortUserId(userId) {
  const text = String(userId || "");
  return text.length > 16 ? text.slice(0, 12) + "…" : text;
}

function formatTime(value) {
  if (!value) return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(date);
  } catch {
    return date.toISOString();
  }
}

function modelText(profile) {
  const metadata = profile && profile.metadata ? profile.metadata : {};
  const parts = [metadata.model_id, metadata.model_version].filter(Boolean);
  return parts.length ? parts.join(" / ") : "未知模型";
}

function profileVersion(profile) {
  const version = Number(profile && profile.version);
  return Number.isFinite(version) ? version : 0;
}

function isUnauthorized(error) {
  return Boolean(error && error.status === 401);
}

function isRequestError(error) {
  return Boolean(error && typeof error.status === "number");
}

function pageErrorText(error) {
  if (error && error.status >= 500) return "服务暂时不可用，请稍后重试。";
  if (error && error.status === 404) return "声纹接口暂不可用，请稍后重试。";
  return "无法加载声纹库，请重试。";
}

function errorCode(error) {
  return error && typeof error.code === "string" ? error.code : "";
}

function taskErrorText(code) {
  return TASK_ERROR_TEXT[code] || "声纹处理失败，请重新录音或稍后再试。";
}

function pickMimeType(win) {
  const Recorder = win.MediaRecorder;
  if (!Recorder || typeof Recorder.isTypeSupported !== "function") return "";
  for (const candidate of MIME_CANDIDATES) {
    try {
      if (Recorder.isTypeSupported(candidate)) return candidate;
    } catch {
      // Ignore a browser that cannot answer; try the next candidate.
    }
  }
  return "";
}

function audioFileName(type) {
  const value = String(type || "").toLowerCase();
  if (value.includes("mp4")) return "sample.mp4";
  if (value.includes("ogg")) return "sample.ogg";
  if (value.includes("wav")) return "sample.wav";
  return "sample.webm";
}

function formatSeconds(total) {
  const safe = Math.max(0, Math.floor(total));
  const minutes = String(Math.floor(safe / 60)).padStart(2, "0");
  const seconds = String(safe % 60).padStart(2, "0");
  return minutes + ":" + seconds;
}

export function mountVoiceprints(root, options = {}) {
  if (!root) throw new Error("voiceprints root is required");
  const doc = root.ownerDocument;
  const win = doc.defaultView;
  const client = options.client || createCloudSession();

  const account = doc.getElementById("account");
  const enrollDialog = doc.getElementById("enroll-dialog");
  const revokeDialog = doc.getElementById("revoke-dialog");
  if (!account || !enrollDialog || !revokeDialog) {
    throw new Error("voiceprints page is missing required elements");
  }

  const enrollForm = enrollDialog.querySelector("#enroll-form");
  const nameInput = enrollDialog.querySelector('[data-testid="enroll-name"]');
  const consentInput = enrollDialog.querySelector('[data-testid="enroll-consent"]');
  const replaceConfirm = enrollDialog.querySelector('[data-testid="replace-confirm"]');
  const replaceConfirmField = enrollDialog.querySelector(
    '[data-testid="replace-confirm-field"]',
  );
  const replaceConfirmLabel = enrollDialog.querySelector(
    '[data-testid="replace-confirm-label"]',
  );
  const replaceWarning = enrollDialog.querySelector('[data-testid="replace-warning"]');
  const enrollTitle = enrollDialog.querySelector('[data-testid="enroll-title"]');
  const recordStatus = enrollDialog.querySelector('[data-testid="record-status"]');
  const recordSeconds = enrollDialog.querySelector('[data-testid="record-seconds"]');
  const enrollError = enrollDialog.querySelector('[data-testid="enroll-error"]');
  const enrollStatus = enrollDialog.querySelector('[data-testid="enroll-status"]');
  const enrollConflict = enrollDialog.querySelector('[data-testid="enroll-conflict"]');
  const enrollConflictText = enrollDialog.querySelector(
    '[data-testid="enroll-conflict-text"]',
  );
  const startButton = enrollDialog.querySelector('[data-testid="enroll-start"]');
  const saveButton = enrollDialog.querySelector('[data-testid="enroll-save"]');
  const cancelButton = enrollDialog.querySelector('[data-testid="enroll-cancel"]');
  const cancelTaskButton = enrollDialog.querySelector(
    '[data-testid="enroll-cancel-task"]',
  );
  const useReplaceButton = enrollDialog.querySelector(
    '[data-testid="enroll-use-replace"]',
  );
  const revokeName = revokeDialog.querySelector('[data-testid="revoke-name"]');
  const revokeError = revokeDialog.querySelector('[data-testid="revoke-error"]');
  const revokeConfirm = revokeDialog.querySelector('[data-testid="revoke-confirm"]');
  const revokeCancel = revokeDialog.querySelector('[data-testid="revoke-cancel"]');

  if (
    !nameInput ||
    !consentInput ||
    !replaceConfirm ||
    !replaceConfirmField ||
    !startButton ||
    !saveButton ||
    !cancelButton ||
    !cancelTaskButton
  ) {
    throw new Error("enrollment dialog is missing required controls");
  }

  const state = {
    session: null,
    loginRequired: false,
    loginUrl: "",
    loading: true,
    loaded: false,
    profiles: [],
    error: "",
    announcement: "",
  };

  const enroll = {
    mode: "create",
    profile: null,
    trigger: null,
    stream: null,
    recorder: null,
    chunks: [],
    mimeType: "",
    recording: false,
    saving: false,
    processing: false,
    completed: false,
    taskId: null,
    taskStatus: "",
    seconds: 0,
    ticker: null,
    pollTimer: null,
    generation: 0,
    conflictName: "",
  };

  const revoke = {
    profile: null,
    trigger: null,
    busy: false,
  };

  function renderAccount() {
    account.replaceChildren();
    if (state.session && state.session.user_id) {
      account.append(
        element(doc, "span", { className: "account-label" }, "账号 "),
        element(
          doc,
          "span",
          { className: "account-id" },
          shortUserId(state.session.user_id),
        ),
      );
      return;
    }
    if (state.loginRequired) {
      const link = element(doc, "a", { href: state.loginUrl || client.loginUrl() }, "登录");
      account.append(link);
    }
  }

  function buildProfileCard(profile, index) {
    const card = element(doc, "li", {
      className: "profile-card",
      "data-testid": "profile-card",
      "data-index": String(index),
    });
    const heading = element(doc, "div", { className: "profile-heading" });
    heading.append(
      element(
        doc,
        "h3",
        { className: "profile-name", "data-testid": "profile-name" },
        String(profile.name || "未命名声纹"),
      ),
      element(
        doc,
        "span",
        { className: "badge", "data-testid": "profile-version" },
        "版本 " + profileVersion(profile),
      ),
    );
    const modelLine = element(doc, "p", { className: "profile-meta" });
    modelLine.append(
      "模型 ",
      element(
        doc,
        "code",
        { className: "profile-model", "data-testid": "profile-model" },
        modelText(profile),
      ),
    );
    card.append(
      heading,
      modelLine,
      element(
        doc,
        "p",
        { className: "profile-meta", "data-testid": "profile-captured" },
        "采集于 " + formatTime(profile.captured_at),
      ),
    );
    const actions = element(doc, "div", { className: "profile-actions" });
    actions.append(
      element(
        doc,
        "button",
        {
          className: "btn",
          type: "button",
          "data-testid": "profile-replace",
          "data-action": "replace",
        },
        "替换此声纹",
      ),
      element(
        doc,
        "button",
        {
          className: "btn btn--danger",
          type: "button",
          "data-testid": "profile-revoke",
          "data-action": "revoke",
        },
        "撤销",
      ),
    );
    card.append(actions);
    return card;
  }

  function buildLibrary() {
    const page = element(doc, "div", { className: "voiceprints" });
    const header = element(doc, "header", { className: "voiceprints-header" });
    const headingBlock = element(doc, "div", { className: "voiceprints-heading" });
    headingBlock.append(
      element(doc, "h1", {}, "我的声纹库"),
      element(
        doc,
        "p",
        { className: "voiceprints-lede" },
        "声纹只用于识别你自己的会议记录，不会跨账号或平台匹配。",
      ),
    );
    header.append(headingBlock);
    page.append(header);

    const disclosure = element(
      doc,
      "p",
      {
        className: "notice notice--blocking voiceprint-disclosure",
        "data-testid": "revoke-disclosure",
      },
      "撤销只停止后续识别，不会自动改写已生成文字稿中的姓名；如需历史匿名化必须另行重跑。",
    );

    const pageStatus = element(doc, "p", {
      className: "form-status page-status",
      "data-testid": "page-status",
      role: "status",
    });
    pageStatus.textContent = state.announcement;
    pageStatus.hidden = !state.announcement;
    page.append(pageStatus);

    if (state.loading && !state.loaded) {
      const loading = element(doc, "section", {
        className: "status-panel",
        "data-testid": "loading-state",
        role: "status",
      });
      loading.textContent = "正在加载声纹库…";
      page.append(loading);
      return page;
    }

    if (state.error) {
      const errorPanel = element(doc, "section", {
        className: "status-panel error-state",
        "data-testid": "page-error",
        role: "alert",
      });
      errorPanel.append(
        element(doc, "p", {}, state.error),
        element(
          doc,
          "button",
          {
            className: "btn",
            type: "button",
            "data-action": "retry",
            "data-testid": "retry-load",
          },
          "重试",
        ),
      );
      page.append(errorPanel);
      return page;
    }

    const listSection = element(doc, "section", {
      className: "panel voiceprint-panel",
      "aria-labelledby": "voiceprint-list-title",
    });
    listSection.append(
      element(
        doc,
        "h2",
        { id: "voiceprint-list-title", className: "visually-hidden" },
        "已登记的声纹",
      ),
    );
    const list = element(doc, "ul", {
      className: "profile-list",
      "data-testid": "profile-list",
    });
    state.profiles.forEach((profile, index) => {
      list.append(buildProfileCard(profile, index));
    });
    listSection.append(list);

    if (!state.profiles.length) {
      listSection.append(
        element(
          doc,
          "p",
          { className: "empty-inline profiles-empty", "data-testid": "profiles-empty" },
          "尚无已登记的声纹。",
        ),
      );
    }
    // 三栏：左栏登记入口，中栏声纹列表，右栏撤销与使用说明。
    const grid = element(doc, "div", { className: "page-grid" });

    const rail = element(doc, "aside", {
      className: "page-rail",
      "data-testid": "voiceprints-rail",
      "aria-label": "声纹登记",
    });
    const enrollCard = element(doc, "section", {
      className: "panel entry-panel",
      "data-testid": "enroll-panel",
      "aria-labelledby": "enroll-panel-title",
    });
    const enrollBody = element(doc, "div", { className: "panel-body" });
    enrollBody.append(
      element(doc, "h2", { id: "enroll-panel-title" }, "登记声纹"),
      element(
        doc,
        "p",
        { className: "field-hint" },
        "录制一段本人语音，会议文字稿里就能认出是谁在说话。",
      ),
      element(
        doc,
        "button",
        {
          className: "btn btn--primary",
          type: "button",
          "data-testid": "enroll-open",
          "data-action": "enroll",
        },
        "新增声纹",
      ),
    );
    enrollCard.append(enrollBody);
    rail.append(enrollCard);

    const main = element(doc, "div", {
      className: "page-main",
      "data-testid": "voiceprints-main",
    });
    main.append(listSection);

    const side = element(doc, "aside", {
      className: "page-side",
      "data-testid": "voiceprints-side",
      "aria-label": "说明",
    });
    const guide = element(doc, "section", {
      className: "panel guide-panel",
      "aria-labelledby": "voiceprint-guide-title",
    });
    const guideBody = element(doc, "div", { className: "panel-body" });
    guideBody.append(
      element(doc, "h2", { id: "voiceprint-guide-title" }, "使用须知"),
      element(
        doc,
        "ul",
        { className: "guide-list" },
        element(doc, "li", {}, "声纹只匹配你自己的会议记录，不会跨账号或平台比对。"),
        element(doc, "li", {}, "每段录音只用于提取本人声纹，保存的是声纹模型，不是原始音频。"),
        element(doc, "li", {}, "替换成功后旧版本失效；失败时仍保留旧版本，识别不受影响。"),
      ),
    );
    guide.append(guideBody);
    side.append(disclosure, guide);

    grid.append(rail, main, side);
    page.append(grid);
    return page;
  }

  function buildSessionError() {
    const panel = element(doc, "section", {
      className: "status-panel error-state",
      "data-testid": "page-error",
      role: "alert",
    });
    panel.append(
      element(doc, "p", {}, state.error),
      element(
        doc,
        "button",
        {
          className: "btn",
          type: "button",
          "data-action": "retry",
          "data-testid": "retry-load",
        },
        "重试",
      ),
    );
    return panel;
  }

  function render() {
    renderAccount();
    setGuestMode(doc, state.loginRequired);
    root.replaceChildren();
    if (state.loginRequired) {
      root.append(renderLoginGate(doc, { next: currentPath(doc) }));
      return;
    }
    if (!state.session && state.error) {
      root.append(buildSessionError());
      return;
    }
    root.append(buildLibrary());
  }

  function requireLogin() {
    state.loginRequired = true;
    state.loginUrl = client.loginUrl();
    state.session = null;
    state.profiles = [];
    state.loading = false;
    state.loaded = false;
    if (enrollDialog.open) enrollDialog.close();
    if (revokeDialog.open) revokeDialog.close();
    render();
  }

  async function refreshProfiles() {
    const payload = await client.request(PROFILES_PATH);
    state.profiles = Array.isArray(payload) ? payload : [];
    state.loaded = true;
    state.error = "";
  }

  async function loadPage() {
    state.loading = true;
    state.error = "";
    state.loginRequired = false;
    try {
      // 会话检查必须先于任何业务 UI：未登录时页面只留登录卡片。
      const session = await client.session();
      if (!session) {
        requireLogin();
        return;
      }
      state.session = session;
      setGuestMode(doc, false);
      render();
      await refreshProfiles();
    } catch (problem) {
      if (isUnauthorized(problem)) {
        requireLogin();
        return;
      }
      state.error = pageErrorText(problem);
    } finally {
      state.loading = false;
      if (!state.loginRequired) render();
    }
  }

  function clearEnrollError() {
    enrollError.textContent = "";
    enrollError.hidden = true;
  }

  function showEnrollError(message) {
    enrollError.textContent = message;
    enrollError.hidden = false;
  }

  function clearConflict() {
    enroll.conflictName = "";
    enrollConflictText.textContent = "";
    enrollConflict.hidden = true;
  }

  function showConflict(name) {
    enroll.conflictName = name;
    enrollConflictText.textContent =
      "该姓名已存在有效声纹。若要更新，请明确改为替换，旧版本不会在保存前被改动。";
    enrollConflict.hidden = false;
  }

  function setRecordStatus(text) {
    recordStatus.textContent = text;
  }

  function setEnrollStatus(text) {
    enrollStatus.textContent = text;
  }

  function stopTicker() {
    if (enroll.ticker !== null) {
      win.clearInterval(enroll.ticker);
      enroll.ticker = null;
    }
  }

  function startTicker() {
    stopTicker();
    enroll.ticker = win.setInterval(() => {
      if (!enroll.recording) return;
      enroll.seconds += 1;
      recordSeconds.textContent = formatSeconds(enroll.seconds);
    }, 1000);
  }

  function stopTracks() {
    const stream = enroll.stream;
    enroll.stream = null;
    if (!stream) return;
    for (const track of stream.getTracks()) {
      try {
        track.stop();
      } catch {
        // A closed track is already effectively stopped.
      }
    }
  }

  function stopRecorderLocally() {
    const recorder = enroll.recorder;
    if (!recorder) return;
    try {
      recorder.ondataavailable = null;
      recorder.onstop = null;
      recorder.onerror = null;
      if (recorder.state !== "inactive") recorder.stop();
    } catch {
      // Stopping the stream below is the safety net.
    }
    enroll.recorder = null;
  }

  function stopPolling() {
    if (enroll.pollTimer !== null) {
      win.clearTimeout(enroll.pollTimer);
      enroll.pollTimer = null;
    }
  }

  function cancelTaskOnServer(taskId) {
    if (!taskId) return;
    void client
      .request(TASKS_PATH + "/" + encodeURIComponent(taskId) + "/cancel", {
        method: "POST",
      })
      .catch(() => {});
  }

  function teardownSession(cancelTask) {
    enroll.generation += 1;
    stopPolling();
    stopTicker();
    const taskId = enroll.taskId;
    const shouldCancel = Boolean(cancelTask && taskId && enroll.processing);
    enroll.taskId = null;
    enroll.processing = false;
    enroll.recording = false;
    enroll.saving = false;
    enroll.completed = false;
    enroll.seconds = 0;
    enroll.chunks = [];
    stopRecorderLocally();
    stopTracks();
    if (shouldCancel) cancelTaskOnServer(taskId);
  }

  function updateSaveState() {
    const hasName = nameInput.value.trim().length > 0;
    const needsReplaceConfirm = enroll.mode === "replace" && !replaceConfirm.checked;
    saveButton.disabled =
      enroll.saving ||
      enroll.processing ||
      !enroll.recording ||
      !hasName ||
      !consentInput.checked ||
      needsReplaceConfirm;
  }

  function updateEnrollControls() {
    const busy = enroll.saving || enroll.processing;
    nameInput.disabled = busy;
    consentInput.disabled = busy;
    replaceConfirm.disabled = busy;
    startButton.disabled = busy;
    startButton.hidden = enroll.recording || busy;
    cancelButton.disabled = enroll.saving;
    cancelTaskButton.hidden = !enroll.processing;
    cancelTaskButton.disabled = !enroll.processing;
    saveButton.hidden = enroll.processing;
    saveButton.textContent = enroll.saving ? "正在保存…" : "停止并保存";
    updateSaveState();
  }

  function resetEnrollFields() {
    const replacing = enroll.mode === "replace";
    enrollTitle.textContent = replacing ? "替换声纹" : "登记声纹";
    nameInput.value = replacing && enroll.profile ? String(enroll.profile.name || "") : "";
    nameInput.readOnly = replacing;
    nameInput.disabled = false;
    consentInput.checked = false;
    consentInput.disabled = false;
    replaceConfirm.checked = false;
    replaceConfirm.disabled = false;
    replaceConfirmField.hidden = !replacing;
    replaceConfirmLabel.textContent = replacing
      ? "我确认在保存成功后才替换版本 " + profileVersion(enroll.profile) + "，失败会保留旧版本。"
      : "";
    replaceWarning.hidden = !replacing;
    replaceWarning.textContent = replacing
      ? "保存成功后才替换版本 " +
        profileVersion(enroll.profile) +
        "；提取或保存失败会保留旧版本。"
      : "";
    clearEnrollError();
    clearConflict();
    setEnrollStatus("");
    setRecordStatus("未开始录音");
    recordSeconds.textContent = "00:00";
    enroll.seconds = 0;
    updateEnrollControls();
  }

  function openEnroll(profile, trigger) {
    teardownSession(true);
    enroll.mode = profile ? "replace" : "create";
    enroll.profile = profile || null;
    enroll.trigger = trigger || null;
    resetEnrollFields();
    if (!enrollDialog.open) enrollDialog.showModal();
    const focusTarget = profile ? consentInput : nameInput;
    try {
      focusTarget.focus();
    } catch {
      // Native dialog focus management remains the fallback.
    }
  }

  function handleEnrollClosed() {
    const trigger = enroll.trigger;
    teardownSession(true);
    enroll.mode = "create";
    enroll.profile = null;
    resetEnrollFields();
    enroll.trigger = null;
    const focusTarget =
      trigger && trigger.isConnected
        ? trigger
        : root.querySelector('[data-testid="enroll-open"]');
    if (focusTarget) {
      try {
        focusTarget.focus();
      } catch {
        // The page may have moved to the login gate.
      }
    }
  }

  async function startRecording() {
    if (enroll.recording || enroll.saving || enroll.processing) return;
    clearEnrollError();
    const name = normalizeName(nameInput.value);
    if (!name) {
      showEnrollError("请填写姓名（1–100 个字符）。");
      nameInput.focus();
      return;
    }
    if (!consentInput.checked) {
      showEnrollError("请先勾选授权声明，再开始录音。");
      consentInput.focus();
      return;
    }
    if (!win.navigator.mediaDevices || !win.navigator.mediaDevices.getUserMedia) {
      showEnrollError("当前浏览器无法录音，请使用支持麦克风的浏览器。");
      return;
    }
    if (!win.MediaRecorder) {
      showEnrollError("当前浏览器不支持录音编码，请更新浏览器后重试。");
      return;
    }

    let stream;
    try {
      stream = await win.navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
      });
    } catch {
      showEnrollError("无法访问麦克风，请检查浏览器权限后重试。");
      return;
    }

    if (!enrollDialog.open) {
      for (const track of stream.getTracks()) {
        try {
          track.stop();
        } catch {
          // Ignore an already ended track.
        }
      }
      return;
    }

    enroll.stream = stream;
    enroll.chunks = [];
    enroll.seconds = 0;
    recordSeconds.textContent = "00:00";

    const mimeType = pickMimeType(win);
    let recorder;
    try {
      recorder = mimeType
        ? new win.MediaRecorder(stream, { mimeType })
        : new win.MediaRecorder(stream);
    } catch {
      try {
        recorder = new win.MediaRecorder(stream);
      } catch {
        stopTracks();
        showEnrollError("当前浏览器无法开始录音，请更新浏览器后重试。");
        return;
      }
    }

    enroll.recorder = recorder;
    enroll.mimeType = recorder.mimeType || mimeType || "";
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) enroll.chunks.push(event.data);
    };
    recorder.onerror = () => {
      stopTicker();
      enroll.recording = false;
      stopTracks();
      setRecordStatus("录音发生错误");
      showEnrollError("录音中断，请重新开始；本次不会保存声纹。");
      updateEnrollControls();
    };

    try {
      recorder.start();
    } catch {
      stopRecorderLocally();
      stopTracks();
      showEnrollError("无法开始录音，请重试。");
      return;
    }

    enroll.recording = true;
    setEnrollStatus("");
    setRecordStatus("正在录音");
    startTicker();
    updateEnrollControls();
  }

  function stopRecorderAndTracks() {
    const recorder = enroll.recorder;
    if (!recorder || recorder.state === "inactive") {
      stopTracks();
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        stopTracks();
        resolve();
      };
      try {
        recorder.onstop = finish;
      } catch {
        finish();
        return;
      }
      try {
        recorder.stop();
      } catch {
        finish();
        return;
      }
      win.setTimeout(finish, 2000);
    });
  }

  function discardRecording() {
    stopTicker();
    stopRecorderLocally();
    stopTracks();
    enroll.chunks = [];
    enroll.recording = false;
    enroll.seconds = 0;
    recordSeconds.textContent = "00:00";
  }

  async function saveRecording() {
    if (!enroll.recording || enroll.saving || enroll.processing) return;
    clearEnrollError();
    const name = normalizeName(nameInput.value);
    if (!name) {
      showEnrollError("请填写姓名（1–100 个字符）。");
      nameInput.focus();
      return;
    }
    if (!consentInput.checked) {
      showEnrollError("请先勾选授权声明，再保存声纹。");
      consentInput.focus();
      return;
    }
    if (enroll.mode === "replace" && !replaceConfirm.checked) {
      showEnrollError("替换前请勾选确认：只有保存成功才会替换旧版本。");
      replaceConfirm.focus();
      return;
    }

    const replacing = enroll.mode === "replace";
    const profile = enroll.profile;
    enroll.saving = true;
    enroll.recording = false;
    stopTicker();
    setRecordStatus("录音已停止");
    setEnrollStatus("正在停止录音并准备上传…");
    updateEnrollControls();
    const generation = enroll.generation;

    try {
      await stopRecorderAndTracks();
      if (generation !== enroll.generation) return;

      const chunks = enroll.chunks.slice();
      enroll.chunks = [];
      enroll.recorder = null;
      if (!chunks.length) {
        throw new Error("empty-recording");
      }
      const type = enroll.mimeType || (chunks[0] && chunks[0].type) || "";
      const blob = new Blob(chunks, type ? { type } : undefined);
      if (!blob.size) throw new Error("empty-recording");

      const form = new FormData();
      form.append("name", name);
      form.append("consent", CONSENT_TEXT);
      form.append(
        "audio",
        new File([blob], audioFileName(type || blob.type), {
          type: type || blob.type || "application/octet-stream",
        }),
      );
      if (replacing && profile) {
        form.append("replace_version", String(profileVersion(profile)));
      }

      const task = await client.request(PROFILES_PATH, { method: "POST", body: form });
      if (generation !== enroll.generation) {
        if (task && task.id) cancelTaskOnServer(task.id);
        return;
      }
      if (!task || typeof task.id !== "string" || !task.id) {
        throw new Error("invalid-task");
      }

      enroll.taskId = task.id;
      enroll.taskStatus = String(task.status || "waiting");
      enroll.processing = true;
      enroll.saving = false;
      setRecordStatus("录音已停止");
      setEnrollStatus("处理中…");
      updateEnrollControls();
      schedulePoll();
    } catch (problem) {
      if (generation !== enroll.generation) return;
      enroll.saving = false;
      enroll.processing = false;
      enroll.taskId = null;
      discardRecording();
      setRecordStatus("未开始录音");
      if (isUnauthorized(problem)) {
        requireLogin();
        return;
      }
      if (
        isRequestError(problem) &&
        problem.status === 409 &&
        errorCode(problem) === "voiceprint_exists" &&
        enroll.mode === "create"
      ) {
        clearEnrollError();
        showConflict(name);
        setEnrollStatus("");
      } else if (isRequestError(problem) && problem.status === 409) {
        showEnrollError("声纹版本已变化，请刷新列表后重新开始替换。");
      } else if (isRequestError(problem) && problem.status === 503) {
        showEnrollError("声纹服务暂未配置完成（" + (errorCode(problem) || "503") + "），请联系管理员。");
      } else if (isRequestError(problem) && problem.status === 413) {
        showEnrollError(taskErrorText("voiceprint_sample_too_large"));
      } else if (isRequestError(problem) && problem.status === 415) {
        showEnrollError(taskErrorText("unsupported_voiceprint_media"));
      } else {
        showEnrollError("未能保存声纹，请重新录音或稍后再试。");
      }
      updateEnrollControls();
    } finally {
      if (generation === enroll.generation) {
        enroll.saving = false;
        updateEnrollControls();
      }
    }
  }

  function schedulePoll() {
    stopPolling();
    if (!enroll.taskId || !enroll.processing) return;
    const generation = enroll.generation;
    const taskId = enroll.taskId;
    enroll.pollTimer = win.setTimeout(() => {
      enroll.pollTimer = null;
      void pollTask(generation, taskId);
    }, TASK_POLL_MS);
  }

  async function pollTask(generation, taskId) {
    if (generation !== enroll.generation || taskId !== enroll.taskId) return;
    let task;
    try {
      task = await client.request(TASKS_PATH + "/" + encodeURIComponent(taskId));
    } catch (problem) {
      if (generation !== enroll.generation) return;
      if (isUnauthorized(problem)) {
        requireLogin();
        return;
      }
      setEnrollStatus("处理状态暂时无法获取，正在重试…");
      schedulePoll();
      return;
    }
    if (generation !== enroll.generation || taskId !== enroll.taskId) return;
    if (!task || typeof task.status !== "string") {
      setEnrollStatus("处理状态暂时无法获取，正在重试…");
      schedulePoll();
      return;
    }
    enroll.taskStatus = task.status;
    if (task.status === "completed") {
      await completeEnrollment();
      return;
    }
    if (task.status === "failed") {
      failEnrollment(task.error_code);
      return;
    }
    if (task.status === "cancelled") {
      cancelEnrollment();
      return;
    }
    setEnrollStatus("处理中…");
    schedulePoll();
  }

  async function completeEnrollment() {
    enroll.completed = true;
    enroll.processing = false;
    enroll.saving = false;
    enroll.recording = false;
    enroll.taskId = null;
    enroll.taskStatus = "completed";
    stopPolling();
    stopTicker();
    stopRecorderLocally();
    stopTracks();
    enroll.chunks = [];
    state.announcement = "声纹已登记";
    try {
      await refreshProfiles();
    } catch (problem) {
      if (isUnauthorized(problem)) {
        requireLogin();
        return;
      }
      state.announcement = "声纹已登记，但列表刷新失败，请刷新页面查看。";
    }
    render();
    if (enrollDialog.open) enrollDialog.close();
  }

  function failEnrollment(code) {
    enroll.processing = false;
    enroll.saving = false;
    enroll.taskId = null;
    enroll.taskStatus = "failed";
    stopPolling();
    discardRecording();
    setRecordStatus("未开始录音");
    setEnrollStatus("处理失败");
    showEnrollError(taskErrorText(code));
    updateEnrollControls();
  }

  function cancelEnrollment() {
    enroll.processing = false;
    enroll.saving = false;
    enroll.taskId = null;
    enroll.taskStatus = "cancelled";
    stopPolling();
    discardRecording();
    setRecordStatus("未开始录音");
    setEnrollStatus("已取消本次处理。");
    clearEnrollError();
    updateEnrollControls();
  }

  async function cancelProcessing() {
    if (!enroll.processing || !enroll.taskId) return;
    const taskId = enroll.taskId;
    enroll.generation += 1;
    enroll.processing = false;
    enroll.taskId = null;
    stopPolling();
    setEnrollStatus("正在取消…");
    updateEnrollControls();
    try {
      await client.request(TASKS_PATH + "/" + encodeURIComponent(taskId) + "/cancel", {
        method: "POST",
      });
    } catch (problem) {
      if (isUnauthorized(problem)) {
        requireLogin();
        return;
      }
      showEnrollError("取消请求失败，请稍后再试；已停止轮询。");
    }
    discardRecording();
    setRecordStatus("未开始录音");
    setEnrollStatus("已取消本次处理。");
    updateEnrollControls();
  }

  function closeEnrollDialog() {
    teardownSession(true);
    if (enrollDialog.open) enrollDialog.close();
  }

  function openRevoke(profile, trigger) {
    revoke.profile = profile;
    revoke.trigger = trigger || null;
    revoke.busy = false;
    revokeName.textContent = String(profile.name || "未命名声纹");
    revokeError.textContent = "";
    revokeError.hidden = true;
    revokeConfirm.disabled = false;
    revokeCancel.disabled = false;
    if (!revokeDialog.open) revokeDialog.showModal();
  }

  function handleRevokeClosed() {
    const trigger = revoke.trigger;
    revoke.profile = null;
    revoke.trigger = null;
    revoke.busy = false;
    revokeName.textContent = "";
    revokeError.textContent = "";
    revokeError.hidden = true;
    revokeConfirm.disabled = false;
    revokeCancel.disabled = false;
    const focusTarget =
      trigger && trigger.isConnected
        ? trigger
        : root.querySelector('[data-testid="enroll-open"]');
    if (focusTarget) {
      try {
        focusTarget.focus();
      } catch {
        // The page may have moved to the login gate.
      }
    }
  }

  async function confirmRevoke() {
    if (!revoke.profile || revoke.busy) return;
    const profile = revoke.profile;
    revoke.busy = true;
    revokeConfirm.disabled = true;
    revokeCancel.disabled = true;
    revokeError.hidden = true;
    try {
      await client.request(PROFILES_PATH + "/" + encodeURIComponent(profile.id), {
        method: "DELETE",
      });
    } catch (problem) {
      revoke.busy = false;
      revokeConfirm.disabled = false;
      revokeCancel.disabled = false;
      if (isUnauthorized(problem)) {
        requireLogin();
        return;
      }
      revokeError.textContent = "撤销失败，请稍后重试。该声纹仍保持有效。";
      revokeError.hidden = false;
      return;
    }
    state.profiles = state.profiles.filter((item) => item.id !== profile.id);
    state.announcement = "已撤销";
    render();
    revoke.busy = false;
    if (revokeDialog.open) revokeDialog.close();
  }

  root.addEventListener("click", (event) => {
    const target = event.target && event.target.closest ? event.target : null;
    if (!target) return;
    const actionNode = target.closest("[data-action]");
    if (!actionNode) return;
    const action = actionNode.getAttribute("data-action");
    if (action === "enroll") {
      openEnroll(null, actionNode);
      return;
    }
    if (action === "retry") {
      void loadPage();
      return;
    }
    const card = actionNode.closest('[data-testid="profile-card"]');
    if (!card) return;
    const index = Number(card.getAttribute("data-index"));
    const profile = state.profiles[index];
    if (!profile) return;
    if (action === "replace") openEnroll(profile, actionNode);
    if (action === "revoke") openRevoke(profile, actionNode);
  });

  enrollForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!startButton.hidden) void startRecording();
  });
  nameInput.addEventListener("input", () => {
    clearConflict();
    updateSaveState();
  });
  consentInput.addEventListener("change", updateSaveState);
  replaceConfirm.addEventListener("change", updateSaveState);
  startButton.addEventListener("click", () => void startRecording());
  saveButton.addEventListener("click", () => void saveRecording());
  cancelButton.addEventListener("click", closeEnrollDialog);
  cancelTaskButton.addEventListener("click", () => void cancelProcessing());
  useReplaceButton.addEventListener("click", () => {
    const targetName = normalizeName(enroll.conflictName || nameInput.value);
    const profile = state.profiles.find(
      (item) => normalizeName(item.name) === targetName,
    );
    if (!profile) {
      showEnrollError("未找到该姓名的有效声纹，请刷新列表后重试。");
      clearConflict();
      return;
    }
    openEnroll(profile, root.querySelector('[data-testid="enroll-open"]'));
  });

  revokeCancel.addEventListener("click", () => {
    if (revokeDialog.open) revokeDialog.close();
  });
  revokeConfirm.addEventListener("click", () => void confirmRevoke());

  enrollDialog.addEventListener("cancel", () => {
    // Escape can close the native dialog before the close event is queued.
    teardownSession(true);
  });
  enrollDialog.addEventListener("close", handleEnrollClosed);
  revokeDialog.addEventListener("close", handleRevokeClosed);

  const handlePageHide = () => teardownSession(true);
  win.addEventListener("pagehide", handlePageHide);

  void loadPage();

  return {
    destroy() {
      teardownSession(true);
      win.removeEventListener("pagehide", handlePageHide);
    },
  };
}