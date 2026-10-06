'use strict';

const fileState = { path: '', parent: '', entries: [], loaded: false, loading: false, busy: false,
  host: '', home: '', capabilities: {}, uploadLimit: 0, controller: null, truncated: false, editor: null,
  operation: null, drag: null, uploadDragDepth: 0 };
const fileLocations = new Map();

function fileURL(endpoint, parameters = {}, host = fileState.host) {
  return endpoint + '?' + new URLSearchParams({ ...parameters, host });
}

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

function fileAction(payload, host = fileState.host) {
  return fileRequest(fileURL('/api/files/action', {}, host), { method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Ops-Hub-Request': '1' }, body: JSON.stringify(payload) });
}

function setFileBusy(busy) {
  fileState.busy = busy;
  for (const id of ['host-pill', 'view-terminal', 'view-files']) byId(id).disabled = busy;
  for (const control of byId('files-local').querySelectorAll('button')) control.disabled = busy;
  byId('files-path').disabled = busy;
  byId('file-trash-empty').disabled = busy || !byId('file-trash-list').children.length;
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

function clearFileDrag() {
  fileState.drag = null;
  for (const node of document.querySelectorAll('.file-drop-target, .file-dragging')) {
    node.classList.remove('file-drop-target', 'file-dragging');
  }
}

function canDropFile(directory) {
  const entry = fileState.drag;
  if (!entry || fileState.busy || fileState.loading || fileState.host !== selectedHost || currentView !== 'files') return false;
  if (directory === entry.path.slice(0, entry.path.lastIndexOf('/')) || (directory === '/' && entry.path.lastIndexOf('/') === 0)) return false;
  return entry.kind !== 'directory' || (directory !== entry.path && !directory.startsWith(entry.path + '/'));
}

function bindFileDrop(node, directory) {
  node.dataset.dropPath = directory;
  node.ondragover = event => {
    if (!fileState.drag) return;
    event.preventDefault();
    const allowed = canDropFile(directory);
    event.dataTransfer.dropEffect = allowed ? 'move' : 'none';
    node.classList.toggle('file-drop-target', allowed);
  };
  node.ondragleave = event => {
    if (!node.contains(event.relatedTarget)) node.classList.remove('file-drop-target');
  };
  node.ondrop = event => {
    if (!fileState.drag) return;
    event.preventDefault();
    const entry = fileState.drag, allowed = canDropFile(directory);
    clearFileDrag();
    if (allowed) performFileAction({ action: 'move', path: entry.path, destination: directory, revision: entry.revision }, '已移动 ' + entry.name);
  };
}

function renderFileBreadcrumbs() {
  const nav = byId('files-breadcrumbs');
  const fragment = document.createDocumentFragment();
  if (fileState.path) {
    let path = '';
    const parts = ['/', ...fileState.path.split('/').filter(Boolean)];
    for (const [index, part] of parts.entries()) {
      path = index === 0 ? '/' : (path === '/' ? '/' : path + '/') + part;
      if (index) {
        const separator = document.createElement('span');
        separator.className = 'breadcrumb-separator';
        separator.textContent = '›';
        separator.setAttribute('aria-hidden', 'true');
        fragment.append(separator);
      }
      const destination = path;
      const button = fileButton(part, () => loadFiles(destination), 'file-breadcrumb');
      button.dataset.path = path;
      button.title = path;
      if (index === parts.length - 1) button.setAttribute('aria-current', 'page');
      bindFileDrop(button, destination);
      fragment.append(button);
    }
  }
  nav.replaceChildren(fragment);
  byId('files-path-edit').disabled = fileState.busy || fileState.loading;
}

function editFilePath(editing) {
  byId('files-path').hidden = !editing;
  byId('files-breadcrumbs').hidden = editing;
  byId('files-path-go').hidden = !editing;
  byId('files-path-edit').setAttribute('aria-label', editing ? '收起目录路径' : '编辑目录路径');
  byId('files-path-edit').title = editing ? '收起目录路径' : '编辑目录路径';
  if (editing) {
    byId('files-path').focus();
    byId('files-path').select();
  } else {
    byId('files-path').value = fileState.path;
    requestAnimationFrame(() => { byId('files-breadcrumbs').scrollLeft = byId('files-breadcrumbs').scrollWidth; });
  }
}

function renderFiles() {
  const query = byId('files-search').value.toLocaleLowerCase();
  const showHidden = byId('files-hidden').checked;
  const entries = fileState.entries.filter(entry => (showHidden || !entry.name.startsWith('.')) && entry.name.toLocaleLowerCase().includes(query));
  const fragment = document.createDocumentFragment();
  for (const entry of entries) {
    const row = document.createElement('tr');
    row.dataset.path = entry.path;
    row.draggable = !entry.protected && Boolean(entry.revision) && !fileState.busy && !fileState.loading;
    row.ondragstart = event => {
      if (!row.draggable || event.target.closest('.file-row-actions')) { event.preventDefault(); return; }
      clearFileDrag();
      fileState.drag = entry;
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('application/x-ops-hub-file', entry.path);
      event.dataTransfer.setData('text/plain', entry.path);
      row.classList.add('file-dragging');
    };
    row.ondragend = clearFileDrag;
    const cell = document.createElement('td');
    const directory = entry.kind === 'directory' || entry.target_directory;
    if (directory && !entry.protected) bindFileDrop(row, entry.path);
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
        actions.append(fileButton(fileState.capabilities.edit === false ? '查看' : '编辑', () => openFile(entry)));
        const download = document.createElement('a');
        download.textContent = '下载';
        download.href = fileURL('/api/files/download', { path: entry.path });
        download.className = 'file-download';
        download.setAttribute('download', entry.name);
        actions.append(download);
      }
      actions.append(fileButton('重命名', () => renameFile(entry)));
      if (fileState.capabilities.trash !== false) actions.append(fileButton('删除', () => deleteFile(entry), 'file-delete'));
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
  byId('files-trash').hidden = fileState.capabilities.trash === false;
  renderFileBreadcrumbs();
}

async function loadFiles(path = fileState.path) {
  if (currentView !== 'files') return;
  const host = selectedHost;
  clearFileDrag();
  fileState.controller?.abort();
  const controller = new AbortController();
  fileState.controller = controller;
  fileState.loading = true;
  fileError();
  renderFiles();
  updateControls();
  try {
    const data = await fileRequest(fileURL('/api/files', { path: path || '' }, host), { signal: controller.signal });
    if (controller !== fileState.controller || host !== selectedHost || currentView !== 'files') return;
    Object.assign(fileState, { host, home: data.home, capabilities: data.capabilities || {},
      path: data.path, parent: data.parent, entries: data.entries, loaded: true, truncated: data.truncated,
      uploadLimit: data.upload_limit || 0 });
    fileLocations.set(host, data.path);
    byId('files-path').value = data.path;
    editFilePath(false);
    byId('directory').textContent = data.path;
    byId('files-up').disabled = data.path === data.parent;
  } catch (error) {
    if (error.name !== 'AbortError' && controller === fileState.controller) {
      fileError(error.message);
      byId('files-path').value = fileState.path || path || '';
    }
  } finally {
    if (controller === fileState.controller) { fileState.loading = false; renderFiles(); updateControls(); }
  }
}

function loadFileTarget() {
  clearFileDrag();
  fileState.controller?.abort();
  if (fileState.host !== selectedHost) {
    Object.assign(fileState, { host: selectedHost, path: fileLocations.get(selectedHost) || '',
      parent: '', home: '', entries: [], loaded: false, uploadLimit: 0,
      capabilities: selectedHost ? { edit: false, trash: false } : {} });
    byId('files-path').value = fileState.path;
  }
  return loadFiles();
}

function setWorkspaceView(view) {
  clearFileDrag();
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
  if (files) return loadFileTarget();
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
  if (fileState.busy || fileState.loading || !fileState.loaded) return;
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
    const host = fileState.host;
    const data = await fileRequest(fileURL('/api/files/text', { path: entry.path }, host));
    if (selectedHost !== host || currentView !== 'files') return;
    data.host = host;
    data.newline = data.content.includes('\r\n') && !/(^|[^\r])\n/.test(data.content) ? '\r\n' : '\n';
    data.displayContent = data.content.replace(/\r\n?/g, '\n');
    fileState.editor = data;
    byId('file-editor-path').textContent = data.path;
    byId('file-editor-content').value = data.content;
    byId('file-editor-title').textContent = data.readonly ? '查看文件' : '编辑文件';
    byId('file-editor-content').readOnly = Boolean(data.readonly);
    byId('file-editor-save').hidden = Boolean(data.readonly);
    byId('file-editor-status').textContent = data.readonly ? 'UTF-8 · 远程文件只读查看' : 'UTF-8 · 保存前自动备份，可在回收站恢复';
    byId('file-editor').showModal();
    byId('file-editor-content').focus();
  } catch (error) { fileError(error.message); }
  finally { setFileBusy(false); }
}

function editorDirty() { return fileState.editor && !fileState.editor.readonly && byId('file-editor-content').value !== fileState.editor.displayContent; }

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
  if (!fileState.editor || fileState.editor.readonly || byId('file-editor-save').disabled) return;
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
    const result = await fileAction({ action: 'save', path: editor.path, content, revision: editor.revision }, editor.host);
    Object.assign(editor, { content, displayContent, revision: result.revision });
    byId('file-editor-status').textContent = '已保存 · 上一版本已备份到回收站';
    await loadFiles();
  } catch (error) { byId('file-editor-status').textContent = error.message; }
  finally { byId('file-editor-save').disabled = false; byId('file-editor-content').readOnly = false; }
}

