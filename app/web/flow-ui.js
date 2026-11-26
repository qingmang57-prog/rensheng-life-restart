'use strict';
/* 五步主流程（20261104）：序言 → 12 题语音建档 → 三观卡 → 赛道 → 链接洗稿 → 两段式定稿。
   前端只负责交互；题库、锚点与三观归纳都在服务端（flow_service.py）。 */
const fq = s => document.querySelector(s);
const flowUi = {wash: {stage: 'input', plan: null, busy: false, error: '', progress: ''}, note: '',
  finalizing: false, finalizeError: '', elapsed: 0, timer: null, hintDone: false};
let flowRec = null;
let flowSaveTimer = null;
const FLOW_SUBMIT_IDS = ['flow-intro-form', 'flow-q-form', 'flow-card-form', 'flow-card-edit-form', 'flow-adv-form', 'flow-track-form', 'wash-link-form'];
const FLOW_CLICK_ACTIONS = ['flow-defer', 'flow-prev', 'flow-skip-q', 'flow-reopen', 'flow-refinalize', 'flow-login',
  'flow-retry-finalize', 'flow-asset-retry', 'talk-long', 'wash-choose', 'wash-open-plan', 'wash-retry-stage1',
  'wash-retry-stage2', 'flow-upgrade'];

/* ---------- t196（2026-11-24）：分享文字 → 链接；短链按落地地址分类 ----------
   抖音/视频号「分享」出来的是整段话（「7.94 复制打开抖音… https://v.douyin.com/xxx/ …」），
   不是裸 URL。之前输入框是 type=url，浏览器先弹「请输入 URL。」把人挡在门外——这是客户截图的来源。
   这里统一做三件事：从任意文本里取链接、按落地类型判断该进哪个框、把失败原因说成用户能动手的下一步。 */
