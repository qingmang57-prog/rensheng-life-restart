# 随包配乐 / 音效授权台账（tools/bgm）

本目录的曲目由「个人 IP 工作台」随包分发，用于成片的背景音乐与音效。
机器可读台账：`library.json`；只有 `enabled=true` 且 `verified=true` 的曲目才会被自动选曲使用。

## 一、背景音乐（bgm/）

全部来自 Kevin MacLeod（incompetech.com），授权 **CC BY 4.0**：
可商用、可改编，义务是**署名**。发布视频时请在简介、置顶评论或片尾写明：

    BGM：<曲名> - Kevin MacLeod (incompetech.com)

- 许可证全文：https://creativecommons.org/licenses/by/4.0/
- 官方曲目库（逐曲核对来源）：https://incompetech.com/music/royalty-free/pieces.json
- 核验方式：2026-11-14 抓取官方曲目库，按 ISRC 逐曲比对（下表 ISRC 即官方编号）
- 随包文件为 128kbps MP3 转码副本（CC BY 4.0 允许的改编）

| 目录 | 曲名 | 作者 | 官方来源（ISRC 页面） | 许可证 | 抓取日期 | 状态 |
|---|---|---|---|---|---|---|
| 01-promo | Night of Chaos | Kevin MacLeod | https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1300042 | CC BY 4.0 | 2026-11-14 | 已核验 |
| 01-promo | Volatile Reaction | Kevin MacLeod | https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1400039 | CC BY 4.0 | 2026-11-14 | 已核验 |
| 02-daily | Daily Beetle | Kevin MacLeod | https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1500025 | CC BY 4.0 | 2026-11-14 | 已核验 |
| 02-daily | Jaunty Gumption | Kevin MacLeod | https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1300017 | CC BY 4.0 | 2026-11-14 | 已核验 |
| 03-warm | Heartbreaking | Kevin MacLeod | https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1100208 | CC BY 4.0 | 2026-11-14 | 已核验 |
| 03-warm | Windswept | Kevin MacLeod | https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1100757 | CC BY 4.0 | 2026-11-14 | 已核验 |
| 04-funny | Fluffing a Duck | Kevin MacLeod | https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1100768 | CC BY 4.0 | 2026-11-14 | 已核验 |
| 04-funny | Monkeys Spinning Monkeys | Kevin MacLeod | https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1400011 | CC BY 4.0 | 2026-11-14 | 已核验 |
| 05-suspense | Hidden Agenda | Kevin MacLeod | https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1200102 | CC BY 4.0 | 2026-11-14 | 已核验 |
| 05-suspense | Tempting Secrets | Kevin MacLeod | https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1300038 | CC BY 4.0 | 2026-11-14 | 已核验 |
| 06-premium | Airport Lounge | Kevin MacLeod | https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1100806 | CC BY 4.0 | 2026-11-14 | 已核验 |
| 06-premium | Bossa Antigua | Kevin MacLeod | https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1700069 | CC BY 4.0 | 2026-11-14 | 已核验 |
| 07-calm | Gymnopédie No. 1 | Kevin MacLeod | https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1100787 | CC BY 4.0 | 2026-11-14 | 已核验 |

> 来源待确认的曲子：默认不启用（`library.json` 里 `verified=false` / `enabled=false`）。以后放入自备曲目时，必须补齐曲名、作者、来源、许可证与日期，否则不会被自动选曲。

平台提示：抖音对曲库外音频没有平台授权兜底。发布抖音时更稳的做法是在剪映曲库里找同情绪曲目替换；
视频号/快手/B 站等可直接使用本库（需署名）。

## 二、音效（sfx/）

来源：Kenney（kenney.nl）ui-audio / digital-audio，**CC0 1.0 公有领域**，可商用、无需署名。
随包文件为原名改名后的副本。

| 随包文件 | 原名 | 来源 | 许可证 | 抓取日期 |
|---|---|---|---|---|
| tick-click.ogg | 卡点-click.ogg | https://kenney.nl/assets/ui-audio | CC0 1.0 | 2026-11-14 |
| switch.ogg | 卡点-switch.ogg | https://kenney.nl/assets/ui-audio | CC0 1.0 | 2026-11-14 |
| rollover.ogg | 轻点-rollover.ogg | https://kenney.nl/assets/ui-audio | CC0 1.0 | 2026-11-14 |
| riser.ogg | 转场-上升.ogg | https://kenney.nl/assets/digital-audio | CC0 1.0 | 2026-11-14 |
| zip.ogg | 转场-zip.ogg | https://kenney.nl/assets/digital-audio | CC0 1.0 | 2026-11-14 |
| pop.ogg | 点缀-弹跳.ogg | https://kenney.nl/assets/ui-audio | CC0 1.0 | 2026-11-14 |
| upgrade.ogg | 亮点-升级.ogg | https://kenney.nl/assets/digital-audio | CC0 1.0 | 2026-11-14 |
| ding.ogg | 提示-双音.ogg | https://kenney.nl/assets/ui-audio | CC0 1.0 | 2026-11-14 |

## 三、产品内的署名位置

成片生成时，配乐曲名与署名会写入任务记录（`recipe.json` 的 `bgm_credit` 字段）并随成品卡片展示；
产品说明 `app/README.md` 的「配乐署名」一节写明了发布时该贴哪一句。
