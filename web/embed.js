/*
 * Ilubot embed widget — add the tutor to any website with one tag:
 *
 *   <script src="https://YOUR-ILUBOT-HOST/embed.js" defer
 *           data-position="right" data-color="#2563eb" data-label="Ask Ilubot"></script>
 *
 * Optional attributes: data-position ("right" | "left"), data-color, data-label,
 * data-open="true" (start open). JS control: window.Ilubot.open() / .close() / .toggle().
 * The site must be listed in the server's ALLOWED_ORIGINS (or that must be "*").
 */
(function () {
  if (window.Ilubot) return;
  var script = document.currentScript;
  if (!script) return;
  var base = new URL(script.src).origin;
  var cfg = script.dataset || {};
  var side = cfg.position === 'left' ? 'left' : 'right';
  var color = cfg.color || '#2563eb';
  var label = cfg.label || 'Ask Ilubot';

  var css = document.createElement('style');
  css.textContent =
    '.ilubot-launcher{position:fixed;bottom:20px;' + side + ':20px;z-index:2147483000;display:flex;align-items:center;gap:8px;' +
    'padding:12px 18px;border:0;border-radius:999px;background:' + color + ';color:#fff;font:600 15px/1 system-ui,sans-serif;' +
    'box-shadow:0 6px 24px rgba(0,0,0,.25);cursor:pointer}' +
    '.ilubot-launcher:focus-visible{outline:3px solid #fff;outline-offset:2px}' +
    '.ilubot-panel{position:fixed;bottom:84px;' + side + ':20px;z-index:2147483000;width:420px;height:680px;' +
    'max-height:calc(100vh - 104px);border-radius:16px;overflow:hidden;background:#0b1220;' +
    'box-shadow:0 12px 48px rgba(0,0,0,.35);display:none}' +
    '.ilubot-panel.open{display:block}' +
    '.ilubot-panel iframe{width:100%;height:100%;border:0}' +
    '.ilubot-close{position:absolute;top:8px;right:8px;z-index:1;width:32px;height:32px;border:0;border-radius:50%;' +
    'background:rgba(0,0,0,.5);color:#fff;font:20px/32px system-ui;cursor:pointer}' +
    '@media (max-width:520px){.ilubot-panel{inset:0;width:auto;height:auto;max-height:none;border-radius:0}}';
  document.head.appendChild(css);

  var panel = document.createElement('div');
  panel.className = 'ilubot-panel';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'Ilubot study tutor');

  var close = document.createElement('button');
  close.className = 'ilubot-close';
  close.type = 'button';
  close.setAttribute('aria-label', 'Close Ilubot');
  close.textContent = '×';
  panel.appendChild(close);

  var btn = document.createElement('button');
  btn.className = 'ilubot-launcher';
  btn.type = 'button';
  btn.setAttribute('aria-expanded', 'false');
  btn.textContent = label;

  var frame = null;
  function open() {
    if (!frame) {
      // Created on first open so the 3D scene and mic prompt don't load until needed.
      frame = document.createElement('iframe');
      frame.src = base + '/?embed=1';
      frame.title = 'Ilubot study tutor';
      frame.allow = 'microphone; camera; autoplay; display-capture; clipboard-write';
      panel.appendChild(frame);
    }
    panel.classList.add('open');
    btn.setAttribute('aria-expanded', 'true');
  }
  function hide() {
    panel.classList.remove('open');
    btn.setAttribute('aria-expanded', 'false');
  }
  function toggle() { panel.classList.contains('open') ? hide() : open(); }

  btn.addEventListener('click', toggle);
  close.addEventListener('click', hide);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hide(); });

  function mount() {
    document.body.appendChild(panel);
    document.body.appendChild(btn);
    if (cfg.open === 'true') open();
  }
  document.body ? mount() : document.addEventListener('DOMContentLoaded', mount);

  window.Ilubot = { open: open, close: hide, toggle: toggle };
})();
