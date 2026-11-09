/* 面试帮助页：职位库、简历（文字 + 粘贴/选择图片）、面试题生成。
 * 依赖 app.js 里的 $ / h / api / busy / toast / errText；依赖 marked.min.js 与 purify.min.js。
 * 这里是整个前端唯一允许写入 HTML 的地方，且只接受 DOMPurify 净化后的结果。
 */
'use strict';

const JOB_TYPES = ['前端', '后端', '客户端', '测试'];
const MAX_IMAGES = 5;
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];
// 面试题只需要这些排版标签；不允许图片、脚本、样式、表单等
const SANITIZE_OPTIONS = {
  ALLOWED_TAGS: ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br', 'hr', 'strong', 'em', 'b', 'i', 'del', 'code', 'pre',
    'blockquote', 'ul', 'ol', 'li', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a'],
  ALLOWED_ATTR: ['href'],
  ALLOWED_URI_REGEXP: /^(?:https?:|mailto:)/i,
  ALLOW_DATA_ATTR: false,
};

const J = {
  jobs: [],
  currentId: null,      // 当前编辑的职位 id；null 表示新建
  images: [],           // [{dataUri, name}]
  statusMsg: '',
  loaded: false,
};

/** 把 Markdown 渲染进容器：marked.parse → DOMPurify.sanitize（白名单）→ 写入 HTML。 */
function renderMarkdown(box, markdown) {
  if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
    box.textContent = markdown;      // 库没加载时退回纯文本，绝不直接插入未净化内容
    return;
  }
  box.innerHTML = DOMPurify.sanitize(marked.parse(String(markdown), { async: false }), SANITIZE_OPTIONS);
  for (const a of box.querySelectorAll('a')) {
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
  }
}

function refreshInterviewControls() {
  const saved = J.currentId !== null;
  const jobBusy = flight.has('job');
  $('job-save').disabled = jobBusy;
  $('job-delete').disabled = jobBusy || !saved;
  $('q-generate').disabled = flight.has('questions') || !saved;
  $('job-new').disabled = jobBusy;
  const status = $('q-status');
  if (!flight.has('questions') && !J.statusMsg) {
    status.textContent = saved ? '填写简历（文字或图片，可留空）后点击“生成面试题”。' : '请先在左侧选择一个已保存的职位。';
  }
}

// ---------- 职位 ----------
function initJobTypes() {
  $('job-type').replaceChildren(...JOB_TYPES.map((t) => h('option', { value: t, text: t })));
}

async function loadJobs() {
  try {
    const data = await api('GET', '/api/jobs');
    J.jobs = Array.isArray(data.jobs) ? data.jobs : [];
    J.loaded = true;
    renderJobs();
  } catch (e) {
    toast(errText(e), true);
  }
}

function renderJobs() {
  const items = J.jobs.map((job) => {
    const btn = h('button', { type: 'button', class: 'hist-title', title: job.title });
    btn.textContent = '[' + job.type + '] ' + job.title;
    btn.addEventListener('click', () => editJob(job.id));
    return h('li', { class: 'hist-item' + (job.id === J.currentId ? ' current' : '') }, [btn]);
  });
  $('job-list').replaceChildren(...items);
  $('job-empty').hidden = items.length > 0;
}

function resetJobForm() {
  J.currentId = null;
  J.statusMsg = '';
  $('job-form-title').textContent = '新建职位';
  $('job-type').value = JOB_TYPES[0];
  $('job-title').value = '';
  $('job-req').value = '';
  $('q-result').textContent = '';
  renderJobs();
  refreshInterviewControls();
}

function editJob(id) {
  const job = J.jobs.find((j) => j.id === id);
  if (!job) return;
  J.currentId = id;
  J.statusMsg = '';
  $('job-form-title').textContent = '编辑职位';
  $('job-type').value = job.type;
  $('job-title').value = job.title;
  $('job-req').value = job.requirements || '';
  $('q-result').textContent = '';
  renderJobs();
  refreshInterviewControls();
}

async function saveJob(ev) {
  ev.preventDefault();
  const body = { type: $('job-type').value, title: $('job-title').value, requirements: $('job-req').value };
  if (!body.title.trim()) {
    toast('职位名称不能为空', true);
    $('job-title').focus();
    return;
  }
  await busy('job', [$('job-save')], async () => {
    try {
      let saved;
      if (J.currentId === null) saved = await api('POST', '/api/jobs', body);
      else saved = await api('PUT', '/api/jobs/' + encodeURIComponent(J.currentId), body);
      J.currentId = saved.id;
      $('job-form-title').textContent = '编辑职位';
      toast('职位已保存');
      await loadJobs();
    } catch (e) {
      toast(errText(e), true);
    }
  });
}