function formatUploadTime(seconds) {
  const remaining = Math.max(0, Math.ceil(seconds));
  if (remaining < 60) return remaining + ' 秒';
  if (remaining < 3600) return Math.floor(remaining / 60) + ' 分 ' + remaining % 60 + ' 秒';
  return Math.floor(remaining / 3600) + ' 小时 ' + Math.floor(remaining % 3600 / 60) + ' 分';
}

function showUploadProgress(file, index, total, completed) {
  const panel = byId('files-upload-progress');
  panel.dataset.state = 'uploading';
  byId('files-upload-name').textContent = file.name;
  byId('files-upload-name').title = file.name;
  byId('files-upload-percent').textContent = '0%';
  byId('files-upload-bar').value = 0;
  byId('files-upload-size').textContent = formatBytes(file.size);
  byId('files-upload-speed').textContent = '—';
  byId('files-upload-eta').textContent = '估算中…';
  byId('files-upload-status').textContent = '正在上传 · 第 ' + (index + 1) + '/' + total + ' 个 · ' + completed + ' 个已完成';
}

function uploadWithProgress(file, path, index, total, completed) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const host = fileState.host;
    request.open('POST', fileURL('/api/files/upload', { path }, host));
    request.setRequestHeader('X-Ops-Hub-Request', '1');
    let loaded = 0, transferTotal = null;
    const samples = [{ time: performance.now(), loaded: 0 }];
    const updateMetrics = () => {
      const now = performance.now();
      samples.push({ time: now, loaded });
      // Use a recent window so the estimate responds to changes and stalled transfers.
      while (samples.length > 2 && samples[1].time <= now - 3000) samples.shift();
      const elapsed = (now - samples[0].time) / 1000;
      const speed = elapsed > 0 ? (loaded - samples[0].loaded) / elapsed : 0;
      byId('files-upload-speed').textContent = formatBytes(Math.round(speed)) + '/s';
      byId('files-upload-eta').textContent = transferTotal !== null && loaded >= transferTotal ? '0 秒'
        : transferTotal !== null && speed > 0 ? formatUploadTime((transferTotal - loaded) / speed) : '估算中…';
    };
    const timer = setInterval(updateMetrics, 250);
    request.upload.onprogress = event => {
      loaded = event.loaded;
      transferTotal = event.lengthComputable && event.total ? event.total : null;
      updateMetrics();
      if (!event.lengthComputable || !event.total) {
        byId('files-upload-bar').removeAttribute('value');
        byId('files-upload-percent').textContent = '上传中';
        return;
      }
      const percent = Math.min(100, event.loaded / event.total * 100);
      byId('files-upload-bar').value = percent;
      byId('files-upload-percent').textContent = Math.floor(percent) + '%';
    };
    request.upload.onload = () => {
      clearInterval(timer);
      if (host) byId('files-upload-bar').removeAttribute('value');
      else byId('files-upload-bar').value = 100;
      byId('files-upload-percent').textContent = host ? '远端传输中' : '100%';
      byId('files-upload-speed').textContent = '—';
      byId('files-upload-eta').textContent = host ? '等待远端确认' : '等待保存';
      byId('files-upload-status').textContent = (host ? '已上传至中枢，正在传输至 ' + host : '传输完成，正在保存')
        + ' · 第 ' + (index + 1) + '/' + total + ' 个 · ' + completed + ' 个已完成';
    };
    request.onload = () => {
      clearInterval(timer);
      let data;
      try { data = JSON.parse(request.responseText); } catch (_) { data = null; }
      if (request.status >= 200 && request.status < 300) resolve(data);
      else reject(new Error(data?.error || request.responseText || '文件上传失败'));
    };
    request.onerror = () => { clearInterval(timer); reject(new Error('上传失败，网络连接中断')); };
    request.onabort = () => { clearInterval(timer); reject(new Error('上传已取消')); };
    const body = new FormData();
    body.append('file', file);
    try { request.send(body); }
    catch (error) { clearInterval(timer); reject(error); }
  });
}

