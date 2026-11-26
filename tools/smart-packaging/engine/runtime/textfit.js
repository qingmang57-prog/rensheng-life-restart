/* textfit.js — 像素级排版校验与自适应（P0.7：字数代理 → canvas 实测）
   三件事：①measureW 用与渲染同字体的 canvas 实测文本宽度
   ②fitEl 超宽自动缩字号（min 62%）并回报（确定性防御"导出才发现出框"）
   ③requireW 供装饰元素（如光环科技光轨）按实测字宽生成几何。 */
(function () {
  let ctx = null;
  function c2d() {
    if (!ctx) ctx = document.createElement('canvas').getContext('2d');
    return ctx;
  }
  function fontOf(el) { return getComputedStyle(el).font; }
  window.measureW = (el, txt) => {
    const c = c2d(); c.font = fontOf(el);
    return c.measureText(txt == null ? el.textContent : txt).width;
  };
  window.fitEl = (el, maxW, opt = {}) => {
    const txt = el.textContent;
    if (!txt) return { ok: true, w: 0 };
    let w = measureW(el, txt);
    if (w <= maxW) return { ok: true, w };
    const cs = getComputedStyle(el);
    let size = parseFloat(cs.fontSize);
    const min = size * (opt.min || .62);
    while (w > maxW && size > min) {
      size -= 2;
      el.style.fontSize = size + 'px';
      w = measureW(el, txt);
    }
    const ok = w <= maxW;
    (window.FIT_VIOLATIONS = window.FIT_VIOLATIONS || []).push(
      { text: txt.slice(0, 24), w: Math.round(w), max: Math.round(maxW), shrunkTo: Math.round(size), ok });
    return { ok, w };
  };
  window.fitReport = () => window.FIT_VIOLATIONS || [];
})();
