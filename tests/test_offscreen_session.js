const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const Shared = require('../extension/shared.js');

const sockets = [];
const messages = [];
class FakeSocket {
  static OPEN = 1;
  constructor() { this.readyState = 1; sockets.push(this); }
  send() {}
  close() { this.readyState = 3; }
}
const context = vm.createContext({
  self: { SubtitleShared: { ...Shared, describeEnvironment() {},
    createStore: () => ({ set: async () => {} }) } },
  chrome: { runtime: { sendMessage: async (message) => { messages.push(message); },
    onMessage: { addListener() {} } } },
  WebSocket: FakeSocket, console, setTimeout, clearTimeout,
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../extension/offscreen.js'), 'utf8'), context);

async function connect(session) {
  vm.runInContext(`sessionId = ${session}`, context);
  const ready = context.connectSocket('en');
  sockets.at(-1).onopen();
  await ready;
  return sockets.at(-1);
}

async function main() {
  const old = await connect(100);
  await context.stop();
  const current = await connect(200);
  messages.length = 0;
  old.onmessage({ data: JSON.stringify({ type: 'event', segment_id: 0, text: 'Old' }) });
  assert.equal(messages.length, 0, 'old subtitle must not be tagged with the new session');
  old.onclose();
  assert.equal(messages.length, 0, 'old close must not mark the new connection closed');
  current.onmessage({ data: JSON.stringify({ type: 'event', segment_id: 0, text: 'New' }) });
  assert.equal(messages.find((m) => m.type === 'subtitle-event').payload.session, 200);
  await context.stop();
  messages.length = 0;
  current.onmessage({ data: JSON.stringify({ type: 'event', segment_id: 1, text: 'Late' }) });
  assert.equal(messages.length, 0, 'stopped capture must not repopulate cleared subtitles');
  console.log('4 offscreen session checks passed');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
