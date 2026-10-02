/**
 * 网页内浮窗：把弹窗（popup.html?embedded=1）嵌进页面，切视频、换模型不用每次点扩展图标。
 *
 * - 只在字幕进行中的标签页出现（用户 10-01 选定），停止字幕后自动消失；
 * - 收起时是一个可拖动的小圆钮，颜色表示状态，鼠标不在上面时半透明；
 * - 展开后是面板：拖标题栏移动，拖右下角调宽度，高度跟随内容；位置、宽度、收起状态都记住；
 * - 全屏时跟字幕一样挂进全屏元素 / 顶层，不会被挡住；
 * - 快捷键 Alt+S（manifest commands，可在 chrome://extensions/shortcuts 改）显示/隐藏。
 *
 * 面板内容是扩展自己的页面（iframe），所以里面的按钮算"扩展界面上的用户操作"，可以直接开始/切换捕获。
 * 用 closed shadow root 隔离样式：页面的 CSS 不会影响浮窗，浮窗的样式也不会漏到页面上。
 */

(() => {
  const Shared = window.SubtitleShared;
  if (!Shared || window.__llsPanel) {
    if (window.__llsPanel) window.__llsPanel.refresh();
    return;
  }

  const HOST_ID = 'local-live-subtitles-panel';
  const MIN_WIDTH = 260;
  const MAX_WIDTH = 520;
  const BUTTON = 34;

  const prefs = { panelLeft: null, panelTop: null, panelWidth: 300, panelOpen: false, panelHidden: false };
  let capturingHere = false;
  let host = null;
  let root = null;
  let ui = {};

  // 外壳颜色与弹窗页（popup.css）一致：跟随系统深浅色
  const CSS = `
:host{all:initial;--bg:#f6f7f9;--bar:#ffffff;--text:#1c1f24;--muted:#6b7280;--line:#e3e6eb;--hover:#eef0f3;--fab:#ffffff}
@media (prefers-color-scheme: dark){
  :host{--bg:#16181c;--bar:#1f2227;--text:#e8eaed;--muted:#9aa1ab;--line:#2c3036;--hover:#2a2e34;--fab:#1f2227}
}
.wrap{position:fixed;z-index:2147483647;font:13px/1.4 "Microsoft YaHei UI","Segoe UI",system-ui,sans-serif}
.fab{width:${BUTTON}px;height:${BUTTON}px;border-radius:50%;border:0;cursor:grab;display:flex;align-items:center;
  justify-content:center;background:var(--fab);box-shadow:0 2px 10px rgba(0,0,0,.45);opacity:.45;transition:opacity .15s;padding:0}
.fab:hover,.fab:focus-visible{opacity:1}
.fab:focus-visible{outline:2px solid #5b8cff;outline-offset:2px}
.fab .dot{width:12px;height:12px;border-radius:50%;background:#3ccf7a}
.fab.warn .dot{background:#f0b429}
.fab.idle .dot{background:#9aa1ab}
.panel{display:none;flex-direction:column;border-radius:12px;overflow:hidden;background:var(--bg);
  box-shadow:0 8px 30px rgba(0,0,0,.45);border:1px solid var(--line)}
.open .panel{display:flex}
.open .fab{display:none}
.bar{display:flex;align-items:center;gap:8px;padding:6px 8px 6px 12px;background:var(--bar);cursor:grab;
  user-select:none;color:var(--text);font-weight:600;border-bottom:1px solid var(--line)}
.bar .title{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar button{all:unset;cursor:pointer;width:24px;height:24px;border-radius:6px;display:flex;align-items:center;
  justify-content:center;color:var(--muted);font-size:15px}
.bar button:hover{background:var(--hover);color:var(--text)}
.bar button:focus-visible{outline:2px solid #5b8cff}
iframe{border:0;width:100%;height:420px;display:block;background:transparent;color-scheme:normal}
.grip{position:absolute;right:0;bottom:0;width:14px;height:14px;cursor:ew-resize;
  background:linear-gradient(135deg,transparent 50%,var(--muted) 50%,var(--muted) 60%,transparent 60%,transparent 70%,var(--muted) 70%,var(--muted) 80%,transparent 80%);opacity:.6}
.dragging iframe{pointer-events:none}
`;

  function build() {
    host = document.createElement('div');
    host.id = HOST_ID;
    root = host.attachShadow({ mode: 'closed' });
    const style = document.createElement('style');
    style.textContent = CSS;
    const wrap = document.createElement('div');
    wrap.className = 'wrap';
    wrap.innerHTML = `
      <button class="fab" type="button" title="本地实时字幕（点击展开，拖动可移动）" aria-label="展开本地字幕面板">
        <span class="dot" aria-hidden="true"></span>
      </button>
      <section class="panel" role="dialog" aria-label="本地实时字幕">
        <div class="bar">
          <span class="title">本地实时字幕</span>
          <button type="button" class="min" title="收起" aria-label="收起面板">–</button>
          <button type="button" class="close" title="隐藏浮窗（按 Alt+S，或在扩展弹窗顶部打开「浮窗」开关找回）" aria-label="隐藏浮窗">×</button>
        </div>
        <iframe title="本地实时字幕设置" allow="" src="${chrome.runtime.getURL('popup.html?embedded=1')}"></iframe>
        <div class="grip" title="拖动调整宽度" aria-hidden="true"></div>
      </section>`;
    root.append(style, wrap);
    ui = {
      wrap,
      fab: wrap.querySelector('.fab'),
      panel: wrap.querySelector('.panel'),
      bar: wrap.querySelector('.bar'),
      frame: wrap.querySelector('iframe'),
      grip: wrap.querySelector('.grip'),
    };
    ui.wrap.querySelector('.min').addEventListener('click', () => setOpen(false));
    ui.wrap.querySelector('.close').addEventListener('click', () => save({ panelHidden: true }));
    draggable(ui.fab, () => setOpen(true));
    draggable(ui.bar, null);
    resizable(ui.grip);
  }

  // --- 位置 / 大小 ----------------------------------------------------------------

  function clamp() {
    const width = prefs.panelOpen ? prefs.panelWidth : BUTTON;
    const height = prefs.panelOpen ? ui.panel.offsetHeight || 300 : BUTTON;
    const left = prefs.panelLeft ?? window.innerWidth - width - 24;
    const top = prefs.panelTop ?? 80;
    // 整个面板都留在窗口内（窗口比面板还小时，优先保证标题栏可见，才能拖回来）
    return {
      left: Math.max(4, Math.min(left, window.innerWidth - width - 4)),
      top: Math.max(4, Math.min(top, window.innerHeight - height - 4)),
    };
  }

  function layout() {
    if (!ui.wrap) return;
    const { left, top } = clamp();
    ui.wrap.style.left = `${left}px`;
    ui.wrap.style.top = `${top}px`;
    ui.panel.style.width = `${prefs.panelWidth}px`;
    ui.wrap.classList.toggle('open', prefs.panelOpen);
  }

  /** 拖动：移动超过 4px 才算拖，否则当作点击（onClick）。 */
  function draggable(handle, onClick) {
    handle.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || event.target.closest('.bar button')) return;
      const start = { x: event.clientX, y: event.clientY, ...clamp() };
      let moved = false;
      handle.setPointerCapture(event.pointerId);
      ui.wrap.classList.add('dragging');
      const move = (e) => {
        const dx = e.clientX - start.x;
        const dy = e.clientY - start.y;
        if (!moved && Math.hypot(dx, dy) < 4) return;
        moved = true;
        prefs.panelLeft = start.left + dx;
        prefs.panelTop = start.top + dy;
        layout();
      };
      const up = () => {
        handle.removeEventListener('pointermove', move);
        ui.wrap.classList.remove('dragging');
        if (moved) save({ panelLeft: clamp().left, panelTop: clamp().top });
        else if (onClick) onClick();
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up, { once: true });
      handle.addEventListener('pointercancel', up, { once: true });
    });
    // 键盘：圆钮可以用回车/空格展开（button 自带），方向键微调位置
    handle.addEventListener('keydown', (event) => {
      const step = event.shiftKey ? 40 : 10;
      const delta = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[event.key];
      if (!delta) return;
      event.preventDefault();
      const { left, top } = clamp();
      save({ panelLeft: left + delta[0], panelTop: top + delta[1] });
    });
  }

  function resizable(grip) {
    grip.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      const startX = event.clientX;
      const startWidth = prefs.panelWidth;
      grip.setPointerCapture(event.pointerId);
      ui.wrap.classList.add('dragging');
      const move = (e) => {
        prefs.panelWidth = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, startWidth + e.clientX - startX));
        layout();
      };
      const up = () => {
        grip.removeEventListener('pointermove', move);
        ui.wrap.classList.remove('dragging');
        save({ panelWidth: prefs.panelWidth });
      };
      grip.addEventListener('pointermove', move);
      grip.addEventListener('pointerup', up, { once: true });
    });
  }

  // --- 显示 / 挂载 ----------------------------------------------------------------

  function shouldShow() {
    return capturingHere && !prefs.panelHidden;
  }

  /** 和字幕一样：全屏时挂进全屏元素；本地媒体页全屏的是 <video> 本身，改用 popover 顶层。 */
  function mount() {
    if (!shouldShow()) {
      if (host && host.isConnected) host.remove();
      return;
    }
    if (!host) build();
    const media = Shared.findStandaloneMedia(document);
    const parent = media ? document.body : Shared.pickMountParent(document);
    if (host.parentElement !== parent) parent.appendChild(host);
    if (media) {
      if (!host.hasAttribute('popover')) {
        host.setAttribute('popover', 'manual');
        host.style.cssText = 'inset:auto;margin:0;padding:0;border:0;background:transparent;overflow:visible';
      }
      try {
        if (host.matches(':popover-open')) host.hidePopover();
        host.showPopover();
      } catch (error) { /* 老版本没有 popover：全屏时可能被挡，非全屏不受影响 */ }
    }
    layout();
  }

  function setOpen(open) {
    save({ panelOpen: open });
    if (open) setTimeout(() => ui.frame && ui.frame.focus(), 50);
    else setTimeout(() => ui.fab && ui.fab.focus(), 50);
  }

  function save(patch) {
    Object.assign(prefs, patch);
    chrome.storage.local.set(patch);
    mount();
  }

  function setTone(capture) {
    if (!ui.fab) return;
    const starting = capture && (capture.state === 'starting' || capture.state === 'connecting');
    ui.fab.classList.toggle('warn', Boolean(starting || (capture && capture.error)));
    ui.fab.classList.toggle('idle', !capturingHere);
  }

  // --- 状态来源 -------------------------------------------------------------------

  async function refresh() {
    const state = await chrome.runtime.sendMessage({ type: 'query-state' }).catch(() => null);
    const me = await chrome.runtime.sendMessage({ type: 'whoami' }).catch(() => null);
    capturingHere = Boolean(state && state.capturing && me && state.activeTabId === me.id);
    mount();
  }

  window.addEventListener('message', (event) => {
    // iframe（扩展页）报告内容高度，面板高度跟着走，避免出现内部滚动条
    if (!ui.frame || event.source !== ui.frame.contentWindow || !event.data || event.data.lls !== 'panel-height') return;
    const max = Math.max(200, window.innerHeight - 80);
    ui.frame.style.height = `${Math.min(event.data.height, max)}px`;
    layout();
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local') {
      let touched = false;
      for (const key of Object.keys(prefs)) {
        if (changes[key]) { prefs[key] = changes[key].newValue; touched = true; }
      }
      if (touched) mount();
    }
  });

  chrome.runtime.onMessage.addListener((message) => {
    if (message && message.type === 'capture-state') {
      // background 转告的捕获状态变化（content script 读不到 storage.session）
      if (message.status !== undefined) setTone(message.status);
      if (message.state) refresh();
      return;
    }
    if (message && message.type === 'toggle-panel') {
      // 快捷键：隐藏着就显示并展开；显示着就隐藏
      save(prefs.panelHidden || !prefs.panelOpen ? { panelHidden: false, panelOpen: true } : { panelHidden: true });
    }
  });

  document.addEventListener('fullscreenchange', mount, true);
  window.addEventListener('resize', layout);

  chrome.storage.local.get(prefs, (value) => {
    Object.assign(prefs, value);
    refresh();
  });

  window.__llsPanel = { refresh };
})();
