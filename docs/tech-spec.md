# `agy-pool` × `agy-hud` 双 Gemini 账号池与无缝接力 — 详细技术方案

> 技术栈：**纯 Python 3.12 标准库、零第三方依赖**（与 `agy-hud` 保持一致），测试框架：`pytest` + `unittest`。

---

## 1. 领域模型与关键架构决策（ADRs）

### 1.1 领域词汇表（GLOSSARY）
- **`TaskThread`（逻辑任务线程）**：用户要完成的一件独立任务（如 `T1`），跨账号、跨会话持续存在，唯一绑定一份置顶的 `Anchor`。
- **`Anchor`（任务锚点）**：记录 `TaskThread` 的不变目标、验收标准与硬性约束的 Markdown 文档，每棒交接必完整携带。
- **`AccountProfile`（账号槽位）**：对一个逻辑 Gemini 账号的只读元数据描述（别名、可选邮箱脱敏提示、最近观测配额快照、健康状态），**不含任何凭据**。
- **`SessionSegment`（物理会话段）**：某一次真实 `agy` 进程运行周期（`seq=1,2,3...`），归属于一个 `TaskThread` 和一个 `AccountProfile`，绑定到 AGY 真实的 `conversation_id` 与 `transcript_path`。
- **`HandoffBundle`（结构化交接包）**：两个相邻 `SessionSegment` 之间传递的 L1/L2/L3 精简 Markdown 接力棒。
- **`LiveTelemetry`（实时遥测分片）**：每个运行中的 `agy-hud` 进程独立上报的轻量状态快照（`conversation_id`、`transcript_path`、最新配额、心跳时间）。
- **`AccountAdapter`（账号切换适配器）**：把“如何合法切换本机账号”封装在统一接口后面的可替换组件。

### 1.2 关键架构决策记录（ADRs）

- **ADR-001：`state.json` 单写者 + `live/<segment_id>.json` 分片心跳**
  - **背景**：多个终端的 `agy-hud` 会高频刷新，若直接读写共享 `state.json`，Windows 下多进程 `os.replace` 极易触发 `PermissionError (WinError 5/32)` 或覆盖丢更新。
  - **决策**：`agy-pool` 作为 `state.json` 的**唯一写者**（配合 `state.lock`）；每个 `agy-hud` 进程**只读** `state.json`（按 `mtime` 缓存），并只原子写自己专属的 `live/<segment_id>.json`（5 秒防抖）。`agy-pool` 运行任意命令时再将 `live/*.json` 合并入 `state.json`。
- **ADR-002：账号切换走 `AccountAdapter` Seam，坚持零凭据与两阶段提交**
  - **背景**：实测本机 `agy --help` 无 `--profile` 子命令，默认通过 Windows 凭据管理器维护单账号 Google OAuth 登录态（`GEMINI_API_KEY` 未设置）。
  - **决策**：切号动作抽离为 `AccountAdapter` 协议；切换前先完成 Phase 1（生成交接包 + `handoff_pending`），再执行 `Adapter.switch()`，最后启动新段提交 `handed_off`。无论哪一步中断，状态可回滚或幂等续接，且核心代码**零接触凭据**。
- **ADR-003：`agy -i` 传“指针型短指令”而非全量交接正文**
  - **背景**：Windows 下 `CreateProcess` 参数上限 32,767 字符，且经由 PowerShell / `.cmd` shim 传递含换行与引号的长 Markdown 极易转义错乱。
  - **决策**：交接正文落盘到 `~/.antigravity/agy-pool/handoffs/<tid>-seg<N>.md`，`agy -i` 仅传入约 150 字单行启动词指向该路径，彻底绕开命令行长度与转义风险。

---

## 2. 包结构与深模块设计

