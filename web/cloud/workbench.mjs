// 视觉草案，未获 G4 批准：本模块只落实可达性与真实状态，未进行视觉签收。
//
// Cloud workbench: renders only server-provided meetings, transcript versions,
// annotations and the capture host's recording snapshot. It never invents
// transcript text, speakers, processing steps or "saved" claims, and every
// dynamic string reaches the DOM through textContent rather than innerHTML.
//
// Node 22 / evergreen browser compatible: DOM, fetch, AbortController only.

import { createCloudSession } from "./session.mjs";
import { LOGIN_PATH, renderLoginGate } from "./gate.mjs";

const SESSION_PATH = "/api/cloud/auth/session";
const MEETINGS_PATH = "/api/cloud/meetings?limit=20";

const KIND_LABELS = Object.freeze({ note: "笔记", highlight: "重点", comment: "注释" });
const STATUS_LABELS = Object.freeze({
  complete: "最终稿",
  completed_with_warnings: "有警告",
  basic_failed: "基础稿",
});
const CAPTURE_LABELS = Object.freeze({
  idle: "未录音",
  requesting_permissions: "等待权限",
  ready: "音源就绪",
  recording: "正在录音",
  stopping: "正在停止",
  stopped: "已停止",
  interrupted: "采集中断",
  storage_failed: "本机暂存失败",
});
const PROCESSING_LABELS = Object.freeze({
  not_submitted: "未提交",
  queued: "排队中",
  running: "处理中",
  completed: "已生成",
  failed: "失败",
  cancelled: "已取消",
  unknown: "状态未知",
});
const MODE_LABELS = Object.freeze({ mic: "仅麦克风", dual: "双音源" });
const SOURCE_LABELS = Object.freeze({ mic: "麦克风", system: "电脑声音" });

export function formatOffset(ms) {
  const value = Number.isFinite(ms) ? Math.max(0, Math.trunc(ms)) : 0;
  const totalSeconds = Math.floor(value / 1000);
  const millis = value % 1000;
  const seconds = totalSeconds % 60;
  const minutes = Math.floor(totalSeconds / 60) % 60;
  const hours = Math.floor(totalSeconds / 3600);
  const pad = (number, width) => String(number).padStart(width || 2, "0");
  if (hours >= 1) {
    return hours + ":" + pad(minutes) + ":" + pad(seconds) + "." + pad(millis, 3);
  }
  return pad(minutes) + ":" + pad(seconds) + "." + pad(millis, 3);
}

// ---------------------------------------------------------------------------
// Recording snapshot normalization
// ---------------------------------------------------------------------------

function asString(value) {
  return typeof value === "string" && value.length > 0 ? value : null;
}

export function normalizeRecording(recording) {
  if (!recording || typeof recording !== "object") {
    return {
      present: false,
      active: false,
      activeMeetingId: null,
      captureState: "idle",
      contiguousMs: null,
      pendingBytes: 0,
      uploadError: null,
      uploadErrorCode: null,
      uploadErrorStatus: null,
      warning: null,
      tracks: [],
      processing: null,
      permissionError: null,
      shareError: null,
    };
  }
  const capture = recording.capture && typeof recording.capture === "object" ? recording.capture : {};
  const upload = recording.upload && typeof recording.upload === "object" ? recording.upload : {};
  const lastAck = upload.lastAck && typeof upload.lastAck === "object" ? upload.lastAck : null;
  const active =
    recording.captureRunning === true ||
    capture.state === "recording" ||
    capture.state === "stopping";
  const activeMeetingId =
    asString(recording.activeMeetingId) ||
    asString(recording.meetingId) ||
    asString(upload.meetingId);
  const tracks = Array.isArray(recording.tracks)
    ? recording.tracks.filter(
        (track) =>
          track &&
          typeof track === "object" &&
          (track.source === "mic" || track.source === "system"),
      )
    : [];
  const contiguous = Number(lastAck && lastAck.contiguousMs);
  const pending = Number(upload.pendingBytes);
  return {
    present: true,
    active,
    activeMeetingId,
    captureState: asString(capture.state) || (active ? "recording" : "idle"),
    contiguousMs: Number.isFinite(contiguous) && contiguous >= 0 ? contiguous : null,
    pendingBytes: Number.isFinite(pending) && pending > 0 ? pending : 0,
    uploadError: upload.error || null,
    uploadErrorCode: asString(upload.errorCode),
    uploadErrorStatus: Number.isInteger(upload.errorStatus) ? upload.errorStatus : null,
    warning: upload.warning || (Array.isArray(recording.warnings) ? recording.warnings[0] : null) || null,
    tracks,
    processing:
      recording.processing && typeof recording.processing === "object"
        ? recording.processing
        : null,
    permissionError: capture.error || null,
    shareError: capture.shareError || null,
  };
}

function sourceState(recording, source) {
  const track = recording.tracks.find((candidate) => candidate.source === source) || null;
  if (source === "mic" && recording.permissionError && !track) return "权限未授予";
  if (source === "system" && recording.shareError && !track) return "权限未授予";
  if (!track) return "未采集";
  const state = String(track.state || track.readyState || "").toLowerCase();
  if (state === "recording" || state === "live" || state === "active" || state === "capturing") {
    return "录音中";
  }
  if (state === "interrupted" || state === "error" || state === "failed") return "中断";
  if (state === "permission_denied" || state === "denied") return "权限未授予";
  if (state === "stopped" || state === "ended" || state === "complete" || state === "completed") {
    return "已停止";
  }
  // track.state is ours; track.readyState is the MediaStreamTrack verb. An
  // unrecognised state must not be upgraded to "recording" just because the
  // session says it is active.
  return "状态未知";
}

function describeProblem(problem) {
  if (problem === null || problem === undefined) return "";
  if (typeof problem === "string") return problem;
  if (typeof problem === "object") {
    if (typeof problem.message === "string" && problem.message) return problem.message;
    if (typeof problem.code === "string" && problem.code) return problem.code;
  }
  return "未知错误";
}

// ---------------------------------------------------------------------------
// DOM helpers: createElement + textContent only.
// ---------------------------------------------------------------------------

function el(doc, tag, options) {
  const settings = options || {};
  const node = doc.createElement(tag);
  if (settings.className) node.className = settings.className;
  if (settings.text !== undefined && settings.text !== null) node.textContent = String(settings.text);
  if (settings.attrs) {
    for (const name of Object.keys(settings.attrs)) {
      const value = settings.attrs[name];
      if (value === null || value === undefined || value === false) continue;
      node.setAttribute(name, String(value));
    }
  }
  if (settings.dataset) {
    for (const name of Object.keys(settings.dataset)) {
      const value = settings.dataset[name];
      if (value === null || value === undefined) continue;
      node.dataset[name] = String(value);
    }
  }
  if (settings.hidden) node.hidden = true;
  if (settings.disabled) node.disabled = true;
  if (settings.children) node.append.apply(node, settings.children);
  return node;
}

