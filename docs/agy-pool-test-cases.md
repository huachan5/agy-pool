# `agy-pool` × `agy-hud` 测试用例规范（Phase 1 MVP）

> **文档定位**：基于《`agy-pool` × `agy-hud` 双 Gemini 账号池与无缝接力 — 产品设计与技术方案（PRD）》编制的**确定性可执行测试用例集**。
> 每条用例严格遵循五要素规范（**标题、前置条件、具体测试数据、离散单步操作、每步唯一可观测预期结果**），不含模糊预期词，可直接用于人工验收、TestRail 导入（配套 [agy-pool-test-cases.csv](agy-pool-test-cases.csv)）及 Phase 1 TDD 自动化测试编码。

---

## 一、测试套件结构与统计概览

采用单仓库浅层分级（`一级模块 > 子特性 > 用例`，共 2 层目录深度），覆盖正向主流程、边界值、异常回滚、并发防撞、HUD 零退化与安全合规红线，共 **38 条**独立测试用例：

| 一级测试模块 | 子特性分组 | 用例编号范围 | 数量 | 关联核心能力 / ADR |
|---|---|---|---:|---|
| **1. 存储与并发底座 (`PoolStore`)** | 状态初始化与原子持久化 / 分片心跳合并与 PID 探活 | `TC-POOL-001` ~ `006` | 6 | C1, C5, ADR-001, AC-10 |
| **2. 任务线程与会话生命周期** | 创建线程与环境变量隔离 / CLI 状态与列表查询 | `TC-POOL-007` ~ `012` | 6 | C3, C7, ADR-003, US-01, AC-01 |
| **3. 交接包生成与裁剪 (`HandoffBuilder`)** | 结构化抽取与多棒保真 / Token 预算裁剪与估算 | `TC-POOL-013` ~ `018` | 6 | C2, US-03, AC-04, AC-05 |
| **4. 两阶段切号编排与适配器** | 三种 `AccountAdapter` / 中断回滚、恢复与幂等续接 | `TC-POOL-019` ~ `025` | 7 | C4, ADR-002, US-04, AC-04, AC-07 |
| **5. 多终端并发防撞与隔离** | 同目录隔离 / 同账号切号防撞与僵尸进程清理 | `TC-POOL-026` ~ `028` | 3 | C3, C5, US-05, AC-06 |
| **6. `agy-hud` 最小侵入联动** | 托管徽章、5s 防抖与低额预警 / 非托管与故障零退化 | `TC-POOL-029` ~ `035` | 7 | C6, US-02, AC-02, AC-03, AC-08 |
| **7. 安全合规、性能与架构红线** | 零凭据落盘与正则脱敏 / 端到端耗时与无黑框约束 | `TC-POOL-036` ~ `038` | 3 | AC-09, AC-10, 1.2 指标, 1.3 红线 |
| **合计** | **13 个子特性分组** | **`TC-POOL-001` ~ `038`** | **38** | **Phase 1 全量覆盖** |

---

## 二、需求双向追溯矩阵（RTM）与未覆盖缺口审查

### 2.1 验收标准（AC-01 ~ AC-10）覆盖映射

| 需求编号 | PRD 验收标准摘要 | 覆盖用例编号 | 覆盖详情 |
|---|---|---|---|
| **AC-01** | 创建线程与环境变量隔离（`thread_id`, `anchors/<tid>.md`, `AGY_POOL_*`） | `TC-POOL-007`, `008`, `009`, `026` | 正向创建、未注册槽位拒绝、空标题拦截、同目录双终端隔离 |
| **AC-02** | `agy-hud` 徽章 `[T1·main·#1]` 与 5s 防抖写入 `live/<seg_id>.json` | `TC-POOL-005`, `029`, `030` | 第 1 行徽章渲染、5s 内抑制重写与超期刷新、`PoolStore` 分片合并 |
| **AC-03** | 低额度视觉预警（阈值触发建议切换目标槽位及最近快照余量） | `TC-POOL-031`, `032` | 默认 `10%` 边界（`11%` / `10%` / `8%`）、自定义 `15%` 阈值、备用号无快照兜底 |
| **AC-04** | 完整切号接力（`≤ 3,500 Tokens` 交接包、旧段 `handed_off`、`agy -i` 拉起） | `TC-POOL-012`, `013`, `015`, `018`, `019`, `025`, `038` | L1/L2/L3 完整性、10 万 Token 裁剪、指针短指令、同号防空转、缺失上下文降级 |
| **AC-05** | 多棒回切不失真（`main → backup → main` 后 L1 完整保留初始 `anchors/<tid>.md`） | `TC-POOL-014` | 三棒循环回切后逐字比对初始 3 条硬约束与接力链历史 |
| **AC-06** | 多终端同账号并发拦截（单登录态存活终端 100% 拦截并列出冲突清单） | `TC-POOL-006`, `027`, `028` | Win32 PID 探活、同号存活进程 100% 阻断、崩溃死进程自动清理放行 |
| **AC-07** | 切号中断可回滚与幂等续接（`Ctrl+C` 或外部失败后状态无损、重跑安全续接） | `TC-POOL-022`, `023` | 外部命令非 0 回滚、`Ctrl+C` 释放锁与幂等续接不跳棒次序号 |
| **AC-08** | 无 `agy-pool` 场景零退化（原生启动或 `state.json` 缺失/损坏时零差异不崩溃） | `TC-POOL-033`, `034`, `035` | 原生会话零差异、`state.json` 缺失/截断乱码静默降级、`pool.enabled=false` 与零耦合 |
| **AC-09** | 零凭据与敏感信息脱敏（落盘文件零密钥，日志偶发密钥正则替换为 `[REDACTED]`） | `TC-POOL-001`, `020`, `036`, `037` | 4 类敏感密钥正则脱敏、`EnvVarAdapter` 不落盘明文、OS 凭据库与强杀红线静态扫描 |
| **AC-10** | 原子持久性与无黑框（临时文件 + `os.replace`，Windows 子进程 `CREATE_NO_WINDOW`） | `TC-POOL-003`, `006`, `021`, `038` | 写盘异常不损毁旧文件、清理 `.tmp`、Windows 子进程 `CREATE_NO_WINDOW` 标志位核验 |

### 2.2 用户故事（US-01 ~ US-05）、能力项（C1 ~ C9）及未覆盖缺口说明

| 维度 / 编号 | 关联用例 | 覆盖结论与缺口审查（Uncovered Requirements Report） |
|---|---|---|
| **US-01 ~ US-05 & 六大场景** | `TC-POOL-001` ~ `TC-POOL-035` | **0 未覆盖缺口**：5 个用户故事与 6 大核心用户场景均具备正向、边界及异常恢复用例。 |
| **C1 ~ C7（Phase 1 MVP）** | `TC-POOL-001` ~ `TC-POOL-038` | **0 未覆盖缺口**：6 个 Must 项（C1~C6）及 1 个 Should 精简项（C7 `status/list`）全覆盖。 |
| **PRD 第三部分待确认前提** | `TC-POOL-019`, `020`, `021`, `031`, `032` | **参数化全覆盖**：3 种切号适配器（选项 A/B/C）与低额度预警阈值（默认 `10%` 及可调 `15%`）均已建例，技术定稿后可直接按选定项勾选执行。 |
| **C7 (`doctor`) / C8 / C9** | **本期无（显式延期）** | **Phase 2 Deferred（非 Phase 1 缺口）**：`agy-pool doctor`、`ConversationService` 后台预热会话、3+ 账号轮转与配额重置倒计时在 PRD 第 1.3/3 节明确划归 Phase 2，不在 Phase 1 验收范围内。 |

