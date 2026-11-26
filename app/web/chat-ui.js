'use strict';
/* 对话首页（2026-11-25，用户定方案 A）：打开软件就是这一个对话口。
   一个口接一切——文字、链接、图片、音视频、整段分享文案，自动识别；过程只给进度，结果直接是逐字稿。

   复用 intake-ui.js（输入、识别、进度、取消、超时、存历史都在它那里，不重复造）。本文件只做三件事：
   ① 布局：输入框固定在底部；进度和结果接在对话流末尾；
   ② 对话流：已完成的转写按时间从旧到新排成一条流（默认收起，点开看逐字稿），左边是记录；
    ③ 第一次来的引导卡：去答 240 问诊断。只在一条都没答过时出现；「先不用」只记在本机，
       不借用建档的「跳过」标记（那会连带把建档也标成跳过）。
    不给用户看：识别出哪个平台、抓取走了哪几步、内部分类。 */
(function () {
  const DISMISS_KEY = 'ip-chat-diag-dismissed';
  const DETAILS = new Map();   // 展开过的逐字稿：id -> 正文（切页回来不用重取）
  const OPENED = new Set();    // 当前展开着的
  const SEEN = new Set();      // 已经见过的转写 id（用来发现「刚完成的那一条」）
  let observer = null, timer = 0, lastSig = '', prevSavedId = '', viewToken = 0, bound = false, lastPhase = '';

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  }
  function whenText(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (isNaN(d.getTime())) return '';
    const p = n => String(n).padStart(2, '0');
    return (d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function kindLabel(item) {
    const kind = (item && (item.source_kind || item.kind)) || '';
    return ({text: '文字', link: '链接', article: '文章', image: '图片', video: '音视频'})[kind]
      || ((item && item.source) === 'image_ocr' ? '图片' : (item && item.source) === 'text' ? '文字' : '内容');
  }
  function titleOf(item) {
    return item.title || item.file_name || '未命名';
  }
  function cur() {
    try { return (window.ipIntake && window.ipIntake.state()) || {}; } catch (e) { return {}; }
  }
  async function fetchHistory() {
    try {
      const d = await window.api('/intake/history?limit=50&offset=0');
      return Array.isArray(d && d.items) ? d.items : [];
    } catch (e) { return []; }
  }
  function dismissed() {
    try { return localStorage.getItem(DISMISS_KEY) === '1'; } catch (e) { return false; }
  }
  async function diagIdle() {
    try {
      const d = await window.api('/diagnosis/state');
      return !!d && d.status === 'idle';
    } catch (e) { return false; }
  }

  function ensureCss() {
    if (ensureCss.done || typeof window.ipAddCssRules !== 'function') return;
    ensureCss.done = true;
    window.ipAddCssRules([
      '.chat-open main{display:flex;overflow:hidden}',
      '.chat-open #qc-fab,.chat-open #intake-history{display:none}',
      // 2026-11-25（界面改版）：记录栏不再占左边一整栏，改放最下方、默认收起；没有记录时不占位
      '.chat-shell{flex:1;min-width:0;min-height:0;display:grid;grid-template-columns:minmax(0,1fr)}',
      '.chat-rail{flex:none;margin-top:8px}',
      '.chat-rail-list{max-height:30vh;overflow:auto;border:1px solid var(--border);border-radius:12px;padding:6px;margin:0 0 6px}',
      '.chat-rail-toggle{display:flex;align-items:center;gap:8px;padding:7px 12px;border:1px solid var(--border);border-radius:999px;background:transparent;color:var(--muted);font:inherit;font-size:13px;cursor:pointer}',
      '.chat-rail-toggle:hover{color:var(--gold);border-color:var(--gold)}',
      '.chat-rail-caret{display:inline-block;transition:transform .15s ease}',
      '.chat-rail-toggle[aria-expanded="true"] .chat-rail-caret{transform:rotate(90deg)}',
      '.chat-rail-head{display:flex;gap:8px;align-items:baseline;font-weight:600;margin-bottom:8px}',
      '.chat-rail-item{display:flex;flex-direction:column;gap:2px;width:100%;text-align:left;background:none;border:0;color:inherit;cursor:pointer;padding:8px;border-radius:10px}',
      '.chat-rail-item:hover{background:var(--surface2)}',
      '.chat-rail-item strong{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
      '.chat-main{display:flex;flex-direction:column;min-height:0;min-width:0}',
      '.chat-stream{flex:1;min-height:0;overflow:auto;padding-right:6px}',
      '.chat-composer{flex:none;margin-top:10px}',
      '.chat-open .ip-intake{margin:0}',
      '.chat-open .intake-actions .small.muted{display:none}',
      '.chat-open #intake-run{font-size:0;min-width:96px}',
      '.chat-open #intake-run::after{content:"发送";font-size:15px}',
      '.chat-open #intake-result [data-intake-action="reset"],.chat-open #intake-result a[href="#/"]{display:none}',
      '.chat-open .intake-source{display:none}',
      '.chat-turn{border:1px solid var(--border);border-radius:14px;padding:12px 14px;margin:0 0 12px;background:var(--surface)}',
      '.chat-me{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}',
      '.chat-kind{font-size:11px;color:var(--gold);border:1px solid var(--gold);border-radius:999px;padding:1px 8px;white-space:nowrap}',
      '.chat-me-text{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
      '.chat-when{font-size:11px;color:var(--muted);white-space:nowrap}',
      '.chat-turn-foot{display:flex;gap:10px;margin-top:8px}',
      '.chat-out{margin-top:10px}',
      '.chat-onboard{border:1px dashed var(--gold);border-radius:14px;padding:14px;margin:0 0 14px}',
      '.chat-empty{color:var(--muted);text-align:center;padding:28px 10px;font-size:15px;line-height:1.9}',
      // 没有对话记录时让空状态垂直居中，读作有意的空屏而非顶部浮一句、下面一片空洞。
      // 弹性占位 flex:1 0 0：内容矮时上下撑满居中；内容超高时占位缩到 0，退回顶部滚动不裁切。
      '.chat-stream-empty{display:flex;flex-direction:column}',
      '.chat-stream-empty::before,.chat-stream-empty::after{content:"";flex:1 0 0}',
      // 侧栏「更多功能」里的「重启系统」：不能用 .sidebar-tools（手机尺寸下那组会被整组隐藏，展开就是空菜单）。
      '.sidebar-more .sidebar-extra{display:grid;gap:2px;padding:2px 0 8px 20px}',
      '.sidebar-more .sidebar-extra a{display:flex;align-items:center;gap:8px;min-height:44px;padding:0 10px;border-radius:10px;color:var(--text);text-decoration:none;font-size:14px}',
      '.sidebar-more .sidebar-extra a.active{background:var(--tint);color:var(--gold)}',
      // 窄屏（对话流左栏隐藏的宽度）：主区域没有固定高度，不能沿用桌面的「内部滚动」列布局，
      // 否则对话流会被输入框压扁。改成页面自然滚动：结果在上，输入口跟在后面。
      '@media(max-width:900px){.chat-shell{grid-template-columns:minmax(0,1fr)}' +
        '.chat-open main{display:block;overflow:visible}.chat-stream{overflow:visible;padding-right:0}.chat-composer{margin-top:12px}}',
      // 手机宽度下，既有样式把「创作中心」的子页在侧栏里整组隐藏了（以前靠旧首页的区块进入）。
      // 首页换成对话页后那些区块不在主视线里，所以这里把创作子页放回侧栏的「创作中心」组里，别让手机用户进不去。
      // home.css 在手机宽度下用 `.sidebar nav.sidebar-tools` 把子页整组隐藏，且排在后面：这里多带一层 .sidebar-main 才能盖回去
      '@media(max-width:760px){.sidebar .sidebar-main nav.sidebar-tools{display:grid;grid-template-columns:minmax(0,1fr);margin:0;padding:2px 0 8px 20px;border-left:0}' +
      '.sidebar .sidebar-main .sidebar-tools a{display:flex;align-items:center;gap:8px;min-height:44px;padding:0 10px;border-radius:10px;color:var(--text);text-decoration:none;font-size:14px}' +
      '.sidebar .sidebar-main .sidebar-tools a.active{background:var(--tint);color:var(--gold)}}'
    ]);
  }

  // 记录栏（最下方、默认收起）：开关记在本机。没有记录时整个不渲染，不占地方。
  const RAIL_KEY = 'ip-chat-rail-open';
  function railBoxHtml(items) {
    const open = localStorage.getItem(RAIL_KEY) === '1';
    return '<div class="chat-rail" id="chat-rail" aria-label="记录">' +
      '<div id="chat-rail-list" class="chat-rail-list"' + (open ? '' : ' hidden') + '>' + railHtml(items) + '</div>' +
      '<button type="button" class="chat-rail-toggle" id="chat-rail-toggle" aria-expanded="' + (open ? 'true' : 'false') + '" aria-controls="chat-rail-list">记录 <span class="small muted">' + items.length + '</span><span class="chat-rail-caret" aria-hidden="true">▸</span></button></div>';
  }
  function railHtml(items) {
    if (!items.length) return '<p class="small muted">还没有记录。<br>把想转的东西放进下面的输入口。</p>';
    // 接口按「新的在前」返回（ORDER BY updated_at DESC），直接用，不要再翻一次。
    return items.map(item => '<button type="button" class="chat-rail-item" data-chat-open="' + esc(item.id) + '">' +
      '<strong>' + esc(titleOf(item)) + '</strong>' +
      '<span class="small muted">' + esc(whenText(item.created_at)) + ' · ' + esc(kindLabel(item)) + '</span></button>').join('');
  }
  function turnHtml(item) {
    const open = OPENED.has(item.id);
    const body = DETAILS.get(item.id);
    return '<article class="chat-turn" id="turn-' + esc(item.id) + '">' +
      '<div class="chat-me"><span class="chat-kind">' + esc(kindLabel(item)) + '</span>' +
      '<span class="chat-me-text">' + esc(titleOf(item)) + '</span>' +
      '<span class="chat-when">' + esc(whenText(item.created_at)) + '</span></div>' +
      (open ? '<div class="chat-out">' + (body == null ? '<p class="small muted">正在读取…</p>' :
        '<div class="intake-transcript">' + esc(body) + '</div>') + '</div>' : '') +
      '<div class="chat-turn-foot">' +
      '<button type="button" class="ghost small-button" data-chat-toggle="' + esc(item.id) + '">' + (open ? '收起' : '看逐字稿') + '</button>' +
      (open && body ? '<button type="button" class="ghost small-button" data-chat-copy="' + esc(item.id) + '">复制</button>' : '') +
      '<button type="button" class="ghost small-button" data-chat-del="' + esc(item.id) + '">删除</button>' +
      '</div></article>';
  }
  function onboardHtml() {
    return '<div class="chat-onboard" id="chat-onboard">' +
      '<strong>第一次来？先答几题，答完出你的画像和行动方案</strong>' +
      '<p class="small muted">240 问分四批问，每批最多 8 题，可以跳过、随时能停。不答也能正常使用。</p>' +
      '<div class="row"><a class="button-link primary" href="#/restart?diag=1">开始</a>' +
      '<button type="button" class="ghost small-button" data-chat-onboard="dismiss">先不用</button></div></div>';
  }

  /* 路由调用：返回整页 HTML。数据（输入、进度、结果）由 mount() 挂 intake-ui。 */
  async function render() {
    ensureCss();
    const token = ++viewToken;
    // 引导卡只在「一条诊断都没答过」且没点过「先不用」时出现。状态读不出来就不显示（诊断在重启系统页仍可进）。
    const show = dismissed() ? Promise.resolve(false) : diagIdle();
    const items = await fetchHistory();
    const needOnboard = await show;
    if (token !== viewToken) return '';
    const saved = cur().savedId || '';
    const past = items.filter(x => x.id !== saved).reverse();     // 接口是新的在前，这里翻成旧的在上
    items.forEach(x => SEEN.add(x.id));
    if (past.length && !saved) OPENED.add(past[past.length - 1].id);
    prevSavedId = saved;
    lastSig = sig();
    return '<section class="chat-shell" id="chat-shell">' +
      // 记录栏不放在这里（会跑到整个对话区上面）：放在下面、输入框的正上方
      '<div class="chat-main">' +
      '<div class="chat-stream' + (past.length ? '' : ' chat-stream-empty') + '" id="chat-stream">' +
      (needOnboard ? onboardHtml() : '') +
      '<div id="chat-turns">' + (past.length ? past.map(turnHtml).join('') :
        '<p class="chat-empty">把想转的东西放到下面——文字、链接、图片、录音、视频都行。<br>点一次，只给你逐字稿。</p>') + '</div>' +
      '<div id="chat-live"></div>' +
      '</div>' +
      (items.length ? railBoxHtml(items) : '') +
      '<div class="chat-composer" id="chat-composer"></div>' +
      '</div></section>';
  }

  /* 路由把 HTML 放进 #main 之后调用：挂 intake-ui，把它的输入框挪到底部。 */
  function mount() {
    unmount();
    const live = document.getElementById('chat-live');
    const comp = document.getElementById('chat-composer');
    if (!live || !comp || typeof window.intakePanel !== 'function') return;
    live.innerHTML = window.intakePanel();
    const form = live.querySelector('#intake-form');
    if (form) comp.appendChild(form);
    const ta = document.getElementById('intake-text');
    if (ta) ta.placeholder = '把想转的放这里：文字、链接、截图、录音、视频，或整段分享文案';
    prevSavedId = cur().savedId || '';
    lastSig = sig();
    lastPhase = '';
    try {
      observer = new MutationObserver(schedule);
      observer.observe(live, {childList: true, subtree: true, characterData: true});
    } catch (e) { observer = null; }
    bind();
    requestAnimationFrame(() => scrollBottom());
  }
  function unmount() {
    if (observer) { try { observer.disconnect(); } catch (e) {} observer = null; }
    clearTimeout(timer); timer = 0;
    viewToken++;
  }

  /* 签名：当前这条的编号 + intake 自己那份（隐藏的）历史的条数。变了才去重取，进度刷新不触发。 */
  function sig() {
    const live = document.getElementById('chat-live');
    const c = cur();
    const n = live ? live.querySelectorAll('.intake-history-item').length : 0;
    return (c.savedId || '') + '#' + (c.phase || '') + '#' + n;
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(() => {
      timer = 0;
      const c = cur();
      if (c.phase === 'running' || c.phase === 'saving' || c.phase === 'done') scrollBottom();
      const s = sig();
      if (s !== lastSig) { lastSig = s; refresh(); }
    }, 120);
  }
  async function refresh() {
    const token = viewToken;
    const c = cur();
    const items = await fetchHistory();
    if (token !== viewToken) return;
    items.forEach(x => { if (!SEEN.has(x.id)) { SEEN.add(x.id); OPENED.add(x.id); } });
    // 刚从「进行中」变成「已完成」的那条：从底部结果区挪进对话流时保持展开。
    if (prevSavedId && prevSavedId !== (c.savedId || '') && items.some(x => x.id === prevSavedId)) OPENED.add(prevSavedId);
    prevSavedId = c.savedId || '';
    const past = items.filter(x => x.id !== (c.savedId || '')).reverse();
    const turns = document.getElementById('chat-turns');
    let rail = document.getElementById('chat-rail-list');
    // 从「没有记录」变成「有记录」：把记录栏补出来；反过来则整块撤掉，不留空白
    if (!rail && items.length) {
      const comp = document.getElementById('chat-composer');
      if (comp) comp.insertAdjacentHTML('beforebegin', railBoxHtml(items));
      rail = document.getElementById('chat-rail-list');
    }
    const railBox = document.getElementById('chat-rail');
    if (railBox && !items.length) { railBox.remove(); rail = null; }
    if (turns) turns.innerHTML = past.length ? past.map(turnHtml).join('') :
      '<p class="chat-empty">把想转的东西放到下面——文字、链接、截图、录音、视频都行。<br>点一次，只给你逐字稿。</p>';
    const stream = document.getElementById('chat-stream');
    if (stream) stream.classList.toggle('chat-stream-empty', !past.length);
    if (rail) {
      rail.innerHTML = railHtml(items);
      const count = railBox.querySelector('.chat-rail-toggle .small');
      if (count) count.textContent = String(items.length);
    }
    for (const id of OPENED) {
      if (!DETAILS.has(id) && past.some(x => x.id === id)) loadDetail(id);
    }
  }
  async function loadDetail(id) {
    try {
      const full = await window.api('/intake/history/' + encodeURIComponent(id));
      const rec = (full && full.item) || full || {};
      DETAILS.set(id, String(rec.content || rec.transcript || ''));
    } catch (e) { DETAILS.set(id, ''); }
    if (!OPENED.has(id)) return;
    const out = document.querySelector('#turn-' + cssEscape(id) + ' .chat-out');
    if (out && DETAILS.get(id)) out.innerHTML = '<div class="intake-transcript">' + esc(DETAILS.get(id)) + '</div>';
    const foot = document.querySelector('#turn-' + cssEscape(id) + ' .chat-turn-foot');
    if (foot && DETAILS.get(id) && !foot.querySelector('[data-chat-copy]')) {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'ghost small-button'; b.setAttribute('data-chat-copy', id); b.textContent = '复制';
      foot.appendChild(b);
    }
  }
  function cssEscape(v) { return (window.CSS && CSS.escape) ? CSS.escape(String(v)) : String(v).replace(/[^A-Za-z0-9_-]/g, ''); }
  function scrollBottom() {
    const s = document.getElementById('chat-stream');
    if (!s) return;
    // 桌面：对话流是固定高度、自己滚动，始终停在最新。
    if (s.scrollHeight > s.clientHeight + 2) { s.scrollTop = s.scrollHeight; return; }
    // 窄屏：页面滚动。只在阶段变化时把进度/结果带到眼前，不跟着每次轮询抢滚动位置。
    const phase = cur().phase || '';
    if (phase === lastPhase) return;
    lastPhase = phase;
    const el = document.getElementById(phase === 'running' ? 'intake-status' : 'intake-result');
    if (el && el.firstElementChild) el.scrollIntoView({block: 'start', behavior: 'smooth'});
  }
  function openTurn(id) {
    OPENED.add(id);
    refresh().then(() => {
      const el = document.getElementById('turn-' + cssEscape(id));
      if (el) el.scrollIntoView({behavior: 'smooth', block: 'center'});
    });
  }

  function bind() {
    if (bound) return;
    bound = true;
    document.addEventListener('click', async event => {
      const t = event.target && event.target.closest ? event.target.closest('[data-chat-toggle],[data-chat-open],[data-chat-copy],[data-chat-del],[data-chat-onboard],#chat-rail-toggle') : null;
      if (!t) return;
      if (t.id === 'chat-rail-toggle') {
        const list = document.getElementById('chat-rail-list');
        if (!list) return;
        const open = list.hidden;
        list.hidden = !open;
        t.setAttribute('aria-expanded', open ? 'true' : 'false');
        try { localStorage.setItem(RAIL_KEY, open ? '1' : '0'); } catch (e) {}
        return;
      }
      const on = t.getAttribute('data-chat-onboard');
      if (on) {
        // 只记在本机：答过一题之后诊断状态就不再是 idle，卡自然不再出现。
        try { localStorage.setItem(DISMISS_KEY, '1'); } catch (e) {}
        const card = document.getElementById('chat-onboard');
        if (card) card.remove();
        return;
      }
      const del = t.getAttribute('data-chat-del');
      if (del) {
        const host = document.getElementById('turn-' + cssEscape(del)) || document.body;
        const ok = await confirmBar(host, '删除这条记录？删除后逐字稿不再出现在对话和记录里。', {okText: '删除', cancelText: '不删', danger: true});
        if (!ok) return;
        try {
          await window.api('/intake/history/' + encodeURIComponent(del), 'DELETE');
          DETAILS.delete(del); OPENED.delete(del); SEEN.delete(del);
          if ((cur().savedId || '') === del && window.ipIntake) window.ipIntake.reset();
          toast('已删除。');
          refresh();
        } catch (e) { toast((e && e.message) || '删除失败，请重试。'); }
        return;
      }
      const cp = t.getAttribute('data-chat-copy');
      if (cp) {
        const text = DETAILS.get(cp) || '';
        try { await navigator.clipboard.writeText(text); toast('已复制。'); }
        catch (e) { toast('复制没成功，请手动选择文字复制。'); }
        return;
      }
      const tg = t.getAttribute('data-chat-toggle');
      if (tg) {
        if (OPENED.has(tg)) { OPENED.delete(tg); refresh(); }
        else openTurn(tg);
        return;
      }
      const op = t.getAttribute('data-chat-open');
      if (op) openTurn(op);
    });
  }

  window.ipChat = {render, mount, unmount};
})();
