/**
 * Popup：状态 + 一个主按钮 + 设置。
 *
 * 10-01 重做（原来有独立的"检查本地服务"按钮，而状态行本身就会探测，两者重复）：
 * - 状态每 2 秒自动探测，出问题才变红并给出下一步；
 * - 主按钮按状态切换：服务没开 → 一键启动（经 Native Messaging，见 tools/native_host.py）再开始字幕；
 * - 识别模型可在这里切换，服务在跑时需要重启服务。
 *
 * 沿用的老经验：
 * 1. 服务探测独立于采集（offscreen 只在开始捕获后才存在）；
 * 2. 错误存在 lastError 里由 render() 统一画，不会被定时刷新冲掉；
 * 3. streamId 在 popup 里取（这里有确定的用户手势），background 只在失败时兜底。
 */

const Shared = self.SubtitleShared;
const store = Shared.createStore('popup');
// 同一个页面也作为网页里的浮窗内容（content.js 用 iframe 嵌入 popup.html?embedded=1）。
// 嵌入时"当前标签页"是浮窗所在的那一页，而不是浏览器里正在看的那一页。
const EMBEDDED = new URLSearchParams(location.search).has('embedded');
if (EMBEDDED) document.documentElement.classList.add('embedded');
const HOST = 'local.live_subtitles';

const $ = (selector) => document.querySelector(selector);
const ui = {
  enabled: $('#enabled'),
  panelShown: $('#panelShown'),
  statusDot: $('#statusDot'),
  statusTitle: $('#statusTitle'),
  statusDetail: $('#statusDetail'),
  problem: $('#problem'),
  primary: $('#primary'),
  primaryLabel: $('#primaryLabel'),
  sessionLine: $('#sessionLine'),
  asr: $('#asr'),
  asrHint: $('#asrHint'),
  earlyFinal: $('#earlyFinal'),
  adaptiveSilence: $('#adaptiveSilence'),
  addressConsistency: $('#addressConsistency'),
  resetAddresses: $('#resetAddresses'),
  idle: $('#idle'),
  viewLog: $('#viewLog'),
  stopService: $('#stopService'),
  hostHint: $('#hostHint'),
};

const DEFAULTS = {
  enabled: true, panelHidden: false, panelOpen: false, language: 'ja', mode: 'bilingual', asr: 'parakeet', enAsr: 'parakeet', idleMinutes: 30,
  earlyFinal: false, adaptiveSilence: false, addressConsistency: false,
};

// 识别模型按语言分开选：日语和英语各记一个（asr / enAsr），服务启动时两个都传过去，
// 之后在弹窗里切语言不用重启服务（服务端按会话语言选引擎，见 app/server.py EnginePool）。
const ASR_OPTIONS = {
  ja: [
    ['parakeet', 'Parakeet · 更准', '日语专用，识别更准；CPU 占用约为 SenseVoice 的 3 倍'],
    ['sensevoice', 'SenseVoice · 更快', '多语言，最快最省；日语错字比 Parakeet 多'],
    ['hybrid', '混合 · 实验', '草稿用 SenseVoice，定稿用 Parakeet（实验，实测未见明显收益）'],
  ],
  en: [
    ['parakeet', 'Parakeet · 更准', '英语版 Parakeet：词错率 0.073（SenseVoice 0.102）；CPU 约 2 倍'],
    ['sensevoice', 'SenseVoice · 更快', '多语言，最快最省；英语错词比 Parakeet 多约 40%'],
  ],
};
const ASR_NAMES = { parakeet: 'Parakeet', sensevoice: 'SenseVoice', hybrid: '混合' };
const ASR_KEY = { ja: 'asr', en: 'enAsr' };   // 每种语言的选择存在哪个设置里

function asrHint(language, value) {
  const row = ASR_OPTIONS[language].find(([key]) => key === value);
  return row ? row[2] : '';
}

/** 按当前语言重建识别下拉框的选项。 */
function renderAsrOptions() {
  const language = settings.language;
  const wanted = settings[ASR_KEY[language]];
  ui.asr.replaceChildren(...ASR_OPTIONS[language].map(([value, label]) => new Option(label, value)));
  ui.asr.value = ASR_OPTIONS[language].some(([key]) => key === wanted) ? wanted : 'parakeet';
}
const TRANSLATE_NAMES = { hymt: 'Hy-MT2', instruct: 'Qwen2.5' };