---

## 三、详细测试用例列表

### 1. 存储与并发底座 (PoolStore) > 状态初始化与原子持久化

#### TC-POOL-001：存储初始化 — 首次执行 agy-pool init 创建标准目录树与 Schema v1 状态文件
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Functional` ｜ **需求追溯**：`C1, US-01, ADR-001`
- **自动化准入策略**：P0 优先自动化（`test_store.py` / `test_cli_e2e.py`）
- **前置条件**：测试临时目录 `POOL_HOME`（映射 `~/.antigravity/agy-pool/`）不存在；Python 3.12 环境可用。
- **测试数据**：`POOL_HOME = <temp_dir>/.antigravity/agy-pool`；默认双账号槽位 `main`、`backup`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 设置环境变量 `AGY_POOL_HOME=<temp_dir>/.antigravity/agy-pool` 并执行 `agy-pool init`。 | 命令退出码为 `0`，标准输出包含初始化完成提示及 `POOL_HOME` 绝对路径。 |
| 2 | 检查 `POOL_HOME` 下的目录与文件结构。 | `state.json` 文件存在，且 `anchors/`、`handoffs/`、`live/` 三个子目录均已创建。 |
| 3 | 解析 `state.json` 的 JSON 结构与键值。 | `schema_version` 等于 `1`，`accounts` 包含 `main` 与 `backup` 两个槽位对象，`threads` 与 `segments` 为空字典/列表，且全文不包含 `token`、`cookie`、`api_key` 字段。 |

#### TC-POOL-002：存储初始化 — 已存在线程数据时重复执行 agy-pool init 保持幂等不清空历史
- **优先级**：`P1 (High)` ｜ **测试类型**：`Boundary` ｜ **需求追溯**：`C1, US-01`
- **自动化准入策略**：P0 优先自动化（`test_store.py`）
- **前置条件**：`POOL_HOME/state.json` 已存在，且已记录线程 `T1`（标题为 `重构登录鉴权`）及锚点文件 `anchors/T1.md`。
- **测试数据**：既有线程 `thread_id = T1`，锚点文件内容 `# 原始目标：重构登录鉴权`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 在不带强制重置参数的情况下再次执行 `agy-pool init`。 | 命令退出码为 `0`，标准输出提示状态库已初始化并跳过覆盖。 |
| 2 | 读取 `state.json` 与 `anchors/T1.md`。 | `state.json` 中完整保留 `T1` 线程对象，`anchors/T1.md` 原文逐字节未发生变化。 |

#### TC-POOL-003：原子持久化 — 写入中途发生异常时原 state.json 完好无损且采用 os.replace 替换
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Negative / Reliability` ｜ **需求追溯**：`C1, AC-10, ADR-001`
- **自动化准入策略**：P0 优先自动化（`test_store.py`）
- **前置条件**：`POOL_HOME/state.json` 已存有合法状态（含线程 `T1`）；通过 Mock 拦截 `os.replace` 使其抛出 `OSError('Simulated disk failure')`。
- **测试数据**：待写入的新增线程 `T2`；模拟系统异常 `OSError`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 调用 `PoolStore.save()` 尝试写入包含 `T2` 的新状态。 | 方法向上抛出 `OSError`（或封装后的存储异常），不静默吞掉写盘错误。 |
| 2 | 使用 `json.loads()` 读取磁盘上的 `POOL_HOME/state.json`。 | JSON 解析成功且不抛 `JSONDecodeError`，内容仍为写入前的 `T1` 完整状态，未产生半截截断文件。 |
| 3 | 检查 `POOL_HOME` 目录下是否残留临时文件。 | 异常清理逻辑已删除临时文件，`POOL_HOME` 下 `.tmp` 文件数量为 `0`。 |

#### TC-POOL-004：文件互斥锁 — state.lock 被其他进程长期占用超时后安全报错且不强行覆写
- **优先级**：`P1 (High)` ｜ **测试类型**：`Concurrency / Negative` ｜ **需求追溯**：`C1, ADR-001`
- **自动化准入策略**：P0 优先自动化（`test_store.py`）
- **前置条件**：`POOL_HOME` 已初始化；测试辅助进程已独占持有 `POOL_HOME/state.lock`；锁获取超时配置为 `1.0` 秒。
- **测试数据**：`lock_timeout = 1.0s`；并发写命令 `agy-pool start "新任务" --account main`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 在锁被占用的状态下执行写操作（或运行 `agy-pool start`），记录返回耗时。 | 进程在 `1.0s ~ 2.5s` 内终止，退出码非 `0`，标准错误输出包含 `state.lock` 获取超时的明确提示。 |
| 2 | 释放锁句柄后读取 `state.json`。 | `state.json` 内容与加锁前完全一致，未写入未获锁的新线程数据。 |

---

### 1. 存储与并发底座 (PoolStore) > 分片心跳合并与 PID 探活

#### TC-POOL-005：分片心跳合并 — PoolStore 只读聚合 live/<seg_id>.json 更新会话 ID 与账号配额快照
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Functional` ｜ **需求追溯**：`C1, AC-02, ADR-001`
- **自动化准入策略**：P0 优先自动化（`test_store.py`）
- **前置条件**：`state.json` 中存在活跃段 `T1-seg1`（所属账号 `main`，初始 `conversation_id` 为空）；`live/T1-seg1.json` 已由 HUD 写入最新遥测分片。
- **测试数据**：`live/T1-seg1.json` 内容：`{"segment_id": "T1-seg1", "conversation_id": "conv-8899", "transcript_path": "C:/mock/transcript.jsonl", "quota": {"gemini-5h": {"remaining_fraction": 0.42}}, "updated_at": "2026-11-26T12:00:00Z"}`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 调用 `PoolStore.load_merged_view()`（或执行 `agy-pool status`）加载合并视图。 | 返回的 `T1-seg1` 视图中 `conversation_id` 等于 `"conv-8899"`，`transcript_path` 等于 `"C:/mock/transcript.jsonl"`。 |
| 2 | 检查合并视图中的 `main` 账号配额快照。 | `main` 槽位的 `gemini-5h` 最近剩余比例更新为 `42%`（`0.42`），读取全程零 `WinError 5` / `WinError 32` 冲突。 |