const SHARE_URL_RE = /https?:\/\/[^\s<>"'“”‘’()（）【】\[\]]+/i;
const SHARE_TRAIL_RE = /[。，,；;！!、：:）)】」』”’"'\]>]+$/;

function shareUrlOf(text) {
  const raw = String(text == null ? '' : text);
  const match = raw.match(SHARE_URL_RE);
  const value = String(raw).trim();
  if (!match) return {url: '', hadText: value.length > 0, bare: false};
  const url = match[0].replace(SHARE_TRAIL_RE, '');
  return {url, hadText: value !== url, bare: value === url};
}

/* 哪些框只收哪种链接：作品/图文/文章/网页 → 洗稿框；账号主页 → 同类账号库；作品 → 登记作品。 */
const SHARE_INPUT_RULES = [
  /* 2026-11-25：原「记一条」浮层里的粘链接框（#qc-link-url）已并入单对话口，这条规则随之摘除。 */
  {match: el => !!el.closest('#wash-link-form'), allow: ['video', 'note', 'article', 'webpage', 'unknown'],
    label: '作品/文章链接', wrong: '这是抖音账号主页，不是单条作品；账号主页请到「发布与复盘 → 同类账号库」保存并采集作品榜。'},
  {match: el => !!el.closest('#bench-account-form'), allow: ['account'],
    label: '抖音账号主页链接', wrong: '这是某条作品的链接，不是账号主页；单条作品请贴到「开始做内容 → 有现成链接」里洗稿，或到「发布与复盘」登记作品。'},
  {match: el => el.name === 'feedback-register-url', allow: ['video', 'note'],
    label: '抖音作品链接', wrong: '这是账号主页，不是某条作品；登记本人作品要贴单条作品链接（/video/… 或分享短链）。'},
];
const shareKindCache = new Map();

function shareRuleOf(el) {
  if (!el || el.tagName !== 'INPUT') return null;
  for (const rule of SHARE_INPUT_RULES) { if (rule.match(el)) return rule; }
  return null;
}

async function shareClassify(url) {
  const key = String(url || '');
  if (!key) return {ok: false, kind: '', reason: ''};
  if (shareKindCache.has(key)) return shareKindCache.get(key);
  let info = {ok: true, kind: 'unknown', reason: ''};
  try {
    info = await api('/wash/classify', 'POST', {url: key});
  } catch (e) {
    // 分类接口不可用时不拦人：让真实抓取去给准确原因。
    info = {ok: true, kind: 'unknown', reason: '', unavailable: String((e && e.message) || '')};
  }
  shareKindCache.set(key, info);
  return info;
}

function shareHint(el, message) {
  const form = el.closest('form') || el.parentElement;
  if (!form) return;
  let box = form.querySelector('[data-share-hint]');
  if (!message) { if (box) box.remove(); return; }
  if (!box) {
    box = document.createElement('p');
    box.className = 'small error-text';
    box.setAttribute('data-share-hint', '1');
    box.setAttribute('role', 'alert');
    el.insertAdjacentElement('afterend', box);
  }
  box.textContent = message;
}

/* 粘贴时就把整段分享文字收成一条链接：不让人先手工删掉前后那半句。 */
document.addEventListener('paste', event => {
  const el = event.target;
  const rule = shareRuleOf(el);
  if (!rule) return;
  const clip = String((event.clipboardData && event.clipboardData.getData('text')) || '');
  const picked = shareUrlOf(clip);
  if (!picked.url) return;
  event.preventDefault();
  el.value = picked.url;
  shareHint(el, '');
  el.dispatchEvent(new Event('input', {bubbles: true}));
  shareClassify(picked.url).then(info => {
    if (!info || !info.kind) return;
    if (info.ok === false && info.reason) { shareHint(el, info.reason); return; }
    if (rule.allow.includes(info.kind)) return;
    shareHint(el, rule.wrong || ('这个框只收' + rule.label + '。'));
  }).catch(() => {});
}, true);

/* 不联网也能判的形态（同步拦「贴错框」）；短链要看落地地址，交给 shareClassify。 */
function shareShapeKind(url) {
  const text = String(url || '');
  if (!/^https?:\/\//i.test(text)) return 'unknown';
  if (/(?:^|\/\/)(?:v\.)?(?:ies)?douyin\.com\/[A-Za-z0-9_-]+\/?$/i.test(text.split('?')[0])) return 'short';
  if (/\b(?:modal_id|aweme_id|vid)=\d{8,30}/i.test(text)) return 'video';
  if (/(?:^|\/\/)(?:www\.|v\.)?(?:ies)?douyin\.com\/(?:share\/)?user\//i.test(text)) return 'account';
  if (/(?:^|\/\/)(?:www\.|v\.)?(?:ies)?douyin\.com\/(?:share\/)?(?:video|note|slides)\//i.test(text)) return 'video';
  if (/(?:^|\/\/)(?:www\.)?(?:ies)?douyin\.com\/live\//i.test(text) || /webcast/i.test(text)) return 'live';
  if (/mp\.weixin\.qq\.com/i.test(text)) return 'article';
  return 'webpage';
}

/* 提交前再规范化一次（键盘输入、拖拽、脚本填值都要覆盖），并把「框不对」当场说清。 */
document.addEventListener('submit', event => {
  const form = event.target;
  if (!form || !form.querySelectorAll) return;
  for (const el of form.querySelectorAll('input')) {
    const rule = shareRuleOf(el);
    if (!rule) continue;
    const picked = shareUrlOf(el.value);
    if (!picked.url) continue;
    if (picked.url !== el.value) el.value = picked.url;
    shareHint(el, '');
    const shape = shareShapeKind(picked.url);
    const known = shareKindCache.get(picked.url);
    const kind = (known && known.kind) || (shape === 'short' ? '' : shape);
    if (!kind || rule.allow.includes(kind)) continue;
    // 只挡「确定贴错了框」的：作品贴进账号库、账号主页贴进作品框。
    event.preventDefault();
    event.stopPropagation();
    shareHint(el, known && known.ok === false && known.reason ? known.reason : (rule.wrong || ('这个框只收' + rule.label + '。')));
    if (el.focus) el.focus();
    return;
  }
}, true);

/* 输入停下来就预先分类一次：等用户点提交时，短链的落地类型通常已经知道了。 */
let shareKindTimer = null;
document.addEventListener('input', event => {
  const el = event.target;
  const rule = shareRuleOf(el);
  if (!rule) return;
  clearTimeout(shareKindTimer);
  const picked = shareUrlOf(el.value);
  if (!picked.url) { shareHint(el, ''); return; }
  shareKindTimer = setTimeout(() => {
    shareClassify(picked.url).then(info => {
      if (!info) return;
      if (info.ok === false && info.reason) { shareHint(el, info.reason); return; }
      if (!info.kind || rule.allow.includes(info.kind)) { shareHint(el, ''); return; }
      shareHint(el, rule.wrong || ('这个框只收' + rule.label + '。'));
    }).catch(() => {});
  }, 700);
}, true);



function flowData() {
  return (state.boot && state.boot.flow) || {intro_seen: false, stage: 'intro', current: 1,
    completed: false, answers: {}, questions: [], tracks: {}, card: null};
}
function flowQuestion(data, index) {
  const list = data.questions || [];
  return list[Math.max(0, Math.min(list.length - 1, index - 1))] || {id: 'q1', section: '世界观', text: ''};
}
/* ---------- 通向作品出口（t28 · P3；2026-11-17 改判）----------
   深度题照问，不删不缩；题目下发时若带 exit 字段，就在题下多显示一行「通向作品」的桥接问。
   出口文案和原题是一体的，属于云端下发的核心资产（见 flow_service 的题库封壳说明）：
   本地**不留**任何题目/出口的兜底副本——把兜底表写回前端等于把核心资产重新塞进产品包。
   服务端没下发 exit，就安静地不显示这一行，题干、hint、答题一切照旧。 */
function flowExitHint(q) {
  return String((q && q.exit) || '').trim();
}
/* ---------- 序言：首启欢迎（一屏一件事：叫名字 → 说清接下来做什么 → 一个开始按钮） ---------- */
function flowName() {
  try { return String(sessionStorage.getItem('ip.user-name') || '').trim(); } catch (e) { return ''; }
}
function flowWelcomeTitle() {
  const name = flowName();
  return name ? (esc(name) + '，欢迎。') : '欢迎。';
}
function flowIntro(data) {
  const total = (data.questions || []).length || 12;
  return '<section class="flow-shell">' +
    '<div class="flow-card flow-intro">' +
    '<p class="flow-kicker">先花五分钟，认识你</p>' +
    '<h1 class="flow-title" id="flow-welcome-name">' + flowWelcomeTitle() + '</h1>' +
    '<p class="flow-note">接下来 ' + total + ' 个问题，大概 5 分钟。你想到哪说到哪，' +
    '我把你的说法变成你自己的东西：三观卡、口吻，和以后写的每一条稿。</p>' +
    '<form id="flow-intro-form">' +
    '<button class="primary flow-big" type="submit">开始回答 <span aria-hidden="true">→</span></button>' +
    '<p class="small muted flow-skip"><button class="flow-link" type="button" data-flow-action="flow-defer">稍后再说，先去工作台</button></p>' +
    '</form>' +
    '</div></section>';
}
// 名字由 auth-ui.js 在拿到账号后广播；晚到时补一下标题，不重绘整页。
document.addEventListener('ip:user', () => {
  const el = fq('#flow-welcome-name');
  if (el) el.innerHTML = flowWelcomeTitle();
  // t43：登录成功后如果正卡在「题库未解锁」引导，自动重新取题，不用手动点。
  if (String(location.hash || '').startsWith('#/start') && flowData().need_login) {
    route().catch(() => {});
  }
});

/* ---------- 题库没取到：一张引导卡（t42 题库封壳后的降级面，2026-11-17） ----------
   建档的题随账号下发（服务端只留骨架，正文走云端资产）。没登录、没联网、
   资产还没下发到这台机器，或第一次取题还在路上时，questions 是空的。
   这时绝不能渲染一道空干题（那是一屏没有题目的答题页），也不能弹教程墙：
   给一句人话说明＋一个重试动作＋一个「先记一条认知」的出口，人不空转。 */
function flowAssetGateView(data) {
  const login = !!(data && data.need_login);
  if (login) {
    return '<section class="flow-shell"><div class="flow-card">' +
      '<p class="flow-kicker">开始 · 认识你</p>' +
      '<h1 class="flow-title">先登录，题库为你解锁。</h1>' +
      '<p class="flow-note">这 12 题存在云端、按账号下发：登录后自动到手。' +
      '没登录时这里只给登录引导，不会塞给你 0 道空白题；本机已经记下的东西都在，不会丢。</p>' +
      '<div class="flow-nav">' +
      '<button class="primary flow-big" type="button" data-flow-action="flow-login">去登录，解锁 12 题 <span aria-hidden="true">→</span></button>' +
      '<button class="ghost" type="button" data-flow-action="flow-asset-retry">已经登录过，重新取题 <span aria-hidden="true">↻</span></button>' +
      '<button class="ghost" type="button" data-action="new-knowledge">先记一条认知</button>' +
      '</div>' +
      '<p class="small muted">登录成功后会自动回到这里继续；第一次打开题库还在路上时，点「重新取题」即可。</p>' +
      '</div></section>';
  }
  return '<section class="flow-shell"><div class="flow-card">' +
    '<p class="flow-kicker">开始 · 认识你</p>' +
    '<h1 class="flow-title">建档的题目，这次没取到。</h1>' +
    '<p class="flow-note">这 12 题随你的账号下发：没联网，或题目还没发到这台机器时会取不到；' +
    '第一次打开也可能还在路上，等几秒再点一次就行。你本机已经记下的东西都在，不会丢。</p>' +
    '<div class="flow-nav">' +
    '<button class="primary flow-big" type="button" data-flow-action="flow-asset-retry">重新取题 <span aria-hidden="true">↻</span></button>' +
    '<button class="ghost" type="button" data-flow-action="flow-login">去登录</button>' +
    '<button class="ghost" type="button" data-action="new-knowledge">先记一条认知</button>' +
    '</div>' +
    '<p class="small muted">还没登录或要换账号：点「去登录」。</p>' +
    '</div></section>';
}

/* ---------- 12 题建档 ---------- */
function flowQuestionView(data) {
  if (!(data.questions || []).length) return flowAssetGateView(data);
  const q = flowQuestion(data, data.current);
  const index = Math.max(1, Math.min((data.questions || []).length || 12, data.current || 1));
  const total = (data.questions || []).length || 12;
  const answer = (data.answers || {})[q.id] || '';
  const last = index >= total;
  return '<section class="flow-shell"><div class="flow-progress">' +
    '<div class="flow-progress-head"><span>建档 · 第 <strong>' + index + '</strong> / ' + total + ' 题</span>' +
    '<span>' + (data.answered || 0) + ' 题已答</span></div>' +
    '<div class="flow-progress-track"><progress max="' + total + '" value="' + (data.answered || 0) + '"></progress></div>' +
    '</div><div class="flow-card">' +
    '<p class="flow-section">' + esc(q.section) + '</p>' +
    '<h1 class="flow-q">' + esc(q.text) + '</h1>' +
    (q.hint ? '<p class="flow-hint">' + esc(q.hint) + '</p>' : '') +
    (flowExitHint(q) ? '<p class="flow-hint flow-exit"><span aria-hidden="true">→</span> 试着想想：' + esc(flowExitHint(q)) + '</p>' : '') +
    '<p class="flow-note">想到哪说到哪，讲你自己的经历和判断就行，不用回答得“标准”。</p>' +
    '<form id="flow-q-form">' +
    '<input type="hidden" name="qid" value="' + esc(q.id) + '">' +
    '<input type="hidden" name="index" value="' + index + '">' +
    '<textarea name="answer" rows="7" maxlength="6000" placeholder="按住下面按钮说话，识别出来的字会进到这个框里…">' + esc(answer) + '</textarea>' +
    '<div class="flow-voice"><button class="flow-talk" type="button" data-flow-action="talk">按住说话</button>' +
    '<button class="flow-long" type="button" data-flow-action="talk-long">录长段（本地转写）</button>' +
    '<span class="small muted" id="flow-voice-note">短句按住说话；长段用「录长段」，首次准备识别约 1–2 分钟；不方便说话就直接打字。</span></div>' +
    '<div id="flow-voice-guide"></div>' +
    '<div class="flow-nav">' +
    '<button class="ghost" type="button" data-flow-action="flow-prev" ' + (index <= 1 ? 'disabled' : '') + '>上一题</button>' +
    '<button class="ghost" type="button" data-flow-action="flow-skip-q">这题先跳过</button>' +
    '<button class="primary" type="submit">' + (last ? '答完，生成我的三观 →' : '下一题 →') + '</button>' +
    '</div></form>' +
    (data.skipped ? '<p class="small muted">你之前跳过了建档；现在补答也算数，答得越完整，洗稿越像你。</p>' : '') +
    '</div></section>';
}

/* ---------- 三观卡（t28 · P1：主张可编辑，用户改/拍才作数） ---------- */
const FLOW_CARD_ROWS = [['世界观', 'worldview'], ['人生观', 'life'], ['价值观', 'values']];
function flowCardFields(card) {
  return FLOW_CARD_ROWS.map(([label, key]) =>
    '<label class="field flow-claim-edit"><span>' + label + '（不超过 120 字）</span>' +
    '<textarea name="' + key + '" rows="2" maxlength="120" placeholder="用你自己的话写这一句；留空就还是候选">' +
    esc(card[key] || '') + '</textarea></label>').join('');
}
function flowCardFromForm(form) {
  const card = {};
  FLOW_CARD_ROWS.forEach(([, key]) => {
    const el = form.querySelector('[name="' + key + '"]');
    if (el) card[key] = String(el.value || '').trim();
  });
  return card;
}
function flowCardView(data) {
  const card = data.card || {};
  const edited = card.edited_by === 'user';
  return '<section class="flow-shell"><div class="flow-card">' +
    '<p class="flow-kicker">我的三观</p>' +
    '<h1 class="flow-title">这是你的主张，你来定稿。</h1>' +
    '<p class="flow-note">下面三句是模型按你的原话凝的<b>候选</b>，不是定稿——改到你认领为止，改了才算你的。' +
    (edited ? '（已按你的定稿保存，随时可以再改。）' : '') +
    (card.method === 'answers' ? '（模型未连接，先按你的原话首句生成，补答后可重新凝成。）' : '') + '</p>' +
    '<form id="flow-card-form">' + flowCardFields(card) +
    '<p class="small muted">定稿会存进「我的经验库 · 三观」，并标上「本人定稿」；以后洗稿时，它们决定稿子的口吻和价值观。</p>' +
    '<button class="primary flow-big" type="submit">保存我的定稿，进入下一步 <span aria-hidden="true">→</span></button></form>' +
    flowAdvancedBlock(data) +
    '<p class="small muted flow-skip"><button class="flow-link" type="button" data-flow-action="flow-reopen">回去补答 / 重新凝成</button></p>' +
    '</div></section>';
}

/* 进阶层 4 题（可选）：答了挂到三观卡「边界」段，不答不影响下一步。 */
function flowAdvancedBlock(data) {
  const qs = data.advanced_questions || [];
  const adv = data.advanced_answers || {};
  const answered = qs.filter(q => String(adv[q.id] || '').trim());
  const boundary = answered.length
    ? '<div class="flow-boundary"><p class="flow-kicker">边界</p>' + answered.map(q =>
        '<div class="flow-boundary-item"><strong>' + esc(q.section) + ' · ' + esc(q.text) + '</strong>' +
        '<p>' + esc(adv[q.id]) + '</p></div>').join('') + '</div>'
    : '';
  const form = qs.length
    ? '<div class="flow-advanced"><p class="flow-kicker">再往下四题（可选）</p>' +
      '<p class="flow-note">不答也能直接进下一步；答了会补到上面的「边界」，以后写稿更稳。</p>' +
      '<form id="flow-adv-form">' + qs.map(q =>
        '<label class="field space"><span>' + esc(q.text) + '</span>' +
        (q.hint ? '<small class="field-explain">' + esc(q.hint) + '</small>' : '') +
        (flowExitHint(q) ? '<small class="field-explain"><span aria-hidden="true">→</span> 试着想想：' + esc(flowExitHint(q)) + '</small>' : '') +
        '<textarea name="' + esc(q.id) + '" rows="2" maxlength="2000">' + esc(adv[q.id] || '') + '</textarea></label>').join('') +
      '<button class="ghost" type="submit">保存这四题</button></form></div>'
    : '';
  return boundary + form;
}

/* ---------- 赛道 ---------- */
function flowTrackView(data) {
  const tracks = data.tracks || {selfmedia: '做自己的自媒体', clip: '做切片'};
  return '<section class="flow-shell"><div class="flow-card">' +
    '<p class="flow-kicker">最后一步</p>' +
    '<h1 class="flow-title">你主要做哪种内容？</h1>' +
    '<p class="flow-note">只选一次，后面不再问其他设置。选了也能随时改。</p>' +
    '<form id="flow-track-form"><div class="flow-tracks">' +
    Object.entries(tracks).map(([key, label]) => '<label class="flow-track-card"><input type="radio" name="track" value="' + esc(key) + '"' + (data.track === key ? ' checked' : '') + '>' +
      '<span><strong>' + esc(label) + '</strong><small>' + (key === 'clip' ? '不用出镜，专注素材剪辑与带货，通过成交赚取佣金。' : '围绕自己的观点，做长期个人 IP。') + '</small></span></label>').join('') +
    '</div><button class="primary flow-big" type="submit">选好了，去立我的主线 <span aria-hidden="true">→</span></button></form>' +
    '<p class="small muted flow-skip">下一步：从你刚说的三观卡里，结晶你的命题和主线目标（目标可以很实际，比如赚钱、涨粉、出片数；命题可选，不填也能开始）。</p>' +
    '<p class="small muted flow-skip">' + (data.answered ? '已答 ' + data.answered + ' 题。' : '你跳过了建档：洗稿会先用通用口吻，建档越完整，洗稿越像你。') + '</p>' +
    '</div></section>';
}

/* ---------- 已完成：回看与补充（t28：已建档也能改定稿） ---------- */
function flowDoneView(data) {
  const card = data.card || {};
  const edited = card.edited_by === 'user';
  return '<section class="flow-shell"><div class="flow-card">' +
    '<p class="flow-kicker">建档完成</p>' +
    '<h1 class="flow-title">你的三观已经存好。</h1>' +
    '<p class="flow-note">' + (edited ? '已按你的定稿保存；想改随时改，改完再存一次。'
      : '这三句还是候选：改到你认领为止，存下来就标「本人定稿」。') + '</p>' +
    '<form id="flow-card-edit-form">' + flowCardFields(card) +
    '<button class="ghost" type="submit">保存我的定稿</button></form>' +
    '<p class="flow-note">赛道：' + esc((data.tracks || {})[data.track] || '还没选') + ' · 已答 ' + (data.answered || 0) + ' / 12 题。</p>' +
    '<div class="flow-nav"><a class="primary button-link flow-big" href="#/restart">下一步：立定我的主线 →</a>' +
    '<a class="ghost button-link" href="#/create">先进工作台</a>' +
    '<button class="ghost" type="button" data-flow-action="flow-reopen">重新完善建档</button>' +
    '<button class="ghost" type="button" data-flow-action="flow-refinalize">重新凝成三观卡</button></div>' +
    '</div></section>';
}

/* 生成三观卡要等模型 20–60 秒：先给可见等待态，成功/失败都自动收尾。 */
function flowWaitView() {
  const failed = Boolean(flowUi.finalizeError);
  return '<section class="flow-shell"><div class="flow-card">' +
    '<p class="flow-kicker">我的三观</p>' +
    '<h1 class="flow-title">' + (failed ? '这次没凝成三观卡' : '正在生成我的三观（约 20–60 秒）') + '</h1>' +
    (failed
      ? '<p class="notice error-box">' + esc(flowUi.finalizeError) + '</p>' +
        '<p class="flow-note">你的回答都已经保存，不用重答。可以直接重试，或先回去补答。</p>' +
        '<div class="flow-nav"><button class="primary" type="button" data-flow-action="flow-retry-finalize">重试生成</button>' +
        '<button class="ghost" type="button" data-flow-action="flow-reopen">回去补答</button></div>'
      : '<div class="wash-note"><span class="processing-mark" aria-hidden="true">◌</span><div>' +
        '<strong>正在把你的口述压成三句主张，请稍等。</strong>' +
        '<p class="small muted" id="flow-wait-elapsed">已等待 0 秒…</p>' +
        '<p class="small muted">生成完会自动进入三观卡，请不要关掉页面。</p></div></div>') +
    '</div></section>';
}

function flowStopWaitTimer() {
  if (flowUi.timer) { clearInterval(flowUi.timer); flowUi.timer = null; }
}

function flowStartWaitTimer() {
  flowStopWaitTimer();
  flowUi.elapsed = 0;
  flowUi.timer = setInterval(() => {
    flowUi.elapsed += 1;
    const el = fq('#flow-wait-elapsed');
    if (el) el.textContent = '已等待 ' + flowUi.elapsed + ' 秒…';
  }, 1000);
}

/* route() 会先重新拉 bootstrap（慢的时候要等十几秒），等待态必须立刻上屏，不能等它。 */
function flowPaintWaitNow() {
  const box = fq('#main');
  if (!box) return;
  box.innerHTML = flowWaitView();
  box.scrollTop = 0;
}

async function flowFinalize() {
  if (flowUi.finalizing) return;
  flowUi.finalizing = true;
  flowUi.finalizeError = '';
  flowPaintWaitNow();
  flowStartWaitTimer();
  route().catch(() => {});
  try {
    const next = await api('/flow/finalize', 'POST', {});
    if (state.boot) state.boot.flow = next;
    flowUi.finalizing = false;
    flowStopWaitTimer();
    route();
  } catch (e) {
    flowUi.finalizing = false;
    flowUi.finalizeError = e.message || '生成没有完成，请重试。';
    flowStopWaitTimer();
    flowPaintWaitNow();
    route().catch(() => {});
  }
}

function flowView() {
  if (flowUi.finalizing || flowUi.finalizeError) return flowWaitView();
  const data = flowData();
  if (data.completed) return flowDoneView(data);
  // 题库没下发到手：序言/答题两步都需要题目，先给引导卡，不渲染空题干。
  if (!(data.questions || []).length && (data.stage === 'intro' || data.stage === 'question')) return flowAssetGateView(data);
  if (data.stage === 'intro') return flowIntro(data);
  if (data.stage === 'card' && data.card) return flowCardView(data);
  if (data.stage === 'track') return flowTrackView(data);
  return flowQuestionView(data);
}

/* ---------- 落地首屏：0 素材写一句开工（主）/ 链接洗稿（次要） ---------- */
function flowHomePanel() {
  const data = flowData();
  const m = data.materials || {};
  const hint = data.card
    ? ('已按你的三观建档；素材库存了 ' + (m.voice || 0) + ' 条原话、' + (m.story || 0) + ' 条故事。')
    : '还没建档：洗稿会先用通用口吻，建档越完整，洗稿越像你。';
  // D5 全民工具：落地首屏不假设用户手上有链接/素材/账号/认知储备。
  // 主路径 = 0 素材写一句就开工；有现成链接的人走次要替代路径（洗稿），整条保留不删。
  // 入口只在未建档时出现，复用 app.js 的 data-action="create" → createDialog()「我直接写稿」。
  // t198：主入口收成一个单对话输入口；有现成链接、想改成自己的讲法的人，
  // 仍走下面这条次要路径（原 wash 链路整条保留，不与 t196 的抓取/改写逻辑抢入口）。
  const intake = (typeof window.intakePanel === 'function') ? window.intakePanel() : '';
  return '<section class="home-hero home-trunk wash-hero">' +
    '<p class="home-kicker">开始做内容</p>' +
    '<h1 class="home-title">把想说的，变成能用的逐字稿</h1>' +
    '<p class="home-sub">一个输入口：贴分享文字或链接，写几句想说的，或把截图、录音、视频拖进来。点一次自动识别，完成后只给你逐字稿。这里的输入口和首页「对话」是同一个，在这儿发的，回到对话也能接着看。</p>' +
    intake +
    '</section>' +
    '<details class="wash-secondary"><summary>有现成链接，想改成你的讲法？</summary>' +
    '<form id="wash-link-form" class="wash-form">' +
    '<div class="wash-input-row"><input name="url" type="text" inputmode="url" autocomplete="off" spellcheck="false" placeholder="粘贴链接，或整段分享文字：公众号 / 网页 / 抖音作品…">' +
    '<button class="primary" type="submit">读取并拆方向 <span aria-hidden="true">→</span></button></div>' +
    '<details class="wash-raw" id="wash-raw"><summary>抓不到正文？直接粘贴原文</summary>' +
    '<textarea name="raw_text" rows="6" placeholder="把对方的原文粘到这里，效果一样。"></textarea>' +
    '<p class="small muted">链接被拦、要登录、抓不到时，用这一条就能继续。</p></details>' +
    '<p class="small muted" id="wash-hint">' + esc(hint) + '</p>' +
    (!data.completed ? '<p class="small muted">想让稿子更像你？<a class="link" href="#/start">补几句你的说法 →</a>（不做也能继续，随时可补）</p>' : '') +
    '</form><div id="wash-stage"></div></details>' +
    '<div id="wash-history"></div>';
}

function flowPaintStage() {
  const box = fq('#wash-stage');
  if (!box) return;
  const w = flowUi.wash;
  if (w.busy) {
    const steps = (w.steps || []).map(s =>
      '<li class="wash-step ' + esc(s.status) + '">' +
      (s.status === 'done' ? '✓' : s.status === 'running' ? '◌' : s.status === 'skipped' ? '·' : '○') +
      ' ' + esc(s.label) + '</li>').join('');
    box.innerHTML = '<div class="wash-note"><span class="processing-mark" aria-hidden="true">◌</span>' +
      '<div><strong>' + esc(w.progress || '处理中…') + '</strong>' +
      (steps ? '<ul class="wash-steps">' + steps + '</ul>' : '') +
      '<p class="small muted">不用重复点，完成后会自动显示；视频要先下载再转写，约 1–2 分钟。</p></div></div>';
    return;
  }
  if (w.blocked) {
    /* t196：正文读到了、卡在改写这一步。文案必须和「没读到正文」分开——
       否则用户以为链接白粘了，实际是他没登录/模板没下发。 */
    const b = w.blocked;
    const login = b.kind === 'login';
    const head = login ? '正文已经读到，但改写要先登录云端模板' : '正文已经读到，改写这一步没通过';
    const chars = Number(b.chars) || 0;
    const tail = login
      ? '登录后会从这里接着做，不用重新粘链接。'
      : '可以点「重试改写」再试一次；还不行就把原文贴进上面「抓不到正文？直接粘贴原文」，用原文继续。';
    box.innerHTML = '<div class="wash-note warn"><div><strong>' + esc(head) + '</strong>' +
      (chars ? '<p class="small muted">已读到正文 ' + chars + ' 字' + (b.url ? '（' + esc(String(b.url).slice(0, 120)) + '）' : '') + '，这一步没有丢。</p>' : '') +
      '<p>' + esc(b.message || '') + '</p>' +
      '<div class="row spread space">' +
      (login ? '<button class="primary small-button" type="button" data-flow-action="flow-login">去登录</button>'
             : '<button class="primary small-button" type="button" data-flow-action="wash-retry-stage1">重试改写</button>') +
      '<button class="ghost small-button" type="button" data-flow-action="' + (login ? 'wash-retry-stage1' : 'flow-login') + '">' +
      (login ? '我已经登录过，直接重试' : '先去登录') + '</button></div>' +
      '<p class="small muted">' + esc(tail) + '</p></div></div>';
    return;
  }
  if (w.error && w.errorKind !== 'stage2') {
    box.innerHTML = '<div class="wash-note warn"><div><strong>这次没直接读到正文</strong>' +
      '<p>' + esc(w.error) + '</p><p class="small muted">不会卡住：点开上面「抓不到正文？直接粘贴原文」，把原文贴进来，再点一次「读取并拆方向」。</p>' +
      '<details><summary class="small muted">查看详情</summary><pre class="raw-markdown">' + esc(w.errorDetail || w.error) + '</pre></details></div></div>';
    return;
  }
  if (w.stage === 'plan' && w.plan) {
    const p = w.plan;
    box.innerHTML = (w.error ? flowStage2FailCard(w) : '') + (w.video ? flowVideoCard(w.video) : '') +
      '<div class="wash-plan"><div class="wash-plan-head"><span class="pill ok">已读到正文</span>' +
      '<h3>' + esc(p.topic) + '</h3></div>' +
      '<p class="small muted">结构：' + esc(p.structure) + '</p>' +
      '<div class="wash-angle-grid">' + (p.angles || []).map((a, i) =>
        '<article class="wash-angle"><span class="wash-angle-num">0' + (i + 1) + '</span><div><strong>' + esc(a.label) + '</strong><p>' + esc(a.angle) + '</p></div>' +
        '<button class="primary small-button" data-flow-action="wash-choose" data-index="' + i + '">选这个方向 →</button></article>').join('') + '</div>' +
      '<p class="small muted">选完直接生成完整口播逐字稿；讲稿页可照读、可复制。</p></div>';
    return;
  }
  box.innerHTML = '';
}

/* 视频资料卡：只展示抓到的真实字段；抓不到的保留行并写明原因。 */
function flowVideoCard(p) {
  const val = (f) => (f && f.value !== null && f.value !== '') ? f.value : null;
  const note = (f) => (f && f.note) ? f.note : '未取到';
  const title = val(p.title) || '（未取到标题）';
  const author = val(p.author);
  const dur = val(p.duration_ms);
  const stats = p.stats || {};
  const s = [['play', '播放'], ['like', '点赞'], ['collect', '收藏'], ['share', '转发'], ['comment', '评论']]
    .map(([k, label]) => label + ' ' + (val(stats[k]) !== null ? val(stats[k]) : '—')).join(' · ');
  const comments = p.comments || {};
  const top = (comments.top || []).slice(0, 3).map(c =>
    '<li>❤ ' + esc(String(c.likes)) + ' · ' + esc(c.content.slice(0, 60)) + '</li>').join('');
  const tr = p.transcript || {};
  return '<div class="wash-plan wash-video"><div class="wash-plan-head"><span class="pill ok">这是视频</span>' +
    '<h3>' + esc(title) + '</h3></div>' +
    '<p class="small muted">' + (author ? '作者：' + esc(author) + ' · ' : '') +
    (dur ? '时长 ' + Math.floor(dur / 1000) + ' 秒 · ' : '') + s + '</p>' +
    '<p class="small muted">逐字稿：' + (tr.text ? esc(tr.chars + ' 字（' + (tr.route_note || tr.route || '转写') + '）')
      : esc(tr.reason || '未取到')) + '</p>' +
    '<p class="small muted">评论：' + (comments.items && comments.items.length
      ? '抓到 ' + comments.items.length + ' 条' + (comments.total_fetched < 30 ? '（不足 30 条：' + esc(comments.note) + '）' : '')
      : esc(comments.note || '未取到')) + '</p>' +
    (top ? '<ul class="small muted wash-top-comments">' + top + '</ul>' : '') +
    (p.markdown_path ? '<p class="small muted">解剖包已存：' + esc(p.markdown_path) + '</p>' : '') +
    '</div>';
}

async function flowRenderHistory() {
  const box = fq('#wash-history');
  if (!box) return;
  let data;
  try { data = await api('/wash/history'); } catch (e) { return; }
  const items = data.items || [];
  if (!items.length) { box.innerHTML = ''; return; }
  box.innerHTML = '<section class="home-section"><div class="home-sec-head"><h2>洗稿记录' +
    '<span class="home-sec-note">已落盘 · 退出重进仍可回看</span></h2></div><div class="panel wash-history">' +
    items.map(x => '<div class="wash-history-row"><div><strong class="small">' + esc(x.topic || '未命名') + '</strong>' +
      '<span class="small muted"> · ' + esc(x.chosen ? '方向：' + x.chosen : '还没选方向') + ' · ' + stamp(x.created_at) + '</span></div>' +
      (x.project_id ? '<a class="link small" href="#/project/' + encodeURIComponent(x.project_id) + '/script">查看逐字稿 →</a>'
        : '<button class="ghost small-button" data-flow-action="wash-open-plan" data-id="' + esc(x.id) + '">继续选方向</button>') +
      '</div>').join('') + '</div></section>';
}

/* ---------- 语音：短句走浏览器识别，长段走本地 ASR ---------- */
let flowMediaRec = null;
let flowLongStream = null;
let flowLongChunks = [];
let flowLongBusy = false;
let flowLongDone = false;
let flowLongSeq = 0;
let flowSpeechSeen = new Set();
let flowSpeechLast = '';
function flowSpeechSupported() {
  return Boolean(window.SpeechRecognition || window.webkitSpeechRecognition);
}
function flowLongSupported() {
  return Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
}
/* 2026-11-17 t82：没有麦克风/不支持录音时的有温度引导（微信转文字＋为什么＋打消顾虑）。
   CSP 用 CSSOM 加类，不写内联 style。 */
let flowVoiceGuideCss = false;
function flowVoiceGuideCssRules() {
  if (flowVoiceGuideCss) return;
  flowVoiceGuideCss = true;
  try {
    const sheet = new CSSStyleSheet();
    [
      '.flow-voice-guide{margin:14px 0 4px;padding:14px 16px;border:1px solid var(--border-strong);border-left:3px solid var(--gold);border-radius:12px;background:var(--surface);text-align:left}',
      '.flow-voice-guide h3{margin:0 0 8px;font-size:14px;color:var(--gold);display:flex;gap:6px;align-items:center}',
      '.flow-voice-guide p{margin:0 0 8px;font-size:13px;line-height:1.8;color:var(--text)}',
      '.flow-voice-guide ol{margin:0 0 10px;padding-left:20px;font-size:13px;line-height:1.9;color:var(--text)}',
      '.flow-voice-guide .fvg-why{font-size:12px;color:var(--gold);line-height:1.8;margin:8px 0}',
      '.flow-voice-guide .fvg-soft{font-size:12px;color:var(--muted);line-height:1.9;margin:0}',
      '.flow-voice-guide .fvg-soft b{color:var(--text);font-weight:500}'
    ].forEach((text, i) => sheet.insertRule(text, i));
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  } catch (err) { /* 样式注入失败不影响引导文字 */ }
}
function flowVoiceGuideHtml() {
  return '<div class="flow-voice-guide" role="note" aria-label="没有麦克风时怎么用语音建档">' +
    '<h3>没有麦克风也能用「说」的</h3>' +
    '<p>拿手机打开微信，对着对话框按住说话，说完长按这条语音点「转文字」，把文字复制回这里粘进输入框就行。</p>' +
    '<ol><li>微信里按住说话，把这道题的想法说出来（30 秒也行，想到哪说到哪）</li>' +
    '<li>长按那条语音 → 点「转文字」→ 复制文字</li>' +
    '<li>回到这里粘贴到输入框；一句两句都算，后面还能改</li></ol>' +
    '<p class="fvg-why">为什么值得费这一步：你的真实声音是去 AI 味的金矿——系统要学的是你本人说话的口气、节奏和用词，稿子才会像你说的，而不是机器腔。打字当然也能存，但「怎么说」的信息会少一点。</p>' +
    '<p class="fvg-soft"><b>说不好、说断了、没头绪都没关系。</b>AI 会帮你修饰成完整句子；碎片、半句、来回改口都有用，不用一次说漂亮。实在不想说，就直接打字，内容一样会保存。</p>' +
    '</div>';
}
function flowVoiceGuide(show) {
  const host = fq('#flow-voice-guide');
  if (!host) return;
  if (!show) { host.innerHTML = ''; return; }
  flowVoiceGuideCssRules();
  host.innerHTML = flowVoiceGuideHtml();
}
function flowVoiceInit() {
  const btn = document.querySelector('[data-flow-action="talk"]');
  const longBtn = document.querySelector('[data-flow-action="talk-long"]');
  const note = fq('#flow-voice-note');
  if (!btn) return;
  if (!flowSpeechSupported()) {
    btn.disabled = true;
    btn.textContent = '短句识别不可用';
  }
  if (longBtn && !flowLongSupported()) {
    longBtn.disabled = true;
    longBtn.textContent = '长段转写不可用';
  }
  if (note) {
    if (flowUi.note) note.textContent = flowUi.note;
    else if (!flowSpeechSupported() && !flowLongSupported()) note.textContent = '这个浏览器没有可用的语音识别；可以按下面「微信转文字」的办法，把说的话转成文字贴进来。';
    flowVoiceGuide(!flowSpeechSupported() && !flowLongSupported());
  }
}
function flowStartTalk(btn) {
  if (flowRec) return;
  if (flowLongBusy || (flowMediaRec && flowMediaRec.state !== 'inactive')) {
    // 与长段录音互斥：长段在录时不开短句识别，避免两条路径同时写同一个输入框。
    const tip = fq('#flow-voice-note');
    if (tip) tip.textContent = '长段录音进行中，先点一下结束它，再用短句。';
    return;
  }
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return;
  const ta = fq('#flow-q-form textarea[name="answer"]');
  if (!ta) return;
  const note = fq('#flow-voice-note');
  const rec = new SR();
  flowRec = rec;
  flowUi.note = '';
  flowSpeechSeen = new Set();
  flowSpeechLast = '';
  rec.lang = 'zh-CN';
  rec.continuous = true;
  rec.interimResults = true;
  rec.onresult = e => {
    let interim = '';
    const fresh = [];
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const r = e.results[i];
      if (!r.isFinal) { interim += r[0].transcript; continue; }
      const text = String(r[0].transcript || '').trim();
      if (!text) continue;
      const key = i + ':' + text;
      // 同一 final 结果被浏览器重复派发（同序号同文本）只写一次。
      if (flowSpeechSeen.has(key)) continue;
      flowSpeechSeen.add(key);
      fresh.push(text);
    }
    // 连续两次派发同一段文本也只写一次（同一句话重复朗读会被合并，属已知取舍）。
    const final = fresh.filter(t => t !== flowSpeechLast).join(' ');
    if (final) {
      const cur = ta.value;
      ta.dataset.via = 'asr';
      ta.value = (cur && !/\s$/.test(cur) ? cur + ' ' : cur) + final;
      flowSpeechLast = fresh[fresh.length - 1];
    }
    if (note) note.textContent = interim ? ('识别中：' + interim) : '正在听，松开就停。';
  };
  rec.onerror = e => {
    const message = {
      'not-allowed': '没有麦克风权限。可以到浏览器地址栏允许麦克风，或按下面「微信转文字」把说的话贴进来。',
      'audio-capture': '没找到麦克风。可以按下面「微信转文字」把说的话贴进来，内容一样会保存。',
      'network': '语音识别连不上网。可以直接打字。',
      'no-speech': '没听到声音，再按一次试试。'
    }[e.error] || ('语音识别出错（' + e.error + '）。可以直接打字。');
    if (e.error === 'audio-capture' || e.error === 'not-allowed') flowVoiceGuide(true);
    flowUi.note = message;
    if (note) note.textContent = message;
  };
  rec.onend = () => {
    btn.classList.remove('is-live');
    btn.textContent = '按住说话';
    if (flowRec === rec) flowRec = null;
    if (note && !flowUi.note) note.textContent = '可以继续按住说话，也可以打字。';
  };
  try {
    rec.start();
    btn.classList.add('is-live');
    btn.textContent = '正在听…松开结束';
  } catch (e) {
    flowUi.note = '浏览器暂时不让开始识别，直接打字也一样。';
    if (note) note.textContent = flowUi.note;
    flowRec = null;
  }
}
function flowStopTalk() {
  if (flowRec) { try { flowRec.stop(); } catch (e) { /* 已结束 */ } }
}

/* 长段：MediaRecorder 录一段 → /api/flow/transcribe 走本地 ASR（首次冷启动 1–2 分钟）。 */
function flowLongToggle(btn) {
  if (flowLongBusy) return;
  if (flowMediaRec && flowMediaRec.state !== 'inactive') {
    try { flowMediaRec.stop(); } catch (e) { /* 已结束 */ }
    return;
  }
  flowStartLong(btn);
}

async function flowStartLong(btn) {
  if (flowRec) flowStopTalk();  // 与短句识别互斥：开长段前先停短句
  const ta = fq('#flow-q-form textarea[name="answer"]');
  const note = fq('#flow-voice-note');
  if (!ta) return;
  if (!flowLongSupported()) {
    if (note) note.textContent = '长段录不了：这个浏览器不支持录音。可以按下面「微信转文字」把说的话贴进来。';
    flowVoiceGuide(true);
    return;
  }
  if (note) note.textContent = '正在请求麦克风权限…';
  try {
    flowLongStream = await navigator.mediaDevices.getUserMedia({audio: true});
  } catch (e) {
    if (note) note.textContent = '没拿到麦克风权限，长段录不了。可以按下面「微信转文字」把说的话贴进来。';
    flowVoiceGuide(true);
    return;
  }
  let rec;
  try {
    const mime = window.MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : '';
    rec = new MediaRecorder(flowLongStream, mime ? {mimeType: mime} : undefined);
  } catch (e) {
    flowLongRelease();
    if (note) note.textContent = '这个浏览器不能录长段，直接打字也一样。';
    return;
  }
  flowLongChunks = [];
  flowLongDone = false;
  const seq = ++flowLongSeq;
  flowMediaRec = rec;
  rec.ondataavailable = e => { if (e.data && e.data.size) flowLongChunks.push(e.data); };
  rec.onstop = () => {
    // Safari/部分 Chromium 在 stop() 与音轨结束时都会触发 onstop；用一次性标记去重。
    if (flowLongDone) return;
    flowLongDone = true;
    flowFinishLong(ta, note, btn, seq);
  };
  rec.start();
  btn.classList.add('is-live');
  btn.textContent = '录长段中…点一下结束';
  if (note) note.textContent = '正在录长段；说完再点一下按钮，转写首次要等 1–2 分钟。';
}

function flowLongRelease() {
  if (flowLongStream) {
    try { flowLongStream.getTracks().forEach(t => t.stop()); } catch (e) { /* 已释放 */ }
    flowLongStream = null;
  }
  flowMediaRec = null;
}

async function flowFinishLong(ta, note, btn, seq) {
  const chunks = flowLongChunks.slice();
  flowLongChunks = [];
  flowLongRelease();
  btn.classList.remove('is-live');
  btn.textContent = '录长段（本地转写）';
  if (!chunks.length) {
    if (note) note.textContent = '没录到声音，再试一次，或直接打字。';
    return;
  }
  const blob = new Blob(chunks, {type: 'audio/webm'});
  if (blob.size < 300) {
    if (note) note.textContent = '这段太短，没录到内容；说完整一点再试。';
    return;
  }
  flowLongBusy = true;
  btn.disabled = true;
  if (note) note.textContent = '正在准备识别（首次约 1–2 分钟），请不要关掉页面…';
  try {
    const audio = await flowBlobBase64(blob);
    const res = await api('/flow/transcribe', 'POST', {audio, mime: blob.type || 'audio/webm'});
    if (seq !== flowLongSeq) return;  // 已有更新的录音，丢弃这次旧结果
    if (!res.ok) throw Error(res.reason || '本地转写没成功。');
    const text = String(res.text || '').trim();
    if (!text) throw Error('没识别出文字，再录一次或直接打字。');
    const cur = ta.value.replace(/\s+$/, '');
    // 幂等写入：同一段识别结果只追加一次，重复回调不会把文本翻倍。
    ta.dataset.via = 'asr';
    ta.value = cur ? (cur.endsWith(text) ? ta.value : cur + ' ' + text) : text;
    if (note) note.textContent = '识别完成，文字已进输入框；可以改一改再继续。';
    const form = fq('#flow-q-form');
    if (form) {
      const qid = form.querySelector('[name="qid"]');
      if (qid) flowSave({answers: {[qid.value]: ta.value}, answer_meta: {[qid.value]: {via: 'asr'}}}).catch(() => {});
    }
  } catch (e) {
    if (note) note.textContent = (e.message || '本地转写失败') + ' 可以直接打字，内容一样能保存。';
  } finally {
    flowLongBusy = false;
    btn.disabled = false;
  }
}

function flowBlobBase64(blob) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result || '').split(',')[1] || '');
    fr.onerror = () => reject(Error('录音读取失败，请重录一次。'));
    fr.readAsDataURL(blob);
  });
}

