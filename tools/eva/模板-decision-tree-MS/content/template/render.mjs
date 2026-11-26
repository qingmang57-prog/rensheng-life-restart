// 决策树流程图 · HyperFrames 视觉核心 · 独立帧泵
// 以 模板-竖屏锐金-MS/content/template/render.mjs 为样板（133 行），本包改动仅 4 处：
//   ① require('playwright') 失败时回退 require('playwright-core')（离线打样机只有 motion-kit/node_modules/playwright-core）
//   ② 默认画幅 1920x1080（上游模板为竖屏 1080x1920）
//   ③ 默认时长 10s（decision-tree 上游定长）
//   ④ 顶部注释与标题
// 用法：
//   node render.mjs --preview --times 1,5,9 --out p{t}.png
//   node render.mjs --params params.demo.json --out demo.mp4 [--scale 1] [--dur 10] [--fps 30]
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { once } from 'node:events';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require('playwright')); }
catch { ({ chromium } = require('playwright-core')); }  // 改动①

const args = process.argv.slice(2);
const val = k => (args.includes(k) ? args[args.indexOf(k) + 1] : undefined);
const root = path.resolve(path.dirname(decodeURIComponent(new URL(import.meta.url).pathname)).replace(/^\/([A-Za-z]:)/, '$1'));
const FPS = +(val('--fps') ?? 30);
const SCALE = +(val('--scale') ?? 0.5);
const W = Math.round(+(val('--w') ?? 1920) * SCALE), H = Math.round(+(val('--h') ?? 1080) * SCALE);  // 改动②
let DUR_OVERRIDE = null;
const DUR = () => +(val('--dur') ?? DUR_OVERRIDE ?? 10);  // 改动③

const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.mp4': 'video/mp4',
  '.ttf': 'font/ttf', '.otf': 'font/otf', '.woff2': 'font/woff2', '.svg': 'image/svg+xml' };

const server = http.createServer((req, res) => {
  try {
    const pn = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
    if (pn === '/favicon.ico') return res.writeHead(204).end();
    const file = path.resolve(root, '.' + pn);
    if (!file.startsWith(root + path.sep) || !fs.statSync(file).isFile()) return res.writeHead(404).end();
    const stat = fs.statSync(file);
    const range = req.headers.range;
    res.setHeader('Content-Type', types[path.extname(file).toLowerCase()] || 'application/octet-stream');
    if (range && /\.(mp4|webm|mov)$/i.test(file)) {
      const m = range.match(/bytes=(\d*)-(\d*)/);
      const start = m[1] ? +m[1] : 0, end = m[2] ? +m[2] : stat.size - 1;
      res.writeHead(206, { 'Content-Range': `bytes ${start}-${end}/${stat.size}`,
        'Accept-Ranges': 'bytes', 'Content-Length': end - start + 1 });
      fs.createReadStream(file, { start, end }).pipe(res);
    } else {
      res.setHeader('Accept-Ranges', 'bytes');
      fs.createReadStream(file).pipe(res);
    }
  } catch { res.writeHead(404).end(); }
});
server.listen(0, '127.0.0.1');
await once(server, 'listening');

let browser, enc;
const errors = [];
const T0 = Date.now();
const step = m => console.log(`[T+${((Date.now() - T0) / 1000).toFixed(1)}s] ${m}`);