#### TC-POOL-006：Win32 PID 探活 — ctypes 调用 OpenProcess 准确识别存活进程与已终止 PID
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Functional` ｜ **需求追溯**：`C5, AC-06, AC-10`
- **自动化准入策略**：P0 优先自动化（`test_store.py`）
- **前置条件**：运行于 Windows 环境；当前测试进程 `os.getpid()` 处于运行态。
- **测试数据**：存活 PID：`os.getpid()`；已退出 PID：短生命周期子进程 `wait()` 结束后的 `dead_pid`；不存在 PID：`99999999`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 对当前进程 `os.getpid()` 调用 `is_pid_alive(pid)`。 | 返回布尔值 `True`，单次调用耗时 `≤ 50ms`，且未拉起 `tasklist` 或 `powershell` 外部进程。 |
| 2 | 对已退出的 `dead_pid` 调用 `is_pid_alive(dead_pid)`。 | 返回布尔值 `False`（通过 `GetExitCodeProcess != STILL_ACTIVE(259)` 准确识别已终止进程）。 |
| 3 | 对不存在的 `99999999` 调用 `is_pid_alive(99999999)`。 | 返回布尔值 `False`，不抛出未捕获的 `OSError` 或 `WinError`。 |

---

### 2. 任务线程与会话生命周期 > 创建线程与环境变量隔离

#### TC-POOL-007：创建新线程 — 执行 agy-pool start 生成锚点文件并注入双环境变量拉起 agy
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Functional (Smoke)` ｜ **需求追溯**：`C3, US-01, AC-01, 场景1`
- **自动化准入策略**：P0 优先自动化（`test_cli_e2e.py`）
- **前置条件**：`POOL_HOME` 已初始化双账号（`main`、`backup`）；工作目录为合法 Git 仓库 `C:/mock_repo`；使用 Mock `SessionLauncher` 捕获拉起参数与子进程环境变量。
- **测试数据**：命令：`agy-pool start "重构登录鉴权" --account main`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 在 `C:/mock_repo` 下执行 `agy-pool start "重构登录鉴权" --account main`。 | 命令退出码为 `0`，分配唯一线程 ID `T1` 与第 1 棒段 ID `T1-seg1`（序号 `#1`）。 |
| 2 | 检查 `POOL_HOME/anchors/T1.md` 文件是否存在及其内容。 | `anchors/T1.md` 已生成，正文包含任务原始目标 `重构登录鉴权`、工作目录 `C:/mock_repo` 与创建时间。 |
| 3 | 检查 `state.json` 中 `T1` 与 `T1-seg1` 的持久化字段。 | `T1.current_segment_id` 为 `"T1-seg1"`，`T1-seg1.account_id` 为 `"main"`，`T1-seg1.seq` 为 `1`，状态由 `starting` 进入 `running`。 |
| 4 | 检查 `SessionLauncher` 传给 `agy` 子进程的环境变量字典。 | 子进程环境变量精确包含 `AGY_POOL_THREAD_ID="T1"` 与 `AGY_POOL_SEGMENT_ID="T1-seg1"`。 |

#### TC-POOL-008：创建新线程 — 指定未注册的账号槽位启动线程被拒绝且不产生脏数据
- **优先级**：`P1 (High)` ｜ **测试类型**：`Negative` ｜ **需求追溯**：`C3, AC-01`
- **自动化准入策略**：P0 优先自动化（`test_cli_e2e.py`）
- **前置条件**：`POOL_HOME/state.json` 仅注册 `main` 与 `backup` 两个账号槽位，当前无任何线程。
- **测试数据**：非法命令：`agy-pool start "重构登录鉴权" --account ghost_slot`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 执行 `agy-pool start "重构登录鉴权" --account ghost_slot`。 | 命令退出码非 `0`，标准错误输出提示账号槽位 `ghost_slot` 未注册，并列出可用槽位 `main, backup`。 |
| 2 | 检查 `state.json` 与 `anchors/` 目录。 | `state.json` 中 `threads` 仍为空，`anchors/` 目录下文件数量为 `0`，未拉起 `agy` 进程。 |

#### TC-POOL-009：创建新线程 — 传入空白任务目标描述时被 CLI 参数校验拦截
- **优先级**：`P2 (Medium)` ｜ **测试类型**：`Boundary / Negative` ｜ **需求追溯**：`C3, US-01`
- **自动化准入策略**：P1 自动化（`test_cli_e2e.py`）
- **前置条件**：`POOL_HOME` 已初始化完成。
- **测试数据**：命令：`agy-pool start "   " --account main`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 执行 `agy-pool start "   " --account main`。 | 命令退出码非 `0`，标准错误输出提示任务目标标题不能为空白字符串。 |
| 2 | 读取 `state.json`。 | 未新增任何 `TaskThread` 或 `SessionSegment` 记录。 |

#### TC-POOL-012：指针型短指令拉起 — agy -i 仅传递单行文件路径短引导语而不拼接交接包正文
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Functional` ｜ **需求追溯**：`C3, AC-04, ADR-003`
- **自动化准入策略**：P0 优先自动化（`test_orchestrator.py`）
- **前置条件**：已生成体积为 `3,400 Tokens`（含多行代码块、单双引号及反斜杠）的 `handoffs/T1-seg1.md`；使用 Mock 捕获传给 `agy` 的命令行列表。
- **测试数据**：交接包路径：`C:/Users/Administrator/.antigravity/agy-pool/handoffs/T1-seg1.md`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 触发下一棒 `SessionLauncher.launch_relay()` 拉起新会话。 | 捕获到的命令行参数列表包含 `["-i", <prompt>]` 参数对。 |
| 2 | 校验 `<prompt>` 字符串的长度、换行符与内容。 | `<prompt>` 不含 `\n` 或 `\r` 换行符，总字符长度 `≤ 250`（规避 Windows 命令行 8,191 字符限制），包含 `T1-seg1.md` 路径与读取指令，不含交接包正文代码块。 |

---

### 2. 任务线程与会话生命周期 > CLI 状态与列表查询

#### TC-POOL-010：状态面板查询 — 执行 agy-pool status 与 agy-pool list 准确输出线程棒次及双槽位余量
- **优先级**：`P1 (High)` ｜ **测试类型**：`Functional` ｜ **需求追溯**：`C7, US-02`
- **自动化准入策略**：P1 自动化（`test_cli_e2e.py`）
- **前置条件**：已存在线程 `T1`（`main` 第 1 棒 `running`，快照余量 `8%`）与历史线程 `T2`（`backup` 第 2 棒 `closed`，`backup` 快照余量 `94%`）。
- **测试数据**：查询命令：`agy-pool status` 与 `agy-pool list`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 执行 `agy-pool status`。 | 退出码为 `0`，标准输出展示 `main`（`8%`）与 `backup`（`94%`）双槽位配额快照，以及当前活跃段 `T1`（`#1 · running`）。 |
| 2 | 执行 `agy-pool list`。 | 退出码为 `0`，标准输出完整列出 `T1` 与 `T2` 的线程 ID、标题、当前棒次、所属账号与状态。 |

#### TC-POOL-011：独立生成交接包 — 执行 agy-pool handoff T1 仅产出交接 Markdown 而不触发账号切换
- **优先级**：`P1 (High)` ｜ **测试类型**：`Functional` ｜ **需求追溯**：`C2, C7, US-03`
- **自动化准入策略**：P1 自动化（`test_cli_e2e.py`）
- **前置条件**：线程 `T1` 处于第 1 棒 `T1-seg1`（账号 `main`，状态 `closed`），对应 `transcript.jsonl` 已有 5 轮对话记录。
- **测试数据**：命令：`agy-pool handoff T1`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 执行 `agy-pool handoff T1`。 | 退出码为 `0`，标准输出打印生成的交接包路径 `POOL_HOME/handoffs/T1-seg1.md`。 |
| 2 | 检查 `handoffs/T1-seg1.md` 与 `state.json`。 | `handoffs/T1-seg1.md` 文件非空且含 L1/L2/L3 三个章节标题；`state.json` 中当前段仍为 `T1-seg1`（`main`），未生成 `T1-seg2` 且未调用 `AccountAdapter`。 |