/* ---------- 保存与流程推进 ---------- */
// 自动存（输入停顿/光标离开）和「下一题」会几乎同时发请求；按发出顺序串起来，
// 免得后发的旧内容先回、把已经推进到第 N+1 题的进度又写回第 N 题。
let flowSaveChain = Promise.resolve();
// 2026-11-17 t53：答案保存失败不许静默——留住没存上的内容，页面上可见「这句还没存上——先别关页面」＋重试。
// 良性兜底（defer_hint 这类非答案标志）走 silent，不弹警告，别误伤。
let flowSavePending = [];   // [{payload, error}] —— 按失败顺序排队，重试逐笔补存
let flowSaveBusy = false;

function flowSaveWarningClear() {
  const box = fq('#flow-save-warning');
  if (box) box.remove();
}

function flowSaveWarning() {
  const main = fq('#main');
  if (!main) return;
  let box = fq('#flow-save-warning');
  if (!box) {
    box = document.createElement('div');
    box.id = 'flow-save-warning';
    box.className = 'flow-hint';
    box.setAttribute('role', 'alert');
    const text = document.createElement('span');
    text.id = 'flow-save-warning-text';
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'flow-link';
    retry.textContent = '重试保存';
    retry.addEventListener('click', () => retryFlowSave());
    box.append('这句还没存上——先别关页面。答案还在框里，点「重试保存」再试一次。 ', text, retry);
    main.insertBefore(box, main.firstChild);
  }
  const t = fq('#flow-save-warning-text');
  if (t) {
    const why = flowSavePending.length && flowSavePending[flowSavePending.length - 1].error;
    t.textContent = why ? '（' + String(why) + '）' : '';
  }
}

