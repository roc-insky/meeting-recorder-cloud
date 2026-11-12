/* 会议录音控制台前端：原生 JS，无框架、无构建、无外链。
 * 所有文本一律经 textContent 写入页面；只有面试题结果（interview.js）经过净化后才允许富文本。
 * 本文件与 interview.js 是同一全局作用域下的两个经典脚本，interview.js 复用这里的 api / toast / busy 等函数。
 */
'use strict';

const KINDS = { note: '📝笔记', highlight: '⭐重点', comment: '💬注释' };
const FINAL_LABELS = { idle: '空闲', running: '转写中', completed: '已完成', failed: '失败' };
const REC_LABELS = { idle: '空闲', recording: '录音中', stopping: '正在停止' };
const VP_SECONDS = 30;
const VP_POLL_MS = 500;
const SSE_RETRY_MS = 2000;
const VP_BUSY_POLL_MS = 1500;
const TS_LINE = /^\[(\d{2,}):(\d{2}):(\d{2})\] ?(.*)$/;

const $ = (id) => document.getElementById(id);

// ---------- 通用：请求、提示、元素构造 ----------
class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

/** 调用后端；失败时抛 ApiError，消息优先取后端返回的中文 {error}。 */
async function api(method, path, body) {
  const opts = { method, headers: {}, cache: 'no-store' };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new ApiError('无法连接服务器，请确认程序仍在运行', 0);
  }
  let data = null;
  try {
    data = await res.json();
  } catch (e) {
    data = null;
  }
  if (!res.ok) {
    const msg = data && typeof data.error === 'string' && data.error ? data.error : '请求失败（' + res.status + '）';
    throw new ApiError(msg, res.status);
  }
  return data;
}

let toastTimer = null;
function toast(message, isError) {
  const box = $('toast');
  box.textContent = message;
  box.className = isError ? 'toast error' : 'toast';
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, isError ? 6000 : 3000);
}

function errText(e) {
  return e instanceof Error && e.message ? e.message : '操作失败';
}

/** 创建元素：props 里的 text 走 textContent，其余按属性设置；children 为元素或文字。 */
function h(tag, props, children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'text') node.textContent = v;
    else if (k === 'class') node.className = v;
    else if (k === 'value' || k === 'checked' || k === 'disabled') node[k] = v;
    else node.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of children || []) node.append(c);
  return node;
}

function fmtTs(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hh = Math.floor(total / 3600);
  const mm = Math.floor((total % 3600) / 60);
  const ss = total % 60;
  return [hh, mm, ss].map((n) => String(n).padStart(2, '0')).join(':');
}

/** 请求进行中：按钮禁用、完成后恢复；flightKey 让 refreshControls 也知道此刻有请求在途。 */
const flight = new Set();
async function busy(flightKey, buttons, fn) {
  flight.add(flightKey);
  for (const b of buttons) b.disabled = true;
  refreshControls();
  try {
    return await fn();
  } finally {
    flight.delete(flightKey);
    for (const b of buttons) b.disabled = false;
    refreshControls();
    if (typeof refreshInterviewControls === 'function') refreshInterviewControls();
  }
}

// ---------- 状态 ----------
const S = {
  snap: { state: 'idle', stopping: false, final: 'idle', meeting_id: null, elapsed: 0, level: 0,
          live: false, live_state: 'idle', live_error: null, error: null },
  vpBusy: false,
  viewId: null,          // 当前查看的会议 id
  title: '',
  lines: [],             // 转写原文逐行
  liveSeen: new Set(),   // 已加入的实时行，用于去重
  annotations: [],
  selected: -1,          // 选中的转写行下标
  hasTranscript: false,
  speakers: [],
  meetings: [],
  loadToken: 0,
  timerBase: 0,
  timerAt: 0,
  lastSeq: null,
  everConnected: false,
  summaryMsg: '',        // 发言归纳的进度/错误提示，空则显示默认引导
};

// ---------- 标签页 ----------
const TABS = ['meeting', 'interview', 'voiceprint'];
function showTab(name) {
  for (const t of TABS) {
    const on = t === name;
    const tab = $('tab-' + t);
    tab.setAttribute('aria-selected', on ? 'true' : 'false');
    tab.tabIndex = on ? 0 : -1;
    $('view-' + t).hidden = !on;
  }
  document.dispatchEvent(new CustomEvent('mr:tab', { detail: name }));
  if (name === 'voiceprint') loadVoiceprints();
}

function initTabs() {
  for (const t of TABS) {
    $('tab-' + t).addEventListener('click', () => showTab(t));
    $('tab-' + t).addEventListener('keydown', (e) => {
      const i = TABS.indexOf(t);
      let j = null;
      if (e.key === 'ArrowRight') j = (i + 1) % TABS.length;
      else if (e.key === 'ArrowLeft') j = (i + TABS.length - 1) % TABS.length;
      if (j !== null) {
        e.preventDefault();
        showTab(TABS[j]);
        $('tab-' + TABS[j]).focus();
      }
    });
  }
}

