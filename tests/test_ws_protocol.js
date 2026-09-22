/**
 * 扩展 ↔ 服务的协议兼容性测试（Node 跑，不需要浏览器）。
 *
 *   node tests/test_ws_protocol.js --speed 3
 *
 * 验证的是最容易出错、又最难在浏览器里定位的一环：
 * **扩展切出来的 PCM 帧，服务端能不能正确解出成句的字幕。**
 * 用 extension/shared.js 里真实的 PcmFramer 和 floatToInt16，
 * 走真实的 WebSocket 协议，所以扩展端改了帧格式这里会立刻红。
 *
 * 不判断延迟（那要靠 tools/ws_client_test.py 按 1.0x 实测），只判断
 * "帧格式对得上、事件按 segment_id + revision 单调到达、定稿文本非空"。
 */

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');
const Shared = require('../extension/shared.js');

const WS_URL = process.env.LLS_WS_URL || Shared.WS_URL;

function parseArgs() {
  const args = { wav: path.join(__dirname, '..', 'sample_0230_0300.wav'), speed: 3, timeout: 90 };
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--wav') args.wav = argv[i + 1];
    if (argv[i] === '--speed') args.speed = Number(argv[i + 1]);
    if (argv[i] === '--timeout') args.timeout = Number(argv[i + 1]);
  }
  return args;
}

/** 极简 WAV 解析：只支持 16-bit PCM，够这个测试用。 */
function readWav(file) {
  const buffer = fs.readFileSync(file);
  if (buffer.toString('ascii', 0, 4) !== 'RIFF' || buffer.toString('ascii', 8, 12) !== 'WAVE') {
    throw new Error('不是 WAV 文件');
  }
  let offset = 12;
  let format = null;
  let data = null;
  while (offset + 8 <= buffer.length) {
    const id = buffer.toString('ascii', offset, offset + 4);
    const size = buffer.readUInt32LE(offset + 4);
    const body = offset + 8;
    if (id === 'fmt ') {
      format = {
        channels: buffer.readUInt16LE(body + 2),
        sampleRate: buffer.readUInt32LE(body + 4),
        bits: buffer.readUInt16LE(body + 14),
      };
    } else if (id === 'data') {
      data = buffer.subarray(body, body + size);
    }
    offset = body + size + (size % 2);
  }
  if (!format || !data) throw new Error('WAV 缺少 fmt 或 data 块');
  if (format.bits !== 16 || format.channels !== 1 || format.sampleRate !== Shared.TARGET_SAMPLE_RATE) {
    throw new Error(`需要 16kHz 单声道 16-bit，实际 ${format.sampleRate}Hz/${format.channels}ch/${format.bits}bit`);
  }
  return data;
}

function pct(values, q) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor((sorted.length - 1) * q))];
}