### 2.1 目录与包结构
```text
C:\Users\Administrator\Desktop\cspace\
├── agy-hud\                         # 现有目录（最小侵入增强，绝不 import agy_pool）
│   ├── hud.py                       # 只读 state.json + 写 live/<seg>.json + 徽章/低额告警
│   ├── config.json                  # 新增 pool 配置段
│   ├── install.ps1                  # 升级配置迁移
│   └── test_hud.py                  # 原 13 项全保留 + 新增 pool 联动测试
│
└── agy-pool\                        # 独立控制器包（纯 Python 3.12 标准库）
    ├── agy_pool\
    │   ├── __init__.py
    │   ├── models.py                # Dataclass 实体、枚举、不变式校验
    │   ├── store.py                 # PoolStore：state.lock + 原子读写 + live 合并 + Win32 PID 探活
    │   ├── handoff.py               # HandoffBuilder：L1/L2/L3 抽取 + Token 预算 + 脱敏
    │   ├── adapters.py              # Seam：AccountAdapter / SessionLauncher 及具体实现
    │   ├── orchestrator.py          # RelayOrchestrator：状态机、并发检查、两阶段切号、回滚
    │   └── cli.py                   # CLI 入口（argparse：init/start/status/list/handoff/switch/resume）
    ├── tests\
    │   ├── test_store.py
    │   ├── test_handoff.py
    │   ├── test_orchestrator.py
    │   └── test_cli_e2e.py
    ├── agy-pool.cmd                 # Windows shim（调用 Python 3.12 -m agy_pool.cli）
    ├── install.ps1                  # 初始化 ~/.antigravity/agy-pool 目录与 PATH shim
    └── README.md
```

### 2.2 深模块接口与 Seam 划分

| 模块 | 对外精简接口 | 隐藏的内部复杂性 | 可测性 Seam |
|---|---|---|---|
| `PoolStore` | `load()`, `transaction()`, `reconcile_live()`, `is_pid_alive(pid)` | `state.lock` 跨进程锁与僵尸锁回收、Windows `os.replace` 重试、schema 迁移、`live/*.json` 合并、Win32 PID 探活 | 注入 `root_dir: Path` 与 `pid_checker` |
| `HandoffBuilder` | `build(thread, segment, anchor_text, git_info) -> HandoffResult` | `transcript.jsonl` 按 `step_index` 去重、工具噪音过滤、`task.md` 解析、`git diff --stat` 采集、Token 估算与裁剪、正则脱敏 | 纯数据变换 + 依赖注入采集器 |
| `AccountAdapter` | `requires_exclusive_machine_auth`, `prepare_switch()`, `execute_switch()`, `build_launch_env()` | 原生引导 `/logout`+OAuth vs `GEMINI_API_KEY` 隔离 vs 外部命令适配 | `ManualGuidedAdapter` / `EnvVarAdapter` / `ExternalCommandAdapter` / `FakeAccountAdapter` |
| `SessionLauncher` | `launch(cwd, prompt, env, extra_args) -> LaunchResult` | Windows 查找 `agy.exe`、注入 `AGY_POOL_*` 环境变量、拉起交互进程并监控早期退出 | `FakeSessionLauncher` |
| `RelayOrchestrator` | `start_thread()`, `prepare_handoff()`, `switch_account()`, `resume_segment()` | 并发冲突拦截矩阵、两阶段状态迁移、异常回滚与幂等重入 | 组合以上 4 个模块全链路单测 |

---

## 3. 持久化目录与 Schema（v1）

### 3.1 运行时目录布局（`~/.antigravity/agy-pool/`）
```text
~/.antigravity/agy-pool/
├── state.json                       # 唯一权威注册表（仅 agy-pool 写入）
├── state.lock                       # 写锁文件（含 pid & created_at）
├── anchors/
│   └── T1.md                        # 逻辑线程 T1 的不变原始目标与约束
├── handoffs/
│   ├── T1-seg1.md                   # 第 1 棒 → 第 2 棒的交接包
│   └── T1-seg2.md                   # 第 2 棒 → 第 3 棒的交接包
└── live/
    ├── T1-seg1.json                 # 仅由 T1-seg1 的 agy-hud 进程原子写
    └── T2-seg1.json                 # 仅由 T2-seg1 的 agy-hud 进程原子写
```

