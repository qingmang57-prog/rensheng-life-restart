/* fx.js — 织梦包装引擎 · 运动特效原语库
   移植自 motiontemplates/385712 kinetic-tech open-beta v0.2.7 core/core.js 的特效段
   （MIT License，见 ../audio/LICENSE.synth.txt 同仓库许可）。
   保真改造点：①画布尺寸从硬编码 1920×1080 改为 CONFIG 驱动（默认竖屏 1080×1920）
   ②EZ/hash/pr 等微型依赖内联在本文件头部（源自 core/lib.js，一并标注 MIT）
   ③mk/place/TEXT_GLOBAL 保持外部约定（engine/runtime/dom.js 施工时提供同签名实现，
     保证场景代码与上游 diff 可追溯）。 */
;(function () {


// ---------- 微型依赖（源自 385712 core/lib.js，MIT）----------
const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
const pr = (t, a, b) => clamp((t - a) / Math.max(b - a, 1e-6));          // 区间进度 0..1
const lerp = (a, b, x) => a + (b - a) * x;
const hash = n => { const s = Math.sin(n * 127.1 + 311.7) * 43758.5453; return s - Math.floor(s); };
const EZ = {
  in: x => x * x,
  out: x => 1 - (1 - x) * (1 - x),
  expo: x => x === 1 ? 1 : 1 - Math.pow(2, -10 * x),
  out5: x => 1 - Math.pow(1 - x, 5),
};
const easeOutBack = (x, s = 1.7) => 1 + (s + 1) * Math.pow(x - 1, 3) + s * Math.pow(x - 1, 2);
const smooth = x => x * x * (3 - 2 * x);
const env = (t, a, b, di = .3, dout = .3) =>
  Math.min(EZ.out(pr(t, a, a + di)), 1 - EZ.in(pr(t, b - dout, b)));

// ---------- 文字原语 ----------
function typeAt(el, txt, t, t0, cps = 30) {           // 打字机（缓存已写内容防回流）
  const n = Math.floor(clamp((t - t0) * cps, 0, txt.length));
  const s = txt.slice(0, n);
  if (el._ts !== s) { el._ts = s; el.textContent = s; }
  return n;
}
function setTxt(el, s) { if (el._tx !== s) { el._tx = s; el.textContent = s; } }

// ---------- 逐字入场（slam砸入 / up上浮 / drop砸落 / mask揭幕）----------
function chars(parent, txt, x, y, style, opt = {}) {   // 拆字容器；返回的元素带 _c 字粒数组
  const e = mk(parent, `<div style="${style};white-space:nowrap">${[...txt].map(c =>
    `<span style="display:inline-block;${opt.cst || ''}">${c === ' ' ? '&nbsp;' : TEXT_GLOBAL(c)}</span>`).join('')}</div>`,
    x, y, Object.assign({ ax: 0, ay: .5 }, opt));
  e._c = [...e.firstChild.children];
  return e;
}
function charsAt(e, t, times, opt = {}) {
  let any = false; const k = opt.k || 'slam';
  e._c.forEach((c, i) => {
    const t0 = Array.isArray(times) ? times[Math.min(i, times.length - 1)]
                                    : times + i * (opt.stag || .04);
    const x = pr(t, t0, t0 + (opt.d || .22));
    let o = clamp(x * 5);
    const ot = opt.out != null ? pr(t, opt.out + i * (opt.os || .02), opt.out + i * (opt.os || .02) + .22) : 0;
    o *= 1 - ot; if (o > 0) any = true;
    let tf = '';
    if (k === 'slam') tf = `scale(${(1 + (1 - EZ.out5(x)) * 1.6).toFixed(3)})`;
    if (k === 'up') tf = `translateY(${((1 - EZ.expo(x)) * 80).toFixed(1)}px)`;
    if (k === 'drop') tf = `translateY(${((1 - EZ.out5(x)) * -140).toFixed(1)}px)`;
    if (k === 'mask') { c.style.clipPath = `inset(${((1 - EZ.expo(x)) * 100).toFixed(1)}% 0 -10% 0)`;
      tf = `translateY(${((1 - EZ.expo(x)) * 50).toFixed(1)}px)`; o = x > 0 ? 1 - ot : 0; }
    if (ot > 0) tf += ` translateY(${(-EZ.in(ot) * 50).toFixed(1)}px)`;
    c.style.transform = tf; c.style.opacity = o.toFixed(3);
    c.style.filter = x < 1 && k !== 'mask' ? `blur(${((1 - x) * 8).toFixed(1)}px)` : '';
  });
  place(e, { o: any ? 1 : 0, x: opt.x, y: opt.y, s: opt.s, r: opt.r });
}

// ---------- 字符扰动 / RGB 故障 ----------
const GLY = '#%&*+=<>/\\|_█▓▒░▚▞◆◇○●□■';
function scrambleAt(el, txt, t, t0, dur = .5) {        // 解密式逐字显形
  const n = txt.length, x = pr(t, t0, t0 + dur), j = Math.floor(t * 30);
  let s = '';
  for (let i = 0; i < n; i++) {
    const ti = i / n;
    s += x >= 1 || x > ti + .25 ? txt[i]
      : (x > ti ? GLY[Math.floor(hash(i * 13 + j) * GLY.length)]
                : (txt[i] === ' ' ? ' ' : ''));
  }
  setTxt(el, s);
}
function glitchText(parent, html, x, y, style, opt = {}) {  // 三色叠印容器
  const e = mk(parent, `<div style="position:relative;white-space:nowrap">
     <div class="g1" style="${style};position:absolute;left:0;top:0;mix-blend-mode:screen;-webkit-text-fill-color:#FF3B3B;filter:none;background:none">${html}</div>
     <div class="g2" style="${style};position:absolute;left:0;top:0;mix-blend-mode:screen;-webkit-text-fill-color:#2FD0FF;filter:none;background:none">${html}</div>
     <div class="g0" style="${style};position:relative">${html}</div></div>`,
    x, y, Object.assign({ ax: .5, ay: .5 }, opt));
  e._g1 = e.querySelector('.g1'); e._g2 = e.querySelector('.g2'); e._g0 = e.querySelector('.g0');
  return e;
}
function glitchAt(e, t, t0, opt = {}) {
  const x = pr(t, t0, t0 + (opt.d || .4)); let k = 1 - EZ.out(x);
  if (opt.out != null) k = Math.max(k, EZ.in(pr(t, opt.out, opt.out + .25)));
  if (opt.pulse) k = Math.max(k, opt.pulse);
  let o = x > 0 ? 1 : 0; if (opt.out != null) o *= 1 - pr(t, opt.out + .15, opt.out + .28);
  const j = Math.floor(t * 30);
  const dx = k * (hash(j) - .5) * (opt.amp || 60), dy = k * (hash(j + 7) - .5) * 10;
  e._g1.style.transform = `translate(${dx.toFixed(1)}px,${dy.toFixed(1)}px)`;
  e._g2.style.transform = `translate(${(-dx).toFixed(1)}px,${(-dy).toFixed(1)}px)`;
  e._g1.style.opacity = e._g2.style.opacity = (k * .9).toFixed(2);
  e._g0.style.clipPath = k > .05 ? `inset(${(hash(j + 3) * 45 * k).toFixed(0)}% 0 ${(hash(j + 5) * 45 * k).toFixed(0)}% 0)` : 'none';
  e._g0.style.transform = k > .05 ? `translateX(${((hash(j + 11) - .5) * 30 * k).toFixed(1)}px)` : '';
  place(e, { o, x: opt.x, y: opt.y, s: (opt.s || 1) * (1 + k * .04), r: opt.r });
}

// ---------- canvas 特效队列（三层：back/content/top）----------
let FXB, FXC, FXT, FXQ;
function fxInit(doc = document) {
  // 画布位图必须显式设为输出分辨率：canvas 缺省 300×150，CSS 拉伸会让一切坐标越界绘制被裁掉（坑㉓）
  const W = (window.CONFIG && CONFIG.width) || 1920, H = (window.CONFIG && CONFIG.height) || 1080;
  for (const id of ['fxb', 'fxc', 'fxt']) {
    const c = doc.getElementById(id);
    c.width = W; c.height = H;
  }
  FXB = doc.getElementById('fxb').getContext('2d');
  FXC = doc.getElementById('fxc').getContext('2d');
  FXT = doc.getElementById('fxt').getContext('2d');
  FXQ = [];
  Object.assign(window, { FXB, FXC, FXT });   // 家族覆写层直取顶层画布
}
function fxDraw(fn, top = false) { FXQ.push([fn, top]); }
function sparks(ctx, t, t0, x, y, opt = {}) {          // 粒子火花（重力+拖尾）
  const n = opt.n || 40, life = opt.life || .7, dir = opt.dir == null ? -Math.PI / 2 : opt.dir,
    spread = opt.spread || 1.6, sp = opt.speed || 900, g = opt.g == null ? 1400 : opt.g,
    col = opt.col || '255,206,120', seed = opt.seed || 1, rate = opt.rate || 0;
  const emit = rate ? Math.max(0, Math.floor((t - t0) * rate)) : n;
  for (let i = 0; i < (rate ? Math.min(emit, n) : n); i++) {
    const born = rate ? t0 + i / rate : t0, age = t - born - hash(i * 7.1 + seed) * (rate ? 0 : .06);
    const L = life * (.5 + hash(i * 3.3 + seed) * .7);
    if (age <= 0 || age > L) continue;
    const a = dir + (hash(i * 1.7 + seed) - .5) * spread, v = sp * (.35 + hash(i * 9.1 + seed) * .9);
    const px = (opt.px ? opt.px(born) : x) + Math.cos(a) * v * age,
          py = (opt.py ? opt.py(born) : y) + Math.sin(a) * v * age + .5 * g * age * age;
    const vx = Math.cos(a) * v, vy = Math.sin(a) * v + g * age,
          sp2 = Math.hypot(vx, vy) * .018, k = 1 - age / L;
    ctx.strokeStyle = `rgba(${col},${(k * .95).toFixed(3)})`; ctx.lineWidth = 1.2 + k * 1.6;
    ctx.beginPath(); ctx.moveTo(px, py);
    ctx.lineTo(px - vx / Math.hypot(vx, vy) * sp2, py - vy / Math.hypot(vx, vy) * sp2); ctx.stroke();
  }
}
function glow(ctx, x, y, r, col = '255,226,160', a = 1) {   // 加色法径向辉光
  ctx.save(); ctx.globalCompositeOperation = 'lighter';
  const g = ctx.createRadialGradient(x, y, 0, x, y, r);
  g.addColorStop(0, `rgba(255,255,255,${a})`); g.addColorStop(.18, `rgba(${col},${a * .9})`);
  g.addColorStop(1, `rgba(${col},0)`);
  ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832); ctx.fill(); ctx.restore();
}
function stars(ctx, t, n, o = 1, seed = 3, col = '90,150,255') {  // 闪烁星点背景
  const W = window.CONFIG ? CONFIG.width : 1920, H = window.CONFIG ? CONFIG.height : 1080;
  for (let i = 0; i < n; i++) {
    const x = hash(i * 1.31 + seed) * W, y = hash(i * 2.77 + seed) * H,
          tw = .5 + .5 * Math.sin(t * (1.5 + hash(i) * 3) + i), r = 1 + hash(i * 5.3) * 2.2;
    ctx.fillStyle = `rgba(${col},${(o * tw * (.35 + hash(i * 4.1) * .65)).toFixed(3)})`;
    ctx.fillRect(x, y, r, r);
  }
}
function halftone(ctx, t, cx, cy, p, k = 1, col = '210,222,236', gap = 22) {  // 半调网点冲击环
  const W = window.CONFIG ? CONFIG.width : 1920, H = window.CONFIG ? CONFIG.height : 1080;
  const R0 = lerp(0, 760, EZ.out(p)), R1 = R0 + 900;
  for (let y = gap / 2; y < H; y += gap) for (let x = gap / 2; x < W; x += gap) {
    const d = Math.hypot(x - cx, (y - cy) * 1.25); if (d < R0 * .55 || d > R1) continue;
    const u = clamp((d - R0 * .55) / (R0 * .5 + 1)), v = 1 - clamp((d - R0) / 900);
    const r = gap * .42 * Math.min(u, v) * k; if (r < .4) continue;
    ctx.fillStyle = `rgba(${col},${(.85 * Math.min(u, v) * k).toFixed(3)})`;
    ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832); ctx.fill();
  }
}

