# 观点变形口播-无角标 · MOTION_DESIGN.md
Version: 0.1.0（方法论样板，2026-11-19，队长据 runtime.js/index.html 实测提炼）
Source: tools/eva/模板-观点变形口播-无角标-MS（rs 链 EVA 组件模板）；视觉核心 content/template/{index.html, runtime.js, eva-window.js}
Rights: 自研（rs-eva-talking 引擎克隆，t18）；EVA 系 MIT（LICENSE-EVA.txt, RoverEva）；可上云分发。
方法来源: OpenMotion Motion.md 七层模型（diag/OpenMotion方法论学习-20261119.md），本篇是落到本模板的创意规格层。

> 定位：这是「创意规格」层，约束信息完整性；**创意决定内容**——版式/节奏由本次原文与导演体验判断决定，本规格不预设镜头数/时长/转场配额。

## 1. Identity & provenance
- 名称：观点变形口播-无角标（16:9 横版母版）
- 风格：深蓝金线 · 圆方变形人物窗 · 原文分镜 · 无左上/右上角标
- 源 revision：runtime.js sha 144018025c21…（含 t24 居中偏移、t30 资产消费段）
- 观察状态：双端帧泵渲染已验（10s/1080p30 rc0）；人物居中已优化（DX=-.0168 DY=-.1601）

## 2. Brand Kit（视觉身份令牌，index.html:2 实测）
- 画布：1920×1080，bg #101a25；径向高光 ellipse 84% 18% #20394a + 线性 140deg #111c27→#14252f
- 主金：#d6b574 / 亮金 #dfbd7a；正文 #f1eee5；次级 #a8b8bd；弱 #96a7af
- 装饰：grid 72px 网格 opacity .16；orb 940px 双环金线（left1040 top44，随 t 漂移 sin/cos）
- 字体：'Microsoft YaHei','Noto Sans CJK SC'（正文）；Georgia（quote 引号）；Arial（数字）
- 字阶：heading 92px/800；big-number 200px；quote 190px；listline 61px；caption 46px；eyebrow 25px
- 品牌条：header brand（金色竖条 i 5×26 + 名）＋ edition；footer 固定免责「个人观点，仅供参考，不构成决策依据」
- → 换品牌只改 brand/edition 文本与主金令牌；**色板/字阶/网格 orb 为风格不变量**，复用 DESIGN 令牌不另起 palette。

## 3. Narrative grammar（叙事语法）
- 受众：知识/观点类口播观众；hook 开场抓注意 → 分点/对照/流程展开 → 金句/纸卡沉淀 → close 收束。
- 表达单位＝「画面让观众经历的变化」，不是版式切换本身。每镜须答：这段原文让观众理解/感受什么？
- 版式语义（runtime.js build()，7 种）：hook/quote/close＝大字宣言；points＝编号并列；process＝带金线流程；contrast＝信息卡对照；paper＝纸卡沉淀。
- **版式由原文语义＋导演情绪调制选择**（mood_bridge），不按配额轮播。讲数据→contrast/图表，讲步骤→process，引原话→quote/paper。

## 4. Visual hierarchy
- 焦点：每镜一个视觉重心（大字 heading / 纸卡 / 人物窗），人物窗 #host 与文案区左右分置（geo.x<900→文案右 x1010 w800，否则左 x95 w1000）。
- 层级：eyebrow(25px 金)→heading(92px)→sub(34px)→listline(61px)；caption 底部独立层 z75。
- 留白：场景 inset 0，文案 top185-190；paper 940×654 旋转 -.6deg 纸感。

## 5. Timing & motion（时间运动，runtime.js:13 实测）
- 帧时钟：renderAt(t) 是 t 的纯函数（确定性，禁 Date.now/未播种随机）。
- 镜间淡入 fadeIn=clamp((t-start-.68)/.22)，淡出 fadeOut=clamp((end-t-.08)/.25)；位移 translateY((1-fadeIn)*15px)。
- **词级卡点**：timed-block data-at=block.start，opacity=clamp((t-data-at)/.25)——说到该词该块才浮现。
- 人物窗：EVAW keys 插值 geo＋zoom s，faceAt(t) YuNet 轨迹 lerp＋居中偏移；ringFn 金环。
- orb 漂移 sin(t*.18)*16 / cos(t*.16)*12；progress 条按 t/duration。
- → 改时长须同步重排淡入淡出与 data-at 锚点；缓动/卡词语法为不变量。

## 6. Materials & sound
- 必需输入：cfg.host（人物视频帧序列 pattern＋count＋fps）、cfg.faces（YuNet 轨迹）、cfg.captions（真实语音字幕 start/end/text）、cfg.scenes（分镜 blocks）。
- 可选注入：assets.json（bg 背景/fx 情绪特效 screen 叠加/broll 开场垫层 <1.2s）——t30 资产消费段，无则零行为。
- 声音：原声优先；BGM 由客户端 wrap_audio 混一次（渲染端 bgm=off 防二次混音）。
- **绝不伪造缺失素材**：无人物帧/无字幕→报告阻塞，不拿生成图冒充真实证据。

## 7. Invariants（换内容必须幸存）
- 1920×1080 深蓝金线风格、grid+orb、字阶色板（§2）
- renderAt(t) 确定性帧时钟；词级 data-at 卡点语法
- 7 版式语义映射；镜间淡入淡出缓动（.68/.22/.08/.25）
- 人物窗 face-track＋居中偏移（DX=-.0168 DY=-.1601, centerY:.5）
- caption 来自真实语音；footer 免责；原声优先/BGM 单次混音
- 容量自适应字号＋溢出缩放（§8）

## 8. Replaceable inputs（可换内容＋容量契约）
| 字段 | 内容 | 容量/溢出行为（runtime.js 实测） |
|---|---|---|
| scenes[].blocks[].text | 分点/金句正文 | 字号自适应：points=min67/max35/floor(w/min15,max7,len))；paper heading=min78/max36/floor(1600/max15,len)；quote/hook=min86/max43/floor(2250/max26,len)；contrast=min68/max35/floor(1800/max25,len) |
| 溢出 | 文案超高 | generic-copy scrollHeight>650 → scale(650/scrollHeight) 整体缩放（runtime.js:12）；caption max-height140 pre-line |
| scenes[].heading | eyebrow 标题 | 缺省回退「观点 NN」 |
| scenes[].layout | 版式 | 枚举 hook/quote/points/process/contrast/paper/close（导演指派） |
| scenes[].geo | 人物窗几何 | 9 预设（GEOMETRIES），zoom s 参数化 |
| captions | 字幕 | 真实语音 start/end/text，逐条 |
| brand/edition | 品牌名/期号 | edition max-width1000 省略号 |
| assets.json | bg/fx/broll | 可选，无则零行为 |
| 长文案 | 超容量 | **拆句或重设计该镜，不缩到不可读**（OpenMotion 纪律） |

## 9. Review（审查规则）
- 技术层：双端帧泵解码 rc0、ffprobe 时长/分辨率/帧率、跨泵确定性等价、FRAME_DIAGNOSTICS 逐帧可查。
- 体验层（**技术通过≠审美证据**）：原速听看——观众能看到所声称的变化吗？去掉装饰仍有具体时间发展吗？卡词是否精确（半秒偏移即不合格）？人物是否居中、字幕可读、声画同步？
- 成对证据：参考/ remix 同时间戳对比＋源/输出 hash；记录残留差距。「check 标题不是成功证明」。
- 人眼定视觉验收（青芒）；不因相似分/自己口味反复打回。
