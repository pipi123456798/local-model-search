import { ModelViewer, Thumbnails } from '/static/viewer.bundle.js';

const $ = id => document.getElementById(id);
const state = { token: '', native: false, libraries: [], selected: new Set(), query: null,
  task: null, busy: false, page: 'search', ready: false, browse: null, browseRows: [], visible: true };
let queryViewer, detailViewer, thumbnails, pollTimer, toastTimer, retryAction, folderGrant;
let browseEpoch = 0, initialized = false, currentDetail, lastPreview = '';
let loadingPreview = '', queryPreviewEpoch = 0, lastResult = null, restored = false;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function toast(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 6000);
}
function action(fn) {
  return (...args) => {
    try { return Promise.resolve(fn(...args)).catch(error => toast(error.message)); }
    catch (error) { toast(error.message); }
  };
}
function bridge(command, data = {}) {
  const message = JSON.stringify({ command, ...data });
  if (window.wx?.postMessage) window.wx.postMessage(message);
  else if (window.chrome?.webview) window.chrome.webview.postMessage(message);
  else throw new Error('此操作需要在 Bambu Studio 内使用');
}
async function api(path, method = 'GET', body) {
  const headers = { 'X-Search-Token': state.token };
  if (body && !(body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch(path, { method, headers, signal: controller.signal,
      body: body ? (body instanceof FormData ? body : JSON.stringify(body)) : undefined });
    const data = await response.json();
    if (!response.ok) throw Object.assign(new Error(data.error || '本地服务返回错误'), { code: data.code });
    return data;
  } catch (error) {
    if (error.name === 'AbortError' || error instanceof TypeError) throw new Error('本地服务连接中断或超时，请重试');
    throw error;
  } finally { clearTimeout(timeout); }
}
function setTheme(theme) {
  document.documentElement.dataset.theme = theme || (navigator.userAgent.includes('(dark)') ? 'dark' : 'light');
}
function updateButtons() {
  $('search').disabled = !state.ready || !state.query || !state.selected.size || state.busy;
  for (const id of ['new-library', 'first-library', 'create-submit']) $(id).disabled = !state.ready || state.busy;
  $('replace-file').disabled = state.busy;
  $('detail-similar').disabled = !state.ready || state.busy;
}
function notice(message, error = false) {
  const box = $('connection');
  box.replaceChildren(document.createTextNode(message));
  box.hidden = !message;
  box.classList.toggle('error', error);
  if (error) {
    const retry = element('button', 'text-button connection-action', '重新连接');
    retry.onclick = action(connect);
    box.append(retry);
  }
}
async function connect() {
  try {
    const health = await api('/api/health');
    state.ready = health.ready;
    notice(health.ready ? '' : health.errors.join('；'), !health.ready);
    await loadLibraries();
    if (!restored) { restored = true; await restoreSession(); }
    if (state.task) pollTask();
  } catch (error) {
    state.ready = false;
    notice(error.message, true);
  }
  updateButtons();
}
function switchPage(page) {
  state.page = page;
  $('search-page').hidden = page !== 'search';
  $('libraries-page').hidden = page !== 'libraries';
  for (const name of ['search', 'libraries']) {
    $(`tab-${name}`).classList.toggle('active', page === name);
    $(`tab-${name}`).setAttribute('aria-selected', String(page === name));
  }
  queryViewer?.setVisible(page === 'search');
  thumbnails?.cancel();
  if (page === 'search') refreshThumbnails($('results'));
  else refreshThumbnails($('browse-models'));
}
async function loadLibraries() {
  const data = await api('/api/libraries');
  const previous = new Set(state.libraries.map(lib => lib.id));
  state.libraries = data.libraries;
  for (const lib of state.libraries) if (!previous.has(lib.id)) state.selected.add(lib.id);
  state.selected = new Set([...state.selected].filter(id => state.libraries.some(lib => lib.id === id)));
  $('library-count').textContent = state.libraries.length;
  $('library-options').replaceChildren();
  $('library-list').replaceChildren();
  $('libraries-empty').hidden = state.libraries.length > 0;
  $('empty-create').hidden = state.libraries.length > 0;
  if (!state.libraries.length) $('library-options').append(element('span', 'subtle', '尚未建立检索库'));
  for (const lib of state.libraries) {
    const label = element('label', 'library-option');
    const input = element('input'); input.type = 'checkbox'; input.checked = state.selected.has(lib.id);
    input.onchange = () => { input.checked ? state.selected.add(lib.id) : state.selected.delete(lib.id); updateButtons(); };
    label.append(input, element('span', 'lib-label', lib.name), element('small', '', lib.count));
    $('library-options').append(label);
    const card = element('article', 'library-card panel');
    const source = element('p', 'source', lib.source); source.title = lib.source;
    const stats = element('div', 'library-stats'); stats.append(element('strong', '', lib.count), element('span', '', '个模型'));
    const actions = element('div', 'library-actions');
    const browse = element('button', 'secondary', '浏览模型'); browse.onclick = action(() => browseLibrary(lib));
    const refresh = element('button', 'secondary', '刷新索引'); refresh.onclick = action(() => refreshLibrary(lib));
    const remove = element('button', 'quiet', '移除'); remove.onclick = () => confirmDelete(lib);
    actions.append(browse, refresh, remove);
    card.append(element('div', 'library-icon', '▦'), element('h3', '', lib.name), source, stats,
      element('p', 'library-date', `更新于 ${new Date(lib.updated_at * 1000).toLocaleString()}${lib.failed ? ` · ${lib.failed} 个文件失败` : ''}`), actions);
    $('library-list').append(card);
  }
  updateButtons();
}
function ensureIdle() { if (state.busy) throw new Error('请等待当前任务完成，或先取消任务'); }
function openCreate() { ensureIdle(); folderGrant = null; $('folder-path').textContent = '未选择文件夹'; $('create-dialog').showModal(); }
async function submit(operation) {
  ensureIdle(); state.busy = true; updateButtons();
  $('dismiss-task').hidden = true; $('cancel-task').hidden = true;
  $('task-message').textContent = '正在提交任务…'; rememberSession();
  try { const task = await operation(); watch(task); return task; }
  catch (error) {
    state.busy = false; $('dismiss-task').hidden = false;
    $('task-message').textContent = error.message; updateButtons(); throw error;
  }
}
async function refreshLibrary(lib) {
  retryAction = () => refreshLibrary(lib);
  await submit(() => api(`/api/libraries/${lib.id}/refresh`, 'POST', {}));
}
function confirmDelete(lib) {
  if (state.busy) return toast('请先等待或取消当前任务');
  $('confirm-text').textContent = `将从本地检索中移除“${lib.name}”（${lib.count} 个模型）。`;
  $('confirm-dialog').showModal();
  $('confirm-yes').onclick = action(async () => {
    await api(`/api/libraries/${lib.id}`, 'DELETE');
    $('confirm-dialog').close();
    $('library-browser').hidden = true;
    await loadLibraries();
    toast('检索库已移除，原始文件保留');
  });
}
function modelURL(row) { return `/api/models/${row.library}/${row.id}/preview`; }
function refreshThumbnails(container) {
  if (!state.visible || document.hidden || !container.children.length) return;
  try {
    thumbnails ||= new Thumbnails();
    thumbnails.fill([...container.querySelectorAll('img[data-model-url]')].filter(img => !img.classList.contains('loaded'))
      .map(img => ({ img, url: img.dataset.modelUrl })), state.token);
  } catch { toast('当前环境无法启用 WebGL，仍可查看检索结果与来源'); }
}
function renderCards(rows, container, append = false) {
  if (!append) container.replaceChildren();
  for (const row of rows) {
    const card = element('button', 'model-card'); card.title = row.name;
    const image = element('div', 'card-image');
    const img = element('img'); img.alt = row.name; img.dataset.modelUrl = modelURL(row);
    image.append(img, element('span', 'rank', row.rank ? `#${String(row.rank).padStart(2, '0')}` : row.category));
    const info = element('div', 'card-info');
    const footer = element('div', 'card-footer');
    footer.append(element('span', '', row.similarity === undefined ? '三维预览' : '余弦相似度'),
      element('strong', '', row.similarity === undefined ? '↗' : row.similarity.toFixed(4)));
    info.append(element('div', 'card-name', row.name), element('div', 'card-meta', `${row.library_name} / ${row.category}`), footer);
    card.append(image, info); card.onclick = action(() => showDetail(row)); container.append(card);
  }
  refreshThumbnails(container);
}
async function browseLibrary(lib, more = false) {
  const epoch = ++browseEpoch;
  if (!more) { state.browse = lib; state.browseRows = []; }
  const data = await api(`/api/libraries/${lib.id}/models?offset=${state.browseRows.length}&limit=24`);
  if (epoch !== browseEpoch) return;
  state.browseRows.push(...data.models);
  $('browse-title').textContent = `${lib.name} · ${data.total} 个模型`;
  $('library-browser').hidden = false;
  renderCards(data.models, $('browse-models'), more);
  $('browse-more').hidden = state.browseRows.length >= data.total;
}
async function showQueryPreview(url) {
  if (!url || lastPreview === url || loadingPreview === url) return;
  const epoch = ++queryPreviewEpoch; loadingPreview = url;
  $('dropzone').hidden = true; $('query-model').hidden = false;
  try {
    queryViewer ||= new ModelViewer($('query-viewer'));
    await queryViewer.load(url, state.token);
    if (epoch === queryPreviewEpoch) lastPreview = url;
  } catch (error) { if (error.name !== 'AbortError') toast(error.message); }
  finally { if (epoch === queryPreviewEpoch) loadingPreview = ''; }
}
function setQuery(query) {
  ensureIdle();
  state.query = query; lastResult = null;
  queryViewer?.clear(); lastPreview = ''; loadingPreview = ''; ++queryPreviewEpoch;
  $('query-name').textContent = query.name;
  $('dropzone').hidden = true; $('query-model').hidden = false;
  $('results').replaceChildren(); $('results-empty').hidden = false; $('result-count').hidden = true;
  $('results-empty').querySelector('h3').textContent = '模型已就绪';
  $('results-empty').querySelector('p').textContent = '选择检索库后点击“查找相似模型”，预览会在模型转换后显示。';
  updateButtons();
}
function chooseFile() {
  ensureIdle();
  if (state.native) bridge('local_search_choose_file');
  else $('file-input').click();
}
function acceptFile(file) {
  if (!file) return;
  if (!/\.(step|stp|stl|obj|ply|glb|gltf|3mf|off)$/i.test(file.name)) throw new Error('不支持的模型格式');
  if (file.size > 256 * 1024 * 1024) throw new Error('模型超过 256 MB 限制');
  setQuery({ file, name: file.name });
}
function options() {
  if (!state.selected.size) throw new Error('请至少选择一个检索库');
  return { libraries: [...state.selected], keywords: $('keywords').value.trim(), topk: Number($('topk').value) };
}
async function search() {
  ensureIdle();
  if (!state.query) throw new Error('请先选择查询模型');
  const opts = options();
  const query = state.query;
  retryAction = search; switchPage('search');
  await submit(() => {
    if (query.model) return api('/api/similar', 'POST', { ...opts, library: query.library, model: query.model });
    if (query.cached) return api('/api/search_cached', 'POST', { ...opts, query: query.cached });
    if (query.grant) return api('/api/search', 'POST', { ...opts, grant: query.grant });
    const form = new FormData(); form.append('file', query.file);
    for (const [key, value] of Object.entries(opts)) form.append(key, Array.isArray(value) ? JSON.stringify(value) : value);
    return api('/api/search', 'POST', form);
  });
}
function watch(task) {
  clearTimeout(pollTimer); state.task = task.id; state.busy = true;
  sessionStorage.setItem('local-search-task', task.id);
  $('task-panel').hidden = false; $('retry-task').hidden = true;
  $('dismiss-task').hidden = true; $('cancel-task').hidden = false;
  $('task-message').textContent = '任务已提交，等待处理…'; $('task-count').textContent = '';
  $('task-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  updateButtons(); pollTask();
}
async function pollTask() {
  clearTimeout(pollTimer);
  if (!state.task) return;
  const tid = state.task;
  try {
    const task = await api(`/api/tasks/${tid}`);
    if (tid !== state.task) return;
    const finished = ['done', 'error', 'cancelled'].includes(task.status);
    state.busy = !finished;
    $('task-panel').hidden = false;
    $('task-kind').textContent = task.kind === 'build' ? '建立索引' : '模型检索';
    $('task-title').textContent = task.label;
    $('task-message').textContent = task.message;
    $('task-count').textContent = task.total ? `${task.done}/${task.total} · 成功 ${task.success} · 失败 ${task.failed} · 缓存 ${task.reused || 0}` : '';
    $('task-progress').classList.toggle('indeterminate', !finished && !task.total);
    $('task-progress').style.width = finished ? '100%' : task.total ? `${100 * task.done / task.total}%` : '';
    $('cancel-task').hidden = finished; $('dismiss-task').hidden = !finished;
    $('task-errors').hidden = !task.failures?.length;
    $('task-error-text').textContent = (task.failures || []).map(item => `${item.file}: ${item.error}`).join('\n');
    if (task.preview && task.kind === 'search') { $('query-name').textContent = task.query_name; showQueryPreview(task.preview); }
    if (finished) {
      state.task = null; sessionStorage.removeItem('local-search-task');
      $('retry-task').hidden = task.status === 'done' || !retryAction;
      if (task.status === 'done') {
        if (task.kind === 'build') { await loadLibraries(); toast(`索引已更新：${task.result.library.count} 个模型`); }
        else { displayResults(task.result); rememberSession(); }
      }
    } else pollTimer = setTimeout(pollTask, 800);
    updateButtons();
  } catch (error) {
    if (tid !== state.task) return;
    if (error.code === 'not_found' || error.code === 'unauthorized') {
      state.task = null; state.busy = false; sessionStorage.removeItem('local-search-task');
      notice(error.message + '；请重新执行任务。', true); updateButtons();
    } else {
      notice(error.message + '；正在尝试恢复任务状态。', true);
      pollTimer = setTimeout(pollTask, 4000);
    }
  }
}
function displayResults(result) {
  lastResult = result;
  const { results, preview, query_name, elapsed } = result;
  if (result.query) state.query = { cached: result.query, name: query_name };
  $('query-name').textContent = query_name; showQueryPreview(preview);
  $('results-empty').hidden = results.length > 0;
  $('result-count').hidden = false; $('result-count').textContent = `${results.length} 个候选`;
  $('results-subtitle').textContent = `按余弦相似度排序${elapsed !== undefined ? ` · 用时 ${elapsed} 秒` : ' · 库内快速检索'}`;
  if (!results.length) {
    $('results-empty').querySelector('h3').textContent = '当前范围内没有候选模型';
    $('results-empty').querySelector('p').textContent = '试试移除关键词，或选择其他检索库。';
  }
  renderCards(results, $('results'));
}
function rememberSession() {
  try {
    const query = state.query?.file ? null : state.query;
    sessionStorage.setItem('local-search-state', JSON.stringify({ query, result: lastResult,
      selected: [...state.selected], keywords: $('keywords').value, topk: $('topk').value, page: state.page }));
  } catch { /* 会话存储不可用时仍可继续检索。 */ }
}
async function restoreSession() {
  try {
    const saved = JSON.parse(sessionStorage.getItem('local-search-state') || 'null');
    if (!saved) return;
    state.selected = new Set(saved.selected.filter(id => state.libraries.some(lib => lib.id === id)));
    $('keywords').value = saved.keywords || ''; $('topk').value = saved.topk || '10';
    if (saved.query) {
      state.query = saved.query; $('query-name').textContent = saved.query.name;
      $('dropzone').hidden = true; $('query-model').hidden = false;
    }
    await loadLibraries();
    if (saved.result) displayResults(saved.result);
    switchPage(saved.page === 'libraries' ? 'libraries' : 'search');
  } catch { sessionStorage.removeItem('local-search-state'); }
}
async function showDetail(row) {
  currentDetail = row;
  $('detail-name').textContent = row.name;
  $('detail-score').textContent = row.similarity === undefined ? '—' : row.similarity.toFixed(4);
  $('detail-meta').replaceChildren();
  const data = { '检索库': row.library_name, '类别': row.category,
    '尺寸': (row.dimensions || []).map(v => v.toFixed(2)).join(' × ') + '（源单位）', '来源': row.path };
  for (const [key, value] of Object.entries(data)) $('detail-meta').append(element('dt', '', key), element('dd', '', value));
  const slot = $('compare-query-slot');
  slot.hidden = !lastPreview;
  if (lastPreview) { slot.append($('query-viewer').parentElement); queryViewer?.setVisible(true); }
  $('detail-edit-options').hidden = true; $('detail-program-options').hidden = true;
  $('detail-dialog').showModal();
  detailViewer ||= new ModelViewer($('detail-viewer'));
  detailViewer.setVisible(true); queryViewer?.resize();
  await detailViewer.load(modelURL(row), state.token);
}

$('tab-search').onclick = () => switchPage('search');
$('tab-libraries').onclick = action(async () => { switchPage('libraries'); await loadLibraries(); });
for (const id of ['new-library', 'first-library', 'empty-create']) $(id).onclick = action(openCreate);
$('choose-folder').onclick = action(() => bridge('local_search_choose_directory'));
$('settings').onclick = action(() => bridge('local_search_settings'));
$('create-form').onsubmit = action(async event => {
  event.preventDefault(); ensureIdle();
  if (!folderGrant) throw new Error('请先选择本地模型文件夹');
  const body = { name: $('library-name').value.trim(), grant: folderGrant };
  retryAction = () => submit(() => api('/api/libraries', 'POST', body));
  await submit(() => api('/api/libraries', 'POST', body));
  folderGrant = null; $('create-dialog').close(); switchPage('libraries');
});
$('select-all').onclick = () => { state.selected = new Set(state.libraries.map(lib => lib.id)); loadLibraries().catch(e => toast(e.message)); };
$('dropzone').onclick = action(chooseFile);
$('dropzone').onkeydown = action(event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); chooseFile(); } });
$('replace-file').onclick = action(chooseFile);
$('file-input').onchange = action(event => { acceptFile(event.target.files[0]); event.target.value = ''; });
for (const eventName of ['dragover', 'dragleave', 'drop']) document.addEventListener(eventName, event => {
  event.preventDefault();
  $('dropzone').classList.toggle('dragging', eventName === 'dragover');
  if (eventName === 'drop') { try { acceptFile(event.dataTransfer.files[0]); } catch (error) { toast(error.message); } }
});
$('search').onclick = action(search);
$('keywords').onkeydown = action(event => { if (event.key === 'Enter') awaitSearch(); });
function awaitSearch() { search().catch(error => toast(error.message)); }
$('cancel-task').onclick = action(() => state.task && api(`/api/tasks/${state.task}/cancel`, 'POST', {}));
$('dismiss-task').onclick = () => { $('task-panel').hidden = true; };
$('retry-task').onclick = action(() => retryAction?.());
$('browse-more').onclick = action(() => browseLibrary(state.browse, true));
$('close-browser').onclick = () => { ++browseEpoch; $('library-browser').hidden = true; thumbnails?.cancel(); };
$('reset-query').onclick = () => queryViewer?.reset(); $('reset-detail').onclick = () => detailViewer?.reset();
$('confirm-no').onclick = () => $('confirm-dialog').close();
for (const button of document.querySelectorAll('.close-dialog')) button.onclick = () => button.closest('dialog').close();
$('detail-dialog').addEventListener('close', () => {
  const wrap = $('query-viewer').parentElement;
  if ($('compare-query-slot').contains(wrap)) $('query-model').prepend(wrap);
  queryViewer?.setVisible(state.page === 'search'); detailViewer?.clear(); detailViewer?.setVisible(false);
  $('detail-edit-options').hidden = true; $('detail-program-options').hidden = true;
});
$('detail-similar').onclick = action(async () => {
  const row = currentDetail; ensureIdle(); $('detail-dialog').close();
  setQuery({ name: row.name, model: row.id, library: row.library });
  showQueryPreview(modelURL(row)); await search();
});
$('detail-reveal').onclick = action(async () => {
  const ticket = await api(`/api/models/${currentDetail.library}/${currentDetail.id}/reveal`, 'POST', {});
  bridge('local_search_reveal', ticket);
});
$('detail-edit').onclick = action(() => {
  const panel = $('detail-edit-options');
  $('detail-program-options').hidden = true;
  panel.hidden = !panel.hidden;
});
function renderProgramChooser(result) {
  const panel = $('detail-program-options');
  panel.replaceChildren(element('span', 'subtle', '选择打开程序 · 本机已安装'));
  const choices = result.choices.slice();
  if (!choices.some(choice => choice.default)) {
    choices.push({ name: '系统默认程序', path: '', hint: '交给系统默认关联程序打开', default: true });
  }
  for (const choice of choices) {
    const button = element('button', 'edit-option');
    button.type = 'button';
    const strong = element('strong', '', choice.name);
    if (choice.default) strong.append(element('em', '', '系统默认'));
    button.append(strong, element('span', '', choice.hint || choice.path));
    button.onclick = action(async () => {
      button.disabled = true;
      try {
        bridge('local_search_edit', { ticket: result.ticket, program: choice.path });
        panel.hidden = true;
      } finally { button.disabled = false; }
    });
    panel.append(button);
  }
  $('detail-edit-options').hidden = true;
  panel.hidden = false;
}
for (const button of document.querySelectorAll('#detail-edit-options .edit-option')) {
  button.onclick = action(async () => {
    const row = currentDetail;
    button.disabled = true;
    try {
      const result = await api(`/api/models/${row.library}/${row.id}/edit`, 'POST', { mode: button.dataset.mode });
      const choices = result.choices || [];
      if (choices.length > 1) renderProgramChooser(result);
      else {
        bridge('local_search_edit', { ticket: result.ticket, program: choices.length ? choices[0].path : '' });
        $('detail-edit-options').hidden = true;
      }
    } finally { button.disabled = false; }
  });
}
window.localSearchHost = async data => {
  if (data.error) return toast(data.error);
  if (data.command === 'edit') {
    const how = data.program ? `已用「${data.program}」打开` : '已交给系统默认程序打开';
    toast(data.mode === 'copy'
      ? `已创建副本「${data.name}」，${how}，原模型保留在库中`
      : `已打开「${data.name}」，${how}，修改保存将覆盖原模型文件`);
    return;
  }
  if (data.command === 'directory') { folderGrant = data.grant; $('folder-path').textContent = data.path; return; }
  if (data.command === 'file') { try { setQuery({ name: data.name, grant: data.grant }); } catch (e) { toast(e.message); } return; }
  if (data.command === 'visibility') {
    state.visible = data.visible;
    queryViewer?.setVisible(data.visible && state.page === 'search'); detailViewer?.setVisible(data.visible && $('detail-dialog').open);
    if (!data.visible) thumbnails?.cancel(); else refreshThumbnails(state.page === 'search' ? $('results') : $('browse-models'));
    return;
  }
  if (data.token) {
    state.token = data.token; state.native = Boolean(data.native); setTheme(data.theme);
    sessionStorage.setItem('local-search-token', state.token);
    if (!initialized) { initialized = true; state.task = sessionStorage.getItem('local-search-task'); await connect(); }
  }
};
window.addEventListener('beforeunload', () => { rememberSession(); clearTimeout(pollTimer); queryViewer?.dispose(); detailViewer?.dispose(); thumbnails?.dispose(); });
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { queryViewer?.render(); detailViewer?.render(); } else thumbnails?.cancel();
});
setTheme();
try { bridge('local_search_ready'); } catch {
  const token = sessionStorage.getItem('local-search-token');
  if (token) window.localSearchHost({ token, native: false });
  else notice('请从 Bambu Studio 首页的“本地模型检索”入口打开此页面。');
}
