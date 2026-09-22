/**
 * Popup：设置 + 连接状态 + 起停捕获。
 *
 * 状态来自 chrome.storage.session 里的 captureStatus（由 offscreen 写入），
 * 不是靠内存变量——service worker 被回收后 popup 仍然能显示真实状态。
 */

const Shared = self.SubtitleShared;

const enabledInput = document.querySelector('#enabled');
const languageInput = document.querySelector('#language');
const modeInput = document.querySelector('#mode');
const captureButton = document.querySelector('#capture');
const pingButton = document.querySelector('#ping');
const statusBox = document.querySelector('#status');

let capturing = false;

const DEFAULTS = { enabled: true, language: 'ja', mode: 'bilingual' };

function renderStatus(status) {
  if (!status) {
    statusBox.className = 'muted';
    statusBox.textContent = '未连接本地服务';
    return;
  }
  const lines = [];
  if (status.connected) {
    lines.push(`● 已连接（${status.state || 'ok'}）`);
  } else {
    lines.push('○ 未连接本地服务');
  }
  if (status.server) {
    lines.push(`引擎：${status.server.engine || '?'} / ${status.server.model || '?'}`);
  }
  if (typeof status.audioSeconds === 'number') {
    lines.push(`已接收音频：${status.audioSeconds} 秒`);
  }
  if (typeof status.lastLatency === 'number') {
    lines.push(`最近一条延迟：${status.lastLatency.toFixed(2)} 秒`
      + `（第 ${status.lastSegment} 句${status.lastIsFinal ? '，已定稿' : '，草稿'}）`);
  }
  if (status.error) lines.push(status.error);
  statusBox.className = status.connected ? 'ok' : 'bad';
  statusBox.textContent = lines.join('\n');
}

async function refreshStatus() {
  const stored = await chrome.storage.session.get(['captureStatus']);
  renderStatus(stored.captureStatus);
}

function renderCaptureButton() {
  captureButton.textContent = capturing ? '停止捕获' : '开始捕获当前标签页';
}

chrome.storage.local.get(DEFAULTS, (value) => {
  enabledInput.checked = value.enabled;
  languageInput.value = value.language;
  modeInput.value = value.mode;
});

chrome.storage.session.get(['capturing']).then((stored) => {
  capturing = Boolean(stored.capturing);
  renderCaptureButton();
});

enabledInput.addEventListener('change', () => {
  chrome.storage.local.set({ enabled: enabledInput.checked });
});

languageInput.addEventListener('change', () => {
  chrome.storage.local.set({ language: languageInput.value });
});

modeInput.addEventListener('change', () => {
  // content script 监听 storage.onChanged，所以这里不需要再发消息
  // （消息在 service worker 被回收时会丢，storage 事件不会）。
  chrome.storage.local.set({ mode: modeInput.value });
});

captureButton.addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) {
    statusBox.className = 'bad';
    statusBox.textContent = '找不到当前标签页';
    return;
  }
  capturing = !capturing;
  renderCaptureButton();
  await chrome.storage.session.set({ capturing });
  const response = await chrome.runtime.sendMessage({
    type: capturing ? 'start-capture' : 'stop-capture',
    tabId: tab.id,
    language: languageInput.value,
  });
  if (response && response.ok === false) {
    statusBox.className = 'bad';
    statusBox.textContent = `操作失败：${response.error}`;
  }
  setTimeout(refreshStatus, 700);
});

pingButton.addEventListener('click', async () => {
  // 走 background 转发（popup 直接广播会同时打给 background 和 offscreen，
  // 谁先 sendResponse 不确定）。
  await chrome.runtime.sendMessage({ type: 'ping-service' }).catch(() => {});
  await refreshStatus();
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'session' && changes.captureStatus) {
    renderStatus(changes.captureStatus.newValue);
  }
});

refreshStatus();
setInterval(refreshStatus, 1000);
