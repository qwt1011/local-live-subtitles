/**
 * 扩展端纯逻辑的单元测试（Node 跑，不需要浏览器）。
 *
 *   node tests/test_extension_logic.js
 *
 * 能在这里测掉的东西就不要留到 Chrome 里点：PCM 分帧错一帧、revision 回跳一次，
 * 在浏览器里都表现为"字幕偶尔怪一下"，极难复现；在这里是确定的失败。
 */

const assert = require('node:assert');
const Shared = require('../extension/shared.js');

let passed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  ok   ${name}`);
  } catch (error) {
    failures.push({ name, error });
    console.log(`  FAIL ${name}\n       ${error.message}`);
  }
}

console.log('floatToInt16');

test('把满量程映射到 int16 边界', () => {
  const out = Shared.floatToInt16(new Float32Array([0, 1, -1, 0.5]));
  assert.strictEqual(out[0], 0);
  assert.strictEqual(out[1], 32767);
  assert.strictEqual(out[2], -32768);
  assert.strictEqual(out[3], 16383);
});

test('超范围输入被截断而不是回绕', () => {
  // 不做截断的话，>1 的值会溢出成负值，听感上是爆音。
  const out = Shared.floatToInt16(new Float32Array([2.5, -3.0]));
  assert.strictEqual(out[0], 32767);
  assert.strictEqual(out[1], -32768);
});

test('返回 Int16Array（服务端按 s16le 解析）', () => {
  assert.ok(Shared.floatToInt16(new Float32Array([0.1])) instanceof Int16Array);
});

console.log('\nPcmFramer');

test('凑不满一帧时不出帧', () => {
  const framer = new Shared.PcmFramer(100);
  framer.push(new Float32Array(40));
  framer.push(new Float32Array(50));
  assert.strictEqual(framer.drain().length, 0);
  assert.strictEqual(framer.length, 90);
});

test('跨多次 push 正确拼帧且不丢样点', () => {
  const framer = new Shared.PcmFramer(4);
  framer.push(new Float32Array([1, 2, 3]));
  framer.push(new Float32Array([4, 5, 6, 7, 8]));
  const frames = framer.drain();
  assert.strictEqual(frames.length, 2);
  assert.deepStrictEqual(Array.from(frames[0]), [1, 2, 3, 4]);
  assert.deepStrictEqual(Array.from(frames[1]), [5, 6, 7, 8]);
  // 3 + 5 = 8 个样点正好切成 2 帧，没有余数
  assert.strictEqual(framer.length, 0);
});

test('样点守恒：出帧样点 + 剩余样点 === 推入样点', () => {
  // 这才是真正要保证的不变量。用不规则的分块长度和帧长反复验证，
  // 比写死某几个期望值更能抓到边界错误（掉样点会让识别结果莫名其妙地缺字）。
  for (const frameSize of [1, 3, 4, 100, 1600]) {
    for (const blockSize of [1, 7, 128, 999, 3000]) {
      const framer = new Shared.PcmFramer(frameSize);
      let pushed = 0;
      for (let round = 0; round < 5; round += 1) {
        framer.push(new Float32Array(blockSize));
        pushed += blockSize;
      }
      const frames = framer.drain();
      const framed = frames.reduce((sum, frame) => sum + frame.length, 0);
      assert.strictEqual(framed + framer.length, pushed,
        `frameSize=${frameSize} blockSize=${blockSize}：${framed}+${framer.length} != ${pushed}`);
      for (const frame of frames) {
        assert.strictEqual(frame.length, frameSize, '每一帧长度必须精确等于 frameSamples');
      }
      assert.strictEqual(frames.length, Math.floor(pushed / frameSize));
    }
  }
});

test('一次 push 很大时能出多帧', () => {
  const framer = new Shared.PcmFramer(2);
  framer.push(new Float32Array([1, 2, 3, 4, 5]));
  const frames = framer.drain();
  assert.strictEqual(frames.length, 2);
  assert.strictEqual(framer.length, 1);
});

test('每一帧长度都精确等于 frameSamples', () => {
  const framer = new Shared.PcmFramer(1600);
  framer.push(new Float32Array(3000));
  const frames = framer.drain();
  assert.strictEqual(frames.length, 1);
  assert.strictEqual(frames[0].length, 1600);
  assert.strictEqual(framer.length, 1400);
});

test('reset 丢弃未成帧的尾巴', () => {
  const framer = new Shared.PcmFramer(10);
  framer.push(new Float32Array(7));
  framer.reset();
  framer.push(new Float32Array(10));
  const frames = framer.drain();
  assert.strictEqual(frames.length, 1);
  assert.deepStrictEqual(Array.from(frames[0]), Array.from({ length: 10 }, () => 0));
});

console.log('\nSubtitleState');

function event(segmentId, revision, text, isFinal) {
  return { segment_id: segmentId, revision, text, is_final: isFinal };
}

test('partial 原地替换，不新增行', () => {
  const state = new Shared.SubtitleState(2);
  state.apply(event(0, 1, 'それ', false));
  state.apply(event(0, 2, 'それじゃあ', false));
  const lines = state.lines();
  assert.strictEqual(lines.length, 1);
  assert.strictEqual(lines[0].original, 'それじゃあ');
  assert.strictEqual(lines[0].isFinal, false);
});

test('final 替换同句的 partial 并转为定稿', () => {
  const state = new Shared.SubtitleState(2);
  state.apply(event(0, 1, 'それじゃ', false));
  state.apply(event(0, 2, 'それじゃあ短いデータと', false));
  state.apply(event(0, 3, 'それじゃあ短いデータと行きましょうか。', true));
  const lines = state.lines();
  assert.strictEqual(lines.length, 1);
  assert.strictEqual(lines[0].isFinal, true);
});

test('迟到的旧 revision 被丢弃（否则字幕会回跳）', () => {
  const state = new Shared.SubtitleState(2);
  assert.strictEqual(state.apply(event(0, 5, '新的', true)), true);
  assert.strictEqual(state.apply(event(0, 3, '旧的', false)), false);
  assert.strictEqual(state.lines()[0].original, '新的');
  assert.strictEqual(state.outOfOrderCount, 1);
});

test('相同 revision 重复到达不重复应用', () => {
  const state = new Shared.SubtitleState(2);
  state.apply(event(0, 2, '甲', true));
  assert.strictEqual(state.apply(event(0, 2, '乙', true)), false);
  assert.strictEqual(state.lines()[0].original, '甲');
});

test('只保留最后 maxLines 句', () => {
  const state = new Shared.SubtitleState(2);
  for (let i = 0; i < 5; i += 1) state.apply(event(i, 1, `第${i}句`, true));
  const lines = state.lines();
  assert.strictEqual(lines.length, 2);
  assert.deepStrictEqual(lines.map((row) => row.segmentId), [3, 4]);
});

test('乱序到达的不同 segment 仍按 id 排序', () => {
  const state = new Shared.SubtitleState(3);
  state.apply(event(2, 1, '丙', true));
  state.apply(event(0, 1, '甲', true));
  state.apply(event(1, 1, '乙', true));
  assert.deepStrictEqual(state.lines().map((row) => row.original), ['甲', '乙', '丙']);
});

test('空文本不占行', () => {
  const state = new Shared.SubtitleState(2);
  state.apply(event(0, 1, '', true));
  assert.strictEqual(state.lines().length, 0);
});

test('translation 作为同句的更高 revision 到达', () => {
  // M5 的翻译就是这么接进来的：同一 segment_id，revision 递增。
  const state = new Shared.SubtitleState(2);
  state.apply(event(0, 1, 'こんにちは。', true));
  state.apply({ segment_id: 0, revision: 2, text: 'こんにちは。', translation: '你好。', is_final: true });
  const lines = state.lines();
  assert.strictEqual(lines[0].translation, '你好。');
  assert.strictEqual(lines[0].original, 'こんにちは。');
});

test('非法事件被安全忽略', () => {
  const state = new Shared.SubtitleState(2);
  assert.strictEqual(state.apply(null), false);
  assert.strictEqual(state.apply({}), false);
  assert.strictEqual(state.lines().length, 0);
});

console.log('\ncomposeLine');

test('三种显示模式', () => {
  const row = { original: '原文', translation: '译文', isFinal: true };
  const bilingual = Shared.composeLine(row, 'bilingual');
  assert.strictEqual(bilingual.original, '原文');
  assert.strictEqual(bilingual.translation, '译文');

  const onlyOriginal = Shared.composeLine(row, 'original');
  assert.strictEqual(onlyOriginal.original, '原文');
  assert.strictEqual(onlyOriginal.translation, '');

  const onlyTranslation = Shared.composeLine(row, 'translation');
  assert.strictEqual(onlyTranslation.original, '');
  assert.strictEqual(onlyTranslation.translation, '译文');
});

console.log('\npickMountParent（全屏消失的修复点）');

test('非全屏时挂到 documentElement', () => {
  const doc = { fullscreenElement: null, documentElement: { tag: 'html' } };
  assert.strictEqual(Shared.pickMountParent(doc).tag, 'html');
});

test('全屏时挂到 fullscreen element（挂到 html 上会完全不可见）', () => {
  const player = { tag: 'player' };
  const doc = { fullscreenElement: player, documentElement: { tag: 'html' } };
  assert.strictEqual(Shared.pickMountParent(doc).tag, 'player');
});

test('兼容 webkit 前缀', () => {
  const player = { tag: 'player' };
  const doc = { fullscreenElement: null, webkitFullscreenElement: player, documentElement: { tag: 'html' } };
  assert.strictEqual(Shared.pickMountParent(doc).tag, 'player');
});

test('标准属性优先于 webkit 属性', () => {
  const standard = { tag: 'standard' };
  const webkit = { tag: 'webkit' };
  const doc = { fullscreenElement: standard, webkitFullscreenElement: webkit, documentElement: { tag: 'html' } };
  assert.strictEqual(Shared.pickMountParent(doc).tag, 'standard');
});

console.log('\nfindStandaloneMedia（Chrome 直接打开的本地媒体文件）');

function mediaDoc(protocol, children) {
  return { location: { protocol }, body: { children } };
}

test('file:// 页面只有一个 <video> 时返回它', () => {
  const video = { tagName: 'VIDEO' };
  assert.strictEqual(Shared.findStandaloneMedia(mediaDoc('file:', [video])), video);
});

test('本地音频文件同样识别', () => {
  const audio = { tagName: 'AUDIO' };
  assert.strictEqual(Shared.findStandaloneMedia(mediaDoc('file:', [audio])), audio);
});

test('已注入的覆盖层不影响判断（扩展重载后重新注入）', () => {
  const video = { tagName: 'VIDEO' };
  const overlay = { tagName: 'DIV', id: 'local-live-subtitles-overlay' };
  assert.strictEqual(Shared.findStandaloneMedia(mediaDoc('file:', [video, overlay])), video);
});

test('本地的普通网页不算', () => {
  const doc = mediaDoc('file:', [{ tagName: 'DIV' }, { tagName: 'VIDEO' }]);
  assert.strictEqual(Shared.findStandaloneMedia(doc), null);
});

test('YouTube 页面不算', () => {
  assert.strictEqual(Shared.findStandaloneMedia(mediaDoc('https:', [{ tagName: 'VIDEO' }])), null);
});

console.log('\ncreateStore（永不抛错的存储封装）');

function fakeArea(backing) {
  return {
    async get(keys) {
      const out = {};
      for (const key of keys) if (key in backing) out[key] = backing[key];
      return out;
    },
    async set(patch) { Object.assign(backing, patch); },
  };
}

test('storage.session 可用时用它', () => {
  global.chrome = { storage: { session: fakeArea({}) } };
  const store = Shared.createStore('t');
  assert.strictEqual(store.where, 'session');
  delete global.chrome;
});

test('session 不可用时退回 local', () => {
  global.chrome = { storage: { local: fakeArea({}) } };
  const store = Shared.createStore('t');
  assert.strictEqual(store.where, 'local');
  delete global.chrome;
});

test('完全没有 chrome.storage 时退回内存，且不抛异常', () => {
  global.chrome = {};
  const store = Shared.createStore('t');
  assert.strictEqual(store.where, 'memory');
  delete global.chrome;
});

test('访问 chrome.storage 本身抛异常时也不能炸（就是真实故障的形态）', () => {
  Object.defineProperty(global, 'chrome', {
    configurable: true,
    get() { throw new TypeError("Cannot read properties of undefined (reading 'session')"); },
  });
  let store;
  assert.doesNotThrow(() => { store = Shared.createStore('t'); });
  assert.strictEqual(store.where, 'memory');
  delete global.chrome;
});

test('get/set 往返正确', async () => {
  const backing = {};
  global.chrome = { storage: { session: fakeArea(backing) } };
  const store = Shared.createStore('t');
  await store.set({ a: 1, b: 'x' });
  assert.deepStrictEqual(await store.get(['a', 'b']), { a: 1, b: 'x' });
  delete global.chrome;
});

test('底层 set 抛异常时只是警告，不把异常抛给调用方', async () => {
  // 状态上报路径上的任何失败都不该影响采集——这正是之前把启动带崩的地方。
  global.chrome = {
    storage: {
      session: {
        async get() { return {}; },
        async set() { throw new Error('quota exceeded'); },
      },
    },
  };
  const store = Shared.createStore('t');
  await assert.doesNotReject(() => store.set({ a: 1 }));
  delete global.chrome;
});

test('底层 get 抛异常时返回空对象而不是抛出', async () => {
  global.chrome = {
    storage: {
      session: {
        async get() { throw new Error('boom'); },
        async set() {},
      },
    },
  };
  const store = Shared.createStore('t');
  assert.deepStrictEqual(await store.get(['a']), {});
  delete global.chrome;
});

test('内存兜底时 set 之后能 get 回来', async () => {
  global.chrome = {};
  const store = Shared.createStore('t');
  await store.set({ k: 'v' });
  assert.deepStrictEqual(await store.get(['k']), { k: 'v' });
  delete global.chrome;
});

console.log('\n常量');

test('帧长与服务端约定一致（100ms @16k）', () => {
  assert.strictEqual(Shared.TARGET_SAMPLE_RATE, 16000);
  assert.strictEqual(Shared.FRAME_SAMPLES, 1600);
  assert.strictEqual(Shared.FRAME_MS, 100);
});

console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) {
  for (const failure of failures) console.error(`\n${failure.name}\n${failure.error.stack}`);
  process.exit(1);
}