function iconSpan(doc, source) {
  return el(doc, "span", {
    className: "source-icon source-icon--" + source,
    attrs: { "aria-hidden": "true" },
  });
}

function sortedAnnotations(annotations) {
  return (Array.isArray(annotations) ? annotations : [])
    .filter((item) => item && typeof item === "object")
    .slice()
    .sort((left, right) => {
      const a = Number(left.offset_ms);
      const b = Number(right.offset_ms);
      const offset = (Number.isFinite(a) ? a : 0) - (Number.isFinite(b) ? b : 0);
      if (offset !== 0) return offset;
      const created = String(left.created_at || "").localeCompare(String(right.created_at || ""));
      if (created !== 0) return created;
      return String(left.id || "").localeCompare(String(right.id || ""));
    });
}

function speakerFor(transcript, speakerId) {
  if (!speakerId || !transcript || !Array.isArray(transcript.speakers)) return null;
  return transcript.speakers.find((speaker) => speaker && speaker.id === speakerId) || null;
}

function meetingModeLabel(meeting) {
  if (!meeting) return "";
  return MODE_LABELS[meeting.mode] || String(meeting.mode || "");
}

function transcriptStatusLabel(transcript) {
  if (!transcript) return "";
  return STATUS_LABELS[transcript.status] || String(transcript.status || "");
}

function historyTitle(state, meetingId) {
  const item = (state.history || []).find((meeting) => meeting && meeting.id === meetingId);
  return item ? item.title : null;
}

// ---------------------------------------------------------------------------
// Recording strip
// ---------------------------------------------------------------------------

function buildRecordingStrip(doc, state) {
  const strip = el(doc, "section", {
    className: "recording-strip",
    attrs: { "data-testid": "recording-strip", "aria-labelledby": "recording-strip-heading" },
  });
  strip.append(
    el(doc, "h2", {
      className: "visually-hidden",
      text: "录音与上传状态",
      attrs: { id: "recording-strip-heading" },
    }),
    el(doc, "p", {
      className: "strip-state",
      attrs: { "data-testid": "capture-state" },
    }),
    el(doc, "p", {
      className: "strip-metrics",
      children: [
        el(doc, "span", {
          className: "metric",
          attrs: { "data-testid": "saved-position" },
        }),
        el(doc, "span", {
          className: "metric metric--pending",
          attrs: { "data-testid": "pending-bytes" },
        }),
      ],
    }),
    el(doc, "ul", {
      className: "source-list",
      attrs: { "data-testid": "sources", "aria-label": "采集音源" },
    }),
    el(doc, "p", {
      className: "strip-processing",
      attrs: { "data-testid": "processing-state" },
    }),
    el(doc, "p", {
      className: "strip-alert",
      attrs: { role: "alert", "data-testid": "upload-error" },
      hidden: true,
    }),
    el(doc, "p", {
      className: "strip-warning",
      attrs: { "data-testid": "upload-warning" },
      hidden: true,
    }),
  );
  fillRecordingStrip(strip, doc, state);
  return strip;
}

function fillRecordingStrip(strip, doc, state) {
  const recording = normalizeRecording(state.recording);
  const viewedId = state.meeting ? state.meeting.id : null;

  const stateEl = strip.querySelector('[data-testid="capture-state"]');
  if (stateEl) {
    stateEl.replaceChildren();
    if (recording.active) {
      stateEl.append(
        el(doc, "span", { className: "recording-dot", attrs: { "aria-hidden": "true" } }),
        el(doc, "span", { className: "recording-label", text: "正在录音" }),
      );
      const title = recording.activeMeetingId ? historyTitle(state, recording.activeMeetingId) : null;
      const where = title || recording.activeMeetingId;
      if (where) {
        stateEl.append(
          el(doc, "span", {
            className: "strip-meeting",
            text: " · 录音属于「" + where + "」",
          }),
        );
      }
    } else {
      const label = CAPTURE_LABELS[recording.captureState] || "采集状态未知";
      stateEl.append(el(doc, "span", { text: label }));
    }
    if (
      viewedId &&
      recording.active &&
      recording.activeMeetingId &&
      recording.activeMeetingId !== viewedId
    ) {
      stateEl.append(
        el(doc, "span", {
          className: "strip-context",
          text: "；正在查看另一场记录",
        }),
      );
    }
  }

  const savedEl = strip.querySelector('[data-testid="saved-position"]');
  if (savedEl) {
    savedEl.textContent =
      recording.contiguousMs !== null && recording.contiguousMs > 0
        ? "已连续保存到 " + formatOffset(recording.contiguousMs)
        : "等待首个分片确认";
  }

  const pendingEl = strip.querySelector('[data-testid="pending-bytes"]');
  if (pendingEl) pendingEl.textContent = "待上传 " + recording.pendingBytes + " 字节";

  const sourcesEl = strip.querySelector('[data-testid="sources"]');
  if (sourcesEl) {
    sourcesEl.replaceChildren();
    for (const source of ["mic", "system"]) {
      const label = sourceState(recording, source);
      sourcesEl.append(
        el(doc, "li", {
          className: "source-card",
          attrs: {
            "data-testid": "source-" + source,
            "data-source-state": label,
          },
          children: [
            iconSpan(doc, source),
            el(doc, "span", { className: "source-name", text: SOURCE_LABELS[source] }),
            el(doc, "span", { className: "source-state", text: label }),
          ],
        }),
      );
    }
  }

  const processingEl = strip.querySelector('[data-testid="processing-state"]');
  if (processingEl) {
    if (recording.processing) {
      const label = PROCESSING_LABELS[recording.processing.state] || "状态未知";
      const code = recording.processing.errorCode ? "（" + recording.processing.errorCode + "）" : "";
      processingEl.textContent = "处理状态：" + label + code;
      processingEl.hidden = false;
    } else {
      processingEl.textContent = "";
      processingEl.hidden = true;
    }
  }

  const errorEl = strip.querySelector('[data-testid="upload-error"]');
  if (errorEl) {
    const text =
      recording.uploadErrorCode === "rate_limited"
        ? "请求过于频繁，请稍后重试（rate_limited）"
        : describeProblem(recording.uploadError);
    errorEl.textContent = text ? "上传错误：" + text : "";
    errorEl.hidden = text === "";
  }

  const warningEl = strip.querySelector('[data-testid="upload-warning"]');
  if (warningEl) {
    const text = describeProblem(recording.warning);
    warningEl.textContent = text ? "上传提醒：" + text : "";
    warningEl.hidden = text === "";
  }

  const stopButton = strip.querySelector('[data-testid="stop-recording"]');
  if (recording.active && !stopButton) {
    strip.append(
      el(doc, "button", {
        className: "btn btn--danger strip-stop",
        text: "停止录音",
        attrs: { type: "button", "data-testid": "stop-recording" },
      }),
    );
  } else if (!recording.active && stopButton) {
    stopButton.remove();
  }
}