---

### 3. 交接包生成与裁剪 (HandoffBuilder) > 结构化抽取与多棒保真

#### TC-POOL-013：交接包三层结构 — 从真实格式日志与 Git 仓库生成完整 L1/L2/L3 交接包
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Functional (Smoke)` ｜ **需求追溯**：`C2, US-03, AC-04, 1.2 一致性指标`
- **自动化准入策略**：P0 优先自动化（`test_handoff.py`）
- **前置条件**：Git 仓库存在未提交改动（修改 `auth.py` 新增 20 行）、存在 `task.md`（2 项已完成、1 项进行中）、`anchors/T1.md`（含约束“禁止引入第三方依赖”）、`transcript.jsonl`（含 4 轮对话及最近一次 `AssertionError` 报错）。
- **测试数据**：`thread_id=T1, segment_id=T1-seg1, account_id=main, conversation_id=conv-001`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 调用 `HandoffBuilder.build()` 生成 `handoffs/T1-seg1.md` 并检查 L1 区域。 | L1 区域 100% 原文包含 `anchors/T1.md`（含“禁止引入第三方依赖”）、`task.md` 进度项，以及 `git status` / `git diff --stat` 中的 `auth.py` 变更统计。 |
| 2 | 检查生成文件中的 L2 区域。 | L2 区域包含最近用户指令与模型结论摘要、改动文件清单（`auth.py`）及最新报错 `AssertionError`，且已过滤工具长输出与模型思考草稿。 |
| 3 | 检查生成文件中的 L3 区域。 | L3 区域准确包含 `segment_id: T1-seg1`、`account_id: main`、`conversation_id: conv-001`、`transcript_path` 及接力链历史 `#1 main`。 |

#### TC-POOL-014：多棒回切保真 — 经历 main→backup→main 三棒接力后初始 anchor.md 零失真保留
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Functional` ｜ **需求追溯**：`C2, US-01, AC-05, 场景6`
- **自动化准入策略**：P0 优先自动化（`test_handoff.py` / `test_orchestrator.py`）
- **前置条件**：线程 `T1` 初始创建 `anchors/T1.md`（含 3 条硬性红线约束）；已完成 `#1 (main)` → `#2 (backup)` 接力，现处于 `#2 (backup)` 末尾准备回切 `main`。
- **测试数据**：初始锚点原文：`1. 纯标准库实现；2. 零凭据落盘；3. 兼容 Windows Python 3.12`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 执行 `agy-pool switch T1 --to main` 完成第 2 次切号，生成 `handoffs/T1-seg2.md` 并进入第 3 棒。 | 新段 `T1-seg3` 创建成功，`seq` 等于 `3`，`account_id` 等于 `"main"`。 |
| 2 | 比对 `handoffs/T1-seg2.md` 的 L1 锚点段落与初始 `anchors/T1.md` 原文。 | 3 条硬性红线约束在 `T1-seg2.md` 的 L1 区域逐字完全一致，未因第 2 棒摘要覆盖而丢失或改写。 |
| 3 | 检查 `handoffs/T1-seg2.md` 的 L3 接力链记录。 | 接力链清晰展示 `#1(main) -> #2(backup)` 历史轨迹，L2 区域更新为 `seg2` 的最新对话结论。 |

#### TC-POOL-016：日志去重抽取 — 同一 step_index 多条流式事件仅保留最终态记录
- **优先级**：`P1 (High)` ｜ **测试类型**：`Functional` ｜ **需求追溯**：`C2, US-03`
- **自动化准入策略**：P0 优先自动化（`test_handoff.py`）
- **前置条件**：`transcript.jsonl` 中包含 `step_index=5` 的 4 条连续 JSON 行（前 3 条 `status="IN_PROGRESS"` 草稿片段，最后 1 条 `status="DONE"` 最终回复 `FINAL_DECISION_V4`）。
- **测试数据**：重复 `step_index = 5` 的 4 条日志事件。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 调用 `HandoffBuilder` 解析该 `transcript.jsonl` 生成 L2 摘要。 | `FINAL_DECISION_V4` 在 L2 摘要中仅出现 `1` 次，前 3 条 `IN_PROGRESS` 中间重复片段未混入交接包。 |

#### TC-POOL-018：缺失上下文降级 — 非 Git 目录且无 task.md 与 transcript.jsonl 时仍可安全生成交接包
- **优先级**：`P1 (High)` ｜ **测试类型**：`Negative / Edge Case` ｜ **需求追溯**：`C2, AC-04`
- **自动化准入策略**：P0 优先自动化（`test_handoff.py`）
- **前置条件**：工作目录为普通空文件夹（无 `.git` 目录、无 `task.md`），且会话刚启动尚未落盘 `transcript.jsonl`，仅存在 `anchors/T1.md`。
- **测试数据**：普通非 Git 目录 `C:/temp/plain_folder`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 调用 `HandoffBuilder.build()` 为 `T1-seg1` 生成交接包。 | 方法正常返回交接包文件路径，不抛出 `FileNotFoundError` 或 `subprocess.CalledProcessError`。 |
| 2 | 检查生成的 `handoffs/T1-seg1.md` 内容。 | L1 完整包含 `anchors/T1.md` 原文，Git 与 `task.md` 字段优雅降级标注为 `非 Git 仓库 / 无记录`，L2 标注为 `暂无历史对话记录`。 |

---

### 3. 交接包生成与裁剪 (HandoffBuilder) > Token 预算裁剪与估算

