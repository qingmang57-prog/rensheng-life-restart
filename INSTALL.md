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

安装器不在客户机跑 pip / npm：所有依赖随组件包交付，避免客户网络装依赖失败（唯一例外：离线转写要用的 torch 由安装器自动从国内镜像补齐，见下方「红线」）。

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

## 6 常见问题（装好之后）

### 6.1 启动器、`tools\chromium` 等被杀毒软件拦截
- 现象（都是同一个原因：程序文件被隔离，或被拦着不让启动）：
  - 双击 `start.bat` 后窗口迟迟不出，或后端一启动就退出。启动器会直接写出「后端进程立即退出且没有任何输出，疑似被安全软件拦截」（`tools/launcher/launcher.py`）。
  - 用到某个功能时界面提示「缺少程序文件：可能被杀毒软件隔离，或安装包不完整」，并给出「导出诊断包」按钮（`app/app.py`）。
  - 抖音采集/发布页显示「执行器未安装」（`tools/chromium` 没落地或被隔离，见 README 组件清单）。
- 处理步骤（以 Windows 安全中心为例，其它杀毒软件的菜单名略有差异）：
  1. 打开「Windows 安全中心」→「病毒和威胁防护」→「保护历史记录」，找到被处理的项，点开选「还原」，并在设备上选「允许」。
  2. 「病毒和威胁防护设置」→「管理设置」→「排除项」→「添加排除」→「文件夹」，只添加本软件所在的文件夹（`tools\python`、`tools\chromium`、`tools\launcher\host\LauncherHost.exe` 都在里面）。不要关闭杀毒软件，也不要添加整个磁盘。
  3. 重新运行一次 `python tools/setup_full.py`：已装好的组件自动跳过；仍提示缺组件时按第 3 节的兜底来源补。
  4. 再双击 `start.bat`。仍不行就按 6.2 导出诊断包，原样发给客服。
- 窗口没出来但不属于被拦截：缺 WebView2 运行时时，启动器会自动弹一页中文引导「还差一个 Windows 组件：WebView2 运行时」，按页面步骤装一次即可；装不了回落浏览器也能用（`tools/launcher/launcher.py`）。

### 6.2 一键自检（三层，从上往下做）
1. **装得齐不齐**：`python tools/setup_full.py --check`，全部显示「齐」为通过；明细看 `tools/install-report.json` 的 `missing` 与 `where`（见第 2 节）。
2. **运行时哪里不对**：在应用内失败提示卡点「下一步」，或在设置页点「导出诊断包」，会生成 `<数据目录>\diagnostics\诊断包-YYYYMMDD-HHMM.zip` 并显示完整路径（`app/diagnostics.py`、`app/web/app.js`）。包里是系统信息、数据量、依赖自检、启动状态和最近错误日志，已自动去掉密钥、Cookie 和本机绝对路径。**把这个 zip 原样发给客服即可。**
3. **启动不了、怀疑端口**：在软件根目录运行 `tools\python\python.exe tools\launcher\port_doctor.py`（可选 `--port 8791 --count 45`）。它会列出 Windows 保留端口段，并从 8791 起逐个实测候选端口，最后给出可用端口；需要换端口时，按它的提示改 `tools\launcher\configs\rensheng.json` 的 `"port"`（`tools/launcher/port_doctor.py`）。
- 自己看日志：启动器日志在 `.runtime\launcher\rensheng\launcher.log` 与 `launcher-error.log`，后端日志在 `<数据目录>\logs\app.log`（`tools/launcher/configs/rensheng.json` 的 `log_dir`、`app/applog.py`）。要发客服时优先用第 2 步的诊断包。

### 6.3 试用还剩多久、端口、「只能开一个」
- 试用：注册成功即享 1 天免费体验（`app/license.py` 的 `TRIAL_DAYS = 1`）。剩余时间在应用内右上角「会员中心」看：体验中显示「体验剩余 N 小时」；结束后显示「体验已结束」，已保存的内容还能查看和导出，新增内容暂时做不了，想继续用点「去开通」（`app/web/app.js`、`app/app.py`）。时长以应用内提示为准。
- 端口：默认 `8791`（`tools/launcher/configs/rensheng.json` 的 `"port"`）。被占用时启动器会自动改试后面的端口（`port_fallback_count` 为 45），所以实际端口以启动输出为准：控制台方式启动会打印 `Workbench starting: http://127.0.0.1:…`；桌面窗口方式在启动器日志里找「后端就绪 http://127.0.0.1:…」。
- 只能开一个：同一台电脑同时只能运行一个实例（端口 + 互斥锁）。已经开着时再双击 `start.bat`，不会开出第二个窗口，启动器只是把原来的窗口叫到前台；叫不到会弹「程序已经在运行（可能最小化到了任务栏右下角托盘）」（`tools/launcher/launcher.py`）。启动失败先看输出末尾。

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
| 双击后窗口不出，或界面提示「缺少程序文件」 | 程序文件被杀毒软件隔离或拦截 | 按「6.1」恢复并加信任，再重跑一次安装命令 |
| 不知道哪里坏了 | —— | 按「6.2」三层自检；诊断包原样发客服 |
| 再双击没反应，或弹出「程序已经在运行」 | 已经有一个实例在运行 | 到任务栏右下角托盘找已开的窗口，不要重复双击 |
| 启动失败，输出末尾提到端口 | 端口被别的程序占用，或落在系统保留段 | 按「6.2」第 3 步跑 port_doctor，按提示改端口 |