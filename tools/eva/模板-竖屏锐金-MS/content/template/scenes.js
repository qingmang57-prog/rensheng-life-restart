/* 竖屏锐金 · 1080×1920 · 夜校风（炭黑 + 荧光金）
 *
 * 设计来源（本地工作空间，2026-10-31）：
 *  - 《学习包装2-逐帧学习档案》12 版式原语 / 常驻角标 / 标签擦入·撞击收拢·编号清单动效语法
 *  - 《新包装风格设计-20261027》D 款「夜校」：炭黑 #101418 + 荧光金 #FFD400，锐利搞钱系
 *  - 参考片 DNA「金句屏 + 圆窗」：金句场人像缩为右下圆窗，大字占满中轴（截图传播点）
 *
 * 竖屏分带（同竖屏学习包的实测结论）：
 *   角标 90–150 / 标题 200–366 / 窗口 380–1304 / 说明 1130–1360 / 字幕带 1405 起（外挂，模板不画）
 */
(function () {
  const C = EV, { el, tf, pop, slide, P, ease, clamp } = C, k = K;
  const _P = window.__EVA_P || {};
  const SC = (Number(_P.duration) > 1 ? Number(_P.duration) / 15 : 1);
  const txt = (key, dft) => { const v = _P[key]; return (v != null && String(v).trim()) ? String(v) : dft; };

  const W = C.CW, H = C.CH;
  const X = n => Math.round(n * W / 1080);
  const Y = n => Math.round(n * H / 1920);
  const F = n => Math.round(n * W / 1080);
  const MID = X(540);

  /* 设计 Token（学习包装2 实测取色 × 夜校配色） */
  const GOLD = '#FFD400', HOT = '#FF1F2E', INK = '#F5F5F0', DIM = '#7E8794', LINE = '#2A313C';
  const BG = 'radial-gradient(1400px 1000px at 50% 28%, #1A2028, #0C1014 62%, #080A0E 100%)';
  const CN = "'Noto Sans CJK SC','Noto Sans SC',sans-serif";
  const EN = "'Anton','Archivo Black',sans-serif";
  const MONO = "'Space Mono',monospace";

  /* ───────────── 全局：帧序列 / 人像窗 ───────────── */
  C.src0 = 0;
  C.end = 15.1 * SC;
  C.evaDir = (_P.host && _P.host.dir) || 'media/eva';
  /* t24 人物居中：素材 540x960 实测肤色质心 (.354,.384)（3 点均值，diag/人物居中优化-20261118）。
     原 fallback (.5,.42) 致窗内人物偏左上（基线 dx=-.141,dy=-.087）。
     覆盖语义下窗≈图同尺寸（cover0），left/top 被 clamp 吞掉 face 偏移——必须配窗 zoom：
     hero s=1.3 + face.h=.45 放宽 cap（cap=.6126*924/(h*960*.963)≥1.3 需 h≤.471）→
     预测 dx=.354*1.252-.5=-.04、top 恰不贴边，dy≈0（y=.344 补 ty=.447 与质心 .384 差）。
     small s=1.13（cap=1.129 上限）；quote 全圆窗 cap=1 压制 zoom，不设 s。 */
  C.face = (_P.host && _P.host.face) || { x: .354, y: .344, h: .45 };
  C.headFill = .62;

  const win = (w, s) => ({ x: X((1080 - w) / 2), y: Y(380), w: X(w), h: Y(Math.round(w * 16 / 9)), r: X(24), s: s || 1 });
  const WIN = {
    hero:  win(520, 1.3),   // 380–1304，钩子/金句前主场；t24 居中 zoom
    small: win(400, 1.13),  // 380–1091，给清单让位；t24 居中 zoom（cap 上限）
    quote: { x: X(760), y: Y(1430), w: X(260), h: Y(260), r: X(130), s: 1 },  // 金句场：右下圆窗
  };
  /* 人像窗时间轴：S1/S2 大窗 → S3 缩小让清单 → S4 右下圆窗（参考片「金句屏+圆窗」DNA） */
  C.evaKeys = (_P.host && _P.host.dir) ? [
    { t: 0,          d: 0,   L: WIN.hero },
    { t: 7.0 * SC,   d: .55, L: WIN.small },
    { t: 11.55 * SC, d: .6,  L: WIN.quote },
  ] : [{ t: 0, d: 0, L: Object.assign({}, WIN.hero, { o: 0 }) }];
  C.ringFn = () => ({ color: 'rgba(245,245,240,.95)', width: X(5), shadow: .35 });

  /* ───────────── 公共构件 ───────────── */
  /* 常驻角标：左上品牌标签 + 右上时间码（学习包装2 五、系统感来源） */
  function hud(L, st, chapter) {
    st.brand = el(L, 'div', 'abs chip', {
      left: X(44) + 'px', top: Y(88) + 'px', fontSize: F(22) + 'px',
      background: '#0C1014', color: GOLD, border: X(2) + 'px solid ' + LINE,
      padding: F(8) + 'px ' + F(20) + 'px', borderRadius: X(8) + 'px',
      fontFamily: CN, zIndex: 8, letterSpacing: F(3) + 'px',
    }, txt('brand', '织梦工坊') + ' · ' + chapter);
    st.tc = el(L, 'div', 'abs hud', {
      right: X(44) + 'px', top: Y(96) + 'px', fontSize: F(20) + 'px',
      color: DIM, fontFamily: MONO, zIndex: 8,
    }, '00:00:00:00');
  }
  function tick(st, t) {
    if (!st.tc) return;
    const ss = Math.floor(t), ff = Math.floor((t - ss) * 30);
    const p2 = n => (n < 10 ? '0' : '') + n;
    st.tc.textContent = '00:' + p2(Math.floor(ss / 60)) + ':' + p2(ss % 60) + ':' + p2(ff);
  }
  /* 暗角（每场一层，压住 radial 底的四角） */
  function vig(L) {
    el(L, 'div', 'abs', { left: 0, top: 0, width: W + 'px', height: H + 'px', zIndex: 0,
      background: 'radial-gradient(120% 90% at 50% 40%, transparent 55%, rgba(0,0,0,.5) 100%)' });
  }

  /* ───────────── S1 · 0–3.2s · 钩子砸字 ───────────── */
  C.scene({ t0: 0, t1: 3.22 * SC, bg: BG, build(L) {
      const st = {};
      vig(L); hud(L, st, 'HOOK');
      /* 两行巨字：白 + 末 2 字荧光金（砸字公式），红色残影撞击收拢 */
      const l1 = txt('s1_l1', '量大'), l2 = txt('s1_l2', '出奇迹');
      const hl2 = l2.slice(0, -2) + '<span style="color:' + GOLD + '">' + l2.slice(-2) + '</span>';
      st.ghost = el(L, 'div', 'abs cn', {
        left: 0, right: 0, textAlign: 'center', top: Y(200) + 'px',
        fontSize: F(78) + 'px', color: HOT, fontFamily: CN, fontWeight: 900,
        lineHeight: 1.16, zIndex: 6,
      }, l1 + '<br>' + l2);
      st.h = el(L, 'div', 'abs cn', {
        left: 0, right: 0, textAlign: 'center', top: Y(200) + 'px',
        fontSize: F(78) + 'px', color: INK, fontFamily: CN, fontWeight: 900,
        textShadow: '0 ' + F(10) + 'px ' + F(30) + 'px rgba(0,0,0,.6)', lineHeight: 1.16, zIndex: 7,
      }, l1 + '<br>' + hl2);
      st.tag = el(L, 'div', 'abs chip', {
        left: MID + 'px', top: Y(152) + 'px', fontSize: F(26) + 'px',
        background: GOLD, color: '#0C1014', padding: F(8) + 'px ' + F(24) + 'px',
        borderRadius: X(10) + 'px', fontFamily: CN, zIndex: 7,
        transform: 'translateX(-50%)',
      }, txt('s1_tag', '实测提醒'));
      k.sfx('impact', .08 * SC, .18);
      k.sfx('pop', 1.1 * SC, .11, { f: 1100 });
      return st;
    }, update(lt, st, t) {
      const u = ease.outBack(P(lt, .05, .55));
      tf(st.h, { s: .82 + .18 * u, o: clamp(u * 3), y: F(-40) * (1 - u) });
      tf(st.ghost, { s: .82 + .18 * u, o: (1 - P(lt, .1, .8)) * .8, x: F(26) * (1 - u), y: F(-40) * (1 - u) });
      tf(st.tag, pop(lt, .9, .45, 1.4));
      tick(st, t);
    } });

  /* ───────────── S2 · 3.2–7.0s · 标签章节 ───────────── */
  C.scene({ t0: 3.22 * SC, t1: 7.02 * SC, bg: BG, build(L) {
      const st = {};
      vig(L); hud(L, st, '01');
      /* 黑标签（红点）擦入 + 巨字标题 + 英文行（chapter 版式竖屏化） */
      st.tag = el(L, 'div', 'abs', {
        left: X(64) + 'px', top: Y(218) + 'px', background: '#0C1014',
        border: X(2) + 'px solid ' + LINE, borderRadius: X(8) + 'px',
        padding: F(10) + 'px ' + F(26) + 'px', fontSize: F(26) + 'px',
        color: INK, fontFamily: CN, fontWeight: 700, zIndex: 6,
        letterSpacing: F(3) + 'px', whiteSpace: 'nowrap',
      }, '<span style="color:' + HOT + '">●</span> ' + txt('s2_tag', '为什么 · WHY'));
      st.h = el(L, 'div', 'abs cn', {
        left: X(64) + 'px', top: Y(268) + 'px', fontSize: F(64) + 'px',
        color: INK, fontFamily: CN, fontWeight: 900, zIndex: 7,
        textShadow: '0 ' + F(8) + 'px ' + F(24) + 'px rgba(0,0,0,.6)',
      }, txt('s2_h', '什么叫「猛」？'));
      st.en = el(L, 'div', 'abs disp', {
        left: X(68) + 'px', top: Y(356) + 'px', fontSize: F(24) + 'px',
        color: DIM, fontFamily: EN, letterSpacing: F(6) + 'px', zIndex: 7,
      }, txt('s2_en', 'CHAPTER 01'));
      /* 荧光金下划线：手绘拉出（晨读朱砂线的锐金版），贴在中文标题底下 */
      st.ul = el(L, 'div', 'abs', {
        left: X(64) + 'px', top: Y(344) + 'px', width: X(320) + 'px', height: F(6) + 'px',
        background: GOLD, transformOrigin: '0 50%', zIndex: 7,
      });
      k.sfx('whoosh', 3.24 * SC, .12);
      k.sfx('impact', 3.5 * SC, .14);
      return st;
    }, update(lt, st, t) {
      const wt = P(lt, .05, .42);
      st.tag.style.clipPath = 'inset(0 ' + ((1 - clamp(wt)) * 100).toFixed(1) + '% 0 0)';
      tf(st.h, { y: F(26) * (1 - ease.outExpo(P(lt, .18, .5))), o: P(lt, .18, .2) });
      tf(st.en, { o: P(lt, .5, .4) });
      tf(st.ul, { sx: ease.outExpo(P(lt, .55, .6)), sy: 1 });
      tick(st, t);
    } });

  /* ───────────── S3 · 7.0–11.55s · 编号清单 ───────────── */
  C.scene({ t0: 7.02 * SC, t1: 11.55 * SC, bg: BG, build(L) {
      const st = {};
      vig(L); hud(L, st, '02');
      st.h = el(L, 'div', 'abs cn', {
        left: 0, right: 0, textAlign: 'center', top: Y(218) + 'px',
        fontSize: F(54) + 'px', color: INK, fontFamily: CN, fontWeight: 900, zIndex: 7,
      }, txt('s3_h', '为什么会这样？'));
      /* 三条目：红底编号方块 + 白字 + 发丝线（list 原语竖屏化，窗口已缩小让位） */
      const items = [txt('s3_i1', '空腹吃刺激肠胃'), txt('s3_i2', '肠胃不好的别吃两条'), txt('s3_i3', '要随餐或者餐后吃')];
      st.rows = items.map((tx2, i) => {
        const y = Y(1150) + i * Y(88);
        const num = el(L, 'div', 'abs disp', {
          left: X(120) + 'px', top: y + 'px', width: X(56) + 'px', height: X(56) + 'px',
          background: HOT, color: '#0C1014', fontSize: F(28) + 'px', fontFamily: EN,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          borderRadius: X(10) + 'px', zIndex: 7,
        }, '0' + (i + 1));
        const t2 = el(L, 'div', 'abs cn', {
          left: X(200) + 'px', top: (y + Y(4)) + 'px', fontSize: F(42) + 'px',
          color: INK, fontFamily: CN, fontWeight: 900, zIndex: 7,
        }, tx2);
        const ln = el(L, 'div', 'abs', {
          left: X(120) + 'px', top: (y + Y(72)) + 'px', width: X(560) + 'px',
          height: X(2) + 'px', background: LINE, zIndex: 6,
        });
        return { num, t2, ln };
      });
      k.sfx('impact', 7.05 * SC, .15);
      [7.62, 8.46, 9.30].forEach((x, i) => k.sfx('pop', x * SC, .11, { f: 820 + i * 190 }));
      return st;
    }, update(lt, st, t) {
      tf(st.h, pop(lt, .08, .45, 1.3));
      st.rows.forEach((r, i) => {
        const u = ease.outExpo(P(lt, [.5, 1.3, 2.1][i], .55));
        const s = pop(lt, [.5, 1.3, 2.1][i], .45, 1.4);
        tf(r.num, { x: F(-60) * (1 - u), o: s.o, s: s.s });
        tf(r.t2,  { x: F(-60) * (1 - u), o: s.o });
        tf(r.ln,  { sx: ease.outExpo(P(lt, [.5, 1.3, 2.1][i] + .25, .5)), sy: 1 });
      });
      tick(st, t);
    } });

  /* ───────────── S4 · 11.55–15.1s · 金句屏 + 右下圆窗 ───────────── */
  C.scene({ t0: 11.55 * SC, t1: 15.1 * SC, bg: BG, quiet: true, build(L) {
      const st = {};
      vig(L); hud(L, st, 'QUOTE');
      /* 金句卡：上金线 + 两行大字（第二行金） + 下金线，逐行升入（astro 竖屏化） */
      st.ln1 = el(L, 'div', 'abs', {
        left: X(160) + 'px', top: Y(470) + 'px', width: X(760) + 'px', height: F(5) + 'px',
        background: GOLD, transformOrigin: '50% 50%', zIndex: 7,
      });
      st.q1 = el(L, 'div', 'abs cn', {
        left: 0, right: 0, textAlign: 'center', top: Y(520) + 'px',
        fontSize: F(92) + 'px', color: INK, fontFamily: CN, fontWeight: 900,
        letterSpacing: F(6) + 'px', zIndex: 7,
      }, txt('s4_l1', '量大出奇迹'));
      st.q2 = el(L, 'div', 'abs cn', {
        left: 0, right: 0, textAlign: 'center', top: Y(650) + 'px',
        fontSize: F(104) + 'px', color: GOLD, fontFamily: CN, fontWeight: 900,
        letterSpacing: F(8) + 'px', textShadow: '0 ' + F(14) + 'px ' + F(40) + 'px rgba(0,0,0,.65)', zIndex: 7,
      }, txt('s4_l2', '肠胃才是本钱'));
      st.en = el(L, 'div', 'abs disp', {
        left: 0, right: 0, textAlign: 'center', top: Y(810) + 'px',
        fontSize: F(26) + 'px', color: DIM, fontFamily: EN, letterSpacing: F(10) + 'px', zIndex: 7,
      }, txt('s4_en', 'VOLUME WINS'));
      st.ln2 = el(L, 'div', 'abs', {
        left: X(160) + 'px', top: Y(878) + 'px', width: X(760) + 'px', height: F(5) + 'px',
        background: GOLD, transformOrigin: '50% 50%', zIndex: 7,
      });
      k.sfx('impact', 11.6 * SC, .18);
      k.sfx('riser', 12.4 * SC, .9);
      return st;
    }, update(lt, st, t) {
      /* 人像 11.55–12.15 缩成圆窗，文字等窗口走开再升入 */
      const rise = (e, t0) => { const u = ease.outExpo(P(lt, t0, .6)); tf(e, { y: F(70) * (1 - u), o: clamp(u * 2) }); };
      rise(st.q1, .5); rise(st.q2, .75); rise(st.en, 1.0);
      tf(st.ln1, { sx: ease.outExpo(P(lt, .35, .6)), sy: 1 });
      tf(st.ln2, { sx: ease.outExpo(P(lt, 1.05, .6)), sy: 1 });
      tick(st, t);
    } });
})();
