// 在 node 的 vm 里加载真实的 web/static/interview.js，用最小的假 DOM 复现“保存职位 A 时切换到 B”的竞态。
// 只输出一行 JSON 给 tests/test_web_ui.py 断言；不依赖 jsdom 或浏览器。
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'web', 'static', 'interview.js'), 'utf8');

const els = {};
function el(id) {
  if (!els[id]) {
    els[id] = {
      id, value: '', textContent: '', disabled: false, hidden: false, children: [],
      addEventListener() {}, replaceChildren(...c) { this.children = c; }, focus() {},
      setAttribute() {}, querySelectorAll() { return []; },
    };
  }
  return els[id];
}

const deferred = [];            // 每个 PUT/POST 请求一个，由测试手动完成
const calls = [];
const jobsOnServer = [
  { id: 'A', type: '前端', title: '职位A', requirements: 'ra' },
  { id: 'B', type: '后端', title: '职位B', requirements: 'rb' },
];

const sandbox = {
  console,
  document: { addEventListener() {} },
  window: { confirm: () => true },
  flight: new Set(),
  $: el,
  h: (tag, props) => Object.assign(el('h-' + Math.random()), { tag, props }),
  toast: () => {},
  errText: (e) => String(e && e.message || e),
  busy: async (key, _btns, fn) => {
    sandbox.flight.add(key);
    try { return await fn(); } finally { sandbox.flight.delete(key); }
  },
  api: (method, url, body) => {
    calls.push({ method, url, body });
    if (method === 'GET') return Promise.resolve({ jobs: jobsOnServer.map((j) => ({ ...j })) });
    return new Promise((resolve) => deferred.push({ method, url, body, resolve }));
  },
};
vm.createContext(sandbox);
vm.runInContext(src + '\n;globalThis.__J = J; globalThis.__fns = { editJob, saveJob, resetJobForm, deleteJob, loadJobs, renderJobs };', sandbox);
const { editJob, saveJob, resetJobForm, deleteJob, loadJobs } = sandbox.__fns;
const J = sandbox.__J;
const tick = () => new Promise((r) => setTimeout(r, 0));

(async () => {
  const out = {};
  await loadJobs();
  // ---- 场景 1：编辑 A 并保存（请求未返回），切换到 B，再让 A 的响应回来 ----
  editJob('A');
  el('job-title').value = '职位A-改';
  const p = saveJob({ preventDefault() {} });
  await tick();
  editJob('B');                                   // 保存期间选中 B
  deferred[0].resolve({ id: 'A', type: '前端', title: '职位A-改', requirements: 'ra' });
  await p;
  out.afterSwitch = { currentId: J.currentId, title: el('job-title').value, req: el('job-req').value };
  // 切换后再保存，必须是对 B 的 PUT，而不是覆盖 A
  const p2 = saveJob({ preventDefault() {} });
  await tick();
  out.secondSave = calls.filter((c) => c.method !== 'GET').slice(-1)[0];
  deferred[1].resolve({ id: 'B', type: '后端', title: '职位B', requirements: 'rb' });
  await p2;
  // ---- 场景 2：新建职位保存中，点“新建”重置并开始填另一份，旧响应不得抢走选择 ----
  resetJobForm();
  el('job-title').value = '新职位X';
  const p3 = saveJob({ preventDefault() {} });
  await tick();
  editJob('A');
  deferred[2].resolve({ id: 'NEW1', type: '前端', title: '新职位X', requirements: '' });
  await p3;
  out.afterCreateSwitch = { currentId: J.currentId, title: el('job-title').value };
  // ---- 场景 3：选择未变时，保存响应仍然把新建的职位设为当前 ----
  resetJobForm();
  el('job-title').value = '新职位Y';
  const p4 = saveJob({ preventDefault() {} });
  await tick();
  deferred[3].resolve({ id: 'NEW2', type: '前端', title: '新职位Y', requirements: '' });
  await p4;
  out.afterCreateSame = { currentId: J.currentId };
  // ---- 场景 4：删除 A 的请求未返回时切到 B，响应回来后不得重置 B 的表单 ----
  editJob('A');
  const p5 = deleteJob();
  await tick();
  editJob('B');
  deferred[4].resolve({});
  await p5;
  out.afterDeleteSwitch = { currentId: J.currentId, title: el('job-title').value };
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