const settings = { ...DEFAULTS };
const view = {
  probe: undefined,       // 服务端 status；null = 连不上；undefined = 还没探测过
  host: undefined,        // native 宿主 status；null = 未安装
  capturing: false,
  capture: null,          // offscreen 上报的 captureStatus
  busy: '',               // 正在进行的操作说明（启动服务、重启…），非空时主按钮转圈
  lastError: '',
  tab: null,
  captureTab: null,       // 正在捕获的标签页（可能不是当前页）
  fileAccessMissing: false,
};

// --- 小工具 -------------------------------------------------------------------

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** 调 native 宿主；未安装 / 路径失效时返回 null，而不是抛错。 */
async function callHost(message) {
  try {
    return await chrome.runtime.sendNativeMessage(HOST, message);
  } catch (error) {
    console.warn('[本地字幕] 一键启动宿主不可用：', error);
    return null;
  }
}

/** 服务当前对某种语言用的识别（下拉框的值）。日语看主引擎，英语看 en_engine。 */
function asrOf(probe, language = settings.language) {
  if (!probe) return null;
  if (language === 'en') {
    if (!probe.en_engine) return null;   // 老版本服务没报这个字段
    return probe.en_engine === 'sherpa' ? 'parakeet' : 'sensevoice';
  }
  if (probe.final_engine === 'sherpa') return 'hybrid';
  if (probe.engine === 'sherpa') return 'parakeet';
  if (probe.engine === 'sensevoice') return 'sensevoice';
  return null;
}

/** 服务的识别配置是否和两种语言的设置都一致。 */
function serviceMatches(probe) {
  const en = asrOf(probe, 'en');
  return asrOf(probe, 'ja') === settings.asr && (en === null || en === settings.enAsr);
}

function formatSeconds(total) {
  const seconds = Math.max(0, Math.round(total || 0));
  const m = Math.floor(seconds / 60);
  const s = String(seconds % 60).padStart(2, '0');
  return `${m}:${s}`;
}

/** 正在捕获，但捕获的不是当前这一页。 */
function capturingElsewhere() {
  return Boolean(view.capturing && view.captureTab && view.tab && view.captureTab.id !== view.tab.id);
}

function shortTitle(title) {
  const text = (title || '未命名').replace(/ - YouTube$/, '');
  return text.length > 18 ? `${text.slice(0, 18)}…` : text;
}

function capturableTab(tab) {
  return Boolean(tab && tab.url && /^(https?|file):/.test(tab.url));
}

// --- 渲染 ---------------------------------------------------------------------

function setStatus(tone, title, detail) {
  ui.statusDot.className = `dot ${tone}`;
  ui.statusTitle.textContent = title;
  ui.statusDetail.textContent = detail || '';
}

