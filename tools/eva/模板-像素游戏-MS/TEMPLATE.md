# 像素游戏机 · TEMPLATE.md（交接卡）
Version: 0.1.0（t17 2026-11-19，施工B）｜配套：MOTION_DESIGN.md（创意规格）＋replacement-contract.json（替换契约）

## 这是什么
1920×1080 像素游戏机模板：深夜蓝＋CRT＋三色霓虹，固定四节拍（开屏 PLAYER 1→风格四选→收集品三框→通关 CTA）。**特色＝逐字稿级中文化运行时**（intro.js 后半段）：英文 demo 大字渲染期被逐字稿原句 verbatim 替换，宁少不假。可执行工程在 `content/template/`（intro.js＋scenes.js＋app.js＋engine/＋render.mjs）。

## 怎么换内容（3 步）
1. 备池子（三选一或混用）：
   - 逐字稿 `lines:[{start,end,speak}]`（首选，上屏即口播原句，≤8 字/条自动截断）；
   - 8 个槽位 `s1_h/s1_n/s2_g/s3_g1/s3_g2/s3_g3/s4_h/s4_n`（无 lines 时 fallback）；
   - 证据图 `shots:[3 张竖图路径]`（B3 三相框）。
   参考 `content/template/params.test.json` 起格式。
2. 注入渲染：`node content/template/render.mjs --params my.params.json --out demo.mp4`（`--scale 0.5` 快速预览）；单帧 `--preview --time 5 --out p5.png`（支持 `--times` 列表）。
3. 回归核对：读 `window.__CN_STATS`（replaced>0、violations=0）与 `__CN_MODE`（verbatim 或 slots-fallback）确认中文化生效；确认 host.dir 人物帧在位。

## 字体
- 中文 'Noto Sans CJK SC'（系统回退）；游戏 HUD/编号 'Space Mono'；展示字库 Anton/Archivo Black/Playfair 随包（engine/fonts/＋licenses）。
- 像素感来自**版式与运动量化**（直角硬卡、qstep、pixelated 精灵），不依赖像素字体——若换像素字体须同步放宽行高（现版式按 SANS 行高设计）。

## 已知差距（诚实清单）
- **两套节拍表**：场景实际用 scenes.js 局部 B=[0,.25,.5,.75]×END；intro.js 的 IN.T/IN.B（sc=dur/15.033）供外部/回归，非 15.033s 时两者不一致——以画面为准，别拿 IN.T 对帧。
- **C.face 居中常量硬编**（intro.js:21，540×960 素材基准）：本包不像竖屏包暴露 host.face 覆盖；换非 540×960 素材须改代码（不在文档层解决）。
- brand 槽定义于 IN 但 scenes.js 未消费（挂了不显示）；stage 坐标硬编 1920×1080，不随 --w/--h 缩放（改分辨率会破版式）。
- 无逐字稿且无槽位时英文词全部置空——是**特性不是 bug**（宁少不假），但成片会没字，交付前必查 __CN_STATS。
- 固定四节拍不可增删镜；套用前自问「这内容适合游戏化包装吗」（防模板塑造作品）。

## 可移植检查（复制到别的机器/项目时）
- [ ] 包内相对路径引用完整（content/template/*、engine/fonts/*）；无绝对路径、无外网依赖
- [ ] node＋Playwright(Chromium) 可用；`node render.mjs --preview --time 1 --out p.png` 出图 rc0
- [ ] 喂一份带 speak 的 params 渲一帧：上屏中文∈逐字稿、__CN_STATS.violations=0（注入对照，证明非默认态）
- [ ] `ffprobe -v error -i preview/demo.mp4` 解码 rc0（1920×1080/30fps）
- [ ] engine/fonts 四套 TTF＋licenses 在位

## 泵契约备忘
与竖屏锐金包同代契约：`--preview --time <秒>`｜`--times 列表`｜`--params x.json --out demo.mp4`｜`--scale`｜`--dur`｜`--fps` 默认 30（render.mjs:17-19）。与 warm-grain 泵 `--preview <t>` 位置参数**不同**，别混用。
