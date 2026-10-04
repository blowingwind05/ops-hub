'use strict';

const byId = id => document.getElementById(id);
const themes = {
  "dark": {
    "background": "#1e1e1e",
    "foreground": "#cccccc",
    "cursor": "#aeafad",
    "cursorAccent": "#1e1e1e",
    "selectionBackground": "#264f78",
    "selectionInactiveBackground": "#3a3d41",
    "black": "#000000",
    "red": "#cd3131",
    "green": "#0dbc79",
    "yellow": "#e5e510",
    "blue": "#2472c8",
    "magenta": "#bc3fbc",
    "cyan": "#11a8cd",
    "white": "#e5e5e5",
    "brightBlack": "#666666",
    "brightRed": "#f14c4c",
    "brightGreen": "#23d18b",
    "brightYellow": "#f5f543",
    "brightBlue": "#3b8eea",
    "brightMagenta": "#d670d6",
    "brightCyan": "#29b8db",
    "brightWhite": "#e5e5e5"
  },
  "light": {
    "background": "#f7f8fa",
    "foreground": "#374151",
    "cursor": "#374151",
    "cursorAccent": "#f7f8fa",
    "selectionBackground": "#add6ff",
    "selectionInactiveBackground": "#e7ebf0",
    "black": "#000000",
    "red": "#cd3131",
    "green": "#107c10",
    "yellow": "#949800",
    "blue": "#0451a5",
    "magenta": "#bc05bc",
    "cyan": "#0598bc",
    "white": "#555555",
    "brightBlack": "#666666",
    "brightRed": "#f14c4c",
    "brightGreen": "#14ce14",
    "brightYellow": "#b5ba00",
    "brightBlue": "#3b8eea",
    "brightMagenta": "#d670d6",
    "brightCyan": "#29b8db",
    "brightWhite": "#a5a5a5"
  }
};
const sessions = [];
const MAX_SESSIONS = 8;
let activeSession = null;
let nextSessionId = 1;
let shellName = 'bash';
let machineInfo = null;
let selectedHost = '';
let currentView = 'terminal';
let configuredHosts = [];
let sshAvailable = true;
let currentTheme = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
let fontSize = 14;
let noticeTimer;

function notify(text) {
  byId('notice').textContent = text;
  byId('notice').classList.add('visible');
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => byId('notice').classList.remove('visible'), 2800);
}

function applyTheme(theme) {
  currentTheme = theme;
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="color-scheme"]').content = theme;
  for (const session of sessions) session.terminal.options.theme = themes[theme];
  const light = theme === 'light';
  const label = light ? '深色模式' : '浅色模式';
  byId('theme-label').textContent = label;
  byId('theme-icon').textContent = light ? '☾' : '☀';
  byId('theme-toggle').setAttribute('aria-label', '切换到' + label);
  byId('theme-toggle').setAttribute('aria-pressed', String(light));
  try { localStorage.setItem('web-terminal-theme', theme); }
  catch (_) { /* Theme switching also works with disabled storage. */ }
}

function updateControls() {
  const session = activeSession;
  byId('status').dataset.state = session?.state || 'disconnected';
  document.querySelector('.terminal-card').dataset.state = session?.state || 'disconnected';
  byId('status-text').textContent = session?.statusText || '无会话';
  if (currentView === 'files') {
    byId('status').dataset.state = selectedHost ? 'disconnected' : 'connected';
    byId('status-text').textContent = selectedHost ? '未接入' : '本机文件';
  }
  for (const id of ['copy', 'clear', 'font-up', 'font-down']) byId(id).disabled = !session;
  for (const id of ['paste', 'interrupt']) byId(id).disabled = session?.state !== 'connected';
  byId('tab-title').textContent = session ? session.program : '终端';
  if (session?.host) byId('tab-title').textContent = session.host + ' · ' + session.program;
  byId('tab-index').textContent = session ? String(session.id).padStart(2, '0') : '—';
  byId('terminal-size').textContent = session ? session.terminal.cols + ' × ' + session.terminal.rows : '— × —';
  byId('terminal-empty').hidden = Boolean(session);
  updateMachineInfo();
}