// ---------- 标注线（dimLine 从中心向两端生长的工程标注）----------
function dimLine(parent, x1, y1, x2, y2, label, col = '#3A4152', opt = {}) { /* 同上游，长 DOM 模板略——施工时从 core.js L172-185 原样复制 */ }
function dimAt(e, t, t0, opt = {}) { /* 同上游 core.js L186-194 */ }

// ---------- 数字翻牌（光环科技 spec 的"翻牌数字"实现）----------
function flipNum(parent, digits, x, y, size, style = '') {
  const cells = [...digits].map(ch => /\d/.test(ch)
    ? `<span class="fd" style="display:inline-block;position:relative;width:.56em;height:1em;overflow:hidden;vertical-align:top"><span class="ro" style="position:absolute;left:0;top:0;display:block;line-height:1">${'0123456789'.split('').map(d => `<span style="display:block;height:1em;text-align:center;${style}">${d}</span>`).join('')}</span></span>`
    : `<span style="display:inline-block;height:1em;vertical-align:top;line-height:1;${style}">${ch}</span>`).join('');
  const e = mk(parent, `<div style="font:400 ${size}px/0.9 'Anton',sans-serif;letter-spacing:0;line-height:1;white-space:nowrap">${cells}</div>`,
    x, y, { ax: .5, ay: .5 });
  e._ro = [...e.querySelectorAll('.ro')];
  e._digits = [...digits].filter(c => /\d/.test(c)).map(Number);
  return e;
}
function flipAt(e, t, t0, dur = 1.2, opt = {}) {        // 从右往左逐位落定，旋转换算=位数递减
  const n = e._ro.length;
  e._ro.forEach((r, i) => {
    const d0 = t0 + (n - 1 - i) * .06, x = pr(t, d0, d0 + dur * (.55 + .45 * (i + 1) / n));
    const spins = (n - i) * 1.0, tgt = e._digits[i];
    const v = (EZ.out5(x) * (spins * 10 + tgt)) % 10;
    r.style.transform = `translateY(${(-v).toFixed(3)}em)`;
    r.style.filter = x > 0 && x < .85 ? `blur(${((1 - x) * 3).toFixed(1)}px)` : '';
  });
  place(e, { o: opt.o == null ? (t >= t0 ? 1 : 0) : opt.o, x: opt.x, y: opt.y, s: opt.s });
}