async function retryFlowSave() {
  if (!flowSavePending.length || flowSaveBusy) return;
  flowSaveBusy = true;
  try {
    while (flowSavePending.length) {
      const item = flowSavePending[0];
      const next = await api('/flow', 'POST', item.payload);
      if (state.boot) state.boot.flow = next;
      flowSavePending.shift();
    }
    flowSaveWarningClear();
    toast('已补存成功。');
    await route();
  } catch (e) {
    const why = (e && e.message) || '网络或服务端没回话';
    if (flowSavePending.length) flowSavePending[flowSavePending.length - 1].error = why;
    flowSaveWarning();
    toast('还是没存上：' + why + '。答案还在框里，先别关页面。');
  } finally {
    flowSaveBusy = false;
  }
}

function flowSave(payload, opts) {
  const silent = !!(opts && opts.silent);
  const same = (a, b) => { try { return JSON.stringify(a) === JSON.stringify(b); } catch (e) { return false; } };
  const run = async () => {
    try {
      const next = await api('/flow', 'POST', payload);
      if (state.boot) state.boot.flow = next;
      if (flowSavePending.length) {
        flowSavePending = flowSavePending.filter(x => !same(x.payload, payload));
        if (!flowSavePending.length) flowSaveWarningClear();
      }
      return next;
    } catch (err) {
      // 不吞：答案类保存失败要让它看得见（defer_hint 这类良性兜底保持静默）。
      if (!silent) {
        flowSavePending = flowSavePending.filter(x => !same(x.payload, payload));
        flowSavePending.push({payload, error: (err && err.message) || '网络或服务端没回话'});
        flowSaveWarning();
      }
      throw err;
    }
  };
  const p = flowSaveChain.then(run, run);
  flowSaveChain = p.catch(() => {});
  return p;
}

