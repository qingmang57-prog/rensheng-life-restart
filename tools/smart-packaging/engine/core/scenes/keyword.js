/* scenes/keyword.js — 关键词语义单元 v2（布局常数表 + 家族覆写钩子） */
function keywordScene(parent, seg) {
  const sc = new Scene(seg.t0, seg.t1, 'var(--bg)');
  sc.id = 'keyword'; window.CURRENT_UNIT = 'keyword';
  const W = CONFIG.width, H = CONFIG.height;
  const _lv = override('keywordLayout', null) || layoutFor('keyword');
  const _raw = (typeof _lv === 'function') ? _lv(layoutFor('keyword')) : _lv;
  const L = (window.fitLayout43 || (x => x))(_raw);   // 3:4 画幅归一化（其它画幅原样返回）
  const p = seg.props;
  const kick = mk(sc.el, '', L.kicker[0] * W, L.kicker[1] * H,
    { style: `font:700 ${Math.round(W * .028)}px 'NotoSC';letter-spacing:${W * .006}px;color:var(--dim);white-space:nowrap` });
  setTxt(kick, p.kicker || '');
  const ws = Math.round(W * L.wordSize);
  const styleTpl = override('keywordWordStyle', `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:__LS__px`);
  const centered = L.word[0] >= .5;                    // 布局覆写约定：x≥.5 视为居中构图
  const wordEl = mk(sc.el, '', L.word[0] * W, L.word[1] * H,
    { ax: centered ? .5 : 0, ay: .5,
      style: styleTpl.split('__WS__').join(ws).split('__LS__').join(Math.round(W * .008)).split('__SW__').join(Math.max(2, Math.round(W * .004))) + ';white-space:nowrap' });
  setTxt(wordEl, p.word || '');
  fitEl(wordEl, W * .80);                          // P0.7 像素级校验：超宽即缩并记录
  const subEls = (p.sub || []).map((line, i) => {
    const e = mk(sc.el, '', L.sub[0] * W, (L.sub[1] + i * L.subGap) * H,
      { ax: centered ? .5 : 0,
        style: `font:600 ${Math.round(W * .040)}px 'NotoSC';color:var(--dim);line-height:1.5;white-space:nowrap;text-align:${centered ? 'center' : 'left'}` });
    setTxt(e, line);
    return e;
  });
  const round = override('presenterRound', CONFIG.height < CONFIG.width);  // 竖屏大卡/横屏圆窗
  const win = L.win;
  const cam = camCard(sc.el, win.w * W, win.h * H, (USER_INPUT.presenterLabel || '// on air'), round);
  const bar = mk(sc.el, `<div style="position:absolute;left:0;right:0;bottom:0;height:100%;background:var(--dim);opacity:.35;border-radius:99px"><i style="display:block;height:100%;width:0%;background:var(--accent);border-radius:99px"></i></div>`,
    W * .08, H * .92, { style: `width:${W * .84}px;height:${Math.round(W * .006)}px` });
  sc.update = t => {
    const kf = override('keywordEntrance', (e, t2, t0) => show(e, t2, t0, { k: 'slam', d: .5 }));
    kf(wordEl, t, seg.t0 + .1);
    show(kick, t, seg.t0, { k: 'up', d: .4 });
    const row = (USER_INPUT.narrationMap || [])[segIndexOf(seg)];
    const beats = (row && row.beats) || [];
    subEls.forEach((e, i) => {
      const b = beats[i + 1];
      show(e, t, b ? b.at : seg.t0 + .9 + i * .8, { k: 'up', d: .35 });
    });
    camAt(cam, t, { o: pr(t, seg.t0 + .1, seg.t0 + .4), x: win.x * W, y: win.y * H, s: 1 });
    bar.firstChild.style.width = (pr(t, seg.t0, seg.t1) * 100).toFixed(1) + '%';
    const pulse = window.beatPulse ? beatPulse(t) : 0;
    wordEl.style.textShadow = (!override.__raw && pulse > .55 && !styleTpl.includes('text-shadow') && !styleTpl.includes('drop-shadow'))
      ? `0 0 ${W * .03}px rgba(125,178,255,.6)` : wordEl.style.textShadow;
  };
  S(seg.t0 + .1, 'hit', .9);                                 // 关键词砸落
  (p.sub || []).forEach((_, i) => S(seg.t0 + .9 + i * .8, 'tick', .35, i % 2 ? -.5 : .5));
  const deco = override('keywordDecorate', null);
  if (deco) deco(sc, wordEl, L, W, H);
  return sc;
}
function segIndexOf(seg) { return (USER_INPUT.segments || []).indexOf(seg); }
