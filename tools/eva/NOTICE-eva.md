# tools/eva 许可与来源说明（NOTICE）

本目录的「MS」版式分三类：**开源**、**主理人自研（可封云端）**、**非自研/归属待确认（不封，留本地）**。
判断依据：每套的 `ms-meta.json` / `eva-meta.json`、源码头部注释，以及主理人 2026-11-17 的口头澄清。

## 一、开源来源（不得移除、不得私自封闭分发）

以下 22 套来自开源项目 `开源-eva-video-style` 的 `template/demo/`；每套 `ms-meta.json` 的 `source=oss`/`origin` 记录出处。
分发时必须保留本 NOTICE 与同目录 `LICENSE-EVA.txt`（MIT，Copyright (c) 2026 RoverEva）。

| 目录 | slug | 上游 origin | 画幅 |
|---|---|---|---|
| 模板-开源T01-MS | T01 | `开源-eva-video-style/template/demo/t01` | 16:9 |
| 模板-开源T02-MS | T02 | `开源-eva-video-style/template/demo/t02` | 16:9 |
| 模板-开源T03-MS | T03 | `开源-eva-video-style/template/demo/t03` | 16:9 |
| 模板-开源T04-MS | T04 | `开源-eva-video-style/template/demo/t04` | 16:9 |
| 模板-开源T05-MS | T05 | `开源-eva-video-style/template/demo/t05` | 16:9 |
| 模板-开源T06-MS | T06 | `开源-eva-video-style/template/demo/t06` | 16:9 |
| 模板-开源T07-MS | T07 | `开源-eva-video-style/template/demo/t07` | 16:9 |
| 模板-开源T08-MS | T08 | `开源-eva-video-style/template/demo/t08` | 16:9 |
| 模板-开源T09-MS | T09 | `开源-eva-video-style/template/demo/t09` | 16:9 |
| 模板-开源T10-MS | T10 | `开源-eva-video-style/template/demo/t10` | 16:9 |
| 模板-开源T11-MS | T11 | `开源-eva-video-style/template/demo/t11` | 16:9 |
| 模板-开源T12-MS | T12 | `开源-eva-video-style/template/demo/t12` | 16:9 |
| 模板-开源T13-MS | T13 | `开源-eva-video-style/template/demo/t13` | 16:9 |
| 模板-开源T14-MS | T14 | `开源-eva-video-style/template/demo/t14` | 16:9 |
| 模板-开源T15-MS | T15 | `开源-eva-video-style/template/demo/t15` | 16:9 |
| 模板-开源T16-MS | T16 | `开源-eva-video-style/template/demo/t16` | 16:9 |
| 模板-开源T17-MS | T17 | `开源-eva-video-style/template/demo/t17` | 16:9 |
| 模板-开源T19-MS | T19 | `开源-eva-video-style/template/demo/t19` | 16:9 |
| 模板-开源T20-MS | T20 | `开源-eva-video-style/template/demo/t20` | 16:9 |
| 模板-开源T21-MS | T21 | `开源-eva-video-style/template/demo/t21` | 16:9 |
| 模板-开源T23-MS | T23 | `开源-eva-video-style/template/demo/t23` | 16:9 |
| 模板-开源T24-MS | T24 | `开源-eva-video-style/template/sample5/t24` | 16:9 |

## 二、主理人自研（**可封云端**，渲染依赖 jd-motion-kit，第三方 GSAP 等留本地）

| 目录 | 名称 | 判定依据 |
|---|---|---|
| 模板-jd-cinema-voice-h1-MS | JD · 影院口播（16:9） | 主理人自研（GPT 制作，`本地包装工作区` → 导入本软件）；渲染走 `tools/jd-motion-kit` 自包含核心（`assistant/motion.mjs` 头部自述 motion-kit self-contained core） |
| 模板-jd-spatial-voice-v1-MS | JD · 空间口播（竖屏） | 主理人自研（同上，竖屏版）；`ms-meta` 自述「motion-kit 自包含核心，已去EVA绿」 |

## 三、非本项目自研 / 归属待确认（**本期不封、留本地**）

| 目录 | 判定 |
|---|---|
| 模板-像素游戏-MS | **基于开源 Eva Video Style 引擎的二创版式（非本项目自研、非主理人所做、无独立许可声明）→ 留本地、随包带本 NOTICE 与 `LICENSE-EVA.txt`，不封云端**。依据：`ms-meta.json` 记 `source=pixel` / `origin=切片工坊/assets/eva_custom/pixel/p01`；`p01/index.html` 引开源 `engine/{engine.css,engine.js,kit.js,land.js,templates.js}`；`pixel/intro.js` 头部自述「Shared timing for the **Eva Video Style demo reel**」；`scenes.js` 头部注明「借鉴 T24 presenter rig」（T24 在开源集内）；`eva_custom/` 下 pixel/maoshu/knowledge/jd-tech-signal 等同批 mtime 2026-11-09 19:02；模板内无任何许可文件 |
| 模板-竖屏学习-MS | eva 自建版式（星空四段骨架）；归属待主理人确认 → 本期不封 |
| 模板-竖屏锐金-MS | eva 自建版式（自述「版式源自学习包装2逐帧档案，配色取新包装风格设计D款」）；归属待确认 → 不封 |
| 模板-观点变形口播-MS | **开源**（随目录带 `content/template/LICENSE-EVA.txt`＝MIT RoverEva）→ 留本地、不封 |
| 模板-硬核解说-MS | eva 自建版式（本地逐帧）；**内附商业/系统字体（见 §四）**；归属待确认 → 不封 |

> 说明：2026-11-17 主理人澄清「我做的不是像素游戏，做完我做了一个」；队长据审计证据裁决：像素游戏＝**开源 Eva 引擎二创**，留本地随包（带 LICENSE/NOTICE），**不封云端**；
> JD 影院/空间口播是主理人 GPT 制作并已导入本软件的两套，改列 §二（可封）。

## 四、字体授权风险（⚠️ 分发前必须处理）

| 字体 | 出现位置 | 许可判定 | 处置建议 |
|---|---|---|---|
| Anton / ArchivoBlack / PlayfairDisplay / SpaceMono | 竖屏学习、竖屏锐金、硬核解说 | **SIL OFL 1.1** | 可随包，保留 OFL 声明 |
| **DINNextLTPro-Bold / -Medium** | 硬核解说（`fonts/` 与 `content/template/fonts/`） | **Monotype 商业字体** | ⚠️ 无购买授权不得再分发 |
| **georgiab.ttf / impact.ttf** | 硬核解说 | **Microsoft 系统字体**（仅随 Windows 使用） | ⚠️ 不得随包分发 |
| HarmonyOS_Sans_SC_Bold | 硬核解说 | 华为 HarmonyOS Sans（商用条款待核） | 待核；无授权则移除 |

> 硬核解说在上述字体处理前**不得进入发行物**；是否保留整套等主理人定。

_本文件由施工 2026-11-17 依据各套实际元数据与主理人澄清生成；仅新增说明，未改动任何模板文件。_