// ---------------------------------------------------------------------------
// Workbench sections
// ---------------------------------------------------------------------------

function buildLoginGate(doc, state) {
  // 登录门本身是共用模块（web/cloud/gate.mjs），含邮箱表单与 next 跳回。
  const loginPath = typeof state.loginUrl === "string" && state.loginUrl ? state.loginUrl : LOGIN_PATH;
  return renderLoginGate(doc, { loginPath });
}

function errorText(error) {
  if (!error) return "未知错误。";
  if (error.status === 401) return "登录已过期，请重新登录。";
  if (error.status === 404) return "该会议不存在或不可访问。";
  const code = error.code ? "（" + error.code + "）" : "";
  return "加载失败" + code + "，请稍后重试。";
}

function buildStatusPanel(doc, state) {
  if (state.loading) {
    return el(doc, "section", {
      className: "status-panel",
      attrs: { "data-testid": "loading-state", role: "status", "aria-live": "polite" },
      children: [el(doc, "p", { text: "正在加载会议记录…" })],
    });
  }
  if (state.error) {
    const panel = el(doc, "section", {
      className: "status-panel status-panel--error",
      attrs: { "data-testid": "error-state", role: "alert" },
    });
    panel.append(
      el(doc, "h2", { text: "无法加载工作台" }),
      el(doc, "p", { text: errorText(state.error) }),
      el(doc, "button", {
        className: "btn",
        text: "重试",
        attrs: { type: "button", "data-testid": "retry-load" },
      }),
    );
    return panel;
  }
  const panel = el(doc, "section", {
    className: "status-panel",
    attrs: { "data-testid": "empty-state", "aria-labelledby": "empty-heading" },
  });
  panel.append(
    el(doc, "h2", { text: "尚无会议记录", attrs: { id: "empty-heading" } }),
    el(doc, "p", {
      text: "开始录音后，会议会出现在这里。这里不会显示示例或推测内容。",
    }),
  );
  return panel;
}

function buildHistoryPanel(doc, state) {
  const details = el(doc, "details", {
    className: "panel history-panel",
    attrs: { id: "history-panel", "data-testid": "history-panel" },
  });
  details.open = true;
  details.append(el(doc, "summary", { text: "历史记录" }));
  const body = el(doc, "div", { className: "panel-body" });
  const items = Array.isArray(state.history) ? state.history : [];
  if (items.length === 0) {
    body.append(el(doc, "p", { className: "muted", text: "尚无历史记录。" }));
  } else {
    const list = el(doc, "ul", {
      className: "history-list",
      attrs: { "data-testid": "history-list" },
    });
    for (const meeting of items) {
      if (!meeting || typeof meeting !== "object") continue;
      const viewed = state.meeting && meeting.id === state.meeting.id;
      const button = el(doc, "button", {
        className: "history-item",
        attrs: {
          type: "button",
          "data-testid": "history-item",
          "data-meeting-id": meeting.id,
          "aria-current": viewed ? "true" : null,
        },
        children: [
          el(doc, "span", { className: "history-title", text: meeting.title || meeting.id }),
          el(doc, "span", {
            className: "history-meta",
            children: [
              el(doc, "span", { text: meetingModeLabel(meeting) }),
              el(doc, "span", {
                className: viewed ? "badge badge--current" : "badge",
                text: viewed ? "当前记录" : "历史",
              }),
            ],
          }),
        ],
      });
      list.append(el(doc, "li", { children: [button] }));
    }
    body.append(list);
  }
  details.append(body);
  return details;
}

function playbackBlocked(state) {
  const recording = normalizeRecording(state.recording);
  const viewedId = state.meeting ? state.meeting.id : null;
  if (!recording.active || !viewedId) return false;
  if (!recording.activeMeetingId) return true;
  return recording.activeMeetingId === viewedId;
}

function playbackCaution(state) {
  const recording = normalizeRecording(state.recording);
  const viewedId = state.meeting ? state.meeting.id : null;
  return Boolean(
    recording.active &&
      viewedId &&
      recording.activeMeetingId &&
      recording.activeMeetingId !== viewedId,
  );
}

