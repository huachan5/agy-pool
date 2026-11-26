# `agy-pool` × `agy-hud` 双 Gemini 账号池与无缝接力 — 产品设计与技术方案

> **一句话通俗理解（ELI5）**：把开发任务想象成一场**接力赛**。`agy-pool` 负责当裁判兼交接员（管理任务线程、生成精炼交接棒、调度安全换号并拉起新会话），`agy-hud` 负责当场边大屏幕（实时展示第几棒、当前账号与低配额切换提醒），各司其职、互不越界。

---

## 第一部分：详细产品设计

### 1. 问题定义（Problem Statement）

#### 1.1 目标用户与核心痛点
- **目标用户**：在 Windows 终端高频使用 Antigravity CLI（`agy`）推进长周期开发任务、且常同时开启多个终端窗口并行工作的双 Gemini 账号开发者。
- **核心痛点**：
  1. **配额打断心流**：单个 Gemini 账号 5h 配额触底后，必须中断任务换号，新开的空白会话丢失全部背景与已做决策。
  2. **手工交接成本高且易失真**：靠人工总结或复制粘贴历史对话，耗时 3~8 分钟/次；且长任务经历 `A → B → A` 多轮接力后，原始目标容易“传话走样”。
  3. **多终端并发串台/误杀**：同目录或跨目录同时开 2~3 个 `agy` 窗口时，单机单登录态下直接切号容易让另一个正在运行的窗口报错，或搞混哪份上下文对应哪个任务。
  4. **配额状态割裂**：`agy-hud` 已能实时显示当前账号的 `Gemini 5h / 7d` 剩余比例，但缺少“换号预警”与“备用账号上次快照余量”的联动视图。

#### 1.2 成功指标（Success Criteria）

| 维度 | 当前基线 | Phase 1 目标 | 验证方式 |
|---|---|---|---|
| **切号交接耗时**（不含人工浏览器点确认） | 3~8 分钟 | **≤ 15 秒** | 实测 `agy-pool switch` 端到端计时 |
| **接力后目标一致性** | 2 棒以上常丢失约束 | **100%** 保留原始 `anchor.md` + `task.md` + `git diff --stat` | 检查 `handoff.md` 必含 L1 全部字段 |
| **交接包体积控制** | 全量 `transcript.jsonl` 动辄数十万 Token | **≤ 3,500 Tokens** | 单元测试验证超大日志自动裁剪 |
| **多终端防撞率** | 无检测，易误切 | **100%** 拦截同账号其他存活终端 | 并发集成测试 |
| **零凭据合规** | N/A | **0 条** Token/Cookie/API Key 落盘 | 自动化脱敏与静态扫描测试 |

#### 1.3 范围边界（Scope Boundaries）
- **In Scope**：逻辑任务线程管理（`TaskThread`）、双账号槽位与配额快照管理、L1/L2/L3 结构化交接包生成、多终端隔离（`AGY_POOL_THREAD_ID`）与并发冲突保护、两阶段切号编排、拉起新 `agy -i` 会话、`agy-hud` 轻量状态徽章与低配额告警。
- **Out of Scope**：
  - **绝不**读取、导出、拷贝、篡改 OS 凭据管理器（Windows Credential Manager）或浏览器 Cookie / OAuth Token；
  - **绝不**让 `agy-hud` 强行接管终端 TTY、模拟敲 `/logout` 或杀掉 `agy` 进程；
  - **绝不**尝试跨账号合并云端历史会话记录（靠本地 `handoff.md` 桥接）。
- **Deferred（Phase 2）**：3+ 账号轮转、`ConversationService/CreateConversation` 后台预热会话、交接包可视化 diff、配额重置倒计时提醒。

---

### 2. 核心用户流程与用户故事

#### 2.1 六大核心场景

