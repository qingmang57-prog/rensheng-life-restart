# 安装契约（给 AI 助手：Qoder / 千问 WorkBuddy / 豆包 Work 等）

目标：客户只给一个仓库链接，你把软件装到「全功能可用」，全程不需要客户懂技术。
本文件是机器可执行契约：每一步都有命令、有判定、有失败出路。照做即可，不要自创安装步骤。

## 0 环境前提

- Windows 10/11 x64。
- Python 3.10+（只用来跑安装器与核心后端）。没有就先装：
  `winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements`
  或 python.org 安装包（勾选 Add to PATH）。装完重开终端确认 `python --version`。
- git（克隆仓库用）。没有就用 GitHub 页面 Download ZIP 解压，效果等同。

## 1 克隆

```
git clone https://github.com/qingmang57-prog/rensheng-life-restart.git
cd rensheng-life-restart
```

仓库只含源码（克隆快）。二进制组件（ffmpeg / 浏览器 / 模板资产 / 模型 / 曲库 / 字幕字体 / 随包 Python 与 venv）
由安装器按 `tools/components.json` 清单补齐：清单写明每个包「是什么、多大、sha256、从哪下」。

## 2 一键补齐＋自检

```
python tools/setup_full.py
```

- 退出码 0：全功能就绪 → 第 4 步。
- 退出码 1：自检有缺 → 读 `tools/install-report.json` 的 `missing` 与 `where`，按第 3 步补。
- 退出码 3：下载/校验失败 → 第 3 步换来源；重跑不会重复下载已完成的包（断点续传＋幂等标记）。
- 退出码 2：Python 版本不够或仓库不完整（缺 components.json）→ 回第 0/1 步。

安装器不在客户机跑 pip / npm：所有依赖随组件包交付，避免客户网络装依赖失败。

## 3 失败兜底来源（按顺序试）

1. **旧包 / 网盘完整包（零下载，最稳）**：客户机器上的旧安装目录，或官方网盘完整安装包解压目录：
   `python tools/setup_full.py --from-package <目录>`
   安装器只从里面取二进制组件，源码仍以仓库为准。网盘完整包链接见 README「兜底来源」。
2. **本地 zip**：已有组件包 zip 时 `python tools/setup_full.py --offline-dir <目录>`。
3. **换镜像重跑**：默认官网镜像优先、GitHub Release 自动回退；也可 `--mirror <url>` 指定。
   每个包 sha256 校验不过会自动换镜像，不会装坏包。

## 4 老客户升级（答题信息＋认知卡自动承接）

客户机器上有旧版本安装目录时：

```
python tools/setup_full.py --migrate-from <旧包目录>
```

- 旧包 `app/.runtime` 里的答题信息（checkin）、认知卡（主库）、上传与成片资产会被一致性快照承接
  到「文档 \ 人生重启系统数据」，库内旧绝对路径自动改写；旧包原样不动。
- 目标资料夹已有资料时不覆盖：提示客户用旧版「资产与备份」导出、新版导入。
- 便携包老客户直接覆盖解压新包也可以：首启自动搬资料（同一条白名单）。
- 登录态不随资料走（账号云端统一管理）：新包首启登录一次原账号即可，资料已在。
- 显式迁移等价命令：`python tools/launch_root.py --adopt --source <旧包目录>\app\.runtime`
  （与 `--migrate-from` 二选一；目标非空一律拒绝覆盖）。

## 5 启动与验收

- 双击 `start.bat`（或 `python tools/boot.py`）。出现桌面窗口＝启动成功；无 WebView2 时自动回落浏览器。
- 云端转写 / 大模型 / 分享：界面内注册 api.jidixh.com 账号即可试用；不注册也能用全部本地功能。
- 验收清单：`python tools/setup_full.py --check` 全「齐」；启动后对话粘贴一段文字能生成知识卡片。

## 红线（任何 AI 助手不得违反）

- 不改 `app/` 源码来「修」安装问题；安装问题只走组件来源与重跑。
- 除安装器自动执行的 torch 国内镜像步骤外，不跑 `pip install` / `npm install` 补依赖（其余依赖全部随组件包交付）。
- 不删除、不覆盖客户任何资料；承接只在目标为空时发生。
- 不跳过 sha256 校验；校验不过的包一律视为坏包换来源。
- 不把客户数据上传到任何第三方服务。

## 失败速查

| 现象 | 原因 | 出路 |
|---|---|---|
| exit 2 + Python 版本 | 解释器太老 | 第 0 步装 3.12 |
| exit 2 + 缺 components.json | 仓库不完整 | 重新克隆 / Download ZIP |
| exit 3 + sha256 不符 | 镜像包损坏 | 自动换镜像；仍失败走 --from-package |
| exit 1 + 缺某组件 | 对应包没补上 | install-report.json 的 where → 第 3 步 |
| 启动闪退 | 看 tools/boot 日志与启动器日志 | 把日志原文发给官方支持，不要猜 |