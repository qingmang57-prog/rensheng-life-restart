# 像素游戏机 · MOTION_DESIGN.md
Version: 0.1.0（创意规格铺包 t17，2026-11-19，施工B 据 scenes.js/intro.js/app.js/kit.js 实测提炼）
Source: tools/eva/模板-像素游戏-MS；视觉核心 content/template/{scenes.js, intro.js, engine/}
规格基准: scenes.js sha256 ea31643a44bfa60d169d316d29492393c87c234b3475cd0a73ef4839e90f984e ／ intro.js sha256 077e542412bcd00deae89a81d3a84e38f8831a556df0188a23bf302ff4ce5d39
方法论: OpenMotion 七层模型（同 diag/方法论样板-观点变形口播-无角标/ 规范），本篇为像素固定节拍模板的比例适配轻规格——固定节拍模板**不套母版重格式**。
> 定位：创意规格层；**创意决定内容**——本模板的叙事语法是「游戏化隐喻」，套用前自问：这次内容适合包装成一局游戏吗（步骤清单/闯关节奏/retro 话题）？不适合就换模板。

## 1. Identity & provenance
- 名称：MS · 像素游戏机（1920×1080 · 15s 四节拍）
- 风格：深夜蓝底＋像素点阵＋CRT 扫描线＋青/黄/品红三色霓虹；硬阴影方块卡＋8-bit 手绘精灵（心/星/币）（scenes.js:1-8 注释与实现一致）
- 出处：origin 切片工坊 assets/eva_custom/pixel/p01（ms-meta.json）；22 款开源/像素接通，默认风格词由逐字稿槽覆盖（ms-meta note）
- 观察状态：preview/demo.mp4 在册；本规格为纯文档新增，不改可执行代码

## 2. Brand Kit（scenes.js:11-14 实测）
- 色板：INK #f4f6ff / CYAN #35e6ff / PINK #ff4d8d / YEL #ffd23f / GRN #7dff6a / DARK #0a0b1e / DIM #9aa3c7
- 底：linear-gradient 165° #12142e→#181b3d 52%→#1e2150（scenes.js:56）＋常驻氛围层：像素点阵＋CRT 扫描线＋漂浮星＋暗角（scenes.js:54-61）
- 字体：SANS 'Noto Sans CJK SC'（中文展示）；MONO 'Space Mono'（游戏 HUD/编号/英文点缀）（scenes.js:13-14）
- 随包字体：engine/fonts/（Anton/Archivo Black/Playfair TTF＋README/licenses）——像素风主要用 SANS/MONO，展示字库随包可用
- 圆角禁令：卡片一律直角＋4px 白边＋10px 硬投影（pcard，scenes.js:50-51）——**圆只留给头像窗 morph 的对比瞬间**（scenes.js:49 注释）

## 3. Narrative grammar（叙事语法：游戏化隐喻）
- 受众经历：**被点名上机（PLAYER 1/INSERT COIN）→ 选技能风格（四张风格卡）→ 收集证据（三张像素相框）→ 通关关注（STAGE CLEAR＋CTA）**。
- 四节拍等分：B=[0, .25, .5, .75]×END（scenes.js:15）→ B1 开屏 / B2 风格四选 / B3 收集品 / B4 通关。**固定节拍＝本模板创意逻辑，不可增删镜**。
- 内容映射：B2 四卡＝4 个并列关键词/风格/卖点（由逐字稿或槽位池供给，见 §8）；B3 三框＝3 张证据图（截图/实例，SHOTS 槽）；B4 大字＝一句行动号召金句。
- 与母版差异：母版 7 版式导演指派；本模板结构固定，**换内容＝换池子文案＋证据图**，结构不可换。

## 4. Visual hierarchy
- B1：像素边框开机屏＋PLAYER 1 大字＋闪烁光标＋血条式进度条（scenes.js:63-115 段）
- B2：LEVEL UP 徽章弹入＋62px 标题＋四张 760×92 风格卡（编号 30px MONO＋34px 文本，交错滑入）（scenes.js:116-139）
- B3：54px 标题＋三张 318×452 像素相框（-1.6°/1.3°/-1.0° 歪斜错落，角标彩点＋编号）（scenes.js:142-165）
- B4：STAGE CLEAR 绿牌＋88px 大字（粉影 textShadow）＋30px FOLLOW 行＋▶ 箭头＋三精灵贺彩（scenes.js:168-196）
- 字阶：88/62/54/34/30px；头像窗全程右上/右中，与左侧文案带不叠。

## 5. Timing & motion（scenes.js/intro.js 实测）
- 帧时钟确定性：renderAt 纯 t 函数；scenes.js/intro.js/engine.js/kit.js grep 无 Date.now/Math.random（实测 0 命中）。
- 时标：dur = P.duration || 15.033（intro.js:3）；END=C.end=dur（intro.js:17）；场景节拍用 scenes.js 局部 B=[0,.25,.5,.75]×END（scenes.js:15）。intro.js 另算 IN.T/IN.B（sc=dur/15.033 精确节拍表，intro.js:5-15）**供外部/回归消费，场景自身以 scenes.js 局部 B 为准**——两套节拍在非 15.033s 时长下不一致，属已知特性（见 TEMPLATE.md）。
- 8-bit 量化：qstep(u,n) 运动一顿一顿（scenes.js:17）；精灵旋转 Math.floor(t*2)%2 二值抖动（scenes.js:137/192）；币上下浮动 floor(t*4)%2（scenes.js:194）。
- 头像窗 morph：方块→圆→小方块→大圆，evaKeys 5 键 outBack 弹一下（scenes.js:20-30；几何 SQ1/CI1/SQ2/CI2 硬编 stage px）＋ringFn 青色辉光脉冲（scenes.js:31-32）。
- 声音线索：k.sfx 登记（riser/impact/pop/ding/whoosh/click/applause），scripts/sfx.py 外渲（kit.js:47-48）——模板不产音频。