| 场景 | 用户操作 | 系统反馈与 `agy-hud` 表现 |
|---|---|---|
| **1. 开新线程** | `agy-pool start "重构登录鉴权" --account main` | 创建线程 `T1`、生成置顶 `anchors/T1.md`、分配第 1 棒 `seg1`、注入环境变量后拉起 `agy`。HUD 第 1 行显示 `[T1·main·#1]`。 |
| **2. 日常查看 HUD** | 正常在 `agy` 中对话写代码 | `agy-hud` 展示 `[T1·main·#1]`，并将 `conversation_id`、`transcript_path` 与 `stdin.quota` 防抖写入 `live/<seg_id>.json`；普通未托管 `agy` 窗口外观 **0 变化**。 |
| **3. 低额度告警** | `main` 账号 `Gemini 5h` 降至阈值以下（如 `≤ 10%`） | `agy-hud` 第 2 行配额尾部亮出提醒：`⚠ 建议切换 backup (上次快照 94%)`。 |
| **4. 切号接力** | 退出当前 `agy` 后运行 `agy-pool switch T1 --to backup` | ① 检查并发冲突；② 进入 `handoff_pending` 并生成 `handoffs/T1-seg1.md`；③ 按官方 `AccountAdapter` 切号；④ 自动运行 `agy -i "<短引导指令>"` 拉起第 2 棒 `seg2`。HUD 更新为 `[T1·backup·#2]`。 |
| **5. 多窗口并行** | 终端 1 跑 `T1`，终端 2 跑 `T2`（可同目录） | 各自持有独立 `AGY_POOL_THREAD_ID` 互不串台。若终端 1 发起全局切号且终端 2 仍在用旧账号运行，**默认拦截并列出冲突进程/线程**。 |
| **6. 回切原账号** | 配额重置后运行 `agy-pool switch --to main` | 基于 `seg2` 最新日志生成 `T1-seg2.md`，同时依然置顶最初的 `anchors/T1.md`，拉起第 3 棒 `[T1·main·#3]`。 |

#### 2.2 用户故事（INVEST）
- **US-01**：作为重度 `agy` 开发者，我希望用一个命令为当前任务创建逻辑线程并记下原始目标与约束，以便后续无论换几次号都不会偏离初衷。
- **US-02**：作为正在写代码的开发者，我希望在 `agy-hud` 上一眼看到当前线程、账号、棒次及低额度提醒，以便在自然断点提前交接。
- **US-03**：作为需要切号的开发者，我希望工具自动从对话日志、`task.md` 和 Git 变更中提炼精炼交接包，以便新账号秒懂现状且不撑爆上下文。
- **US-04**：作为使用双账号的开发者，我希望切号过程可中断、可重试，切完后自动开新会话并喂入交接包。
- **US-05**：作为常开多个终端的开发者，我希望同目录下的不同任务互不串台，且全局切号前自动拦住正在运行的其他窗口。

---

### 3. MVP 与迭代优先级（ICE + MoSCoW）

| 编号 | 候选能力 | Impact | Confidence | Ease | ICE | MoSCoW | 归属 |
|---|---|---:|---:|---:|---:|---|---|
| C1 | 核心数据模型 + `state.json` 原子存储与文件锁 | 10 | 10 | 8 | **800** | **Must** | Phase 1 (MVP) |
| C2 | L1/L2/L3 结构化 `handoff.md` 生成与 Token 预算裁剪 | 10 | 9 | 8 | **720** | **Must** | Phase 1 (MVP) |
| C3 | `agy -i` 拉起新会话 + `AGY_POOL_THREAD_ID` 终端隔离 | 10 | 9 | 8 | **720** | **Must** | Phase 1 (MVP) |
| C4 | `AccountAdapter` 两阶段切号编排 + 失败回滚 | 10 | 8 | 7 | **560** | **Must** | Phase 1 (MVP) |
| C5 | 多终端同账号并发冲突检测 + Windows PID 探活 | 9 | 8 | 7 | **504** | **Must** | Phase 1 (MVP) |
| C6 | `agy-hud` 最小侵入联动（徽章 + 实时同步 + 低额提醒） | 9 | 9 | 8 | **648** | **Must** | Phase 1 (MVP) |
| C7 | CLI 状态面板（`status / list`；`doctor` 放 Phase 2） | 7 | 9 | 9 | **567** | **Should** | Phase 1 精简版 |
| C8 | `ConversationService` 本地 HTTP 后台预建会话 | 6 | 7 | 5 | **210** | **Could** | Phase 2 |
| C9 | 3+ 账号自动路由 + 配额重置倒计时通知 | 5 | 7 | 5 | **175** | **Could** | Phase 2 |