function render() {
  const addressSupported = Boolean(view.probe && view.probe.address_consistency_supported);
  ui.addressConsistency.disabled = Boolean(view.probe && !addressSupported);
  ui.addressConsistency.title = view.probe && !addressSupported ? '请先重启字幕服务以使用称呼一致性' : '下次开始字幕时生效';
  ui.resetAddresses.disabled = !view.capturing || !addressSupported;
  const { probe, host, capture, busy } = view;
  const running = Boolean(probe);
  const runningAsr = asrOf(probe);

  // 状态卡
  if (busy) {
    setStatus('warn', busy, '首次加载模型约需 10–40 秒');
  } else if (probe === undefined) {
    setStatus('', '正在检查本地服务…', '');
  } else if (running) {
    const translate = probe.translate
      ? (TRANSLATE_NAMES[probe.translate_engine] || '翻译已开') : '未开翻译';
    const where = view.capturing && capturingElsewhere() && view.captureTab
      ? `正在捕获另一个标签页：${shortTitle(view.captureTab.title)}` : '';
    setStatus('ok', view.capturing ? '字幕进行中' : '服务运行中',
      where || `${ASR_NAMES[runningAsr] || probe.model || '?'} · ${translate}`);
  } else {
    setStatus('', '服务未启动',
      host ? '点下方按钮会自动启动' : '一键启动未安装，需先手动运行启动脚本');
  }

  // 问题提示：只放需要用户动手的事
  const problems = [];
  if (view.lastError) problems.push(view.lastError);
  if (capture && capture.error && view.capturing) problems.push(capture.error);
  if (view.fileAccessMissing) {
    problems.push('本地文件未授权：打开 chrome://extensions → 本扩展「详细信息」→ 打开「允许访问文件网址」，然后刷新页面');
  }
  if (!running && host === null && probe !== undefined && !busy) {
    problems.push('想在这里一键启动：运行项目里的「安装一键启动.bat」，然后重新加载扩展。\n'
      + '或者先手动运行「启动字幕服务.bat」。');
  }
  ui.problem.hidden = problems.length === 0;
  ui.problem.textContent = problems.join('\n\n');

  // 主按钮
  const button = ui.primary;
  button.classList.toggle('busy', Boolean(busy));
  button.classList.toggle('stop', view.capturing && !busy);
  if (busy) {
    ui.primaryLabel.textContent = busy;
    button.disabled = true;
  } else if (view.capturing && capturingElsewhere()) {
    ui.primaryLabel.textContent = '⇄ 切换到这个标签页';
    button.disabled = !capturableTab(view.tab);
  } else if (view.capturing) {
    ui.primaryLabel.textContent = '■ 停止字幕';
    button.disabled = false;
  } else if (!capturableTab(view.tab)) {
    ui.primaryLabel.textContent = '这个页面无法捕获';
    button.disabled = true;
  } else if (running) {
    ui.primaryLabel.textContent = '▶ 开始字幕';
    button.disabled = false;
  } else {
    ui.primaryLabel.textContent = host ? '▶ 启动服务并开始字幕' : '等待本地服务启动…';
    button.disabled = probe === undefined || !host;
  }

  // 本次统计
  if (view.capturing && capture) {
    const parts = [`已识别 ${formatSeconds(capture.audioSeconds)}`];
    if (typeof capture.lastLatency === 'number') parts.push(`延迟 ${capture.lastLatency.toFixed(1)}s`);
    ui.sessionLine.textContent = parts.join(' · ');
    ui.sessionLine.hidden = false;
  } else {
    ui.sessionLine.hidden = true;
  }

  // 识别说明：运行中的模型和下拉框不一致时提示
  let hint = asrHint(settings.language, ui.asr.value);
  if (running && runningAsr && runningAsr !== ui.asr.value) {
    hint = host && host.managed
      ? `服务当前用的是 ${ASR_NAMES[runningAsr]}，开始字幕时会重启服务切换（约 10–40 秒）`
      : `服务当前用的是 ${ASR_NAMES[runningAsr]}（手动启动的，不会自动切换）`;
  }
  ui.asrHint.textContent = hint;

  // 高级区
  ui.viewLog.disabled = !host;
  ui.stopService.disabled = !host || !running || !host.managed || Boolean(busy);
  ui.hostHint.hidden = Boolean(host && host.managed) || !running;
  ui.hostHint.textContent = host
    ? '当前服务是手动用 .bat 启动的，要停止请关闭那个窗口。'
    : '一键启动未安装：无法在这里停止服务或查看日志。';
}

function renderSegments() {
  for (const group of document.querySelectorAll('.segmented')) {
    const value = settings[group.dataset.name];
    for (const button of group.querySelectorAll('button')) {
      const on = button.dataset.value === value;
      button.setAttribute('aria-checked', String(on));
      button.tabIndex = on ? 0 : -1;
    }
  }
}

// --- 状态刷新 -----------------------------------------------------------------

let refreshing = false;

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const [probe, stored] = await Promise.all([
      Shared.probeService(Shared.WS_URL, 1500),
      store.get(['capturing', 'captureStatus', 'activeTabId']),
    ]);
    view.captureTab = null;
    if (stored.activeTabId) {
      view.captureTab = await chrome.tabs.get(stored.activeTabId).catch(() => null);
    }
    view.probe = probe;
    view.capturing = Boolean(stored.capturing);
    view.capture = stored.captureStatus || null;
    // 服务在采集中被关掉（闲置退出、手动停止）：别再显示"字幕进行中"
    if (!probe && view.capturing && !view.busy) {
      view.capturing = false;
      await store.set({ capturing: false });
    }
  } finally {
    refreshing = false;
  }
  render();
}