async function flowHandleSubmit(form) {
  const data = flowData();
  if (form.id === 'flow-intro-form') {
    // 开始答题：从已答的题接着走（中途退出再进来不重来）。
    await flowSave({intro_seen: true, stage: 'question', current: data.current || 1, deferred: false});
    return route();
  }
  if (form.id === 'flow-q-form') {
    const qid = form.querySelector('[name="qid"]').value;
    const index = Number(form.querySelector('[name="index"]').value) || 1;
    const answer = form.querySelector('[name="answer"]').value || '';
    if (index >= (data.questions || []).length) {
      await flowSave({answers: {[qid]: answer}, current: index, stage: 'question'});
      return flowFinalize();
    } else {
      await flowSave({answers: {[qid]: answer}, current: index + 1, stage: 'question'});
    }
    return route();
  }
  if (form.id === 'flow-card-form') {
    // t28 · P1：三观定稿随这一步一起存（用户改/拍才作数），再进赛道。
    const card = flowCardFromForm(form);
    await flowSave({card, stage: 'track'});
    toast('定稿已保存——这是你的主张。');
    return route();
  }
  if (form.id === 'flow-card-edit-form') {
    const card = flowCardFromForm(form);
    await flowSave({card});
    toast('定稿已保存——这是你的主张。');
    return route();
  }
  if (form.id === 'flow-adv-form') {
    const answers = {};
    (data.advanced_questions || []).forEach(q => {
      answers[q.id] = String(form.elements[q.id] ? form.elements[q.id].value : '').trim();
    });
    await flowSave({answers});
    return route();
  }
  if (form.id === 'flow-track-form') {
    const track = String(Object.fromEntries(new FormData(form)).track || '');
    if (!track) throw Error('先选一个赛道。');
    await flowSave({track, stage: 'done', completed: true});
    // t28 · P2/P5：建档完成不再直接丢进工作台，先接立轴（从三观卡结晶命题/目标），再进工作台。
    if (location.hash === '#/restart') return route();
    location.hash = '#/restart';
    return;
  }
  if (form.id === 'wash-link-form') {
    return flowWashSubmit(form);
  }
}