function buildTranscriptPanel(doc, state) {
  const meeting = state.meeting;
  const transcript = state.transcript;
  const blocked = playbackBlocked(state);
  const panel = el(doc, "section", {
    className: "panel transcript-panel",
    attrs: {
      id: "transcript-section",
      "data-testid": "transcript-panel",
      "aria-labelledby": "transcript-heading",
    },
  });
  const header = el(doc, "header", { className: "panel-header" });
  header.append(
    el(doc, "h2", { text: meeting ? meeting.title : "文字稿", attrs: { id: "transcript-heading" } }),
  );
  const meta = el(doc, "p", { className: "transcript-meta" });
  if (meeting) meta.append(el(doc, "span", { className: "tag", text: meetingModeLabel(meeting) }));
  if (transcript) {
    meta.append(
      el(doc, "span", { text: "版本 " }),
      el(doc, "code", {
        className: "mono",
        text: transcript.id,
        attrs: { "data-testid": "transcript-version" },
      }),
      el(doc, "span", {
        className: "tag status-tag status-tag--" + (transcript.status || "unknown"),
        text: transcriptStatusLabel(transcript),
        attrs: { "data-testid": "transcript-status" },
      }),
    );
  } else {
    meta.append(
      el(doc, "span", {
        className: "tag",
        text: "尚无文字稿",
        attrs: { "data-testid": "transcript-version" },
      }),
    );
  }
  header.append(meta);
  panel.append(header);

  const audioBar = el(doc, "div", { className: "audio-bar" });
  const audio = el(doc, "audio", {
    className: "audio-player",
    attrs: {
      "data-testid": "audio-player",
      preload: blocked ? "none" : "metadata",
      "aria-label": "会议回放",
    },
  });
  audio.controls = !blocked;
  if (!blocked && typeof state.audioUrl === "string" && state.audioUrl) {
    audio.setAttribute("src", state.audioUrl);
  } else {
    audio.setAttribute("aria-disabled", "true");
  }
  audioBar.append(
    audio,
    el(doc, "span", {
      className: "mono audio-position",
      text: "00:00.000",
      attrs: { "data-testid": "audio-position", "aria-hidden": "true" },
    }),
  );
  panel.append(audioBar);

  if (blocked) {
    panel.append(
      el(doc, "p", {
        className: "notice notice--blocking",
        text: normalizeRecording(state.recording).activeMeetingId
          ? "录音中不可回放当前记录"
          : "无法确认当前录音属于哪场会议，已停用回放",
        attrs: { "data-testid": "playback-blocked", role: "note" },
      }),
    );
  } else if (playbackCaution(state)) {
    panel.append(
      el(doc, "p", {
        className: "notice",
        text: "另一场会议正在录音；此处回放可能被系统音源再次录入。",
        attrs: { "data-testid": "playback-caution", role: "note" },
      }),
    );
  }

  if (!transcript) {
    panel.append(
      el(doc, "p", {
        className: "empty-inline",
        text: "本场会议还没有文字稿。录音与上传状态见上方录音条。",
        attrs: { "data-testid": "transcript-empty" },
      }),
    );
    return panel;
  }

  const scroller = el(doc, "div", {
    className: "transcript-scroll",
    attrs: {
      "data-testid": "transcript-scroll",
      tabindex: "0",
      role: "region",
      "aria-label": "文字稿内容",
    },
  });
  const segments = Array.isArray(transcript.segments) ? transcript.segments : [];
  if (segments.length === 0) {
    scroller.append(
      el(doc, "p", {
        className: "empty-inline",
        text: "未识别到语音",
        attrs: { "data-testid": "transcript-empty" },
      }),
    );
  } else {
    const list = el(doc, "ol", {
      className: "transcript-list",
      attrs: { "data-testid": "transcript-list" },
    });
    for (const segment of segments) {
      if (!segment || typeof segment !== "object") continue;
      const speaker = speakerFor(transcript, segment.speaker_id);
      const row = el(doc, "li", {
        className: "segment",
        attrs: { "data-testid": "transcript-segment" },
      });
      const seek = el(doc, "button", {
        className: "time-button mono",
        text: formatOffset(segment.start_ms),
        attrs: {
          type: "button",
          "data-testid": "segment-seek",
          "data-start-ms": segment.start_ms,
          "aria-label": "定位到 " + formatOffset(segment.start_ms),
          disabled: blocked,
        },
      });
      const body = el(doc, "div", { className: "segment-body" });
      const heading = el(doc, "p", { className: "segment-heading" });
      if (speaker) {
        heading.append(el(doc, "span", { className: "speaker", text: speaker.label }));
        if (speaker.suspect) {
          heading.append(el(doc, "span", { className: "badge badge--suspect", text: "疑似" }));
        }
      }
      heading.append(el(doc, "span", { className: "segment-role", text: "发言" }));
      body.append(
        heading,
        el(doc, "p", {
          className: "segment-text",
          text: typeof segment.text === "string" ? segment.text : "",
        }),
      );
      row.append(seek, body);
      list.append(row);
    }
    scroller.append(list);
  }
  panel.append(
    scroller,
    el(doc, "button", {
      className: "btn back-to-latest",
      text: "回到最新",
      attrs: { type: "button", "data-testid": "back-to-latest" },
      hidden: true,
    }),
  );
  return panel;
}

function annotationItem(doc, annotation, state) {
  const blocked = playbackBlocked(state);
  const offset = Number(annotation.offset_ms);
  const offsetText = Number.isFinite(offset) ? formatOffset(offset) : "—";
  const item = el(doc, "li", {
    className: "annotation-item",
    attrs: {
      "data-testid": "annotation-item",
      "data-annotation-id": annotation.id,
      "data-offset-ms": annotation.offset_ms,
    },
  });
  const header = el(doc, "div", { className: "annotation-heading" });
  header.append(
    el(doc, "button", {
      className: "time-button mono",
      text: offsetText,
      attrs: {
        type: "button",
        "data-testid": "annotation-seek",
        "data-offset-ms": annotation.offset_ms,
        "aria-label": "定位到 " + offsetText,
        disabled: blocked,
      },
    }),
    el(doc, "span", {
      className: "badge badge--kind",
      text: KIND_LABELS[annotation.kind] || String(annotation.kind || "标注"),
    }),
  );
  item.append(
    header,
    el(doc, "p", {
      className: "annotation-text",
      text: typeof annotation.text === "string" ? annotation.text : "",
    }),
  );
  return item;
}