function updateMachineInfo() {
  const host = currentView === 'files' ? selectedHost : activeSession?.host;
  const target = selectedHost || machineInfo?.hostname || '本机';
  byId('host-name').textContent = target;
  byId('host-pill').title = target + ' · 选择服务器';
  byId('host-pill').setAttribute('aria-label', '当前目标：' + target + '，选择服务器');
  byId('identity').textContent = host ? 'SSH · ' + host : machineInfo ? machineInfo.username + '@' + machineInfo.hostname : '本机终端';
  byId('directory').textContent = host ? '远程服务器 · ' + host
    : currentView === 'files' ? fileState.path || machineInfo?.directory || '本机'
    : machineInfo?.directory || '~';
  byId('shell').textContent = currentView === 'files' ? '文件管理' : host ? 'ssh' : machineInfo?.shell || shellName;
}

function renderSessions() {
  const fragment = document.createDocumentFragment();
  for (const session of sessions) {
    const row = document.createElement('div');
    row.className = 'session-row' + (session === activeSession ? ' active' : '');
    row.dataset.state = session.state;
    const select = document.createElement('button');
    select.className = 'session-select';
    select.id = 'session-tab-' + session.id;
    select.dataset.selectSession = session.id;
    select.setAttribute('role', 'tab');
    select.setAttribute('aria-selected', String(session === activeSession));
    select.setAttribute('aria-controls', session.element.id);
    select.tabIndex = session === activeSession ? 0 : -1;
    const label = document.createElement('span');
    label.className = 'session-name';
    label.textContent = (session.host || session.program) + ' ' + session.id;
    label.title = session.host ? 'SSH · ' + session.host : '本机 · ' + session.program;
    const state = document.createElement('span');
    state.className = 'session-state';
    state.textContent = session.unread && session.state === 'connected' ? '有新输出' : session.statusText;
    if (session.host && session.state === 'connected') state.textContent = session.program + ' · ' + state.textContent;
    select.append(label, state);
    const close = document.createElement('button');
    close.className = 'session-close';
    close.dataset.closeSession = session.id;
    close.textContent = '×';
    close.title = '关闭 ' + session.program + ' ' + session.id + '，结束其中运行的命令';
    close.setAttribute('aria-label', '关闭终端 ' + session.id);
    row.append(select, close);
    fragment.append(row);
  }
  byId('session-list').replaceChildren(fragment);
  byId('session-count').textContent = sessions.length;
  byId('sidebar-empty').hidden = sessions.length > 0;
}

function send(session, data) {
  if (session && !session.removed && session.socket?.readyState === WebSocket.OPEN) {
    session.socket.send(JSON.stringify(data));
  }
}

function fit(session = activeSession) {
  if (!session || session.removed || session !== activeSession) return;
  if (!session.element.clientWidth || !session.element.clientHeight) return;
  session.fitter.fit();
  updateControls();
  send(session, { type: 'resize', cols: session.terminal.cols, rows: session.terminal.rows });
}

function activateSession(session) {
  activeSession = session;
  if (session && currentView === 'terminal') selectedHost = session.host;
  for (const item of sessions) item.element.classList.toggle('active', item === session);
  if (session) session.unread = false;
  renderSessions();
  updateControls();
  renderHostMenu();
  if (session) byId('session-tab-' + session.id)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  fit(session);
  if (session && currentView === 'terminal') {
    session.terminal.focus();
    requestAnimationFrame(() => fit(session));
  }
}

function setState(session, state, text) {
  if (session.removed) return;
  session.state = state;
  session.statusText = text;
  renderSessions();
  if (session === activeSession) updateControls();
}