// ---------- 转写行渲染 ----------
/** 渲染一行转写：带 [HH:MM:SS] 的行是两列 grid（时间 | 文本），其余行是普通文字。
 * onClick(秒数, 元素, 标注) 为空则不可点击；anchor 是落在这一行上的标注数组，每条在行尾打一个标记。
 * 全部内容经 textContent 写入。 */
function renderTranscriptLine(raw, onClick, anchor) {
  const m = TS_LINE.exec(raw);
  if (!m) {
    return h('div', { class: 'line-plain', text: raw });
  }
  const t = Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]);
  const text = h('span', { class: 'line-text' });
  text.textContent = m[4];
  const time = h('span', { class: 'line-time' });
  time.textContent = m[1] + ':' + m[2] + ':' + m[3];
  const row = h('div', { class: 'line' }, [time, text]);
  row.dataset.t = String(t);
  for (const ann of anchor || []) {
    const mark = h('span', { class: 'mark' });
    mark.textContent = Array.from(KINDS[ann.kind] || '📝')[0];
    mark.title = (KINDS[ann.kind] || '') + '：' + ann.text;
    if (onClick) {
      mark.addEventListener('click', (ev) => {
        ev.stopPropagation();
        onClick(t, row, ann);
      });
    }
    text.append(mark);
  }
  if (onClick) {
    row.setAttribute('role', 'button');
    row.tabIndex = 0;
    row.addEventListener('click', () => onClick(t, row));
    row.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        onClick(t, row);
      }
    });
  }
  return row;
}

/** 每条标注挂到「时间不晚于它」的最后一个带时间的转写行；都比它晚则挂第一行。返回 行下标 -> 标注数组。 */
function anchorMap() {
  const stamps = [];
  S.lines.forEach((raw, i) => {
    const m = TS_LINE.exec(raw);
    if (m) stamps.push([i, Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3])]);
  });
  const map = new Map();
  if (!stamps.length) return map;
  for (const ann of S.annotations) {
    let idx = stamps[0][0];
    for (const [i, t] of stamps) {
      if (t <= ann.t) idx = i;
      else break;
    }
    if (!map.has(idx)) map.set(idx, []);
    map.get(idx).push(ann);
  }
  return map;
}

function renderTranscript() {
  const box = $('transcript');
  const scroller = $('col-main');
  const stick = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 60;
  const anchors = anchorMap();
  const nodes = S.lines.map((raw, i) => {
    const node = renderTranscriptLine(raw, (t, el, ann) => onLineClick(i, t, ann), anchors.get(i));
    if (i === S.selected) node.classList.add('selected');
    node.dataset.index = String(i);
    return node;
  });
  box.replaceChildren(...nodes);
  $('transcript-empty').hidden = S.lines.length > 0;
  $('transcript-title').textContent = S.viewId ? (S.title || S.viewId) : '转写';
  if (stick && S.snap.state === 'recording' && S.viewId === S.snap.meeting_id) {
    scroller.scrollTop = scroller.scrollHeight;
  }
}

function flashNode(node) {
  if (!node) return;
  node.scrollIntoView({ block: 'center', behavior: 'smooth' });
  node.classList.remove('flash');
  void node.offsetWidth;
  node.classList.add('flash');
}

function lineNode(index) {
  return $('transcript').querySelector('[data-index="' + index + '"]');
}

function selectLine(index) {
  S.selected = index;
  for (const n of $('transcript').querySelectorAll('.line.selected')) n.classList.remove('selected');
  const node = lineNode(index);
  if (node) node.classList.add('selected');
}

function onLineClick(index, t, ann) {
  selectLine(index);
  if (ann) {
    const li = $('ann-list').querySelector('[data-ann="' + S.annotations.indexOf(ann) + '"]');
    flashNode(li);
    return;
  }
  $('ann-time').value = String(t);
}

function clearView() {
  S.viewId = null;
  S.title = '';
  S.lines = [];
  S.liveSeen = new Set();
  S.annotations = [];
  S.selected = -1;
  S.hasTranscript = false;
  S.speakers = [];
  S.loadToken += 1;
  $('ann-time').value = '';
  $('ann-text').value = '';
  $('summary-result').textContent = '';
  S.summaryMsg = '';
  renderSpeakers();
  renderAnnotations();
  renderTranscript();
  renderHistory();
  refreshControls();
}

function splitLines(text) {
  return String(text || '').split('\n').map((s) => s.replace(/\r$/, '')).filter((s) => s.trim() !== '');
}

// ---------- 历史会议 ----------
async function loadHistory() {
  try {
    const data = await api('GET', '/api/meetings');
    S.meetings = Array.isArray(data.meetings) ? data.meetings : [];
    renderHistory();
  } catch (e) {
    toast(errText(e), true);
  }
}

function meetingActive(id) {
  return id === S.snap.meeting_id && (S.snap.state === 'recording' || S.snap.final === 'running');
}

