/* captions.js — 底部字幕层（口播无字幕=不可用）
   数据源：USER_INPUT.captionsData（句级实测时间）；渲染：底部胶囊，逐句切换（250ms 淡入）。
   2026-11-05：移植切片工坊双行花字与关键词高亮。拆行/高亮词由 app/smart_packaging.py
   预计算写入 cue.lines（两行）或 cue.hi（单个高亮词），运行时只负责画，不重复切分逻辑。
   cue.lines 缺失即保持旧版单行白字，旧产物与旧方案不受影响。 */
(function () {
  let box = null, txtEl = null, lastTxt = null;
  const GOLD = '#E8C547';          // 切片工坊 SUBTITLE_GOLD #E8C547
  const HL = '#C96F4A';            // 切片工坊单行高亮暖橙 (0.788,0.435,0.290)
  window.initCaptions = function () {
    const W = CONFIG.width, H = CONFIG.height;
    const layer = document.body;
    box = document.createElement('div');
    // 必须给绝对定位写死宽度：只给 left:50% 时收缩宽度只有半屏，双行会被挤成三行。
    box.style.cssText = `position:absolute;left:${W * .03}px;width:${W * .94}px;bottom:${H * .055}px;`
      + `box-sizing:border-box;background:rgba(12,12,16,.82);border-radius:${W * .02}px;`
      + `padding:${H * .012}px ${W * .035}px;opacity:0;transition:opacity .25s;z-index:80;`;
    txtEl = document.createElement('div');
    txtEl.style.cssText = `font:600 ${Math.round(W * .042)}px 'NotoSC';color:#fff;line-height:1.4;`
      + `text-align:center;white-space:normal;`;
    box.appendChild(txtEl);
    layer.appendChild(box);
  };
  function paint(cue) {
    const W = CONFIG.width;
    txtEl.textContent = '';
    if (cue.lines && cue.lines.length === 2) {          // 双行花字：上行白、下行金色加大
      const stroke = Math.max(2, Math.round(W * .004));
      const up = document.createElement('div');
      up.textContent = cue.lines[0];
      up.style.cssText = `font:900 ${Math.round(W * .036)}px 'NotoSC';color:#fff;line-height:1.15;`
        + `-webkit-text-stroke:${stroke}px rgba(0,0,0,.8);paint-order:stroke fill;`;
      const down = document.createElement('div');
      down.textContent = cue.lines[1];
      down.style.cssText = `font:900 ${Math.round(W * .052)}px 'NotoSC';color:${GOLD};line-height:1.15;`
        + `-webkit-text-stroke:${stroke}px rgba(0,0,0,.8);paint-order:stroke fill;`;
      txtEl.append(up, down);
      return;
    }
    const hi = cue.hi || '';
    if (hi && cue.text.includes(hi)) {                  // 关键词高亮：暖橙 + 放大
      const at = cue.text.indexOf(hi);
      const before = document.createElement('span');
      before.textContent = cue.text.slice(0, at);
      const mid = document.createElement('span');
      mid.textContent = hi;
      mid.style.cssText = `color:${HL};font-size:1.5em;font-weight:900;`
        + `-webkit-text-stroke:${Math.max(1, Math.round(W * .002))}px rgba(0,0,0,.55);`;
      const after = document.createElement('span');
      after.textContent = cue.text.slice(at + hi.length);
      txtEl.append(before, mid, after);
      return;
    }
    txtEl.textContent = cue.text;
  }
  window.OVERLAY = function (t) {
    if (!box) return;
    const caps = (window.USER_INPUT || {}).captionsData || [];
    let cur = null;
    for (const c of caps) if (c.start <= t && t < c.end) { cur = c; break; }
    if (cur) {
      const key = cur.text + '|' + (cur.lines || []).join('/') + '|' + (cur.hi || '');
      if (lastTxt !== key) { lastTxt = key; paint(cur); }
      box.style.opacity = '1';
    } else {
      box.style.opacity = '0';
    }
  };
})();