---

### 4. 验收标准（Given / When / Then）与边界异常清单

- **AC-01 创建线程与环境变量隔离**：Given 合法项目目录与已注册双账号，When 执行 `agy-pool start`，Then 生成唯一 `thread_id` 与 `anchors/<tid>.md`，拉起 `agy` 并注入 `AGY_POOL_THREAD_ID` 与 `AGY_POOL_SEGMENT_ID`。
- **AC-02 `agy-hud` 徽章与实时同步**：Given 托管会话传入 `stdin` JSON，When 渲染 HUD，Then 第 1 行包含 `[T1·main·#1]`，并防抖写入 `live/<seg_id>.json`。
- **AC-03 低额度视觉预警**：Given `gemini-5h` 剩余降至阈值以下，When 渲染 HUD，Then 第 2 行末尾显示建议切换的目标槽位及最近快照余量。
- **AC-04 完整切号接力**：Given 无并发冲突且完成官方切号确认，When 执行 `agy-pool switch`，Then 生成 `handoffs/<tid>-seg<N>.md`（≤ 3,500 Tokens），旧段置为 `handed_off`，以 `agy -i` 拉起下一棒。
- **AC-05 多棒回切不失真**：Given 经过 `main → backup → main` 回切，Then 新交接包 L1 依然完整保留初始 `anchors/<tid>.md`。
- **AC-06 多终端同账号并发拦截**：Given 其他终端仍有同账号 `running` 段且使用单机单登录态，When 尝试 `switch`，Then 拒绝直接切换并列出冲突进程清单。
- **AC-07 切号中断可回滚与幂等续接**：Given 进入 `handoff_pending` 后用户 `Ctrl+C` 取消，Then `state.json` 完整无损，交接包保留，再次运行 `switch` 可安全续接。
- **AC-08 无 `agy-pool` 场景零退化**：Given 原生 `agy` 启动或 `state.json` 缺失/损坏，Then `agy-hud` 输出与改造前完全一致、不抛异常。
- **AC-09 零凭据与敏感信息脱敏**：`state.json`、`live/*.json`、`handoffs/*.md` 不得包含任何 Token/Cookie/API Key；日志中偶发密钥正则替换为 `[REDACTED]`。
- **AC-10 原子持久性与无黑框**：采用写临时文件 + `os.replace`，Windows 子进程保持 `CREATE_NO_WINDOW`。

---

## 第二部分：详细技术方案

### 1. 领域模型与关键架构决策（ADRs）

- **ADR-001：`state.json` 单写者 + `live/<segment_id>.json` 分片心跳**
  - `agy-pool` 是 `~/.antigravity/agy-pool/state.json` 的唯一写者（配合 `state.lock`）；
  - 每个 `agy-hud` 进程只读 `state.json`，并仅原子写入自身专属的 `live/<segment_id>.json`（5s 防抖），彻底消除 Windows 多进程 `os.replace` 冲突（`WinError 5/32`）。
- **ADR-002：账号切换走 `AccountAdapter` Seam，坚持零凭据与两阶段提交**
  - 支持多适配器：`ManualGuidedAdapter`（默认，引导走官方 `/logout` + Google OAuth）、`EnvVarAdapter`（`GEMINI_API_KEY` 进程隔离）、`ExternalCommandAdapter`（外部合法切号命令）及测试用 `FakeAccountAdapter`。
- **ADR-003：`agy -i` 传指针型短指令而非全量交接正文**
  - 交接正文落盘于 `handoffs/<tid>-seg<N>.md`，`agy -i` 仅传递 ~150 字单行引导语指向该文件路径，彻底规避 Windows 命令行长度与引号转义问题。