## 6. Materials & sound
- 人物：C.evaDir = host.dir || 'media/eva'（intro.js:18）；**C.face 居中常量 {x:.354, y:.364, h:.42} 硬编**（intro.js:19-22，t24 540×960 肤色质心实测，引用 diag/人物居中优化-20261118；srcW/srcH=540/960）——本包**不暴露 host.face 覆盖**，换非 540×960 素材须改 intro.js:21（属代码变更，不在本契约内）。
- 证据图：SHOTS = P.shots（intro.js:24）→ B3 三相框 im.src=SHOTS[i%len]（scenes.js:152）。
- 声音：sfx 线索清单外渲；无字幕层；S5 全局块（intro.js:31-43）供 sample5/t24 消费者 cut(t) 帧寻址（face {x:.5,y:.2,h:.36} 为该消费者专用）。
- 绝不伪造：无证据图留空框、无逐字稿置空文案（见 §8 缺料行为），不拿生成内容冒充。

## 7. Invariants（换内容必须幸存）
- 像素风四件套：CRT 扫描线＋点阵底（scenes.js:54-61）、qstep 运动量化（:17）、canvas 精灵 pixelated（:35-43）、直角硬阴影卡禁圆角（:50-51）
- 夜晚蓝霓虹色板与字阶（§2/§4）；固定 B 四节拍等分（scenes.js:15）
- 头像窗 4 窗 morph choreography＋青色 ring 辉光（scenes.js:20-32）；**1920×1080 stage 坐标硬编**（不随参数缩放，与竖屏包 X()/Y() 机制不同）
- **verbatim 中文化铁律**：上屏中文必须是逐字稿归一化子串，否则置空——宁少不假，绝不翻译、绝不编造（intro.js:46-49/75-78/116）
- C.face 居中常量（intro.js:21）；确定性帧时钟；sfx 线索外渲

## 8. Replaceable inputs（可换内容＋容量契约，intro.js 实测）
**容量机制与竖屏包根本不同：本包容量由 verbatim 中文化运行时的 clip 封顶保护，而非人工字数预算。**
| 输入 | 类型 | 机制（file:line） | 容量 | 溢出行为 |
|---|---|---|---|---|
| duration | number | intro.js:3，默认 15.033 | 建议 12–20s | 节拍 B 等分随 END 拉长 |
| lines[] | 逐字稿 | {start,end,speak/text}，句中点按 25/50/80% 归 4 桶（intro.js:58-63） | 每条上屏 ≤8 字（cap） | clip 标点截断（intro.js:79-87） |
| s1_h/s1_n/s2_g/s3_g1/s3_g2/s3_g3/s4_h/s4_n | 槽位 | 无 lines 时 slots-fallback 分桶（intro.js:65-73） | 同 cap ≤8 字 | 同上 |
| 上屏 cap | 机制 | cap=max(2, min(8, round(英文词 ASCII 长×0.45)))（intro.js:113） | **≤8 字硬顶** | 超长标点截断，不溢出 |
| shots[] | 图片 | B3 三相框 i%len 取（intro.js:24, scenes.js:152） | 3 张，竖图 302×436 | 多于 3 截断，少于 3 循环复用 |
| host.dir | asset_dir | intro.js:18 | 540×960 帧序列 | 缺→回退 'media/eva'（目录不存在则人物窗空） |
| brand | text | IN.BRAND=P.brand（intro.js:28） | ≤10 字 | **定义于 IN 但 scenes.js 未消费**（诚实差距，见 TEMPLATE） |
- 上屏文案去向：英文 demo 词（PLAYER 1/WHY IT FEELS LIKE A GAME 等）由 TreeWalker 逐文本节点判定 isEN 后按语义角色分桶（roleOf，intro.js:97-103）替换为逐字稿句子；每帧幂等补漏＋__CN_STATS 回归出口（intro.js:120-155）。
- **缺料行为**：①无 lines 且 8 槽全空 → 语料空 → pick 返回空串 → 英文文本节点**全部置空**（宁少不假，intro.js:112-116/133），画面剩装饰＋人物窗，不造假文案；②无 shots → 三相框空框（不造假图）；③无 host.dir → 回退默认目录，目录不存在则窗空。**均不阻塞渲染，但成片信息量趋零——交付前必须核对 __CN_STATS.replaced>0 且 shots/host.dir 已供**。

## 9. Review（审查规则）
- 技术层：ffprobe 1920×1080 解码 rc0；泵契约同 render.mjs（--preview --time/--times/--params/--out/--scale/--dur/--fps）；注入对照：喂带 speak 的 params 渲帧，验证上屏中文∈逐字稿（__CN_REPLACED 全过 verbatim、violations=0）——技术通过≠内容验收。
- 体验层：原速听看——游戏化隐喻是否成立（不是硬贴皮）？四张风格卡是否被读完？三张证据图是否看清？STAGE CLEAR 收束是否带行动冲动？
- 契约验收：逐项对 §8 核对池子供给与缺料声明；执行人自测≠独立核验；人眼（青芒）定视觉验收。
