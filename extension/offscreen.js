/**
 * Offscreen：采集标签页音频 → 转 16 kHz 单声道 PCM → WebSocket 推给本地流式服务。
 *
 * 这一层只做"哑采集"：不分段、不判断句子、不做任何识别相关的决策
 * （docs/ARCHITECTURE_REVIEW.md 第 4 节不变式 1）。
 */

const Shared = self.SubtitleShared;

// 状态上报用永不抛错的封装：上报失败绝不能把采集启动带崩。
const store = Shared.createStore('offscreen');
Shared.describeEnvironment('offscreen');

let socket = null;
let audioContext = null;
let mediaStream = null;
let workletNode = null;
let sourceNode = null;
let framer = new Shared.PcmFramer();
let sessionLanguage = 'ja';
let sessionOptions = {};   // 实验开关，随 start 消息交给服务端
let connected = false;
let sentFrames = 0;
let sentSamples = 0;
// 每次开始捕获生成一个新的会话号，随字幕事件一起发给页面。
// 服务端每个会话的 segment_id 都从 0 开始：不区分会话的话，上一个会话留在页面上的旧字幕
// 会和新会话的同号句子撞车（revision 比新的大，新字幕被当成"迟到的旧版本"丢掉），
// 直到新会话的 segment_id 超过旧的最大值才恢复——10-01 用户实测：英语切日语后约 150 秒仍显示英文。
let sessionId = 0;

/**
 * 上报状态。**整个函数都不能抛异常**——它是遥测，不该有能力弄坏采集。
 * 之前正是它内部的 chrome.storage.session 访问抛错，把启动流程整个带崩了。
 */
function publishStatus(patch) {
  try {
    const payload = {
      connected,
      language: sessionLanguage,
      sentFrames,
      audioSeconds: Math.round((sentSamples / Shared.TARGET_SAMPLE_RATE) * 10) / 10,
      updatedAt: Date.now(),
      ...patch,
    };
    store.set({ captureStatus: payload });
    chrome.runtime.sendMessage({ type: 'capture-status', status: payload }).catch(() => {});
  } catch (error) {
    // 连日志都要防一手：状态上报失败不影响采集。
    try {
      console.warn('[本地字幕] 上报状态失败（已忽略）：', error);
    } catch (ignored) { /* 什么都不做 */ }
  }
}

/**
 * 启动采集。每一步都单独盯错误。
 *
 * 为什么这么啰嗦：原来只有 getUserMedia 一步有错误处理，而且失败后
 * 只是记一条状态就 return（调用方以为成功了）；后面几步（AudioContext、
 * addModule、AudioWorkletNode）完全没有保护，抛出的异常会被 background 的
 * `.catch(() => {})` 吞掉。结果就是"点了开始捕获、什么都没发生、也没有任何报错"。
 * 现在每一步都有名字，失败时把**是哪一步**写进状态，popup 与 offscreen 控制台都能看到，
 * 并且 rethrow 让调用方知道失败了。
 */
async function start(streamId, language, options) {
  await stop();
  sessionLanguage = language || 'ja';
  sessionId = Math.max(Date.now(), sessionId + 1);
  sessionOptions = options || {};
  sentFrames = 0;
  sentSamples = 0;
  framer.reset();

  const step = async (name, action) => {
    publishStatus({ connected: false, state: 'starting', step: name, error: null });
    console.log(`[本地字幕] ${name}…`);
    try {
      const value = await action();
      console.log(`[本地字幕] ${name} 完成`);
      return value;
    } catch (error) {
      const message = `${name}失败：${error && error.name ? error.name + ' ' : ''}${error}`;
      console.error(`[本地字幕] ${message}`, error);
      publishStatus({ connected: false, state: 'error', step: name, error: message });
      throw error;      // 让 background / popup 也能看到失败
    }
  };

  mediaStream = await step('① 获取标签页音频', () => navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: 'tab', chromeMediaSourceId: streamId } },
    video: false,
  }));

  // 直接建 16 kHz 的上下文，让浏览器负责重采样，比在 JS 里手写重采样可靠。
  audioContext = await step('② 创建 16 kHz AudioContext',
    () => new AudioContext({ sampleRate: Shared.TARGET_SAMPLE_RATE }));
  if (audioContext.state === 'suspended') await audioContext.resume();

  await step('③ 加载 PCM worklet',
    () => audioContext.audioWorklet.addModule(chrome.runtime.getURL('pcm-worklet.js')));

  sourceNode = await step('④ 接上音频源', () => {
    const node = audioContext.createMediaStreamSource(mediaStream);
    // tabCapture 会把原标签页静音，必须把音频接回输出，否则用户听不到声音。
    node.connect(audioContext.destination);
    return node;
  });

  workletNode = await step('⑤ 建立 AudioWorkletNode', () => {
    const node = new AudioWorkletNode(audioContext, 'pcm-tap');
    node.port.onmessage = (event) => onSamples(event.data);
    sourceNode.connect(node);
    return node;
  });

  await step('⑥ 连接本地服务', () => connectSocket(sessionLanguage));
  publishStatus({ connected: false, state: 'connecting', step: null, error: null });
}