function connectSession(session) {
  const terminal = session.terminal;
  const parameters = new URLSearchParams({ cols: terminal.cols, rows: terminal.rows });
  if (session.host) parameters.set('host', session.host);
  const socket = new WebSocket('/ws?' + parameters);
  socket.binaryType = 'arraybuffer';
  session.socket = socket;
  socket.onopen = () => {
    if (session.removed) return socket.close();
    setState(session, 'connected', session.host ? '终端就绪' : '已连接');
    fit(session);
  };
  socket.onmessage = event => {
    if (session.removed) return;
    if (event.data instanceof ArrayBuffer) {
      // Hidden terminals keep their own buffer and continue consuming output.
      terminal.write(new Uint8Array(event.data));
      if (session !== activeSession && !session.unread) {
        session.unread = true;
        renderSessions();
      }
    } else {
      try {
        const message = JSON.parse(event.data);
        if (message.type === 'process' && typeof message.name === 'string' && message.name && message.name !== session.program) {
          session.program = message.name;
          renderSessions();
          if (session === activeSession) updateControls();
        } else if (message.type === 'exit') {
          session.exited = true;
          setState(session, 'disconnected', '已退出');
          notify('终端 ' + session.id + ' 已退出，输出仍保留在侧栏中');
        }
      } catch (_) { /* Shell output uses binary frames. */ }
    }
  };
  socket.onerror = () => {
    setState(session, 'disconnected', '连接失败');
    if (!session.removed) notify('终端 ' + session.id + ' 连接失败，请检查服务及服务器配置');
  };
  socket.onclose = () => {
    if (session.state === 'disconnected') return;
    setState(session, 'disconnected', session.exited ? '已退出' : '已断开');
  };
}

function createSession(host = selectedHost) {
  if (sessions.length >= MAX_SESSIONS) return notify('最多保留 8 个终端，请先关闭一个会话');
  const id = nextSessionId++;
  const element = document.createElement('div');
  element.id = 'terminal-' + id;
  element.className = 'terminal-view';
  element.setAttribute('role', 'tabpanel');
  element.setAttribute('aria-labelledby', 'session-tab-' + id);
  byId('terminal').append(element);
  const terminal = new Terminal({
    cursorBlink: true, fontSize,
    fontFamily: '"Cascadia Code", "DejaVu Sans Mono", "Noto Sans Mono", monospace',
    scrollback: 8000, convertEol: false, theme: themes[currentTheme],
  });
  const fitter = new FitAddon.FitAddon();
  terminal.loadAddon(fitter);
  terminal.open(element);
  const session = { id, host, element, terminal, fitter, program: host ? 'ssh' : shellName, socket: null, state: 'connecting', statusText: '正在连接', unread: false, exited: false, removed: false };
  sessions.push(session);
  terminal.onData(data => send(session, { type: 'input', data }));
  terminal.onResize(({ cols, rows }) => {
    if (session === activeSession) updateControls();
    send(session, { type: 'resize', cols, rows });
  });
  terminal.attachCustomKeyEventHandler(event => {
    if (event.type !== 'keydown') return true;
    if (event.ctrlKey && event.shiftKey && event.code === 'KeyC') {
      byId('copy').click();
      return false;
    }
    if (event.ctrlKey && event.shiftKey && event.code === 'KeyV') {
      byId('paste').click();
      return false;
    }
    return true;
  });
  activateSession(session);
  connectSession(session);
}

function closeSession(session) {
  if (!session || session.removed) return;
  const index = sessions.indexOf(session);
  session.removed = true;
  session.socket?.close();
  session.terminal.dispose();
  session.element.remove();
  sessions.splice(index, 1);
  if (activeSession === session) activateSession(sessions[Math.min(index, sessions.length - 1)] || null);
  else renderSessions();
}