function buildAnnotationsPanel(doc, state) {
  const details = el(doc, "details", {
    className: "panel annotations-panel",
    attrs: {
      id: "annotations-section",
      "data-testid": "annotations-panel",
      "aria-labelledby": "annotations-heading",
    },
  });
  details.open = true;
  details.append(el(doc, "summary", { text: "笔记 / 重点 / 注释" }));
  const body = el(doc, "div", { className: "panel-body" });
  body.append(
    el(doc, "h2", {
      className: "visually-hidden",
      text: "笔记、重点与注释",
      attrs: { id: "annotations-heading" },
    }),
  );
  const versionLabel = state.transcript ? state.transcript.id : "尚无文字稿";
  body.append(
    el(doc, "p", {
      className: "annotation-version",
      attrs: { "data-testid": "annotations-version" },
      children: [
        el(doc, "span", { text: "按会议时间保存，不随转写版本改变；当前查看版本 " }),
        el(doc, "code", { className: "mono", text: versionLabel }),
      ],
    }),
  );

  const annotations = sortedAnnotations(state.annotations);
  if (annotations.length === 0) {
    body.append(
      el(doc, "p", {
        className: "empty-inline",
        text: "尚无标注。选择会议时间，写下第一条笔记。",
        attrs: { "data-testid": "annotations-empty" },
      }),
    );
  } else {
    const list = el(doc, "ol", {
      className: "annotation-list",
      attrs: { "data-testid": "annotation-list" },
    });
    for (const annotation of annotations) {
      list.append(annotationItem(doc, annotation, state));
    }
    body.append(list);
  }

  const formState = state.form || {};
  const form = el(doc, "form", {
    className: "annotation-form",
    attrs: { "data-testid": "annotation-form", novalidate: "novalidate" },
  });
  const kindId = "annotation-kind";
  const offsetId = "annotation-offset";
  const textId = "annotation-text";
  const kindField = el(doc, "div", { className: "field" });
  kindField.append(el(doc, "label", { text: "类型", attrs: { for: kindId } }));
  const select = el(doc, "select", {
    attrs: { id: kindId, name: "kind", "data-testid": "annotation-kind" },
  });
  for (const value of Object.keys(KIND_LABELS)) {
    const option = el(doc, "option", { text: KIND_LABELS[value], attrs: { value } });
    if ((formState.kind || "note") === value) option.selected = true;
    select.append(option);
  }
  kindField.append(select);

  const offsetField = el(doc, "div", { className: "field" });
  offsetField.append(
    el(doc, "label", { text: "会议时间（毫秒）", attrs: { for: offsetId } }),
    el(doc, "input", {
      attrs: {
        id: offsetId,
        name: "offset_ms",
        type: "number",
        min: "0",
        step: "1",
        inputmode: "numeric",
        "data-testid": "annotation-offset",
      },
    }),
    el(doc, "p", {
      className: "field-hint",
      text: "随播放位置更新；手动修改后不再自动覆盖。",
    }),
  );

  const textField = el(doc, "div", { className: "field" });
  textField.append(
    el(doc, "label", { text: "内容（1–2000 字）", attrs: { for: textId } }),
    el(doc, "textarea", {
      attrs: {
        id: textId,
        name: "text",
        rows: "3",
        maxlength: "2000",
        "data-testid": "annotation-text",
      },
    }),
  );

  const actions = el(doc, "div", { className: "form-actions" });
  actions.append(
    el(doc, "button", {
      className: "btn btn--primary",
      text: "保存标注",
      attrs: { type: "submit", "data-testid": "annotation-save" },
    }),
  );

  form.append(
    kindField,
    offsetField,
    textField,
    actions,
    el(doc, "p", {
      className: "form-status",
      attrs: { role: "status", "data-testid": "annotation-status" },
      text: formState.status || "",
    }),
    el(doc, "p", {
      className: "form-error",
      attrs: { role: "alert", "data-testid": "annotation-error" },
      text: formState.error || "",
      hidden: !formState.error,
    }),
    el(doc, "p", {
      className: "field-hint",
      text: "标注保存到会议时间轴，不支持编辑或删除；文字稿不支持手工修改。",
    }),
  );
  body.append(form);
  details.append(body);
  return details;
}

function buildBanner(doc, state) {
  const recording = normalizeRecording(state.recording);
  const viewedId = state.meeting ? state.meeting.id : null;
  if (!recording.active || !viewedId) return null;
  if (recording.activeMeetingId === viewedId) return null;
  const title = recording.activeMeetingId ? historyTitle(state, recording.activeMeetingId) : null;
  const text = recording.activeMeetingId
    ? "当前正在录音的会议是「" + (title || recording.activeMeetingId) + "」，你查看的是另一场记录的文字稿，不是实时稿。"
    : "当前有录音在进行，但无法确认它属于哪场会议；这里显示的不是实时稿。";
  return el(doc, "p", {
    className: "banner",
    text,
    attrs: { role: "note", "data-testid": "history-banner" },
  });
}

// ---------------------------------------------------------------------------
// Public renderer
// ---------------------------------------------------------------------------

export function renderWorkbench(root, state) {
  if (!root) return;
  const settings = state || {};
  const doc = root.ownerDocument || globalThis.document;
  root.replaceChildren();
  if (settings.loginRequired) {
    root.append(buildLoginGate(doc, settings));
    return;
  }
  const workbench = el(doc, "div", {
    className: "workbench",
    attrs: { "data-testid": "workbench" },
  });
  workbench.append(el(doc, "h1", { className: "visually-hidden", text: "当前会议工作台" }));
  // 录音控件与实时文本是页面长期持有的节点（录音中不能被重渲染销毁），
  // 这里只负责把它们放进三栏：左栏录音状态与控件，中栏文字稿，右栏注释。
  const recorderPanel = settings.recorderPanel || null;
  const livePanel = settings.livePanel || null;
  const grid = el(doc, "div", { className: "workbench-grid" });
  const rail = el(doc, "div", {
    className: "workbench-rail",
    attrs: { "data-testid": "workbench-rail" },
  });
  rail.append(buildRecordingStrip(doc, settings));
  if (recorderPanel) rail.append(recorderPanel);
  const main = el(doc, "div", {
    className: "workbench-main",
    attrs: { "data-testid": "workbench-main" },
  });
  if (!settings.meeting) {
    main.append(buildStatusPanel(doc, settings));
    grid.append(rail, main);
    workbench.append(grid);
    root.append(workbench);
    return;
  }
  rail.append(buildHistoryPanel(doc, settings));
  const banner = buildBanner(doc, settings);
  if (banner) workbench.append(banner);
  const transcript = buildTranscriptPanel(doc, settings);
  if (livePanel) transcript.prepend(livePanel);
  main.append(transcript);
  grid.append(rail, main, buildAnnotationsPanel(doc, settings));
  workbench.append(grid);
  root.append(workbench);
}

// ---------------------------------------------------------------------------
// Mounting / behaviour
// ---------------------------------------------------------------------------

