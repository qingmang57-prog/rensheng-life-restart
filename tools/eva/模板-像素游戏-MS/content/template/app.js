/* 竖屏学习风 · 参数注入层（2026-10-31）
 * render.mjs 通过 Playwright 注入 window.USER_PARAMS，这里转给 scenes.js 读的 __EVA_P。
 * 必须在 scenes.js 之前加载——scenes.js 是 IIFE，加载即执行并读 window.__EVA_P。
 */
(function () {
  window.__EVA_P = window.USER_PARAMS || window.__EVA_P || {};
})();
