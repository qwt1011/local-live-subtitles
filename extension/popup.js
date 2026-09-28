/**
 * Popup：设置 + 连接状态 + 起停捕获。
 *
 * 几个踩过的坑，都体现在这里：
 *
 * 1. **服务探测必须独立于采集**。早期版本读的是 offscreen 写的 captureStatus，
 *    而 offscreen 只有点过「开始捕获」后才存在，于是服务明明在跑也显示"未连接"。
 *    现在用 Shared.probeService() 自己连一次。
 *
 * 2. **每秒刷新会把错误信息冲掉**。render() 原本每秒重写 statusBox，
 *    点「开始捕获」失败时写进去的报错不到一秒就被覆盖，用户于是"什么也没看见"。
 *    现在错误存在 lastError 里，由 render() 一并画出来，直到下一次操作才清。
 *
 * 3. **streamId 在 popup 里取**。popup 有确定的用户手势，而 tabCapture
 *    对这个手势敏感（service worker 里隔了几次 await 之后可能已经失效）。
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
let lastError = '';      // 最近一次操作的失败原因，由 render() 一并显示

const DEFAULTS = { enabled: true, language: 'ja', mode: 'bilingual' };

function render() {
  chrome.storage.session.get(['captureStatus']).then((stored) => {
    const capture = stored.captureStatus;
    const lines = [];

    // 服务在线与否
    if (probe) {
      lines.push(`● 本地服务已连接（${probe.engine || '?'} / ${probe.model || '?'}）`);
    } else {
      lines.push('○ 未连接本地服务');
      lines.push(`  地址：${Shared.WS_URL}`);
    }

    // 采集状态
    if (capture) {
      if (capture.error) {
        lines.push(`✗ ${capture.error}`);
      } else if (capture.state === 'starting') {
        lines.push(`采集：正在启动…（${capture.step || ''}）`);
      } else if (capture.state === 'listening' || capture.connected) {
        lines.push(`采集：进行中（已接收 ${capture.audioSeconds ?? 0} 秒音频）`);
      } else if (capture.state === 'connecting') {
        lines.push('采集：正在连接本地服务…');
      } else if (capture.state === 'stopped') {
        lines.push('采集：已停止');
      }
      if (typeof capture.lastLatency === 'number') {
        lines.push(`最近一条延迟：${capture.lastLatency.toFixed(2)} 秒`
          + `（第 ${capture.lastSegment} 句${capture.lastIsFinal ? '，已定稿' : '，草稿'}）`);
      }
    } else if (capturing) {
      lines.push('采集：等待音频…');
    }

    if (lastError) lines.push(`✗ ${lastError}`);

    const bad = Boolean(lastError) || (capture && capture.error) || !probe;
    statusBox.className = bad ? 'bad' : 'ok';
    statusBox.textContent = lines.join('\n');
  });
}

async function runProbe() {
  if (probing) return;
  probing = true;
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
  lastError = '';
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) {
    lastError = '找不到当前标签页';
    render();
    return;
  }

  const starting = !capturing;

  if (!starting) {
    capturing = false;
    renderCaptureButton();
    await chrome.storage.session.set({ capturing });
    await chrome.runtime.sendMessage({ type: 'stop-capture' })
      .catch((error) => ({ ok: false, error: String(error) }));
    render();
    return;
  }

  // 在 popup 里取 streamId：这里用户手势是确定的。
  let streamId = null;
  try {
    streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
  } catch (error) {
    // 不直接失败：让 background 也试一次（不同 Chrome 版本行为不一致）。
    console.warn('[本地字幕] popup 取 streamId 失败，交给 background 重试：', error);
  }

  const response = await chrome.runtime.sendMessage({
    type: 'start-capture',
    tabId: tab.id,
    streamId,
    language: languageInput.value,
  }).catch((error) => ({ ok: false, error: String(error) }));

  if (response && response.ok === false) {
    lastError = `启动失败：${response.error}`;
    render();
    return;    // 失败时不把状态切成"正在捕获"
  }

  capturing = true;
  renderCaptureButton();
  await chrome.storage.session.set({ capturing });
  setTimeout(runProbe, 600);
  setTimeout(render, 900);
});

pingButton.addEventListener('click', runProbe);

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'session' && changes.captureStatus) render();
});

runProbe();                        // 打开 popup 就独立探测一次
setInterval(() => { if (!probing) render(); }, 1000);
