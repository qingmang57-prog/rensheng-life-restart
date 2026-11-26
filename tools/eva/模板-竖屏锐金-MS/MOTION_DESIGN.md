# 竖屏锐金 · MOTION_DESIGN.md
Version: 0.1.0（创意规格铺包 t17，2026-11-19，施工B 据 scenes.js/app.js/render.mjs 实测提炼）
Source: tools/eva/模板-竖屏锐金-MS；视觉核心 content/template/{scenes.js, app.js, engine/}
规格基准: content/template/scenes.js sha256 6b2acba4305000bc09399b9b813ec8a900399d973142da3c317f35eb35f3a2fa
方法论: OpenMotion 七层模型（同 diag/方法论样板-观点变形口播-无角标/ 规范），本篇为竖屏固定节拍模板的比例适配轻规格——固定节拍模板**不套母版重格式**，容量与节拍以本模板实测为准。

> 定位：创意规格层，约束信息完整性；创意决定内容——**每镜文案必须能装进对应字数预算**（本模板无字号自适应，见 §8），超预算先改文案再渲染。

## 1. Identity & provenance
- 名称：竖屏锐金（1080×1920 竖屏带货口播）
- 风格：炭黑底 + 荧光金 #FFD400 · 夜校搞钱系（新包装风格设计 D 款）· 学习包装2 版式原语
- 分带（scenes.js:8-9 注释实测）：角标 90–150 / 标题 200–366 / 窗口 380–1304 / 说明 1130–1360 / 字幕带 1405 起（**外挂，模板不画**）
- 观察状态：preview/demo.mp4 在册（1080×1920，2026-11-18 产出）；本规格为纯文档新增，不改可执行代码

## 2. Brand Kit（scenes.js:24-28 实测）
- 色板：GOLD #FFD400 / HOT #FF1F2E / INK #F5F5F0 / DIM #7E8794 / LINE #2A313C
- 底：radial-gradient #1A2028→#0C1014 62%→#080A0E（scenes.js:25）+ 每场暗角 vig（scenes.js:78-81）
- 字体：CN 'Noto Sans CJK SC','Noto Sans SC'；EN 'Anton','Archivo Black'；MONO 'Space Mono'（scenes.js:26-28）
- 随包字体：engine/fonts/（Anton/Archivo Black/Playfair/Space Mono TTF + licenses/）——英文展示字体已随包，CJK 走系统回退
- 常驻系统感：左上品牌 chip + 右上时间码 hud（scenes.js:59-70），每场复挂 → 换品牌只改 brand 槽

## 3. Narrative grammar（叙事语法：带货口播四节拍）
- 受众：短视频带货/科普观众。固定叙事弧：**钩子砸字（断言）→ 标签章节（设问）→ 编号清单（证据 3 条）→ 金句屏（传播点收束）**。
- 固定节拍＝本模板的创意逻辑（拖课堂节奏）：S1 0–3.22s → S2 3.22–7.02s → S3 7.02–11.55s → S4 11.55–15.1s（scenes.js:84/118/156/199），全部节拍乘 SC 随 duration 等比缩放（scenes.js:14）。
- 内容映射：S1 放最强断言（≤13 字/行）；S2 放设问/悬念；S3 放 3 条并列证据（要点句）；S4 放可截图传播的金句对（白+金两行）。
- 与母版差异：母版 7 版式由导演按语义指派；本模板四节拍固定不可增删——**换内容≠换结构**，只换槽位文案与人物素材。

## 4. Visual hierarchy
- 每镜一个视觉重心：S1 两行巨字（78px 白+末 2 字金+红残影，scenes.js:88-99）→ S2 章节标签+巨字+英文行（26/64/24px，scenes.js:122-137）→ S3 三条目红底编号方块+42px 要点+发丝线（scenes.js:164-181）→ S4 金句卡：上金线+两行大字（92px 白/104px 金）+下金线+英文行（scenes.js:203-224）。
- 人物窗与文案分带不叠：窗 380–1304（S3 缩小到 380–1091 给清单让位），金句场缩为右下 260×260 圆窗（scenes.js:44-48）。
- 字阶（F()＝1080 宽等比）：78/64/54/42/26/24/22/92/104 px。

## 5. Timing & motion（scenes.js 实测）
- 帧时钟确定性：renderAt 为 t 纯函数；grep 全包 scenes.js/engine/{engine,kit}.js 无 Date.now/Math.random（实测 0 命中）。
- SC 时标：SC = duration/15（duration>1 才生效，scenes.js:14）；全部节拍/evaKeys/sfx 时刻乘 SC；几何 X()/Y()/F() 按 1080/1920 等比（scenes.js:18-20）→ **改 duration 不破版式，只拉时间**。
- 砸字公式：白字 + 末 2 字染金（scenes.js:89）+ 红色残影错位撞击收拢（ghost，scenes.js:90-99/112）。
- 擦入/升入：S2 标签 clipPath 左→右擦入（scenes.js:148）、清单 outExpo 滑入+线拉出（scenes.js:188-193）、S4 文字等圆窗落位后 rise 升入（scenes.js:230-233）。
- 声音线索：k.sfx 仅登记（impact/pop/whoosh/riser + 频率参数），渲染期 scripts/sfx.py 外部合成（kit.js:47-48）——模板不产音频。
- → 改 duration 只动 SC；砸字/擦入/逐条 pop 的相对节奏为不变量，禁止为省时删动效。