byId('session-list').onclick = event => {
  const select = event.target.closest('[data-select-session]');
  const close = event.target.closest('[data-close-session]');
  if (select) activateSession(sessions.find(session => session.id === Number(select.dataset.selectSession)));
  if (close) closeSession(sessions.find(session => session.id === Number(close.dataset.closeSession)));
};
const narrowScreen = matchMedia('(max-width: 700px)');
function updateSidebarOrientation() {
  byId('session-list').setAttribute('aria-orientation', narrowScreen.matches ? 'horizontal' : 'vertical');
}
updateSidebarOrientation();
narrowScreen.addEventListener('change', updateSidebarOrientation);
byId('session-list').onkeydown = event => {
  if (!event.target.matches('[data-select-session]')) return;
  const previous = narrowScreen.matches ? 'ArrowLeft' : 'ArrowUp';
  const next = narrowScreen.matches ? 'ArrowRight' : 'ArrowDown';
  if (![previous, next, 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const current = sessions.indexOf(activeSession);
  const index = event.key === 'Home' ? 0 : event.key === 'End' ? sessions.length - 1 : (current + (event.key === next ? 1 : -1) + sessions.length) % sessions.length;
  const session = sessions[index];
  activateSession(session);
  byId('session-tab-' + session.id).focus();
};

applyTheme(currentTheme);
byId('theme-toggle').onclick = () => applyTheme(currentTheme === 'dark' ? 'light' : 'dark');
byId('copy').onclick = async () => {
  const selected = activeSession?.terminal.getSelection();
  if (!selected) return notify('先选中需要复制的终端文本');
  try { await navigator.clipboard.writeText(selected); notify('已复制'); }
  catch (_) { notify('无法访问剪贴板，请使用浏览器复制功能'); }
};
byId('paste').onclick = async () => {
  const session = activeSession;
  try {
    const text = await navigator.clipboard.readText();
    if (!session || session.removed || session.state !== 'connected') return;
    session.terminal.paste(text);
    if (session === activeSession) session.terminal.focus();
  } catch (_) { notify('请允许剪贴板访问，或直接按 Ctrl+Shift+V 粘贴'); }
};
byId('clear').onclick = () => { activeSession?.terminal.clear(); activeSession?.terminal.focus(); };
byId('interrupt').onclick = () => { send(activeSession, { type: 'input', data: '\x03' }); activeSession?.terminal.focus(); };
function changeFont(delta) {
  fontSize = Math.max(10, Math.min(22, fontSize + delta));
  for (const session of sessions) session.terminal.options.fontSize = fontSize;
  fit();
}
byId('font-up').onclick = () => changeFont(1);
byId('font-down').onclick = () => changeFont(-1);
byId('new-session').onclick = () => createSession();

function renderHostMenu() {
  const list = byId('host-options');
  const focusedHost = document.activeElement?.closest('[data-host]')?.dataset.host;
  const fragment = document.createDocumentFragment();
  const entries = [{ alias: '', label: '本机', detail: machineInfo?.hostname || '本机终端' },
    ...configuredHosts.map(host => ({ alias: host.alias, label: host.alias, detail: 'SSH' }))];
  for (const host of entries) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'host-option';
    button.tabIndex = -1;
    button.dataset.host = host.alias;
    button.setAttribute('role', 'menuitemradio');
    button.setAttribute('aria-checked', String(host.alias === selectedHost));
    button.disabled = Boolean(host.alias && !sshAvailable);
    const labels = document.createElement('span');
    labels.className = 'host-option-labels';
    const name = document.createElement('span');
    name.className = 'host-option-name';
    name.textContent = host.label;
    const detail = document.createElement('span');
    detail.className = 'host-option-detail';
    detail.textContent = host.detail;
    const check = document.createElement('span');
    check.className = 'host-option-check';
    check.setAttribute('aria-hidden', 'true');
    check.textContent = host.alias === selectedHost ? '✓' : '';
    labels.append(name, detail);
    button.append(labels, check);
    fragment.append(button);
  }
  list.replaceChildren(fragment);
  if (focusedHost !== undefined) {
    const option = [...list.children].find(button => button.dataset.host === focusedHost && !button.disabled);
    (option || list.firstElementChild)?.focus();
  }
}

function closeHostMenu(restoreFocus = false) {
  byId('host-menu').hidden = true;
  byId('host-pill').setAttribute('aria-expanded', 'false');
  if (restoreFocus) byId('host-pill').focus();
}

function openHostMenu(edge = 'selected') {
  byId('host-menu').hidden = false;
  byId('host-pill').setAttribute('aria-expanded', 'true');
  const options = [...byId('host-options').querySelectorAll('button:not(:disabled)')];
  const selected = options.find(button => button.getAttribute('aria-checked') === 'true');
  (edge === 'last' ? options.at(-1) : edge === 'first' ? options[0] : selected || options[0])?.focus();
}

byId('host-pill').onclick = () => {
  if (byId('host-menu').hidden) openHostMenu();
  else closeHostMenu();
};
byId('host-options').onclick = event => {
  const option = event.target.closest('[data-host]');
  if (!option || option.disabled) return;
  const host = option.dataset.host;
  closeHostMenu(true);
  if (currentView === 'files') {
    selectedHost = host;
    updateControls();
    renderHostMenu();
    loadFileTarget();
    return;
  }
  const session = activeSession?.host === host && activeSession.state !== 'disconnected'
    ? activeSession
    : sessions.find(item => item.host === host && !item.removed && item.state !== 'disconnected');
  if (session) activateSession(session);
  else createSession(host);
};
byId('host-switcher').onkeydown = event => {
  if (event.key === 'Escape') {
    event.preventDefault();
    closeHostMenu(true);
    return;
  }
  if (event.target === byId('host-pill') && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
    event.preventDefault();
    openHostMenu(event.key === 'ArrowUp' ? 'last' : 'first');
    return;
  }
  if (!event.target.matches('[data-host]') || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const options = [...byId('host-options').querySelectorAll('button:not(:disabled)')];
  const index = options.indexOf(event.target);
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
  options[next]?.focus();
};
document.addEventListener('pointerdown', event => {
  if (!byId('host-switcher').contains(event.target)) closeHostMenu();
});
document.addEventListener('focusin', event => {
  if (!byId('host-switcher').contains(event.target)) closeHostMenu();
});

async function refreshHosts() {
  const button = byId('refresh-hosts');
  const status = byId('hosts-status');
  const restoreFocus = document.activeElement === button;
  button.disabled = true;
  status.textContent = '正在读取服务器列表…';
  try {
    const response = await fetch('/api/hosts');
    if (!response.ok) throw new Error(await response.text());
    const data = await response.json();
    configuredHosts = data.hosts;
    sshAvailable = data.ssh_available;
    renderHostMenu();
    status.textContent = !data.ssh_available ? '本机未安装 SSH 客户端' : data.hosts.length ? data.hosts.length + ' 台已配置服务器' : '暂无服务器，请在 SSH 配置中添加 Host';
  } catch (error) {
    status.textContent = '服务器列表读取失败';
    notify(error.message || '无法读取服务器配置');
  } finally {
    button.disabled = false;
    if (restoreFocus && !byId('host-menu').hidden && document.activeElement === document.body) button.focus();
  }
}
byId('refresh-hosts').onclick = refreshHosts;
new ResizeObserver(() => requestAnimationFrame(() => fit())).observe(byId('terminal'));
window.addEventListener('pagehide', () => {
  for (const session of sessions) session.socket?.close();
});

fetch('/api/info').then(response => {
  if (!response.ok) throw new Error('Cannot read machine information');
  return response.json();
}).then(info => {
  machineInfo = info;
  shellName = info.shell.split('/').pop();
  renderSessions();
  updateControls();
  renderHostMenu();
}).catch(() => { updateMachineInfo(); });

refreshHosts();
createSession('');