#### TC-POOL-015：超大日志裁剪 — 输入 100,000 Tokens 对话日志时自动按轮次裁剪至 ≤ 3,500 Tokens
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Boundary / Performance` ｜ **需求追溯**：`C2, US-03, AC-04, 1.2 体积控制指标`
- **自动化准入策略**：P0 优先自动化（`test_handoff.py`）
- **前置条件**：构造包含 120 轮对话、累计约 `100,000 Tokens` 的 `transcript.jsonl`（第 1 轮标记 `OLDEST_TURN_001`，第 120 轮标记 `LATEST_TURN_120`）；`anchors/T1.md` 为 `300 Tokens`。
- **测试数据**：总预算硬上限 `3,500 Tokens`（L1 ≤ 1,200，L2 ≤ 2,000，L3 ≤ 300）。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 运行 `HandoffBuilder.build()` 生成交接包并调用 `estimate_tokens()` 计算全文 Token 数。 | 生成的 `handoff.md` 全文估算 Token 数 `≤ 3,500`，其中 L2 部分 `≤ 2,000` Tokens。 |
| 2 | 检查生成文件中的轮次保留顺序与 L1 完整性。 | 全文包含最新轮次标记 `LATEST_TURN_120`，最老轮次标记 `OLDEST_TURN_001` 已被裁剪移除，且 `anchors/T1.md` 原文 100% 完整保留。 |

#### TC-POOL-017：Token 估算公式 — 中英混合字符严格按 ceil(cjk_chars/1.5 + other_chars/4.0) 计算
- **优先级**：`P1 (High)` ｜ **测试类型**：`Boundary` ｜ **需求追溯**：`C2, AC-04`
- **自动化准入策略**：P0 优先自动化（`test_handoff.py`）
- **前置条件**：`HandoffBuilder` 的 `estimate_tokens(text)` 纯函数可独立调用。
- **测试数据**：样本 A：`""`；样本 B：`3` 个汉字 `"测试池"` + `4` 个 ASCII `"pool"`；样本 C：`4` 个汉字 `"双账号池"` + `1` 个 ASCII `"A"`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 对样本 A `""` 调用 `estimate_tokens`。 | 返回整数 `0`。 |
| 2 | 对样本 B `"测试池pool"`（3 CJK + 4 Other）调用 `estimate_tokens`。 | `ceil(3 / 1.5 + 4 / 4.0) = ceil(2.0 + 1.0)`，精确返回整数 `3`。 |
| 3 | 对样本 C `"双账号池A"`（4 CJK + 1 Other）调用 `estimate_tokens`。 | `ceil(4 / 1.5 + 1 / 4.0) = ceil(2.6667 + 0.25) = ceil(2.9167)`，精确返回整数 `3`。 |

---

### 4. 两阶段切号编排与适配器 > 切号接力与三种 AccountAdapter

#### TC-POOL-019：标准切号接力 (ManualGuidedAdapter) — 完成引导确认后旧段置为 handed_off 并拉起第 2 棒
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Functional (Smoke)` ｜ **需求追溯**：`C4, US-04, AC-04, ADR-002, 场景4`
- **自动化准入策略**：P0 优先自动化（`test_orchestrator.py`）+ 发版前真实浏览器手工走查 1 次
- **前置条件**：线程 `T1` 的 `T1-seg1`（`main`）已退出（状态 `closed`），无其他并发同账号存活进程；配置使用 `ManualGuidedAdapter`（模拟输入 `y` 确认已完成 `/logout` 与 `backup` OAuth 登录）。
- **测试数据**：命令：`agy-pool switch T1 --to backup`；确认输入：`y\n`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 启动 `agy-pool switch T1 --to backup`，在人工确认阻塞点检查 `state.json` 与 `handoffs/`。 | `T1-seg1.status` 已先迁移为 `"handoff_pending"`，且 `handoffs/T1-seg1.md` 已落盘生成。 |
| 2 | 向标准输入传入确认指令 `y` 完成 `ManualGuidedAdapter` 切号确认。 | 编排器将 `T1-seg1.status` 更新为 `"handed_off"`，新增 `T1-seg2`（`account_id="backup"`, `seq=2`, `status="running"`），并更新 `T1.current_segment_id="T1-seg2"`。 |
| 3 | 检查 `SessionLauncher` 拉起参数与子进程环境变量。 | 已通过 `agy -i` 携带指向 `handoffs/T1-seg1.md` 的短引导语拉起第 2 棒，子进程注入 `AGY_POOL_THREAD_ID="T1"` 与 `AGY_POOL_SEGMENT_ID="T1-seg2"`。 |

#### TC-POOL-020：环境变量隔离切号 (EnvVarAdapter) — 按槽位映射传递目标环境变量且 state.json 不落盘密钥值
- **优先级**：`P1 (High)` ｜ **测试类型**：`Functional / Security` ｜ **需求追溯**：`C4, AC-09, ADR-002, 待确认前提1`
- **自动化准入策略**：P0 优先自动化（`test_orchestrator.py`）
- **前置条件**：配置适配器为 `EnvVarAdapter`，槽位 `backup` 配置引用宿主环境变量名 `GEMINI_KEY_BACKUP`；当前宿主环境中 `GEMINI_KEY_BACKUP="AIzaSyDummyTestKey123456"`。
- **测试数据**：槽位配置：`{"adapter": "env_var", "env_key_ref": "GEMINI_KEY_BACKUP"}`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 执行 `agy-pool switch T1 --to backup`。 | 无需人工阻塞确认，自动完成两阶段切换并拉起 `T1-seg2`。 |
| 2 | 检查传给新 `agy` 子进程的环境变量字典与磁盘上的 `state.json`。 | 新子进程环境中 `GEMINI_API_KEY` 已注入宿主 `GEMINI_KEY_BACKUP` 的运行期值；而在 `state.json` 与 `handoffs/T1-seg1.md` 中搜索 `AIzaSyDummyTestKey123456` 命中数为 `0`。 |

#### TC-POOL-021：外部命令切号 (ExternalCommandAdapter) — 外部合法命令退出码为 0 时无黑框完成切号
- **优先级**：`P1 (High)` ｜ **测试类型**：`Functional` ｜ **需求追溯**：`C4, AC-10, ADR-002, 待确认前提1`
- **自动化准入策略**：P0 优先自动化（`test_orchestrator.py`）
- **前置条件**：配置适配器为 `ExternalCommandAdapter`，切号命令配置为返回退出码 `0` 的可执行脚本；Spy 监控 `subprocess.run` 的 `creationflags`。
- **测试数据**：目标槽位：`backup`；预期 Windows 标志位：`subprocess.CREATE_NO_WINDOW`（`0x08000000`）。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 执行 `agy-pool switch T1 --to backup`。 | 命令退出码为 `0`，`T1-seg1` 置为 `handed_off`，`T1-seg2` 成功拉起。 |
| 2 | 检查外部命令调用时的 `subprocess.run` 参数。 | 在 Windows 平台上 `creationflags` 包含 `CREATE_NO_WINDOW` 标志位。 |

---

### 4. 两阶段切号编排与适配器 > 中断回滚、异常恢复与幂等续接

#### TC-POOL-022：外部命令切号失败回滚 — 外部命令返回非 0 退出码时回滚段状态且不拉起新会话
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Negative / Recovery` ｜ **需求追溯**：`C4, US-04, AC-07, ADR-002`
- **自动化准入策略**：P0 优先自动化（`test_orchestrator.py`）
- **前置条件**：线程 `T1` 的 `T1-seg1` 处于 `closed` 状态；`ExternalCommandAdapter` 配置命令返回退出码 `2` 并向 stderr 输出 `Auth switch failed`。
- **测试数据**：外部命令退出码 `2`，错误信息 `Auth switch failed`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 执行 `agy-pool switch T1 --to backup`。 | 命令返回非 `0` 退出码，输出失败原因 `Auth switch failed`，且未调用 `SessionLauncher` 拉起新会话。 |
| 2 | 读取 `state.json` 检查回滚结果。 | `T1-seg1.status` 已从 `handoff_pending` 安全回滚为 `"closed"`，`T1.current_segment_id` 仍为 `"T1-seg1"`，未残留脏 `T1-seg2` 记录，且 `handoffs/T1-seg1.md` 保留供重试。 |

#### TC-POOL-023：用户 Ctrl+C 中断与幂等续接 — handoff_pending 期间取消后再次运行 switch 不跳棒不丢状态
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Recovery / Idempotency` ｜ **需求追溯**：`C4, US-04, AC-07`
- **自动化准入策略**：P0 优先自动化（`test_orchestrator.py`）
- **前置条件**：线程 `T1`（`T1-seg1`, `#1`, `main`）在 `ManualGuidedAdapter` 等待用户确认阶段触发 `KeyboardInterrupt`（模拟按 `Ctrl+C`）。
- **测试数据**：第 1 次执行触发 `KeyboardInterrupt`；第 2 次执行正常输入 `y` 确认。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 执行 `agy-pool switch T1 --to backup` 并在等待确认处触发 `KeyboardInterrupt`。 | 程序捕获中断优雅退出，`POOL_HOME/state.lock` 已释放，`state.json` 可正常 JSON 解析，`handoffs/T1-seg1.md` 完好保留。 |
| 2 | 再次执行相同的 `agy-pool switch T1 --to backup` 并输入 `y` 完成确认。 | 幂等续接成功：`T1-seg1` 变为 `handed_off`，新创建的下一棒为 `T1-seg2`（棒次序号严格为 `#2`，未跳号成 `#3`）。 |

