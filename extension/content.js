/**
 * Content script：字幕覆盖层。
 *
 * 修复的三件事（都来自 docs/ARCHITECTURE_REVIEW.md S3/S6）：
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
 *
 * 本地文件（file:///…mp4，用 Chrome 直接打开）另走一条挂载路径：那种页面全屏的是
 * <video> 元素本身，媒体元素不能有子节点，"挂到全屏元素里"行不通。
 * 这时把覆盖层做成 popover 放进顶层（top layer）：顶层里后放入的元素显示在上面，
 * 所以每次全屏切换后重新 showPopover() 一次，覆盖层就压在全屏视频之上。
 */

(() => {
  const Shared = window.SubtitleShared;
  if (!Shared) return;

  const OVERLAY_ID = 'local-live-subtitles-overlay';
  // 3 行而不是 2 行：实测（tools/diag_display.py）译文平均在定稿后 2–3 秒才到，
  // 2 行时长句的译文常在到达前就被后面两句挤出屏幕；3 行时这类情况为 0。
  const LINE_COUNT = 3;

  const settings = {
    enabled: true,
    mode: 'bilingual',
    fontSize: 22,
    left: null,
    top: null,
  };

  let overlay = null;
  let box = null;
  let standaloneMedia = null;   // Chrome 直接打开的本地媒体文件里的 <video>/<audio>
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
#${OVERLAY_ID}[popover]{
  inset:auto;left:50%;bottom:8%;margin:0;padding:0;border:0;
  background:transparent;color:inherit;overflow:visible;width:max-content;
}
`;

  function buildOverlay() {
    overlay = document.createElement('div');
    overlay.id = OVERLAY_ID;

    const style = document.createElement('style');
    style.dataset.lls = '1';   // teardown() 靠这个标记找回并清掉旧样式
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
    if (standaloneMedia) {
      mountTopLayer();
      return;
    }
    const parent = Shared.pickMountParent(document);
    if (overlay.parentElement !== parent) parent.appendChild(overlay);
  }

  /** 本地媒体页面：覆盖层放进顶层，并在全屏切换后重新放一次，保证排在全屏视频之上。 */
  function mountTopLayer() {
    if (overlay.parentElement !== document.body) document.body.appendChild(overlay);
    if (!overlay.hasAttribute('popover')) overlay.setAttribute('popover', 'manual');
    try {
      if (overlay.matches(':popover-open')) overlay.hidePopover();
      overlay.showPopover();
    } catch (error) {
      console.warn('[本地字幕] popover 不可用，全屏时字幕可能被视频挡住：', error);
    }
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

  function handleMessage(message, _sender, sendResponse) {
    if (!message) return;
    if (message.type === 'overlay-ping') {
      // background 开始捕获前用它确认本页有字幕脚本，没有才补注入。
      sendResponse({ ok: true });
      return;
    }
    if (message.type === 'subtitle') {
      if (state.apply(message.payload)) {
        if (message.payload.is_final && !message.payload.translation) {
          console.log('[本地字幕] 定稿:', message.payload.text);
        }
        if (message.payload.translation) {
          console.log('[本地字幕] 译文:', message.payload.translation);
        }
        render();
      }
    } else if (message.type === 'status') {
      // 连接状态只在 popup 里展示，覆盖层不显示，避免打扰观看。
      if (message.status && message.status.error) {
        console.warn('[本地字幕] 采集异常:', message.status.error);
      }
    }
  }

  /**
   * 重新注入时，把上一版留下的覆盖层和样式清掉再重建。
   *
   * 原来这里是 `if (已有覆盖层) return;`——那样在"改了扩展代码后重载扩展"时，
   * 新注入的 content script 会因为发现旧覆盖层而**直接退出**，
   * 既不注册消息监听也不重新渲染，表现就是"改了代码但页面毫无反应"，
   * 而唯一的解法（刷新页面）看起来又像是没生效。这是开发期最容易踩的坑。
   */
  function teardown() {
    const existing = document.getElementById(OVERLAY_ID);
    if (existing) existing.remove();
    for (const node of document.querySelectorAll('style[data-lls]')) node.remove();
    slots.length = 0;
    state.reset();
  }

  function init() {
    teardown();
    standaloneMedia = Shared.findStandaloneMedia(document);
    if (location.protocol === 'file:' && !standaloneMedia) return;   // 其他本地网页不管
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
    console.log(`[本地字幕] content script 已就绪（v0.6.0${standaloneMedia ? '，本地媒体' : ''}）`);
  }

  init();
})();