### 2. 包与模块划分（纯 Python 3.12 标准库，零外部依赖）

```text
C:\Users\Administrator\Desktop\cspace\
├── agy-hud\                         # 最小侵入改造（绝不 import agy_pool）
│   ├── hud.py
│   ├── config.json
│   ├── install.ps1
│   └── test_hud.py
└── agy-pool\                        # 独立控制器包
    ├── agy_pool\
    │   ├── __init__.py
    │   ├── models.py                # TaskThread / AccountProfile / SessionSegment / LiveTelemetry
    │   ├── store.py                 # PoolStore：state.lock + 原子读写 + live 合并 + Win32 PID 探活
    │   ├── handoff.py               # HandoffBuilder：L1/L2/L3 抽取 + Token 预算 + 脱敏
    │   ├── adapters.py              # AccountAdapter & SessionLauncher (Seam + 实现)
    │   ├── orchestrator.py          # RelayOrchestrator：状态机、并发检查、两阶段切号、回滚
    │   └── cli.py                   # CLI 入口（init/start/status/list/handoff/switch/resume）
    ├── tests\
    │   ├── test_store.py
    │   ├── test_handoff.py
    │   ├── test_orchestrator.py
    │   └── test_cli_e2e.py
    ├── agy-pool.cmd
    ├── install.ps1
    └── README.md
```

### 3. `state.json` Schema（v1）与状态机

- **目录布局**（`~/.antigravity/agy-pool/`）：`state.json`、`state.lock`、`anchors/<tid>.md`、`handoffs/<tid>-seg<N>.md`、`live/<seg_id>.json`。
- **`SessionSegment` 状态迁移**：`starting → running → handoff_pending → handed_off`（支持 `running → closed` 及 `closed → handoff_pending`，以及 `handoff_pending → running/closed` 回滚）。
- **Win32 PID 探活**：使用 `ctypes` 调用 `kernel32.OpenProcess` + `GetExitCodeProcess`，无子进程开销与弹窗。

### 4. `HandoffBuilder` 交接包预算算法（硬上限 3,500 Tokens）
- **L1（≤ 1,200 tokens）**：`anchors/<tid>.md` 全文（永不裁剪） + `task.md` 进度 + `git status` / `git diff --stat`。
- **L2（≤ 2,000 tokens）**：复用 `hud.py` 的 `step_index` 去重逻辑，抽取最近 N 轮用户指令与关键结论、改动文件清单、最新报错，过滤大段工具输出与思考草稿；超限时逐轮裁剪最老轮次。
- **L3（≤ 300 tokens）**：`segment_id`、`account_id`、`conversation_id`、`transcript_path`、接力链历史。
- **Token 估算**：`ceil(cjk_chars / 1.5 + other_chars / 4.0)`；出库前经正则脱敏替换敏感凭据为 `[REDACTED]`。

### 5. `agy-hud` 最小侵入式改造点
- `config.json` & `DEFAULT_CONFIG` 新增 `pool` 配置段（`enabled`、`showThreadBadge`、`lowQuotaThresholdPercent`、`syncLiveTelemetry`、`liveSyncIntervalSeconds`）。
- `hud.py` 新增 `load_pool_context()` 与 `sync_pool_live_telemetry()`；在 `render_project_line()` 追加 `[T1·main·#1]`，在 `render_usage_segment()` 低额度时追加切换提示；非托管会话 **0 变化**。

---

## 第三部分：待用户确认的关键前提

1. **本机双 Gemini 账号实际切换方式**：
   - 选项 A（推荐）：引导式官方切号 `ManualGuidedAdapter`（`/logout` + Google OAuth 浏览器登录）
   - 选项 B：`GEMINI_API_KEY` 环境变量隔离 `EnvVarAdapter`
   - 选项 C：外部合法切号命令 `ExternalCommandAdapter`
2. **`agy-hud` 低额度预警阈值**：默认 `10%` 还是调整为 `15%` / `20%`？