#### TC-POOL-024：同账号中断恢复 — 执行 agy-pool resume T1 在不换号前提下重新拉起当前活跃线程
- **优先级**：`P1 (High)` ｜ **测试类型**：`Functional` ｜ **需求追溯**：`C3, C4, US-04`
- **自动化准入策略**：P1 自动化（`test_cli_e2e.py`）
- **前置条件**：线程 `T1` 当前处于 `backup` 账号第 2 棒 `T1-seg2`，因终端意外关闭处于 `closed` 状态，配额充足无需换号。
- **测试数据**：命令：`agy-pool resume T1`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 执行 `agy-pool resume T1`。 | 命令退出码为 `0`，不触发 `AccountAdapter` 切号，自动生成或复用最新上下文引导并拉起 `agy` 会话，子进程环境变量保持绑定线程 `T1`。 |

#### TC-POOL-025：无效目标槽位拦截 — 尝试向当前已在使用的同一账号槽位执行 switch 时拒绝空转
- **优先级**：`P2 (Medium)` ｜ **测试类型**：`Negative` ｜ **需求追溯**：`C4, AC-04`
- **自动化准入策略**：P1 自动化（`test_orchestrator.py`）
- **前置条件**：线程 `T1` 当前段 `T1-seg1` 所属账号已经是 `main`。
- **测试数据**：命令：`agy-pool switch T1 --to main`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 执行 `agy-pool switch T1 --to main`。 | 命令返回非 `0` 退出码，提示线程 `T1` 当前已处于 `main` 账号槽位，无需切换至相同账号；`T1-seg1` 状态与棒次序号保持不变。 |

---

### 5. 多终端并发防撞与隔离 > 同目录隔离与同账号切号拦截