### 3.2 `state.json` Schema
```json
{
  "schema_version": 1,
  "updated_at": "2026-11-26T11:30:00Z",
  "adapter_mode": "manual_guided",
  "active_machine_account": "main",
  "next_thread_seq": 2,
  "accounts": {
    "main": {
      "account_id": "main",
      "label": "主力账号",
      "email_hint": "a***@gmail.com",
      "status": "active",
      "quota_snapshot": {
        "gemini_5h_remaining": 0.08,
        "gemini_5h_reset_at": "2026-11-26T14:20:00Z",
        "gemini_7d_remaining": 0.72,
        "updated_at": "2026-11-26T11:29:40Z"
      },
      "cooldown_until": null
    },
    "backup": {
      "account_id": "backup",
      "label": "备用账号",
      "email_hint": "b***@gmail.com",
      "status": "ready",
      "quota_snapshot": {
        "gemini_5h_remaining": 0.94,
        "gemini_5h_reset_at": "2026-11-26T16:00:00Z",
        "gemini_7d_remaining": 0.90,
        "updated_at": "2026-11-26T09:10:00Z"
      },
      "cooldown_until": null
    }
  },
  "threads": {
    "T1": {
      "thread_id": "T1",
      "title": "改造 agy-hud 与双账号池",
      "cwd": "C:/Users/Administrator/Desktop/cspace",
      "anchor_path": "C:/Users/Administrator/.antigravity/agy-pool/anchors/T1.md",
      "status": "active",
      "current_segment_id": "T1-seg1",
      "segment_count": 1,
      "created_at": "2026-11-26T10:00:00Z",
      "updated_at": "2026-11-26T11:29:40Z"
    }
  },
  "segments": {
    "T1-seg1": {
      "segment_id": "T1-seg1",
      "thread_id": "T1",
      "seq": 1,
      "account_id": "main",
      "parent_segment_id": null,
      "conversation_id": "a2eb9845-fe56-4112-b12b-5c0537ef48df",
      "transcript_path": "C:/Users/Administrator/.gemini/antigravity-cli/brain/a2eb9845-.../transcript.jsonl",
      "handoff_in_path": null,
      "handoff_out_path": null,
      "pid": 18240,
      "status": "running",
      "started_at": "2026-11-26T10:00:02Z",
      "ended_at": null,
      "last_heartbeat_at": "2026-11-26T11:29:40Z"
    }
  }
}
```
- **Schema 迁移与安全守卫**：低于当前版本时先备份 `state.json.v{old}.bak` 再顺序迁移；高于当前版本时拒绝写入。`PoolStore.save()` 内置递归键名断言，严禁出现 `token` / `cookie` / `secret` / `api_key` / `password`。

### 3.3 `live/<segment_id>.json` Schema（由 `agy-hud` 写入）
```json
{
  "segment_id": "T1-seg1",
  "thread_id": "T1",
  "account_id": "main",
  "conversation_id": "a2eb9845-fe56-4112-b12b-5c0537ef48df",
  "transcript_path": "C:/Users/.../transcript.jsonl",
  "observed_email_hint": "a***@gmail.com",
  "plan_tier": "Google AI Pro",
  "agent_state": "idle",
  "quota": {
    "gemini_5h_remaining": 0.08,
    "gemini_5h_reset_at": "2026-11-26T14:20:00Z",
    "gemini_7d_remaining": 0.72
  },
  "updated_at": 1795663800.12
}
```

---

## 4. 核心状态机与两阶段交接时序图

### 4.1 `SessionSegment` 状态机
```text
                        (agy-pool start / switch)
                                    │
                                    ▼
                              ┌──────────┐
                ┌────────────►│ starting │ (尚未收到首帧 live 心跳)
                │             └────┬─────┘
                │                  │ 进程存活 / 首帧 live 写入
                │                  ▼
                │             ┌──────────┐  进程正常退出 / 探活已死
                │             │ running  │──────────────────────────┐
                │             └────┬─────┘                          │
                │                  │ 用户发起 switch / handoff       ▼
       回滚/重试│                  ▼                           ┌────────┐
                │         ┌─────────────────┐                  │ closed │
                └─────────│ handoff_pending │◄─────────────────┘        │
              (切号取消/   │ (已冻结交接包)  │   (已退出段也可发起       │
               拉起失败)   └───────┬─────────┘    向下一棒交接)          │
                                  │ 新段 seg(N+1) 拉起并提交             │
                                  ▼                                     │
                            ┌────────────┐                              │
                            │ handed_off │                              │
                            └────────────┘                              │
```