async function uploadFiles(files) {
  if (!files.length || fileState.busy || fileState.loading || !fileState.loaded || fileState.host !== selectedHost || currentView !== 'files') return;
  const path = fileState.path;
  byId('files-upload-progress').hidden = false;
  setFileBusy(true);
  fileError();
  let completed = 0;
  const errors = [];
  try {
    for (const [index, file] of files.entries()) {
      showUploadProgress(file, index, files.length, completed);
      if (fileState.uploadLimit && file.size > fileState.uploadLimit) {
        errors.push(file.name + '：超过 ' + formatBytes(fileState.uploadLimit)); continue;
      }
      try {
        await uploadWithProgress(file, path, index, files.length, completed);
        completed++;
        byId('files-upload-bar').value = 100;
        byId('files-upload-percent').textContent = '100%';
      } catch (error) { errors.push(file.name + '：' + error.message); }
    }
    byId('files-upload-progress').dataset.state = errors.length ? completed ? 'partial' : 'error' : 'done';
    byId('files-upload-status').textContent = errors.length
      ? '上传结束 · ' + completed + ' 个成功，' + errors.length + ' 个失败'
      : '上传完成 · ' + completed + '/' + files.length + ' 个文件';
    byId('files-upload-speed').textContent = '—';
    byId('files-upload-eta').textContent = errors.length ? '—' : '0 秒';
    await loadFiles();
    if (completed) notify(completed + ' 个文件上传完成');
    if (errors.length) fileError(errors.join('；'));
  } finally { setFileBusy(false); byId('files-upload-input').value = ''; }
}

