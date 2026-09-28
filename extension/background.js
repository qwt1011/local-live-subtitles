/**
 * Background service worker：只做路由与状态保管，不碰音频。
 *
 * 关键改动：活动标签页 id 存进 chrome.storage.session。
 * 原来的实现放在内存变量里，service worker 一旦被回收（MV3 里很常见），
 * activeTabId 就丢了，字幕会**静默停止**且不会恢复（ARCHITECTURE_REVIEW.md S6）。
 */

const SESSION_KEYS = { activeTabId: 'activeTabId', capturing: 'capturing' };

async function setState(patch) {
  await chrome.storage.session.set(patch);
}

async function getState() {
  return chrome.storage.session.get([SESSION_KEYS.activeTabId, SESSION_KEYS.capturing]);
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  // 用 async IIFE 而不是让 listener 变成 async：
  // async listener 返回的 Promise 不会被 Chrome 等待，sendResponse 会失效。
  (async () => {
    try {
      if (message.type === 'start-capture') {
        await startCapture(message.tabId, message.language);
        sendResponse({ ok: true });
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
        await chrome.runtime.sendMessage({ type: 'offscreen-ping' }).catch(() => {});
        sendResponse({ ok: true, status: (await chrome.storage.session.get(['captureStatus'])).captureStatus });
      } else if (message.type === 'query-state') {
        sendResponse(await getState());
      }
    } catch (error) {
      sendResponse({ ok: false, error: String(error) });
    }
  })();
  return true; // 保持消息通道打开，等待异步 sendResponse
});

async function startCapture(tabId, language) {
  await stopCapture();
  await setState({ [SESSION_KEYS.activeTabId]: tabId, [SESSION_KEYS.capturing]: true });

  await ensureOffscreenDocument();

  const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });
  console.log('[本地字幕] 已取得标签页音频流，交给 offscreen');
  await chrome.runtime.sendMessage({ type: 'offscreen-start', streamId, language }).catch(() => {});
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
  await chrome.runtime.sendMessage({ type: 'offscreen-stop' }).catch(() => {});
}

async function relayToTab(message) {
  const state = await getState();
  const tabId = state[SESSION_KEYS.activeTabId];
  if (!tabId) return;
  // 标签页可能已经关闭或跳转，失败是正常情况。
  await chrome.tabs.sendMessage(tabId, message).catch(() => {});
}
