# 竖屏锐金 · TEMPLATE.md（交接卡）
Version: 0.1.0（t17 2026-11-19，施工B）｜配套：MOTION_DESIGN.md（创意规格）＋replacement-contract.json（替换契约）

## 这是什么
1080×1920 竖屏带货口播模板：炭黑底＋荧光金，固定四节拍（钩子砸字→标签章节→编号清单→金句圆窗），夜校搞钱系。可执行工程在 `content/template/`（scenes.js＋app.js＋engine/＋render.mjs），本包根三份文档为创意知识包规格层。

## 怎么换内容（3 步）
1. 备内容：把 14 个文案槽按 replacement-contract.json 容量表写好（s1_l1/s1_l2/s1_tag/s2_tag/s2_h/s2_en/s3_h/s3_i1/i2/i3/s4_l1/s4_l2/s4_en/brand），对照字数预算逐槽核对——**模板无字号自适应，超预算＝出血，先改文案**。
2. 注入：复制 `content/template/params.test.json` 为你的 params 文件，替换槽位值（可加 `duration`、`host:{dir,face}`），跑：
   `node content/template/render.mjs --params my.params.json --out demo.mp4`（可加 `--scale 0.5` 快速预览；`--dur` 可覆盖时长）。
   逐帧检查：`node render.mjs --preview --time 5 --out p5.png`（支持 `--times 1,7,12` 列表）。
3. 核对：人物素材放 `host.dir`（540×960 帧序列，t24 居中语义见 MOTION_DESIGN §6）；成片前确认无「默认值文案」残留（缺槽会静默用内置默认，见契约 missing_input_behavior）。

## 字体
- 英文展示字体**随包**：engine/fonts/（Anton、Archivo Black、Playfair、Space Mono TTF＋licenses/ 许可文件）。
- 中文走系统字体 'Noto Sans CJK SC'/'Noto Sans SC' 回退——目标机需装任一，否则中文用系统黑体近似（视觉可接受，字重 900 建议核对）。

## 已知差距（诚实清单）
- **无字号自适应/无溢出缩放**：定字号＋绝对定位，容量靠几何字数预算人工守（契约 capacity_rule）；超限不报错、直接出血。
- 固定四节拍不可增删镜头（SC 只等比拉时间）；叙事结构来自模板，套用前自问「这结构来自本次内容吗」（方法论标准防模板塑造作品）。
- 字幕带 1405 起**外挂**，本模板不画字幕；声音为 sfx 线索清单，成片音频由 scripts/sfx.py 渲染管线合成。
- IN.* 槽位机制是像素游戏包（intro.js）的特性，本包不适用；本包槽位只走 __EVA_P。

## 可移植检查（复制到别的机器/项目时）
- [ ] 全部引用为包内相对路径（content/template/*、engine/fonts/*）；无绝对路径、无外网依赖
- [ ] node＋Playwright(Chromium) 可用；`node render.mjs --preview --time 1 --out p.png` 出图 rc0
- [ ] engine/fonts 四套 TTF 在位且 licenses/ 齐全
- [ ] 预算复核：`ffprobe -v error -i preview/demo.mp4` 解码 rc0（1080×1920/30fps）
- [ ] 槽位注入对照：用改过的 params 渲一帧，画面文案＝你的文案（证明注入生效，非默认值）

## 泵契约备忘
`--preview --time <秒>`（单帧）｜`--times 列表`｜`--params x.json --out demo.mp4`｜`--scale`｜`--dur`｜`--fps` 默认 30（render.mjs:17-19）。与 warm-grain 泵的 `--preview <t>` 位置参数**不同**，别混用。
