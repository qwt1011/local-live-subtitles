(() => {
  const id = 'local-live-subtitles-overlay';
  let displayMode = 'bilingual';
  let fontSize = 22;
  let dragging = false;
  let offsetX = 0;
  let offsetY = 0;
  let overlay = document.getElementById(id);
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = id;
    overlay.innerHTML = '<div class="original"></div><div class="translation"></div>';
    document.documentElement.appendChild(overlay);
    const style = document.createElement('style');
    style.textContent = `#${id}{position:fixed;z-index:2147483647;left:50%;bottom:8%;transform:translateX(-50%);max-width:85%;padding:8px 16px;text-align:center;color:#fff;font:600 22px/1.45 Arial,sans-serif;text-shadow:0 2px 4px #000;background:rgba(0,0,0,.45);border-radius:4px;cursor:move;user-select:none;pointer-events:auto;display:none}.translation{color:#ffe9a8;font-size:.9em}`;
    document.documentElement.appendChild(style);
  }
  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === 'toggle') overlay.style.display = message.enabled ? 'block' : 'none';
    if (message.type === 'mode') displayMode = message.mode || 'bilingual';
    if (message.type === 'subtitle') {
      overlay.querySelector('.original').textContent = message.original || '';
      overlay.querySelector('.translation').textContent = message.translation || '';
      overlay.querySelector('.original').style.display = displayMode === 'translation' ? 'none' : 'block';
      overlay.querySelector('.translation').style.display = displayMode === 'original' ? 'none' : 'block';
      overlay.style.display = message.original || message.translation ? 'block' : 'none';
    }
  });
  chrome.storage.local.get({enabled: false}, (value) => {
    overlay.style.display = value.enabled ? 'block' : 'none';
  });
  overlay.addEventListener('pointerdown', (event) => { dragging = true; offsetX = event.clientX - overlay.offsetLeft; offsetY = event.clientY - overlay.offsetTop; overlay.setPointerCapture(event.pointerId); });
  overlay.addEventListener('pointermove', (event) => { if (!dragging) return; overlay.style.left = `${event.clientX - offsetX}px`; overlay.style.top = `${event.clientY - offsetY}px`; overlay.style.bottom = 'auto'; overlay.style.transform = 'none'; });
  overlay.addEventListener('pointerup', () => { dragging = false; });
  overlay.addEventListener('wheel', (event) => { event.preventDefault(); fontSize = Math.max(12, Math.min(48, fontSize + (event.deltaY < 0 ? 2 : -2))); overlay.style.fontSize = `${fontSize}px`; }, {passive: false});
})();