function renderHistory() {
  const list = $('history-list');
  histHoverItem = null;              // 列表重建后旧行失效，悬停状态一并清掉
  clearTimeout(histHoverTimer);
  const items = S.meetings.map((m) => {
    const active = meetingActive(m.id);
    const title = h('button', { type: 'button', class: 'hist-title', title: m.title });
    title.textContent = m.title + (active ? '（进行中）' : '');
    title.addEventListener('click', () => selectMeeting(m.id));
    const edit = h('button', { type: 'button', class: 'small', 'aria-label': '编辑标题：' + m.title, disabled: active });
    edit.textContent = '编辑';
    edit.addEventListener('click', () => renameMeeting(m, edit));
    const del = h('button', { type: 'button', class: 'small danger', 'aria-label': '删除会议：' + m.title, disabled: active });
    del.textContent = '删除';
    del.addEventListener('click', () => deleteMeeting(m, del));
    const parts = [title];
    if (m.incomplete) {
      parts.push(h('span', {
        class: 'hist-badge',
        style: 'font-size:11px;color:#b45309;border:1px solid #f59e0b;border-radius:4px;padding:0 4px;white-space:nowrap;',
        text: '未完成',
      }));
    }
    const actions = h('div', { class: 'hist-actions' });
    if (m.incomplete || m.has_transcript) {
      const force = !!m.has_transcript;
      const retry = h('button', {
        type: 'button', class: 'small',
        'aria-label': (force ? '重新转写：' : '补转写：') + m.title,
        disabled: active,
      });
      retry.textContent = force ? '重新转写' : '补转写';
      retry.addEventListener('click', () => retranscribeMeeting(m, retry, force));
      actions.append(retry);
    }
    actions.append(edit, del);
    parts.push(actions);
    const cls = 'hist-item' + (m.id === S.viewId ? ' current' : '') + (m.has_transcript ? '' : ' no-text');
    return h('li', { class: cls }, parts);
  });
  list.replaceChildren(...items);
  $('history-empty').hidden = items.length > 0;
}

// ---------- 历史会议：操作按钮悬停/聚焦才显示 ----------
const HIST_ACTIONS_DELAY_MS = 150;   // 鼠标停留多久才显示，避免扫过列表时按钮闪烁
let histHoverTimer = null;
let histHoverItem = null;

/** 只认键盘焦点：鼠标点击标题后不该把按钮一直留在屏幕上。 */
function historyFocusVisible(node) {
  try { return node.matches(':focus-visible'); } catch (e) { return true; }
}

function bindHistoryActions() {
  const list = $('history-list');
  list.addEventListener('mouseover', (e) => {
    const li = e.target.closest('.hist-item');
    if (!li || li === histHoverItem) return;          // 行内移动不算重新进入
    clearTimeout(histHoverTimer);
    histHoverItem = li;
    histHoverTimer = setTimeout(() => li.classList.add('show-actions'), HIST_ACTIONS_DELAY_MS);
  });
  list.addEventListener('mouseout', (e) => {
    const li = e.target.closest('.hist-item');
    if (!li || li !== histHoverItem) return;
    if (e.relatedTarget && li.contains(e.relatedTarget)) return;
    clearTimeout(histHoverTimer);
    li.classList.remove('show-actions');
    histHoverItem = null;
  });
  list.addEventListener('focusin', (e) => {
    const li = e.target.closest('.hist-item');
    if (!li || !historyFocusVisible(e.target)) return;
    clearTimeout(histHoverTimer);
    li.classList.add('show-actions');
  });
  list.addEventListener('focusout', (e) => {
    const li = e.target.closest('.hist-item');
    if (!li || (e.relatedTarget && li.contains(e.relatedTarget))) return;
    li.classList.remove('show-actions');
  });
}

async function renameMeeting(m, btn) {
  const next = window.prompt('新的会议标题（最多 100 字）', m.title);
  if (next === null) return;
  await busy('rename', [btn], async () => {
    try {
      const r = await api('PATCH', '/api/meetings/' + encodeURIComponent(m.id), { title: next });
      if (S.viewId === m.id) S.title = r.title;
      toast('标题已更新');
      await loadHistory();
      renderTranscript();
    } catch (e) {
      toast(errText(e), true);
    }
  });
}

async function retranscribeMeeting(m, btn, force) {
  if (force && !window.confirm('重新转写会覆盖现有文字稿；旧稿会先备份为 transcript.md.bak。继续？')) return;
  await busy('retranscribe:' + m.id, [btn], async () => {
    try {
      await api('POST', '/api/meetings/' + encodeURIComponent(m.id) + '/retranscribe', { force: Boolean(force) });
      toast(force ? '已重新转写，旧稿备份为 transcript.md.bak' : '补转写完成');
      await loadHistory();
      if (S.viewId === m.id) await selectMeeting(m.id);
    } catch (e) {
      toast(errText(e), true);
    }
  });
}

async function deleteMeeting(m, btn) {
  if (!window.confirm('确定删除会议“' + m.title + '”？录音、文字稿和标注都会被删除，无法恢复。')) return;
  await busy('delete', [btn], async () => {
    try {
      await api('DELETE', '/api/meetings/' + encodeURIComponent(m.id));
      if (S.viewId === m.id) clearView();
      toast('已删除');
      await loadHistory();
    } catch (e) {
      toast(errText(e), true);
    }
  });
}