#### TC-POOL-026：同目录多窗口并行 — 同一工作目录下并发运行 T1 与 T2 拥有独立环境变量与分片心跳
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Concurrency` ｜ **需求追溯**：`C3, C5, US-05, AC-01, 场景5`
- **自动化准入策略**：P0 优先自动化（`test_orchestrator.py` / `test_cli_e2e.py`）
- **前置条件**：终端 A 与终端 B 的当前工作目录均为 `C:/Users/Administrator/Desktop/cspace`。
- **测试数据**：终端 A：`agy-pool start "重构登录鉴权" --account main`；终端 B：`agy-pool start "修复报表导出" --account main`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 先后在终端 A 和终端 B 启动两个任务。 | 分别生成独立线程 `T1`（`T1-seg1`）与 `T2`（`T2-seg1`），各自生成独立的 `anchors/T1.md` 与 `anchors/T2.md`。 |
| 2 | 分别在终端 A 与终端 B 的环境变量上下文下触发 `agy-hud` 渲染与遥测同步。 | 终端 A 仅渲染 `[T1·main·#1]` 并写入 `live/T1-seg1.json`；终端 B 仅渲染 `[T2·main·#1]` 并写入 `live/T2-seg1.json`，两份上下文零交叉覆盖。 |

#### TC-POOL-027：同账号存活终端拦截 — 单机单登录态下其他窗口仍在运行旧账号时 100% 阻断 switch
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Concurrency / Safety` ｜ **需求追溯**：`C5, US-05, AC-06, 场景5, 1.2 防撞率指标`
- **自动化准入策略**：P0 优先自动化（`test_orchestrator.py`）
- **前置条件**：适配器为单机单登录态（`ManualGuidedAdapter`）；线程 `T1`（`main`）已退出（`closed`），而线程 `T2`（`T2-seg1`，账号 `main`，绑定当前存活子进程 `alive_pid`）状态为 `running`。
- **测试数据**：冲突进程：`thread_id=T2, segment_id=T2-seg1, account=main, pid=<alive_pid>, cwd=C:/mock_repo`；命令：`agy-pool switch T1 --to backup`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 在终端 1 执行 `agy-pool switch T1 --to backup`。 | 命令立即拒绝切号并返回非 `0` 退出码，不调用 `AccountAdapter`。 |
| 2 | 检查标准错误输出的冲突报告内容与 `state.json`。 | 错误输出明确列出冲突线程 `T2`（`T2-seg1`）、存活 `PID` 及工作目录；`T1` 与 `T2` 在 `state.json` 中的状态均保持原样未受损。 |

#### TC-POOL-028：死进程自动收割放行 — 其他同账号段虽标记 running 但 PID 已终止时自动归档并放行切号
- **优先级**：`P1 (High)` ｜ **测试类型**：`Recovery / Concurrency` ｜ **需求追溯**：`C5, AC-06`
- **自动化准入策略**：P0 优先自动化（`test_orchestrator.py`）
- **前置条件**：线程 `T2`（`T2-seg1`，账号 `main`）在 `state.json` 中遗留状态为 `running`，但其记录的 `pid` 对应进程已崩溃终止（`is_pid_alive` 返回 `False`）；线程 `T1` 请求从 `main` 切往 `backup`。
- **测试数据**：`T2-seg1.pid = <terminated_pid>`，`T2-seg1.status = "running"`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 执行 `agy-pool switch T1 --to backup`（配合 `FakeAccountAdapter`）。 | 并发检测识别到 `<terminated_pid>` 已死亡，不误报冲突拦截，顺利完成 `T1` 向 `backup` 的第 2 棒切换。 |
| 2 | 读取更新后的 `state.json` 检查 `T2-seg1` 状态。 | `T2-seg1.status` 已被自动清理修正为 `"closed"`，消除僵尸锁。 |

---

### 6. agy-hud 最小侵入联动 > 托管徽章、防抖同步与低额度预警

#### TC-POOL-029：托管会话徽章渲染 — agy-hud 第 1 行项目状态栏准确追加 [T1·main·#1] 徽章
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Functional (Smoke)` ｜ **需求追溯**：`C6, US-02, AC-02, 场景1, 场景2`
- **自动化准入策略**：P0 优先自动化（`agy-hud/test_hud.py`）
- **前置条件**：`POOL_HOME/state.json` 含 `T1`（`T1-seg1`, `account=main`, `seq=1`）；环境变量设置 `AGY_POOL_THREAD_ID=T1`、`AGY_POOL_SEGMENT_ID=T1-seg1`；`config.json` 中 `pool.enabled=true`、`pool.showThreadBadge=true`。
- **测试数据**：标准 HUD `stdin` JSON payload（`Gemini 4 Neon (High)`，`gemini-5h.remaining_fraction=0.85`）。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 调用 `hud.render_hud(payload, config)` 并剥离 ANSI 颜色码检查输出行。 | 输出保持标准 4 行布局，第 1 行精确包含字符串 `[T1·main·#1]`，第 2 行因配额 `85% > 10%` 不显示低额度切换告警。 |

#### TC-POOL-030：实时遥测 5 秒防抖写入 — agy-hud 在 5s 窗口内抑制重复写盘且超期后原子刷新 live 分片
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Functional / Performance` ｜ **需求追溯**：`C6, AC-02, ADR-001, 场景2`
- **自动化准入策略**：P0 优先自动化（`agy-hud/test_hud.py`）
- **前置条件**：托管环境变量指向 `T1-seg1`；配置 `pool.syncLiveTelemetry=true`、`pool.liveSyncIntervalSeconds=5`。
- **测试数据**：`t0 = 1000.0s`（配额 `0.80`），`t1 = 1002.0s`（配额 `0.79`，间隔 2s < 5s），`t2 = 1006.0s`（配额 `0.75`，间隔 6s ≥ 5s）。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 在 `now=1000.0` 调用 `render_hud()` 并读取 `live/T1-seg1.json`。 | `live/T1-seg1.json` 首次创建成功，`remaining_fraction` 为 `0.80`。 |
| 2 | 在 `now=1002.0` 传入新配额 `0.79` 调用 `render_hud()` 并读取 `live/T1-seg1.json`。 | 受 5 秒防抖保护未执行磁盘重写，`live/T1-seg1.json` 内 `remaining_fraction` 仍保持 `0.80`。 |
| 3 | 在 `now=1006.0` 传入新配额 `0.75` 调用 `render_hud()` 并读取 `live/T1-seg1.json`。 | 防抖窗口已满，`live/T1-seg1.json` 原子刷新成功，`remaining_fraction` 更新为 `0.75`。 |

#### TC-POOL-031：低额度视觉预警边界 — 默认 10% 阈值在 10% 与 8% 触发提醒而在 11% 不触发
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Boundary` ｜ **需求追溯**：`C6, US-02, AC-03, 场景3`
- **自动化准入策略**：P0 优先自动化（`agy-hud/test_hud.py`）
- **前置条件**：当前托管段账号为 `main`；`state.json`（或 `live/`）记录备用账号 `backup` 上次快照 `gemini-5h` 余量为 `94%`（`0.94`）；`lowQuotaThresholdPercent` 为默认值 `10`。
- **测试数据**：三组 `stdin.quota["gemini-5h"]["remaining_fraction"]`：`0.11`（11%）、`0.10`（10%）、`0.08`（8%）。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 以 `remaining_fraction = 0.11`（高于阈值 1%）渲染 HUD 第 2 行。 | 第 2 行显示配额百分比 `11%`，不包含 `建议切换 backup` 预警文本。 |
| 2 | 以 `remaining_fraction = 0.10`（等于阈值边界）渲染 HUD 第 2 行。 | 第 2 行配额尾部包含预警提示：`⚠ 建议切换 backup (上次快照 94%)`。 |
| 3 | 以 `remaining_fraction = 0.08`（低于阈值）渲染 HUD 第 2 行。 | 第 2 行配额尾部包含预警提示：`⚠ 建议切换 backup (上次快照 94%)`。 |

#### TC-POOL-032：自定义预警阈值与无快照兜底 — 配置 15% 阈值生效且备用账号无历史快照时不抛异常
- **优先级**：`P1 (High)` ｜ **测试类型**：`Boundary / Edge Case` ｜ **需求追溯**：`C6, AC-03, 待确认前提2`
- **自动化准入策略**：P0 优先自动化（`agy-hud/test_hud.py`）
- **前置条件**：`config.json` 配置 `pool.lowQuotaThresholdPercent = 15`；`backup` 账号槽位刚初始化，尚无任何历史配额快照（`last_quota = null`）。
- **测试数据**：`remaining_fraction = 0.14`（14% ≤ 15%）；`backup` 快照为 `None`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 以 `remaining_fraction = 0.14` 调用 `render_hud()`。 | 渲染不抛 `TypeError` 或 `KeyError`，第 2 行触发预警并显示无快照兜底文案（如 `⚠ 建议切换 backup (暂无快照)`）。 |

---

### 6. agy-hud 最小侵入联动 > 非托管会话与故障零退化

#### TC-POOL-033：非托管原生会话零退化 — 未注入 AGY_POOL_* 环境变量时 HUD 外观与磁盘零变化
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Regression` ｜ **需求追溯**：`C6, AC-08, 场景2`
- **自动化准入策略**：P0 优先自动化（`agy-hud/test_hud.py`）
- **前置条件**：当前进程环境变量不含 `AGY_POOL_THREAD_ID` 与 `AGY_POOL_SEGMENT_ID`；即使 `POOL_HOME/state.json` 存在活跃线程且 `gemini-5h` 仅为 `0.05`（5%）。
- **测试数据**：原生 `agy` 会话 `stdin` JSON（`gemini-5h.remaining_fraction = 0.05`）。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 调用 `render_hud()` 渲染状态栏并与未启用 `pool` 功能的基线输出逐字符比对。 | 输出字符串与改造前基线 100% 相同：第 1 行无 `[T1·...]` 徽章，第 2 行无 `建议切换` 提示，且 `POOL_HOME/live/` 目录无新增文件。 |

#### TC-POOL-034：状态文件缺失或损坏容错 — state.json 被删或写入截断乱码时 agy-hud 静默降级不报错
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Negative / Reliability` ｜ **需求追溯**：`C6, AC-08`
- **自动化准入策略**：P0 优先自动化（`agy-hud/test_hud.py`）
- **前置条件**：环境变量已设置 `AGY_POOL_THREAD_ID=T1`、`AGY_POOL_SEGMENT_ID=T1-seg1`。
- **测试数据**：异常态 A：`POOL_HOME/state.json` 不存在；异常态 B：`POOL_HOME/state.json` 内容为截断文本 `{"schema_version": 1, "threads":`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 在异常态 A（文件缺失）下运行 `hud.py` 传入标准 JSON payload。 | 进程退出码为 `0`，`stderr` 为空字符串，`stdout` 输出基础 4 行 HUD 信息。 |
| 2 | 在异常态 B（JSON 损坏）下运行 `hud.py` 传入标准 JSON payload。 | 进程退出码为 `0`，`stderr` 为空字符串（无未捕获 Traceback），`stdout` 退化输出基础 4 行 HUD 信息。 |

#### TC-POOL-035：配置开关与包间零耦合 — pool.enabled=false 彻底关闭联动且 hud.py 绝不导入 agy_pool
- **优先级**：`P1 (High)` ｜ **测试类型**：`Functional / Architecture` ｜ **需求追溯**：`C6, AC-08, Part 2.2/2.5`
- **自动化准入策略**：P0 优先自动化（`agy-hud/test_hud.py`）
- **前置条件**：环境变量含 `AGY_POOL_THREAD_ID=T1`；`config.json` 显式配置 `pool.enabled = false`。
- **测试数据**：`pool = {"enabled": false, "showThreadBadge": true, "syncLiveTelemetry": true}`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 使用上述配置调用 `render_hud()`（配额设为 `0.05`）。 | 输出不包含 `[T1·main·#1]` 徽章，不包含低额度切换提醒，且不写入 `live/T1-seg1.json`。 |
| 2 | 使用 Python `ast` 模块解析 `agy-hud/hud.py` 的全部 `Import` 与 `ImportFrom` 节点。 | 导入模块集合中 `agy_pool` 出现次数为 `0`（仅依赖 Python 3.12 标准库）。 |

---

### 7. 安全合规、性能与架构红线 > 零凭据落盘与敏感密钥脱敏

#### TC-POOL-036：敏感凭据正则脱敏 — 日志偶发 Google API Key / OAuth Token / Bearer / Cookie 时强制替换为 [REDACTED]
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Security` ｜ **需求追溯**：`AC-09, C2, 1.2 零凭据合规`
- **自动化准入策略**：P0 优先自动化（`test_handoff.py`）
- **前置条件**：构造包含多种真实格式测试假密钥的 `transcript.jsonl` 与报错栈文本。
- **测试数据**：注入 4 类测试凭据串：① `AIzaSyB9x8W7v6U5t4R3e2W1q0P9o8I7u6Y5t4R`；② `Authorization: Bearer ya29.a0AfH6SMBx_test_oauth_token_998877`；③ `Cookie: SID=test_sid_value; __Secure-1PSID=test_secure_psid`；④ `GEMINI_API_KEY=sk-proj-testsecret987654321`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 运行 `HandoffBuilder.build()` 生成 `handoffs/T1-seg1.md`。 | 交接包文件生成在 `POOL_HOME/handoffs/T1-seg1.md`。 |
| 2 | 在生成的 `handoffs/T1-seg1.md` 全文中检索上述 4 组原始密钥子串。 | 4 组原始密钥子串的匹配命中数均为 `0`，对应位置均已替换为字面量 `[REDACTED]`。 |

#### TC-POOL-037：架构越界红线扫描 — 严禁触碰 OS 凭据库/浏览器 Cookie 且严禁 agy-hud 强杀或接管终端进程
- **优先级**：`P0 (Critical)` ｜ **测试类型**：`Security / Compliance` ｜ **需求追溯**：`AC-09, 1.3 Out of Scope`
- **自动化准入策略**：P0 优先自动化（静态合规扫描测试）
- **前置条件**：`agy-pool` 与 `agy-hud` 代码库及运行一轮完整 `start -> hud -> switch` 后的 `POOL_HOME` 目录。
- **测试数据**：禁用 API/命令黑名单：`CredRead`、`CredWrite`、`CryptUnprotectData`、`cmdkey`、`Login Data`、`Cookies`（浏览器 SQLite），以及 `hud.py` 中的 `TerminateProcess`、`os.kill`、`taskkill`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 对 `agy-pool` 与 `agy-hud` 源码执行 AST 与关键字静态合规扫描。 | 凭据库读取 API 在两套代码库中命中数为 `0`；进程终止与 TTY 注入 API 在 `agy-hud/hud.py` 中命中数为 `0`。 |
| 2 | 递归扫描运行后 `POOL_HOME`（`state.json`、`live/*.json`、`anchors/*.md`、`handoffs/*.md`）全部文件内容。 | 不含任何 OAuth Token、Refresh Token、Cookie 或明文 API Key，满足零凭据落盘指标。 |

---

### 7. 安全合规、性能与架构红线 > 切号性能耗时与无黑框约束

#### TC-POOL-038：切号端到端性能与无黑框约束 — 500 轮日志场景下 switch 编排耗时 ≤ 15 秒且子进程全带 CREATE_NO_WINDOW
- **优先级**：`P1 (High)` ｜ **测试类型**：`Performance / Non-Functional` ｜ **需求追溯**：`1.2 性能指标, AC-04, AC-10`
- **自动化准入策略**：P1 自动化 + 发版前人工目视终验（`test_cli_e2e.py`）
- **前置条件**：测试 Git 仓库包含 15 个修改文件、`transcript.jsonl` 含 500 轮真实结构对话记录（约 8 MB）；使用免人工等待的 `FakeAccountAdapter` 测量纯工具编排耗时。
- **测试数据**：性能红线阈值：`elapsed_seconds ≤ 15.0`；Windows 平台 `sys.platform == "win32"`。

| 步骤 # | 执行动作（Action） | 可观测预期结果（Expected Result） |
|---:|---|---|
| 1 | 使用 `time.perf_counter()` 测量执行 `agy-pool switch T1 --to backup` 从入口到 `SessionLauncher` 拉起完成的总耗时。 | 端到端总耗时 `≤ 15.0` 秒（目标基线 `< 3.0` 秒），生成的 `handoffs/T1-seg1.md` 体积 `≤ 3,500 Tokens`。 |
| 2 | 审查执行期间所有内部后台命令（`git status`、`git diff --stat`、外部适配器命令）的 `subprocess` 调用参数，并在真实 Windows Terminal 中人工目视核验一次。 | 所有非交互后台子进程调用在 Windows 下均传入 `creationflags=subprocess.CREATE_NO_WINDOW`，真实终端切换全程零 conhost 黑框闪现。 |

---

## 四、自动化准入分级建议（Automation Graduation ROI）

依据 ROI 评估公式 `价值 ≈ 执行频率 × 回归关键度 ÷ (自动化实现成本 × 预期维护成本)`，结合本项目“纯 Python 3.12 标准库 CLI + 明确适配器 Seam”的低维护成本特征，准入排序如下：

| 准入优先级 | 覆盖用例 | 对应自动化测试模块 | ROI 依据 |
|---|---|---|---|
| **第一梯队：每次提交必跑（核心单元与组件自动化，32 条）** | `TC-POOL-001`~`008`, `012`~`023`, `026`~`037` | `agy-pool/tests/test_store.py`<br>`agy-pool/tests/test_handoff.py`<br>`agy-pool/tests/test_orchestrator.py`<br>`agy-hud/test_hud.py` | 覆盖原子写盘、Token 预算裁剪、状态机回滚、多终端防撞、敏感信息脱敏与 HUD 零退化；纯函数与文件 I/O 边界稳定、执行速度毫秒级，ROI 极高。 |
| **第二梯队：合并前必跑（CLI 端到端与性能基准，6 条）** | `TC-POOL-009`~`011`, `024`, `025`, `038`（自动计时与标志位断言） | `agy-pool/tests/test_cli_e2e.py` | 校验 CLI 参数解析、子进程退出码及 8MB 超大日志 `≤ 15s` 耗时门禁。 |
| **保留人工验收（Manual Only，2 项）** | 1. 真实 Google OAuth 浏览器 `/logout` + 登录交互切换走查（`TC-POOL-019` 真实浏览器侧）<br>2. 真实 Windows Terminal 双窗口并行色值排版与无黑框闪烁肉眼终验（`TC-POOL-038` 步骤 2） | 里程碑发布前人工 Checklist | 真实 Google OAuth 含浏览器安全/人机校验且严禁触碰凭据，自动化成本高且脆弱（负 ROI）；终端视觉观感适合人工终验。 |
