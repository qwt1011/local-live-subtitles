/**
 * Popup：设置 + 连接状态 + 起停捕获。
 *
 * 状态有两个来源，必须分开看：
 *
 * 1. **本地服务是否在线** —— 由 popup **自己直接探测**（`Shared.probeService()`）。
 *    不能靠 offscreen 写的 `captureStatus`：offscreen 文档只有在点了「开始捕获」之后
 *    才会被创建，在那之前它根本不存在，于是 popup 会一直显示"未连接"，
 *    哪怕服务跑得好好的。（第一版就是这么写的，"检查本地服务"按钮因此形同虚设。）
 * 2. **采集会话状态** —— 来自 `chrome.storage.session` 的 `captureStatus`（由 offscreen 写），
 *    不是内存变量：service worker 被回收后 popup 仍能显示真实状态。
 */

const Shared = self.SubtitleShared;

const enabledInput = document.querySelector('#enabled');
const languageInput = document.querySelector('#language');
const modeInput = document.querySelector('#mode');
const captureButton = document.querySelector('#capture');
const pingButton = document.querySelector('#ping');
const statusBox = document.querySelector('#status');

let capturing = false;
let probe = null;        // 上一次探测结果：服务端 status 对象，或 null
let probing = false;

const DEFAULTS = { enabled: true, language: 'ja', mode: 'bilingual' };

function render() {
  statusBox.className = probe ? 'ok' : 'bad';

  const lines = [];
  if (probe) {
    lines.push(`● 本地服务已连接（${probe.engine || '?'} / ${probe.model || '?'}）`);
  } else {
    lines.push('○ 未连接本地服务');
    lines.push(`  地址：${Shared.WS_URL}`);
    lines.push('  请确认服务已启动（见 extension/README.md）');
  }

  chrome.storage.session.get(['captureStatus']).then((stored) => {
    const capture = stored.captureStatus;
    if (capture) {
      if (capture.state === 'listening' || capture.connected) {
        lines.push(`采集：进行中（已接收 ${capture.audioSeconds ?? 0} 秒音频）`);
      } else if (capture.state === 'connecting') {
        lines.push('采集：正在连接…');
      } else if (capture.state === 'stopped') {
        lines.push('采集：已停止');
      }
      if (typeof capture.lastLatency === 'number') {
        lines.push(`最近一条延迟：${capture.lastLatency.toFixed(2)} 秒`
          + `（第 ${capture.lastSegment} 句${capture.lastIsFinal ? '，已定稿' : '，草稿'}）`);
      }
      if (capture.error) lines.push(`采集错误：${capture.error}`);
    } else if (capturing) {
      lines.push('采集：等待音频…');
    }
    statusBox.textContent = lines.join('\n');
  });
}

async function runProbe() {
  if (probing) return;
  probing = true;
  statusBox.className = 'muted';
  statusBox.textContent = '正在检查本地服务…';
  // 独立探测：连一次 WebSocket 并发 ping，不依赖 offscreen 是否已创建。
  probe = await Shared.probeService();
  probing = false;
  render();
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
  }).catch((error) => ({ ok: false, error: String(error) }));
  if (response && response.ok === false) {
    statusBox.className = 'bad';
    statusBox.textContent = `操作失败：${response.error}`;
    return;
  }
  setTimeout(runProbe, 800);
});

pingButton.addEventListener('click', runProbe);

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'session' && changes.captureStatus) render();
});

runProbe();                        // 打开 popup 就独立探测一次
setInterval(() => { if (!probing) render(); }, 1000);
