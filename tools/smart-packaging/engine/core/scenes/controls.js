/* scenes/controls.js — 其余 10 控件场景库（全部主题无关：读 LAYOUT 常数 + THEME 令牌）
   与 keyword 同一模式：Scene + mk(opt.style) + show/camAt + 覆写钩子。props 合同见 catalog.json。 */
function _mkWin(sc, L, W, H, round = null) {
  if (round === null) round = CONFIG.height < CONFIG.width;
  const win = L.win; if (!win) return null;
  const cam = camCard(sc.el, win.w * W, win.h * H, (USER_INPUT.presenterLabel || '// on air'), round);
  cam._pos = { x: win.x * W, y: win.y * H };
  return cam;
}
function _winShow(cam, t, t0) { if (cam) camAt(cam, t, { o: pr(t, t0 + .1, t0 + .4), x: cam._pos.x, y: cam._pos.y, s: 1 }); }
function _txt(sc, x, y, style) { return mk(sc.el, '', x, y, { style }); }
function _tx(parent, x, y, style) { return mk(parent, '', x, y, { style }); }

const SCENE_BUILDERS = {
  quote(sc, seg, W, H, L) {
    S(seg.t0 + .15, 'whoosh', .6);
    const p = seg.props;
    _txt(sc, L.mark[0] * W, L.mark[1] * H, `font:900 ${Math.round(W * .10)}px 'NotoSC';color:var(--accent);opacity:.9`).textContent = '“';
    // 折行交给 width+normal 自动排（≤26字/行的经验值由 L.textW 控制；像素级校验在 P0.7 textfit）
    const body = _txt(sc, L.text[0] * W, L.text[1] * H,
      `font:900 ${Math.round(W * L.textSize)}px 'NotoSC';color:var(--ink);line-height:1.5;width:${L.textW * W}px;white-space:normal`);
    body.textContent = p.text || '';
    const who = _txt(sc, L.who[0] * W, L.who[1] * H, `font:500 ${Math.round(W * .030)}px 'Anton';letter-spacing:${W*.004}px;color:var(--dim)`);
    who.textContent = '— ' + (p.who || '');
    const cam = _mkWin(sc, L, W, H);
    sc.update = t => {
      show(body, t, seg.t0 + .15, { k: 'up', d: .6 });
      show(who, t, seg.t0 + 1.2, { k: 'up', d: .4 });
      _winShow(cam, t, seg.t0);
    };
  },
  list(sc, seg, W, H, L) {
    (p => (p.items || []).slice(0, 5).forEach((_, i) => S(seg.t0 + .2 + i * .55, 'pop', .6)))(seg.props);
    const p = seg.props;
    _txt(sc, L.title[0] * W, L.title[1] * H, `font:900 ${Math.round(W * L.titleSize)}px 'NotoSC';color:var(--ink);white-space:nowrap`).textContent = p.title || '';
    const rows = (p.items || []).slice(0, 5).map((it, i) => {
      const y = (L.items[1] + i * L.itemGap) * H;
      const dot = _txt(sc, L.items[0] * W, y, `font:900 ${Math.round(W * .040)}px 'NotoSC';color:var(--accent)`);
      dot.textContent = String(i + 1).padStart(2, '0');
      const tx = _txt(sc, (L.items[0] + .07) * W, y, `font:600 ${Math.round(W * L.itemSize)}px 'NotoSC';color:var(--ink);line-height:1.4;white-space:nowrap`);
      tx.textContent = it;
      return { dot, tx };
    });
    const cam = _mkWin(sc, L, W, H);
    sc.update = t => {
      rows.forEach((r, i) => {
        const t0 = seg.t0 + .2 + i * .55;
        show(r.dot, t, t0, { k: 'pop', d: .3 });
        show(r.tx, t, t0 + .08, { k: 'right', d: .4, dist: 60 });
      });
      _winShow(cam, t, seg.t0);
    };
  },
  compare(sc, seg, W, H, L) {
    S(seg.t0 + .1, 'whoosh', .65, -.3); S(seg.t0 + .2, 'whoosh', .65, .3); S(seg.t0 + 1.2, 'hit', .95);
    const p = seg.props;
    const side = (cfg, lay, col) => {
      const [x, y, w, h] = lay.map(v => v * (cfg === 'x' || true ? 1 : 1));
      const box = mk(sc.el, '', lay[0] * W, lay[1] * H, { style: `width:${lay[2] * W}px;height:${lay[3] * H}px;border:3px solid var(--dim);border-radius:${W*.015}px;background:color-mix(in srgb, var(--bg) 80%, var(--accent) 6%);padding:${W*.03}px` });
      const big = _tx(box, W * .02, H * .01, `font:900 ${Math.round(W * .075)}px 'NotoSC';color:var(--${col});white-space:nowrap`);
      big.textContent = cfg.big || '';
      const lines = (cfg.lines || []).map((ln, i) => {
        const e = _tx(box, W * .02, H * (.175 + i * .055), `font:600 ${Math.round(W * .034)}px 'NotoSC';color:var(--dim);white-space:nowrap`);
        e.textContent = ln; return e;
      });
      const lab = _tx(box, W * .02, -W * .038, `font:700 ${Math.round(W * .026)}px 'NotoSC';color:var(--dim);letter-spacing:${W*.004}px;background:var(--bg);padding:0 ${W*.012}px;white-space:nowrap`);
      lab.textContent = cfg.label || '';
      return { box, big, lines, lab };
    };
    const Ls = side(p.left || {}, L.left, 'ink');
    const Rs = side(p.right || {}, L.right, 'ink');
    const vs = p.verdict ? (() => {
      const e = _txt(sc, L.verdict[0] * W, L.verdict[1] * H, `font:900 ${Math.round(W * L.verdictSize)}px 'NotoSC';color:var(--accent);white-space:nowrap`);
      e.textContent = p.verdict; return e;
    })() : null;
    const cam = _mkWin(sc, L, W, H);
    sc.update = t => {
      show(Ls.box, t, seg.t0 + .1, { k: 'left', d: .5 }); Ls.lines.forEach((e, i) => show(e, t, seg.t0 + .5 + i * .15, { k: 'up', d: .3 })); show(Ls.big, t, seg.t0 + .3, { k: 'slam', d: .4 }); show(Ls.lab, t, seg.t0 + .2, { k: 'up', d: .3 });
      show(Rs.box, t, seg.t0 + .2, { k: 'right', d: .5 }); Rs.lines.forEach((e, i) => show(e, t, seg.t0 + .6 + i * .15, { k: 'up', d: .3 })); show(Rs.big, t, seg.t0 + .4, { k: 'slam', d: .4 }); show(Rs.lab, t, seg.t0 + .3, { k: 'up', d: .3 });
      if (vs) show(vs, t, seg.t0 + 1.2, { k: 'slam', d: .4 });
      _winShow(cam, t, seg.t0);
    };
  },
  tip(sc, seg, W, H, L) {
    S(seg.t0 + .25, 'ding', .7);
    const p = seg.props;
    const kinds = { tip: ['var(--accent)', '💡'], warn: ['var(--pop)', '⚠'], info: ['var(--accent)', 'ⓘ'], ok: ['#3ECFB2', '✔'] };
    const [col, glyph] = kinds[p.kind] || kinds.info;
    const badge = _txt(sc, L.badge[0] * W, L.badge[1] * H, `font:900 ${Math.round(W * .09)}px 'NotoSC';color:${col}`);
    badge.textContent = glyph;
    const title = _txt(sc, L.title[0] * W, L.title[1] * H, `font:900 ${Math.round(W * L.titleSize)}px 'NotoSC';color:var(--ink);white-space:nowrap`);
    title.textContent = p.title || '';
    const body = _txt(sc, L.text[0] * W, L.text[1] * H, `font:600 ${Math.round(W * L.textSize)}px 'NotoSC';color:var(--dim);line-height:1.6;width:${W * .8}px;white-space:normal`);
    body.textContent = p.text || '';
    const cam = _mkWin(sc, L, W, H);
    sc.update = t => {
      show(badge, t, seg.t0 + .1, { k: 'pop', d: .35 });
      show(title, t, seg.t0 + .25, { k: 'slam', d: .4 });
      show(body, t, seg.t0 + .6, { k: 'up', d: .5 });
      _winShow(cam, t, seg.t0);
    };
  },
  steps(sc, seg, W, H, L) {
    (p => (p.steps || []).slice(0, 5).forEach((_, i) => S(seg.t0 + .3 + i * .6, 'ding', .55, i % 2 ? .4 : -.4)))(seg.props);
    const p = seg.props;
    _txt(sc, L.title[0] * W, L.title[1] * H, `font:900 ${Math.round(W * L.titleSize)}px 'NotoSC';color:var(--ink);white-space:nowrap`).textContent = p.title || '';
    const rows = (p.steps || []).slice(0, 5).map((s, i) => {
      const y = (L.steps[1] + i * L.stepGap) * H;
      const line = mk(sc.el, '', L.steps[0] * W, y, { style: `width:${W * .86}px;height:2px;background:var(--dim);opacity:.4` });
      const bead = _txt(sc, L.steps[0] * W, y - W * .026, `font:900 ${Math.round(W * .036)}px 'Anton';color:var(--accent)`);
      bead.textContent = '0' + (i + 1);
      const tx = _txt(sc, (L.steps[0] + .06) * W, y - W * .030, `font:700 ${Math.round(W * L.stepSize)}px 'NotoSC';color:var(--ink);white-space:nowrap`);
      tx.textContent = s;
      return { line, bead, tx };
    });
    const cam = _mkWin(sc, L, W, H);
    sc.update = t => {
      rows.forEach((r, i) => {
        const t0 = seg.t0 + .3 + i * .6;
        show(r.line, t, t0, { k: 'mask', d: .4 });
        show(r.bead, t, t0 + .1, { k: 'pop', d: .3 });
        show(r.tx, t, t0 + .15, { k: 'up', d: .35 });
      });
      _winShow(cam, t, seg.t0);
    };
  },
  myth(sc, seg, W, H, L) {
    const p = seg.props;
    const kicker = _txt(sc, L.kicker[0] * W, L.kicker[1] * H, `font:700 ${Math.round(W * .028)}px 'NotoSC';color:var(--dim);letter-spacing:${W*.006}px;white-space:nowrap`);
    kicker.textContent = p.kicker || '';
    const panel = (txt, lay, lab, labCol, strike) => {
      const [x, y, w, h] = [lay[0] * W, lay[1] * H, lay[2] * W, lay[3] * H];
      const box = mk(sc.el, '', x, y, { style: `width:${w}px;height:${h}px;border:3px solid var(--dim);border-radius:${W*.012}px;padding:${W*.028}px` });
      const label = _tx(box, 0, -W * .030, `font:900 ${Math.round(W * .026)}px 'NotoSC';color:${labCol};background:var(--bg);padding:0 ${W*.012}px;white-space:nowrap`);
      label.textContent = lab;
      const body = _tx(box, 0, H * .02, `font:600 ${Math.round(W * L.bodySize)}px 'NotoSC';color:var(--ink);line-height:1.35;white-space:normal;width:${w - W*.056}px`);
      body.textContent = txt || '';
      if (strike) body.style.textDecoration = 'line-through'; body.style.opacity = .55;
      place(label, { o: 1 }); place(body, { o: strike ? .55 : 1 });   // 子元素随盒体入场，不再单独 show
      return box;                       // 返回盒体：show 要抬盒体，内层才有可见父级
    };
    const mb = panel(p.myth, L.myth, p.mythLabel || '误区', 'var(--dim)', true);
    const fb = panel((p.fact || '').replaceAll('*', ''), L.fact, p.factLabel || '真相', 'var(--accent)', false);
    const cam = _mkWin(sc, L, W, H);
    sc.update = t => {
      show(kicker, t, seg.t0, { k: 'up', d: .4 });
      show(mb, t, seg.t0 + .2, { k: 'up', d: .4 });
      show(fb, t, seg.t0 + 1.0, { k: 'slam', d: .4 });
      _winShow(cam, t, seg.t0);
    };
  },
  chapter(sc, seg, W, H, L) {
    S(seg.t0 + .15, 'hit', 1); S(seg.t0 + .3, 'swoosh', .5);
    const p = seg.props;
    const no = _txt(sc, L.no[0] * W, L.no[1] * H, `font:500 ${Math.round(W * .040)}px 'Anton';letter-spacing:${W*.010}px;color:var(--accent)`);
    no.textContent = (p.no || '').toUpperCase();
    const name = _txt(sc, L.name[0] * W, L.name[1] * H, `font:900 ${Math.round(W * L.nameSize)}px 'NotoSC';color:var(--ink);white-space:nowrap`);
    name.textContent = p.name || '';
    const sub = _txt(sc, L.sub[0] * W, L.sub[1] * H, `font:600 ${Math.round(W * .038)}px 'NotoSC';color:var(--dim);white-space:nowrap`);
    sub.textContent = p.sub || '';
    const cam = _mkWin(sc, L, W, H);
    sc.update = t => {
      show(no, t, seg.t0, { k: 'up', d: .4 });
      show(name, t, seg.t0 + .15, { k: 'slam', d: .5 });
      show(sub, t, seg.t0 + .5, { k: 'up', d: .4 });
      _winShow(cam, t, seg.t0);
    };
  },
  intro(sc, seg, W, H, L) {
    const p = seg.props;
    const t1 = _txt(sc, L.title[0] * W, L.title[1] * H, `font:900 ${Math.round(W * L.titleSize)}px 'NotoSC';color:var(--ink);white-space:nowrap`);
    t1.textContent = p.title || '';
    const st = _txt(sc, L.subtitle[0] * W, L.subtitle[1] * H, `font:600 ${Math.round(W * L.subtitleSize)}px 'NotoSC';color:var(--dim);white-space:nowrap`);
    st.textContent = p.subtitle || '';
    const ep = _txt(sc, L.episode[0] * W, L.episode[1] * H, `font:500 ${Math.round(W * .032)}px 'Anton';letter-spacing:${W*.008}px;color:var(--accent)`);
    ep.textContent = p.episode || '';
    sc.update = t => {
      show(t1, t, seg.t0 + .1, { k: 'slam', d: .55 });
      show(st, t, seg.t0 + .6, { k: 'up', d: .5 });
      show(ep, t, seg.t0 + .9, { k: 'up', d: .4 });
    };
  },
  outro(sc, seg, W, H, L) {
    S(seg.t0 + .1, 'chime', .7); S(seg.t0 + 1.1, 'pop', .8);
    const p = seg.props;
    const th = _txt(sc, L.thanks[0] * W, L.thanks[1] * H, `font:900 ${Math.round(W * L.thanksSize)}px 'NotoSC';color:var(--ink);white-space:nowrap`);
    th.textContent = p.thanks || '';
    const sb = _txt(sc, L.sub[0] * W, L.sub[1] * H, `font:600 ${Math.round(W * .040)}px 'NotoSC';color:var(--dim);white-space:nowrap`);
    sb.textContent = p.sub || '';
    const nm = _txt(sc, L.name[0] * W, L.name[1] * H, `font:500 ${Math.round(W * .034)}px 'Anton';letter-spacing:${W*.006}px;color:var(--accent)`);
    nm.textContent = p.name || '';
    const rows = (p.items || []).slice(0, 3).map((it, i) => {
      const e = _txt(sc, L.items[0] * W, (L.items[1] + i * L.itemGap) * H, `font:600 ${Math.round(W * .038)}px 'NotoSC';color:var(--ink);white-space:nowrap`);
      e.textContent = '▸ ' + (it.text || it); return e;
    });
    const fol = mk(sc.el, '', L.follow[0] * W, L.follow[1] * H, { style: `width:${W*.18}px;height:${W*.075}px;border-radius:99px;background:var(--accent);display:flex;align-items:center;justify-content:center;font:900 ${Math.round(W*.040)}px 'NotoSC';color:var(--bg);white-space:nowrap` });
    fol.textContent = p.follow || '关注';
    const cam = _mkWin(sc, { win: { w: .18, h: .30, x: .82, y: .20 } }, W, H);
    sc.update = t => {
      show(th, t, seg.t0 + .1, { k: 'slam', d: .5 });
      show(sb, t, seg.t0 + .5, { k: 'up', d: .4 });
      show(nm, t, seg.t0 + .7, { k: 'up', d: .4 });
      rows.forEach((e, i) => show(e, t, seg.t0 + .9 + i * .3, { k: 'right', d: .35, dist: 60 }));
      show(fol, t, seg.t0 + 1.1, { k: 'pop', d: .4 });
      _winShow(cam, t, seg.t0);
    };
  },
  cta(sc, seg, W, H, L) {
    const p = seg.props;
    _txt(sc, L.kicker[0] * W, L.kicker[1] * H, `font:700 ${Math.round(W * .028)}px 'NotoSC';color:var(--dim);letter-spacing:${W*.006}px;white-space:nowrap`).textContent = p.kicker || '';
    const tx = _txt(sc, L.text[0] * W, L.text[1] * H, `font:900 ${Math.round(W * L.textSize)}px 'NotoSC';color:var(--accent);white-space:nowrap`);
    tx.textContent = p.text || '';
    const sb = _txt(sc, L.sub[0] * W, L.sub[1] * H, `font:600 ${Math.round(W * .040)}px 'NotoSC';color:var(--dim);white-space:nowrap`);
    sb.textContent = p.sub || '';
    const cam = _mkWin(sc, L, W, H);
    sc.update = t => {
      show(tx, t, seg.t0 + .1, { k: 'slam', d: .5 });
      show(sb, t, seg.t0 + .6, { k: 'up', d: .4 });
      _winShow(cam, t, seg.t0);
    };
  }
};

function buildScene(seg) {
  const builder = SCENE_BUILDERS[seg.control];
  if (seg.control === 'keyword') return keywordScene(null, seg);
  if (!builder) return null;
  const sc = new Scene(seg.t0, seg.t1, 'var(--bg)');
  sc.id = seg.control; window.CURRENT_UNIT = seg.control;
  const W = CONFIG.width, H = CONFIG.height;
  const _lv = override(seg.control + 'Layout', null) || layoutFor(seg.control);
  const _raw = (typeof _lv === 'function') ? _lv(layoutFor(seg.control)) : _lv;
  const L = (window.fitLayout43 || (x => x))(_raw);   // 3:4 画幅归一化（其它画幅原样返回）
  builder(sc, seg, W, H, L);
  return sc;
}
