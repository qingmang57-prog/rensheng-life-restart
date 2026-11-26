/* bootstrap.js — 实例启动：CONFIG 装配 + 场景装配 + 就绪信号
   垂直切片：仅装配 keyword 控件的场景，其余控件在 P0.4 后按同模式注册。 */
(function () {
  const W = window.USER_INPUT.width || 1080, H = window.USER_INPUT.height || 1920;
  window.CONFIG = { brand: 'DREAMWEAVER', account: window.USER_INPUT.presenterLabel || '织梦者',
    fps: 60, width: W, height: H, end: window.USER_INPUT.duration,
    fontChecks: ['900 40px NotoSC', '40px Anton'] };
  window.CONSUMER_READY = (async () => {
    FX.fxInit(document);
    const segs = window.USER_INPUT.segments || [];
    for (const seg of segs) {
      if (seg.control === 'keyword' && typeof keywordScene === 'function') keywordScene(null, seg);
    }
    await document.fonts.ready;
    await Promise.all((CONFIG.fontChecks || []).map(f => document.fonts.load(f, '字ABC0123')));
    await window.imgWait();
    document.title = 'done';
  })();
})();
