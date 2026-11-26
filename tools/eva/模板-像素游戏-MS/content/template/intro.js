(function () {
  var P = window.USER_PARAMS || {};
  var dur = +(P.duration) || 15.033;
  var sc = dur / 15.033;
  var T = { hi:.1, meet:.9, oss:1.9, cine:3.87, fut:4.73, soc:5.87, tpl:7.73,
            pick:8.97, apply:9.87, ai:10.67, less:12.1, more:12.9, tryit:13.77 };
  for (var k in T) T[k] = +(T[k]*sc).toFixed(2);
  var ln = (P.lines||[]).map(function(l){return l && l.speak;}).filter(Boolean);
  // 展示位优先用逐字稿原句 / 四段文案槽，不编造
  var pool = [P.s1_h,P.s2_g,P.s3_g1,P.s3_g2,P.s3_g3,P.s4_h].concat(ln)
             .filter(function(x){return x && String(x).trim();});
  function take(n, dft){ var a=pool.slice(0,n); while(a.length<n) a.push(dft[a.length]||''); return a; }
  window.IN = {
    T: T, END: dur,
    B: [0, +(3.87*sc).toFixed(2), +(7.73*sc).toFixed(2), +(12.07*sc).toFixed(2), dur],
    setup: function (C) {
      C.src0 = 0; C.end = dur;
      C.evaDir = (P.host && P.host.dir) || 'media/eva';
      /* t24 人物居中：素材 540x960 实测肤色质心 (.354,.384)（diag/人物居中优化-20261118）。
         原 (.51,.25) 致窗内人物偏左下（基线 dx=-.113,dy=+.074）；y 按 engine ty=lerp(.44,.52,rnd) 方/圆窗折中取 .364。 */
      C.face = { x:.354, y:.364, h:.42 };
      C.srcW = 540; C.srcH = 960;
    },
    SHOTS: (P.shots && P.shots.length) ? P.shots : [],
    STYLES: take(4, ['CINEMATIC','FUTURISTIC TECH','SOCIAL MEDIA','MINIMALIST']),
    HEADS: take(6, ln.length?ln:['内容创作']),
    SUBS: take(6, ['']),
    LAYOUTS: [], SECT: 0, SEED: 1, BRAND: (P.brand||''), ILLUS: []
  };
  // sample5/t24 用全局 S5 而非 IN：同样把人像帧目录 + 片长喂进去
  window.S5 = {
    END: dur, FRAMES: Math.round(dur*30),
    setup: function (C) {
      C.src0 = 0; C.end = dur;
      C.evaDir = (P.host && P.host.dir) || 'media/eva';
      C.face = { x:.5, y:.2, h:.36 };
    },
    cut: function (t) {
      var d = (P.host && P.host.dir) || 'media/eva';
      var n = Math.max(0, Math.min(Math.round(dur*30)-1, Math.round(t*30)));
      return d + '/f_' + String(n).padStart(5,'0') + '.jpg';
    }
  };
})();

/* 逐字稿级中文化运行时 · MS 开源/像素模板
 * 在 EV.start() 构建 DOM 后，把「英文 demo 大字」文本节点替换为逐字稿原句。
 * 铁律：上屏中文必须是逐字稿归一化后的子串（verbatim），否则置空（宁少不假），
 *       绝不翻译、绝不编造。结果落 window.__CN_* 供回归测试读取。
 */