async function flowWashSubmit(form) {
  const values = Object.fromEntries(new FormData(form));
  // t196：框里允许直接粘整段分享文字；这里再收一次链接（粘贴事件没覆盖到的路径兜底）。
  const picked = shareUrlOf(values.url);
  const url = picked.url || String(values.url || '').trim();
  const raw = String(values.raw_text || '').trim();
  if (!url && !raw) throw Error('先粘贴一个链接，或把原文贴进下面的框里。');
  // t201：长文不在这里硬拒；交给 wash_service 按模型上限预检，返回结构化 too_long。
  const wash = flowUi.wash;
  Object.assign(wash, {busy: true, error: '', blocked: null, progress: raw ? '正在拆出选题、结构和一个方向…' : '正在读取链接正文…', stage: 'input', plan: null});
  flowPaintStage();
  let text = raw;
  let title = '';
  let sourceUrl = url;
  if (!text) {
    try {
      const link = await api('/wash/link', 'POST', {url});
      if (!link.ok && link.kind !== 'video') throw Error(link.reason || '这个链接没读到正文。');
      if (link.ok) {
        text = link.text;
        title = link.title || '';
        sourceUrl = link.url || url;
      } else {
        // 视频链接不再提示“不支持”：转投既有视频通道（下载→转写→整理）。
        const started = await api('/wash/video/start', 'POST', {url});
        Object.assign(wash, {busy: true, error: '', steps: started.steps || [],
          progress: started.message || '这是视频，正在解析（下载→转写，约 1–2 分钟）'});
        flowPaintStage();
        const result = await flowWashPollVideo(started.job_id);
        if (!result.ok) throw Error(result.error);
        text = result.text;
        title = result.title || '';
        sourceUrl = result.url || url;
        wash.video = result.package || null;
      }
    } catch (e) {
      wash.busy = false;
      wash.steps = [];
      // 只有「真的没读到正文」才挂到读正文那一栏；模板/登录类错误照实说，不冒充抓取失败。
      if (e && e.code === 'prompt_login_required') {
        wash.blocked = {kind: 'login', message: e.message, chars: 0, text: '', url: sourceUrl};
        wash.error = '';
      } else {
        wash.error = e.message;
        wash.errorDetail = (e && e.raw) || (e && e.message) || '';   // 原话留给「查看详情」
      }
      flowPaintStage();
      const rawBox = fq('#wash-raw');
      if (rawBox) rawBox.open = true;
      return;
    }
  }
  wash.progress = '正在拆出选题、结构和一个方向…';
  flowPaintStage();
  try {
    const plan = await api('/wash/stage1', 'POST', {text, url: sourceUrl, source_title: title, track: flowData().track || ''});
    flowUi.wash = {stage: 'plan', plan, busy: false, error: '', blocked: null, progress: '', video: wash.video || null};
  } catch (e) {
    // 正文已经读到了：失败在「改写」这一步。登录/模板没到位就如实说，并留重试路径——
    // 不能把这句话显示成「这次没直接读到正文」，那会让人以为链接白粘了。
    const login = e && e.code === 'prompt_login_required';
    flowUi.wash = {stage: 'input', plan: null, busy: false, error: '', progress: '',
      errorKind: 'rewrite', video: wash.video || null,
      blocked: {kind: login ? 'login' : 'rewrite', message: e.message, chars: text.length,
                text, title, url: sourceUrl}};
  }
  flowPaintStage();
}

