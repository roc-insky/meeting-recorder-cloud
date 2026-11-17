// 浏览器录音面板：点击“开始录音”→ 浏览器请求麦克风授权 → getUserMedia 采集 →
// MediaRecorder 分片 → IndexedDB 缓冲 → 带租约上传到 /api/cloud/uploads。
// 勾选“实时转写”时，同一路麦克风音频另走 AudioWorklet → 16 kHz PCM → /api/dev-live（仅开发服务提供）。
//
// 所有动态文本都通过 textContent 写入 DOM；面板只汇报真实状态，不编造转写结果。

import { ChunkBuffer } from "./buffer.mjs";
import { RecordingSession } from "./recording.mjs";
import { UploadPump, createHttpTransport } from "./upload.mjs";
import { createCloudSession } from "./session.mjs";

const LEASE_HEADER = "X-Capture-Lease";
const HEARTBEAT_MS = 40000;
const SNAPSHOT_MS = 1000;
const LIVE_RATE = 16000;
const LIVE_FLUSH_MS = 1000;
const LIVE_PATH = "/api/dev-live";

const ERROR_TEXT = Object.freeze({
  NotAllowedError: "浏览器没有授予麦克风权限。请在地址栏的站点设置里允许麦克风后重试。",
  SecurityError: "浏览器出于安全限制拒绝了麦克风（需要 https 或 127.0.0.1/localhost）。",
  NotFoundError: "没有找到可用的麦克风设备。",
  NotReadableError: "麦克风被其他程序占用或无法读取。",
  capture_in_progress: "该账号已有一场录音在进行（可能在另一个页面），请先结束它。",
  csrf_missing: "登录状态已失效，请刷新页面重新登录。",
});

function el(doc, tag, props = {}) {
  const node = doc.createElement(tag);
  if (props.className) node.className = props.className;
  if (props.text !== undefined) node.textContent = props.text;
  for (const [key, value] of Object.entries(props.attrs || {})) node.setAttribute(key, value);
  return node;
}

function pad(value) {
  return String(value).padStart(2, "0");
}

function defaultTitle(now = new Date()) {
  return (
    "会议 " + now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate()) +
    " " + pad(now.getHours()) + ":" + pad(now.getMinutes())
  );
}

function clock(seconds) {
  const whole = Math.max(0, Math.floor(seconds));
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = whole % 60;
  return (h ? pad(h) + ":" : "") + pad(m) + ":" + pad(s);
}

export function describeError(error) {
  if (!error) return "未知错误";
  const key = error.code || error.name;
  if (ERROR_TEXT[key]) return ERROR_TEXT[key];
  if (error.status && error.message) return "服务器拒绝了请求：" + error.message;
  return error.message || String(error);
}

// 把 Float32（任意采样率）线性累积下采样为 16 kHz Int16。
export class Downsampler {
  constructor(inputRate, outputRate = LIVE_RATE) {
    this.ratio = inputRate / outputRate;
    this.carry = new Float32Array(0);
    this.position = 0;
  }

  push(input) {
    const merged = new Float32Array(this.carry.length + input.length);
    merged.set(this.carry, 0);
    merged.set(input, this.carry.length);
    const out = [];
    let pos = this.position;
    while (pos + this.ratio <= merged.length) {
      const from = Math.floor(pos);
      const to = Math.max(from + 1, Math.floor(pos + this.ratio));
      let sum = 0;
      for (let i = from; i < to; i += 1) sum += merged[i];
      const mean = sum / (to - from);
      out.push(Math.max(-1, Math.min(1, mean)));
      pos += this.ratio;
    }
    const keepFrom = Math.floor(pos);
    this.carry = merged.slice(keepFrom);
    this.position = pos - keepFrom;
    const pcm = new Int16Array(out.length);
    for (let i = 0; i < out.length; i += 1) {
      pcm[i] = out[i] < 0 ? Math.round(out[i] * 32768) : Math.round(out[i] * 32767);
    }
    return pcm;
  }
}