async function deleteJob() {
  const id = J.currentId;
  if (id === null) return;
  const job = J.jobs.find((j) => j.id === id);
  if (!window.confirm('确定删除职位“' + (job ? job.title : '') + '”？')) return;
  await busy('job', [$('job-delete')], async () => {
    try {
      await api('DELETE', '/api/jobs/' + encodeURIComponent(id));
      toast('职位已删除');
      resetJobForm();
      await loadJobs();
    } catch (e) {
      toast(errText(e), true);
    }
  });
}

// ---------- 简历图片 ----------
function readAsDataUri(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error('读取图片失败'));
    reader.readAsDataURL(file);
  });
}

async function addImageFiles(files) {
  for (const file of files) {
    if (J.images.length >= MAX_IMAGES) {
      toast('最多 ' + MAX_IMAGES + ' 张图片', true);
      break;
    }
    if (!IMAGE_TYPES.includes(file.type)) {
      toast('只支持 png、jpeg、webp、gif 图片', true);
      continue;
    }
    if (file.size > MAX_IMAGE_BYTES) {
      toast('单张图片不能超过 5MB', true);
      continue;
    }
    try {
      J.images.push({ dataUri: await readAsDataUri(file), name: file.name || '粘贴的截图' });
    } catch (e) {
      toast(errText(e), true);
    }
  }
  renderImages();
}

function renderImages() {
  const items = J.images.map((img, i) => {
    const pic = h('img', { alt: img.name });
    pic.src = img.dataUri;
    const rm = h('button', { type: 'button', 'aria-label': '移除图片：' + img.name });
    rm.textContent = '×';
    rm.addEventListener('click', () => {
      J.images.splice(i, 1);
      renderImages();
    });
    return h('li', {}, [pic, rm]);
  });
  $('resume-images').replaceChildren(...items);
}

function onResumePaste(ev) {
  const files = [];
  for (const item of (ev.clipboardData && ev.clipboardData.items) || []) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const f = item.getAsFile();
      if (f) files.push(f);
    }
  }
  if (files.length) {
    ev.preventDefault();       // 有图片就不再粘贴文字，避免把文件名之类混进简历
    addImageFiles(files);
  }
}

function clearResume() {
  $('resume-text').value = '';
  J.images = [];
  $('resume-file').value = '';
  renderImages();
}

// ---------- 生成面试题 ----------
async function generateQuestions() {
  const id = J.currentId;
  if (id === null) return;
  const body = { resume_text: $('resume-text').value };
  if (J.images.length) body.images = J.images.map((i) => i.dataUri);
  const status = $('q-status');
  await busy('questions', [$('q-generate')], async () => {
    J.statusMsg = '正在生成面试题，图片较多时可能需要一两分钟…';
    status.textContent = J.statusMsg;
    $('q-result').textContent = '';
    try {
      const r = await api('POST', '/api/jobs/' + encodeURIComponent(id) + '/questions', body);
      if (J.currentId === id) {
        renderMarkdown($('q-result'), typeof r.questions === 'string' ? r.questions : '');
        J.statusMsg = '已生成（简历与结果不会保存）。';
      }
    } catch (e) {
      J.statusMsg = errText(e);
      toast(errText(e), true);
    }
    status.textContent = J.statusMsg;
  });
}

function initInterview() {
  initJobTypes();
  $('job-new').addEventListener('click', resetJobForm);
  $('job-form').addEventListener('submit', saveJob);
  $('job-delete').addEventListener('click', deleteJob);
  $('resume-text').addEventListener('paste', onResumePaste);
  $('resume-file').addEventListener('change', (ev) => {
    addImageFiles(Array.from(ev.target.files || []));
    ev.target.value = '';
  });
  $('resume-clear').addEventListener('click', clearResume);
  $('q-generate').addEventListener('click', generateQuestions);
  document.addEventListener('mr:tab', (ev) => {
    if (ev.detail === 'interview' && !J.loaded) loadJobs();
  });
  refreshInterviewControls();
}

document.addEventListener('DOMContentLoaded', initInterview);