### 4.2 两阶段切号与并发保护时序图
```text
用户终端           agy-pool (Orchestrator)      PoolStore / live/        AccountAdapter          新 agy + agy-hud
   │                         │                         │                       │                        │
   │ switch T1 --to backup   │                         │                       │                        │
   ├────────────────────────►│                         │                       │                        │
   │                         │ 1. acquire(state.lock)  │                       │                        │
   │                         │    reconcile(live/*.json) & Win32 PID 探活       │                        │
   │                         ├────────────────────────►│                       │                        │
   │                         │                         │                       │                        │
   │                         │ 2. 并发冲突检测：若 adapter.requires_exclusive_machine_auth==True         │
   │                         │    检查是否存在其他 thread 的 running 段占用旧账号                         │
   │                         │    ├─ 有冲突且未 --force ─► 释放锁，返回冲突清单并中止                     │
   │                         │    └─ 无冲突 ─► 继续                             │                        │
   │                         │                                                 │                        │
   │                         │ 3. [Phase 1: Prepare]                           │                        │
   │                         │    构建 handoffs/T1-seg1.md (原子落盘)           │                        │
   │                         │    原子更新 state.json: seg1=handoff_pending     │                        │
   │                         ├────────────────────────►│                       │                        │
   │                         │                         │                       │                        │
   │                         │ 4. 释放 state.lock (避免阻塞人工 OAuth 等待)     │                        │
   │                         │ 5. [Phase 2: Switch] ──────────────────────────►│                        │
   │◄────────────────────────┼─ (若手动模式：提示完成官方 /logout 与切换确认) ───│                        │
   │   确认已切 / 或 Ctrl+C  │                                                 │                        │
   ├────────────────────────►│                                                 │                        │
   │                         │    ├─ 取消/失败 ─► 保留 T1-seg1.md，不建 seg2，提示可重入                  │
   │                         │    └─ 成功 ─► 重新加锁创建 seg2(starting)        │                        │
   │                         │                                                 │                        │
   │                         │ 6. [Phase 3: Launch & Commit]                   │                        │
   │                         │    拉起 agy -i "<短引导词指向 T1-seg1.md>"       │                        │
   │                         │    注入 ENV: AGY_POOL_THREAD_ID=T1              │                        │
   │                         │             AGY_POOL_SEGMENT_ID=T1-seg2         │                        │
   │                         │             AGY_POOL_ACCOUNT=backup             │                        │
   │                         ├─────────────────────────────────────────────────┼───────────────────────►│
   │                         │    seg1=handed_off, seg2=running, 释放锁         │                        │
   │                         │                                                 │   hud.py 首帧写 live/  │
   │                         │                         │◄──────────────────────┼─── T1-seg2.json ───────┤
   │                         │                         │                       │   (回传 conv_id+quota) │
```

- **Win32 PID 探活**：使用 `ctypes` 调用 `kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)` + `GetExitCodeProcess`，耗时 `< 1ms` 且不产生子进程或黑框窗口。

---

## 5. 交接包生成算法（`HandoffBuilder`）

### 5.1 三层结构与 Token 预算（默认硬上限 `3,500 tokens`）
1. **L1 任务现状（≤ 1,200 tokens，最高优先级）**：
   - `anchors/<tid>.md` 全文（永不裁剪）；
   - `brain/<conv_id>/task.md` 进度（复用 `hud.py` 同款复选框解析）；
   - `git status --porcelain` + `git diff --stat`（文件过多时保留前 25 个 + 剩余计数）。
2. **L2 对话精华（≤ 2,000 tokens，去噪）**：
   - 复用 `hud.py` 的 `_read_dedup_transcript_entries` 按 `step_index` 去重；
   - 抽取最近 N 轮（默认 6 轮）`USER_INPUT` 摘要 + `PLANNER_RESPONSE` 结论、改动文件列表、最新报错（每条 ≤ 300 字符）；
   - **全部丢弃**大段 `read_file` 原文、`run_command` 正常输出与思考草稿；
   - 超出预算时逐轮丢弃最老轮次，再压缩单轮长度，保底留最近 2 轮 + 改动文件表 + 最新报错。
3. **L3 溯源指针（≤ 300 tokens）**：
   - 上一棒 `segment_id`、`account_id`、`conversation_id`、`transcript_path`、接力链历史（如 `#1(main) -> #2(backup)`）。

### 5.2 Token 估算与出库脱敏
- **估算公式**：`ceil(cjk_chars / 1.5 + other_chars / 4.0)`（偏保守估算）。
- **正则脱敏**：替换 `AIza[0-9A-Za-z_-]{35}`、`sk-[A-Za-z0-9_-]{20,}`、`Bearer\s+\S+`、`x-antigravity-csrf-token` 等为 `[REDACTED]`。

