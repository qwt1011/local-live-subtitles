/**
 * Content script：字幕覆盖层。
 *
 * 修复的三件事（都来自 ARCHITECTURE_REVIEW.md S3/S6）：
 *
 * 1. **全屏下字幕消失**。原来覆盖层挂在 document.documentElement 上，
 *    而 YouTube 进入全屏后只有 fullscreen element 及其后代参与渲染，
 *    挂在 <html> 上的兄弟节点不会显示。现在挂到 document.fullscreenElement 内部，
 *    并在 fullscreenchange 时重新挂载。
 * 2. **字幕回跳 / 闪烁**。现在用 segment_id + revision 状态机：
 *    partial 原地替换成草稿样式，final 到达后转成定稿样式，
 *    迟到的旧 revision 直接丢弃。
 * 3. **刷新后设置回落**。现在 mode / enabled / 位置 / 字号都从 chrome.storage.local 读回，
 *    并且监听 storage.onChanged，popup 改了不用重新注入。
 */

(() => {
  const Shared = window.SubtitleShared;
  if (!Shared) return;

  const OVERLAY_ID = 'local-live-subtitles-overlay';
  const LINE_COUNT = 2;

  const settings = {
    enabled: true,
    mode: 'bilingual',
    fontSize: 22,
    left: null,
    top: null,
  };

  let overlay = null;
  let box = null;
  const slots = [];
  const state = new Shared.SubtitleState(LINE_COUNT);

  const STYLE = `
#${OVERLAY_ID}{
  position:fixed;z-index:2147483647;left:50%;bottom:8%;transform:translateX(-50%);
  max-width:88%;pointer-events:none;display:none;
  font:600 var(--lls-font-size,22px)/1.45 "Hiragino Sans","Yu Gothic UI",Arial,sans-serif;
}
#${OVERLAY_ID} .lls-box{
  pointer-events:auto;cursor:move;user-select:none;
  padding:8px 16px;border-radius:6px;background:rgba(0,0,0,.5);
  text-align:center;color:#fff;text-shadow:0 2px 4px #000;
}
#${OVERLAY_ID} .lls-line{display:block}
#${OVERLAY_ID} .lls-line + .lls-line{margin-top:2px}
#${OVERLAY_ID} .lls-orig{display:block}
#${OVERLAY_ID} .lls-trans{display:block;color:#ffe9a8;font-size:.9em}
#${OVERLAY_ID} .lls-line.is-pending .lls-orig,
#${OVERLAY_ID} .lls-line.is-pending .lls-trans{opacity:.62}
#${OVERLAY_ID} .lls-line.is-pending .lls-orig::after{
  content:"…";opacity:.8;margin-left:2px
}
#${OVERLAY_ID}.lls-dragging .lls-box{outline:1px solid rgba(255,255,255,.45)}
`;

  function buildOverlay() {
    overlay = document.createElement('div');
    overlay.id = OVERLAY_ID;

    const style = document.createElement('style');
    style.textContent = STYLE;
    document.documentElement.appendChild(style);

    box = document.createElement('div');
    box.className = 'lls-box';
    for (let i = 0; i < LINE_COUNT; i += 1) {
      const line = document.createElement('span');
      line.className = 'lls-line';
      const orig = document.createElement('span');
      orig.className = 'lls-orig';
      const trans = document.createElement('span');
      trans.className = 'lls-trans';
      line.appendChild(orig);
      line.appendChild(trans);
      box.appendChild(line);
      slots.push({ line, orig, trans });
    }
    overlay.appendChild(box);
    attachInteractions();
  }

  /**
   * 挂载点必须是 fullscreen element 的后代，否则全屏时整个覆盖层不会渲染。
   * 这是 DEVELOPMENT.md 验收项"全屏可用"之前一直没过的原因。
   */
  function mount() {
    if (!overlay) return;
    const parent = Shared.pickMountParent(document);
    if (overlay.parentElement !== parent) parent.appendChild(overlay);
  }

  function applyPosition() {
    if (settings.left === null || settings.top === null) return;
    overlay.style.left = `${settings.left}px`;
    overlay.style.top = `${settings.top}px`;
    overlay.style.bottom = 'auto';
    overlay.style.transform = 'none';
  }

  function applyFontSize() {
    overlay.style.setProperty('--lls-font-size', `${settings.fontSize}px`);
  }

  function render() {
    if (!overlay) return;
    if (!settings.enabled) {
      overlay.style.display = 'none';
      return;
    }
    const lines = state.lines();
    if (!lines.length) {
      overlay.style.display = 'none';
      return;
    }

    // 新的一行永远落在最后一个槽位，旧的一行上移，读起来是自然的滚动感。
    const offset = LINE_COUNT - lines.length;
    for (let i = 0; i < LINE_COUNT; i += 1) {
      const slot = slots[i];
      const row = lines[i - offset];
      if (!row) {
        slot.line.style.display = 'none';
        continue;
      }
      const composed = Shared.composeLine(row, settings.mode);
      setText(slot.orig, composed.original);
      setText(slot.trans, composed.translation);
      slot.line.style.display = 'block';
      slot.line.classList.toggle('is-pending', !composed.isFinal);
    }
    overlay.style.display = 'block';
  }

  /** 只在内容真的变了才写 DOM，避免每次 partial 都触发重排造成闪烁。 */
  function setText(node, value) {
    const next = value || '';
    if (node.textContent !== next) node.textContent = next;
  }

  function attachInteractions() {
    let dragging = false;
    let offsetX = 0;
    let offsetY = 0;

    box.addEventListener('pointerdown', (event) => {
      dragging = true;
      const rect = overlay.getBoundingClientRect();
      offsetX = event.clientX - rect.left;
      offsetY = event.clientY - rect.top;
      overlay.classList.add('lls-dragging');
      box.setPointerCapture(event.pointerId);
    });

    box.addEventListener('pointermove', (event) => {
      if (!dragging) return;
      const left = event.clientX - offsetX;
      const top = event.clientY - offsetY;
      overlay.style.left = `${left}px`;
      overlay.style.top = `${top}px`;
      overlay.style.bottom = 'auto';
      overlay.style.transform = 'none';
      settings.left = left;
      settings.top = top;
    });

    const finish = () => {
      if (!dragging) return;
      dragging = false;
      overlay.classList.remove('lls-dragging');
      chrome.storage.local.set({ left: settings.left, top: settings.top });
    };
    box.addEventListener('pointerup', finish);
    box.addEventListener('pointercancel', finish);

    box.addEventListener('wheel', (event) => {
      event.preventDefault();
      settings.fontSize = Math.max(12, Math.min(48, settings.fontSize + (event.deltaY < 0 ? 2 : -2)));
      applyFontSize();
      chrome.storage.local.set({ fontSize: settings.fontSize });
    }, { passive: false });
  }

  function handleMessage(message) {
    if (!message) return;
    if (message.type === 'subtitle') {
      if (state.apply(message.payload)) render();
    } else if (message.type === 'status') {
      // 连接状态只在 popup 里展示，覆盖层不显示，避免打扰观看。
    }
  }

  function init() {
    if (document.getElementById(OVERLAY_ID)) return; // 避免重复注入
    buildOverlay();

    chrome.storage.local.get(
      { enabled: true, mode: 'bilingual', fontSize: 22, left: null, top: null },
      (value) => {
        Object.assign(settings, value);
        applyPosition();
        applyFontSize();
        mount();
        render();
      },
    );

    // popup 改动后立即生效，不依赖消息（消息可能在 service worker 回收时丢）。
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== 'local') return;
      if (changes.mode) settings.mode = changes.mode.newValue;
      if (changes.enabled) settings.enabled = changes.enabled.newValue;
      if (changes.fontSize) {
        settings.fontSize = changes.fontSize.newValue;
        applyFontSize();
      }
      render();
    });

    document.addEventListener('fullscreenchange', mount, true);
    document.addEventListener('webkitfullscreenchange', mount, true);
    chrome.runtime.onMessage.addListener(handleMessage);
  }

  init();
})();
