/* beats.js — 节拍总线 v2（P0.5：接 analyze_beats.py 产物 [[t,isDown]] + hits）
   四路消费：卡片切换 snap / 字幕高亮 / 闪环 / SFX 触发，全部量化到同一网格。 */
window.BEATS = ((window.KINETIC_INPUT || {}).beatsData || {}).beats || [];
window.HITS = ((window.KINETIC_INPUT || {}).beatsData || {}).hits || [];
window.beatIdx = t => {           // 二分：当前拍序号
  let lo = 0, hi = BEATS.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (BEATS[m][0] <= t) lo = m + 1; else hi = m; }
  return lo - 1;
};
window.beatPulse = t => {         // 拍强 0..1：downbeat 满格，间拍半格，随时间衰减
  const i = beatIdx(t);
  if (i < 0) return 0;
  const [tb, down] = BEATS[i], d = t - tb, beat = (BEATS[i + 1] ? BEATS[i + 1][0] - tb : .5);
  return (down ? 1 : .5) * Math.max(0, 1 - d / beat * 1.6);
};
window.snap = (t, onlyDown = false) => {   // 吸附最近拍（卡片切换用）
  let best = t, d = 9;
  for (const b of BEATS) {
    if (onlyDown && !b[1]) continue;
    const e = Math.abs(b[0] - t); if (e < d) { d = e; best = b[0]; }
  }
  return best;
};
window.nextHit = t => { for (const h of HITS) if (h[0] > t) return h[0]; return Infinity; };
window.hitPulse = t => {          // hit 点闪环信号：hit 后 0.18s 内为 1→0
  for (const h of HITS) { const d = t - h[0]; if (0 <= d && d < .18) return 1 - d / .18; }
  return 0;
};
