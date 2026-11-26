# agy-pool — Antigravity CLI 双 Gemini 账号池与无缝接力控制器

> 当前状态：**v2.2 技术方案定稿 · GPT/Codex 直接编码就绪（M0 预研已完成，已闭环 M2 三大并发/校验/日志边界，准备进入 M1+M2 开发）**

## 简介

`agy-pool` 是为 Antigravity CLI (`agy`) 打造的轻量级双账号池与任务接力控制器（纯 Python 3.12 标准库，零第三方依赖），配合 [agy-hud](https://github.com/huachan5/agy-hud) 实现：

1. **逻辑任务与物理会话解耦**：以 `TaskThread`（`T1`, `T2`...）与置顶锚点（`anchors/<tid>.md`，≤ 600 Tokens）为核心，支持 `main → backup → main` 多棒循环接力而不偏离初心。
2. **SQLite WAL 单库轻量底座**：采用标准库 `sqlite3` WAL 单库（`pool.db`，实测只读 0.75ms）管理账号槽位、线程、棒次与事件审计，免去手写文件锁与分片合并复杂度。
3. **三层结构化交接包（`handoff.md`）**：从 `anchor.md` + `task.md` + `git diff --stat` + 去噪后的 `transcript.jsonl` 生成 ≤ 3,500 Tokens 交接棒，出库强制脱敏，并通过 `agy -i` 单行短引导词自动拉起下一棒。
4. **全量防撞与账号校验**：基于 Win32 `ctypes` 全量扫描本机 `agy.exe`（托管 + 非托管）并记录 `(pid, create_time)` 防 PID 复用；换号后校验登录账号哈希防登错烧配额；全程**零凭据落盘**。

## 文档索引

- 产品需求文档（PRD v1.2）：[docs/agy-pool-PRD.md](docs/agy-pool-PRD.md)
- 详细技术方案（**v2.2** · SQLite WAL + 4 深模块 + M0 结论 + M2 缺口闭环）：[docs/agy-pool-技术方案.md](docs/agy-pool-技术方案.md)
- 界面与 CLI 交互原型图（PNG + 7 张 SVG）：[docs/agy-pool-原型图/](docs/agy-pool-原型图/)
- 测试用例规范（v1.0 黑盒场景参考，Markdown）：[docs/agy-pool-test-cases.md](docs/agy-pool-test-cases.md)
- 测试用例导入表（v1.0 黑盒场景参考，CSV）：[docs/agy-pool-test-cases.csv](docs/agy-pool-test-cases.csv)