async function performTrashAction(payload, success) {
  if (fileState.busy) return;
  setFileBusy(true);
  for (const button of byId('file-trash-list').querySelectorAll('button')) button.disabled = true;
  byId('file-trash-status').textContent = payload.action === 'empty_trash' ? '正在清空回收站…'
    : payload.action === 'purge' ? '正在彻底删除…' : '正在恢复…';
  try {
    const result = await fileAction(payload);
    notify(payload.action === 'empty_trash' ? '已彻底删除 ' + result.deleted + ' 条记录' : success);
    if (payload.action === 'restore') await loadFiles();
    await loadTrash();
    if (result.errors?.length) byId('file-trash-status').textContent = '已删除 ' + result.deleted + ' 条，'
      + result.errors.length + ' 条删除失败。请检查访问权限和回收站路径后重试。';
    else if (result.warning) byId('file-trash-status').textContent = result.warning;
  } catch (error) { byId('file-trash-status').textContent = error.message; }
  finally {
    setFileBusy(false);
    for (const button of byId('file-trash-list').querySelectorAll('button')) button.disabled = button.dataset.unavailable === 'true';
  }
}

async function loadTrash() {
  byId('file-trash-status').textContent = '正在读取回收站…';
  byId('file-trash-empty').disabled = true;
  try {
    const data = await fileRequest(fileURL('/api/files/trash'));
    const fragment = document.createDocumentFragment();
    for (const entry of data.entries) {
      const row = document.createElement('div');
      row.className = 'trash-row';
      const labels = document.createElement('div');
      labels.className = 'trash-labels';
      const path = document.createElement('strong');
      path.textContent = entry.path;
      const detail = document.createElement('span');
      detail.textContent = (entry.recoverable === false ? '内容缺失或记录损坏，可清理' : entry.operation === 'edit' ? '编辑备份' : '已删除')
        + ' · ' + new Date(entry.time * 1000).toLocaleString('zh-CN', { hour12: false });
      labels.append(path, detail);
      const actions = document.createElement('div');
      actions.className = 'trash-actions';
      actions.append(fileButton('恢复', async () => {
        if (fileState.busy) return;
        const destination = await askFileOperation('恢复文件', '恢复不会覆盖已有文件。可修改为其他绝对路径。', entry.path, true, '恢复');
        if (destination === null) return;
        await performTrashAction({ action: 'restore', id: entry.id, destination }, '文件已恢复');
      }), fileButton('彻底删除', async () => {
        if (fileState.busy) return;
        const confirmed = await askFileOperation('彻底删除', entry.path + '\n\n将永久删除这条记录及其文件内容，文件夹内的所有内容也会删除。删除后无法通过回收站恢复。'
          + (entry.operation === 'edit' ? '\n此操作仅删除编辑备份，当前文件不受影响。' : ''), '', false, '彻底删除');
        if (!confirmed) return;
        await performTrashAction({ action: 'purge', id: entry.id }, '已彻底删除');
      }, 'trash-delete'));
      if (entry.recoverable === false) {
        actions.firstElementChild.disabled = true;
        actions.firstElementChild.dataset.unavailable = 'true';
      }
      row.append(labels, actions);
      fragment.append(row);
    }
    byId('file-trash-list').replaceChildren(fragment);
    byId('file-trash-status').textContent = data.entries.length ? data.entries.length + ' 条可恢复记录' : '回收站为空';
  } catch (error) { byId('file-trash-status').textContent = error.message; }
  finally { byId('file-trash-empty').disabled = fileState.busy || !byId('file-trash-list').children.length; }
}

