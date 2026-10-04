'use strict';

const fileState = { path: '', parent: '', entries: [], loaded: false, loading: false, busy: false,
  controller: null, truncated: false, editor: null, operation: null };

function fileError(message = '') {
  byId('files-error').textContent = message;
  byId('files-error').hidden = !message;
}

async function fileRequest(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let data;
  try { data = JSON.parse(text); } catch (_) { data = null; }
  if (!response.ok) throw new Error(data?.error || text || '文件操作失败');
  return data;
}

function fileAction(payload) {
  return fileRequest('/api/files/action', { method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Ops-Hub-Request': '1' }, body: JSON.stringify(payload) });
}

function setFileBusy(busy) {
  fileState.busy = busy;
  for (const control of byId('files-local').querySelectorAll('button')) control.disabled = busy;
  byId('files-path').disabled = busy;
  if (!busy) {
    byId('files-up').disabled = !fileState.path || fileState.path === fileState.parent;
    renderFiles();
  }
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return '—';
  if (bytes < 1024) return bytes + ' B';
  const units = ['KiB', 'MiB', 'GiB', 'TiB'];
  let value = bytes / 1024, index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
  return value.toFixed(value < 10 ? 1 : 0) + ' ' + units[index];
}

function fileButton(label, handler, className = '') {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = label;
  button.className = className;
  button.disabled = fileState.busy || fileState.loading;
  button.onclick = handler;
  return button;
}

function renderFiles() {
  const query = byId('files-search').value.toLocaleLowerCase();
  const showHidden = byId('files-hidden').checked;
  const entries = fileState.entries.filter(entry => (showHidden || !entry.name.startsWith('.')) && entry.name.toLocaleLowerCase().includes(query));
  const fragment = document.createDocumentFragment();
  for (const entry of entries) {
    const row = document.createElement('tr');
    row.dataset.path = entry.path;
    const cell = document.createElement('td');
    const directory = entry.kind === 'directory' || entry.target_directory;
    const name = fileButton(entry.name, () => directory ? loadFiles(entry.path) : openFile(entry), 'file-name');
    name.disabled = fileState.busy || fileState.loading || entry.protected || (!directory && entry.kind !== 'file');
    name.title = entry.protected ? '受保护的目录，请使用终端管理' : entry.link_target ? '链接到 ' + entry.link_target : directory ? '打开目录' : '打开文本编辑器';
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('viewBox', '0 0 24 24');
    icon.setAttribute('aria-hidden', 'true');
    icon.classList.add('file-icon');
    if (directory) icon.classList.add('folder-icon');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '/static/vendor/lucide/file-icons.svg#' + (directory ? 'folder' : entry.kind === 'symlink' ? 'link' : 'file-text'));
    icon.append(use);
    name.prepend(icon);
    cell.append(name);
    row.append(cell);
    for (const [className, value] of [
      ['file-size', directory ? '—' : formatBytes(entry.size)],
      ['file-modified', entry.modified ? new Date(entry.modified * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'],
      ['file-mode', entry.mode],
    ]) {
      const item = document.createElement('td');
      item.className = className;
      item.textContent = value;
      row.append(item);
    }
    const actions = document.createElement('td');
    actions.className = 'file-row-actions';
    if (!entry.protected && entry.revision) {
      if (entry.kind === 'file') {
        actions.append(fileButton('编辑', () => openFile(entry)));
        const download = document.createElement('a');
        download.textContent = '下载';
        download.href = '/api/files/download?' + new URLSearchParams({ path: entry.path });
        download.className = 'file-download';
        download.setAttribute('download', entry.name);
        actions.append(download);
      }
      actions.append(fileButton('重命名', () => renameFile(entry)), fileButton('删除', () => deleteFile(entry), 'file-delete'));
    } else {
      const unavailable = document.createElement('span');
      unavailable.textContent = '受保护';
      actions.append(unavailable);
    }
    row.append(actions);
    fragment.append(row);
  }
  byId('files-list').replaceChildren(fragment);
  byId('files-empty').hidden = entries.length > 0;
  byId('files-empty').textContent = fileState.loading ? '正在读取目录…' : fileState.loaded ? '没有符合条件的文件' : '请输入目录路径后重试';
  byId('files-summary').textContent = fileState.loading ? '正在读取目录…' : entries.length + ' 项 / 共 ' + fileState.entries.length + ' 项' + (fileState.truncated ? ' · 仅显示前 10000 项，请进入子目录查看' : '');
  for (const id of ['files-upload', 'files-mkdir', 'files-create']) byId(id).disabled = fileState.busy || fileState.loading || !fileState.loaded;
}

async function loadFiles(path = fileState.path) {
  if (selectedHost || currentView !== 'files') return;
  fileState.controller?.abort();
  const controller = new AbortController();
  fileState.controller = controller;
  fileState.loading = true;
  fileError();
  renderFiles();
  try {
    const data = await fileRequest('/api/files?' + new URLSearchParams({ path: path || '' }), { signal: controller.signal });
    if (controller !== fileState.controller || selectedHost || currentView !== 'files') return;
    Object.assign(fileState, { path: data.path, parent: data.parent, entries: data.entries, loaded: true, truncated: data.truncated });
    byId('files-path').value = data.path;
    byId('directory').textContent = data.path;
    byId('files-up').disabled = data.path === data.parent;
  } catch (error) {
    if (error.name !== 'AbortError' && controller === fileState.controller) {
      fileError(error.message);
      byId('files-path').value = fileState.path || path || '';
    }
  } finally {
    if (controller === fileState.controller) { fileState.loading = false; renderFiles(); }
  }
}

function loadFileTarget() {
  fileState.controller?.abort();
  byId('files-local').hidden = Boolean(selectedHost);
  byId('files-remote').hidden = !selectedHost;
  byId('files-remote-description').textContent = '当前选择：' + selectedHost + '。本机文件管理已可使用，该服务器仍可通过终端管理文件。';
  if (!selectedHost) loadFiles();
}

function setWorkspaceView(view) {
  currentView = view;
  const files = view === 'files';
  byId('terminal-workspace').hidden = files;
  byId('files-workspace').hidden = !files;
  byId('view-terminal').setAttribute('aria-pressed', String(!files));
  byId('view-files').setAttribute('aria-pressed', String(files));
  byId('new-session').hidden = files;
  byId('workspace-title').textContent = files ? '文件管理' : '服务器终端';
  byId('workspace-description').textContent = files ? '浏览、传输和编辑文件。' : '连接本机或已配置的服务器。';
  document.title = (files ? '文件管理' : '服务器终端') + ' · Ops Hub';
  document.querySelector('.shortcuts').hidden = files;
  if (!files) selectedHost = activeSession?.host || '';
  updateControls();
  renderHostMenu();
  document.querySelector('.host-menu-hint').textContent = files ? '选择后切换文件管理目标' : '选择后切换或新建终端';
  if (files) loadFileTarget();
  else { fileState.controller?.abort(); requestAnimationFrame(() => fit()); }
}

function askFileOperation(title, description, value = '', input = true, confirm = '确定') {
  return new Promise(resolve => {
    fileState.operation = resolve;
    byId('file-operation-title').textContent = title;
    byId('file-operation-description').textContent = description;
    byId('file-operation-name').hidden = !input;
    byId('file-operation-label').hidden = !input;
    byId('file-operation-name').required = input;
    byId('file-operation-name').value = value;
    byId('file-operation-error').textContent = '';
    byId('file-operation-confirm').textContent = confirm;
    byId('file-operation').showModal();
    if (input) { byId('file-operation-name').focus(); byId('file-operation-name').select(); }
    else byId('file-operation-cancel').focus();
  });
}

function finishFileOperation(value = null) {
  byId('file-operation').close();
  const resolve = fileState.operation;
  fileState.operation = null;
  resolve?.(value);
}

async function performFileAction(payload, success) {
  if (fileState.busy) return null;
  setFileBusy(true);
  fileError();
  try {
    const result = await fileAction(payload);
    notify(success);
    await loadFiles();
    return result;
  } catch (error) { fileError(error.message); return null; }
  finally { setFileBusy(false); }
}

async function createFileItem(directory) {
  if (fileState.busy || !fileState.loaded || selectedHost) return;
  const path = fileState.path;
  const name = await askFileOperation(directory ? '新建文件夹' : '新建文本文件', '创建于 ' + path, '', true, '创建');
  if (name === null) return;
  const result = await performFileAction({ action: directory ? 'mkdir' : 'create', path, name }, directory ? '文件夹已创建' : '文件已创建');
  if (result && !directory) openFile({ path: result.path, kind: 'file' });
}

async function renameFile(entry) {
  if (fileState.busy) return;
  const name = await askFileOperation('重命名', entry.path, entry.name, true, '重命名');
  if (name === null || name === entry.name) return;
  await performFileAction({ action: 'rename', path: entry.path, name, revision: entry.revision }, '已重命名');
}

async function deleteFile(entry) {
  if (fileState.busy) return;
  const result = await askFileOperation('移入回收站', entry.path + '\n可在回收站中恢复；文件夹及其内容会一起移入。', '', false, '移入回收站');
  if (result === null) return;
  await performFileAction({ action: 'delete', path: entry.path, revision: entry.revision }, '已移入回收站');
}

async function openFile(entry) {
  if (fileState.busy || entry.kind !== 'file') return;
  setFileBusy(true);
  fileError();
  try {
    const data = await fileRequest('/api/files/text?' + new URLSearchParams({ path: entry.path }));
    if (selectedHost || currentView !== 'files') return;
    data.newline = data.content.includes('\r\n') && !/(^|[^\r])\n/.test(data.content) ? '\r\n' : '\n';
    data.displayContent = data.content.replace(/\r\n?/g, '\n');
    fileState.editor = data;
    byId('file-editor-path').textContent = data.path;
    byId('file-editor-content').value = data.content;
    byId('file-editor-status').textContent = 'UTF-8 · 保存前自动备份，可在回收站恢复';
    byId('file-editor').showModal();
    byId('file-editor-content').focus();
  } catch (error) { fileError(error.message); }
  finally { setFileBusy(false); }
}

function editorDirty() { return fileState.editor && byId('file-editor-content').value !== fileState.editor.displayContent; }

async function closeFileEditor() {
  if (byId('file-editor-save').disabled) return;
  if (editorDirty()) {
    const discard = await askFileOperation('放弃未保存的修改？', fileState.editor.path, '', false, '放弃修改');
    if (discard === null) return;
  }
  byId('file-editor').close();
  fileState.editor = null;
}

async function saveFileEditor(event) {
  event.preventDefault();
  if (!fileState.editor || byId('file-editor-save').disabled) return;
  const editor = fileState.editor, displayContent = byId('file-editor-content').value;
  const content = editor.newline === '\r\n' ? displayContent.replace(/\n/g, '\r\n') : displayContent;
  if (new TextEncoder().encode(content).length > 2 * 1024 * 1024) {
    byId('file-editor-status').textContent = '文本编辑限制为 2 MiB。';
    return;
  }
  byId('file-editor-save').disabled = true;
  byId('file-editor-content').readOnly = true;
  byId('file-editor-status').textContent = '正在保存…';
  try {
    const result = await fileAction({ action: 'save', path: editor.path, content, revision: editor.revision });
    Object.assign(editor, { content, displayContent, revision: result.revision });
    byId('file-editor-status').textContent = '已保存 · 上一版本已备份到回收站';
    await loadFiles();
  } catch (error) { byId('file-editor-status').textContent = error.message; }
  finally { byId('file-editor-save').disabled = false; byId('file-editor-content').readOnly = false; }
}

async function uploadFiles(files) {
  if (!files.length || fileState.busy || selectedHost) return;
  const path = fileState.path;
  setFileBusy(true);
  fileError();
  let completed = 0;
  const errors = [];
  try {
    for (const file of files) {
      if (file.size > 256 * 1024 * 1024) { errors.push(file.name + '：超过 256 MiB'); continue; }
      byId('files-summary').textContent = '正在上传 ' + file.name + '（' + (completed + 1) + '/' + files.length + '）';
      const body = new FormData();
      body.append('file', file);
      try {
        await fileRequest('/api/files/upload?' + new URLSearchParams({ path }), { method: 'POST', headers: { 'X-Ops-Hub-Request': '1' }, body });
        completed++;
      } catch (error) { errors.push(file.name + '：' + error.message); }
    }
    await loadFiles();
    if (completed) notify(completed + ' 个文件上传完成');
    if (errors.length) fileError(errors.join('；'));
  } finally { setFileBusy(false); byId('files-upload-input').value = ''; }
}

async function loadTrash() {
  byId('file-trash-status').textContent = '正在读取回收站…';
  try {
    const data = await fileRequest('/api/files/trash');
    const fragment = document.createDocumentFragment();
    for (const entry of data.entries) {
      const row = document.createElement('div');
      row.className = 'trash-row';
      const labels = document.createElement('div');
      const path = document.createElement('strong');
      path.textContent = entry.path;
      const detail = document.createElement('span');
      detail.textContent = (entry.operation === 'edit' ? '编辑备份' : '已删除') + ' · ' + new Date(entry.time * 1000).toLocaleString('zh-CN', { hour12: false });
      labels.append(path, detail);
      row.append(labels, fileButton('恢复', async () => {
        const destination = await askFileOperation('恢复文件', '恢复不会覆盖已有文件。可修改为其他绝对路径。', entry.path, true, '恢复');
        if (destination === null) return;
        setFileBusy(true);
        for (const button of byId('file-trash-list').querySelectorAll('button')) button.disabled = true;
        try {
          await fileAction({ action: 'restore', id: entry.id, destination });
          notify('文件已恢复');
          await loadFiles();
          await loadTrash();
        } catch (error) { byId('file-trash-status').textContent = error.message; }
        finally {
          setFileBusy(false);
          for (const button of byId('file-trash-list').querySelectorAll('button')) button.disabled = false;
        }
      }));
      fragment.append(row);
    }
    byId('file-trash-list').replaceChildren(fragment);
    byId('file-trash-status').textContent = data.entries.length ? data.entries.length + ' 条可恢复记录' : '回收站为空';
  } catch (error) { byId('file-trash-status').textContent = error.message; }
}

byId('view-terminal').onclick = () => setWorkspaceView('terminal');
byId('view-files').onclick = () => setWorkspaceView('files');
byId('files-local-switch').onclick = () => { selectedHost = ''; updateControls(); renderHostMenu(); loadFileTarget(); };
byId('files-path-form').onsubmit = event => { event.preventDefault(); if (!fileState.busy) loadFiles(byId('files-path').value); };
byId('files-up').onclick = () => loadFiles(fileState.parent);
byId('files-home').onclick = () => loadFiles(machineInfo?.directory || '');
byId('files-refresh').onclick = () => loadFiles();
byId('files-hidden').onchange = renderFiles;
byId('files-search').oninput = renderFiles;
byId('files-mkdir').onclick = () => createFileItem(true);
byId('files-create').onclick = () => createFileItem(false);
byId('files-upload').onclick = () => { if (fileState.loaded) byId('files-upload-input').click(); };
byId('files-upload-input').onchange = event => uploadFiles([...event.target.files]);
byId('file-operation-form').onsubmit = event => { event.preventDefault(); finishFileOperation(byId('file-operation-name').hidden ? true : byId('file-operation-name').value); };
byId('file-operation-cancel').onclick = () => finishFileOperation();
byId('file-operation').oncancel = event => { event.preventDefault(); finishFileOperation(); };
byId('file-editor-form').onsubmit = saveFileEditor;
byId('file-editor-close').onclick = closeFileEditor;
byId('file-editor-cancel').onclick = closeFileEditor;
byId('file-editor').oncancel = event => { event.preventDefault(); closeFileEditor(); };
byId('file-editor-content').oninput = () => { byId('file-editor-status').textContent = editorDirty() ? '有未保存的修改' : 'UTF-8 · 保存前自动备份'; };
byId('file-editor-content').onkeydown = event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); byId('file-editor-form').requestSubmit(); }
};
byId('files-trash').onclick = () => { byId('file-trash').showModal(); loadTrash(); };
byId('file-trash-close').onclick = () => { if (!fileState.busy) byId('file-trash').close(); };
byId('file-trash').oncancel = event => { if (fileState.busy) event.preventDefault(); };
window.addEventListener('beforeunload', event => { if (editorDirty()) { event.preventDefault(); event.returnValue = ''; } });
