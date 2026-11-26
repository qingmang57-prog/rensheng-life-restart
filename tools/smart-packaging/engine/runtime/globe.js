/* globe.js — 点阵投影与地球连线（深空舞台的"假 3D"全套实现）
   移植自 motiontemplates/385712 kinetic-tech open-beta v0.2.7 core/core.js L392-448
   （MIT License）。保真改造点：画布尺寸/中心改为 CONFIG 驱动；LAND 数据点阵
   沿用 core/data/land.js（同源 MIT），须在 HTML 中先引入 land.js。 */
function gproj(lon, lat, rot, tilt) {          // 球面投影：经纬度 → 旋转倾斜后的 [x,y,z]
  const la = lat * Math.PI / 180, lo = lon * Math.PI / 180 + rot;
  let x = Math.cos(la) * Math.sin(lo), y = -Math.sin(la), z = Math.cos(la) * Math.cos(lo);
  const y2 = y * Math.cos(tilt) - z * Math.sin(tilt), z2 = y * Math.sin(tilt) + z * Math.cos(tilt);
  return [x, y2, z2];
}
function globeDraw(c, t, o) {
  const W = window.CONFIG ? CONFIG.width : 1920, H = window.CONFIG ? CONFIG.height : 1080;
  const cx = o.cx == null ? W / 2 : o.cx, cy = o.cy == null ? H / 2 : o.cy;
  const R = o.R || Math.min(W, H) * .36, rot = o.rot || 0,
        tilt = o.tilt == null ? -.35 : o.tilt, op = o.o == null ? 1 : o.o;
  if (op <= .002) return;
  // 球体径向底光
  const g = c.createRadialGradient(cx - R * .3, cy - R * .35, R * .1, cx, cy, R * 1.25);
  g.addColorStop(0, `rgba(125,178,255,${.10 * op})`); g.addColorStop(1, 'rgba(125,178,255,0)');
  c.fillStyle = g; c.beginPath(); c.arc(cx, cy, R * 1.25, 0, 6.2832); c.fill();
  // 经纬网格（4° 步进，背面剔除 z<0）
  for (let lat = -60; lat <= 60; lat += 30) {
    c.beginPath(); let first = true;
    for (let lon = -180; lon <= 180; lon += 4) {
      const [x, y, z] = gproj(lon, lat, rot, tilt); if (z < 0) { first = true; continue; }
      const X = cx + x * R, Y = cy + y * R; first ? c.moveTo(X, Y) : c.lineTo(X, Y); first = false;
    }
    c.strokeStyle = `rgba(125,178,255,${.12 * op})`; c.stroke();
  }
  for (let lon = -180; lon < 180; lon += 30) {
    c.beginPath(); let first = true;
    for (let lat = -88; lat <= 88; lat += 4) {
      const [x, y, z] = gproj(lon, lat, rot, tilt); if (z < 0) { first = true; continue; }
      const X = cx + x * R, Y = cy + y * R; first ? c.moveTo(X, Y) : c.lineTo(X, Y); first = false;
    }
    c.strokeStyle = `rgba(125,178,255,${.10 * op})`; c.stroke();
  }
  // 陆地点云（深度驱动透明度与点径）
  const L = window.LAND || [];
  for (let i = 0; i < L.length; i++) {
    const [x, y, z] = gproj(L[i][0], L[i][1], rot, tilt); if (z < -.05) continue;
    const a = (.25 + z * .75) * op, r = 1.1 + z * 1.4;
    c.fillStyle = `rgba(205,220,240,${a.toFixed(3)})`;
    c.fillRect(cx + x * R - r / 2, cy + y * R - r / 2, r, r);
  }
  // 大圆连线弧（进度 p 描边，弧顶隆起 h=1+sin(u·π)·.28，端点辉光）
  (o.arcs || []).forEach(([lo1, la1, lo2, la2, p, col]) => {
    if (p <= 0) return;
    const N = 60, pts = [];
    for (let i = 0; i <= N * Math.min(1, Math.max(0, p)); i++) {
      const u = i / N, lon = lo1 + (lo2 - lo1) * u, lat = la1 + (la2 - la1) * u,
            h = 1 + Math.sin(u * Math.PI) * .28;
      const [x, y, z] = gproj(lon, lat, rot, tilt);
      pts.push([cx + x * R * h, cy + y * R * h, z]);
    }
    c.strokeStyle = col || '245,185,46'; c.lineWidth = 3;
    c.shadowColor = col || '245,185,46'; c.shadowBlur = 14;
    c.beginPath(); pts.forEach((q, i) => i ? c.lineTo(q[0], q[1]) : c.moveTo(q[0], q[1])); c.stroke();
    c.shadowBlur = 0;
    const h = pts[pts.length - 1];
    if (h && window.FX) FX.glow(c, h[0], h[1], 26, '255,214,120', op);
  });
  // 地点 pin（实点 + 声纳扩散环：t*30 % 22）
  (o.pins || []).forEach(([lon, lat, lab, col, k]) => {
    const [x, y, z] = gproj(lon, lat, rot, tilt); if (z < 0) return;
    const X = cx + x * R, Y = cy + y * R, a = (k == null ? 1 : k) * op;
    c.fillStyle = col || '245,185,46'; c.globalAlpha = a;
    c.beginPath(); c.arc(X, Y, 7, 0, 6.2832); c.fill();
    c.strokeStyle = col || '245,185,46'; c.lineWidth = 1.5;
    c.beginPath(); c.arc(X, Y, 7 + (t * 30 % 22), 0, 6.2832);
    c.globalAlpha = a * (1 - (t * 30 % 22) / 22); c.stroke(); c.globalAlpha = 1;
  });
}
window.GLOBE = { gproj, globeDraw };
