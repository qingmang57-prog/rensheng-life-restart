/* 程序外壳自绘标题条（无边框窗口专用）。
   只在 WebView2 宿主（pywebview 外壳）里插入；用浏览器直接打开工作台时不生效。
   与 tools/launcher 的 splash / 失败页 / 重连页共用同一份实现，避免两套外观走样。

   为什么不用 pywebview 的 .pywebview-drag-region：
   它的注入脚本在页面 load 之后才挂上，标题条可能已经渲染完、拖动区还没生效；
   这里统一走 ShellApi.shell_bounds / shell_set_bounds（与缩放同一条通路），行为可预期。 */
(function () {
  var shell = (window.chrome && window.chrome.webview) || window.pywebview;
  if (!shell) return;                       // 浏览器直接打开：不插标题条
  var doc = document.documentElement;
  if (doc.classList.contains('shell-hosted')) return;

  var cfg = window.__SHELL_TITLEBAR__ || {};
  var name = cfg.name || document.title || '';
  /* 产品标跟着皮肤走：深色皮肤用 on-dark，浅色皮肤用 on-light（CSS 里按 html.light 切换）。
     splash / 失败页自带的 data URI 图标只用于暗色外壳，所以两个槽位都用它。 */
  var logoDark = cfg.logo || '/brand/on-dark/logo-64.png';
  var logoLight = cfg.logo || '/brand/on-light/logo-64.png';

  /* ---- 与 Python 侧 ShellApi 的桥：API 在页面 load 后才挂上，调用前等一次 pywebviewready */
  function api() { return window.pywebview && window.pywebview.api; }
  function call(method, args) {
    var a = api();
    if (a && a[method]) {
      try { return Promise.resolve(a[method].apply(a, args || [])); } catch (e) { return Promise.resolve(null); }
    }
    return new Promise(function (resolve) {
      window.addEventListener('pywebviewready', function () {
        var b = api();
        try { resolve(b && b[method] ? b[method].apply(b, args || []) : null); }
        catch (e) { resolve(null); }
      }, { once: true });
    });
  }

  var ICONS = {
    min: '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M0 5.5h10" stroke="currentColor" stroke-width="1"/></svg>',
    max: '<svg viewBox="0 0 10 10" aria-hidden="true"><rect x="0.5" y="0.5" width="9" height="9" fill="none" stroke="currentColor" stroke-width="1"/></svg>',
    restore: '<svg viewBox="0 0 10 10" aria-hidden="true"><rect x="0.5" y="3" width="6.5" height="6.5" fill="none" stroke="currentColor" stroke-width="1"/><path d="M3 3V0.5h6.5V7H8" fill="none" stroke="currentColor" stroke-width="1"/></svg>',
    close: '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M0.5 0.5l9 9M9.5 0.5l-9 9" stroke="currentColor" stroke-width="1"/></svg>'
  };

  var bar = document.createElement('div');
  bar.className = 'shell-titlebar';
  bar.id = 'shell-titlebar';
  bar.innerHTML =
    '<span class="shell-tb-brand"><img class="shell-tb-logo-dark" alt=""><img class="shell-tb-logo-light" alt=""><span class="shell-tb-name"></span></span>' +
    '<span class="shell-tb-actions">' +
      '<button type="button" class="shell-tb-btn" data-shell="min" title="最小化" aria-label="最小化">' + ICONS.min + '</button>' +
      '<button type="button" class="shell-tb-btn" data-shell="max" title="最大化 / 还原" aria-label="最大化或还原">' +
        '<span class="shell-tb-max">' + ICONS.max + '</span>' +
        '<span class="shell-tb-restore">' + ICONS.restore + '</span>' +
      '</button>' +
      '<button type="button" class="shell-tb-btn" data-shell="close" title="关闭（最小化到托盘）" aria-label="关闭">' + ICONS.close + '</button>' +
    '</span>';
  bar.querySelector('.shell-tb-logo-dark').src = logoDark;
  bar.querySelector('.shell-tb-logo-light').src = logoLight;
  bar.querySelector('.shell-tb-name').textContent = name;

  function setMaximized(on) {
    if (on) doc.classList.add('shell-maximized');
    else doc.classList.remove('shell-maximized');
  }

  /* 一轮按住拖动/拉伸：pointer capture 保证指针移出窗口也能收到事件 */
  function beginTrack(el, ev, onMove) {
    var pid = ev.pointerId, raf = 0, pending = null, finished = false;
    function flush() {
      raf = 0;
      if (pending) { call('shell_set_bounds', pending); pending = null; }
    }
    function queue(rect) {
      pending = rect;
      if (!raf) raf = requestAnimationFrame(flush);   // 每帧最多一次，别把桥刷爆
    }
    function move(e) { if (e.pointerId === pid) onMove(e, queue); }
    function end(e) {
      if (e.pointerId !== pid || finished) return;
      finished = true;
      try { el.releasePointerCapture(pid); } catch (err) {}
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', end);
      el.removeEventListener('pointercancel', end);
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      flush();
    }
    try { el.setPointerCapture(pid); } catch (err) {}
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
  }

  /* 把一块区域接成拖动区：整块都能按住拖窗、双击最大化/还原。
     skip 里的元素（按钮、导航项等）保持原本点击行为；拖过之后的那一次 click 会被吞掉，
     避免在侧栏 logo 上拖完窗口顺手跳页。 */
  function attachDrag(el, skip) {
    if (!el) return;
    el.classList.add('shell-drag-zone');
    el.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      if (skip && e.target.closest(skip)) return;
      /* 双击最大化/还原：自己按 pointerdown 的时间与位置判定，不依赖原生 dblclick
         （无边框 + 指针捕获下原生 dblclick 的投递不稳定），两次间隔 <420ms 且基本没挪动即算双击 */
      var now = Date.now();
      var last = el.__shellLastTap;
      if (last && now - last.t < 420 && Math.abs(e.clientX - last.x) < 8 && Math.abs(e.clientY - last.y) < 8) {
        el.__shellLastTap = null;
        call('shell_max').then(function (r) { setMaximized(r && r.maximized); });
        return;
      }
      el.__shellLastTap = { t: now, x: e.clientX, y: e.clientY };
      call('shell_bounds').then(function (b) {
        if (!b || b.maximized) return;
        var sx = e.screenX, sy = e.screenY, moved = false;
        beginTrack(el, e, function (ev, queue) {
          var dx = ev.screenX - sx, dy = ev.screenY - sy;
          if (!moved && Math.abs(dx) + Math.abs(dy) > 3) moved = true;
          if (!moved) return;
          el.__shellDragged = true;
          queue([Math.round(b.x + dx), Math.round(b.y + dy), b.w, b.h]);
        });
      });
    });
    el.addEventListener('click', function (e) {
      if (!el.__shellDragged) return;
      el.__shellDragged = false;
      e.preventDefault();
      e.stopPropagation();
    });
  }

  /* 标题条整条可拖：产品标、标与文字间空白、标题文字、按钮之间的空隙都算，只有三个按钮除外 */
  attachDrag(bar, 'button');
  bar.addEventListener('click', function (e) {
    var btn = e.target.closest('button[data-shell]');
    if (!btn) return;
    var act = btn.getAttribute('data-shell');
    if (act === 'min') call('shell_min');
    else if (act === 'max') call('shell_max').then(function (r) { setMaximized(!!(r && r.maximized)); });
    else if (act === 'close') call('shell_close');
  });

  /* 无边框窗口丢掉系统缩放边：八条透明热区把八方向拉伸补回来 */
  var EDGES = ['t', 'b', 'l', 'r', 'tl', 'tr', 'bl', 'br'];
  var edges = document.createDocumentFragment();
  EDGES.forEach(function (dir) {
    var strip = document.createElement('div');
    strip.className = 'shell-edge shell-edge-' + dir;
    strip.setAttribute('data-shell-edge', dir);
    strip.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      call('shell_bounds').then(function (b) {
        if (!b || b.maximized) return;
        var sx = e.screenX, sy = e.screenY;
        beginTrack(strip, e, function (ev, queue) {
          var dx = ev.screenX - sx, dy = ev.screenY - sy;
          var x = b.x, y = b.y, w = b.w, h = b.h;
          if (dir.indexOf('l') >= 0) { x = b.x + dx; w = b.w - dx; }
          if (dir.indexOf('r') >= 0) { w = b.w + dx; }
          if (dir.indexOf('t') >= 0) { y = b.y + dy; h = b.h - dy; }
          if (dir.indexOf('b') >= 0) { h = b.h + dy; }
          if (w < b.min_w) { if (dir.indexOf('l') >= 0) x = b.x + b.w - b.min_w; w = b.min_w; }
          if (h < b.min_h) { if (dir.indexOf('t') >= 0) y = b.y + b.h - b.min_h; h = b.min_h; }
          queue([Math.round(x), Math.round(y), Math.round(w), Math.round(h)]);
        });
      });
    });
    edges.appendChild(strip);
  });

  doc.classList.add('shell-hosted');
  document.body.insertBefore(bar, document.body.firstChild);
  document.body.appendChild(edges);

  /* 侧栏顶部（logo + 人生重启系统）也纳入拖动区，方便从侧栏直接拖窗；
     侧栏里的导航项 / 按钮不在拖动区范围内，点击行为不变。 */
  attachDrag(document.querySelector('.app-shell .sidebar .brand'), null);

  call('shell_bounds').then(function (b) { if (b) setMaximized(!!b.maximized); });
})();
