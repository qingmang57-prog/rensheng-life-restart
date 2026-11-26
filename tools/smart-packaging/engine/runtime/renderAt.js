// 织梦包装引擎 · 帧时钟运行时（结构移植自 motiontemplates/385712 open-beta MIT，已改造）
// 原文：core/main.js；改造点：①相机锚点参数化（9:16/16:9）②场景接口不变 ③GLOBAL/OVERLAY 挂 ours.js
(function () {
  const Q = new URLSearchParams(location.search);
  window.END = CONFIG.end;
  // 每帧入口：t = 输出时钟秒
  window.renderAt = function (t) {
    for (const id of ['fxb', 'fxc', 'fxt']) {           // 三层特效画布逐帧清屏（坑㉓），防全帧渲染叠影
      const c = document.getElementById(id);
      c.getContext('2d').clearRect(0, 0, c.width, c.height);
    }
    const active = SCENES.filter(sc => t >= sc.s - 1e-4 && t < sc.e);
    for (const sc of SCENES) {
      const on = active.includes(sc);
      sc.el.style.display = on ? 'block' : 'none';
      if (sc.top) sc.top.style.display = on ? 'block' : 'none';
    }
    let cam = null;
    for (const sc of active) { sc.update(t); const c = sc.cam(t); if (c) cam = c; }
    const w = document.getElementById('world');
    if (cam) {
      const ax = CONFIG.width / 2, ay = CONFIG.height / 2;
      w.style.transform =
        `translate(${(cam.sx || 0).toFixed(2)}px,${(cam.sy || 0).toFixed(2)}px)` +
        ` scale(${(cam.z || 1).toFixed(4)}) rotate(${(cam.r || 0).toFixed(3)}deg)` +
        ` translate(${(ax - (cam.x || ax)).toFixed(2)}px,${(ay - (cam.y || ay)).toFixed(2)}px)`;
    } else w.style.transform = '';
    window.CAM = cam || {};
    window.GLOBAL && window.GLOBAL(t);
    document.getElementById('fade').style.opacity =
      Math.min(1, Math.max(0, (t - (END - .5)) / .45)).toFixed(3);
    window.OVERLAY && window.OVERLAY(t);
    window.OVERLAY_OPENING && window.OVERLAY_OPENING(t);
  };
  window.READY = (async () => {
    await document.fonts.ready;
    await Promise.all((CONFIG.fontChecks || []).map(f => document.fonts.load(f, '字ABC0123')));
    await Promise.all([...document.images].map(im => im.complete ? 0 :
      new Promise(r => { im.onload = im.onerror = r; })));
    document.title = 'done';
  })();
})();