## 6. Materials & sound
- 必需输入：host.dir 人物视频帧序列目录（C.evaDir，scenes.js:33）。**唯一缺料影响点**：无 host.dir → evaKeys 回退单 hero 窗且 o:0 隐藏（scenes.js:54），文字版式照常——人物缺料**不阻塞渲染**，但成片无人物，交付前须声明。
- 人物居中语义（t24，scenes.js:34-40 实测注释）：素材 540×960 肤色质心 (.354,.384)；C.face 默认 {x:.354,y:.344,h:.45}，headFill .62；**覆盖语义下窗≈图同尺寸（cover0），left/top 被 clamp 吞 face 偏移——必须配窗 zoom**。
- 声音：模板只登记 sfx 线索清单，成片声音由渲染管线（scripts/sfx.py）合成叠加；无字幕层（1405 带外挂）。
- 绝不伪造缺失素材：无人物帧不拿生成图冒充；无逐条证据文案不硬凑三条（可留默认值渲染 demo，交付须标注）。

## 7. Invariants（换内容必须幸存）
- 1080×1920 分带几何与四场版式（砸字/章节/清单/金句圆窗）
- 夜校色板与字阶（§2/§4）；EN/MONO 展示字体（随包）
- 固定四节拍相对比例（SC 缩放不删节）；确定性帧时钟
- 人物窗三窗 choreography：hero win(520,s=1.3) → small win(400,s=1.13) → quote 圆窗 s=1（scenes.js:44-48）；**zoom cap 语义**：hero s=1.3+h.45 放宽 cap（需 h≤.471）、small s=1.13 为 cap 上限、quote cap=1 压制 zoom（scenes.js:36-39）
- 砸字公式（白+末 2 字金+红残影）；清单红底编号方块；金句双金线
- 常驻 chip+hud；字幕外挂不画；sfx 线索外渲

## 8. Replaceable inputs（可换内容＋容量契约，scenes.js txt() 逐槽实测）
**本模板无字号自适应、无溢出缩放**（全文无 fit/scale 逻辑，k.cn 系定字号——kit.js 实测）→ 容量＝几何字数预算（CJK 全角≈字号宽，画布宽 1080）；**超预算＝出血裁切/换行溢出，必须拆句守预算**。
| 槽位 | 版式（file:line） | 字数预算 | 溢出行为 |
|---|---|---|---|
| s1_l1 / s1_l2 | 78px 两行居中（:88-99） | ≤13 字/行，共 2 行 | 出血；l2 末 2 字染金需 ≥2 字 |
| s1_tag | 26px 金 chip 居中（:100-105） | ≤8 字 | chip 随内容变宽 |
| s2_tag | 26px nowrap 左标签（:122-128） | ≤12 字 | nowrap 溢出出血 |
| s2_h | 64px 左对齐（:129-133） | ≤14 字 | 出血 |
| s2_en | 24px EN letterSpacing6（:134-137） | ≤24 ASCII | 出血 |
| s3_h | 54px 居中（:159-162） | ≤20 字 | 出血 |
| s3_i1..i3 | 42px 左 200 起（:164-176） | ≤20 字/条 | 出血 |
| s4_l1 | 92px 居中（:207-211） | ≤11 字 | 出血 |
| s4_l2 | 104px 金 居中（:212-216） | ≤10 字 | 出血 |
| s4_en | 26px EN letterSpacing10（:217-220） | ≤22 ASCII | 出血 |
| brand | 22px chip（:60-65） | ≤10 字 | chip 变宽 |
| duration | >1 生效（:14） | 建议 10–30s | 全节拍 SC 等比拉长 |
| host.dir | 帧序列目录（:33） | 帧覆盖 0–end | 缺→人物窗隐藏（:54） |
| host.face | {x,y,h} 单点（:40） | hero 需 h≤.471 | 越限 zoom cap 失效窗内偏移 |
- 全部文本槽有内置默认值（txt(key,dft)，scenes.js:15）→ 缺参数不阻塞渲染，出 demo 但内容为默认值，**交付前必须核对非默认**。

## 9. Review（审查规则）
- 技术层：ffprobe 1080×1920/30fps 解码 rc0；泵契约 render.mjs `--preview --time <s> --out p.png`／`--times 列表`／`--params params.demo.json --out demo.mp4 [--scale] [--dur]`（render.mjs:3-4/17/19/84）；USER_PARAMS 经 addInitScript 注入（render.mjs:68）→ app.js 转 __EVA_P（app.js 实测）。
- 体验层（技术通过≠内容验收）：原速听看——砸字撞击感是否成立？三条清单是否被逐条「读」到？金句两行是否值得截图传播？人物窗缩放节奏（大→小→圆）是否服务叙事？
- 容量验收：逐槽对 §8 字数预算核对；超预算回炉改文案，不允许「渲染出来看运气」。
- 执行人自测≠独立核验；人眼（青芒）定视觉验收。
