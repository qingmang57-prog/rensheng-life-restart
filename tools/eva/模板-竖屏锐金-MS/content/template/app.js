/* 竖屏锐金 · 参数注入层（2026-10-31）
 * render.mjs 注入 window.USER_PARAMS，转给 scenes.js 读的 __EVA_P（必须在 scenes.js 之前）。
 */
(function () { window.__EVA_P = window.USER_PARAMS || window.__EVA_P || {}; })();