function externalFileDrag(event) {
  return !fileState.drag && Array.from(event.dataTransfer?.types || []).includes('Files');
}

function uploadDropIssue() {
  if (fileState.busy) return '正在处理文件，请等待当前操作完成。';
  if (document.querySelector('dialog[open]')) return '请先关闭当前弹窗，再拖入文件。';
  if (fileState.loading) return '正在读取目录，请稍后再拖入文件。';
  if (currentView === 'files' && !fileState.loaded) return '请先打开可访问的本机目录。';
  if (!selectedHost && !fileState.path && !machineInfo?.directory) return '正在读取本机信息，请稍后再拖入文件。';
  return '';
}

function hideUploadDrop() {
  fileState.uploadDragDepth = 0;
  byId('files-drop-overlay').hidden = true;
}

function showUploadDrop() {
  const issue = uploadDropIssue();
  const title = issue ? '暂时无法上传' : '松开以上传文件';
  const path = fileState.host === selectedHost ? fileState.path : fileLocations.get(selectedHost);
  const description = issue || '上传至' + (selectedHost || '本机') + '：' + (path || (selectedHost ? '主目录' : machineInfo.directory));
  const overlay = byId('files-drop-overlay');
  overlay.dataset.state = issue ? 'blocked' : 'ready';
  overlay.hidden = false;
  if (byId('files-drop-title').textContent !== title) byId('files-drop-title').textContent = title;
  if (byId('files-drop-description').textContent !== description) byId('files-drop-description').textContent = description;
  return !issue;
}

async function uploadDroppedFiles(event) {
  event.preventDefault();
  hideUploadDrop();
  const issue = uploadDropIssue();
  if (issue) { notify(issue); return; }
  const transfer = event.dataTransfer;
  const items = Array.from(transfer.items || []).filter(item => item.kind === 'file');
  if (items.some(item => item.webkitGetAsEntry?.()?.isDirectory)) {
    notify('暂不支持上传文件夹，请单独拖入文件。');
    return;
  }
  // Read the File objects during the drop event, before the browser protects the transfer.
  const files = Array.from(transfer.files || []);
  if (!files.length) { notify('未能读取拖入的文件，请使用上传文件按钮重试。'); return; }
  const host = selectedHost;
  const destination = (fileState.host === host ? fileState.path : fileLocations.get(host)) || (host ? '' : machineInfo.directory);
  if (currentView !== 'files') await setWorkspaceView('files');
  if (uploadDropIssue() || !fileState.loaded || selectedHost !== host || (destination && fileState.path !== destination) || currentView !== 'files') {
    notify('上传目录尚未就绪或已切换，请重新拖入文件。');
    return;
  }
  await uploadFiles(files);
}

