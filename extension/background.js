/**
 * Background service worker：只做路由与状态保管，不碰音频。
 *
 * 两个关键点，都是踩过坑之后写下来的：
 *
 * 1. **活动标签页 id 存进 chrome.storage.session**。原来放在内存变量里，
 *    service worker 一旦被回收（MV3 里很常见），activeTabId 就丢了，
 *    字幕会静默停止且不会恢复。
 *
 * 2. **给 offscreen 发消息必须重试**。`chrome.offscreen.createDocument()` 返回时
 *    文档只是"被创建"，里面的 JS 还没执行完、消息监听器还没注册。
 *    紧接着发消息会得到 "Receiving end does not exist"，而这个失败如果被
 *    `.catch(() => {})` 吞掉，表现就是"点了开始捕获，什么都没发生，也没有任何报错"。
 *    这正是用户实际遇到的现象，所以这里用 sendToOffscreen() 带重试。
 */

// service worker 不会自动加载 shared.js（manifest 的 content_scripts 只管页面），
// 必须显式 importScripts，否则下面 self.SubtitleShared 是 undefined。
// popup.html / offscreen.html 各自用 <script src="shared.js"> 加载，双向都要照顾到。
importScripts('shared.js');

const Shared = self.SubtitleShared;
const store = Shared.createStore('background');
Shared.describeEnvironment('background');

const SESSION_KEYS = { activeTabId: 'activeTabId', capturing: 'capturing' };

async function setState(patch) {
  await store.set(patch);
  if (SESSION_KEYS.capturing in patch) setBadge(Boolean(patch[SESSION_KEYS.capturing]));
}

/** 工具栏图标角标：字幕进行中时显示，不打开弹窗也知道在跑。 */
function setBadge(on) {
  try {
    chrome.action.setBadgeText({ text: on ? 'ON' : '' });
    if (on) chrome.action.setBadgeBackgroundColor({ color: '#2f6fed' });
  } catch (error) {
    console.warn('[本地字幕] 设置角标失败（已忽略）：', error);
  }
}

async function getState() {
  return store.get([SESSION_KEYS.activeTabId, SESSION_KEYS.capturing]);
}

// 快捷键 Alt+S：让当前标签页的浮窗显示/隐藏（浮窗只在字幕进行中的标签页存在）
chrome.commands.onCommand.addListener(async (command) => {
  if (command !== 'toggle-panel') return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab && tab.id) chrome.tabs.sendMessage(tab.id, { type: 'toggle-panel' }).catch(() => {});
});

// popup 也会直接写 capturing（停止、服务闲置退出后的纠正）：角标跟着 storage 走，而不是只跟 setState。
// 同时通知网页里的浮窗（panel.js）：content script 默认读不到 storage.session，所以由这里转告。
chrome.storage.onChanged.addListener(async (changes, area) => {
  if (area !== 'session' || !(changes.capturing || changes.activeTabId || changes.captureStatus)) return;
  if (changes.capturing) setBadge(Boolean(changes.capturing.newValue));
  const state = await getState();
  const tabs = new Set([state[SESSION_KEYS.activeTabId]]);
  if (changes.activeTabId && changes.activeTabId.oldValue) tabs.add(changes.activeTabId.oldValue);
  const status = changes.captureStatus ? changes.captureStatus.newValue : undefined;
  for (const tabId of tabs) {
    if (!tabId) continue;
    chrome.tabs.sendMessage(tabId, { type: 'capture-state', state, status }).catch(() => {});
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  // 用 async IIFE 而不是让 listener 变成 async：
  // async listener 返回的 Promise 不会被 Chrome 等待，sendResponse 会失效。
  (async () => {
    try {
      if (message.type === 'start-capture') {
        sendResponse(await startCapture(message.tabId, message.language, message.streamId,
          message.options));
      } else if (message.type === 'stop-capture') {
        await stopCapture();
        sendResponse({ ok: true });
      } else if (message.type === 'subtitle-event') {
        await relayToTab({ type: 'subtitle', payload: message.payload });
        sendResponse({ ok: true });
      } else if (message.type === 'capture-status') {
        await relayToTab({ type: 'status', status: message.status });
        sendResponse({ ok: true });
      } else if (message.type === 'ping-service') {
        // 统一由 background 转发，避免 popup 直接广播时多个监听器抢答。
        await sendToOffscreen({ type: 'offscreen-ping' }, { retries: 1 });
        sendResponse({ ok: true });
      } else if (message.type === 'query-state') {
        sendResponse(await getState());
      } else if (message.type === 'whoami') {
        // 浮窗 iframe 问"我在哪个标签页里"：sender.tab 就是嵌着它的那一页
        sendResponse(_sender.tab ? { id: _sender.tab.id, url: _sender.tab.url, title: _sender.tab.title } : null);
      }
    } catch (error) {
      sendResponse({ ok: false, error: String(error) });
    }
  })();
  return true; // 保持消息通道打开，等待异步 sendResponse
});

/**
 * 给 offscreen 发消息，带重试。
 *
 * 这是修"点了开始捕获却没反应"的关键：offscreen 文档的脚本是异步加载的，
 * 刚创建完就发消息会打空。重试到它把监听器注册好为止。
 */
async function sendToOffscreen(message, { retries = 30, delayMs = 100 } = {}) {
  let lastError = null;
  for (let attempt = 0; attempt < retries; attempt += 1) {
    try {
      const response = await chrome.runtime.sendMessage(message);
      if (response) return response;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, delayMs));
  }
  console.error('[本地字幕] offscreen 未就绪，消息无人接收：', message.type, lastError);
  return { ok: false, error: `offscreen 文档未就绪（${message.type} 无人接收）` };
}