/** 打开一场会议：转写（没有文字稿时用实时转写内容）、标注和说话人。 */
async function selectMeeting(id) {
  const token = ++S.loadToken;
  try {
    const d = await api('GET', '/api/meetings/' + encodeURIComponent(id));
    if (token !== S.loadToken) return;     // 期间又点了别的会议
    const sameMeeting = S.viewId === d.id;
    S.viewId = d.id;
    S.title = d.title;
    const source = d.transcript ? d.transcript : (d.live || '');
    S.hasTranscript = Boolean(d.transcript);
    S.lines = splitLines(source);
    S.liveSeen = new Set(S.lines);
    S.annotations = Array.isArray(d.annotations) ? d.annotations : [];
    S.speakers = Array.isArray(d.speakers) ? d.speakers : [];
    if (!sameMeeting) {
      S.selected = -1;
      $('ann-time').value = '';
      $('summary-result').textContent = '';
      S.summaryMsg = '';
    }
    renderSpeakers();
    renderAnnotations();
    renderTranscript();
    renderHistory();
    refreshControls();
  } catch (e) {
    if (token !== S.loadToken) return;
    toast(errText(e), true);
    if (e.status === 404) loadHistory();
  }
}

// ---------- 录音控制 ----------
function parseSpeakers() {
  const raw = $('speakers').value.trim();
  if (raw === '') return null;
  if (!/^\d+$/.test(raw)) throw new Error('当前会议人数必须是 1–20 的整数');
  const n = Number(raw);
  if (n < 1 || n > 20) throw new Error('当前会议人数必须是 1–20 的整数');
  return n;
}

async function startRecording() {
  let speakers;
  try {
    speakers = parseSpeakers();
  } catch (e) {
    toast(errText(e), true);
    $('speakers').focus();
    return;
  }
  const body = { live: $('live-toggle').checked };
  if (speakers !== null) body.speakers = speakers;
  await busy('start', [$('btn-start')], async () => {
    try {
      const r = await api('POST', '/api/recording/start', body);   // 失败时保留原有选中行与转写
      beginRecordingView(r.meeting_id);                            // 成功后才清掉上一场的选中行与转写残留
    } catch (e) {
      toast(errText(e), true);
    }
  });
}

/** 新录音开始：视图切到这场会议，空白转写，等待实时转写事件。 */
function beginRecordingView(meetingId) {
  if (S.viewId === meetingId) return;
  clearView();
  S.viewId = meetingId;
  S.title = meetingId;
  renderTranscript();
  loadHistory().then(renderHistory);
  refreshControls();
}

async function stopRecording() {
  await busy('stop', [$('btn-stop')], async () => {
    try {
      await api('POST', '/api/recording/stop');
    } catch (e) {
      toast(errText(e), true);
    }
  });
}

function refreshControls() {
  const snap = S.snap;
  const recording = snap.state === 'recording';
  const starting = flight.has('start');
  $('btn-start').disabled = starting || recording || snap.final === 'running' || S.vpBusy || vpJob !== null;
  $('btn-stop').disabled = flight.has('stop') || !recording || snap.stopping;
  $('speakers').disabled = recording;
  $('live-toggle').disabled = recording;
  const vpBtn = $('vp-start');
  if (vpBtn) vpBtn.disabled = flight.has('vp') || recording || vpJob !== null || S.vpBusy;
  const canSum = Boolean(S.viewId) && S.hasTranscript && !meetingActive(S.viewId);
  $('summary-submit').disabled = flight.has('summary') || !canSum;
  $('ann-submit').disabled = flight.has('ann') || !S.viewId;
  $('ann-now').disabled = !(snap.state === 'recording');
  renderSummaryPrompt();
}

/** 发言归纳下方的提示：有请求结果/错误时显示它，否则按当前会议状态给出引导。 */
function renderSummaryPrompt() {
  const el = $('summary-prompt');
  if (S.summaryMsg) {
    el.textContent = S.summaryMsg;
    return;
  }
  const canSum = Boolean(S.viewId) && S.hasTranscript && !meetingActive(S.viewId);
  if (!S.viewId) el.textContent = '选择有文字稿的会议后可生成纪要（需在 config.toml 配置 [llm]）。';
  else if (!canSum) el.textContent = meetingActive(S.viewId) ? '会议录音或转写进行中，完成后才能生成纪要。' : '这场会议没有文字稿。';
  else el.textContent = '选择说话人（默认全部）后点击“生成纪要”。';
}

// ---------- 状态与计时 ----------
function applySnapshot(snap) {
  const prevId = S.snap.meeting_id;
  const prevState = S.snap.state;
  S.snap = Object.assign({}, S.snap, snap);
  S.timerBase = Number(S.snap.elapsed) || 0;
  S.timerAt = performance.now();
  if (S.snap.state === 'recording' && S.snap.meeting_id && S.snap.meeting_id !== prevId) {
    beginRecordingView(S.snap.meeting_id);
  } else if (S.snap.state === 'recording' && S.snap.meeting_id && S.viewId === null) {
    beginRecordingView(S.snap.meeting_id);
  }
  if (prevState === 'recording' && S.snap.state !== 'recording') {
    $('level-bar').style.width = '0%';
  }
  renderStatus();
  renderLiveStatus();
  renderHistory();
  refreshControls();
  updateTimer();
}