document.addEventListener('dragenter', event => {
  if (!externalFileDrag(event)) return;
  event.preventDefault();
  fileState.uploadDragDepth++;
  showUploadDrop();
});
document.addEventListener('dragover', event => {
  if (!externalFileDrag(event)) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = showUploadDrop() ? 'copy' : 'none';
});
document.addEventListener('dragleave', event => {
  if (!fileState.uploadDragDepth) return;
  fileState.uploadDragDepth = Math.max(0, fileState.uploadDragDepth - 1);
  if (!fileState.uploadDragDepth) hideUploadDrop();
});
document.addEventListener('drop', event => {
  if (externalFileDrag(event)) uploadDroppedFiles(event).catch(() => notify('文件上传失败，请重试。'));
  else hideUploadDrop();
});
document.addEventListener('dragend', hideUploadDrop);
// External drags may end outside this document without a dragend event.
document.addEventListener('pointermove', event => { if (!event.buttons) hideUploadDrop(); });
document.addEventListener('pointerdown', hideUploadDrop);
window.addEventListener('blur', hideUploadDrop);
document.addEventListener('keydown', event => { if (event.key === 'Escape') hideUploadDrop(); });

function updateFileSearchButton() {
  const button = byId('files-search-toggle'), input = byId('files-search');
  const open = !input.hidden;
  button.parentElement.classList.toggle('is-open', open);
  button.setAttribute('aria-expanded', String(open));
  button.setAttribute('aria-label', open ? '收起搜索' : '搜索');
  button.dataset.filtered = String(Boolean(input.value));
  button.title = open ? '收起搜索' : input.value ? '搜索：' + input.value : '搜索';
}

function toggleFileSearch(open, { clear = true, focus = true } = {}) {
  const button = byId('files-search-toggle'), input = byId('files-search');
  input.hidden = !open;
  if (!open && clear) {
    input.value = '';
    renderFiles();
  }
  updateFileSearchButton();
  if (focus) (open ? input : button).focus();
}

byId('view-terminal').onclick = () => setWorkspaceView('terminal');
byId('view-files').onclick = () => setWorkspaceView('files');
byId('files-path-form').onsubmit = event => { event.preventDefault(); if (!fileState.busy) loadFiles(byId('files-path').value); };
byId('files-path-edit').onclick = () => editFilePath(byId('files-path').hidden);
byId('files-path').onkeydown = event => {
  if (event.key === 'Escape') { event.preventDefault(); editFilePath(false); byId('files-path-edit').focus(); }
};
byId('files-up').onclick = () => loadFiles(fileState.parent);
byId('files-home').onclick = () => loadFiles(fileState.home || (selectedHost ? '' : machineInfo?.directory) || '');
byId('files-refresh').onclick = () => loadFiles();
byId('files-hidden').onchange = renderFiles;
byId('files-search').oninput = () => { renderFiles(); updateFileSearchButton(); };
byId('files-search-toggle').onclick = () => toggleFileSearch(byId('files-search').hidden);
byId('files-search').onblur = () => {
  // Let the clicked control handle its action before collapsing the input.
  requestAnimationFrame(() => {
    const input = byId('files-search');
    if (!input.hidden && document.activeElement !== input) toggleFileSearch(false, { clear: false, focus: false });
  });
};
byId('files-search').onkeydown = event => {
  if (event.key === 'Escape') { event.preventDefault(); toggleFileSearch(false); }
};
byId('files-mkdir').onclick = () => createFileItem(true);
byId('files-create').onclick = () => createFileItem(false);
byId('files-upload').onclick = () => { if (fileState.loaded) byId('files-upload-input').click(); };
byId('files-upload-input').onchange = event => uploadFiles([...event.target.files]);
byId('files-upload-close').onclick = () => { byId('files-upload-progress').hidden = true; };
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
byId('file-trash-empty').onclick = async () => {
  if (fileState.busy || byId('file-trash-empty').disabled) return;
  const confirmed = await askFileOperation('清空回收站', '将永久删除回收站中的所有文件、文件夹和编辑备份。\n删除后无法通过回收站恢复，当前文件和符号链接指向的目标不受影响。', '', false, '清空回收站');
  if (confirmed) await performTrashAction({ action: 'empty_trash' });
};
byId('file-trash-close').onclick = () => { if (!fileState.busy) byId('file-trash').close(); };
byId('file-trash').oncancel = event => { if (fileState.busy) event.preventDefault(); };
window.addEventListener('beforeunload', event => { if (editorDirty()) { event.preventDefault(); event.returnValue = ''; } });
