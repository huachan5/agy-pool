# agy-pool — Antigravity CLI 双 Gemini 账号池与无缝接力控制器

> 当前状态：**设计定稿阶段（待确认最终切号前提后进入 Phase 1 编码实现）**

## 简介

`agy-pool` 是为 Antigravity CLI (`agy`) 打造的轻量级双账号池与任务接力控制器，配合 [agy-hud](https://github.com/huachan5/agy-hud) 实现：

1. **逻辑任务与物理会话解耦**：以 `TaskThread`（`T1`, `T2`...）为锚点，跨账号、跨会话持续推进同一任务，`A → B → A` 多棒循环接力不丢失初始目标。
2. **三层结构化交接包（`handoff.md`）**：基于 `anchor.md` + `task.md` + `git diff --stat` + 去噪后的 `transcript.jsonl`，按 3,500 Token 预算自动裁剪并脱敏，通过 `agy -i` 指针型短指令拉起新会话无缝接棒。
3. **多终端并发防撞**：通过 `AGY_POOL_THREAD_ID` 环境变量与 Win32 PID 探活，防止同目录多任务串台或单登录态下误切账号。
4. **零凭据安全设计**：绝不保存、导出或篡改任何 OAuth Token / Cookie / API Key。

## 文档索引

- 完整产品设计与技术方案：[docs/agy-pool-design.md](docs/agy-pool-design.md)
