let activeTabId = null;
let activeLanguage = 'en';
chrome.runtime.onMessage.addListener(async (message) => {
  if (message.type === 'start-capture') {
    activeTabId = message.tabId;
    activeLanguage = message.language || 'en';
    await chrome.offscreen.createDocument({url: 'offscreen.html', reasons: ['USER_MEDIA'], justification: 'Capture tab audio for local subtitles'}).catch(() => {});
    const streamId = await chrome.tabCapture.getMediaStreamId({targetTabId: activeTabId});
    chrome.runtime.sendMessage({type: 'offscreen-start', streamId, language: activeLanguage});
  }
  if (message.type === 'stop-capture') chrome.runtime.sendMessage({type: 'offscreen-stop'});
  if (message.type === 'subtitle' && activeTabId) chrome.tabs.sendMessage(activeTabId, message).catch(() => {});
});