export function mountRecorder(root, options = {}) {
  if (!root) throw new TypeError("root element is required");
  const doc = root.ownerDocument;
  const win = doc.defaultView;
  const client = options.client || createCloudSession();
  const onMeeting = options.onMeeting || (() => {});
  const onFinished = options.onFinished || (() => {});
  const onSessionExpired = options.onSessionExpired || (() => {});

  // -- DOM ------------------------------------------------------------------
  root.replaceChildren();
  const heading = el(doc, "h2", { className: "recorder-heading", text: "浏览器录音", attrs: { id: "recorder-heading" } });
  const titleInput = el(doc, "input", {
    className: "recorder-title",
    attrs: { type: "text", maxlength: "200", "aria-label": "会议标题", "data-testid": "recorder-title" },
  });
  titleInput.value = defaultTitle();
  const toggle = el(doc, "button", {
    className: "recorder-button",
    text: "开始录音",
    attrs: { type: "button", "data-testid": "recorder-toggle" },
  });
  const liveLabel = el(doc, "label", { className: "recorder-live" });
  const liveBox = el(doc, "input", { attrs: { type: "checkbox", "data-testid": "recorder-live" } });
  liveLabel.append(liveBox, el(doc, "span", { text: "实时转写" }));
  const controls = el(doc, "div", { className: "recorder-controls" });
  controls.append(titleInput, liveLabel, toggle);
  const status = el(doc, "p", {
    className: "recorder-status",
    text: "点击“开始录音”后，浏览器会请求麦克风授权。",
    attrs: { role: "status", "aria-live": "polite", "data-testid": "recorder-status" },
  });
  const note = el(doc, "p", { className: "recorder-note", attrs: { "data-testid": "recorder-live-note" } });
  const liveList = el(doc, "ol", { className: "recorder-live-list", attrs: { "data-testid": "recorder-live-list", "aria-label": "实时转写" } });
  // 录音控件留在左栏；实时文本单独成卡，由工作台放进中栏（文字稿上方）。
  const section = el(doc, "section", {
    className: "recorder-panel",
    attrs: { "aria-labelledby": "recorder-heading", "data-testid": "recorder-panel" },
  });
  section.append(heading, controls, status);
  const liveCard = el(doc, "section", {
    className: "recorder-live-card",
    attrs: { "data-testid": "recorder-live-card", "aria-labelledby": "recorder-live-heading" },
  });
  liveCard.hidden = true;
  liveCard.append(
    el(doc, "h2", {
      className: "recorder-live-heading",
      text: "本次实时转写",
      attrs: { id: "recorder-live-heading" },
    }),
    note,
    liveList,
  );
  root.append(section, liveCard);

  // -- runtime state --------------------------------------------------------
  let current = null; // 进行中的录音上下文
  let busy = false;
  const sleep = (ms) => new Promise((resolve) => win.setTimeout(resolve, ms));

  function setStatus(text, tone = "") {
    status.textContent = text;
    status.dataset.tone = tone;
  }

  function setRunning(running) {
    toggle.textContent = running ? "结束录音" : "开始录音";
    toggle.classList.toggle("is-recording", running);
    titleInput.disabled = running;
    liveBox.disabled = running;
  }

  function addLiveText(items) {
    for (const item of items || []) {
      if (!item || typeof item.text !== "string" || !item.text) continue;
      liveList.append(
        el(doc, "li", { text: "[" + clock(Number(item.start_s) || 0) + "] " + item.text }),
      );
    }
    if (items && items.length) liveList.lastElementChild?.scrollIntoView?.({ block: "nearest" });
  }

  function emitSnapshot(ctx) {
    ctx.rec
      .snapshot()
      .then((snapshot) => {
        win.dispatchEvent(
          new win.CustomEvent("workbench:recording", {
            detail: { ...snapshot, activeMeetingId: ctx.meetingId },
          }),
        );
      })
      .catch(() => {});
  }

  // -- live transcription ---------------------------------------------------
  async function startLive(stream) {
    const created = await client.request(LIVE_PATH, { method: "POST" });
    const live = {
      id: created.id,
      pending: [],
      pendingSamples: 0,
      inflight: null,
      timer: null,
      closed: false,
      context: null,
      node: null,
      source: null,
      sink: null,
      modelState: created.model_state,
    };
    const AudioContextCtor = win.AudioContext || win.webkitAudioContext;
    let context;
    try {
      context = new AudioContextCtor({ sampleRate: LIVE_RATE });
    } catch {
      context = new AudioContextCtor();
    }
    await context.audioWorklet.addModule(new URL("./pcm-worklet.js", import.meta.url).toString());
    if (context.state === "suspended") await context.resume();
    const resampler = new Downsampler(context.sampleRate, LIVE_RATE);
    const source = context.createMediaStreamSource(stream);
    const node = new win.AudioWorkletNode(context, "pcm-tap", { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1 });
    const sink = context.createGain();
    sink.gain.value = 0;
    node.port.onmessage = (event) => {
      if (live.closed) return;
      const pcm = resampler.push(event.data);
      if (pcm.length) {
        live.pending.push(pcm);
        live.pendingSamples += pcm.length;
      }
    };
    source.connect(node);
    node.connect(sink);
    sink.connect(context.destination);
    Object.assign(live, { context, node, source, sink });

    live.send = async () => {
      if (live.inflight) return live.inflight;
      if (live.pendingSamples === 0 && !live.poll) return null;
      const merged = new Int16Array(live.pendingSamples);
      let at = 0;
      for (const part of live.pending) {
        merged.set(part, at);
        at += part.length;
      }
      live.pending = [];
      live.pendingSamples = 0;
      live.inflight = client
        .request(LIVE_PATH + "/" + live.id + "/pcm", { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: merged })
        .then((result) => {
          live.modelState = result.model_state;
          addLiveText(result.texts);
          updateLiveNote(live);
        })
        .catch((error) => {
          live.error = error;
          note.textContent = "实时转写出错，录音不受影响：" + describeError(error);
        })
        .finally(() => {
          live.inflight = null;
        });
      return live.inflight;
    };
    live.timer = win.setInterval(() => {
      void live.send();
    }, LIVE_FLUSH_MS);
    updateLiveNote(live);
    return live;
  }

  function updateLiveNote(live) {
    if (live.error) return;
    note.textContent =
      live.modelState === "ready"
        ? "实时转写已就绪（仅开发服务提供，文字不保存到云端）。"
        : "实时转写模型加载中，首次约需 1 分钟，期间说的话会在加载完成后依次转写。";
  }

  async function finishLive(live) {
    if (!live) return;
    live.closed = true;
    win.clearInterval(live.timer);
    try {
      live.node.port.onmessage = null;
      live.source.disconnect();
      live.node.disconnect();
      await live.context.close();
    } catch {
      // 音频图已关闭。
    }
    try {
      if (live.inflight) await live.inflight;
      const merged = new Int16Array(live.pendingSamples);
      let at = 0;
      for (const part of live.pending) {
        merged.set(part, at);
        at += part.length;
      }
      if (merged.length) {
        const sent = await client.request(LIVE_PATH + "/" + live.id + "/pcm", { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: merged });
        addLiveText(sent.texts);
      }
      note.textContent = "正在完成最后几句的转写…";
      const done = await client.request(LIVE_PATH + "/" + live.id + "/finish", { method: "POST" });
      addLiveText(done.texts);
      note.textContent = "实时转写已结束（文字仅显示在此页面，没有保存到云端）。";
    } catch (error) {
      note.textContent = "实时转写收尾失败：" + describeError(error);
    }
  }

  // -- start / stop ---------------------------------------------------------
  // 登录过期：先把已切好的分片封存，再把页面交回登录门（不丢已录内容）。
  async function expireSession() {
    const ctx = current;
    if (!ctx) {
      onSessionExpired();
      return;
    }
    current = null;
    toggle.disabled = true;
    setStatus("登录已过期，正在保存已录制的音频…", "warning");
    try {
      await ctx.rec.stop();
    } catch {
      // 已尽量让上传队列把已切分片交出去。
    }
    await cleanup(ctx, { release: true });
    await finishLive(ctx.live);
    setRunning(false);
    toggle.disabled = false;
    setStatus("登录已过期，音频已尽量保存；请重新登录后继续。", "warning");
    onSessionExpired();
  }

  async function start() {
    const title = titleInput.value.trim();
    if (!title) {
      setStatus("请先填写会议标题。", "error");
      titleInput.focus();
      return;
    }
    // 先确认登录：未登录不得弹麦克风授权、不得创建会议或租约。
    setStatus("正在检查登录状态…", "info");
    let session;
    try {
      session = await client.request("/api/cloud/auth/session");
    } catch (error) {
      if (error && error.status === 401) {
        await expireSession();
        return;
      }
      setStatus("无法开始录音：" + describeError(error), "error");
      return;
    }
    if (!session || !session.user_id) {
      await expireSession();
      return;
    }
    setStatus("正在请求麦克风授权…请在浏览器弹窗中选择“允许”。", "info");
    const mediaDevices = win.navigator.mediaDevices;
    if (!mediaDevices || typeof mediaDevices.getUserMedia !== "function") {
      setStatus(ERROR_TEXT.SecurityError, "error");
      return;
    }
    // 先弹出授权：没有麦克风就不创建会议、不占用租约。
    let stream;
    try {
      stream = await mediaDevices.getUserMedia({ audio: true, video: false });
    } catch (error) {
      setStatus(describeError(error), "error");
      return;
    }
    const ctx = { stream, meetingId: null, leaseToken: null, uploadId: null, rec: null, live: null, heartbeat: null, snapshotTimer: null, startedAt: Date.now() };
    liveList.replaceChildren();
    note.textContent = "";
    liveCard.hidden = !liveBox.checked;
    try {
      const userId = session.user_id;
      const meeting = await client.request("/api/cloud/meetings", { method: "POST", body: { title, mode: "mic" } });
      ctx.meetingId = meeting.id;
      const lease = await client.request("/api/cloud/capture-lease", { method: "POST", body: { meeting_id: meeting.id } });
      ctx.leaseToken = lease.lease_token;
      const leased = {
        ...client,
        request(path, init = {}) {
          if (typeof path === "string" && path.includes("/api/cloud/uploads")) {
            const headers = new Headers(init.headers || {});
            headers.set(LEASE_HEADER, ctx.leaseToken);
            return client.request(path, { ...init, headers });
          }
          return client.request(path, init);
        },
      };
      const upload = await leased.request("/api/cloud/uploads", { method: "POST", body: { meeting_id: meeting.id } });
      ctx.uploadId = upload.id;
      ctx.leased = leased;

      const buffer = await ChunkBuffer.open();
      const pump = new UploadPump({ buffer, transport: createHttpTransport({ session: leased }) });
      const epochId = (win.crypto && win.crypto.randomUUID ? win.crypto.randomUUID() : "epoch-" + Date.now());
      const anchorMs = win.performance.now();
      const startedAt = new Date().toISOString();
      let wrapped = false;
      const mediaProxy = {
        getUserMedia: async (constraints) => {
          if (!wrapped) {
            wrapped = true;
            return stream;
          }
          return mediaDevices.getUserMedia(constraints);
        },
      };
      const rec = new RecordingSession({
        userId,
        uploadId: upload.id,
        mode: "mic",
        epoch: { id: epochId, offsetMs: 0, anchorMs },
        mediaDevices: mediaProxy,
        buffer,
        pump,
        onState: () => emitSnapshot(ctx),
      });
      ctx.rec = rec;
      await rec.start();
      const specs = rec.handle.specs();
      const trackSettings = stream.getAudioTracks()[0]?.getSettings?.() || {};
      await leased.request("/api/cloud/uploads/" + encodeURIComponent(upload.id) + "/epochs", {
        method: "POST",
        body: {
          id: epochId,
          offset_ms: 0,
          uncertainty_ms: 50,
          started_at: startedAt,
          tracks: specs.map((spec) => ({
            id: spec.id,
            source: spec.source,
            codec: spec.codec,
            sample_rate: spec.sample_rate || trackSettings.sampleRate || 48000,
          })),
        },
      });
      ctx.heartbeat = win.setInterval(() => {
        client
          .request("/api/cloud/capture-lease/heartbeat", { method: "POST", body: { lease_token: ctx.leaseToken } })
          .catch((error) => {
            if (error && error.status === 401) void expireSession();
          });
      }, HEARTBEAT_MS);
      if (liveBox.checked) {
        try {
          ctx.live = await startLive(stream);
        } catch (error) {
          note.textContent = "实时转写不可用，只录音：" + describeError(error);
        }
      }
    } catch (error) {
      await cleanup(ctx, { release: true });
      setStatus("无法开始录音：" + describeError(error), "error");
      return;
    }
    current = ctx;
    setRunning(true);
    ctx.snapshotTimer = win.setInterval(() => {
      emitSnapshot(ctx);
      setStatus("正在录音 " + clock((Date.now() - ctx.startedAt) / 1000) + " · 音频由浏览器采集并分片上传", "recording");
    }, SNAPSHOT_MS);
    setStatus("正在录音 00:00 · 音频由浏览器采集并分片上传", "recording");
    emitSnapshot(ctx);
    onMeeting(ctx.meetingId);
  }

  async function cleanup(ctx, { release }) {
    win.clearInterval(ctx.heartbeat);
    win.clearInterval(ctx.snapshotTimer);
    for (const track of ctx.stream?.getTracks?.() || []) {
      try {
        track.stop();
      } catch {
        // 已结束。
      }
    }
    if (release && ctx.leaseToken) {
      try {
        await client.request("/api/cloud/capture-lease/release", { method: "POST", body: { lease_token: ctx.leaseToken } });
      } catch {
        // 封存时服务器已释放租约。
      }
    }
  }

  // 停止后：入队一次会后转写，然后轮询文字稿；出稿就刷新中栏。
  async function watchTranscript(meetingId, inputVersion) {
    const base = "/api/cloud/meetings/" + encodeURIComponent(meetingId);
    try {
      await client.request(base + "/tasks/final", {
        method: "POST",
        body: { input_version: inputVersion, force: false },
      });
    } catch (error) {
      if (error && error.status === 401) return;
      if (!error || error.status !== 409) {
        setStatus("音频已保存，但排队转写失败：" + describeError(error), "warning");
        return;
      }
    }
    setStatus("音频已保存，正在转写（开发 worker 首次要加载模型，约需一分钟）…", "info");
    const deadline = Date.now() + 180000;
    while (Date.now() < deadline) {
      await sleep(3000);
      let versions;
      try {
        versions = await client.request(base + "/transcripts");
      } catch (error) {
        if (error && error.status === 401) return;
        continue;
      }
      const ready = Array.isArray(versions)
        ? versions.find((version) => version && Array.isArray(version.segments) && version.segments.length)
        : null;
      if (ready) {
        setStatus("会后文字稿已生成，已显示在中间一栏。", "success");
        win.dispatchEvent(new win.CustomEvent("workbench:refresh"));
        return;
      }
    }
    setStatus("音频已保存；转写 3 分钟内没有完成（开发 worker 可能没启动），稍后刷新页面查看。", "warning");
  }

  async function stop() {
    const ctx = current;
    if (!ctx) return;
    current = null;
    setStatus("正在结束录音并上传最后的分片…", "info");
    toggle.disabled = true;
    let result = null;
    let failure = null;
    try {
      result = await ctx.rec.stop();
    } catch (error) {
      failure = error;
    }
    // 封存成功时服务器已释放租约；仅在没封存成功时才主动释放，避免多余的 409。
    await cleanup(ctx, { release: !(result && result.seal) });
    await finishLive(ctx.live);
    emitSnapshot(ctx);
    const sealed = result && result.seal;
    if (failure) {
      setStatus("结束录音时出错：" + describeError(failure), "error");
    } else if (sealed) {
      setStatus(
        sealed.status === "complete"
          ? "录音已结束，音频已完整保存到云端。"
          : "录音已结束，但音频有缺口（已如实标注）。",
        sealed.status === "complete" ? "success" : "warning",
      );
      if (sealed.status === "complete" && Number.isInteger(sealed.input_version)) {
        void watchTranscript(ctx.meetingId, sealed.input_version);
      }
    } else {
      setStatus("录音已结束，但封存失败；已缓存的音频会在重新登录后继续上传。", "warning");
    }
    setRunning(false);
    toggle.disabled = false;
    onFinished(ctx.meetingId);
  }

  toggle.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    try {
      if (current) await stop();
      else await start();
    } finally {
      busy = false;
    }
  });

  // 左栏录音状态条自带的"停止录音"按钮会走 workbench 的确认对话框，确认后派发该事件；
  // 之前生产页面没有任何监听者，按钮点了等于没反应。
  win.addEventListener("workbench:stop-recording", () => {
    if (busy || !current) return;
    busy = true;
    void (async () => {
      try {
        await stop();
      } finally {
        busy = false;
      }
    })();
  });

  win.addEventListener("beforeunload", (event) => {
    if (!current) return undefined;
    event.preventDefault();
    event.returnValue = "";
    return "";
  });

  return { panel: section, liveCard, isRecording: () => Boolean(current) };
}