/* 正文已读到、只差改写这一步（登录/模板/模型）时的重试：不再重新抓链接。 */
async function flowWashRetryStage1() {
  const w = flowUi.wash;
  const blocked = w.blocked || {};
  const text = String(blocked.text || '');
  if (text.length < 60) { toast('正文不够长，先把原文贴进下面的框。'); return; }
  Object.assign(flowUi.wash, {busy: true, error: '', blocked: null, steps: [],
    progress: '正文已读到 ' + text.length + ' 字，正在拆出选题、结构和一个方向…'});
  flowPaintStage();
  try {
    const plan = await api('/wash/stage1', 'POST', {text, url: blocked.url || '', source_title: blocked.title || '', track: flowData().track || ''});
    flowUi.wash = {stage: 'plan', plan, busy: false, error: '', blocked: null, progress: '', video: w.video || null};
  } catch (e) {
    const login = e && e.code === 'prompt_login_required';
    flowUi.wash = {stage: 'input', plan: null, busy: false, error: '', progress: '',
      errorKind: 'rewrite', video: w.video || null,
      blocked: {kind: login ? 'login' : 'rewrite', message: e.message, chars: text.length,
                text, title: blocked.title || '', url: blocked.url || ''}};
  }
  flowPaintStage();
}

async function flowWashPollVideo(jobId) {
  const started = Date.now();
  for (;;) {
    if (Date.now() - started > 240000) {
      return {ok: false, error: '解析超时（4 分钟）。视频可能太长或网络慢；可以粘贴原文继续。'};
    }
    let job;
    try {
      job = await api('/wash/video/status?job=' + encodeURIComponent(jobId));
    } catch (e) {
      return {ok: false, error: e.message};
    }
    Object.assign(flowUi.wash, {steps: job.steps || [], progress: job.stage_text || job.stage || '处理中…', busy: true});
    flowPaintStage();
    if (job.status === 'done') {
      const p = job.result || {};
      const t = p.transcript || {};
      if (t.text) return {ok: true, text: t.text, title: (p.title || {}).value || '',
        url: (p.url || {}).canonical || '', package: p};
      const reason = t.reason || '未取到口播逐字稿';
      const extra = p.collection_note ? '；' + p.collection_note : '';
      return {ok: false, error: reason + extra + '。可以点开「抓不到正文？直接粘贴原文」继续。'};
    }
    if (job.status === 'failed') {
      return {ok: false, error: (job.error || '视频解析失败') + '。可以粘贴原文继续。'};
    }
    await new Promise(r => setTimeout(r, 1500));
  }
}

async function flowChooseAngle(el) {
  const plan = flowUi.wash.plan;
  if (!plan || flowUi.wash.busy) return;
  flowUi.wash.busy = true;
  flowUi.wash.progress = '正在写完整口播稿，约 20–60 秒…';
  flowUi.wash.error = '';
  flowUi.wash.errorKind = '';
  flowUi.wash.chosen = Number(el.dataset.index) || 0;
  flowPaintStage();
  try {
    const result = await api('/wash/stage2', 'POST', {plan_id: plan.plan_id, index: Number(el.dataset.index)});
    flowUi.wash = {stage: 'input', plan: null, busy: false, error: '', progress: ''};
    toast('完整口播稿已生成，正在打开讲稿页。');
    location.hash = '#/project/' + encodeURIComponent(result.project_id) + '/script';
  } catch (e) {
    /* 2026-11-25：失败发生在「成稿」这一步，正文和方向都还在（plan 不清）。标题如实说成稿没通过；
       人话与下一步来自后端登记表（app.py customer_error）：e.message 是人话，e.raw 是原话。 */
    flowUi.wash.busy = false;
    flowUi.wash.progress = '';
    flowUi.wash.error = e.message;
    flowUi.wash.errorKind = 'stage2';
    flowUi.wash.errorDetail = (e.raw || (e.data && e.data.detail) || e.message || '');
    flowUi.wash.errorNext = (e && e.next) || {label: '重试成稿', action: 'retry'};
    flowUi.wash.errorRetry = typeof e.retry === 'boolean' ? e.retry : true;   // 配置类失败后端说 retry:false，就不再给重试
    flowPaintStage();
  }
}

/* 只重做成稿这一步：沿用上面记下的方向序号，不重新抓链接、不重新拆方向。接口同上：POST /api/wash/stage2。 */
async function flowWashRetryStage2() {
  const w = flowUi.wash;
  if (!w.plan || w.busy) return;
  return flowChooseAngle({dataset: {index: String(w.chosen || 0)}});
}

function flowStage2FailCard(w) {
  const next = w.errorNext || {label: '重试成稿', action: 'retry'};
  const login = next.action === 'login';
  const showRetry = w.errorRetry !== false || !['settings', 'upgrade'].includes(next.action);
  return '<div class="wash-note warn" id="wash-stage2-error"><div><strong>正文读到了，方向也选好了，但成稿这一步没通过</strong>' +
    '<p>' + esc(w.error) + '</p>' +
    '<div class="row">' +
    (login ? '<button class="primary small-button" type="button" data-flow-action="flow-login">去登录</button>'
           : (showRetry ? '<button class="primary small-button" type="button" data-flow-action="wash-retry-stage2">' + esc(next.action === 'retry' ? next.label || '重试成稿' : '重试成稿') + '</button>' : '')) +
    (next.action === 'settings' ? '<a class="ghost small-button" href="#/settings">' + esc(next.label) + '</a>' : '') +
    (next.action === 'upgrade' ? '<button class="primary small-button" type="button" data-flow-action="flow-upgrade">' + esc(next.label) + '</button>' : '') +
    '<details><summary class="small muted">查看详情</summary><pre class="raw-markdown">' + esc(w.errorDetail || w.error) + '</pre></details>' +
    '</div>' +
    '<p class="small muted">' + (login ? '登录后页面会刷新，需要重新贴一次链接。' : '不想重试，也可以在下面换一个方向。') + '</p></div></div>';
}

