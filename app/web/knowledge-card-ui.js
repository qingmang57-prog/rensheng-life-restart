(function () {
  'use strict';
  // 知识卡（需求单 H，2026-11-25）：在唯一输入口里打字停顿后，自动整理出一张卡；还没发送，不打断打字。
  // 两段式（方案 A′）：卡坯零模型（服务端本地算）；精修才调模型，且同一段文字只调一次（服务端缓存）。
  // 卡片就是一条认知：标题 / 主张（=原文）/ 边界（原文里带条件的句子）；「存下来」走现有 /api/knowledge。
  const MIN_CHARS = 50;        // 需求单 H.2.3：超过 50 字才出卡
  const PAUSE_MS = 1500;       // 需求单 H.2.3：停顿 1.5 秒触发
  const CHANGE_CHARS = 10;     // 需求单 H.2.3：改动超过 10 个字才重新生成
  let timer = 0, seq = 0, state = 'idle', card = null, lastText = '', dismissed = '', failedFor = '', autoRefine = true;

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, ch => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }
  function say(text) {
    if (typeof window.toast === 'function') window.toast(text);
    else {
      const note = document.querySelector('#kcard .kcard-note');
      if (note) note.textContent = text;
    }
  }
  let cssDone = false;
  function ensureCss() {
    // 本脚本排在主程序前面加载：注入函数那时还不存在，所以每次要用卡片时再试，成功一次为止。
    if (cssDone || typeof window.ipAddCssRules !== 'function') return;
    window.ipAddCssRules([
      // 对话页的主区域不滚动：卡片限高、内部滚动，放在输入框上方，保证一出来就看得见
      '.kcard{margin:0 0 10px;display:flex;flex-direction:column;max-height:min(34vh,300px);border:1px solid rgba(230,197,106,.3);border-radius:14px;padding:12px 14px;background:rgba(230,197,106,.06)}',
      '.kcard:empty{display:none;margin:0;border:0;padding:0}',
      '.kcard-head,.kcard-actions,.kcard-note,.kcard-path{flex:none}',
      '.kcard-note{margin:8px 0 0;max-height:4.6em;overflow:auto}',
      // 窄屏下卡片头部会折成两行，把按钮挤出卡片：这句说明在窄屏不显示
      '@media(max-width:600px){.kcard-head .kcard-hint{display:none}}',
      '.kcard-body{min-height:0;overflow:auto;padding-right:4px}',
      '.kcard-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13px}',
      '.kcard-chip{display:inline-block;padding:2px 10px;border-radius:12px;background:rgba(230,197,106,.9);color:#221a10;font-weight:600}',
      '.kcard select.kcard-cat{font:inherit;font-size:13px;font-weight:600;padding:3px 8px;border-radius:12px;border:1px solid rgba(230,197,106,.6);background:rgba(230,197,106,.9);color:#221a10}',
      '.kcard-hint{opacity:.7}',
      '.kcard-head .kcard-drop{margin-left:auto}',
      '.kcard-field{display:block;margin-top:8px}',
      '.kcard-field>span{display:block;font-size:12px;opacity:.72;margin-bottom:3px}',
      '.kcard input,.kcard textarea{display:block;width:100%;height:auto;min-height:36px;box-sizing:border-box;font:inherit;line-height:1.5;color:inherit;background:transparent;border:1px solid rgba(230,197,106,.22);border-radius:8px;padding:6px 8px;margin:0}',
      '.kcard-title{font-size:17px;font-weight:600}',
      '.kcard-claim{min-height:60px;max-height:160px;resize:vertical;font-size:15px;line-height:1.6}',
      '.kcard-boundary{font-size:14px}',
      '.kcard-foot{font-size:13px;opacity:.78;margin-top:8px;line-height:1.6}',
      '.kcard-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}',
      '.kcard-note{font-size:13px;margin:8px 0 0;line-height:1.6}',
      '.kcard-path{font-size:12px;opacity:.7;word-break:break-all;margin:4px 0 0;max-height:2.8em;overflow:auto}',
      '@keyframes kcard-pulse{0%,100%{opacity:.55}50%{opacity:1}}',
      '.kcard-skel{display:inline-block;width:60%;height:14px;border-radius:7px;background:rgba(230,197,106,.18);vertical-align:middle;animation:kcard-pulse 1.2s ease-in-out infinite}'
    ]);
    // 上面的注入函数不返回结果：查样式表里有没有自己的规则，查到才算成功（没查到下次再试）。
    cssDone = Array.prototype.some.call(document.styleSheets, sheet => {
      try { return Array.prototype.some.call(sheet.cssRules, rule => (rule.cssText || '').indexOf('.kcard') === 0); }
      catch (e) { return false; }
    });
  }
  function host() {
    ensureCss();
    // 对话页挂载时会把 #intake-form 整个搬到页底的输入区（面板里只留结果区），
    // 所以卡片要认「表单」而不是面板：插在表单里、输入框正上方，两种页面形态下都和输入框同处一个容器。
    const form = document.getElementById('intake-form');
    if (!form) return null;
    let box = document.getElementById('kcard');
    if (!box) {
      box = document.createElement('div');
      box.id = 'kcard';
      box.className = 'kcard';
      form.insertBefore(box, form.querySelector('.intake-box') || null);
    } else if (box.parentElement !== form) {
      form.insertBefore(box, form.querySelector('.intake-box') || null);
    }
    return box;
  }
  function hide() {
    const box = document.getElementById('kcard');
    if (box) box.innerHTML = '';
    card = null;
    state = 'idle';
  }
  function currentText() {
    const ta = document.getElementById('intake-text');
    return ta ? String(ta.value || '').trim() : '';
  }
  function shouldOffer(text) {
    if (Array.from(text).length < MIN_CHARS) return false;
    if (/https?:\/\//i.test(text)) return false;             // 链接走转写，不出卡
    const picker = document.getElementById('intake-file');
    if (picker && picker.files && picker.files.length) return false;  // 选了文件走识别
    return true;
  }
  function values() {
    if (!card) return null;
    const read = (sel, fallback) => {
      const el = document.querySelector('#kcard ' + sel);
      return el ? String(el.value || '').trim() : fallback;
    };
    return {
      title: read('.kcard-title', card.title).slice(0, 160),
      claim: read('.kcard-claim', card.claim),
      boundary: read('.kcard-boundary', card.boundary || ''),
      category: read('.kcard-cat', card.category)
    };
  }
  const CATEGORIES = ['认知底座', '人性关系', '赚钱逻辑', '操盘手法', '成功心法', '随手记'];
  function categoryHtml() {
    const now = CATEGORIES.indexOf(card.category) >= 0 ? card.category : '随手记';
    return '<select class="kcard-cat" aria-label="知识卡分类">' +
      CATEGORIES.map(name => '<option value="' + esc(name) + '"' + (name === now ? ' selected' : '') + '>' + esc(name) + '</option>').join('') +
      '</select>';
  }
  function render(note, freshCategory) {
    const box = host();
    if (!box || !card) return;
    // 重画前先收好用户正在编辑的内容，别把他改的字冲掉（精修刚给出新分类时，界面上的旧分类不读回）
    if (document.querySelector('#kcard .kcard-claim')) {
      const typed = values();
      card.title = typed.title;
      card.claim = typed.claim;
      card.boundary = typed.boundary;
      if (!freshCategory) card.category = typed.category;
    }
    const busy = state === 'drafting' || state === 'refining';
    const titleHtml = state === 'refining'
      ? '<span class="kcard-skel"></span>'
      : '<input class="kcard-title" aria-label="知识卡标题" value="' + esc(card.title) + '">';
    const related = (card.related || []).map(row => esc(row.title)).join('、');
    const refineLine = card.refined
      ? '标题和分类已精修' + (card.cached ? '（同一段文字不重复调用）' : '（用了 1 次模型调用）')
      : '卡坯（未用模型）';
    box.innerHTML =
      '<div class="kcard-head">' + categoryHtml() +
      '<span class="kcard-hint">边打字边整理的知识卡 · 还没发送</span>' +
      '<button type="button" class="ghost small-button kcard-drop" data-kcard="drop">丢弃</button></div>' +
      '<div class="kcard-body">' +
      '<div class="kcard-field"><span>标题（可改）</span>' + titleHtml + '</div>' +
      '<div class="kcard-field"><span>主张（你的原话，可改）</span><textarea class="kcard-claim" aria-label="知识卡主张">' + esc(card.claim) + '</textarea></div>' +
      (card.boundary || state === 'ready'
        ? '<div class="kcard-field"><span>边界（这句话在什么范围内成立；没说就留空）</span><input class="kcard-boundary" aria-label="知识卡边界" value="' + esc(card.boundary || '') + '"></div>'
        : '') +
      '<div class="kcard-foot">' + esc(refineLine) +
      (related ? '<br>关联已有认知 ' + (card.related || []).length + ' 条：' + related : '<br>暂时没有相近的已有认知') +
      '<br>自动精修：<button type="button" class="ghost small-button" data-kcard="toggle">' + (autoRefine ? '开（每次停顿会用 1 次模型调用，同文不重复）' : '关') + '</button>' +
      (card.refined ? '' : ' <button type="button" class="ghost small-button" data-kcard="refine">现在精修</button>') +
      '</div>' +
      '</div>' +   // .kcard-body 到此为止
      // 正文到此为止，下面是常驻的按钮区（正文再长也不把按钮滚出去）
      (note ? '<p class="kcard-note">' + esc(note) + '</p>' : '') +
      (card.imagePath ? '<p class="kcard-path">图片已保存到：' + esc(card.imagePath) + '</p>' : '') +
      // 按钮和提示永远常驻在卡片底部（不跟着正文滚动）：任何一步没成，客户都能看到原因和下一步
      '<div class="kcard-actions">' +
      '<button type="button" class="primary" data-kcard="save"' + (busy ? ' disabled' : '') + '>存进知识库</button>' +
      '<button type="button" class="ghost small-button" data-kcard="image"' + (busy ? ' disabled' : '') + '>存为图片</button>' +
      (card.imagePath ? '<button type="button" class="ghost small-button" data-kcard="reveal">打开所在文件夹</button>' : '') +
      '</div>';
  }
  async function runRefine() {
    if (!card) return;
    const mine = ++seq;
    state = 'refining';
    render(card.refine_note || '');
    try {
      const refined = await window.api('/knowledge-card/refine', 'POST', { text: card.claim });
      if (mine !== seq) return;
      card = Object.assign({}, card, refined);
      state = 'ready';
      render(refined.refine_note || '', true);
    } catch (error) {
      if (mine !== seq) return;
      state = 'ready';
      render('精修没成：' + (error && error.message ? error.message : '原因未知') + ' 卡坯照常可以存下来。');
    }
  }
  async function runDraft(text) {
    const mine = ++seq;
    state = 'drafting';
    card = { title: '', claim: text, boundary: '', category: '随手记', related: [] };
    const skeleton = host();
    if (skeleton) skeleton.innerHTML = '<div class="kcard-head"><span class="kcard-skel"></span><span class="kcard-hint">正在整理成知识卡…</span></div>';
    try {
      const data = await window.api('/knowledge-card/draft', 'POST', { text: text });
      if (mine !== seq) return;
      card = data;
      lastText = text;
      failedFor = '';
      state = 'ready';
      if (autoRefine) {
        await runRefine();
      } else {
        render();
      }
      // 卡片刚出来时滚到看得见的位置（手机端页面可滚动；桌面端输入区不滚动，靠限高保证在屏内）
      const box = document.getElementById('kcard');
      if (box && box.scrollIntoView) box.scrollIntoView({ block: 'nearest' });
    } catch (error) {
      if (mine !== seq) return;
      // P1/P3：出不了卡要说清原因，不能骨架屏一直转；同一段文字不再反复重试。
      failedFor = text;
      hide();
      const box = host();
      if (box) {
        box.innerHTML = '<div class="kcard-head"><span class="kcard-hint">知识卡没出来：' +
          esc(error && error.message ? error.message : '原因未知') + '</span>' +
          '<button type="button" class="ghost small-button kcard-drop" data-kcard="drop">知道了</button></div>';
      }
      state = 'ready';
    }
  }
  function onPause() {
    const text = currentText();
    if (!shouldOffer(text)) { hide(); return; }
    if (dismissed && dismissed === text) return;
    if (text === failedFor) return;
    // 需求单 H.2.3：改动不到 10 个字不重新生成（卡还在的时候；卡被收起后同一段文字要能再出）
    if (card && lastText && Math.abs(Array.from(text).length - Array.from(lastText).length) < CHANGE_CHARS) return;
    runDraft(text);
  }
  document.addEventListener('input', event => {
    const el = event.target;
    if (!el || el.id !== 'intake-text') return;
    clearTimeout(timer);
    const text = String(el.value || '').trim();
    if (!shouldOffer(text)) { hide(); return; }
    timer = setTimeout(onPause, PAUSE_MS);
  });
  document.addEventListener('keydown', event => {
    // 卡片在表单里：在标题/边界里按回车不能误触发「发送」
    if (event.key === 'Enter' && event.target && event.target.closest && event.target.closest('#kcard') &&
        event.target.tagName !== 'TEXTAREA') {
      event.preventDefault();
    }
  });
  document.addEventListener('submit', event => {
    if (event.target && event.target.closest && event.target.closest('#intake-form')) {
      clearTimeout(timer);
      hide();
    }
  });
  document.addEventListener('click', async event => {
    const btn = event.target && event.target.closest ? event.target.closest('[data-kcard]') : null;
    if (!btn) return;
    const action = btn.getAttribute('data-kcard');
    if (action === 'drop') { dismissed = currentText(); hide(); return; }
    if (action === 'toggle') {
      autoRefine = !autoRefine;
      try { await window.api('/knowledge-card/pref', 'POST', { auto_refine: autoRefine }); } catch (error) { /* 设置没存上也要照常能用 */ }
      render();
      return;
    }
    if (!card) return;
    if (action === 'refine') { runRefine(); return; }
    if (action === 'save') {
      const data = values();
      btn.disabled = true;
      try {
        await window.api('/knowledge', 'POST', data);
        dismissed = data.claim;
        hide();
        say('已存进知识库（' + data.category + '），在「认知搜索」里能找到。');
      } catch (error) {
        btn.disabled = false;
        render('没存上：' + (error && error.message ? error.message : '原因未知'));
      }
      return;
    }
    if (action === 'image') {
      const data = values();
      btn.disabled = true;
      try {
        const saved = await window.api('/knowledge-card/image', 'POST', data);
        card.image_id = saved.id;
        card.imagePath = saved.path;
        render('');
      } catch (error) {
        btn.disabled = false;
        render('图片没存上：' + (error && error.message ? error.message : '原因未知'));
      }
      return;
    }
    if (action === 'reveal') {
      try {
        await window.api('/knowledge-card/reveal', 'POST', { id: card.image_id });
      } catch (error) {
        render(error && error.message ? error.message : '没能打开文件夹，可以复制完整路径手动打开。');
      }
    }
  });
  async function start() {
    ensureCss();
    for (let i = 0; i < 100 && typeof window.api !== 'function'; i++) {
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    if (typeof window.api !== 'function') return;
    try {
      const pref = await window.api('/knowledge-card/pref');
      autoRefine = pref && pref.auto_refine !== false;
    } catch (error) { /* 读不到设置就按默认（开） */ }
  }
  start();
})();
