/** 服务日志页：经一键启动宿主读 runs/service.log 的最后 400 行。 */

const logBox = document.querySelector('#log');
const pathLine = document.querySelector('#path');
const auto = document.querySelector('#auto');

async function load() {
  let reply = null;
  try {
    reply = await chrome.runtime.sendNativeMessage('local.live_subtitles', { cmd: 'log', lines: 400 });
  } catch (error) {
    logBox.textContent = `读不到日志：一键启动宿主不可用（${error.message || error}）。\n`
      + '运行项目里的「安装一键启动.bat」后重新加载扩展。';
    return;
  }
  if (!reply || reply.ok === false) {
    logBox.textContent = `读不到日志：${(reply && reply.error) || '宿主无响应'}`;
    return;
  }
  // 只在用户停在底部时自动滚到底，别打断往上翻看
  const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 40;
  pathLine.textContent = reply.path;
  logBox.textContent = reply.text || '（日志为空）';
  if (atBottom) window.scrollTo(0, document.body.scrollHeight);
}

document.querySelector('#reload').addEventListener('click', load);
setInterval(() => { if (auto.checked && !document.hidden) load(); }, 3000);
load().then(() => window.scrollTo(0, document.body.scrollHeight));