function renderStatus() {
  const snap = S.snap;
  const recKey = snap.state === 'recording' ? (snap.stopping ? 'stopping' : 'recording') : 'idle';
  const rec = $('rec-state');
  rec.textContent = REC_LABELS[recKey];
  rec.className = 'status-pill rec-' + recKey;
  const fin = Object.prototype.hasOwnProperty.call(FINAL_LABELS, snap.final) ? snap.final : 'idle';
  const pill = $('final-status');
  pill.textContent = FINAL_LABELS[fin];
  pill.className = 'status-pill final-' + fin;
  const note = $('final-note');
  if (fin === 'failed' && snap.error) {
    note.textContent = snap.error;
    note.hidden = false;
  } else {
    note.textContent = '';
    note.hidden = true;
  }
}

/** 实时转写状态行：让“准备中/不可用”永远可见，避免中栏一片空白让人以为坏了。 */
function renderLiveStatus() {
  const el = $('live-status');
  const snap = S.snap;
  const on = $('live-toggle').checked;
  const recording = snap.state === 'recording';
  let text = '';
  let warn = false;
  if (recording) {
    if (!snap.live) {
      text = '实时转写：本场未开启，只录音';
    } else if (snap.live_state === 'warming') {
      text = '实时转写：正在准备模型，稍后自动出字幕（录音不受影响）…';
    } else if (snap.live_state === 'failed') {
      text = '实时转写不可用：' + (snap.live_error || '模型加载失败') + '；录音继续，结束后仍会生成文字稿。';
      warn = true;
    } else {
      text = '实时转写：进行中，字幕出现在下方。';
    }
  } else if (!on) {
    text = '实时转写：未勾选，本场只录音（结束后生成文字稿）。';
  } else if (snap.live_state === 'warming') {
    text = '实时转写：正在后台准备模型（首次约 1 分钟），现在也可以开始录音。';
  } else if (snap.live_state === 'ready') {
    text = '实时转写：已就绪，开始录音后会实时显示字幕。';
  } else if (snap.live_state === 'failed') {
    text = '实时转写不可用：' + (snap.live_error || '模型加载失败') + '；录音和会后转写不受影响。';
    warn = true;
  } else if (snap.live_state === 'unavailable') {
    text = '实时转写：当前环境未启用。';
  } else {
    text = '实时转写：模型尚未预热，开始录音后可能需要等待一会儿。';
  }
  el.textContent = text;
  el.hidden = !text;
  el.classList.toggle('warn', warn);
}

/** 勾选状态默认开启，并用 localStorage 记住上次选择（隐私模式等取不到就沿用默认值）。 */
function initLiveToggle() {
  const el = $('live-toggle');
  try {
    const saved = localStorage.getItem('mr.live');
    if (saved === '1' || saved === '0') el.checked = saved === '1';
  } catch (e) { /* 无法读取时保留 HTML 默认的勾选 */ }
  el.addEventListener('change', () => {
    try { localStorage.setItem('mr.live', el.checked ? '1' : '0'); } catch (e) { /* 忽略 */ }
    renderLiveStatus();
  });
}

function updateTimer() {
  let secs = S.timerBase;
  if (S.snap.state === 'recording') secs += (performance.now() - S.timerAt) / 1000;
  $('timer').textContent = fmtTs(secs);
}

function currentElapsed() {
  let secs = S.timerBase;
  if (S.snap.state === 'recording') secs += (performance.now() - S.timerAt) / 1000;
  return Math.round(secs * 10) / 10;
}

// ---------- SSE ----------
let es = null;
function setConn(on) {
  const pill = $('conn');
  pill.textContent = on ? '已连接' : '连接中断，正在重连…';
  pill.className = 'status-pill ' + (on ? 'conn-on' : 'conn-off');
}

function parseData(e) {
  try {
    const v = JSON.parse(e.data);
    return v && typeof v === 'object' ? v : {};
  } catch (err) {
    return {};
  }
}

