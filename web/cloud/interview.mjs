// Private, ephemeral interview-question workbench.
//
// Resumes, images and generated questions live only in this page's memory. The
// server stores positions, never AI input or output; this module therefore keeps
// no localStorage, sessionStorage, URL or IndexedDB copy of any of them.
import { createCloudSession } from "./session.mjs";
import { renderSafeMarkdown } from "./render.mjs";

const POSITIONS_PATH = "/api/cloud/positions";
const POSITION_TYPES = ["前端", "后端", "客户端", "测试"];
const RESUME_MAX_CHARS = 20000;
const IMAGE_MAX_BYTES = 5 * 1024 * 1024;
const IMAGE_TOTAL_MAX_BYTES = 30 * 1024 * 1024;
const IMAGE_MAX_COUNT = 5;

const ERROR_TEXT = {
  ai_not_configured: "AI 服务尚未配置，暂时无法生成。",
  ai_timeout: "生成超时，请稍后重试。",
  ai_failed: "生成失败，请稍后重试。",
  ai_output_too_long: "生成内容超出长度限制，请缩短输入后重试。",
  resume_too_long: "简历内容过长，请控制在 20000 字以内。",
  image_too_large: "图片尺寸过大，请换一张更小的图片。",
  request_too_large: "本次上传的图片总大小超出限制，请减少图片数量或大小。",
  unsupported_media_type: "仅支持 PNG、JPEG、WebP 或 GIF 图片。",
  invalid_request: "提交内容无效，请检查后重试。",
  meeting_not_found: "找不到这场会议。",
  transcript_not_found: "找不到这份文字稿。",
  not_found: "找不到这个职位。",
  csrf_failed: "页面已过期，请刷新后重试。",
  csrf_missing: "页面已过期，请刷新后重试。",
  forbidden: "没有权限执行此操作。",
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

function shortUserId(userId) {
  const text = String(userId || "");
  return text.length > 16 ? text.slice(0, 12) + "…" : text;
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "未知大小";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function errorText(error) {
  if (!error) return "请求失败，请重试。";
  if (error.status === 401) return "登录已过期，请重新登录。";
  if (error.status === 403) return "没有权限执行此操作。";
  if (error.status === 404) return "找不到对应的职位或资源。";
  if (error.status === 413) return ERROR_TEXT.request_too_large;
  if (error.status === 415) return ERROR_TEXT.unsupported_media_type;
  if (typeof error.code === "string" && ERROR_TEXT[error.code]) return ERROR_TEXT[error.code];
  if (error.status >= 500) return "服务暂时不可用，请稍后重试。";
  return "请求失败，请重试。";
}

export function mountInterview(root, options = {}) {
  if (!root) throw new Error("interview root is required");
  const doc = root.ownerDocument;
  const win = doc.defaultView;
  const client = options.client || createCloudSession();
  const renderMarkdown = options.renderMarkdown || renderSafeMarkdown;
  const account = doc.getElementById("account");

  if (account) account.replaceChildren();

  const state = {
    session: null,
    loginRequired: false,
    loginUrl: "",
    positions: [],
    selectedId: "",
    busy: false,
    result: "",
    files: [],
  };

  let ui = null;

  function renderAccount() {
    if (!account) return;
    account.replaceChildren();
    if (state.session && state.session.user_id) {
      account.append(
        element(doc, "span", { className: "account-label" }, "账号 "),
        element(doc, "span", { className: "account-id" }, shortUserId(state.session.user_id)),
      );
      return;
    }
    if (state.loginRequired) {
      account.append(
        element(doc, "a", { href: state.loginUrl || client.loginUrl() }, "登录"),
      );
    }
  }

  function renderLoginGate() {
    const gate = element(doc, "section", {
      className: "status-panel login-gate",
      "data-testid": "login-gate",
    });
    gate.append(
      element(doc, "h1", {}, "职位与面试"),
      element(
        doc,
        "p",
        {},
        "登录后可以管理职位，并私密、临时地生成面试题。简历、图片和生成结果不会保存。",
      ),
      element(
        doc,
        "a",
        {
          className: "btn btn--primary",
          href: state.loginUrl || client.loginUrl(),
          "data-testid": "login-link",
        },
        "登录后使用",
      ),
    );
    root.replaceChildren(gate);
    gate.setAttribute("tabindex", "-1");
    gate.focus();
  }

  function renderLoading() {
    const panel = element(doc, "section", {
      className: "status-panel",
      "data-testid": "loading-panel",
      role: "status",
    });
    panel.append(element(doc, "p", {}, "正在载入职位…"));
    root.replaceChildren(panel);
  }

  function renderFatal(message) {
    const panel = element(doc, "section", {
      className: "status-panel status-panel--error",
      "data-testid": "page-error",
    });
    panel.append(
      element(doc, "h1", {}, "职位与面试"),
      element(doc, "p", { role: "alert" }, message),
      (() => {
        const retry = element(
          doc,
          "button",
          { className: "btn", type: "button", "data-testid": "page-retry" },
          "重试",
        );
        retry.addEventListener("click", () => boot());
        return retry;
      })(),
    );
    root.replaceChildren(panel);
  }

  function buildPage() {
    const page = element(doc, "div", { className: "interview" });
    const header = element(doc, "header", { className: "voiceprints-header" });
    const heading = element(doc, "div", { className: "voiceprints-heading" });
    heading.append(
      element(doc, "h1", {}, "职位与面试"),
      element(
        doc,
        "p",
        { className: "voiceprints-lede" },
        "职位会保存；简历、图片、OCR 文字和生成结果只在本次请求中临时使用，不写入数据库，刷新页面即消失。",
      ),
    );
    header.append(heading);
    page.append(header);

    const pageStatus = element(doc, "p", {
      className: "form-status page-status",
      "data-testid": "page-status",
      role: "status",
      "aria-live": "polite",
    });
    page.append(pageStatus);

    // ---- position picker ----
    const positionPanel = element(doc, "section", {
      className: "panel interview-panel",
      "data-testid": "position-panel",
      "aria-labelledby": "position-heading",
    });
    const positionBody = element(doc, "div", { className: "panel-body" });
    const positionHead = element(doc, "div", { className: "interview-section-head" });
    positionHead.append(
      element(doc, "h2", { id: "position-heading" }, "职位"),
      element(
        doc,
        "button",
        {
          className: "btn",
          type: "button",
          "data-testid": "position-create-open",
        },
        "新建职位",
      ),
    );
    positionBody.append(positionHead);
    positionBody.append(
      element(
        doc,
        "p",
        { className: "field-hint" },
        "选择本次面试的职位；没有合适的职位时先新建一个。",
      ),
    );

    const positionField = element(doc, "div", { className: "field" });
    positionField.append(element(doc, "label", { for: "position-select" }, "选择职位"));
    const positionSelect = element(doc, "select", {
      id: "position-select",
      "data-testid": "position-select",
    });
    positionField.append(positionSelect);
    positionBody.append(positionField);

    const selectedSummary = element(doc, "div", {
      className: "position-summary",
      "data-testid": "position-summary",
      hidden: true,
    });
    selectedSummary.append(
      element(doc, "p", { className: "position-summary-title", "data-testid": "selected-position-title" }),
      element(doc, "p", {
        className: "position-summary-meta",
        "data-testid": "selected-position-type",
      }),
      element(doc, "p", {
        className: "position-summary-requirements",
        "data-testid": "selected-position-requirements",
      }),
    );
    positionBody.append(selectedSummary);

    const positionError = element(doc, "p", {
      className: "form-error",
      "data-testid": "position-error",
      role: "alert",
      hidden: true,
    });
    positionBody.append(positionError);

    const positionEditor = element(doc, "section", {
      className: "position-editor",
      "data-testid": "position-editor",
      "aria-labelledby": "position-editor-title",
      hidden: true,
    });
    positionEditor.append(
      element(doc, "h3", { id: "position-editor-title" }, "新建职位"),
    );

    const typeField = element(doc, "div", { className: "field" });
    typeField.append(element(doc, "label", { for: "position-type" }, "职位类型"));
    const typeSelect = element(doc, "select", {
      id: "position-type",
      "data-testid": "position-type",
      required: true,
    });
    for (const type of POSITION_TYPES) {
      typeSelect.append(element(doc, "option", { value: type }, type));
    }
    typeField.append(typeSelect);
    positionEditor.append(typeField);

    const titleField = element(doc, "div", { className: "field" });
    titleField.append(element(doc, "label", { for: "position-title" }, "职位名称"));
    const titleInput = element(doc, "input", {
      id: "position-title",
      "data-testid": "position-title",
      type: "text",
      maxlength: "200",
      required: true,
      autocomplete: "off",
    });
    titleField.append(titleInput);
    positionEditor.append(titleField);

    const requirementsField = element(doc, "div", { className: "field" });
    requirementsField.append(
      element(doc, "label", { for: "position-requirements" }, "职位要求"),
      element(
        doc,
        "p",
        { className: "field-hint" },
        "写清技能、经验和职责；生成面试题时只使用这里的文字和可选简历。",
      ),
    );
    const requirementsInput = element(doc, "textarea", {
      id: "position-requirements",
      "data-testid": "position-requirements",
      maxlength: String(RESUME_MAX_CHARS),
      rows: "5",
      required: true,
    });
    requirementsField.append(requirementsInput);
    positionEditor.append(requirementsField);

    const positionEditorActions = element(doc, "div", { className: "form-actions" });
    const positionSave = element(
      doc,
      "button",
      { className: "btn btn--primary", type: "button", "data-testid": "position-save" },
      "保存职位",
    );
    const positionCancel = element(
      doc,
      "button",
      { className: "btn", type: "button", "data-testid": "position-cancel" },
      "取消",
    );
    positionEditorActions.append(positionSave, positionCancel);
    positionEditor.append(positionEditorActions);
    positionBody.append(positionEditor);
    positionPanel.append(positionBody);
    page.append(positionPanel);

    // ---- interview form ----
    const form = element(doc, "form", {
      className: "panel interview-panel",
      "data-testid": "interview-form",
      novalidate: true,
      "aria-labelledby": "interview-heading",
    });
    const formBody = element(doc, "div", { className: "panel-body" });
    formBody.append(
      element(doc, "h2", { id: "interview-heading" }, "候选人资料"),
      element(
        doc,
        "p",
        { className: "field-hint" },
        "可粘贴简历文字，也可上传最多 5 张简历截图。两者都没有时，只按职位要求出题。",
      ),
    );

    const resumeField = element(doc, "div", { className: "field" });
    const resumeLabel = element(doc, "label", { for: "resume-input" }, "简历文字（可选）");
    const resumeCounter = element(doc, "span", {
      className: "field-hint",
      "data-testid": "resume-count",
      "aria-live": "polite",
    });
    resumeField.append(resumeLabel, resumeCounter);
    const resumeInput = element(doc, "textarea", {
      id: "resume-input",
      "data-testid": "resume-input",
      maxlength: String(RESUME_MAX_CHARS),
      rows: "8",
      placeholder: "粘贴候选人简历；留空则只按职位要求出题。",
      "aria-describedby": "resume-hint",
    });
    resumeField.append(
      element(
        doc,
        "p",
        { className: "field-hint", id: "resume-hint" },
        "简历只在本次请求中使用，不会保存。",
      ),
      resumeInput,
    );
    formBody.append(resumeField);

    const imageField = element(doc, "div", { className: "field" });
    imageField.append(
      element(doc, "label", { for: "images-input" }, "简历图片（可选）"),
      element(
        doc,
        "p",
        { className: "field-hint" },
        "支持 PNG、JPEG、WebP、GIF；单张不超过 5 MB，最多 5 张，合计不超过 30 MB。图片仅在本次请求中识别，不保存。",
      ),
    );
    const imageInput = element(doc, "input", {
      id: "images-input",
      "data-testid": "images-input",
      type: "file",
      accept: "image/png,image/jpeg,image/webp,image/gif",
      multiple: true,
      className: "file-input",
    });
    const imageList = element(doc, "ul", {
      className: "image-list",
      "data-testid": "images-list",
      "aria-live": "polite",
    });
    const imageCount = element(doc, "p", {
      className: "field-hint",
      "data-testid": "images-count",
    });
    imageField.append(imageInput, imageList, imageCount);
    formBody.append(imageField);

    const formError = element(doc, "p", {
      className: "form-error",
      "data-testid": "form-error",
      role: "alert",
      hidden: true,
    });
    const formStatus = element(doc, "p", {
      className: "form-status",
      "data-testid": "form-status",
      role: "status",
      "aria-live": "polite",
    });
    const formActions = element(doc, "div", { className: "form-actions" });
    const submit = element(
      doc,
      "button",
      {
        className: "btn btn--primary",
        type: "submit",
        "data-testid": "generate-submit",
      },
      "生成面试题",
    );
    const clear = element(
      doc,
      "button",
      { className: "btn", type: "button", "data-testid": "input-clear" },
      "清空输入",
    );
    formActions.append(submit, clear);
    formBody.append(formError, formStatus, formActions);
    form.append(formBody);
    page.append(form);

    // ---- result ----
    const resultPanel = element(doc, "section", {
      className: "panel interview-panel result-panel",
      "data-testid": "result-panel",
      "aria-labelledby": "result-heading",
      tabindex: "-1",
      hidden: true,
    });
    const resultBody = element(doc, "div", { className: "panel-body" });
    const resultHead = element(doc, "div", { className: "interview-section-head" });
    resultHead.append(
      element(doc, "h2", { id: "result-heading" }, "面试题"),
      element(
        doc,
        "span",
        { className: "form-status", "data-testid": "result-status", role: "status" },
        "",
      ),
    );
    const resultMeta = element(doc, "p", {
      className: "field-hint",
      "data-testid": "result-meta",
    });
    const resultMarkdown = element(doc, "div", {
      className: "markdown",
      "data-testid": "result-markdown",
    });
    const resultActions = element(doc, "div", { className: "form-actions" });
    const copy = element(
      doc,
      "button",
      { className: "btn", type: "button", "data-testid": "result-copy" },
      "复制全文",
    );
    const clearResult = element(
      doc,
      "button",
      { className: "btn", type: "button", "data-testid": "result-clear" },
      "清空结果",
    );
    resultActions.append(copy, clearResult);
    resultBody.append(resultHead, resultMeta, resultMarkdown, resultActions);
    resultPanel.append(resultBody);
    page.append(resultPanel);

    root.replaceChildren(page);
    root.setAttribute("tabindex", "-1");

    return {
      pageStatus,
      positionSelect,
      positionError,
      positionEditor,
      typeSelect,
      titleInput,
      requirementsInput,
      positionSave,
      positionCancel,
      positionCreateOpen: positionHead.querySelector('[data-testid="position-create-open"]'),
      selectedSummary,
      selectedTitle: selectedSummary.querySelector('[data-testid="selected-position-title"]'),
      selectedType: selectedSummary.querySelector('[data-testid="selected-position-type"]'),
      selectedRequirements: selectedSummary.querySelector(
        '[data-testid="selected-position-requirements"]',
      ),
      form,
      resumeInput,
      resumeCounter,
      imageInput,
      imageList,
      imageCount,
      formError,
      formStatus,
      submit,
      clear,
      resultPanel,
      resultStatus: resultHead.querySelector('[data-testid="result-status"]'),
      resultMeta,
      resultMarkdown,
      copy,
      clearResult,
    };
  }

  function setError(node, message) {
    node.textContent = message || "";
    node.hidden = !message;
  }

  function setStatus(node, message) {
    node.textContent = message || "";
  }

  function selectedPosition() {
    return state.positions.find((position) => position.id === state.selectedId) || null;
  }

  function renderPositions() {
    if (!ui) return;
    ui.positionSelect.replaceChildren();
    if (state.positions.length === 0) {
      ui.positionSelect.append(
        element(doc, "option", { value: "" }, "还没有职位，请先新建"),
      );
      ui.positionSelect.disabled = true;
      ui.selectedSummary.hidden = true;
      setStatus(ui.pageStatus, "还没有职位，先新建一个职位再生成面试题。");
      return;
    }
    ui.positionSelect.disabled = false;
    if (!selectedPosition()) state.selectedId = state.positions[0].id;
    for (const position of state.positions) {
      ui.positionSelect.append(
        element(
          doc,
          "option",
          { value: position.id, selected: position.id === state.selectedId },
          position.title + "（" + position.type + "）",
        ),
      );
    }
    ui.positionSelect.value = state.selectedId;
    renderSelectedPosition();
    setStatus(ui.pageStatus, "");
  }

  function renderSelectedPosition() {
    const position = selectedPosition();
    if (!position) {
      ui.selectedSummary.hidden = true;
      return;
    }
    ui.selectedSummary.hidden = false;
    ui.selectedTitle.textContent = position.title;
    ui.selectedType.textContent = "职位类型：" + position.type;
    ui.selectedRequirements.textContent = position.requirements;
  }

  function showPositionEditor(show) {
    ui.positionEditor.hidden = !show;
    setError(ui.positionError, "");
    if (show) {
      ui.typeSelect.focus();
    } else {
      ui.positionCreateOpen.focus();
    }
  }

  function renderImages() {
    ui.imageList.replaceChildren();
    for (const file of state.files) {
      const item = element(doc, "li", { className: "image-item" });
      item.append(
        element(doc, "span", { className: "image-name" }, file.name || "未命名图片"),
        element(doc, "span", { className: "image-size" }, formatBytes(file.size)),
      );
      ui.imageList.append(item);
    }
    ui.imageCount.textContent =
      state.files.length === 0
        ? "未选择图片"
        : "已选择 " + state.files.length + " 张图片";
  }

  function validateFiles(files) {
    if (files.length > IMAGE_MAX_COUNT) {
      return "最多上传 5 张图片。";
    }
    let total = 0;
    for (const file of files) {
      if (file.size > IMAGE_MAX_BYTES) {
        return "单张图片不能超过 5 MB，请压缩后重试。";
      }
      total += file.size;
    }
    if (total > IMAGE_TOTAL_MAX_BYTES) {
      return "图片总大小不能超过 30 MB，请减少图片数量或大小。";
    }
    return "";
  }

  function resetResult() {
    state.result = "";
    ui.resultPanel.hidden = true;
    ui.resultMarkdown.replaceChildren();
    setStatus(ui.resultStatus, "");
    setStatus(ui.resultMeta, "");
  }

  function clearInput() {
    ui.resumeInput.value = "";
    ui.resumeCounter.textContent = "0 / " + RESUME_MAX_CHARS;
    ui.imageInput.value = "";
    state.files = [];
    renderImages();
    setError(ui.formError, "");
    setStatus(ui.formStatus, "");
    ui.resumeInput.focus();
  }

  async function copyResult() {
    if (!state.result) return;
    setStatus(ui.resultStatus, "");
    try {
      if (win.navigator.clipboard && typeof win.navigator.clipboard.writeText === "function") {
        await win.navigator.clipboard.writeText(state.result);
        setStatus(ui.resultStatus, "已复制全文");
        return;
      }
      const helper = element(doc, "textarea", {
        className: "visually-hidden",
        readonly: true,
        "aria-hidden": "true",
      });
      helper.value = state.result;
      doc.body.append(helper);
      helper.select();
      const copied = doc.execCommand && doc.execCommand("copy");
      helper.remove();
      setStatus(ui.resultStatus, copied ? "已复制全文" : "复制失败，请手动选择文本。");
    } catch {
      setStatus(ui.resultStatus, "复制失败，请手动选择文本。");
    }
  }

  async function createPosition() {
    if (state.busy) return;
    setError(ui.positionError, "");
    const type = ui.typeSelect.value;
    const title = ui.titleInput.value.trim();
    const requirements = ui.requirementsInput.value.trim();
    if (!title) {
      setError(ui.positionError, "请填写职位名称。");
      ui.titleInput.focus();
      return;
    }
    if (!requirements) {
      setError(ui.positionError, "请填写职位要求。");
      ui.requirementsInput.focus();
      return;
    }
    ui.positionSave.disabled = true;
    ui.positionSave.textContent = "保存中…";
    try {
      const created = await client.request(POSITIONS_PATH, {
        method: "POST",
        body: { type, title, requirements },
      });
      if (!created || typeof created.id !== "string") {
        throw new Error("invalid position response");
      }
      state.positions = [created, ...state.positions.filter((item) => item.id !== created.id)];
      state.selectedId = created.id;
      ui.typeSelect.value = POSITION_TYPES[0];
      ui.titleInput.value = "";
      ui.requirementsInput.value = "";
      renderPositions();
      showPositionEditor(false);
      setStatus(ui.pageStatus, "职位已保存。");
      ui.positionSelect.focus();
    } catch (error) {
      if (error && error.status === 401) {
        state.loginRequired = true;
        state.loginUrl = client.loginUrl();
        renderAccount();
        renderLoginGate();
        return;
      }
      setError(ui.positionError, errorText(error));
    } finally {
      ui.positionSave.disabled = false;
      ui.positionSave.textContent = "保存职位";
    }
  }

  async function submitInterview(event) {
    event.preventDefault();
    if (state.busy) return;
    setError(ui.formError, "");
    setStatus(ui.formStatus, "");
    const position = selectedPosition();
    if (!position) {
      setError(ui.formError, "请先选择或新建一个职位。");
      ui.positionSelect.focus();
      return;
    }
    const resume = ui.resumeInput.value;
    if (resume.length > RESUME_MAX_CHARS) {
      setError(ui.formError, "简历内容过长，请控制在 20000 字以内。");
      ui.resumeInput.focus();
      return;
    }
    const fileError = validateFiles(state.files);
    if (fileError) {
      setError(ui.formError, fileError);
      ui.imageInput.focus();
      return;
    }

    const body = new win.FormData();
    body.append("resume_text", resume);
    for (const file of state.files) {
      body.append("images", file, file.name || "resume-image");
    }

    state.busy = true;
    ui.submit.disabled = true;
    ui.submit.textContent = "生成中…";
    setStatus(ui.formStatus, "正在生成，请稍候…");
    resetResult();
    try {
      const response = await client.request(
        POSITIONS_PATH + "/" + encodeURIComponent(position.id) + "/interview",
        { method: "POST", body },
      );
      const markdown = response && typeof response.markdown === "string" ? response.markdown : "";
      if (!markdown.trim()) throw new Error("empty markdown");
      renderMarkdown(ui.resultMarkdown, markdown);
      state.result = markdown;
      ui.resultMeta.textContent =
        "根据「" + position.title + "」生成；结果只保留在当前页面，刷新即消失。";
      ui.resultPanel.hidden = false;
      setStatus(ui.formStatus, "面试题已生成。");
      setStatus(ui.resultStatus, "");
      ui.resultPanel.focus();
    } catch (error) {
      if (error && error.status === 401) {
        state.loginRequired = true;
        state.loginUrl = client.loginUrl();
        renderAccount();
        renderLoginGate();
        return;
      }
      if (error && error.name === "RangeError") {
        setError(ui.formError, ERROR_TEXT.ai_output_too_long);
      } else {
        setError(ui.formError, errorText(error));
      }
    } finally {
      state.busy = false;
      ui.submit.disabled = false;
      ui.submit.textContent = "生成面试题";
      if (!ui.resultPanel.hidden) setStatus(ui.formStatus, "面试题已生成。");
    }
  }

  function bind() {
    ui.positionCreateOpen.addEventListener("click", () => {
      ui.positionEditor.hidden ? showPositionEditor(true) : showPositionEditor(false);
    });
    ui.positionCancel.addEventListener("click", () => showPositionEditor(false));
    ui.positionSave.addEventListener("click", createPosition);
    ui.positionSelect.addEventListener("change", () => {
      state.selectedId = ui.positionSelect.value;
      renderSelectedPosition();
      setError(ui.formError, "");
    });
    ui.resumeInput.addEventListener("input", () => {
      ui.resumeCounter.textContent = ui.resumeInput.value.length + " / " + RESUME_MAX_CHARS;
    });
    ui.imageInput.addEventListener("change", () => {
      const files = Array.from(ui.imageInput.files || []);
      const problem = validateFiles(files);
      if (problem) {
        state.files = [];
        ui.imageInput.value = "";
        renderImages();
        setError(ui.formError, problem);
        return;
      }
      state.files = files;
      setError(ui.formError, "");
      renderImages();
    });
    ui.form.addEventListener("submit", submitInterview);
    ui.clear.addEventListener("click", clearInput);
    ui.copy.addEventListener("click", copyResult);
    ui.clearResult.addEventListener("click", () => {
      resetResult();
      setStatus(ui.formStatus, "");
      ui.resumeInput.focus();
    });
  }

  async function loadPositions() {
    try {
      const positions = await client.request(POSITIONS_PATH);
      state.positions = Array.isArray(positions)
        ? positions.filter(
            (position) =>
              position &&
              typeof position.id === "string" &&
              typeof position.type === "string" &&
              typeof position.title === "string" &&
              typeof position.requirements === "string",
          )
        : [];
      renderPositions();
    } catch (error) {
      if (error && error.status === 401) {
        state.loginRequired = true;
        state.loginUrl = client.loginUrl();
        renderAccount();
        renderLoginGate();
        return;
      }
      setStatus(ui.pageStatus, "");
      setError(ui.positionError, "无法载入职位列表，请重试。");
    }
  }

  async function boot() {
    renderLoading();
    try {
      const session = await client.session();
      state.session = session;
      state.loginRequired = false;
      state.loginUrl = client.loginUrl();
      renderAccount();
      if (!ui) {
        ui = buildPage();
        bind();
      }
      ui.resumeCounter.textContent = "0 / " + RESUME_MAX_CHARS;
      renderImages();
      await loadPositions();
    } catch (error) {
      if (error && error.status === 401) {
        state.loginRequired = true;
        state.loginUrl = client.loginUrl();
        renderAccount();
        renderLoginGate();
        return;
      }
      renderFatal("无法连接服务，请稍后重试。");
    }
  }

  boot();
  return {
    refresh: boot,
    clear: () => {
      if (ui) clearInput();
    },
  };
}
