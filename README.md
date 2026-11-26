# agy-pool — Antigravity CLI 双 Gemini 账号池与无缝接力控制器

> 当前状态：**Windows M1/M2；macOS 预览，待真机验收**。HUD 徽章与默认 8% 提醒属于 M3；真实浏览器换号和完整接力在 M4 验收。

## 简介

`agy-pool` 是为 Antigravity CLI (`agy`) 打造的轻量级双账号池与任务接力控制器（纯 Python 3.12 标准库，零第三方依赖），配合 [agy-hud](https://github.com/huachan5/agy-hud) 实现：

1. **逻辑任务与物理会话解耦**：以 `TaskThread`（`T1`, `T2`...）与置顶锚点（`anchors/<tid>.md`，≤ 600 Tokens）为核心，支持 `main → backup → main` 多棒循环接力而不偏离初心。
2. **SQLite WAL 单库轻量底座**：采用标准库 `sqlite3` WAL 单库（`pool.db`，实测只读 0.75ms）管理账号槽位、线程、棒次与事件审计，免去手写文件锁与分片合并复杂度。
3. **三层结构化交接包（`handoff.md`）**：从 `anchor.md` + `task.md` + `git diff --stat` + 去噪后的 `transcript.jsonl` 生成 ≤ 3,500 Tokens 交接棒，出库强制脱敏，并通过 `agy -i` 单行短引导词自动拉起下一棒。
4. **全量防撞与账号校验**：基于 Win32 `ctypes` 全量扫描本机 `agy.exe`（托管 + 非托管）并记录 `(pid, create_time)` 防 PID 复用；换号后校验登录账号哈希防登错烧配额；全程**零凭据落盘**。

## Windows 快速开始

需要 Python 3.12、能从 PATH 启动的 `agy.exe` 和已启用的现有 `agy-hud`。在仓库目录用 `.\agy-pool.cmd init` 可以直接初始化，无需安装；如需加入当前用户 PATH，可执行 `.\install.ps1`。

```powershell
.\agy-pool.cmd account add main --from-current
.\agy-pool.cmd start '重构登录鉴权' --account main
.\agy-pool.cmd list --json
```

注册账号前，先用对应账号启动普通 `agy`，等 HUD 刷新。为保证账号正确，`start` 会先开一个独立校验窗口：等 HUD 显示后输入 `/exit`，不用发送任务消息。接棒前在源窗口 `/exit`，执行 `agy-pool switch T1 --to backup`，按提示完成浏览器登录，并在接下来的校验窗口确认 HUD 已显示再退出。校验失败不会启动下一棒。`agy-pool switch T1 --cancel` 只取消本地接棒，**不切回 Google 账号**。

- `agy-pool handoff T1 --transcript <路径>` 可在 HUD 未关联日志时指定来源。自动定位时只保存对话 ID，不锁死本机日志路径。探针放在对应库的数据目录；当前 HUD 会把邮箱写入临时状态，`agy-pool` 不把明文邮箱写进数据库或交接包。
- 多个库可以使用同一基础 `AGY_POOL_HOME`，自定义库会使用 `pools/` 下各自的目录。不同 Windows 登录会话不共享该互斥锁。
- `py -3.12 -m pytest -q` 使用隔离目录和模拟登录；不改真实账号、真实数据库或用户 PATH。

## macOS 预览

需要 Python 3.12（命令为 `python3.12`）、PATH 中的 `agy`，以及能写 `AGY_STATUS_STATE` 的现有 `agy-hud`。在本机终端运行，不需要安装器或授权终端自动化：

```sh
sh /path/to/agy-pool/agy-pool.sh init
sh /path/to/agy-pool/agy-pool.sh account add main --from-current
sh /path/to/agy-pool/agy-pool.sh start '重构登录鉴权' --account main
```

在 Mac 上，引导、校验和正式会话都在**当前终端**依次运行，每个 `agy` 会话用 `/exit` 结束。用另一个终端可执行 `list --json` 或 `handoff`；只要源会话还活着就不能换号。默认换号锁对同一 macOS 用户的不同终端和不同数据库生效。

这条开发分支是预览，仍需在 Mac 真机验证系统接口和 Google 登录接力。[macOS 验收清单](docs/agy-pool-macos.md)列了实际测试步骤。

## 文档索引

- 产品需求文档（PRD v1.2）：[docs/agy-pool-PRD.md](docs/agy-pool-PRD.md)
- 详细技术方案（**v2.3** · 最终定稿：相对路径去耦合 + 跨库互斥硬锁 + M0 结论）：[docs/agy-pool-技术方案.md](docs/agy-pool-技术方案.md)
- 界面与 CLI 交互原型图（PNG + 7 张 SVG）：[docs/agy-pool-原型图/](docs/agy-pool-原型图/)
- 测试用例规范（v1.0 黑盒场景参考，Markdown）：[docs/agy-pool-test-cases.md](docs/agy-pool-test-cases.md)
- 测试用例导入表（v1.0 黑盒场景参考，CSV）：[docs/agy-pool-test-cases.csv](docs/agy-pool-test-cases.csv)
- M1/M2 v2.3 补充验收用例：[Markdown](docs/agy-pool-v2.3-acceptance.md) · [CSV](docs/agy-pool-v2.3-acceptance.csv)