function connectEvents() {
  if (es) es.close();
  const url = S.lastSeq === null ? '/api/events' : '/api/events?last_seq=' + encodeURIComponent(S.lastSeq);
  es = new EventSource(url);
  es.onopen = () => {
    setConn(true);
  };
  const track = (e) => {
    const n = Number(e.lastEventId);
    if (Number.isInteger(n) && n >= 0) S.lastSeq = n;
  };
  es.addEventListener('state', (e) => {
    track(e);
    const d = parseData(e);
    const resync = d.resync === true;
    delete d.resync;
    applySnapshot(d);
    if (!S.everConnected && S.snap.state === 'recording' && S.snap.meeting_id) {
      selectMeeting(S.snap.meeting_id);   // 刷新页面时正在录音：补上已有的实时转写
    }
    if (resync && S.everConnected) {      // 断线期间可能错过事件：重新对一遍历史和当前会议
      loadHistory();
      if (S.viewId) selectMeeting(S.viewId);
    }
    S.everConnected = true;
  });
  es.addEventListener('level', (e) => {
    track(e);
    const rms = Number(parseData(e).rms) || 0;
    $('level-bar').style.width = Math.min(100, Math.round(rms * 400)) + '%';
  });
  es.addEventListener('live_text', (e) => {
    track(e);
    const d = parseData(e);
    if (typeof d.text !== 'string' || S.viewId === null || S.viewId !== S.snap.meeting_id) return;
    const raw = '[' + fmtTs(d.t) + '] ' + d.text;
    if (S.liveSeen.has(raw)) return;
    S.liveSeen.add(raw);
    S.lines.push(raw);
    renderTranscript();
  });
  es.addEventListener('final', (e) => {
    track(e);
    const d = parseData(e);
    loadHistory();
    if (d.status === 'completed') toast('转写完成');
    if (d.meeting_id && S.viewId === d.meeting_id) selectMeeting(d.meeting_id);
  });
  es.addEventListener('warning', (e) => {
    track(e);
    const d = parseData(e);
    if (d.message) toast(d.message, false);
  });
  es.addEventListener('error', (e) => {
    if (e.data === undefined) return;     // 连接层错误由 onerror 处理
    track(e);
    const d = parseData(e);
    if (d.message) toast(d.message, true);
  });
  es.onerror = () => {
    setConn(false);
    if (es && es.readyState === EventSource.CLOSED) {   // 浏览器放弃了自动重连：手动带 last_seq 重连
      setTimeout(connectEvents, SSE_RETRY_MS);
    }
  };
}

// ---------- 标注 ----------
function initKinds() {
  const box = $('ann-kinds');
  Object.entries(KINDS).forEach(([key, label], i) => {
    const input = h('input', { type: 'radio', name: 'ann-kind', value: key, checked: i === 0 });
    const wrap = h('label', { class: 'check' }, [input, ' ' + label]);
    box.append(wrap);
  });
}

function selectedKind() {
  const r = document.querySelector('input[name="ann-kind"]:checked');
  return r ? r.value : 'note';
}

function renderAnnotations() {
  const items = S.annotations.map((ann, i) => {
    const meta = h('span', { class: 'ann-meta' });
    meta.textContent = (KINDS[ann.kind] || ann.kind) + ' · ' + fmtTs(ann.t);
    const text = h('span', { class: 'ann-text' });
    text.textContent = ann.text;
    const btn = h('button', { type: 'button', class: 'ann-item', title: '点击定位到转写' }, [meta, text]);
    btn.addEventListener('click', () => locateAnnotation(ann));
    const li = h('li', {}, [btn]);
    li.dataset.ann = String(i);
    return li;
  });
  $('ann-list').replaceChildren(...items);
  $('ann-empty').hidden = items.length > 0;
}

function locateAnnotation(ann) {
  const idx = anchorMap();
  for (const [lineIndex, list] of idx) {
    if (list.includes(ann)) {
      selectLine(lineIndex);
      flashNode(lineNode(lineIndex));
      return;
    }
  }
  toast('转写中没有对应的位置', false);
}

async function submitAnnotation(ev) {
  ev.preventDefault();
  if (!S.viewId) {
    toast('请先选择一场会议', true);
    return;
  }
  const raw = $('ann-time').value.trim();
  const t = Number(raw);
  if (raw === '' || !Number.isFinite(t) || t < 0) {
    toast('时间必须是不小于 0 的数字（秒）', true);
    $('ann-time').focus();
    return;
  }
  const text = $('ann-text').value;
  if (!text.trim()) {
    toast('标注内容不能为空', true);
    $('ann-text').focus();
    return;
  }
  const id = S.viewId;
  await busy('ann', [$('ann-submit')], async () => {
    try {
      const rec = await api('POST', '/api/meetings/' + encodeURIComponent(id) + '/annotations',
        { kind: selectedKind(), t, text });
      if (S.viewId === id) {
        S.annotations.push(rec);
        S.annotations.sort((a, b) => a.t - b.t);
        $('ann-text').value = '';
        renderAnnotations();
        renderTranscript();
      }
      toast('已添加标注');
    } catch (e) {
      toast(errText(e), true);
    }
  });
}

// ---------- 发言归纳 ----------
function renderSpeakers() {
  const sel = $('summary-speaker');
  const opts = [h('option', { value: '', text: '全部说话人' })];
  for (const name of S.speakers) opts.push(h('option', { value: name, text: name }));
  sel.replaceChildren(...opts);
  sel.disabled = S.speakers.length === 0;
  const multi = $('summary-multi');
  const boxes = S.speakers.map((name) => {
    const input = h('input', { type: 'checkbox', value: name });
    return h('label', { class: 'check' }, [input, ' ' + name]);
  });
  multi.replaceChildren(...boxes);
}

