/* layouts.js — 布局常数表（P0.4 后续：版式几何从场景代码抽离，修重叠+支持家族微调）
   所有坐标为画幅分数（0..1）；场景代码只读表，不写真数。家族可在覆写层整体替换某控件布局。 */
window.LAYOUT = {
  keyword: { kicker: [.08, .14], word: [.08, .30], wordSize: .15, sub: [.08, .66], subGap: .062, win: { w: .34, h: .44, x: .78, y: .22 } },
  quote:   { mark: [.08, .12], text: [.10, .30], textSize: .062, textW: .78, who: [.10, .68], win: { w: .30, h: .40, x: .80, y: .24 } },
  list:    { title: [.07, .10], titleSize: .058, items: [.07, .30], itemGap: .105, itemSize: .048, win: { w: .26, h: .34, x: .82, y: .22 } },
  compare: { left: [.06, .24, .40, .30], right: [.54, .24, .40, .30], verdict: [.08, .72, .84, .10], verdictSize: .052, win: { w: .16, h: .22, x: .50, y: .12 } },
  tip:     { badge: [.08, .16], title: [.08, .30], titleSize: .072, text: [.08, .48], textSize: .050, win: { w: .30, h: .40, x: .80, y: .24 } },
  steps:   { title: [.07, .12], titleSize: .058, steps: [.07, .34], stepGap: .14, stepSize: .052, win: { w: .26, h: .34, x: .82, y: .60 } },
  myth:    { kicker: [.08, .12], myth: [.08, .24, .84, .21], fact: [.08, .51, .84, .21], labelSize: .048, bodySize: .028, win: { w: .24, h: .32, x: .84, y: .10 } },
  chapter: { no: [.08, .20], name: [.08, .38], nameSize: .115, sub: [.08, .60], win: { w: .24, h: .32, x: .82, y: .70 } },
  intro:   { title: [.08, .30], titleSize: .105, subtitle: [.08, .52], subtitleSize: .048, episode: [.08, .68] },
  outro:   { thanks: [.08, .22], thanksSize: .085, sub: [.08, .40], name: [.08, .62], items: [.08, .74], itemGap: .09, follow: [.72, .62] },
  cta:     { kicker: [.08, .30], text: [.08, .44], textSize: .075, sub: [.08, .62], win: { w: .26, h: .34, x: .80, y: .24 } }
};

/* 竖屏(9:16)覆写：人物大卡置上，文案区在下——对齐学习包装的信息结构 */
window.LAYOUT_P = {
  keyword: { kicker: [.08, .565], word: [.08, .615], wordSize: .125, sub: [.08, .73], subGap: .052,
             win: { w: .94, h: .52, x: .50, y: .285 } },
  quote:   { mark: [.08, .575], text: [.10, .625], textSize: .055, textW: .84, who: [.10, .86],
             win: { w: .94, h: .52, x: .50, y: .285 } },
  myth:    { kicker: [.08, .565], myth: [.08, .615, .84, .135], fact: [.08, .765, .84, .135],
             labelSize: .042, bodySize: .027, win: { w: .94, h: .50, x: .50, y: .27 } },
  tip:     { badge: [.08, .575], title: [.16, .585], titleSize: .062, text: [.08, .685], textSize: .042,
             win: { w: .94, h: .52, x: .50, y: .285 } },
  steps:   { title: [.07, .565], titleSize: .050, steps: [.07, .66], stepGap: .062, stepSize: .042,
             win: { w: .94, h: .50, x: .50, y: .27 } },
  compare: { left: [.06, .60, .42, .16], right: [.52, .60, .42, .16], verdict: [.08, .80, .84, .07],
             verdictSize: .042, win: { w: .94, h: .52, x: .50, y: .29 } },
  chapter: { no: [.08, .58], name: [.08, .645], nameSize: .095, sub: [.08, .76],
             win: { w: .94, h: .52, x: .50, y: .285 } },
  outro:   { thanks: [.08, .56], thanksSize: .068, sub: [.08, .66], name: [.08, .74], items: [.08, .82],
             itemGap: .05, follow: [.70, .63], win: { w: .94, h: .52, x: .50, y: .29 } }
};
window.layoutFor = c => ((CONFIG.height > CONFIG.width) ? (LAYOUT_P[c] || LAYOUT[c]) : LAYOUT[c]) || {};

/* 3:4（1440×1920）适配层 —— 与竖屏同高，仅宽 +33%。
   纵向全部是 H 的分数，px 值不变；唯一的形变是窗口宽度按 W 放大后被拉宽，
   所以在解析完布局（含家族覆写）后统一归一化一次：
   · 窗口高度保持，宽度封顶为近 1:1，避免 9:16 口播片在窗口里被上下裁死
   · x 为中心坐标，夹紧在安全框内
   其余常数（字号随 W、行距随 H）保持竖屏比例，不做二次调整。 */
window.is43 = function () {
  const r = (window.CONFIG ? CONFIG.width / CONFIG.height : 0.5625);
  return r > .66 && r < .95;
};
window.fitLayout43 = function (L) {
  if (!L || !window.is43 || !window.is43()) return L;
  const out = Object.assign({}, L);
  const win = L.win;
  if (win) {
    const hPx = win.h * CONFIG.height;
    const maxW = hPx * 1.05 / CONFIG.width;
    const w = Math.min(win.w, maxW);
    const x = Math.max(.02 + w / 2, Math.min(win.x, .98 - w / 2));
    out.win = { w: w, h: win.h, x: x, y: win.y };
  }
  if (typeof L.textW === 'number') out.textW = Math.min(.94, L.textW);
  return out;
};