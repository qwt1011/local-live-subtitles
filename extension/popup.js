const enabled = document.querySelector('#enabled');
const language = document.querySelector('#language');
const mode = document.querySelector('#mode');
const status = document.querySelector('#status');
let capturing = false;
chrome.storage.local.get({enabled: false, language: 'en', mode: 'bilingual'}, (value) => { enabled.checked = value.enabled; language.value = value.language; mode.value = value.mode; });
language.addEventListener('change', () => chrome.storage.local.set({language: language.value}));
mode.addEventListener('change', async () => {
  await chrome.storage.local.set({mode: mode.value});
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  if (tab?.id) chrome.tabs.sendMessage(tab.id, {type: 'mode', mode: mode.value});
});
enabled.addEventListener('change', async () => {
  await chrome.storage.local.set({enabled: enabled.checked});
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  if (tab?.id) chrome.tabs.sendMessage(tab.id, {type: 'toggle', enabled: enabled.checked});
});
document.querySelector('#health').addEventListener('click', async () => {
  try { const response = await fetch('http://127.0.0.1:8765/health'); status.textContent = response.ok ? '本地服务已连接' : '服务返回错误'; }
  catch { status.textContent = '未连接本地服务'; }
});
document.querySelector('#capture').addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  if (!tab?.id) return;
  capturing = !capturing;
  chrome.runtime.sendMessage({type: capturing ? 'start-capture' : 'stop-capture', tabId: tab.id, language: language.value});
  status.textContent = capturing ? '正在捕获当前标签页音频' : '已停止捕获';
});