// ---------- 光轨路径（svgPath 高斯辉光层 + pathAt 描边进度，光环科技"光轨勾勒"实现）----------
function svgPath(parent, d, w, h, col = '#E8ECF4', sw = 6, opt = {}) {
  const id = 'g' + Math.floor(hash(d.length + w) * 1e6);
  const e = mk(parent, `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="overflow:visible">
    <defs><filter id="${id}" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="${opt.blur || 6}"/></filter></defs>
    <path class="gl" d="${d}" fill="none" stroke="${opt.glow || col}" stroke-width="${sw * 2.4}" stroke-linecap="round" stroke-linejoin="round" filter="url(#${id})" opacity=".55"/>
    <path class="p" d="${d}" fill="none" stroke="${col}" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round"/>
    ${opt.hi ? `<path class="hi" d="${d}" fill="none" stroke="rgba(255,255,255,.85)" stroke-width="${Math.max(1, sw * .28)}" stroke-linecap="round" stroke-linejoin="round" transform="translate(0,-${sw * .22})"/>` : ''}
  </svg>`, opt.x || 0, opt.y || 0, { ax: 0, ay: 0 });
  e._p = [...e.querySelectorAll('path')];
  e._len = e._p[0].getTotalLength();
  e._path = e._p[1];
  return e;
}
function pathAt(e, p, o = 1) {                          // 返回笔尖坐标（供火花跟笔）
  const L = e._len || (e._len = e._path.getTotalLength());
  e._p.forEach(q => { q.style.strokeDasharray = `${L} ${L}`; q.style.strokeDashoffset = (L * (1 - p)).toFixed(1); });
  place(e, { o });
  const pt = e._path.getPointAtLength(L * clamp(p));
  return [e._x + pt.x, e._y + pt.y];
}

