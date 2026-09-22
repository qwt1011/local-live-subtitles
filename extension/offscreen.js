/**
 * Offscreen：采集标签页音频 → 转 16 kHz 单声道 PCM → WebSocket 推给本地流式服务。
 *
 * 这一层只做"哑采集"：不分段、不判断句子、不做任何识别相关的决策
 * （ARCHITECTURE_REVIEW.md 第 4 节不变式 1）。
 */

const Shared = self.SubtitleShared;

let socket = null;
let audioContext = null;
let mediaStream = null;
let workletNode = null;
let sourceNode = null;
let framer = new Shared.PcmFramer();
let sessionLanguage = 'ja';
let connected = false;
let sentFrames = 0;
let sentSamples = 0;

function publishStatus(patch) {
  // 状态放在 session 存储里：service worker 随时可能被回收，
  // 内存变量会丢，popup 就会显示成"未连接"。
  const payload = {
    connected,
    language: sessionLanguage,
    sentFrames,
    audioSeconds: Math.round((sentSamples / Shared.TARGET_SAMPLE_RATE) * 10) / 10,
    updatedAt: Date.now(),
    ...patch,
  };
  chrome.storage.session.set({ captureStatus: payload }).catch(() => {});
  chrome.runtime.sendMessage({ type: 'capture-status', status: payload }).catch(() => {});
}

async function start(streamId, language) {
  await stop();
  sessionLanguage = language || 'ja';
  sentFrames = 0;
  sentSamples = 0;
  framer.reset();

  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { mandatory: { chromeMediaSource: 'tab', chromeMediaSourceId: streamId } },
      video: false,
    });
  } catch (error) {
    publishStatus({ connected: false, error: `获取标签页音频失败：${error}` });
    return;
  }

  // 直接建 16 kHz 的上下文，让浏览器负责重采样，比在 JS 里手写重采样可靠。
  audioContext = new AudioContext({ sampleRate: Shared.TARGET_SAMPLE_RATE });
  if (audioContext.state === 'suspended') await audioContext.resume();
  await audioContext.audioWorklet.addModule(chrome.runtime.getURL('pcm-worklet.js'));

  sourceNode = audioContext.createMediaStreamSource(mediaStream);
  // tabCapture 会把原标签页静音，必须把音频接回输出，否则用户听不到声音。
  sourceNode.connect(audioContext.destination);

  workletNode = new AudioWorkletNode(audioContext, 'pcm-tap');
  workletNode.port.onmessage = (event) => onSamples(event.data);
  sourceNode.connect(workletNode);

  connectSocket(sessionLanguage);
  publishStatus({ connected: false, state: 'connecting' });
}

function connectSocket(language) {
  try {
    socket = new WebSocket(Shared.WS_URL);
  } catch (error) {
    publishStatus({ connected: false, error: String(error) });
    return;
  }
  socket.binaryType = 'arraybuffer';

  socket.onopen = () => {
    connected = true;
    socket.send(JSON.stringify({ type: 'start', language }));
    publishStatus({ connected: true, state: 'listening', error: null });
  };

  socket.onmessage = (event) => {
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
      chrome.runtime.sendMessage({ type: 'subtitle-event', payload }).catch(() => {});
      publishStatus({
        connected: true,
        lastLatency: payload.latency,
        lastSegment: payload.segment_id,
        lastIsFinal: payload.is_final,
      });
    }
  };

  socket.onerror = () => {
    publishStatus({ connected: false, state: 'error', error: '无法连接本地流式服务（ws://127.0.0.1:8766）' });
  };

  socket.onclose = () => {
    connected = false;
    publishStatus({ connected: false, state: 'closed' });
  };
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
    start(message.streamId, message.language)
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