export function mountWorkbench(root, options) {
  if (!root) throw new TypeError("root element is required");
  const settings = options || {};
  const doc = root.ownerDocument || globalThis.document;
  const win = doc.defaultView || globalThis;
  const client = settings.client || createCloudSession();
  const accountEl = settings.account || doc.getElementById("account");

  let destroyed = false;
  let loadGeneration = 0;
  let abortCurrent = null;
  let lastRenderKey = null;
  let pendingSeekMs = null;
  let resumeAfterMetadata = false;
  let pendingWhileDialog = false;

  const state = {
    loading: true,
    error: null,
    loginRequired: false,
    loginUrl: LOGIN_PATH,
    session: null,
    history: [],
    meeting: null,
    transcript: null,
    transcripts: [],
    annotations: [],
    audioUrl: null,
    recording: null,
    recorderPanel: settings.recorderPanel || null,
    livePanel: settings.livePanel || null,
    viewingHistory: false,
    form: {
      kind: "note",
      offset_ms: 0,
      text: "",
      saving: false,
      error: null,
      status: "",
      offsetEdited: false,
    },
  };
  const scroll = { atBottom: true, top: 0 };

  const dialog = resolveDialog(doc, settings.stopDialog);
  const dialogHandlers = wireDialog(dialog);

  function resolveDialog(document, provided) {
    if (provided && typeof provided.showModal === "function") return provided;
    const existing = document.getElementById("stop-dialog");
    if (existing) return existing;
    const created = el(document, "dialog", {
      className: "dialog",
      attrs: { id: "stop-dialog", "aria-labelledby": "stop-dialog-title" },
    });
    created.append(
      el(document, "h2", { text: "停止录音？", attrs: { id: "stop-dialog-title" } }),
      el(document, "p", {
        text: "停止后仍会保留已确认的分片；未上传内容不能视为已保存。",
      }),
      el(document, "div", {
        className: "dialog-actions",
        children: [
          el(document, "button", {
            className: "btn",
            text: "取消",
            attrs: { type: "button", "data-testid": "stop-cancel" },
          }),
          el(document, "button", {
            className: "btn btn--danger",
            text: "确认停止",
            attrs: { type: "button", "data-testid": "stop-confirm" },
          }),
        ],
      }),
    );
    (document.body || document.documentElement).append(created);
    return created;
  }

  function wireDialog(element) {
    if (!element) return () => {};
    const cancelButton = element.querySelector('[data-testid="stop-cancel"]');
    const confirmButton = element.querySelector('[data-testid="stop-confirm"]');
    const onCancel = () => {
      // Native cancel closes the dialog; the close handler restores focus.
    };
    const onCancelClick = () => {
      element.close();
    };
    const onConfirm = () => {
      element.close();
      win.dispatchEvent(new win.CustomEvent("workbench:stop-recording"));
    };
    const onClose = () => {
      if (pendingWhileDialog) {
        pendingWhileDialog = false;
        render();
      }
      const stopTrigger = root.querySelector('[data-testid="stop-recording"]');
      if (stopTrigger && typeof stopTrigger.focus === "function") {
        stopTrigger.focus();
      }
    };
    element.addEventListener("cancel", onCancel);
    element.addEventListener("close", onClose);
    if (cancelButton) cancelButton.addEventListener("click", onCancelClick);
    if (confirmButton) confirmButton.addEventListener("click", onConfirm);
    return () => {
      element.removeEventListener("cancel", onCancel);
      element.removeEventListener("close", onClose);
      if (cancelButton) cancelButton.removeEventListener("click", onCancelClick);
      if (confirmButton) confirmButton.removeEventListener("click", onConfirm);
    };
  }

  function buildLogoutButton() {
    const button = el(doc, "button", {
      className: "account-logout",
      text: "退出登录",
      attrs: { type: "button", "data-testid": "logout-button" },
    });
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await client.request("/api/cloud/auth/logout", { method: "POST" });
      } catch {
        // 会话可能已经失效，直接回登录门。
      }
      if (typeof settings.onSessionExpired === "function") {
        settings.onSessionExpired();
        return;
      }
      state.loginRequired = true;
      state.session = null;
      lastRenderKey = null;
      render();
    });
    return button;
  }

  function updateAccount() {
    if (!accountEl) return;
    accountEl.replaceChildren();
    const userId = state.session ? state.session.user_id : null;
    if (typeof userId === "string" && userId) {
      const short = userId.length > 12 ? userId.slice(0, 8) + "…" + userId.slice(-2) : userId;
      accountEl.append(el(doc, "span", { className: "account-id", text: "账号 " + short }));
      accountEl.append(buildLogoutButton());
    } else if (state.loginRequired) {
      accountEl.append(
        el(doc, "a", {
          className: "account-login",
          text: "未登录 · 去登录",
          attrs: { href: state.loginUrl || LOGIN_PATH },
        }),
      );
    }
  }

  function render() {
    if (destroyed) return;
    if (dialog && dialog.open) {
      pendingWhileDialog = true;
      return;
    }
    const key = renderKey();
    if (key === lastRenderKey && root.querySelector('[data-testid="workbench"]')) {
      const strip = root.querySelector('[data-testid="recording-strip"]');
      if (strip) fillRecordingStrip(strip, doc, state);
      updateAccount();
      return;
    }
    lastRenderKey = key;
    capturePlayback();
    renderWorkbench(root, state);
    updateAccount();
    wire();
    applyWideDetails();
    restoreScroll();
  }

  function renderKey() {
    const recording = normalizeRecording(state.recording);
    return JSON.stringify([
      state.loading,
      Boolean(state.loginRequired),
      state.error ? String(state.error.status) + ":" + String(state.error.code) : null,
      state.session ? state.session.user_id : null,
      state.meeting ? state.meeting.id : null,
      state.meeting ? state.meeting.current_transcript_id : null,
      state.meeting ? state.meeting.title : null,
      state.transcript ? state.transcript.id : null,
      state.transcript ? state.transcript.status : null,
      state.transcript && state.transcript.segments ? state.transcript.segments.length : 0,
      state.annotations.map((annotation) => (annotation ? annotation.id : "")).join(","),
      recording.active,
      recording.activeMeetingId,
      playbackBlocked(state),
      playbackCaution(state),
    ]);
  }

  function capturePlayback() {
    const audio = root.querySelector('[data-testid="audio-player"]');
    if (!audio) return;
    if (Number.isFinite(audio.currentTime) && audio.currentTime > 0) {
      pendingSeekMs = Math.round(audio.currentTime * 1000);
    }
    resumeAfterMetadata = audio.controls && !audio.paused && !audio.ended;
  }

  function applyWideDetails() {
    let wide = false;
    try {
      wide = win.matchMedia("(min-width: 1280px)").matches;
    } catch {
      wide = false;
    }
    if (!wide) return;
    const panels = root.querySelectorAll("details.panel");
    for (const details of panels) {
      details.open = true;
    }
  }

  function restoreScroll() {
    const scroller = root.querySelector('[data-testid="transcript-scroll"]');
    if (!scroller) {
      syncBackToLatest();
      return;
    }
    if (scroll.atBottom) {
      scroller.scrollTop = scroller.scrollHeight;
    } else {
      scroller.scrollTop = Math.min(scroll.top, scroller.scrollHeight);
    }
    scroller.addEventListener("scroll", onScroll, { passive: true });
    syncBackToLatest();
  }

  function onScroll(event) {
    const scroller = event.currentTarget;
    const remaining = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
    scroll.atBottom = remaining <= 24;
    if (!scroll.atBottom) scroll.top = scroller.scrollTop;
    syncBackToLatest();
  }

  function syncBackToLatest() {
    const button = root.querySelector('[data-testid="back-to-latest"]');
    if (!button) return;
    button.hidden = scroll.atBottom;
  }

  function updatePlaybackPosition(ms) {
    const readout = root.querySelector('[data-testid="audio-position"]');
    if (readout) readout.textContent = formatOffset(ms);
    const input = root.querySelector('[data-testid="annotation-offset"]');
    if (input && !state.form.offsetEdited) {
      state.form.offset_ms = Math.max(0, Math.round(ms));
      input.value = String(state.form.offset_ms);
    }
  }

  function seek(ms) {
    const target = Number(ms);
    if (!Number.isFinite(target) || target < 0) return;
    if (playbackBlocked(state)) return;
    const audio = root.querySelector('[data-testid="audio-player"]');
    if (!audio) return;
    const offset = Math.round(target);
    pendingSeekMs = offset;
    audio.dataset.requestedOffsetMs = String(offset);
    if (audio.readyState >= 1) {
      try {
        audio.currentTime = offset / 1000;
      } catch {
        // The pending seek is applied again on loadedmetadata.
      }
    }
    updatePlaybackPosition(offset);
  }

  function bindAudio() {
    const audio = root.querySelector('[data-testid="audio-player"]');
    if (!audio) return;
    audio.addEventListener("loadedmetadata", () => {
      if (pendingSeekMs !== null) {
        try {
          audio.currentTime = pendingSeekMs / 1000;
        } catch {
          // Keep the requested offset in the dataset for inspection.
        }
        updatePlaybackPosition(pendingSeekMs);
        pendingSeekMs = null;
      }
      if (resumeAfterMetadata) {
        resumeAfterMetadata = false;
        const playing = audio.play();
        if (playing && typeof playing.catch === "function") playing.catch(() => {});
      }
    });
    audio.addEventListener("timeupdate", () => {
      updatePlaybackPosition(audio.currentTime * 1000);
    });
  }

  function bindSeekButtons() {
    const segmentButtons = root.querySelectorAll('[data-testid="segment-seek"]');
    for (const button of segmentButtons) {
      button.addEventListener("click", () => seek(button.dataset.startMs));
    }
    const annotationButtons = root.querySelectorAll('[data-testid="annotation-seek"]');
    for (const button of annotationButtons) {
      button.addEventListener("click", () => seek(button.dataset.offsetMs));
    }
  }

  function annotationError(problem) {
    if (problem && problem.status === 400) return "标注内容或时间不被接受，请检查后重试。";
    if (problem && problem.status === 404) return "该会议不存在或不可访问。";
    if (problem && problem.status === 403) return "标注请求缺少有效的 CSRF 凭证，请刷新页面后重试。";
    const code = problem && problem.code ? "（" + problem.code + "）" : "";
    return "保存失败" + code + "，请稍后重试。";
  }

  function bindAnnotationForm() {
    const form = root.querySelector('[data-testid="annotation-form"]');
    if (!form) return;
    const kind = form.querySelector('[data-testid="annotation-kind"]');
    const offset = form.querySelector('[data-testid="annotation-offset"]');
    const text = form.querySelector('[data-testid="annotation-text"]');
    const save = form.querySelector('[data-testid="annotation-save"]');
    const status = form.querySelector('[data-testid="annotation-status"]');
    const error = form.querySelector('[data-testid="annotation-error"]');
    if (offset) offset.value = String(state.form.offset_ms);
    if (text) text.value = state.form.text;
    if (kind) kind.value = state.form.kind;

    if (kind) {
      kind.addEventListener("change", () => {
        state.form.kind = kind.value;
      });
    }
    if (offset) {
      offset.addEventListener("input", () => {
        state.form.offsetEdited = true;
        state.form.offset_ms = Number(offset.value);
      });
    }
    if (text) {
      text.addEventListener("input", () => {
        state.form.text = text.value;
      });
    }
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      void submitAnnotation();
    });

    function showError(message) {
      state.form.error = message;
      state.form.status = "";
      if (status) status.textContent = "";
      if (error) {
        error.textContent = message;
        error.hidden = false;
      }
    }

    function insertAnnotation(annotation) {
      let list = root.querySelector('[data-testid="annotation-list"]');
      if (!list) {
        const empty = root.querySelector('[data-testid="annotations-empty"]');
        list = el(doc, "ol", {
          className: "annotation-list",
          attrs: { "data-testid": "annotation-list" },
        });
        if (empty) empty.replaceWith(list);
      }
      if (!list) return;
      const item = annotationItem(doc, annotation, state);
      const offsetValue = Number(annotation.offset_ms);
      const children = Array.from(list.children);
      const next = children.find((child) => Number(child.dataset.offsetMs) > offsetValue);
      if (next) list.insertBefore(item, next);
      else list.append(item);
    }

    async function submitAnnotation() {
      if (state.form.saving) return;
      const meetingId = state.meeting ? state.meeting.id : null;
      if (!meetingId) return;
      const kindValue = kind ? kind.value : "note";
      const offsetValue = Number(offset ? offset.value : NaN);
      const textValue = (text ? text.value : "").trim();
      if (!Number.isInteger(offsetValue) || offsetValue < 0) {
        showError("请填写不小于 0 的整数毫秒。");
        return;
      }
      if (textValue.length < 1 || textValue.length > 2000) {
        showError("内容需为 1–2000 字。");
        return;
      }
      state.form.saving = true;
      state.form.error = null;
      state.form.status = "";
      if (error) {
        error.textContent = "";
        error.hidden = true;
      }
      if (status) status.textContent = "";
      if (save) {
        save.disabled = true;
        save.textContent = "正在保存…";
      }
      try {
        const created = await client.request(
          "/api/cloud/meetings/" + encodeURIComponent(meetingId) + "/annotations",
          {
            method: "POST",
            body: { kind: kindValue, offset_ms: offsetValue, text: textValue },
          },
        );
        if (destroyed) return;
        if (!created || typeof created !== "object" || typeof created.id !== "string") {
          throw new Error("invalid_annotation_response");
        }
        state.annotations = sortedAnnotations(state.annotations.concat([created]));
        insertAnnotation(created);
        state.form.text = "";
        state.form.offsetEdited = false;
        state.form.status = "标注已保存";
        if (text) text.value = "";
        if (status) status.textContent = "标注已保存";
        lastRenderKey = renderKey();
      } catch (problem) {
        if (destroyed) return;
        if (problem && problem.status === 401) {
          state.loginRequired = true;
          state.error = null;
          render();
          return;
        }
        showError(annotationError(problem));
      } finally {
        if (!destroyed) {
          state.form.saving = false;
          if (save) {
            save.disabled = false;
            save.textContent = "保存标注";
          }
        }
      }
    }
  }

  function wire() {
    const historyButtons = root.querySelectorAll('[data-testid="history-item"]');
    for (const button of historyButtons) {
      button.addEventListener("click", () => selectMeeting(button.dataset.meetingId));
    }
    const stopButton = root.querySelector('[data-testid="stop-recording"]');
    if (stopButton) {
      stopButton.addEventListener("click", () => {
        if (dialog && typeof dialog.showModal === "function" && !dialog.open) {
          dialog.showModal();
        }
      });
    }
    const backButton = root.querySelector('[data-testid="back-to-latest"]');
    if (backButton) {
      backButton.addEventListener("click", () => {
        const scroller = root.querySelector('[data-testid="transcript-scroll"]');
        scroll.atBottom = true;
        if (scroller) {
          scroller.scrollTop = scroller.scrollHeight;
          scroller.focus({ preventScroll: true });
        }
        syncBackToLatest();
      });
    }
    const retry = root.querySelector('[data-testid="retry-load"]');
    if (retry) retry.addEventListener("click", () => void reload());
    bindAudio();
    bindSeekButtons();
    bindAnnotationForm();
  }

  function setRecording(snapshot) {
    state.recording = snapshot || null;
    const recording = normalizeRecording(snapshot);
    state.viewingHistory = Boolean(
      recording.active &&
        state.meeting &&
        recording.activeMeetingId &&
        recording.activeMeetingId !== state.meeting.id,
    );
    render();
  }

  function currentMeetingFromUrl() {
    try {
      return new URL(win.location ? win.location.href : "http://localhost").searchParams.get("meeting");
    } catch {
      return null;
    }
  }

  async function load(loadOptions) {
    const choices = loadOptions || {};
    const generation = ++loadGeneration;
    if (abortCurrent) abortCurrent();
    const controller = new AbortController();
    abortCurrent = () => controller.abort();
    const requestedMeetingId = typeof choices.meetingId === "string" ? choices.meetingId : null;
    if (requestedMeetingId && state.meeting && state.meeting.id !== requestedMeetingId) {
      // A failed switch must not leave the previous meeting looking current.
      state.meeting = null;
      state.transcript = null;
      state.transcripts = [];
      state.annotations = [];
      state.audioUrl = null;
    }
    state.loading = true;
    state.error = null;
    render();
    try {
      const session = await client.request(SESSION_PATH, { signal: controller.signal });
      if (generation !== loadGeneration) return;
      state.session = session && typeof session === "object" ? session : null;
      state.loginRequired = false;
      state.loginUrl = client.loginUrl ? client.loginUrl() : LOGIN_PATH;

      const page = await client.request(MEETINGS_PATH, { signal: controller.signal });
      if (generation !== loadGeneration) return;
      state.history = Array.isArray(page && page.items)
        ? page.items.filter((meeting) => meeting && typeof meeting.id === "string")
        : [];

      const selected =
        (typeof choices.meetingId === "string" ? choices.meetingId : null) ||
        (state.meeting ? state.meeting.id : null) ||
        (typeof settings.initialMeetingId === "string" ? settings.initialMeetingId : null) ||
        currentMeetingFromUrl() ||
        (state.history[0] ? state.history[0].id : null);

      if (!selected) {
        state.meeting = null;
        state.transcript = null;
        state.transcripts = [];
        state.annotations = [];
        state.audioUrl = null;
        state.loading = false;
        lastRenderKey = null;
        render();
        return;
      }

      const encoded = encodeURIComponent(selected);
      const results = await Promise.all([
        client.request("/api/cloud/meetings/" + encoded, { signal: controller.signal }),
        client.request("/api/cloud/meetings/" + encoded + "/transcripts", {
          signal: controller.signal,
        }),
        client.request("/api/cloud/meetings/" + encoded + "/annotations", {
          signal: controller.signal,
        }),
      ]);
      if (generation !== loadGeneration) return;
      const meeting = results[0];
      const transcripts = results[1];
      const annotations = results[2];
      if (!meeting || typeof meeting.id !== "string") throw new Error("invalid_meeting_response");

      const versions = Array.isArray(transcripts)
        ? transcripts.filter((version) => version && typeof version === "object")
        : [];
      const current =
        versions.find((version) => version.id === meeting.current_transcript_id) ||
        (meeting.current_transcript_id ? null : versions[0] || null);

      state.meeting = meeting;
      state.transcripts = versions;
      state.transcript = current;
      state.annotations = sortedAnnotations(annotations);
      state.audioUrl = "/api/cloud/meetings/" + encoded + "/audio";
      state.loading = false;
      state.error = null;
      const recording = normalizeRecording(state.recording);
      state.viewingHistory = Boolean(
        recording.active &&
          recording.activeMeetingId &&
          recording.activeMeetingId !== meeting.id,
      );
      lastRenderKey = null;
      render();
    } catch (problem) {
      if (generation !== loadGeneration) return;
      if (problem && problem.name === "AbortError") return;
      if (problem && problem.status === 401) {
        state.loginRequired = true;
        state.loginUrl = client.loginUrl ? client.loginUrl() : LOGIN_PATH;
        state.session = null;
        state.meeting = null;
        state.transcript = null;
        state.annotations = [];
        state.history = [];
        state.loading = false;
        state.error = null;
        if (typeof settings.onSessionExpired === "function") {
          settings.onSessionExpired();
          return;
        }
        lastRenderKey = null;
        render();
        return;
      }
      state.loading = false;
      state.error = problem || new Error("load_failed");
      lastRenderKey = null;
      render();
    }
  }

  function selectMeeting(meetingId) {
    if (typeof meetingId !== "string" || meetingId.length === 0) return;
    if (state.meeting && meetingId === state.meeting.id) return;
    void load({ meetingId });
  }

  function reload() {
    return load({ meetingId: state.meeting ? state.meeting.id : null });
  }

  const onRecording = (event) => {
    setRecording(event ? event.detail : null);
  };
  const onRefresh = () => {
    void reload();
  };
  win.addEventListener("workbench:recording", onRecording);
  win.addEventListener("workbench:refresh", onRefresh);
  win.addEventListener("resize", applyWideDetails);

  void load({
    meetingId: typeof settings.initialMeetingId === "string" ? settings.initialMeetingId : null,
  });

  return {
    reload,
    setRecording,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      loadGeneration += 1;
      if (abortCurrent) abortCurrent();
      abortCurrent = null;
      win.removeEventListener("workbench:recording", onRecording);
      win.removeEventListener("workbench:refresh", onRefresh);
      win.removeEventListener("resize", applyWideDetails);
      dialogHandlers();
      try {
        if (dialog && dialog.open) dialog.close();
      } catch {
        // A detached dialog is already closed.
      }
      root.replaceChildren();
      if (accountEl) accountEl.replaceChildren();
    },
  };
}
