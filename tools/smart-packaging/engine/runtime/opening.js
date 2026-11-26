/* opening.js — 开场卡（移植切片工坊 jy_bridge/product_to_draft.py 的 _plan_intro_clips）：
   全屏底色 + 橙底品牌块 + 最多两行大标题 + 「本期看点」+ 顶部 17% 半透明黑幕。
   全屏底色是必要的：底层场景卡片会同时画大字，没有底就会被穿透叠字（实测 0.4s 重叠）。
   数据来自 USER_INPUT.openingCard（由 app/smart_packaging.py 写入 settings）；
   缺字段或旧实例没有该节点时整个层不创建，旧产物行为不变。 */
(function () {
  let layer = null, curtain = null, cfg = null;
  window.initOpening = function () {
    cfg = (window.USER_INPUT || {}).openingCard;
    if (!cfg || !cfg.lines || !cfg.lines.length) return;
    const W = CONFIG.width, H = CONFIG.height;
    const theme = (window.USER_INPUT || {}).theme || {};
    const bg = (theme.colors && theme.colors.bg) || '#131110';
    layer = document.createElement('div');
    layer.style.cssText = 'position:absolute;inset:0;z-index:78;opacity:0;pointer-events:none;';
    const backdrop = document.createElement('div');
    backdrop.style.cssText = `position:absolute;inset:0;background:${bg};`;
    layer.appendChild(backdrop);
    if (cfg.brand) {                                   // 品牌块：橙底白字（#F5822A）
      const pill = document.createElement('div');
      pill.textContent = cfg.brand;
      pill.style.cssText = `position:absolute;left:${W * .06}px;top:${H * .038}px;`
        + `font:900 ${Math.round(W * .030)}px 'NotoSC';color:#fff;background:#F5822A;`
        + `padding:${H * .006}px ${W * .028}px;border-radius:99px;white-space:nowrap;`;
      layer.appendChild(pill);
    }
    if (cfg.subtitle) {
      const sub = document.createElement('div');
      sub.textContent = cfg.subtitle;
      sub.style.cssText = `position:absolute;left:${W * .06}px;top:${H * .27}px;`
        + `font:700 ${Math.round(W * .030)}px 'NotoSC';color:rgba(255,255,255,.85);`
        + 'letter-spacing:' + Math.round(W * .004) + 'px;white-space:nowrap;';
      layer.appendChild(sub);
    }
    const title = document.createElement('div');
    title.textContent = cfg.lines.join('\n');
    const stroke = Math.max(2, Math.round(W * .004));
    title.style.cssText = `position:absolute;left:${W * .06}px;top:${H * .34}px;width:${W * .90}px;`
      + `font:900 ${Math.round(W * .062)}px 'NotoSC';color:#fff;line-height:1.22;white-space:pre-line;`
      + `-webkit-text-stroke:${stroke}px rgba(0,0,0,.8);paint-order:stroke fill;`;
    layer.appendChild(title);
    document.body.appendChild(layer);
    curtain = document.createElement('div');
    curtain.style.cssText = `position:absolute;left:0;right:0;top:0;height:${H * .17}px;`
      + 'background:rgba(0,0,0,.45);z-index:76;opacity:0;pointer-events:none;';
    document.body.appendChild(curtain);
  };
  window.OVERLAY_OPENING = function (t) {
    if (!layer) return;
    const sec = cfg.seconds || 3;
    const fadeIn = pr(t, 0, .35);
    const fadeOut = t >= sec - .45 ? pr(t, sec - .45, sec) : 0;
    const opacity = Math.min(fadeIn, 1 - fadeOut);
    layer.style.opacity = opacity.toFixed(3);
    curtain.style.opacity = (opacity * .5).toFixed(3);
    layer.style.display = t < sec ? 'block' : 'none';
    curtain.style.display = t < sec ? 'block' : 'none';
  };
})();