function chosenSpeakers() {
  const checked = Array.from($('summary-multi').querySelectorAll('input:checked')).map((i) => i.value);
  if (checked.length) return checked;
  const one = $('summary-speaker').value;
  return one ? [one] : null;
}

async function submitSummary() {
  const id = S.viewId;
  if (!id) return;
  const speakers = chosenSpeakers();
  await busy('summary', [$('summary-submit')], async () => {
    S.summaryMsg = '正在生成纪要，请稍候…';
    renderSummaryPrompt();
    $('summary-result').textContent = '';
    try {
      const r = await api('POST', '/api/meetings/' + encodeURIComponent(id) + '/summary',
        speakers ? { speakers } : {});
      if (S.viewId === id) {
        $('summary-result').textContent = typeof r.summary === 'string' ? r.summary : '';
        S.summaryMsg = '已生成。';
      }
    } catch (e) {
      S.summaryMsg = errText(e);
      toast(errText(e), true);
    }
    renderSummaryPrompt();
  });
}

// ---------- 声纹登记 ----------
let vpJob = null;       // {id, finished}
let vpTimer = null;

async function loadVoiceprints() {
  try {
    const data = await api('GET', '/api/voiceprints');
    const list = Array.isArray(data.voiceprints) ? data.voiceprints : [];
    const items = list.map((p) => {
      const info = h('span', { class: 'hist-title' });
      info.textContent = p.name + (p.seconds ? '（有效语音 ' + Math.round(p.seconds) + ' 秒）' : '');
      const del = h('button', { type: 'button', class: 'small danger', 'aria-label': '删除声纹：' + p.name });
      del.textContent = '删除';
      del.addEventListener('click', () => deleteVoiceprint(p.name, del));
      return h('li', { class: 'hist-item' }, [info, del]);
    });
    $('vp-list').replaceChildren(...items);
    $('vp-empty').hidden = items.length > 0;
  } catch (e) {
    toast(errText(e), true);
  }
}

async function deleteVoiceprint(name, btn) {
  if (!window.confirm('确定删除声纹“' + name + '”？')) return;
  await busy('vp-del', [btn], async () => {
    try {
      await api('DELETE', '/api/voiceprints/' + encodeURIComponent(name));
      toast('已删除');
      await loadVoiceprints();
    } catch (e) {
      toast(errText(e), true);
    }
  });
}

function vpMessage(text) {
  const box = $('vp-message');
  box.textContent = text;
  box.hidden = !text;
}

async function startVoiceprint(ev) {
  ev.preventDefault();
  const name = $('vp-name').value.trim();
  if (!name) {
    toast('请填写姓名', true);
    $('vp-name').focus();
    return;
  }
  vpMessage('');
  await busy('vp', [$('vp-start')], async () => {
    try {
      const r = await api('POST', '/api/voiceprints/jobs',
        { kind: 'record', name, seconds: VP_SECONDS, replace: $('vp-replace').checked });
      openVoiceprintDialog(r.job_id);
    } catch (e) {
      vpMessage(errText(e));
      toast(errText(e), true);
    }
  });
}

function openVoiceprintDialog(jobId) {
  vpJob = { id: jobId, finished: false };
  $('vp-progress').value = 0;
  $('vp-progress-text').textContent = '正在录音，请开始朗读…';
  const dlg = $('voiceprint-dialog');
  if (typeof dlg.showModal === 'function' && !dlg.open) dlg.showModal();
  else dlg.setAttribute('open', '');
  clearTimeout(vpTimer);
  vpTimer = setTimeout(pollVoiceprint, VP_POLL_MS);
  refreshControls();
}

function closeVoiceprintDialog() {
  const dlg = $('voiceprint-dialog');
  if (dlg.open && typeof dlg.close === 'function') dlg.close();
  else dlg.removeAttribute('open');
}

async function pollVoiceprint() {
  const job = vpJob;
  if (!job || job.finished) return;
  try {
    const st = await api('GET', '/api/voiceprints/jobs/' + encodeURIComponent(job.id));
    if (vpJob !== job || job.finished) return;
    const secs = Math.min(VP_SECONDS, Number(st.progress) || 0);
    $('vp-progress').value = secs;
    if (st.state === 'completed') {
      finishVoiceprint(job, '声纹已登记。', false);
      return;
    }
    if (st.state === 'failed') {
      finishVoiceprint(job, st.error || '登记失败', true);
      return;
    }
    if (st.state === 'cancelled') {
      finishVoiceprint(job, '已取消。', false);
      return;
    }
    $('vp-progress-text').textContent = secs >= VP_SECONDS
      ? '录音完成，正在提取声纹…'
      : '已录 ' + Math.floor(secs) + ' / ' + VP_SECONDS + ' 秒';
  } catch (e) {
    if (vpJob !== job || job.finished) return;
    if (e.status === 404) {
      finishVoiceprint(job, '任务已失效，请重新登记', true);
      return;
    }
    $('vp-progress-text').textContent = errText(e);
  }
  vpTimer = setTimeout(pollVoiceprint, VP_POLL_MS);
}