async function startCapture(tabId, language, providedStreamId, options) {
  await stopCapture();
  await setState({ [SESSION_KEYS.activeTabId]: tabId, [SESSION_KEYS.capturing]: true });

  await ensureOverlay(tabId);
  await ensureOffscreenDocument();

  // 优先用 popup 传来的 streamId：popup 有确定的用户手势，
  // 而 tabCapture 对这个手势敏感（service worker 里隔了几次 await 后可能失效）。
  let streamId = providedStreamId;
  if (!streamId) {
    try {
      streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });
    } catch (error) {
      return { ok: false, error: `取标签页音频流失败：${error}` };
    }
  }
  console.log('[本地字幕] 已取得标签页音频流，交给 offscreen');

  const response = await sendToOffscreen({ type: 'offscreen-start', streamId, language, options });
  if (response && response.ok === false) return response;
  return { ok: true };
}

/**
 * 确保标签页里有字幕脚本，没有就现场注入。
 *
 * 09-30 用户实测：本地 mp4 页面音频照常识别翻译（服务端日志 15 句定稿），页面上却没有字幕。
 * 原因是 Chrome 在"打开允许访问文件网址"或重新加载扩展时，不会给**已经打开**的标签页补注入
 * content script，必须刷新页面；而 relayToTab 会吞掉"没人接收"的错误，所以毫无提示。
 * 现在开始捕获时先探测一次，探测不到就用 chrome.scripting 注入，不再依赖用户刷新。
 */
async function ensureOverlay(tabId) {
  const alive = await chrome.tabs.sendMessage(tabId, { type: 'overlay-ping' }).catch(() => null);
  if (alive && alive.ok) return;
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ['shared.js', 'content.js', 'panel.js'] });
    console.log('[本地字幕] 标签页里没有字幕脚本，已补注入');
  } catch (error) {
    // 例如 chrome:// 页面、或本地文件但没开「允许访问文件网址」：采集照常进行，只是没有覆盖层。
    console.warn('[本地字幕] 无法注入字幕脚本：', error);
  }
}

/**
 * 确保 offscreen 文档存在。
 *
 * 优先用 hasDocument()（Chrome 116+）；老版本没有这个 API，
 * 就直接尝试创建并吞掉"已存在"的错误，而不是让整个流程失败。
 */
async function ensureOffscreenDocument() {
  if (typeof chrome.offscreen.hasDocument === 'function') {
    const has = await chrome.offscreen.hasDocument().catch(() => false);
    if (has) return;
  }
  try {
    await chrome.offscreen.createDocument({
      url: 'offscreen.html',
      reasons: ['USER_MEDIA'],
      justification: '捕获标签页音频用于本地字幕',
    });
  } catch (error) {
    // 已经存在时 createDocument 会抛错，这是正常情况，不是失败。
    if (!String(error).includes('Only a single offscreen')) throw error;
  }
}

async function stopCapture() {
  await setState({ [SESSION_KEYS.capturing]: false });
  // 停止时不重试：offscreen 可能压根没创建过，等它没意义。
  await sendToOffscreen({ type: 'offscreen-stop' }, { retries: 1 });
}

async function relayToTab(message) {
  const state = await getState();
  const tabId = state[SESSION_KEYS.activeTabId];
  if (!tabId) return;
  // 标签页可能已经关闭或跳转，失败是正常情况。
  await chrome.tabs.sendMessage(tabId, message).catch(() => {});
}