async function refreshHost() {
  view.host = await callHost({ cmd: 'status' });
  render();
}

/** 轮询直到服务能连上，或超时。 */
async function waitForService(timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const probe = await Shared.probeService(Shared.WS_URL, 1500);
    if (probe) return probe;
    await sleep(1000);
  }
  return null;
}

// --- 操作 ---------------------------------------------------------------------

/** 实验开关随 start 消息发给服务端，每个会话单独生效，不用重启服务。 */
function experimentOptions() {
  return {
    early_final: ui.earlyFinal.checked,
    adaptive_silence: ui.adaptiveSilence.checked ? 0.2 : null,
    address_consistency: ui.addressConsistency.checked,
  };
}

async function ensureService() {
  const wanted = ui.asr.value;
  if (view.probe && (serviceMatches(view.probe) || !view.host || !view.host.managed)) {
    // 已经在跑：识别配置一致，或者是用户手动开的（不替用户重启）
    return true;
  }
  if (!view.host) {
    view.lastError = '服务未启动，而一键启动未安装。请先运行「启动字幕服务.bat」。';
    return false;
  }
  const restarting = Boolean(view.probe);
  view.busy = restarting ? `正在切换到 ${ASR_NAMES[wanted]}…` : '正在启动本地服务…';
  render();
  const reply = await callHost({
    cmd: restarting ? 'restart' : 'start', asr: settings.asr, en_asr: settings.enAsr,
    idle_minutes: settings.idleMinutes,
  });
  if (!reply || reply.ok === false) {
    view.busy = '';
    view.lastError = `启动服务失败：${(reply && reply.error) || '一键启动宿主无响应'}`;
    return false;
  }
  const probe = await waitForService();
  view.busy = '';
  if (!probe) {
    view.lastError = '服务启动超时（90 秒）。点「高级 → 查看服务日志」看看卡在哪一步。';
    return false;
  }
  view.probe = probe;
  view.host = await callHost({ cmd: 'status' });
  return true;
}

async function startSubtitles() {
  const tab = view.tab;
  // streamId 必须在 await 任何耗时操作之前取：它依赖本次点击的用户手势。
  let streamId = null;
  try {
    streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
  } catch (error) {
    // 不直接失败：让 background 也试一次（不同 Chrome 版本行为不一致）。
    console.warn('[本地字幕] popup 取 streamId 失败，交给 background 重试：', error);
  }

  if (!(await ensureService())) {
    render();
    return;
  }

  const response = await chrome.runtime.sendMessage({
    type: 'start-capture',
    tabId: tab.id,
    streamId,
    language: settings.language,
    options: experimentOptions(),
  }).catch((error) => ({ ok: false, error: String(error) }));

  if (response && response.ok === false) {
    view.lastError = `开始字幕失败：${response.error}`;
    render();
    return;
  }
  view.capturing = true;
  await store.set({ capturing: true });
  render();
  setTimeout(refresh, 800);
}

async function stopSubtitles() {
  view.capturing = false;
  await store.set({ capturing: false });
  await chrome.runtime.sendMessage({ type: 'stop-capture' })
    .catch((error) => ({ ok: false, error: String(error) }));
  render();
}

ui.primary.addEventListener('click', async () => {
  view.lastError = '';
  if (view.capturing && capturingElsewhere()) {
    // 先停掉旧标签页的捕获，再开始这一页（startCapture 内部也会先 stop，这里显式做是为了状态立即更新）
    await stopSubtitles();
    await startSubtitles();
  } else if (view.capturing) {
    await stopSubtitles();
  } else {
    await startSubtitles();
  }
});

ui.stopService.addEventListener('click', async () => {
  view.lastError = '';
  if (view.capturing) await stopSubtitles();
  view.busy = '正在停止服务…';
  render();
  const reply = await callHost({ cmd: 'stop' });
  view.busy = '';
  if (!reply || reply.ok === false) view.lastError = `停止服务失败：${(reply && reply.error) || '宿主无响应'}`;
  view.host = reply && reply.ok !== false ? reply : view.host;
  await refresh();
});

ui.viewLog.addEventListener('click', () => {
  chrome.tabs.create({ url: chrome.runtime.getURL('log.html') });
});

// --- 设置 ---------------------------------------------------------------------

function save(patch) {
  Object.assign(settings, patch);
  chrome.storage.local.set(patch);
}

