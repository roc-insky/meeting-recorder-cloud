// 在 node 的 vm 里加载真实的 web/static/app.js（假 DOM），验证历史会议操作按钮的悬停/聚焦逻辑。
// 只输出一行 JSON 给 tests/test_web_ui.py 断言；不执行 init()，不依赖 jsdom 或浏览器。
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'web', 'static', 'app.js'), 'utf8');

// ---- 最小假 DOM：只做 bindHistoryActions 用到的那部分 ----
const timers = [];
let timerId = 0;

function makeClassList(initial) {
  const set = new Set((initial || '').split(' ').filter(Boolean));
  return {
    add: (c) => { set.add(c); },
    remove: (c) => { set.delete(c); },
    contains: (c) => set.has(c),
    toggle: (c, on) => { if (on) set.add(c); else set.delete(c); },
  };
}

function makeEl(cls) {
  const el = {
    classList: makeClassList(cls),
    handlers: {},
    children: [],
    addEventListener(type, fn) { el.handlers[type] = fn; },
    append(...c) { el.children.push(...c); },
    appendChild(c) { el.children.push(c); },
    replaceChildren(...c) { el.children = c; },
    contains(node) { return node === el; },
    setAttribute() {},
    getAttribute() { return null; },
    focus() {},
    querySelectorAll() { return []; },
  };
  return el;
}

const list = makeEl('history-list');
const documentStub = {
  getElementById: (id) => (id === 'history-list' ? list : makeEl(id)),
  createElement: (tag) => makeEl(tag),
  querySelectorAll: () => [],
  addEventListener: () => {},
  body: makeEl('body'),
};

const sandbox = {
  console,
  document: documentStub,
  window: {},
  location: { href: 'http://127.0.0.1/', hash: '' },
  setTimeout: (fn, ms) => { const id = ++timerId; timers.push({ id, fn, ms, done: false }); return id; },
  clearTimeout: (id) => { const t = timers.find((x) => x.id === id); if (t) t.cancelled = true; },
  setInterval: () => 0,
  clearInterval: () => {},
  fetch: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) }),
  EventSource: class { constructor() { this.readyState = 0; } addEventListener() {} close() {} },
  localStorage: { getItem: () => null, setItem: () => {} },
  URLSearchParams,
  encodeURIComponent,
  JSON,
  Math,
  Date,
  Promise,
  Set,
  Map,
  Intl,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: 'app.js' });

// 最后一个 setTimeout 就是悬停延迟定时器
function lastTimer() { return timers[timers.length - 1]; }
function fire() {
  const t = lastTimer();
  if (!t || t.cancelled) throw new Error('没有待触发的定时器');
  t.done = true;
  t.fn();
}

const out = {};
const li = makeEl('hist-item');
const inner = makeEl('span');
const outside = makeEl('li');
li.contains = (node) => node === li || node === inner;

sandbox.bindHistoryActions();

// ---- 1. 鼠标进入：150ms 前不得显示，150ms 后显示 ----
list.handlers.mouseover({ target: { closest: () => li }, relatedTarget: null });
out.delayMs = lastTimer().ms;
out.beforeDelay = li.classList.contains('show-actions');
fire();
out.afterDelay = li.classList.contains('show-actions');

// ---- 2. 行内移动不算重新进入：不得重开定时器 ----
const before = timers.length;
list.handlers.mouseover({ target: { closest: () => li }, relatedTarget: inner });
out.roamInsideOpenedTimer = timers.length !== before;

// ---- 3. 停在行内其它元素上不消失；移出整行立即消失 ----
list.handlers.mouseout({ target: { closest: () => li }, relatedTarget: inner });
out.keepInside = li.classList.contains('show-actions');
list.handlers.mouseout({ target: { closest: () => li }, relatedTarget: outside });
out.hideOnLeave = li.classList.contains('show-actions');

// ---- 4. 键盘聚焦：立即显示（不延迟），移出后隐藏 ----
li.classList.remove('show-actions');
const focusTarget = { closest: () => li, matches: (sel) => sel === ':focus-visible' };
list.handlers.focusin({ target: focusTarget });
out.focusShows = li.classList.contains('show-actions');
out.focusOpenedTimer = timers.length > before + 1;
list.handlers.focusout({ target: focusTarget, relatedTarget: outside });
out.focusHides = li.classList.contains('show-actions');

// ---- 5. 鼠标点击标题（:focus-visible 为假）不该把按钮留在屏幕上 ----
const clickFocus = { closest: () => li, matches: () => false };
list.handlers.focusin({ target: clickFocus });
out.clickFocusDoesNotShow = !li.classList.contains('show-actions');

// ---- 6. 定时器在隐藏前被取消：移出后 150ms 不得再出现 ----
li.classList.remove('show-actions');
list.handlers.mouseover({ target: { closest: () => li }, relatedTarget: null });
list.handlers.mouseout({ target: { closest: () => li }, relatedTarget: outside });
const pendingCount = timers.length;
out.cancelledTimerStaysHidden = !li.classList.contains('show-actions') && timers[pendingCount - 1].cancelled;

console.log(JSON.stringify(out));
