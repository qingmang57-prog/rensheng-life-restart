/* user-runtime.js — 实例运行时注入（adapter prepare 产物侧）
   口播帧源 / 人脸几何 / 媒体路径解析。签名对齐上游 core/user-runtime.js。 */
;(function () {

const TEXT_GLOBAL = s => String(s ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
window.resolveMedia = src => src;
(function () {
  const pres = (window.USER_INPUT.media || {}).presenter || {};
  const seq = pres.sequence || { dir: 'sc/presenter', fps: 12, frames: 0 };
  const face = pres.face || { cx: .5, cy: .4, h: .3 };
  const rows = pres.tracking || [];
  /* 人脸跟踪（上游 user-runtime.js 同款）：tracking 行 {time,cx,cy,h}（720×1280 裁后坐标），
     时间轴 = 输出时钟；相邻行线性插值，无行回退静态锚点。 */
  window.userFace = t => {
    if (!rows.length) return face;
    let i = 0;
    while (i < rows.length - 1 && rows[i + 1].time <= t) i++;
    const a = rows[i], b = rows[Math.min(i + 1, rows.length - 1)];
    const k = b.time > a.time ? Math.min(1, Math.max(0, (t - a.time) / (b.time - a.time))) : 0;
    return { cx: a.cx + (b.cx - a.cx) * k, cy: a.cy + (b.cy - a.cy) * k, h: a.h + (b.h - a.h) * k };
  };
  const pad = i => 'f_' + String(Math.min(seq.frames - 1, Math.max(0, i))).padStart(4, '0') + '.jpg';
  window.userPresenter = t => {
    if (!seq.frames) return 'user-assets/blank.svg';
    const i = Math.floor(t * seq.fps + 1e-6) % seq.frames;
    return seq.dir + '/' + pad(i);
  };
})();

})();