try {
  const execPath = process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
  browser = await chromium.launch({ headless: true, executablePath: execPath,
    protocolTimeout: 900000,
    args: ['--force-color-profile=srgb', '--disable-gpu', '--disable-dev-shm-usage',
      '--font-render-hinting=none', '--disable-lcd-text'] });
  const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
  const paramsFile = val('--params');
  let up = null;
  if (paramsFile) {
    up = JSON.parse(fs.readFileSync(path.resolve(paramsFile), 'utf8'));
    if (up.duration && !val('--dur')) DUR_OVERRIDE = up.duration;
    await page.addInitScript(v => { window.USER_PARAMS = v; }, up);
  }
  page.on('pageerror', e => errors.push('PE:' + (e.message || '').slice(0, 300)));
  page.on('console', m => { const tx = m.text() || ''; if (m.type() === 'error' && !/favicon/i.test(tx)) errors.push('CE:' + tx.slice(0, 200)); });

  step('goto');
  await page.goto(`http://127.0.0.1:${server.address().port}/index.html?w=${W}&h=${H}`, { waitUntil: 'domcontentloaded' });
  step('dom ready');
  await page.evaluate(async () => await window.CONSUMER_READY);
  step('consumer ready');
  await page.evaluate(async () => await document.fonts.ready);
  step('fonts ready');
  if (SCALE !== 1) await page.evaluate(s => { document.getElementById('stage').style.transform = `scale(${s})`; }, SCALE);
  if (errors.length) throw Error(errors.join('\n'));

  if (args.includes('--preview')) {
    const times = (val('--times') || String(+(val('--time') ?? 1))).split(',').map(Number);
    for (const t of times) {
      await page.evaluate(async t => { await window.renderAt(t); if (window.hostSeek) await window.hostSeek(t); }, t);
      await page.evaluate(async () => {
        await document.fonts.ready;
        await Promise.all([...document.images].map(i => (i.complete ? 0 : new Promise(r => { i.onload = r; i.onerror = r; }))));
        await new Promise(r => setTimeout(r, 150));
      });
      const base = val('--out') || 'preview.png';
      const out = path.resolve(times.length > 1 ? base.replace(/\.png$/, `-${t}.png`) : base);
      await page.screenshot({ path: out });
      step('preview saved: ' + out);
    }
  } else {
    const out = path.resolve(val('--out') || 'demo.mp4');
    if (fs.existsSync(out) && !args.includes('--force')) throw Error('输出已存在（加 --force 覆盖）：' + out);
    const gamma = 'format=rgb24,colorspace=ispace=bt709:irange=pc:iprimaries=bt709:itrc=srgb:' +
      'space=bt709:range=tv:primaries=bt709:trc=bt709:format=yuv420p,sidedata=mode=delete';
    enc = spawn('ffmpeg', ['-v', 'error', '-y', '-f', 'image2pipe', '-vcodec', 'mjpeg',
      '-framerate', String(FPS), '-i', 'pipe:0', '-vf', gamma, '-c:v', 'libx264',
      '-preset', 'veryfast', '-crf', val('--crf') || '19', '-pix_fmt', 'yuv420p',
      '-frames:v', String(Math.round(DUR() * FPS)), '-color_primaries', 'bt709', '-color_trc', 'bt709',
      '-colorspace', 'bt709', '-color_range', 'tv', '-movflags', '+faststart', out],
      { stdio: ['pipe', 'ignore', 'pipe'] });
    let log = '';
    enc.stderr.on('data', d => { log += d; if (log.length < 800) console.error('FFERR:', String(d).slice(0, 300)); });
    const finish = once(enc, 'exit');
    const count = Math.round(DUR() * FPS);
    for (let f = 0; f < count; f++) {
      await page.evaluate(async t => { await window.renderAt(t); if (window.hostSeek) await window.hostSeek(t); }, f / FPS);
      if (errors.length) { console.error('PAGE ERRORS:', JSON.stringify(errors)); throw Error(errors.join('\n')); }
      let image = null;
      for (let a = 0; a < 3 && !image; a++) {
        try { image = await page.screenshot({ type: 'jpeg', quality: 94 }); }
        catch (e) { console.error('shot retry', a + 1); await new Promise(r => setTimeout(r, 600)); }
      }
      if (!image) throw Error('screenshot failed');
      if (!enc.stdin.write(image)) await once(enc.stdin, 'drain');
      if (f % 150 === 0) console.log(JSON.stringify({ frame: f, total: count }));
    }
    enc.stdin.end();
    const [c] = await finish;
    if (c !== 0) throw Error(log);
    step('video saved: ' + out);
  }
} finally {
  if (enc && !enc.killed && enc.exitCode === null) enc.kill('SIGTERM');
  if (browser) await browser.close();
  server.close();
}