### 5.3 `agy -i` 启动词模板
```text
你是任务线程 T1（改造 agy-hud 与双账号池）的第 2 棒接力助手。请立刻用 read_file 读取交接包 C:/Users/Administrator/.antigravity/agy-pool/handoffs/T1-seg1.md。读完后用 3~5 条简短要点向我确认：当前目标、已完成进度、下一步行动，然后继续推进。不要编造未在交接包或仓库中看到的事实。
```

---

## 6. `agy-hud` 最小侵入式改造方案

保持 `agy-hud/hud.py` 单文件独立可运行、绝不 `import agy_pool`，仅通过环境变量 + 文件协议联动。

### 6.1 配置新增（`agy-hud/config.json` & `hud.py` `DEFAULT_CONFIG` L141-L198）
```json
"pool": {
  "enabled": true,
  "showThreadBadge": true,
  "lowQuotaThresholdPercent": 10,
  "syncLiveTelemetry": true,
  "liveSyncIntervalSeconds": 5
}
```
（同步在 `agy-hud/install.ps1` L75-L85 的配置迁移脚本中添加 `setdefault`。）

### 6.2 `agy-hud/hud.py` 精确改动清单

| 位置 | 改动内容 | 降级与兼容 |
|---|---|---|
| 路径常量区 (L41-L56) | 新增 `POOL_DIR`（支持 `AGY_POOL_DIR`）、`POOL_STATE_FILE`、`POOL_LIVE_DIR` | 仅定义路径，不主动建目录 |
| `I18N` 字典 (L114-L139) | 新增 `pool.lowQuotaHint`（中文：`建议切换` / 英文：`switch to`） | 兼容中英双语 |
| 新增 `load_pool_context(config)` | 读取 `AGY_POOL_THREAD_ID` / `AGY_POOL_SEGMENT_ID` / `AGY_POOL_ACCOUNT`；存在时按 `mtime` 缓存只读 `state.json` | 无环境变量或 `state.json` 缺失/损坏时返回 `None`，0 开销 |
| 新增 `sync_pool_live_telemetry(data, stdin_pools, config, now)` | 在 `main()` 调用 `write_status_state(data)` (L2570, L2590) 旁调用；防抖写 `live/<seg_id>.json` | 仅写本段分片，写失败静默忽略 |
| `render_project_line()` (L1790-L1895) | 在 `[Model]` 后追加 `[T1·main·#2]` | 非托管会话输出 0 变化 |
| `render_usage_segment()` (L1991-L2066) | 活跃池 `5h` 剩余 `<= lowQuotaThresholdPercent` 时追加 `⚠ 建议切换 backup (94%)` | 仅触达阈值时显示 |

---

## 7. 分阶段开发计划与 `pytest` 测试矩阵

### 7.1 里程碑
- **M1**：`models.py`、`store.py`、`handoff.py` 及单测
- **M2**：`adapters.py`、`orchestrator.py`、`cli.py`、`agy-pool.cmd`、`install.ps1` 及单测
- **M3**：`agy-hud` 联动改造与 `test_hud.py` 回归扩展
- **M4**：本机双窗口真机端到端演练

### 7.2 测试矩阵与执行命令

| 测试文件 | 覆盖范围 |
|---|---|
| `agy-pool/tests/test_store.py` | 原子写、损坏恢复、僵尸 `state.lock` 回收、`live/*.json` 合并、Win32 PID 探活、零凭据键名守卫 |
| `agy-pool/tests/test_handoff.py` | `step_index` 去重、L1 完整保留 + `task.md` + `git diff --stat`、3500 Token 裁剪、空/损坏日志容错、正则脱敏 |
| `agy-pool/tests/test_orchestrator.py` | `start` 建锚点、并发拦截与 `--force`、两阶段取消回滚与重入、`A → B → A` 循环、同目录多任务隔离 |
| `agy-pool/tests/test_cli_e2e.py` | 子进程运行 `agy-pool` CLI 全命令（注入 `FakeAdapter` + `FakeLauncher`） |
| `agy-hud/test_hud.py` | 原有 13 项测试全过 + 新增无 env 零退化、徽章渲染、低额阈值告警、`live/<seg_id>.json` 防抖写入 |

```powershell
& "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe" -m pytest C:\Users\Administrator\Desktop\cspace\agy-pool\tests -v
& "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe" -m unittest discover -s C:\Users\Administrator\Desktop\cspace\agy-hud -p "test_*.py" -v
```
