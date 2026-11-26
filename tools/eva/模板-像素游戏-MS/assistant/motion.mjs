#!/usr/bin/env node
/* MS 开源模板通用 CLI：render 时补 shots/duration，转发到母版帧泵。 */
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
const here = path.dirname(decodeURIComponent(new URL(import.meta.url).pathname)).replace(/^\/([A-Za-z]:)/, '$1');
const TPL = path.resolve(here, '../content/template');
const args = process.argv.slice(2);
const val = (k, d) => (args.includes(k) ? args[args.indexOf(k) + 1] : d);
const pf = path.resolve(val('--params'));
const P = JSON.parse(fs.readFileSync(pf, 'utf8'));
P.duration = +P.duration || 15.033;
// host.dir 相对 content/template（帧泵 http 根），扫描帧目录拼配图位
let shots = [];
const dir = (P.host && P.host.dir) ? path.resolve(TPL, P.host.dir) : '';
if (dir && fs.existsSync(dir)) {
  const fs_ = fs.readdirSync(dir).filter(f => /^f_\d+\.jpg$/.test(f)).sort();
  const step = Math.max(1, Math.floor(fs_.length / 10));
  shots = fs_.filter((_, i) => i % step === 0).slice(0, 10).map(f => P.host.dir + '/' + f);
}
P.shots = shots;
const npf = path.join(path.dirname(pf), 'params.runtime.json');
fs.writeFileSync(npf, JSON.stringify(P));
const out = path.resolve(process.cwd(), val('--out', 'silent.mp4'));
try { if (fs.existsSync(out)) fs.unlinkSync(out); } catch (e) {}
const env = { ...process.env };
const pkgModules = path.resolve(here, '../../../node/node_modules');
if (!env.NODE_PATH && fs.existsSync(pkgModules))
  env.NODE_PATH = pkgModules;
const pass = ['--params', npf, '--out', out, '--w', '1920', '--h', '1080',
              '--dur', String(P.duration), '--scale', val('--scale', '1'), '--fps', '30'];
const r = spawnSync(process.execPath, [path.join(TPL, 'render.mjs'), ...pass],
                    { stdio: 'inherit', env, cwd: process.cwd() });
process.exit(r.status ?? 1);