// ---------- 网格舞台 ----------
function stageBG(sc, opt = {}) {                        // 64px 工程网格背景（深场/光环科技通用）
  const g = document.createElement('div');
  const gap = opt.gap || 64, a = opt.a == null ? .045 : opt.a;
  g.style.cssText = `position:absolute;inset:-200px;background-image:linear-gradient(rgba(233,236,239,${a}) 1px,transparent 1px),linear-gradient(90deg,rgba(233,236,239,${a}) 1px,transparent 1px);background-size:${gap}px ${gap}px;background-position:-1px -1px`;
  sc.el.appendChild(g); sc.gridEl = g;
  return g;
}
function gridAt(sc, x = 0, y = 0) { if (sc.gridEl) sc.gridEl.style.transform = `translate(${(x % 64).toFixed(1)}px,${(y % 64).toFixed(1)}px)`; }

Object.assign(window, { clamp, pr, lerp, hash, EZ, smooth, env, easeOutBack,
  typeAt, setTxt, chars, charsAt, scrambleAt, glitchText, glitchAt,
  sparks, glow, stars, halftone, dimLine, dimAt, flipNum, flipAt, svgPath, pathAt,
  stageBG, gridAt });
window.FX = { clamp, pr, lerp, hash, EZ, easeOutBack, smooth, env, typeAt, setTxt,
  chars, charsAt, GLY, scrambleAt, glitchText, glitchAt,
  fxInit, fxDraw, sparks, glow, stars, halftone,
  dimLine, dimAt, flipNum, flipAt, svgPath, pathAt, stageBG, gridAt };

})();