(function () {
  var P = window.USER_PARAMS || {};
  var dur = +(P.duration) || 15.033;

  // 1) 逐字稿四段桶：按句子中点归段（与工作台 eva_slots 同口径）
  var raw = P.lines || [];
  var ln = raw.map(function (l) { return String((l && (l.speak || l.text)) || '').trim(); }).filter(Boolean);
  var segs = [[], [], [], []];
  raw.forEach(function (l) {
    var s = +(l && l.start) || 0, e = +(l && l.end) || 0, mid = (s + e) / 2;
    var q = mid < dur * .25 ? 0 : mid < dur * .5 ? 1 : mid < dur * .8 ? 2 : 3;
    var t = String((l && (l.speak || l.text)) || '').trim();
    if (t) segs[q].push(t);
  });
  var MODE = 'verbatim';
  if (!ln.length) {
    // 工作台未透传 lines 时：退回 slots 槽（同样是逐字稿确定性抽取，不编造）
    var fb = [P.s1_h, P.s1_n, P.s2_g, P.s3_g1, P.s3_g2, P.s3_g3, P.s4_h, P.s4_n]
      .filter(Boolean).map(String);
    ln = fb;
    if (fb.length) segs = [fb.slice(0, 2), fb.slice(2, 3), fb.slice(3, 6), fb.slice(6, 8)];
    MODE = 'slots-fallback';
  }

  // 2) 归一化 + verbatim 守卫
  function norm(s) { return String(s || '').replace(/[^一-龥a-zA-Z0-9]/g, '').toLowerCase(); }
  var CORPUS = norm(ln.join(''));
  function verbatim(t) { var n = norm(t); return n.length > 0 && CORPUS.indexOf(n) >= 0; }
  function clip(t, cap) {
    t = String(t || '').trim(); var a = Array.from(t);
    if (a.length <= cap) return t;
    var out = '', last = 0;
    for (var i = 0; i < a.length && i < cap; i++) {
      out += a[i]; if (/[，。！？、；,.!?;]/.test(a[i])) last = out.length;
    }
    return last >= 2 ? out.slice(0, last) : out;
  }

  // 3) 英文 demo 判定 + 语义角色分桶（TreeWalker 天然跳过样式/class/属性）
  function isEN(t) {
    t = String(t || '').trim(); if (t.length < 2) return false;
    var chars = t.replace(/\s/g, ''); if (!chars.length) return false;
    var letters = (t.match(/[A-Za-z]/g) || []).length;
    var cjk = (t.match(/[一-龥]/g) || []).length;
    return cjk === 0 && letters / chars.length > 0.5;
  }
  function roleOf(s) {
    s = s.toUpperCase();
    if (/LESS|MORE|TRY|TODAY|TURN|RESULTS|CREATING|SIMPLE|TIME|→|★/.test(s)) return 3;
    if (/CHOOSE|STYLE|CINEMATIC|FUTURISTIC|SOCIAL|MINIMAL|PICK|APPLY|TEMPLATE|READY|STEP|IDEA|PLAN|BUILD|EDIT|AI\b/.test(s)) return 2;
    if (/OPEN.?SOURCE|VIDEO.?EDITING|SKILL|EDITION|DASHBOARD|KEYNOTE|LICENSE|MIT/.test(s)) return 1;
    return 0;
  }

  var roleCount = [0, 0, 0, 0], REPLACED = [], VIOL = [];
  var STATS = { mode: MODE, total: 0, replaced: 0, kept: 0, cleared: 0, violations: 0, frames: 0 };
  function pick(en) {
    var r = roleOf(en), bucket = segs[r];
    if (!bucket || !bucket.length) {
      for (var i = 0; i < 4; i++) { if (segs[i] && segs[i].length) { bucket = segs[i]; break; } }
    }
    if (!bucket || !bucket.length) return '';
    var cap = Math.max(2, Math.min(8, Math.round(en.replace(/[^\x00-\x7F]/g, '').length * 0.45)));
    var s = bucket[roleCount[r] % bucket.length]; roleCount[r]++;
    var t = clip(s, cap);
    return verbatim(t) ? t : '';
  }

  // 4) DOM 文本节点遍历替换（幂等：中文节点 isEN=false 自动跳过）
  function localizeOnce() {
    var root = document.getElementById('stage') || document.body;
    if (!root) return;
    var tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
    var nodes = [], n; while ((n = tw.nextNode())) nodes.push(n);
    if (!nodes.length) return;
    var rep = 0, kept = 0, cleared = 0, tot = 0;
    nodes.forEach(function (node) {
      var v = node.nodeValue || '';
      if (!isEN(v)) { kept++; return; }
      tot++;
      var t = pick(v);
      if (t) { node.nodeValue = t; rep++; REPLACED.push(t); }
      else { node.nodeValue = ''; cleared++; VIOL.push(v.slice(0, 40)); }
    });
    STATS.frames++;
    if (tot > 0 || STATS.total === 0) {
      STATS.total = tot; STATS.replaced = rep; STATS.kept = kept;
      STATS.cleared = cleared; STATS.violations = VIOL.length;
      STATS.samples = VIOL.slice(0, 8);
    }
    window.__CN_STATS = STATS; window.__CN_MODE = MODE; window.__CN_REPLACED = REPLACED;
  }

  // 5) hook：EV.start 构建后跑一次；renderAt 每帧幂等补漏（build 若懒加载）
  function hook() { try { localizeOnce(); } catch (e) { window.__CN_ERR = String(e); } }
  try {
    var _s = EV.start;
    EV.start = function () {
      var r = _s.apply(this, arguments);
      hook();
      if (typeof window.renderAt === 'function' && !window.__CN_HOOKED) {
        window.__CN_HOOKED = true;
        var _r = window.renderAt;
        window.renderAt = function () { var x = _r.apply(this, arguments); hook(); return x; };
      }
      return r;
    };
  } catch (e) { window.__CN_ERR = String(e); }
  window.__CN = { stats: STATS, replaced: REPLACED, verbatim: verbatim, pick: pick, corpus: CORPUS, mode: MODE };
  [0, 60, 180, 450].forEach(function (ms) { setTimeout(hook, ms); });
})();
