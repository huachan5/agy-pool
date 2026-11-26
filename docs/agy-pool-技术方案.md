# `agy-pool` × `agy-hud` 双 Gemini 账号池与无缝接力 — 详细技术方案

| 项 | 内容 |
|---|---|
| 版本 | **v2.3**（2026-11-26，最终编码定稿：相对路径去耦合 + 跨库 Win32 命名互斥体硬锁） |
| 对齐基线 | [agy-pool-PRD.md](agy-pool-PRD.md)（v1.2：FR-01~FR-14、NFR-01~NFR-07、AC-01~AC-15） |
| 技术栈 | **纯 Python 3.12 标准库、零第三方依赖**（与 `agy-hud` 保持一致） |
| 测试框架 | `pytest` + `unittest` |

### 修订记录（v1.0 → v2.3 演进摘要）

| 版本 | 核心演进 |
|---|---|
| v1.0 | `state.json` + 手写 `state.lock` + `live/*.json` 分片合并 + `events.jsonl`，6 模块 + 4 个抽象适配器，含多项初稿预设 |
| v2.0 | ① **存储瘦身**：单库 `pool.db`（标准库 `sqlite3` + WAL + 5s `busy_timeout`）取代文件锁与分片合并，并发与崩溃恢复由引擎原生保障；<br>② **模块收敛**：6 模块收敛为 **4 个深模块**（`store.py` / `handoff.py` / `runtime.py` / `cli.py`），消除 Phase 1 过早抽象；<br>③ **对齐 PRD v1.2**：补齐 `email_hash`、全量 `agy.exe` 枚举（托管+非托管）与 `(pid, create_time)` 防复用、源会话存活硬拦截、前置账号校验、600 Token 锚点门禁、HUD 告警优先级与三态文案；<br>④ **M0 预研落盘**：见 [§15 M0 预研实测结论](#15-m0-预研实测结论) |
| v2.1 | 新增 **§0 Agent / GPT 编码执行契约**、**§5.5 上游数据真实格式样本**、**§8 CLI 参数、退出码与 `--json` 输出契约** |
| v2.2 | 补齐 M2 开工前评审发现的 3 个关键设计缺口：新增 **§4.4 全局换号流程互斥租约**、**§4.5 换号窗口独立状态探针与 Fail-Closed 校验**、**§4.6 会话日志绑定机制（`seg-<id>.json` 探针 + `--transcript`）与 M2/M3 验收边界** |
| **v2.3** | **最终编码定稿**：① `pool.db` 内 `anchor_path` / `handoff_*_path` 统一存相对 `AGY_POOL_HOME` 的相对路径去耦合（`transcript_path` 优先按 `conversation_id` 动态定位）；② 新增跨 `AGY_POOL_DB` 的 Win32 命名互斥体硬锁 `Local\agy-pool-machine-switch-lock`；③ 明确多机各自独立安装、额度由 Google 云端自动同步，不做跨机状态同步 |

---

## 0. Agent / GPT 编码执行契约（必读）

> 若本仓库交给 GPT / Codex 等自动化编程 Agent 实施，请严格遵守以下边界，无需反问确认：

1. **权威优先级与旧文档隔离**：
   - 规格冲突时优先级：**`agy-pool-技术方案.md`（v2.3） > `agy-pool-PRD.md`（v1.2）**；
   - `docs/agy-pool-test-cases.md`（及 `.csv`）为基于 v1.0（`state.json`）编制的黑盒场景参考，**严禁**在编码或编写 `pytest` 时混用 `state.json` / `state.lock` / `live/*.json` / 多 Adapter 套壳，所有持久化与模块划分一律以本文档 **SQLite WAL `pool.db` + 4 模块**为准。
   - **单机轻量定位**：多机场景采用各自独立安装，账号额度由 Google 云端自动同步，**不做**跨机状态同步。
2. **已敲定决策（锁死 PRD §12 Q1~Q5）**：
   - `Q1 = A`：引导式官方换号（`/logout` + 浏览器 OAuth 登录）；
   - `Q2 = 15%`：HUD 低额度预警阈值默认 `15%`；
   - `Q3 = 默认拦截`：切号前扫描所有存活 `agy.exe`（托管 + 非托管）并默认拦截（`--force` 需交互输入 `yes`）；
   - `Q4 = 600 Tokens`：锚点内容硬上限 `600 Tokens`；
   - `Q5 = 永久保留`：Phase 1 交接包永久保留，不做自动清理 `gc`。
3. **分工边界（先只做 M1 + M2）**：
   - 外部 GPT / Codex 本次仅实现 **`agy-pool/` 独立包**（`store.py`、`handoff.py`、`runtime.py`、`cli.py`、`agy-pool.cmd`、`install.ps1` 及 `tests/` 全套 `pytest`）；
   - **严禁修改 `agy-hud/` 目录**（M3 需先在本地 `agy-hud` 仓库执行 `git pull origin main` 合入远端 `713dfd1` 后单独对接实施）。

---

## 1. 领域模型与关键架构决策（ADRs）

### 1.1 领域词汇表（GLOSSARY）
- **`TaskThread`（逻辑任务线程）**：用户推进的一件独立任务（如 `T1`），跨账号、跨会话持续存在，唯一绑定一份置顶 `Anchor`。
- **`Anchor`（任务锚点）**：记录 `TaskThread` 的不变目标、验收标准与硬性约束的 Markdown 文档（上限 600 Tokens），每棒交接必完整携带、永不裁剪。
- **`AccountProfile`（账号槽位）**：对一个逻辑 Gemini 账号的只读元数据描述（别名、`email_hint` 脱敏提示、`email_hash` 本地 SHA-256 截断哈希、最近配额快照与健康状态），**零凭据**。
- **`SessionSegment`（物理会话段）**：一次真实 `agy` 进程运行周期（`seq=1,2,3...`），归属于一个 `TaskThread` 和一个 `AccountProfile`，记录 `pid`、`pid_create_time`、`conversation_id`、`transcript_path` 与段状态。
- **`HandoffBundle`（结构化交接包）**：两个相邻 `SessionSegment` 之间传递的 L1/L2/L3 精简 Markdown 接力棒（≤ 3,500 Tokens）。
- **`EventRecord`（操作审计事件）**：写入 SQLite `events` 表的本地不可变操作日志（建线程、冻结、换号、校验、拉起、回滚等，满足 NFR-07）。

### 1.2 关键架构决策记录（ADRs）

- **ADR-001：使用标准库 `sqlite3` WAL 单库 `pool.db` 替代 JSON + 手写文件锁 + `live/` 分片合并**
  - **背景**：v1.0 设计需要自维护 `state.lock`、僵尸锁清理、`os.replace` 冲突重试、`live/*.json` 分片与 `reconcile_live()` 合并、`events.jsonl` 轮转，属于极易在 Windows 下出现 `WinError 5/32` 与竞态 bug 的偶然复杂度。
  - **决策**：采用 Python 3.12 标准库内置的 `sqlite3` 配合 `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`。实测本机只读查询均值 **0.747 ms**（远低于 NFR-03 的 5 ms P95 红线），并发串行、5 秒等锁（PRD §9）、崩溃事务回滚与事件审计（NFR-07）全部由 SQLite 引擎原生提供；`agy-hud` 仍零外部依赖、绝不 `import agy_pool`，仅用标准库 `sqlite3` 执行单次只读查询与防抖单行更新。
- **ADR-002：收敛为 4 个深模块，消除 Phase 1 过早抽象**
  - **背景**：Phase 1 本机仅使用一种真实切号模式（选项 A：引导式官方 `/logout` + Google OAuth 登录），提前引入 3 套 Adapter 接口和独立 Orchestrator 套壳会增加近一倍无用样板代码。
  - **决策**：系统划分为 `store.py`、`handoff.py`、`runtime.py`、`cli.py` 四个深模块，外部 OS 边界统一收口在 `runtime.py`。Phase 2 若新增 `GEMINI_API_KEY` 或外部命令切号，只需在 `runtime.py` 扩展切换函数即可，零重构成本。
- **ADR-003：`agy -i` 传“指针型单行短指令”而非全量交接正文**
  - **背景**：Windows 下 `CreateProcess` 参数上限 32,767 字符，且经由 PowerShell / `.cmd` shim 传递含换行与引号的长 Markdown 极易转义错乱。
  - **决策**：交接正文落盘到 `~/.antigravity/agy-pool/handoffs/<tid>-seg<N>.md`，`agy -i` 仅传入 ≤ 250 字、不含换行符的单行引导词指向该文件路径。

---

## 2. 4 模块深架构与目录布局

### 2.1 目录与包结构
```text
C:\Users\Administrator\Desktop\cspace\
├── agy-hud\                         # 现有仓库（最小侵入增强，绝不 import agy_pool）
│   ├── hud.py                       # 只读/防抖更新 pool.db + 徽章/告警渲染
│   ├── config.json                  # 新增 pool 配置段
│   ├── install.ps1                  # 升级配置迁移
│   └── test_hud.py                  # 原有全部测试 + pool 联动测试
│
└── agy-pool\                        # 独立控制器仓库（纯 Python 3.12 标准库）
    ├── agy_pool\
    │   ├── __init__.py
    │   ├── store.py                 # 深模块：SQLite WAL 5 表、事务迁移、零凭据守卫、事件日志
    │   ├── handoff.py               # 深模块：锚点 600-token 门禁、L1/L2/L3 抽取、3500-token 裁剪、脱敏
    │   ├── runtime.py               # 唯一 OS 边界：Win32 枚举 agy.exe、防 PID 复用探活、账号校验、引导换号、agy -i 拉起
    │   └── cli.py                   # CLI 入口与两阶段切号编排（account/start/anchor/handoff/switch/status/list/close）
    ├── tests\
    │   ├── test_store.py
    │   ├── test_handoff.py
    │   ├── test_runtime.py
    │   └── test_cli_e2e.py
    ├── agy-pool.cmd                 # Windows shim（调用 Python 3.12 -m agy_pool.cli）
    ├── install.ps1                  # 初始化 ~/.antigravity/agy-pool 目录与 PATH shim
    └── README.md
```

### 2.2 深模块接口与可测性 Seam

| 模块 | 对外精简接口 | 隐藏的内部复杂性 | 可测性 Seam |
|---|---|---|---|
| `store.py` (`PoolStore`) | `init_db()`, `transaction()`, `list_threads()`, `get_account()`, `record_event()`, `export_snapshot()` | WAL 初始化、5s `busy_timeout`、`schema_version` 备份迁移、零凭据递归键名/模式守卫、事务原子提交 | 传入 `tmp_path / "pool.db"` 即可完整实测 |
| `handoff.py` (`HandoffBuilder`) | `validate_anchor(text, max_tokens=600)`, `estimate_tokens(text)`, `build(...) -> HandoffResult` | `transcript.jsonl` 按 `step_index` 去重、噪音过滤、`task.md` 解析、`git status/diff --stat`、预算优先级裁剪、出库正则脱敏 | 纯数据变换 + 依赖注入采集器 |
| `runtime.py` (`AgyRuntime`) | `scan_agy_processes()`, `is_process_alive(pid, create_time)`, `read_current_auth()`, `guide_account_switch()`, `launch_agy(...)` | Win32 `ctypes` `CreateToolhelp32Snapshot` 全量枚举 `agy.exe`、`GetProcessTimes` 防 PID 复用、读 `status-state.json` / 兜底 `GetUserStatus`、拉起并做 3s 存活监测 | 测试时仅需注入 `FakeRuntime` 替换这一处边界 |
| `cli.py` | `main(argv)` + 子命令处理 | 线程省略规则（`AGY_POOL_THREAD_ID` → `cwd` 唯一活跃线程 → 报错列候选）、并发冲突拦截（退出码 3）、两阶段切号与 `--cancel` 回滚、`--json` 输出 | 通过 `FakeRuntime` + `tmp_path` 跑全命令行端到端 |

---

## 3. SQLite Schema（`~/.antigravity/agy-pool/pool.db`）

### 3.1 运行时目录布局
```text
~/.antigravity/agy-pool/
├── pool.db                          # SQLite WAL 主库（+ pool.db-wal / pool.db-shm）
├── anchors/
│   └── T1.md                        # 线程 T1 的不变初心锚点（≤ 600 Tokens）
└── handoffs/
    ├── T1-seg1.md                   # 第 1 棒 → 第 2 棒交接包（≤ 3,500 Tokens）
    └── T1-seg2.md
```

### 3.2 `schema_version = 1` 完整建表 SQL

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- 初始键：schema_version='1', active_machine_account='', next_thread_seq='1'
-- 运行时租约键：active_switch_lease = JSON({"switch_id", "thread_id", "from_account", "to_account", "owner_pid", "owner_pid_create_time", "started_at"})（空闲时删除或置空）

CREATE TABLE IF NOT EXISTS accounts (
    account_id          TEXT PRIMARY KEY,
    label               TEXT,
    email_hint          TEXT,               -- 脱敏展示，如 a***@gmail.com
    email_hash          TEXT UNIQUE,        -- SHA-256(lower(email))[:16]，绝不存明文邮箱
    status              TEXT NOT NULL,      -- ready / active / cooldown / exhausted
    gemini_5h_remaining REAL,               -- 0.0 ~ 1.0
    gemini_5h_reset_at  TEXT,               -- ISO-8601 UTC
    gemini_7d_remaining REAL,
    quota_updated_at    TEXT,
    cooldown_until      TEXT
);

CREATE TABLE IF NOT EXISTS threads (
    thread_id          TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    cwd                TEXT NOT NULL,
    anchor_path        TEXT NOT NULL,       -- 一律存相对 AGY_POOL_HOME 的相对路径（如 'anchors/T1.md'），禁止硬编码绝对路径
    status             TEXT NOT NULL,       -- active / closed
    current_segment_id TEXT,
    segment_count      INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    segment_id          TEXT PRIMARY KEY,
    thread_id           TEXT NOT NULL REFERENCES threads(thread_id),
    seq                 INTEGER NOT NULL,
    account_id          TEXT NOT NULL REFERENCES accounts(account_id),
    parent_segment_id   TEXT,
    conversation_id     TEXT,
    transcript_path     TEXT,               -- 可留空；优先按 conversation_id 动态定位本机 ~/.gemini/<app_dir>/brain/<conv_id>/...，仅 --transcript 显式覆盖时记录
    handoff_in_path     TEXT,               -- 相对 AGY_POOL_HOME 路径（如 'handoffs/T1-seg1.md'）
    handoff_out_path    TEXT,               -- 相对 AGY_POOL_HOME 路径（如 'handoffs/T1-seg1.md'）
    pid                 INTEGER,
    pid_create_time     INTEGER,            -- Win32 FILETIME 64-bit 整数，防 PID 复用
    status              TEXT NOT NULL,      -- starting / running / closed / handoff_pending / handed_off / failed
    observed_email_hint TEXT,
    observed_email_hash TEXT,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    last_heartbeat_at   TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    event_type  TEXT NOT NULL,              -- thread_start / handoff_frozen / switch_start / switch_commit / switch_cancel / auth_mismatch / launch_failed
    thread_id   TEXT,
    segment_id  TEXT,
    account_id  TEXT,
    detail_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_threads_cwd_status ON threads(cwd, status);
CREATE INDEX IF NOT EXISTS idx_segments_thread_seq ON segments(thread_id, seq);
CREATE INDEX IF NOT EXISTS idx_segments_status ON segments(status);
```

### 3.3 迁移与零凭据写前守卫
- **Schema 迁移**：打开数据库前校验 `meta.schema_version`；若低于当前版本，先复制备份 `pool.db.v{old}.bak`，再在单一事务内顺序执行 `MIGRATIONS[v -> v+1]`；若高于当前版本，拒绝写入并提示升级 `agy-pool`。
- **零凭据写前守卫（NFR-01）**：`PoolStore` 写入任何字段或 `detail_json` 前执行键名与正则守卫，**严禁**出现 `token`、`cookie`、`secret`、`api_key`、`password` 键名及原始邮箱/`AIza...`/`sk-...`/`Bearer ...` 模式；邮箱在进入 `store.py` 前只保留脱敏提示 `email_hint` 与不可逆截断哈希 `email_hash`。

---

## 4. 进程枚举、防 PID 复用与核心状态机

### 4.1 全量 `agy.exe` 枚举与防 PID 复用（FR-09 / AC-14 / PRD §9）
- 使用纯标准库 `ctypes` 调用 Win32 API：
  1. `CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)` + `Process32FirstW` / `Process32NextW` 枚举本机**所有** `agy.exe` 进程（同时覆盖 `segments` 表中的托管会话与未登记的原生非托管窗口）；
  2. 对每个进程调用 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)` + `GetProcessTimes` 提取 64 位 `FILETIME` 启动时间 `pid_create_time`，配合 `GetExitCodeProcess == STILL_ACTIVE (259)` 判断存活。
- **判定逻辑**：只有当 `pid` 存活 **且** `pid_create_time == segment.pid_create_time` 时，才认为该段仍在运行；若系统把死进程的 PID 复用到其他程序，直接识别为已退出并收敛为 `closed`。

### 4.2 `SessionSegment` 状态机（严格对齐 FR-08 / FR-12）
```text
                        (agy-pool start / switch)
                                    │
                                    ▼
                              ┌──────────┐  拉起后 3s 内退出
                              │ starting │──────────────────────► ┌────────┐
                              └────┬─────┘                        │ failed │ (保留交接包)
                                   │ 3s 存活确认 / HUD 心跳        └────────┘
                                   ▼
                              ┌──────────┐
                              │ running  │◄──(FR-08 硬约束：running 时 switch 最多冻结
                              └────┬─────┘     handoff.md，必须先 /exit 才能进换号)
                                   │ 进程退出 / 探活已死
                                   ▼
                              ┌──────────┐
                ┌────────────►│  closed  │◄───────────────┐
                │             └────┬─────┘                │
                │                  │ 冻结交接包并启动换号   │ switch --cancel 回滚
                │                  ▼                      │
                │         ┌─────────────────┐             │
                │         │ handoff_pending │─────────────┘
                │         │ (交接包已冻结)  │ (Ctrl+C 中断后重跑 switch 幂等续接)
                │         └────────┬────────┘
                │                  │ 换号通过账号校验 + 下一棒 seg(N+1) 3s 存活提交
                │                  ▼
                │           ┌────────────┐
                │           │ handed_off │
                │           └────────────┘
                │
       (新段 seg(N+1) 走同一生命周期，handoff_in_path 指向 seg(N) 的交接包)
```

### 4.3 两阶段切号、全局互斥租约与独立探针校验时序图
```text
用户终端           agy-pool (cli.py)           PoolStore (pool.db)       AgyRuntime (OS/引导)       新 agy + 探针
   │                      │                           │                         │                        │
   │ switch --to backup   │                           │                         │                        │
   ├─────────────────────►│                           │                         │                        │
   │                      │ 0. [Acquire Lease] BEGIN IMMEDIATE 抢占 meta['active_switch_lease']          │
   │                      │    ├─ 他窗口正换号(PID存活) ─► 退出码 3 拦截                                  │
   │                      │    └─ 死进程残留租约       ─► 自动回收 + 写 switch_lease_reclaimed            │
   │                      │                                                                              │
   │                      │ 1. reconcile + 日志回填: 探活已知段 + 扫描全机 agy.exe + 读 seg-<id>.json 回填日志 │
   │                      │    ├─ 源段仍 running ─► 仅冻结交接包，提示先 /exit (退出码 6, FR-08)          │
   │                      │    └─ 发现其他存活 agy (托管/非托管) 且未 --force(yes) ─► 退出码 3 拦截 (FR-09)│
   │                      │                                                                              │
   │                      │ 2. [Phase 1: Freeze]                                                         │
   │                      │    生成 handoffs/T1-seg1.md (≤3500 Tokens, 已脱敏)                            │
   │                      │    事务置 seg1=handoff_pending + 写 events ────────►│                        │
   │                      │                                                                              │
   │                      │ 3. [Phase 2: Guided Switch (独立探针隔离)]                                   │
   │                      │    记录 switch_started_at，注入 AGY_STATUS_STATE=.../probes/auth-<sid>.json  │
   │◄─────────────────────┼─ 提示: ① /logout ② 浏览器登录 backup ③ /exit 返回 ──│                        │
   │  登录完毕 /exit      │                                                                              │
   ├─────────────────────►│                                                                              │
   │                      │ 4. Fail-Closed 校验 (FR-10, 禁用共享 status-state.json):                     │
   │                      │    仅读取 auth-<sid>.json: 需存在 & ts>=switch_started_at & email非空 & 哈希匹配│
   │                      │    ├─ 缺失/过期/email空 ─► 退出码 1 拦截，禁起下一棒 (finally 清理探针)      │
   │                      │    ├─ 哈希不匹配       ─► 退出码 4 拦截，提示重试或 --cancel                 │
   │                      │    └─ 校验通过         ─► 删除 auth-<sid>.json，继续                         │
   │                      │                                                                              │
   │                      │ 5. [Phase 3: Launch & Commit]                                                │
   │                      │    注入 AGY_POOL_THREAD_ID / AGY_POOL_SEGMENT_ID / AGY_POOL_ACCOUNT            │
   │                      │         AGY_STATUS_STATE = ~/.antigravity/probes/seg-T1-seg2.json            │
   │                      │    拉起 agy -i "<≤250字单行短指令指向 T1-seg1.md>" ──────────────────────────►│
   │                      │    等待 3s 存活检查，finally 释放 active_switch_lease:                       │
   │                      │    ├─ 3s 内闪退 ─► seg2=failed, seg1 回退 closed, 保留交接包                   │
   │                      │    └─ 存活成功  ─► 事务: seg1=handed_off, seg2=running, active_account=backup  │
   │                      │◄──────────────────────────┤                         │   HUD 写回 seg-T1-seg2.json│
```

### 4.4 全局换号流程互斥锁（机器级命名互斥体 + DB 诊断租约）
- **问题背景**：SQLite 的 `busy_timeout` 只序列化毫秒级的数据库读写，无法让跨越“退出 → 浏览器登录 → 校验”的长流程排队；同时若两个终端通过 `AGY_POOL_DB` 指向不同数据库，仅靠 DB 内锁看不到彼此，但它们仍共享同一台机器的 Windows 登录态。
- **双层互斥机制**：
  1. **机器级硬锁（跨 `AGY_POOL_DB` 生效，`runtime.py`）**：
     - 换号前优先调用 Win32 `kernel32.CreateMutexW(None, False, mutex_name)`（默认 `Local\agy-pool-machine-switch-lock`，支持环境变量 `AGY_POOL_SWITCH_MUTEX` 与 `FakeRuntime` 隔离测试）+ 非阻塞 `WaitForSingleObject(h, 0)`；
     - 返回 `WAIT_TIMEOUT (0x102)` → 说明本机**任意终端**（无论 `AGY_POOL_DB` 指向何处）正在换号，直接以**退出码 `3`** 拦截；
     - 返回 `WAIT_OBJECT_0 (0x0)` 或 `WAIT_ABANDONED (0x80)`（前序进程强杀/崩溃后由 OS 内核自动回收，**永无僵尸锁**）→ 获取成功，`finally` 中执行 `ReleaseMutex(h)` + `CloseHandle(h)`。
  2. **DB 内诊断租约（同库详细诊断与审计，`store.py`）**：
     - 获取机器级互斥体后，再在单次 `BEGIN IMMEDIATE` 事务中抢占 `meta['active_switch_lease']`（持库锁 `< 2ms`），记录 JSON：`{"switch_id": "<uuid>", "thread_id": "T1", "from_account": "main", "to_account": "backup", "owner_pid": 12345, "owner_pid_create_time": 1344..., "started_at": "..."}`。
     - 若已有同库租约且 `is_process_alive(owner_pid, owner_pid_create_time)` 为真（且非本进程），以**退出码 `3`** 拒绝并输出正在换号的线程、账号与 PID；若持有进程已死则自动回收并写 `events(switch_lease_reclaimed)`。
     - 整个 `switch` 包在 `try ... finally` 中；无论正常完成、账号不符、`--cancel`、异常还是 `Ctrl+C`，都在 `finally` 中按 `switch_id` 做 CAS 释放 DB 租约，并释放 Win32 命名互斥体。

### 4.5 换号窗口独立状态探针与 Fail-Closed 校验（解决“共享状态文件认错账号”）
- **问题背景**：所有未隔离的 `agy` 窗口都会把状态写入同一个 `~/.antigravity/status-state.json`；一旦旧窗口仍在刷新，极易把旧窗口的 `email` 误判成刚换好号的新账号。
- **隔离方案（零改动现有 `agy-hud`）**：
  - 实测现有 `agy-hud/hud.py` 第 46 行已原生支持读取环境变量 `AGY_STATUS_STATE`。
  - `runtime.guide_account_switch()` 拉起临时引导换号 `agy` 窗口时，必须注入**本次换号专属探针路径**：`AGY_STATUS_STATE = ~/.antigravity/probes/auth-<switch_id>.json`（置于 `~/.antigravity/probes/` 而非 `pool.db` 内，与其他运行中的 `agy` 窗口完全物理隔离）。
- **Fail-Closed 严格校验规则**：
  - 临时换号窗口退出后，**严禁**回退读取全局 `status-state.json`，**仅**读取 `probes/auth-<switch_id>.json`（测试环境下支持通过注入读取器模拟）；
  - 必须同时满足 3 个条件才算通过：① 探针文件存在且 JSON 合法；② `timestamp >= switch_started_at`；③ `email` 非空且计算出的 `email_hash == target.email_hash`。
  - **任一条件不满足一律禁止启动下一棒**：`email_hash` 不匹配返回**退出码 `4`**；探针文件缺失、过期、JSON 损坏或 `email` 为空返回**退出码 `1`**，并明确提示用户重试或执行 `switch --cancel`。
  - 无论校验成败，在 `finally` 中立即删除 `auth-<switch_id>.json`，确保明文邮箱不在磁盘滞留。

### 4.6 会话日志绑定机制与 M2 / M3 验收边界（解决“M2 启动下一棒没带对话”）
- **问题背景**：`HandoffBuilder` 需要 `transcript.jsonl` 才能抽取 L2 对话精华；若把 `conversation_id` / `transcript_path` 回填完全推迟到 M3（改造 `agy-hud`），M2 真机跑起来只能打出缺对话的半成品交接包。
- **M2 零侵入日志绑定（直接复用现有 `agy-hud` 能力）**：
  1. `start` 与 `switch` 拉起托管 `agy` 时，为每棒注入专属段探针环境变量：`AGY_STATUS_STATE = ~/.antigravity/probes/seg-<segment_id>.json`（现有未改动的 `agy-hud` 每次渲染会自动把 `session_id` 即 `conversation_id` 写入该文件）。
  2. 执行 `handoff` 或 `switch` 冻结交接包前，若 `segments.transcript_path` 仍为空，按以下优先级自动回填并落库：
     - ① 若用户显式传入 `--transcript <path>`（逃生舱与自动化测试入口），直接使用；
     - ② 否则读取 `probes/seg-<segment_id>.json` 的 `session_id`，按确定性路径依次探测 `~/.gemini/<app_dir>/brain/<conv_id>/.system_generated/logs/transcript.jsonl`（`<app_dir>` 依次为 `antigravity-cli`、`antigravity`、`antigravity-ide`），存在即回填 `conversation_id` 与 `transcript_path` 到 `segments` 表。
  3. 当段进入 `closed` 或 `handed_off` 且已完成回填时，顺带清理对应的 `seg-<segment_id>.json`；若本机未启用 `agy-hud` 且未传 `--transcript`，则降级为仅 L1 交接包，并输出**显式黄色警告**（说明缺失对话原因及通过 `--transcript` 或安装 `agy-hud` 解决）。
- **明确里程碑验收边界**：
  - **M2 验收范围**：CLI 全命令、全局换号租约互斥、独立探针 Fail-Closed 防错号、以及在已启用现有 `agy-hud` 环境下通过 `seg-<segment_id>.json` 自动回填 `transcript_path` 生成完整 L1+L2+L3 交接包（未启用 HUD 时仅验收命令、保护机制与黄色降级警告）；
  - **M3 验收范围**：升级 `agy-hud` 实现实时线程徽章、配额直写 `pool.db`、低额三态提醒与运行中账号不符红警；
  - **M4 验收范围**：双窗口真机全流程接力验收。

---

## 5. 交接包生成与锚点门禁（`handoff.py`）

### 5.1 锚点硬门禁（FR-02 / AC-12）
- 执行 `agy-pool start` 或 `agy-pool anchor edit` 保存后，立即调用 `validate_anchor(text, max_tokens=600)`；
- 若估算 Token 数 `> 600`，拒绝创建线程或拒绝落盘新锚点，报错并精确输出当前 Token 数与 `600` 上限，提示精简条目。

### 5.2 三层结构与裁剪顺序（总预算硬上限 `3,500 Tokens`）
1. **L1 任务现状（目标预算 1,200 Tokens，锚点永不裁剪）**：
   - `anchors/<tid>.md` 全文（最高优先级，**任何情况下永不裁剪**）；
   - `brain/<conv_id>/task.md` 复选框进度；
   - `git status --porcelain` + `git diff --stat`（非 Git 目录优雅降级标注 `"非 Git 仓库"`）。
2. **L2 对话精华（目标预算 2,000 Tokens，去噪）**：
   - 按 `step_index` 去重（跳过尾部半截非法 JSON 行；缺失时降级标注 `"暂无历史对话记录"`）；
   - 提取最近 6 轮 `USER_INPUT` 与 `PLANNER_RESPONSE` 结论、改动文件路径表、最近报错摘要（每条 ≤ 300 字符）；
   - **全部丢弃**大段 `read_file` 原文、`run_command` 正常 stdout 与思考草稿。
3. **L3 溯源指针（预算 300 Tokens）**：
   - 上一棒 `segment_id`、`account_id`、`conversation_id`、`transcript_path` 与接力链轨迹（如 `#1(main) -> #2(backup)`）。
- **超预算裁剪顺序（PRD §9）**：
  1. 精简 L3 非必要展示文本（保留 `segment_id` 与 `transcript_path`）；
  2. 逐轮移除 L2 最老对话轮次（保底保留最近 2 轮 + 改动文件表 + 最新报错）；
  3. 压缩 L2 单轮摘要字符上限；
  4. 若 L1 因超大 `git diff --stat`（如改动数百个文件）挤压整体预算，截断文件清单为前 25 个并附 `...及另外 N 个文件`——**`anchors/<tid>.md` 全文始终 100% 原样保留**。

### 5.3 Token 估算与出库脱敏
- **估算公式**：`math.ceil(cjk_chars / 1.5 + other_chars / 4.0)`。
- **出库脱敏**：所有写入 `handoff.md` 的内容经过正则脱敏（`AIza[0-9A-Za-z_-]{35}`、`sk-[A-Za-z0-9_-]{20,}`、`Bearer\s+\S+`、`x-antigravity-csrf-token`、`gh[pousr]_[A-Za-z0-9_]{20,}` 等），统一替换为 `[REDACTED]`。

### 5.4 `agy -i` 短引导词模板（无换行、≤ 250 字）
```text
你是任务线程 T1（重构登录鉴权）的第 2 棒接力助手。请先用 read_file 读取交接包 C:/Users/Administrator/.antigravity/agy-pool/handoffs/T1-seg1.md，用3-5条简短要点复述目标、已完成进度和下一步行动后继续推进，不要编造交接包或仓库外信息。
```

### 5.5 上游数据真实格式样本（严禁猜测 OpenAI 格式）

1. **`transcript.jsonl`（按 `step_index` 覆盖去重，不是 `{"role":"user","content":...}`）**：
```jsonl
{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": "2026-11-26T03:00:00Z", "content": "请重构登录鉴权模块并补单测"}
{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "IN_PROGRESS", "created_at": "2026-11-26T03:00:05Z", "model": "gemini-4-neon-high", "content": "思考草稿片段..."}
{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-11-26T03:00:10Z", "model": "gemini-4-neon-high", "content": "已完成 auth.py 中间件拆分", "tool_calls": [{"name": "edit_file", "args": {"Path": "C:/repo/auth.py"}}, {"name": "run_command", "args": {"CommandLine": "pytest", "toolSummary": "Run tests"}}]}
{"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "ERROR", "created_at": "2026-11-26T03:00:12Z", "content": "AssertionError: token expired check failed"}
```
   - **解析规则**：同一 `step_index` 多次出现时以后者覆盖前者（流式增量最终态）；仅从 `type == "USER_INPUT"` 取用户指令，从 `type == "PLANNER_RESPONSE"` 取模型结论与 `tool_calls`（其中 `edit_file`/`write_file` 的 `args.Path` 提取为改动文件清单），从 `status == "ERROR"` 提取最近报错（每条截断 ≤ 300 字符）；`GENERIC` 中非 `ERROR` 的大段工具返回值全部丢弃。

2. **`~/.antigravity/status-state.json`（支持 `AGY_STATUS_STATE` 环境变量覆盖）**：
```json
{
  "email": "user@example.com",
  "plan_tier": "Google AI Pro",
  "session_id": "a2eb9845-fe56-4112-b12b-5c0537ef48df",
  "model": "Gemini 4 Neon (High)",
  "timestamp": 1795663800.12
}
```
   - 读取后立即在内存中生成 `email_hint`（如 `u***@example.com`）与 `email_hash`（`hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]`），**绝不**将明文 `email` 写入 `pool.db` 或 `events`。

3. **`brain/<conv_id>/task.md`（复选框解析正则）**：
   - 正则：`^\s*(?:[-*+]|\d+\.)\s+\[([ xX/])\]\s+(.+)$`
   - 状态映射：`x`/`X` → `completed`、`/` → `in_progress`、空格 → `pending`。

---

## 6. `agy-hud` 最小侵入式改造方案

> **前置同步要求**：M3 动工前，必须先在 `agy-hud` 目录执行 `git pull origin main` 合入远端最新提交 `713dfd1`，再基于最新版本实施改造。

保持 `agy-hud/hud.py` 单文件零第三方依赖、**绝不 `import agy_pool`**，仅通过环境变量 + 标准库 `sqlite3` 访问 `~/.antigravity/agy-pool/pool.db`。

### 6.1 配置新增（`agy-hud/config.json` & `hud.py` `DEFAULT_CONFIG` & `install.ps1`）
```json
"pool": {
  "enabled": true,
  "showThreadBadge": true,
  "lowQuotaThresholdPercent": 15,
  "syncLiveTelemetry": true,
  "liveSyncIntervalSeconds": 5
}
```

### 6.2 `hud.py` 精确改动设计
1. **路径与常量**：新增 `POOL_DB_FILE`（支持 `AGY_POOL_HOME` / `AGY_POOL_DB` 覆盖，默认 `~/.antigravity/agy-pool/pool.db`）与 `I18N` 提示词条。
2. **`load_pool_context(config)`**：
   - 检查环境变量 `AGY_POOL_THREAD_ID` / `AGY_POOL_SEGMENT_ID` / `AGY_POOL_ACCOUNT`；
   - **非托管会话**（无环境变量）直接返回 `None`，零 SQLite 打开开销、零输出变化（满足 NFR-02 / AC-10）；
   - **托管会话**使用 `sqlite3.connect(f"file:{POOL_DB_FILE}?mode=ro", uri=True, timeout=0.2)` 单次只读查询当前段 `seq`、所有备用槽位配额快照与目标槽位 `email_hash`；库缺失或 `sqlite3.Error` 静默返回 `None`。
3. **`sync_pool_live_telemetry(data, stdin_pools, pool_ctx, config, now)`**：
   - 在 `main()` 写入 `status-state.json` 旁调用；按 `liveSyncIntervalSeconds`（默认 5s，或首次拿到 `conversation_id` 时立即）执行一次轻量 `UPDATE` 写入 `conversation_id`、`transcript_path`、`observed_email_hint`、`observed_email_hash`、`last_heartbeat_at` 及当前账号 `accounts` 配额快照；失败静默忽略。
4. **第 1 行徽章（`render_project_line()`）**：在 `[Model]` 徽章后追加青绿色 `[T1·main·#1]`。
5. **第 2 行尾部告警（`render_usage_segment()`，严格按优先级互斥展示）**：
   - **优先级 1（红色，FR-10）**：若 `stdin.email` 计算出的哈希与当前槽位 `email_hash` 不一致 → `⚠ 账号不符：实际 a***@gmail.com`；
   - **优先级 2（橙色，FR-06 三态）**：若当前账号 `gemini-5h` 剩余 `<= lowQuotaThresholdPercent`（默认 `15%`），选择备用槽位按三态渲染：
     - 有快照且未过重置时间：`⚠ 建议切换 backup（94%·2h前）`
     - 快照记录的 `gemini_5h_reset_at <= now`：`⚠ 建议切换 backup（已重置）`
     - 备用槽位从未记录过配额：`⚠ 建议切换 backup（未知余量）`

---

## 7. 分阶段开发计划（M1~M4）与 `pytest` 测试矩阵

### 7.1 里程碑计划
- **M0 预研**：✅ 已完成（见 [§15](#15-m0-预研实测结论)）。
- **M1 内核（`store.py` + `handoff.py`）**：SQLite WAL 5 表与迁移、零凭据守卫、600 Token 锚点校验、L1/L2/L3 交接包生成与 3,500 Token 裁剪、脱敏 → 对应 AC-06 / AC-11 / AC-12。
- **M2 编排与 CLI（`runtime.py` + `cli.py` + `install.ps1`）**：Win32 `agy.exe` 全量枚举、`(pid, create_time)` 探活、`account add/list/remove`、`start`、`anchor edit`、`handoff`、`switch`（含 `--cancel` / `--force`）、`status`、`list`、`close`、`resume` → 对应 AC-01 / AC-02 / AC-05 / AC-08 / AC-09 / AC-13 / AC-14 / AC-15。
- **M3 HUD 联动**：先 `git pull origin main` 同步 `agy-hud` 远端 `713dfd1`，再修改 `hud.py`、`config.json`、`install.ps1`、`test_hud.py` → 对应 AC-03 / AC-04 / AC-07 / AC-10 + 原有测试全绿。
- **M4 真机演练**：双窗口并行 + `main → backup → main` 真实 `/logout` + OAuth 登录 + `agy -i` 全流程实测验收。

### 7.2 自动化测试矩阵与运行命令

| 测试文件 | 核心用例与覆盖标准 |
|---|---|
| `agy-pool/tests/test_store.py` | 初始化幂等、WAL 并发 `busy_timeout`、事务回滚、schema 备份迁移、零凭据键名与原文守卫、`events` 审计记录（AC-01, AC-11, NFR-01, NFR-04） |
| `agy-pool/tests/test_handoff.py` | 锚点 600 Token 拦截、L1/L2/L3 生成、`step_index` 去重、三棒回切锚点零失真、10 万 Token 超大日志裁剪至 ≤ 3,500 Tokens、Token 公式边界、缺失上下文降级、正则 `[REDACTED]` 脱敏（AC-05, AC-06, AC-11, AC-12） |
| `agy-pool/tests/test_runtime.py` | Win32 `ctypes` `agy.exe` 枚举与 `(pid, create_time)` 防复用、3 秒闪退判 `failed`、无黑框标志核验、`FakeRuntime` 契约（AC-09, AC-14） |
| `agy-pool/tests/test_cli_e2e.py` | `account add --from-current`、`start`、`anchor edit`、源段存活禁换号、托管+非托管并发拦截与 `--force` 输入 `yes`、误登账号阻断、`Ctrl+C` 续接与 `switch --cancel`、`list --all --json`、`close`（AC-01, AC-02, AC-05, AC-07~09, AC-13~15） |
| `agy-hud/test_hud.py` | 既有全量测试绿灯 + 非托管零差异、`pool.db` 缺失/损坏静默降级、徽章渲染、账号不符红色告警优先、低额三态文案（AC-03, AC-04, AC-07, AC-08, AC-10, NFR-02, NFR-03） |

```powershell
& "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe" -m pytest C:\Users\Administrator\Desktop\cspace\agy-pool\tests -v
& "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe" -m unittest discover -s C:\Users\Administrator\Desktop\cspace\agy-hud -p "test_*.py" -v
```

---

## 8. CLI 参数、退出码与 `--json` 输出契约

### 8.1 子命令与参数规范
| 命令 | 参数与可选项 | 说明 |
|---|---|---|
| `agy-pool init` | — | 初始化 `AGY_POOL_HOME` 目录与 `pool.db`（幂等，不覆盖已有数据） |
| `agy-pool account add` | `<id> --from-current [--label <text>]` | 读取 `status-state.json` 当前账号，只存 `email_hint` + `email_hash` |
| `agy-pool account list` | `[--json]` | 列出所有已注册账号槽位及配额快照 |
| `agy-pool account remove` | `<id>` | 移除未绑定活跃线程的账号槽位 |
| `agy-pool start` | `<title> --account <id> [--anchor-text <text> \| --anchor-file <path>] [--cwd <dir>]` | 校验锚点 ≤ 600 Tokens，创建线程与 `#1` 段并拉起托管 `agy` |
| `agy-pool anchor edit` | `[<tid>] [--text <text> \| --file <path>]` | 编辑或替换线程锚点，保存前强制校验 ≤ 600 Tokens |
| `agy-pool handoff` | `[<tid>] [--transcript <path>]` | 仅生成/预览三层交接包 `handoffs/<tid>-seg<N>.md`，不触发换号（支持手动指定日志覆盖） |
| `agy-pool switch` | `[<tid>] --to <account_id> [--transcript <path>] [--force] [--cancel]` | 两阶段切号（带全局租约互斥 + 独立探针校验）；`--cancel` 回滚；`--force` 须交互输入 `yes` |
| `agy-pool status` | `[<tid>] [--json]` | 查看当前（或指定）线程详情、最新棒次及双槽位余量 |
| `agy-pool list` | `[--all] [--json]` | 列出活跃线程与账号池总览；`--all` 包含已 `closed` 线程 |
| `agy-pool close` | `[<tid>]` | 将线程标记为 `closed`（保留文件与历史，从默认视图隐藏） |
| `agy-pool resume` | `[<tid>]` | （Phase 2 / Could）基于最新交接包在当前账号开启新一棒清空上下文续聊 |

### 8.2 环境变量约定
- **`AGY_POOL_HOME`**：运行时根目录覆盖（默认 `~/.antigravity/agy-pool`）
- **`AGY_POOL_DB`**：SQLite 库文件路径覆盖（默认 `$AGY_POOL_HOME/pool.db`）
- **`AGY_POOL_SWITCH_MUTEX`**：跨库机器级换号互斥体名称覆盖（可选，测试隔离用，默认 `Local\agy-pool-machine-switch-lock`）
- **`AGY_STATUS_STATE`**：AGY 状态文件路径覆盖（默认 `~/.antigravity/status-state.json`）。`account add --from-current` 读取当前 `AGY_STATUS_STATE`；而 `agy-pool` 拉起临时换号窗口时会强注入隔离路径 `~/.antigravity/probes/auth-<switch_id>.json`，拉起托管段时会注入 `~/.antigravity/probes/seg-<segment_id>.json`
- **`AGY_POOL_THREAD_ID` / `AGY_POOL_SEGMENT_ID` / `AGY_POOL_ACCOUNT`**：托管启动 `agy` 时自动注入到子进程环境变量中，供子窗口 `agy-hud` 与子终端命令识别身份。

### 8.3 固定退出码表
| 退出码 | 含义 | 典型触发场景 |
|---:|---|---|
| `0` | 成功 | 命令正常完成 |
| `1` | 运行期 / IO / 拉起异常 | `pool.db` 损坏或版本过高、`agy` 拉起后 3s 内闪退、锁等待超时、**独立换号探针缺失/过期/不可读（Fail-Closed 拒绝启动下一棒）** |
| `2` | 参数或校验失败 | 空标题、未注册账号槽位、`--to` 与当前账号相同、未指定 `<tid>` 且当前目录有多个活跃线程歧义 |
| `3` | 并发冲突拦截 | 检测到其他存活 `agy.exe`（托管或非托管）且未 `--force` 确认（对齐 PRD 原型 05），或另一终端正持有 `active_switch_lease` / **机器级命名互斥体 `Local\agy-pool-machine-switch-lock`** |
| `4` | 账号一致性校验失败 | 引导换号后实际登录账号 `email_hash` 与目标槽位不符（FR-10） |
| `5` | 锚点超限 | `start` / `anchor edit` 时锚点内容估算超过 `600 Tokens`（FR-02 / AC-12） |
| `6` | 源会话仍存活禁切号 | 源段仍处于 `running` 状态，仅冻结交接包并提示先 `/exit`（FR-08 / AC-13） |
| `130` | 用户中断 | 切号确认过程中按 `Ctrl+C`（保留 `handoff_pending` 与交接包，支持幂等续接） |

### 8.4 `--json` 稳定输出结构

**`agy-pool list --json`**：
```json
{
  "active_machine_account": "backup",
  "accounts": [
    {
      "account_id": "main",
      "label": "主力账号",
      "email_hint": "a***@gmail.com",
      "status": "active",
      "gemini_5h_remaining": 0.08,
      "gemini_5h_reset_at": "2026-11-26T14:20:00Z",
      "gemini_7d_remaining": 0.72,
      "quota_updated_at": "2026-11-26T11:29:40Z"
    }
  ],
  "threads": [
    {
      "thread_id": "T1",
      "title": "重构登录鉴权",
      "cwd": "C:/Users/Administrator/Desktop/cspace",
      "status": "active",
      "current_segment_id": "T1-seg2",
      "current_seq": 2,
      "current_account_id": "backup",
      "current_segment_status": "running"
    }
  ]
}
```

**`agy-pool status [<tid>] --json`**：
```json
{
  "thread": {
    "thread_id": "T1",
    "title": "重构登录鉴权",
    "cwd": "C:/Users/Administrator/Desktop/cspace",
    "anchor_path": "anchors/T1.md",
    "status": "active",
    "segment_count": 2
  },
  "current_segment": {
    "segment_id": "T1-seg2",
    "seq": 2,
    "account_id": "backup",
    "status": "running",
    "pid": 22108,
    "conversation_id": "a2eb9845-fe56-4112-b12b-5c0537ef48df",
    "handoff_in_path": "handoffs/T1-seg1.md"
  },
  "accounts": [
    {"account_id": "main", "email_hint": "a***@gmail.com", "gemini_5h_remaining": 0.08},
    {"account_id": "backup", "email_hint": "b***@gmail.com", "gemini_5h_remaining": 0.94}
  ]
}
```

---

## 15. M0 预研实测结论

> 已于 2026-11-26 在本机（Windows + Python 3.12）完成非破坏性技术验证（未触发 `/logout` 以保持当前工作会话稳定）：

1. **M0-1 `stdin.email` 可用性**：✅ **已验证可用**
   - 实测读取本机 `~/.antigravity/status-state.json`（由 `agy` 调用 `agy-hud` 写入），确认包含 `email`（已脱敏核验非空）、`plan_tier`（`Google AI Pro`）、`session_id`、`model`、`timestamp` 字段；
   - 可直接支撑 `account add --from-current`、换号后账号一致性校验（FR-10）以及 HUD 运行中账号不符红色告警，同时保留本地 LanguageServer `GetUserStatus` 作为缺失兜底。
2. **M0-2 Win32 进程枚举与 SQLite WAL 性能**：✅ **已验证远超指标**
   - 纯 Python 3.12 `ctypes` 调用 `CreateToolhelp32Snapshot` + `GetProcessTimes` 全量枚举本机 `agy.exe`（返回 `pid`、`ppid` 与 64 位启动 `FILETIME`），实测耗时 **6.56 ms**，零子进程、无黑框弹窗；
   - Python 3.12 `sqlite3` WAL 模式只读查询 20 次平均耗时 **0.747 ms**（远低于 NFR-03 的 `≤ 5 ms` P95 限制）。
3. **M0-3 `agy -i` 参数与 `/logout` 单登录态确认**：✅ **已确认前提**
   - 实测本机 `agy --help` 原生支持 `-i`（`--prompt-interactive`）与 `-r`（`--resume`），且**无** `login/logout` CLI 子命令；
   - 本机 `GEMINI_API_KEY` 未设置，登录态统一由 Windows 凭据管理器维护，确认为全机单登录态（`/logout` 会影响所有存活 `agy` 窗口，验证了 FR-09 全量并发拦截的必要性）；
   - 交互式 `/logout` 浏览器换号及 `agy -i` 完整对话接力留待 **M4 真机演练** 阶段一并验收。
