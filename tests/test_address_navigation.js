const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const state = { capturing: true, activeTabId: 1 };
const messages = [];
let updated;
const context = vm.createContext({
  importScripts() {}, console, setTimeout,
  self: { SubtitleShared: { createStore: () => ({ get: async () => state }), describeEnvironment() {} } },
  chrome: {
    commands: { onCommand: { addListener() {} } },
    storage: { onChanged: { addListener() {} } },
    tabs: { onUpdated: { addListener(fn) { updated = fn; } } },
    runtime: { onMessage: { addListener() {} }, sendMessage: async message => { messages.push(message); return { ok: true }; } },
  },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../extension/background.js'), 'utf8'), context);
(async () => {
  await updated(2, { url: 'https://www.youtube.com/watch?v=other' });
  await updated(1, { title: 'Same video' });
  assert.equal(messages.length, 0);
  await updated(1, { url: 'https://www.youtube.com/watch?v=new' });
  assert.equal(messages.at(-1).type, 'offscreen-reset-address-memory');
  state.capturing = false;
  await updated(1, { url: 'file:///next.mp4' });
  assert.equal(messages.length, 1);
  console.log('3 address navigation checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