/** 连上本地流式服务。返回 Promise，让失败能被第 ⑥ 步捕获。 */
function connectSocket(language) {
  return new Promise((resolve, reject) => {
    try {
      socket = new WebSocket(Shared.WS_URL);
    } catch (error) {
      publishStatus({ connected: false, error: `连接本地服务失败：${error}` });
      reject(error);
      return;
    }
    socket.binaryType = 'arraybuffer';
    const connection = socket;
    const connectionSession = sessionId;

    // 连不上时不要让第 ⑥ 步永远挂着。
    const timer = setTimeout(() => reject(new Error(`连接 ${Shared.WS_URL} 超时`)), 5000);

    socket.onopen = () => {
      clearTimeout(timer);
      if (socket !== connection) { reject(new Error('Capture stopped')); return; }
      connected = true;
      socket.send(JSON.stringify({ type: 'start', language, options: sessionOptions }));
      publishStatus({ connected: true, state: 'listening', step: null, error: null });
      resolve();
    };

    socket.onerror = () => {
      clearTimeout(timer);
      if (socket !== connection) { reject(new Error('Capture stopped')); return; }
      publishStatus({ connected: false, error: `无法连接本地流式服务（${Shared.WS_URL}）` });
      reject(new Error(`无法连接 ${Shared.WS_URL}`));
    };

    socket.onclose = () => {
      clearTimeout(timer);
      if (socket !== connection) return;
      connected = false;
      publishStatus({ connected: false, state: 'closed' });
    };

    socket.onmessage = (event) => {
      if (socket !== connection) return;
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      if (payload.type === 'status') {
        publishStatus({ connected: true, state: payload.state, server: payload });
        return;
      }
      if (payload.type === 'event') {
        // 只做转发，渲染由 content script 负责。
        chrome.runtime.sendMessage({ type: 'subtitle-event', payload: { ...payload, session: connectionSession } })
          .catch(() => {});
        publishStatus({
          connected: true,
          lastLatency: payload.latency,
          lastSegment: payload.segment_id,
          lastIsFinal: payload.is_final,
        });
      }
    };
  });
}

function onSamples(samples) {
  framer.push(samples);
  const frames = framer.drain();
  for (const frame of frames) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      // 没连上就丢掉，不排队：宁可丢音频也不要让延迟无界增长。
      continue;
    }
    const pcm = Shared.floatToInt16(frame);
    socket.send(pcm.buffer);
    sentFrames += 1;
    sentSamples += pcm.length;
  }
  if (frames.length) publishStatus({ connected });
}

async function stop() {
  if (socket) {
    try {
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'stop' }));
      socket.close();
    } catch (error) {
      /* 关闭失败无所谓 */
    }
    socket = null;
  }
  connected = false;
  if (workletNode) {
    workletNode.port.onmessage = null;
    workletNode.disconnect();
    workletNode = null;
  }
  if (sourceNode) {
    sourceNode.disconnect();
    sourceNode = null;
  }
  if (audioContext) {
    await audioContext.close().catch(() => {});
    audioContext = null;
  }
  if (mediaStream) {
    mediaStream.getTracks().forEach((track) => track.stop());
    mediaStream = null;
  }
  framer.reset();
  publishStatus({ connected: false, state: 'stopped' });
}

// 必须显式 sendResponse：否则 background 那边 await sendMessage 会以
// "message port closed before a response was received" 失败，
// 在外层看来就是"点了开始捕获没反应"。
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === 'offscreen-start') {
    start(message.streamId, message.language, message.options)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message.type === 'offscreen-stop') {
    stop()
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message.type === 'offscreen-ping') {
    publishStatus({ state: 'ok' });
    sendResponse({ ok: true });
    return false;
  }
  return false;
});