ui.enabled.addEventListener('change', () => save({ enabled: ui.enabled.checked }));
// 打开浮窗开关时顺便展开面板：用户找浮窗多半是想用它，不是想看那个小圆钮
ui.panelShown.addEventListener('change', () => save(ui.panelShown.checked
  ? { panelHidden: false, panelOpen: true } : { panelHidden: true }));
ui.earlyFinal.addEventListener('change', () => save({ earlyFinal: ui.earlyFinal.checked }));
ui.adaptiveSilence.addEventListener('change', () => save({ adaptiveSilence: ui.adaptiveSilence.checked }));
ui.addressConsistency.addEventListener('change', () => save({ addressConsistency: ui.addressConsistency.checked }));
ui.resetAddresses.addEventListener('click', async () => {
  const reply = await chrome.runtime.sendMessage({ type: 'reset-address-memory' }).catch((error) => ({ ok: false, error: String(error) }));
  if (!reply || !reply.ok) view.lastError = reply?.error || '清除称呼记忆失败';
  render();
});
ui.idle.addEventListener('change', () => save({ idleMinutes: Number(ui.idle.value) }));
ui.asr.addEventListener('change', () => {
  save({ [ASR_KEY[settings.language]]: ui.asr.value });
  render();
});

for (const group of document.querySelectorAll('.segmented')) {
  const buttons = Array.from(group.querySelectorAll('button'));
  const choose = (button) => {
    // mode 改了 content script 通过 storage.onChanged 立即生效，不用发消息。
    save({ [group.dataset.name]: button.dataset.value });
    renderSegments();
    if (group.dataset.name === 'language') renderAsrOptions();
    render();
    button.focus();
  };
  group.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (button) choose(button);
  });
  // 单选组的键盘操作：左右方向键切换
  group.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const index = buttons.findIndex((button) => button.getAttribute('aria-checked') === 'true');
    const step = event.key === 'ArrowRight' ? 1 : -1;
    choose(buttons[(index + step + buttons.length) % buttons.length]);
  });
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.addressConsistency) {
    settings.addressConsistency = changes.addressConsistency.newValue === true;
    ui.addressConsistency.checked = settings.addressConsistency;
  }
  if (area === 'local' && changes.panelHidden) {
    settings.panelHidden = Boolean(changes.panelHidden.newValue);
    ui.panelShown.checked = !settings.panelHidden;
  }
  if (area === 'session' && (changes.captureStatus || changes.capturing)) {
    if (changes.captureStatus) view.capture = changes.captureStatus.newValue || null;
    if (changes.capturing) view.capturing = Boolean(changes.capturing.newValue);
    render();
  }
});

// --- 初始化 -------------------------------------------------------------------

async function init() {
  const value = await chrome.storage.local.get(DEFAULTS);
  Object.assign(settings, value);
  ui.enabled.checked = settings.enabled;
  ui.panelShown.checked = !settings.panelHidden;
  renderAsrOptions();
  ui.idle.value = String(settings.idleMinutes);
  ui.earlyFinal.checked = settings.earlyFinal;
  ui.adaptiveSilence.checked = settings.adaptiveSilence;
  ui.addressConsistency.checked = settings.addressConsistency;
  renderSegments();

  let tab = null;
  if (EMBEDDED) {
    // iframe 里 tabs.query 拿到的是浏览器当前激活的页，不一定是浮窗所在页；问 background 要发送方的标签页
    tab = await chrome.runtime.sendMessage({ type: 'whoami' }).catch(() => null);
  } else {
    [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  }
  view.tab = tab || null;
  if (tab && tab.url && tab.url.startsWith('file:')) {
    // 没开「允许访问文件网址」时字幕脚本注入不进去：采集照样能跑，但页面上什么都不显示。
    chrome.extension.isAllowedFileSchemeAccess((allowed) => {
      view.fileAccessMissing = !allowed;
      render();
    });
  }

  render();
  refreshHost();
  await refresh();
  setInterval(() => { if (!view.busy) refresh(); }, 2000);
  if (EMBEDDED) {
    const report = () => parent.postMessage(
      { lls: 'panel-height', height: document.documentElement.scrollHeight }, '*');
    new ResizeObserver(report).observe(document.body);
    report();
  }
}

init();