async function main() {
  const args = parseArgs();
  const pcm = readWav(args.wav);
  const totalSamples = pcm.length / 2;
  console.log(`音频 ${(totalSamples / Shared.TARGET_SAMPLE_RATE).toFixed(2)}s，`
    + `速度 ${args.speed}x，目标 ${WS_URL}`);

  const events = [];
  let statusMessage = null;
  let frameCount = 0;
  let sentSamples = 0;

  const socket = new WebSocket(WS_URL);
  socket.binaryType = 'arraybuffer';

  await new Promise((resolve, reject) => {
    const guard = setTimeout(() => reject(new Error('连接超时（服务没启动？）')), 10000);
    socket.onopen = () => { clearTimeout(guard); resolve(); };
    socket.onerror = () => { clearTimeout(guard); reject(new Error(`无法连接 ${WS_URL}`)); };
  });
  console.log('已连接');

  socket.onmessage = (message) => {
    const payload = JSON.parse(message.data);
    if (payload.type === 'status') statusMessage = payload;
    if (payload.type === 'event') events.push(payload);
  };

  socket.send(JSON.stringify({ type: 'start', language: 'ja' }));

  // 完全照扩展端的路径走一遍：Int16 → Float32 → floatToInt16 → 分帧 → 发送。
  const framer = new Shared.PcmFramer();
  const started = Date.now();
  const blockSamples = 128; // AudioWorklet 的 process() 就是 128 个采样一块
  for (let index = 0; index < totalSamples; index += blockSamples) {
    const view = new Int16Array(pcm.buffer, pcm.byteOffset + index * 2,
      Math.min(blockSamples, totalSamples - index));
    const float = Float32Array.from(view, (value) => value / 32768);
    framer.push(float);
    for (const frame of framer.drain()) {
      const int16 = Shared.floatToInt16(frame);
      socket.send(int16.buffer);
      frameCount += 1;
      sentSamples += int16.length;
    }
    const targetMs = ((index + blockSamples) / Shared.TARGET_SAMPLE_RATE) * 1000 / args.speed;
    const waitMs = targetMs - (Date.now() - started);
    if (waitMs > 0) await new Promise((resolve) => setTimeout(resolve, waitMs));
  }

  console.log(`已推送 ${frameCount} 帧 / ${sentSamples} 样点`
    + `（${(sentSamples / Shared.TARGET_SAMPLE_RATE).toFixed(2)}s）`);

  // 等最后一句被冲刷出来
  const deadline = Date.now() + args.timeout * 1000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 200));
    const finals = events.filter((event) => event.is_final && event.text);
    if (finals.length && Date.now() - started > (totalSamples / Shared.TARGET_SAMPLE_RATE / args.speed) * 1000 + 2000) {
      break;
    }
  }
  socket.send(JSON.stringify({ type: 'stop' }));
  await new Promise((resolve) => setTimeout(resolve, 400));
  socket.close();

  console.log(`\n收到 ${events.length} 个事件，服务状态：${JSON.stringify(statusMessage)}`);

  const partials = events.filter((event) => !event.is_final);
  const finals = events.filter((event) => event.is_final && event.text);
  const latencies = partials.map((event) => event.latency);

  console.log('\n---- 断言 ----');
  const checks = [];
  const check = (name, fn) => {
    try {
      fn();
      checks.push([name, true, '']);
      console.log(`  ok   ${name}`);
    } catch (error) {
      checks.push([name, false, error.message]);
      console.log(`  FAIL ${name}\n       ${error.message}`);
    }
  };

  check('服务端回复了 status（握手成功）', () => {
    assert.ok(statusMessage, '没有收到 status');
    assert.strictEqual(statusMessage.state, 'listening');
  });

  check('收到过 partial 事件（帧格式能被服务端解析）', () => {
    assert.ok(partials.length > 0, '一个 partial 都没有说明 PCM 没被正确解析');
  });

  check('收到非空 final（识别出了内容）', () => {
    assert.ok(finals.length > 0, '没有非空 final');
  });

  check('revision 单调递增（字幕不会回跳）', () => {
    const highest = new Map();
    for (const event of events) {
      const key = event.segment_id;
      if (highest.has(key) && event.revision <= highest.get(key)) {
        throw new Error(`seg=${key} revision 从 ${highest.get(key)} 退回 ${event.revision}`);
      }
      highest.set(key, event.revision);
    }
  });

  check('每个 segment 最多一个 final', () => {
    const finalsPerSegment = new Map();
    for (const event of events) {
      if (!event.is_final) continue;
      const key = event.segment_id;
      finalsPerSegment.set(key, (finalsPerSegment.get(key) || 0) + 1);
    }
    for (const [key, count] of finalsPerSegment) {
      assert.strictEqual(count, 1, `seg=${key} 有 ${count} 个 final`);
    }
  });

  check('final 的 revision 不小于该句任何 partial', () => {
    const lastPartial = new Map();
    for (const event of events) {
      const key = event.segment_id;
      if (!event.is_final) lastPartial.set(key, event.revision);
      else if (lastPartial.has(key)) {
        assert.ok(event.revision > lastPartial.get(key),
          `seg=${key} final rev=${event.revision} <= partial rev=${lastPartial.get(key)}`);
      }
    }
  });

  check('音频位置单调不倒退', () => {
    let previous = -1;
    for (const event of events) {
      assert.ok(event.audio_end >= previous - 1e-6,
        `audio_end ${event.audio_end} < 上一个 ${previous}`);
      previous = Math.max(previous, event.audio_start);
    }
  });

  if (latencies.length && args.speed === 1) {
    // 非 1.0x 时服务端的延迟推算不成立（它假设音频按 1x 到达），
    // 所以只在真实速度下报延迟，避免把测试假象当成结论。
    console.log(`\n草稿延迟 p50=${pct(latencies, 0.5).toFixed(3)}s `
      + `p95=${pct(latencies, 0.95).toFixed(3)}s max=${Math.max(...latencies).toFixed(3)}s`);
  } else if (latencies.length) {
    console.log(`\n（速度 ${args.speed}x，延迟数字无意义故不报；测延迟请用 --speed 1）`);
  }
  console.log('\n---- 定稿文本 ----');
  console.log(finals.map((event) => event.text).join(' '));

  const failed = checks.filter(([, ok]) => !ok);
  console.log(`\n${checks.length - failed.length}/${checks.length} 项通过`);
  return failed.length ? 1 : 0;
}

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(`\n失败：${error.message}`);
    process.exit(1);
  });
