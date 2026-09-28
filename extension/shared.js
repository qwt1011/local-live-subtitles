/**
 * 扩展端的纯逻辑：PCM 分帧 + 字幕事件状态机。
 *
 * 单独抽出来的原因：这部分是唯一能在浏览器之外被验证的（tests/test_extension_logic.js 用 Node 跑）。
 * 音频采集和渲染必须靠真机验证，但"PCM 有没有切错""revision 会不会回跳"这类错误
 * 完全可以在 Node 里先测掉，不必每次都去开 Chrome 点 YouTube。
 *
 * 同时在浏览器（挂到 self.SubtitleShared）和 Node（module.exports）下可用。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.SubtitleShared = api;
  }
})(typeof self !== 'undefined' ? self : globalThis, function () {
  'use strict';

  // 服务端要求的格式：16 kHz 单声道 s16le 裸 PCM（见 app/server.py 的协议说明）。
  const TARGET_SAMPLE_RATE = 16000;
  const WS_URL = 'ws://127.0.0.1:8766';
  // 100ms 一帧：太小会让消息数量爆炸，太大会增加传输延迟。
  const FRAME_MS = 100;
  const FRAME_SAMPLES = (TARGET_SAMPLE_RATE * FRAME_MS) / 1000;

  /** Float32 [-1,1] → Int16。服务端按 s16le 解析。 */
  function floatToInt16(input) {
    const out = new Int16Array(input.length);
    for (let i = 0; i < input.length; i += 1) {
      let value = input[i];
      if (value > 1) value = 1;
      else if (value < -1) value = -1;
      out[i] = value < 0 ? value * 0x8000 : value * 0x7fff;
    }
    return out;
  }

  /**
   * 把不定长的音频块拼成固定长度的帧。
   * AudioWorklet 每次给 128 个采样，服务端要的是 100ms 一帧，中间必须缓冲。
   */
  class PcmFramer {
    constructor(frameSamples = FRAME_SAMPLES) {
      this.frameSamples = frameSamples;
      this.chunks = [];
      this.length = 0;
    }

    push(samples) {
      if (!samples || samples.length === 0) return;
      this.chunks.push(samples);
      this.length += samples.length;
    }

    /** 凑够一帧就取出；一次可能取出多帧。 */
    drain() {
      const frames = [];
      while (this.length >= this.frameSamples) {
        const frame = new Float32Array(this.frameSamples);
        let filled = 0;
        while (filled < this.frameSamples) {
          const head = this.chunks[0];
          const take = Math.min(head.length, this.frameSamples - filled);
          frame.set(head.subarray(0, take), filled);
          filled += take;
          if (take === head.length) this.chunks.shift();
          else this.chunks[0] = head.subarray(take);
        }
        this.length -= this.frameSamples;
        frames.push(frame);
      }
      return frames;
    }

    /** 丢弃未成帧的尾巴，避免上一句话的残音被拼到下一句开头。 */
    reset() {
      this.chunks = [];
      this.length = 0;
    }
  }

  /**
   * 字幕事件状态机。
   *
   * 服务端会为同一句话连续发多个 revision（partial 在前、final 在后），
   * 渲染端必须：
   *   1. 只允许 revision 单调前进（否则字幕会回跳）；
   *   2. partial 用"未定稿"样式原地替换，final 到达后转成定稿样式（不闪烁）。
   */
  class SubtitleState {
    constructor(maxLines = 2) {
      this.maxLines = maxLines;
      this.segments = new Map();
      this.lastEventAt = 0;
      this.eventCount = 0;
      this.appliedCount = 0;
      this.outOfOrderCount = 0;
    }

    /** 返回 true 表示这次事件真的改变了显示内容。 */
    apply(event) {
      if (!event || typeof event.segment_id !== 'number') return false;
      this.eventCount += 1;
      this.lastEventAt = Date.now();

      const previous = this.segments.get(event.segment_id);
      if (previous && event.revision <= previous.revision) {
        // 迟到的旧 revision：丢弃，否则用户会看到字幕倒退。
        this.outOfOrderCount += 1;
        return false;
      }
      this.segments.set(event.segment_id, {
        segmentId: event.segment_id,
        revision: event.revision,
        original: event.text || '',
        translation: event.translation || '',
        isFinal: Boolean(event.is_final),
      });
      this.appliedCount += 1;
      return true;
    }

    /** 取最后 maxLines 句，按 segment_id 升序。 */
    lines() {
      const ordered = Array.from(this.segments.values())
        .sort((a, b) => a.segmentId - b.segmentId)
        .filter((row) => row.original || row.translation);
      return ordered.slice(-this.maxLines);
    }

    /** 当前是否还有未定稿的行（用于给最新一行加"草稿"样式）。 */
    hasPending() {
      const lines = this.lines();
      return lines.length > 0 && !lines[lines.length - 1].isFinal;
    }

    reset() {
      this.segments.clear();
      this.eventCount = 0;
      this.appliedCount = 0;
      this.outOfOrderCount = 0;
    }
  }

  /**
   * 一个**永不抛错**的键值存储封装。
   *
   * 起因是一个真实故障：offscreen 里的 `chrome.storage.session.set(...)`
   * 抛了 `TypeError: Cannot read properties of undefined (reading 'session')`，
   * 而它是在状态上报路径上——**状态上报把整个采集启动流程带崩了**。
   * 遥测不该有能力弄坏被测的功能，所以这里：
   *
   * - 优先 `storage.session`；不可用则退回 `storage.local`；再不可用则退回内存。
   * - 任何一步失败都只是记一条 warning，绝不向调用方抛异常。
   * - 把实际用的是哪一种报出来，方便定位"到底哪个 API 在某个上下文里没有"。
   */
  function createStore(label) {
    const area = (() => {
      try {
        if (typeof chrome === 'undefined' || !chrome.storage) return null;
        if (chrome.storage.session) return { kind: 'session', impl: chrome.storage.session };
        if (chrome.storage.local) return { kind: 'local', impl: chrome.storage.local };
      } catch (error) {
        /* 下面统一按"没有可用存储"处理 */
      }
      return null;
    })();

    const memory = new Map();
    const where = area ? area.kind : 'memory';

    if (!area) {
      console.warn(`[本地字幕] ${label}: chrome.storage 不可用，改用内存存储`
        + '（状态在 service worker 回收后会丢）');
    } else if (area.kind !== 'session') {
      console.warn(`[本地字幕] ${label}: storage.session 不可用，退回 ${area.kind}`);
    }
    console.log(`[本地字幕] ${label}: 存储 = ${where}`);

    return {
      where,
      async get(keys) {
        const result = {};
        const list = Array.isArray(keys) ? keys : Object.keys(keys || {});
        try {
          if (area) {
            const got = await area.impl.get(list);
            return got || {};
          }
        } catch (error) {
          console.warn(`[本地字幕] ${label}: 读取失败，改用内存`, error);
        }
        for (const key of list) if (memory.has(key)) result[key] = memory.get(key);
        return result;
      },
      async set(patch) {
        for (const [key, value] of Object.entries(patch || {})) memory.set(key, value);
        try {
          if (area) await area.impl.set(patch);
        } catch (error) {
          console.warn(`[本地字幕] ${label}: 写入失败`, error);
        }
      },
    };
  }

  /** 各上下文启动时打一行环境自检，用来确认到底哪个 API 缺失。 */
  function describeEnvironment(label) {
    const info = {
      hasChrome: typeof chrome !== 'undefined',
      hasRuntime: typeof chrome !== 'undefined' && Boolean(chrome.runtime),
      hasStorage: typeof chrome !== 'undefined' && Boolean(chrome.storage),
      hasSession: typeof chrome !== 'undefined' && Boolean(chrome.storage && chrome.storage.session),
      hasOffscreen: typeof chrome !== 'undefined' && Boolean(chrome.offscreen),
      hasTabCapture: typeof chrome !== 'undefined' && Boolean(chrome.tabCapture),
    };
    console.log(`[本地字幕] ${label} 环境自检:`, info);
    return info;
  }

  const MODES = ['original', 'translation', 'bilingual'];

  /**
   * 探测本地服务是否在线：连一次 WebSocket，发一条 ping，等 status 回复。
   *
   * **为什么不复用 offscreen 写的状态**：offscreen 文档只有在点了「开始捕获」之后
   * 才会被创建，所以在那之前 `captureStatus` 根本不存在——popup 于是永远显示
   * "未连接本地服务"，哪怕服务跑得好好的。这就是"检查本地服务"按钮一开始没用的原因：
   * 探测必须独立于采集。
   *
   * 返回服务端的 status 对象；连不上或超时返回 null。
   */
  function probeService(url = WS_URL, timeoutMs = 3000) {
    return new Promise((resolve) => {
      let socket = null;
      let settled = false;
      let timer = null;

      const finish = (value) => {
        if (settled) return;      // onerror 之后 onclose 还会再来一次
        settled = true;
        if (timer) clearTimeout(timer);
        try {
          if (socket && socket.readyState <= 1) socket.close();
        } catch (error) { /* 关闭失败无所谓 */ }
        resolve(value);
      };

      timer = setTimeout(() => finish(null), timeoutMs);
      try {
        socket = new WebSocket(url);
      } catch (error) {
        finish(null);
        return;
      }

      socket.onopen = () => {
        try {
          socket.send(JSON.stringify({ type: 'ping' }));
        } catch (error) {
          finish(null);
        }
      };
      socket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data && data.type === 'status') finish(data);
        } catch (error) { /* 非 JSON 直接忽略 */ }
      };
      socket.onerror = () => finish(null);
      socket.onclose = () => finish(null);
    });
  }

  /**
   * 选择字幕覆盖层的挂载点。
   *
   * 必须挂到 fullscreen element 的后代里：YouTube 进入全屏后，只有该元素及其后代参与渲染，
   * 挂在 document.documentElement 上的兄弟节点**完全不会显示**——
   * 这就是"全屏下字幕消失"的原因（ARCHITECTURE_REVIEW.md S6）。
   * 单独抽成函数是为了能在 Node 里测掉，不必真的去开全屏。
   */
  function pickMountParent(doc) {
    return doc.fullscreenElement
      || doc.webkitFullscreenElement
      || doc.documentElement;
  }

  /** 按显示模式决定每一行显示什么。 */
  function composeLine(row, mode) {
    const showOriginal = mode !== 'translation';
    const showTranslation = mode !== 'original';
    return {
      original: showOriginal ? row.original : '',
      translation: showTranslation ? row.translation : '',
      isFinal: row.isFinal,
    };
  }

  return {
    TARGET_SAMPLE_RATE,
    WS_URL,
    FRAME_MS,
    FRAME_SAMPLES,
    MODES,
    floatToInt16,
    PcmFramer,
    SubtitleState,
    composeLine,
    pickMountParent,
    probeService,
    createStore,
    describeEnvironment,
  };
});