/** 任务结束：先标记完成再关弹窗，这样关闭事件不会再去调用取消。 */
function finishVoiceprint(job, message, isError) {
  job.finished = true;
  clearTimeout(vpTimer);
  vpJob = null;
  closeVoiceprintDialog();
  vpMessage(message);
  toast(message, isError);
  loadVoiceprints();
  pollVpBusy();                         // 工作线程退出前后端仍算忙，轮询到空闲为止
  refreshControls();
}

/** 关闭按钮、Esc 或其他方式关弹窗：取消“这一次”录制。 */
async function cancelVoiceprint() {
  const job = vpJob;
  if (!job || job.finished) return;
  job.finished = true;
  clearTimeout(vpTimer);
  vpJob = null;
  closeVoiceprintDialog();
  vpMessage('正在取消…');
  try {
    const r = await api('POST', '/api/voiceprints/jobs/' + encodeURIComponent(job.id) + '/cancel');
    vpMessage(r && r.cancelled ? '已取消本次录制。' : '这次登记已经结束。');
    loadVoiceprints();
    pollVpBusy();
  } catch (e) {
    vpMessage(errText(e));
    toast(errText(e), true);
  }
  refreshControls();
}

/** 声纹任务占着麦克风时，隔一会儿问一次 /api/state，直到空闲，这样不用刷新页面就能恢复“开始录音”。 */
let vpBusyTimer = null;
async function pollVpBusy() {
  clearTimeout(vpBusyTimer);
  try {
    const st = await api('GET', '/api/state');
    S.vpBusy = Boolean(st.voiceprint_busy);
  } catch (e) {
    S.vpBusy = false;                   // 取不到状态时不要一直锁住按钮，真正冲突由后端 409 提示
  }
  refreshControls();
  if (S.vpBusy) vpBusyTimer = setTimeout(pollVpBusy, VP_BUSY_POLL_MS);
}

// ---------- 自检 ----------
const DOCTOR_STATUS = { ok: '正常', warn: '注意', fail: '故障' };

/** 六项自检：内容全部走 textContent，后端只给固定中文短句（不含路径）。 */
function renderDoctor(checks) {
  const items = checks.map((c) => {
    const li = h('li');
    const status = DOCTOR_STATUS[c.status] || '未知';
    li.textContent = String(c.name || '') + '：' + status + (c.detail ? '（' + c.detail + '）' : '');
    return li;
  });
  $('doctor-list').replaceChildren(...items);
}

async function loadDoctor() {
  const status = $('doctor-status');
  status.hidden = false;
  status.textContent = '正在检查…';
  try {
    const r = await api('GET', '/api/doctor');
    renderDoctor(Array.isArray(r.checks) ? r.checks : []);
    status.textContent = '检查完成';
  } catch (e) {
    status.textContent = errText(e);
  }
}

/** 测试大模型连接：只把后端给的固定中文原因显示出来，不显示地址或 Key。 */
async function testLlmConnection(btn) {
  const status = $('doctor-status');
  await busy('doctor-llm-test', [btn], async () => {
    try {
      const r = await api('POST', '/api/doctor/llm-test');
      const msg = (r.ok ? '连接正常：' : '连接失败：') + String(r.reason || '');
      status.hidden = false;
      status.textContent = msg;
      toast(msg, !r.ok);
    } catch (e) {
      status.hidden = false;
      status.textContent = errText(e);
      toast(errText(e), true);
    }
  });
}

// ---------- 初始化 ----------
async function loadState() {
  try {
    const st = await api('GET', '/api/state');
    S.vpBusy = Boolean(st.voiceprint_busy);
    applySnapshot(st);
    if (S.vpBusy) pollVpBusy();
  } catch (e) {
    toast(errText(e), true);
  }
}

function init() {
  initTabs();
  initKinds();
  initLiveToggle();
  renderSpeakers();
  $('btn-start').addEventListener('click', startRecording);
  $('btn-stop').addEventListener('click', stopRecording);
  $('history-refresh').addEventListener('click', loadHistory);
  bindHistoryActions();
  $('ann-form').addEventListener('submit', submitAnnotation);
  $('ann-now').addEventListener('click', () => { $('ann-time').value = String(currentElapsed()); });
  $('summary-submit').addEventListener('click', submitSummary);
  $('vp-form').addEventListener('submit', startVoiceprint);
  $('vp-refresh').addEventListener('click', loadVoiceprints);
  $('vp-cancel').addEventListener('click', cancelVoiceprint);
  $('doctor-refresh').addEventListener('click', loadDoctor);
  $('doctor-llm-test').addEventListener('click', (e) => testLlmConnection(e.currentTarget));
  const dlg = $('voiceprint-dialog');
  dlg.addEventListener('cancel', () => { cancelVoiceprint(); });     // Esc
  dlg.addEventListener('close', () => { cancelVoiceprint(); });      // 其他方式关闭
  setInterval(updateTimer, 250);
  renderTranscript();
  renderAnnotations();
  refreshControls();
  loadState().then(() => { connectEvents(); });
  loadHistory();
  loadDoctor();
}

document.addEventListener('DOMContentLoaded', init);
