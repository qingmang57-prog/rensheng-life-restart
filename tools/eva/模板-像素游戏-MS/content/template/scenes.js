/* Pixel Arcade · 像素游戏机 · 1920x1080 · 15s 四节拍
 * 定位：游戏化科普、闯关节奏、步骤清单、 retro 感话题。
 * 画风：深夜蓝底 + 像素点阵 + CRT 扫描线 + 青/黄/品红三色霓虹；硬阴影方块卡 + 8-bit 贴纸。
 * 头像窗运动（借鉴 T24 presenter rig + 引擎 evaKeys）：
 *   B1 方块硬边窗 → B2 morph 成圆形（outBack 弹一下）→ B3 缩小成方块挪到右上 → B4 再 morph 回大圆。
 *   方转圆 = evaKeys 关键帧里 r: 0 → w/2，d 控制过渡时长，引擎自动补间。
 * 贴纸 = canvas 手绘 8-bit 精灵（心/星/币），pop 弹出；血条式进度条 + 闪烁光标。
 * 所有文案槽走 k.cn；装饰只用数字与符号。 */
(function () {
  const C = EV, { el, tf, P, ease, clamp } = C, k = K, I = IN; I.setup(C);
  const INK = '#f4f6ff', CYAN = '#35e6ff', PINK = '#ff4d8d', YEL = '#ffd23f',
        GRN = '#7dff6a', DARK = '#0a0b1e', DIM = '#9aa3c7';
  const MONO = "'Space Mono','DejaVu Sans Mono',monospace";
  const SANS = "'Noto Sans CJK SC','Noto Sans SC',sans-serif";
  const END = C.end, B = [0, END * .25, END * .5, END * .75];
  /* 像素感的关键：运动量化（8-bit 一顿一顿），u 0..1 → n 档阶梯 */
  const qstep = (u, n) => Math.floor(clamp(u) * n) / (n - 1 || 1);

  /* ── 头像窗：方块 → 圆 → 小方块 → 大圆，全程 evaKeys 驱动 ── */
  const SQ1 = { x: 1330, y: 250, w: 470, h: 470, r: 0, s: 1.4 };
  const CI1 = { x: 1352, y: 272, w: 426, h: 426, r: 213, s: 1.5 };
  const SQ2 = { x: 1420, y: 128, w: 372, h: 372, r: 0, s: 1.35 };
  const CI2 = { x: 1340, y: 292, w: 444, h: 444, r: 222, s: 1.5 };
  C.evaKeys = [
    { t: 0, d: 0, L: Object.assign({}, SQ1, { o: 0 }) },
    { t: .08, d: .7, L: SQ1 },
    { t: B[1] - .22, d: .85, ease: 'outBack', L: CI1 },
    { t: B[2] - .22, d: .85, ease: 'outBack', L: SQ2 },
    { t: B[3] - .22, d: .85, ease: 'outBack', L: CI2 },
  ];
  C.ringFn = t => ({ color: INK, width: 6, shadow: .5,
    glow: (t >= B[1] - .2 && t < B[1] + .6) || (t >= B[3] - .2 && t < B[3] + .6) ? 'rgba(53,230,255,.45)' : '' });

  /* ── 8-bit 精灵工厂：字符串地图 + 调色板 → canvas → pixelated img ── */
  function sprite(L, rows, pal, px, x, y, z) {
    const cv = document.createElement('canvas'); cv.width = rows[0].length; cv.height = rows.length;
    const g = cv.getContext('2d');
    rows.forEach((r, j) => { for (let i = 0; i < r.length; i++) if (r[i] !== '.') { g.fillStyle = pal[r[i]]; g.fillRect(i, j, 1, 1); } });
    const im = el(L, 'img', 'abs', { left: x + 'px', top: y + 'px', width: rows[0].length * px + 'px',
      height: rows.length * px + 'px', imageRendering: 'pixelated', transformOrigin: '50% 50%' });
    if (z) im.style.zIndex = z;
    im.src = cv.toDataURL(); return im;
  }
  const HEART = ['.11.11.', '1111111', '1111111', '.11111.', '..111..', '...1...'];
  const STAR = ['...1...', '..111..', '1111111', '.11111.', '..111..', '.11.11.', '1.....1'];
  const COIN = ['.1111.', '12..21', '12..21', '12..21', '12..21', '.1111.'];
  const HEART_P = { '1': PINK }, STAR_P = { '1': YEL }, COIN_P = { '1': YEL, '2': 'rgba(255,210,63,.35)' };

  /* 像素卡：直角 + 白粗边 + 硬投影（不做圆角——圆只留给头像窗 morph 的对比瞬间） */
  const pcard = (L, x, y, w, h, bg) => el(L, 'div', 'abs', { left: x + 'px', top: y + 'px', width: w + 'px',
    height: h + 'px', background: bg, border: '4px solid ' + INK, boxShadow: '10px 10px 0 rgba(10,11,30,.8)' });
  const rise = (t, a, d, dy) => { const u = ease.smooth5(P(t, a, d)); return { y: (dy || 26) * (1 - u), o: u }; };

  /* ── 常驻氛围层：深夜蓝渐变 + 像素点阵 + CRT 扫描线 + 漂浮像素星 + 暗角 ── */
  C.scene({ t0: 0, t1: END + .05, quiet: true,
      bg: 'linear-gradient(165deg,#12142e 0%,#181b3d 52%,#1e2150 100%)',
      build(L) {
        const st = {};
        st.dots = el(L, 'div', 'abs', { left: '0', top: '0', width: '1920px', height: '1080px',
          backgroundImage: 'radial-gradient(rgba(154,163,199,.16) 2px,transparent 2.5px)',
          backgroundSize: '48px 48px', pointerEvents: 'none' });
        st.scan = el(L, 'div', 'abs', { left: '0', top: '0', width: '1920px', height: '1080px',
          backgroundImage: 'repeating-linear-gradient(0deg,rgba(5,6,15,.22) 0 2px,transparent 2px 5px)',
          pointerEvents: 'none', zIndex: 40 });
        st.stars = [[90, 120, 4, CYAN], [420, 80, 3, YEL], [240, 880, 3, PINK], [1140, 950, 4, YEL],
                    [820, 140, 3, CYAN], [1560, 980, 3, GRN], [80, 520, 3, YEL]]
          .map(([x, y, s, c], i) => el(L, 'div', 'abs', { left: x + 'px', top: y + 'px', width: s * 6 + 'px',
            height: s * 6 + 'px', background: c, opacity: .5, transformOrigin: '50% 50%' }));
        st.vig = el(L, 'div', 'abs', { left: '0', top: '0', width: '1920px', height: '1080px',
          background: 'radial-gradient(circle at 50% 46%,rgba(5,6,15,0) 52%,rgba(5,6,15,.55) 100%)', pointerEvents: 'none', zIndex: 41 });
        return st;
      },
      update(lt, st, t) {
        /* 星星闪烁 + 漂移全部量化到 6 档，8-bit 手感 */
        st.stars.forEach((s, i) => { const ph = Math.floor(t * 3 + i * 1.7) % 6;
          tf(s, { o: [.25, .55, .9, .55, .35, .7][ph], y: Math.floor((Math.sin(t * .5 + i * 2.1) * 12 + 12) / 4) * 4 }); });
      } });

  /* ── B1 · 开机：PLAYER 1 角标 + 像素对话框大问题 + 闪烁光标 + 顶部血条 ── */
  C.scene({ t0: 0, t1: B[1], build(L) {
      const st = {};
      st.bar = pcard(L, 118, 96, 560, 34, 'rgba(10,11,30,.6)'); st.bar.style.transformOrigin = '0 50%';
      st.fill = el(st.bar, 'div', 'abs', { left: '0', top: '0', width: '0px', height: '26px', background: GRN });
      st.p1 = el(L, 'div', 'abs', { left: '118px', top: '168px', fontSize: '26px', color: YEL,
        fontFamily: MONO, letterSpacing: '.3em' }, 'PLAYER 1');
      st.box = pcard(L, 110, 232, 1060, 420, 'rgba(10,11,30,.92)');
      st.h = k.cn(st.box, 'WHY IT FEELS LIKE A GAME', INK, 78, 54, 70, { fontFamily: SANS, whiteSpace: 'normal',
        width: '900px', lineHeight: 1.42, fontWeight: 900, textShadow: '6px 6px 0 rgba(53,230,255,.35)' });
      st.cursor = el(st.box, 'div', 'abs', { left: '56px', bottom: '34px', fontSize: '40px', color: CYAN,
        fontFamily: MONO, transformOrigin: '50% 50%' }, '\u25B6');
      st.tag = k.chip(L, 'INSERT COIN', DARK, YEL, 26, 118, 742, { border: '3px solid ' + YEL, padding: '10px 22px',
        fontFamily: MONO, letterSpacing: '.22em', boxShadow: '6px 6px 0 rgba(10,11,30,.8)' });
      st.h1 = sprite(L, HEART, HEART_P, 7, 1220, 130, 42);
      st.c1 = sprite(L, COIN, COIN_P, 7, 1150, 860, 42);
      k.sfx('click', .05, .1); k.sfx('riser', .25, .07, { d: .9 }); k.sfx('pop', 1.1, .12, { f: 660 });
      k.sfx('ding', 2.6, .06, { f: 1180 });
      return st;
    }, update(lt, st, t) {
      /* 血条按节拍进度量化填充（24 档） */
      st.fill.style.width = Math.max(2, 552 * qstep(t / END, 24)) + 'px';
      tf(st.p1, rise(t, .08, .7, 10));
      tf(st.box, { s: .96 + .04 * ease.outBack(P(t, .45, .6), 1.6), o: P(t, .4, .35) });
      tf(st.h, rise(t, .62, 1.05, 26));
      st.cursor.style.opacity = Math.floor(t * 2.5) % 2 ? 1 : .1;
      tf(st.tag, rise(t, 1.5, .8, 14));
      tf(st.h1, { s: ease.outBack(P(t, 1.2, .5), 2), o: P(t, 1.2, .2), r: Math.floor(t) % 2 ? 6 : -6 });
      tf(st.c1, { s: ease.outBack(P(t, 1.8, .5), 2), o: P(t, 1.8, .2), y: -(Math.floor(t * 4) % 2) * 10 });
    } });

  /* ── B2 · ★升级：头像窗 morph 成圆 + LEVEL UP 横幅 + 四关键词像素徽章 ── */
  C.scene({ t0: B[1], t1: B[2], build(L) {
      const st = {}, b = B[1];
      st.lvl = el(L, 'div', 'abs', { left: '118px', top: '110px', fontSize: '54px', color: DARK, background: YEL,
        fontFamily: MONO, fontWeight: 700, letterSpacing: '.14em', padding: '14px 30px', border: '4px solid ' + INK,
        boxShadow: '10px 10px 0 rgba(10,11,30,.8)', transformOrigin: '0 50%' }, 'LEVEL UP');
      st.hd = k.cn(L, 'FOUR MOVES TO LEARN', INK, 62, 118, 260, { fontFamily: SANS, fontWeight: 900,
        whiteSpace: 'normal', width: '820px', lineHeight: 1.4 });
      const spots = [[118, 420], [118, 540], [118, 660], [118, 780]];
      st.tags = I.STYLES.slice(0, 4).map((s, i) => {
        const g = pcard(L, spots[i][0], spots[i][1], 760, 92, i % 2 ? 'rgba(53,230,255,.14)' : 'rgba(255,77,141,.14)');
        g.style.transformOrigin = '0 50%';
        el(g, 'div', 'abs', { left: '26px', top: '18px', fontSize: '30px', color: [CYAN, PINK, YEL, GRN][i], fontFamily: MONO }, '0' + (i + 1));
        k.cn(g, s, INK, 34, 96, 0, { fontFamily: SANS, fontWeight: 700, top: '50%', transform: 'translateY(-50%)' });
        return g;
      });
      st.s1 = sprite(L, STAR, STAR_P, 8, 1180, 180, 42);
      st.s2 = sprite(L, STAR, STAR_P, 6, 1770, 800, 42);
      k.sfx('riser', b - .3, .1, { d: 1.0 }); k.sfx('impact', b + .25, .12, { f: 220 });
      [1.0, 1.45, 1.9, 2.35].forEach((x, i) => k.sfx('pop', b + x, .12, { f: 700 + i * 160 }));
      k.sfx('ding', b + 2.9, .05, { f: 1320 });
      return st;
    }, update(lt, st, t) {
      const b = B[1];
      tf(st.lvl, { s: ease.outBack(P(t, b + .15, .55), 2.2), o: P(t, b + .15, .15) });
      tf(st.hd, rise(t, b + .45, .95, 22));
      st.tags.forEach((g, i) => { const u = ease.outBack(P(t, b + 1.0 + i * .45, .55), 1.8); tf(g, { x: -40 * (1 - u), o: P(t, b + 1.0 + i * .45, .2) }); });
      tf(st.s1, { s: ease.outBack(P(t, b + 2.2, .5), 2), o: P(t, b + 2.2, .2), r: Math.floor(t * 2) % 2 ? 12 : -12 });
      tf(st.s2, { s: ease.outBack(P(t, b + 2.5, .5), 2), o: P(t, b + 2.5, .2) });
    } });

  /* ── B3 · 收集品：三张 SHOTS 像素相框（证据/实例） ── */
  C.scene({ t0: B[2], t1: B[3], build(L) {
      const st = {}, b = B[2];
      st.hd = k.cn(L, 'THE PROOF FRAMES', INK, 54, 118, 128, { fontFamily: SANS, fontWeight: 900, letterSpacing: '.04em' });
      st.line = el(L, 'div', 'abs', { left: '120px', top: '212px', width: '120px', height: '4px', background: PINK, transformOrigin: '0 50%' });
      const POS = [[128, 300, -1.6], [458, 262, 1.3], [788, 304, -1.0]];
      st.frames = POS.map(([x, y, r], i) => {
        const f = el(L, 'div', 'abs', { left: x + 'px', top: y + 'px', width: '318px', height: '452px',
          background: 'rgba(10,11,30,.92)', border: '4px solid ' + INK, boxShadow: '10px 10px 0 rgba(10,11,30,.8)',
          transform: 'rotate(' + r + 'deg)', transformOrigin: '50% 88%' });
        const im = el(f, 'img', 'abs', { left: '8px', top: '8px', width: '302px', height: '436px', objectFit: 'cover' });
        im.src = I.SHOTS[i % I.SHOTS.length];
        el(f, 'div', 'abs', { left: '14px', top: '14px', width: '16px', height: '16px', background: [CYAN, PINK, YEL][i] });
        el(f, 'div', 'abs', { right: '14px', bottom: '12px', fontSize: '22px', color: DIM, fontFamily: MONO }, '0' + (i + 1));
        return f;
      });
      k.sfx('whoosh', b + .05, .09); k.sfx('click', b + 1.0, .08); k.sfx('pop', b + 2.2, .1, { f: 820 });
      return st;
    }, update(lt, st, t) {
      const b = B[2];
      tf(st.hd, rise(t, b + .06, .9, 16));
      tf(st.line, { s: ease.smooth5(P(t, b + .35, .8)), o: P(t, b + .35, .5) });
      st.frames.forEach((f, i) => { const u = ease.outBack(P(t, b + .4 + i * .5, .8), 1.6);
        tf(f, { y: 56 * (1 - u), o: P(t, b + .4 + i * .5, .25), s: .94 + .06 * u }); });
    } });

  /* ── B4 · 通关：金句 + 星星贴纸 + CTA ── */
  C.scene({ t0: B[3], t1: END + .05, build(L) {
      const st = {}, b = B[3];
      st.halo = el(L, 'div', 'abs', { left: '-60px', top: '120px', width: '1040px', height: '840px',
        background: 'radial-gradient(ellipse at 40% 50%,rgba(53,230,255,.20),rgba(53,230,255,0) 66%)', transformOrigin: '40% 50%' });
      st.win = el(L, 'div', 'abs', { left: '118px', top: '150px', fontSize: '34px', color: DARK, background: GRN,
        fontFamily: MONO, fontWeight: 700, letterSpacing: '.2em', padding: '10px 24px', border: '4px solid ' + INK,
        boxShadow: '8px 8px 0 rgba(10,11,30,.8)', transformOrigin: '0 50%' }, 'STAGE CLEAR');
      st.h = k.cn(L, 'LEVEL UP TODAY', INK, 88, 116, 320, { fontFamily: SANS, whiteSpace: 'normal',
        width: '880px', lineHeight: 1.4, fontWeight: 900, textShadow: '6px 6px 0 rgba(255,77,141,.4)' });
      st.s = k.cn(L, 'FOLLOW FOR MORE', DIM, 30, 120, 720, { fontFamily: SANS, letterSpacing: '.24em' });
      st.ar = el(L, 'div', 'abs', { left: '120px', top: 830 + 'px', fontSize: '44px', color: YEL, fontFamily: MONO, transformOrigin: '0 50%' }, '\u25B6');
      st.s1 = sprite(L, STAR, STAR_P, 8, 1210, 150, 42);
      st.s2 = sprite(L, HEART, HEART_P, 7, 1700, 820, 42);
      st.s3 = sprite(L, COIN, COIN_P, 6, 1140, 560, 42);
      k.sfx('impact', b + .02, .08, { f: 200 }); k.sfx('ding', b + 1.1, .06, { f: 1260 });
      k.sfx('pop', b + 1.7, .1, { f: 900 }); k.sfx('applause', b + 2.1, .05);
      return st;
    }, update(lt, st, t) {
      const b = B[3];
      tf(st.halo, { o: ease.smooth5(P(t, b, 1.3)) * .9, s: .88 + .12 * ease.smooth5(P(t, b, 1.5)) });
      tf(st.win, { s: ease.outBack(P(t, b + .12, .5), 2.2), o: P(t, b + .12, .12) });
      tf(st.h, rise(t, b + .3, 1.1, 28));
      tf(st.s, rise(t, b + .9, .9, 14));
      tf(st.ar, { x: 24 * ease.io3(P(t, b + 1.4, 1.1)) * (Math.sin(t * 2.4) * .5 + .5), o: P(t, b + 1.3, .4) });
      tf(st.s1, { s: ease.outBack(P(t, b + .6, .5), 2), o: P(t, b + .6, .2), r: Math.floor(t * 2) % 2 ? 10 : -10 });
      tf(st.s2, { s: ease.outBack(P(t, b + .9, .5), 2), o: P(t, b + .9, .2) });
      tf(st.s3, { s: ease.outBack(P(t, b + 1.2, .5), 2), o: P(t, b + 1.2, .2), y: -(Math.floor(t * 4) % 2) * 12 });
    } });
})();
