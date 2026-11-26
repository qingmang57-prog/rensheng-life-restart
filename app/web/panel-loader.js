'use strict';
// 2026-11-16（性能 E）：非首屏面板脚本延后加载。
// 这些文件原本在 index.html 里随首屏一起下载、解析，但首屏（工作台）一个都用不到。
// 现在改成首屏可交互之后（load + 空闲）再拉；路由切换前用 ensurePanels() 兜底等待，
// 已经加载过就直接返回，不会重复插入。
window.__PANEL_SRC = [
  ['/knowledge-ui.js', '20261125.1220'],
  ['/interview-ui.js', '20261116-iv3'],
  ['/workflow-ui.js', '20261124.2355'],
  ['/anchor-ui.js', '20261113.4'],
  ['/chain-ui.js', '20261118.0030'],
  ['/textcut-ui.js', '20261117.1820'],
  ['/creation-ui.js', '20261126.1200'],
  ['/packaging-ui.js', '20261118.0010'],
  ['/cover-ui.js', '20261119.cover2'],
  ['/release-ui.js', '20261116-nav1'],
  ['/hint-ui.js', '20261113.4'],
  ['/checkin-ui.js', '20261118.0200']
];

(function () {
  const loaded = new Map();
  function one(src, ver) {
    if (loaded.has(src)) return loaded.get(src);
    const p = new Promise(resolve => {
      const el = document.createElement('script');
      el.src = src + (ver ? '?v=' + ver : '');
      el.async = false;
      el.onload = () => resolve(src);
      // 失败也只记这一次，不重试（同一面板最多预取一次）。
      el.onerror = () => { console.warn('[panel] 加载失败', src); resolve(src); };
      document.head.appendChild(el);
    });
    loaded.set(src, p);
    return p;
  }
  const VER = new Map(window.__PANEL_SRC.map(x => [x[0], x[1]]));
  const all = () => Promise.all(window.__PANEL_SRC.map(x => one(x[0], x[1])));
  window.ensurePanels = all;

  // 2026-11-16（性能 E-2）：每个路由只等它真正用到的面板，别让直达深链陪等全部 13 个。
  const SET = {
    // 2026-11-25：brain-chat.js（独立对话窗）不再加载——「问知识库」对话页已取消，文件保留不挂载。
    brain: ['/knowledge-ui.js', '/interview-ui.js'],
    knowledge: ['/knowledge-ui.js'],
    create: ['/workflow-ui.js', '/creation-ui.js'],
    bench: ['/workflow-ui.js', '/creation-ui.js'],
    matrix: ['/workflow-ui.js'],
    feedback: ['/workflow-ui.js', '/release-ui.js'],
    checkin: ['/checkin-ui.js'],
    start: [], restart: [], settings: [], ask: [], prompter: [],
    // 2026-11-25（方案 A）：对话页只用首屏已带的 intake-ui / chat-ui，不用等别的面板。
    chat: []
  };
  // 作品页按步骤取；chain-ui 是基础（直剪模式会回落 edit，必须有），
  // workflow-ui 提供 jobs() 用到的 jobLabels 等共享常量，也在基础集里。
  const PROJECT_STEP = {
    source: ['/workflow-ui.js', '/chain-ui.js'],
    script: ['/workflow-ui.js', '/chain-ui.js', '/creation-ui.js'],
    pack: ['/workflow-ui.js', '/chain-ui.js', '/anchor-ui.js'],
    shoot: ['/workflow-ui.js', '/chain-ui.js'],
    edit: ['/workflow-ui.js', '/chain-ui.js', '/textcut-ui.js', '/packaging-ui.js', '/cover-ui.js', '/anchor-ui.js'],
    publish: ['/workflow-ui.js', '/chain-ui.js', '/textcut-ui.js']
  };
  const PROJECT_ALL = ['/workflow-ui.js', '/chain-ui.js', '/textcut-ui.js', '/packaging-ui.js', '/cover-ui.js', '/anchor-ui.js', '/creation-ui.js'];
  function panelSrcs(key, step) {
    if (key === 'project') return PROJECT_STEP[step] || PROJECT_ALL;
    return SET[key] || SET.create;
  }
  window.ensurePanelsFor = (key, step) => Promise.all(panelSrcs(key, step).map(s => one(s, VER.get(s))));

  // 作品列表数据预取：全站只发一次，失败也是空数组兜底，交给 api() 走鉴权回落。
  window.prefetchProjects = function () {
    if (!window.__bootProjectsPromise) {
      window.__bootProjectsPromise = fetch('/api/projects', { credentials: 'same-origin' })
        .then(r => (r.ok ? r.json() : null)).catch(() => null);
    }
    return window.__bootProjectsPromise;
  };
  const NEED_PROJECTS = new Set(['create', 'bench', 'matrix', 'feedback']);

  // 2026-11-16（性能 E-2）：冷启动直达深链没有悬停机会，进页面就把该页要的脚本和数据一起发出去，
  // 别等 bootstrap 回来再串行排队。数据只预取一次，失败返回 null，交给 api() 正常回落。
  const apiCache = new Map();
  function prefetchApi(p) {
    if (!apiCache.has(p)) apiCache.set(p, fetch(p, { credentials: 'same-origin' }).then(r => (r.ok ? r.json() : null)).catch(() => null));
  }
  window.__apiPrefetch = { take(p) { const hit = apiCache.get(p); apiCache.delete(p); return hit; } };

  // 侧栏：悬停 150ms 才预取，按下/键盘聚焦立即预取。
  const timers = new WeakMap();
  function clearTimer(a) { const t = timers.get(a); if (t) { clearTimeout(t); timers.delete(a); } }
  function prefetch(a) {
    const href = (a.getAttribute('href') || '').split('?')[0];
    if (href.indexOf('#/') !== 0) return;
    const segs = href.slice(2).split('/');
    const key = segs[0], step = segs[2] || '';
    window.ensurePanelsFor(key, step);
    if (NEED_PROJECTS.has(key)) window.prefetchProjects();
  }
  const fromEvent = e => (e.target && e.target.closest ? e.target.closest('.sidebar a[href^="#/"]') : null);
  document.addEventListener('pointerover', e => {
    const a = fromEvent(e);
    if (a && !timers.has(a)) timers.set(a, setTimeout(() => { timers.delete(a); prefetch(a); }, 150));
  });
  document.addEventListener('pointerout', e => {
    const a = fromEvent(e);
    if (a && !a.contains(e.relatedTarget)) clearTimer(a);
  });
  document.addEventListener('pointerdown', e => { const a = fromEvent(e); if (a) { clearTimer(a); prefetch(a); } });
  document.addEventListener('focusin', e => { const a = fromEvent(e); if (a) { clearTimer(a); prefetch(a); } });

  // 直达深链：DOM 一就绪就把该路由要的脚本和数据发出去（restart/start 的集合是空的，首屏不会多发）。
  function kickRouteNow() {
    const raw = location.hash || '#/chat';   /* 2026-11-25（方案 A）：默认页是对话 */
    const cut = raw.indexOf('?');
    const segs = (cut < 0 ? raw : raw.slice(0, cut)).slice(2).split('/').map(decodeURIComponent);
    const key = segs[0] || '';
    if (key === 'project' && segs[1]) prefetchApi('/api/projects/' + segs[1]);
    else if (key === 'brain') { prefetchApi('/api/knowledge?' + (cut < 0 ? '' : raw.slice(cut + 1))); prefetchApi('/api/brain/news'); }
    window.ensurePanelsFor(key, segs[2] || '');
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', kickRouteNow, {once: true});
  else kickRouteNow();

  // 首屏之后不再把 13 个面板一次性压上去抢带宽（单线程服务，会拖慢当前页自己的数据请求）；
  // 只补 hint-ui 这个全局增强，其余面板等真要用或侧栏预取时再拉。
  const kick = () => { one('/hint-ui.js', VER.get('/hint-ui.js')); };
  const idle = () => (window.requestIdleCallback ? requestIdleCallback(kick, {timeout: 800}) : setTimeout(kick, 120));
  if (document.readyState === 'complete') idle();
  else window.addEventListener('load', idle, {once: true});
})();