async function flowAction(name, el) {
  const data = flowData();
  if (name === 'flow-asset-retry') {
    // 整页重载：登录态重新检查一次，服务端也重新取一次题库资产。
    location.reload();
    return;
  }
  if (name === 'flow-login') {
    return flowOpenLogin();
  }
  if (name === 'flow-defer') {
    /* 稍后再说：记下来（skipped 让路由不再把人硬拉回答题页，deferred 用于首屏提醒一次），
       直接进工作台。之后从工作台点「补几句你的说法」仍可回到这里接着答。 */
    await flowSave({intro_seen: true, skipped: true, deferred: true, stage: 'intro'});
    if (location.hash === '#/create') return route();
    location.hash = '#/create';
    return;
  }
  if (name === 'flow-prev') {
    await flowSave({current: Math.max(1, (data.current || 1) - 1), stage: 'question'});
    return route();
  }
  if (name === 'flow-skip-q') {
    const total = (data.questions || []).length || 12;
    if ((data.current || 1) >= total) {
      return flowFinalize();
    } else {
      await flowSave({current: (data.current || 1) + 1, stage: 'question'});
    }
    return route();
  }
  if (name === 'flow-reopen') {
    await flowSave({completed: false, stage: 'question', current: 1});
    return route();
  }
  if (name === 'flow-refinalize') {
    return flowFinalize();
  }
  if (name === 'flow-retry-finalize') {
    return flowFinalize();
  }
  if (name === 'wash-choose') {
    return flowChooseAngle(el);
  }
  if (name === 'wash-retry-stage1') {
    return flowWashRetryStage1();
  }
  if (name === 'wash-retry-stage2') {
    return flowWashRetryStage2();
  }
  if (name === 'flow-upgrade') {
    // 「去开通」：复用 app.js 现成的 member-renew（打开会员中心并停在续费区）。
    const m = document.createElement('button'); m.dataset.action = 'member-renew'; m.hidden = true;
    document.body.appendChild(m); m.click(); m.remove();
    return;
  }
  if (name === 'talk-long') {
    return flowLongToggle(el);
  }
  if (name === 'wash-open-plan') {
    const plan = await api('/wash/plan/' + encodeURIComponent(el.dataset.id));
    flowUi.wash = {stage: 'plan', plan, busy: false, error: '', progress: ''};
    return flowPaintStage();
  }
}

/* 选了「稍后再说」的人：工作台/立轴页给一面镜子和最小引导（t28 · P6），提醒过就不再出现。
   镜子只用他自己已有的积累（成长总览的 by_category），空库就说「先记一条」，不空转、不催。 */
async function flowMirrorLine() {
  try {
    const d = await api('/growth/overview');
    const stats = d.stats || {};
    const by = stats.by_category || {};
    const total = Number(stats.fragments) || 0;
    const rows = Object.entries(by).filter(([, n]) => Number(n) > 0).sort((a, b) => b[1] - a[1]);
    if (!rows.length) return '你这里还什么都没记。先记一条你昨天说过的话，第一条就从此刻开始。';
    const top = rows[0];
    const share = total ? Math.round(top[1] * 100 / total) : 0;
    const empty = Object.entries(by).filter(([, n]) => !Number(n)).map(([k]) => k);
    return '你的积累集中在「' + top[0] + '」（' + top[1] + ' 条' + (share ? '，占 ' + share + '%' : '') + '）' +
      (empty.length ? '；一直没碰的是「' + empty.slice(0, 2).join('」「') + '」' : '') + '。';
  } catch (e) { return ''; }
}

async function flowDeferHint() {
  if (flowUi.hintDone) return;
  const data = flowData();
  if (!data.deferred || data.completed || data.defer_hint_at) return;
  const hash = String(location.hash || '');
  if (!hash.startsWith('#/create') && !hash.startsWith('#/restart')) return;
  const main = fq('#main');
  if (!main || fq('#flow-defer-hint')) return;
  const mirror = await flowMirrorLine();
  if (!document.body.contains(main) || fq('#flow-defer-hint')) return;
  const box = document.createElement('div');
  box.id = 'flow-defer-hint';
  box.className = 'flow-hint';
  const title = document.createElement('strong');
  title.textContent = '你跳过了建档——先开工也没问题。';
  box.append(title);
  if (mirror) {
    const mirrorEl = document.createElement('p');
    mirrorEl.className = 'small muted';
    mirrorEl.textContent = '先看一眼你自己的积累：' + mirror;
    box.append(mirrorEl);
  }
  const note = document.createElement('p');
  note.className = 'small muted';
  note.textContent = '想更像你的稿子：补几句你的说法（随时可补），或先把主线立起来——目标可以是赚钱、涨粉、出片数，命题可选。';
  box.append(note);
  const row = document.createElement('div');
  row.className = 'flow-nav';
  const more = document.createElement('a');
  more.className = 'link';
  more.href = '#/start';
  more.textContent = '补几句我的说法 →';
  const axis = document.createElement('a');
  axis.className = 'link';
  axis.href = '#/restart';
  axis.textContent = '先立我的主线 →';
  const write = document.createElement('button');
  write.type = 'button';
  write.className = 'ghost small-button';
  write.dataset.action = 'create';
  write.textContent = '写一句就开工';
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'flow-link';
  close.textContent = '知道了';
  close.addEventListener('click', () => box.remove());
  row.append(more, axis, write);
  box.append(row, close);
  main.insertBefore(box, main.firstChild);
  flowUi.hintDone = true;
  flowSave({defer_hint: true}, {silent: true}).catch(() => {});
}

/* ---------- 登录引导（t43）：打开登录门禁 / 其它入口的「登录后可用」条 ---------- */
function flowOpenLogin() {
  const gate = document.getElementById('auth-gate');
  if (!gate) {
    toast('请在右上角「退出」后重新登录，或在设置里登录。');
    return;
  }
  gate.hidden = false;
  gate.classList.remove('is-checking');
  const user = document.getElementById('auth-user');
  if (user && !user.disabled) user.focus();
}

/* 创作台/对标库/合规等入口：资产（题库/模板）没就绪且未登录时给一条引导，不崩不空转。 */
function flowAssetsBar() {
  const data = flowData();
  if (!data.need_login) return;
  const hash = String(location.hash || '');
  if (!hash || hash.startsWith('#/start') || hash.startsWith('#/restart')) return;
  const main = fq('#main');
  if (!main || fq('#flow-assets-bar')) return;
  const box = document.createElement('div');
  box.id = 'flow-assets-bar';
  box.className = 'flow-hint';
  const text = document.createElement('span');
  text.textContent = '登录后可用：题库、模板和合规复核在云端按账号下发。';
  const go = document.createElement('button');
  go.type = 'button';
  go.className = 'link-button';
  go.textContent = '去登录 →';
  go.addEventListener('click', () => flowOpenLogin());
  box.append(text, document.createTextNode(' '), go);
  main.insertBefore(box, main.firstChild);
}

/* ---------- 路由挂载点 ---------- */
function flowAfterRender() {
  if (flowSavePending.length) flowSaveWarning();
  flowVoiceInit();
  flowPaintStage();
  flowRenderHistory();
  flowDeferHint();
  flowAssetsBar();
  if (!fq('#flow-q-form')) {
    flowStopTalk();
    if (flowMediaRec && flowMediaRec.state !== 'inactive') {
      try { flowMediaRec.stop(); } catch (e) { /* 已结束 */ }
    }
  }
}

/* ---------- 事件：捕获阶段先处理，避免和旧提交逻辑互相打架 ---------- */
document.addEventListener('submit', async event => {
  const form = event.target;
  if (!FLOW_SUBMIT_IDS.includes(form.id)) return;
  event.preventDefault();
  event.stopPropagation();
  const button = form.querySelector('[type="submit"]');
  if (button) button.disabled = true;
  try { await flowHandleSubmit(form); }
  catch (e) { toast(e.message); }
  finally { if (button) button.disabled = false; }
}, true);

document.addEventListener('click', async event => {
  const el = event.target.closest('[data-flow-action]');
  if (!el || el.disabled) return;
  const name = el.dataset.flowAction;
  if (!FLOW_CLICK_ACTIONS.includes(name) && name !== 'talk') return;
  if (name === 'talk') return;
  event.preventDefault();
  event.stopPropagation();
  try { await flowAction(name, el); }
  catch (e) { toast(e.message); }
}, true);

document.addEventListener('pointerdown', event => {
  const btn = event.target.closest('[data-flow-action="talk"]');
  if (!btn || btn.disabled) return;
  event.preventDefault();
  btn.disabled = true;
  flowStartTalk(btn);
  setTimeout(() => { btn.disabled = false; }, 60);
}, true);
document.addEventListener('pointerup', event => {
  if (event.target.closest('[data-flow-action="talk"]') || flowRec) flowStopTalk();
}, true);
document.addEventListener('pointercancel', () => flowStopTalk(), true);

document.addEventListener('input', event => {
  const form = event.target.closest('#flow-q-form');
  if (!form) return;
  clearTimeout(flowSaveTimer);
  flowSaveTimer = setTimeout(() => {
    const qid = form.querySelector('[name="qid"]');
    const answer = form.querySelector('[name="answer"]');
    if (!qid || !answer) return;
    flowSave({answers: {[qid.value]: answer.value}}).catch(() => {});
  }, 1200);
}, true);

// 光标离开输入框就先存一次：中途关页面/退出，回来能接着答，不用重说。
document.addEventListener('focusout', event => {
  const form = event.target.closest && event.target.closest('#flow-q-form');
  if (!form) return;
  const to = event.relatedTarget;
  // 点本页的「下一题/跳过/上一题」时由提交逻辑保存这一题，不再重复发一次。
  if (to && to.closest && to.closest('#flow-q-form')) return;
  const qid = form.querySelector('[name="qid"]');
  const answer = form.querySelector('[name="answer"]');
  if (!qid || !answer) return;
  clearTimeout(flowSaveTimer);
  flowSave({answers: {[qid.value]: answer.value}}).catch(() => {});
}, true);
