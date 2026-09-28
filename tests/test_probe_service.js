/**
 * 探测函数探测真实服务的集成测试（Node 跑，服务要开着）。
 *
 *   node tests/test_probe_service.js
 *
 * 这条测试专门守一个曾经真实发生过的坑：popup 的"检查本地服务"按钮
 * 原本依赖 offscreen 文档写的状态，而 offscreen 只有在点过「开始捕获」后才存在，
 * 于是服务明明在跑、popup 却一直显示"未连接本地服务"。
 *
 * probeService 必须**独立于采集**就能判断服务在不在——这条测试就是证明这一点。
 */

const assert = require('node:assert');
const Shared = require('../extension/shared.js');

const WS_URL = process.env.LLS_WS_URL || Shared.WS_URL;

let passed = 0;
const failures = [];

async function test(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log(`  ok   ${name}`);
  } catch (error) {
    failures.push({ name, error });
    console.log(`  FAIL ${name}\n       ${error.message}`);
  }
}

async function main() {
  console.log(`探测目标：${WS_URL}\n`);

  await test('服务在线时能探测到，并拿到引擎信息', async () => {
    const status = await Shared.probeService(WS_URL, 5000);
    assert.ok(status, '探测返回 null，服务没在跑？');
    assert.strictEqual(status.type, 'status');
    assert.ok(status.engine, '缺少 engine 字段');
    console.log(`       引擎=${status.engine} 模型=${status.model}`);
  });

  await test('不依赖任何采集会话（这正是原来的 bug）', async () => {
    // 连续探测两次，中间什么都不做；如果它依赖采集状态，第二次就会失败。
    const first = await Shared.probeService(WS_URL, 5000);
    const second = await Shared.probeService(WS_URL, 5000);
    assert.ok(first && second, '连续探测应当都成功');
  });

  await test('探测不会留下悬挂的连接（能反复调用）', async () => {
    for (let i = 0; i < 5; i += 1) {
      const status = await Shared.probeService(WS_URL, 5000);
      assert.ok(status, `第 ${i + 1} 次探测失败`);
    }
  });

  await test('连不上的地址返回 null 而不是抛异常或永久挂起', async () => {
    const started = Date.now();
    const status = await Shared.probeService('ws://127.0.0.1:1', 1500);
    const elapsed = Date.now() - started;
    assert.strictEqual(status, null);
    assert.ok(elapsed < 4000, `耗时 ${elapsed}ms，超时没有生效`);
  });

  console.log(`\n${passed} passed, ${failures.length} failed`);
  if (failures.length) {
    for (const failure of failures) console.error(`\n${failure.name}\n${failure.error.stack}`);
    process.exit(1);
  }
}

main();
