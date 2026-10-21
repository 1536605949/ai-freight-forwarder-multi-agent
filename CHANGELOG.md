# Changelog

## 2.0.0-rc1 — 从业务流水线到 Agent 系统

1.0 是一个**有 Agent 角色的业务流程引擎**：状态机、确定性定价、审批闸门都在，但编排是固定
循环，没有检索，没有模型选工具。这一版把 JD 点名的三件事接进了真实执行路径。

### 新增：LangGraph 条件路由与可恢复闸门（`app/graph/`）

- `builder.py` 把 10 个节点组织成状态图，在四个位置使用条件边：字段不全 → `clarify`、
  报价不足 → 停止等待、审批 → 两个出口、任一节点失败 → 终止。
- `set_conditional_entry_point` 让「从停止的节点继续」成为一等路径，而不是另写一条
  `continue` 代码——两份实现必然漂移。
- **刻意不使用内存 checkpointer**：报价等审批可能跨越一次发布，内存 checkpoint 必丢。
  持久化的是 `pipeline_graph_runs` 里的游标，业务状态仍然只在领域表里。
- 图**没有任何自动审批路径**，比旧的 `orchestrator` 更严格。

### 新增：SOP 知识库与带引用作答（`app/knowledge/`）

- 标题感知切片（不跨标题合并，标题随片段参与向量化）、哈希向量 + BM25、RRF 排序、
  手写重排、有界打分、抽取式作答 + 引用。
- **拒答是一等结果**：域外问题由字符覆盖门在打分前拒绝，返回 `insufficient_evidence`。
- 默认哈希向量**明确不是语义模型**，代码与 README 都写明；`EMBEDDING_PROVIDER=openai`
  是替换路径，不是承诺。
- 新增 `evaluation/knowledge_eval.py`：22 正例 / 12 负例，hit@1 90.9%、hit@3 95.5%、
  MRR 0.932、负例误答 0。脚本会打印样本量，防止把 22 条读成质量保证。

### 新增：受控工具调用（`app/agents/tools.py`、`tool_loop.py`）

- 5 个只读工具，全部租户隔离。`invoke()` 在**分发路径**上拒绝任何 `read_only=False` 的工具，
  即使将来有人注册了写工具，模型也无法通过选择它来调用。
- 循环有硬步数上限（`AGENT_MAX_TOOL_STEPS`），被拒绝的调用记录但不重试。
- 作答是抽取式的：SOP 没检索到就明确说没有依据，不让模型「总结」出看似合理的内容。

### 新增：超时、重试与 token 成本（`app/agents/resilience.py`）

- 每次模型调用有超时；重试**只对瞬态故障**生效，业务拒绝（`ValueError`）不重试。
- token 从 MAF 与 OpenAI 两种 provider 形态提取，成本按配置单价计算；
  **未上报就记 NULL，不记 0**——记 0 会让成本指标永远好看。

### 修复

- **图在闸门失败后仍继续执行**：`quote_send` 原本用普通边连到 `booking`，审批拒绝后图
  仍去订舱，只在订舱处第二次失败。改为条件边，并由
  `test_resume_without_approval_fails_at_the_gate_and_does_not_send` 守住。
- **生产环境可以注入合成报价**：旧 `OrchestrateIn.auto_supplier_quotes` 默认 `True`，
  生产调用方省略该字段就会把演示报价混进真实比价。现在 `None` 表示按环境决定，
  生产强制关闭；`auto_approve` 在生产直接抛错，而不是静默忽略。
- **策略把询价单号当检索词**：`inq_...` 的 15 个字符计入未知内容，把一句完全可答的问题
  拉到域外门以下。工具调用前剥离对象 id。
- **迁移 AST 检查只看第一个版本**：新增 revision 可能重新引入 `create_all` 而不被发现。
  现在检查 `migrations/versions/*.py` 全部文件；版本号断言改为与 `ScriptHead` 比较，
  不再硬编码 `0001_initial`。
- `bootstrap_dev.py` 建库后同时灌入 SOP 语料（幂等），否则知识接口对每个问题都拒答，
  看起来像 bug 而不像没灌数据。

### 指标

| 项目 | 1.0.0-rc3 | 2.0.0-rc1 |
|---|---|---|
| 测试 | 189 | **238** |
| 覆盖率（整体） | 85% | **82%**（新增约 900 行 Agent 代码，新模块自身 88%） |
| API 路由 | 43 | **51** |
| 数据表 / 索引 | 20 / 35 | **23 / 41** |
| 检索评测 | 无 | **hit@3 95.5%，负例误答 0/12** |
| 依赖 | 无编排框架 | **langgraph 1.2.x（硬依赖）** |


## 1.0.0-rc3 — 第三次复核：完整性制品与 schema 归属

以「交付物自己能不能验证自己」为标准再复核一轮，又发现两个问题。它们的共同点是
**声明与执行不一致**：文件声称保证某件事，实际并不保证。

### 修复（P0）

- **`SHA256SUMS.txt` 在全新 clone 上校验失败**：该校验和是用 Windows 工作区字节（CRLF）算的，
  而 `.gitattributes`（`* text=auto eol=lf`）在仓库里存的是 LF。结果是 11 个文件在
  `git clone` 之后 `sha256sum -c` 直接报错——**完整性制品保护不了它声称要保护的东西**。
  新增 `scripts/gen_checksums.py`：按「clone 真正拿到的字节」计算（二进制按 NUL 判定后原样，
  文本先归一化为 LF），两个文件都以 LF 写出，因此跨平台幂等。
  `tests/test_packaging_manifest.py` 用 `git cat-file blob HEAD:<path>` 作为基准做交叉校验，
  正是这个测试抓出了最初的 CRLF 缺陷。CI 新增 `sha256sum -c SHA256SUMS.txt` 与
  `gen_checksums.py --check` 两步。
- **`bootstrap_dev.py` 让 schema 有了两个真相来源**：它用 `Base.metadata.create_all()` 建库，
  会建出全部表但**不写 `alembic_version` 行**，于是本地库看似健康、下一次
  `alembic upgrade head`（生产路径）却死在 `table agent_runs already exists`。
  已改为执行 `alembic upgrade head`，与生产同一条代码路径；
  `--reset-db` 会连带清掉 `alembic_version` 再重建。
  老库会被明确识别（区分「无 `alembic_version` 表」与「表存在但为空」）并给出两条出路，
  刻意不自动 stamp——自动 stamp 会掩盖真实漂移。

### 修复（P1）

- `scripts/gen_checksums.py` 自身修掉一个构造性缺陷：`MANIFEST.txt` 不能从磁盘读取后计算哈希
  （本次运行正要重写它，读到的是旧内容），改为对**新内容**取哈希。
- 10 个文本文件的工作区行尾从 CRLF 归一化为 LF，与仓库内实际存储一致；
  内容零变化（`git diff` 对这些文件为空）。

### 基线变化

| 指标 | rc2 | rc3 |
|---|---|---|
| 测试数 | 179 | 189 |
| 覆盖率 | 83% | 85% |
| 完整性校验 | 未纳入 CI | 130/130，CI 强制 |
| 建库路径 | `create_all`（与迁移脱节） | `alembic upgrade head`（与生产一致） |
| CI 步骤 | 5 | 7 |

## 1.0.0-rc2 — 交付可信度复核

第二轮以「能否经得起面试级追问」为标准复核，修复 3 个 P0 并补齐回归测试。
逐项对照见 [`docs/ARCHITECTURE_REVIEW.md`](docs/ARCHITECTURE_REVIEW.md) 第十节。

### 修复（P0）

- **评测脚本假通过**：`evaluation/run_eval.py` 中 3 个 critical 用例（`hitl1` / `pricing1` /
  `tenant1`）从未真正执行却恒返回通过，使「critical eval 0 fail」上线门槛形同虚设。
  已改为真实端到端断言，并引入 `executed` 标记——**未执行的用例不算通过**，未知
  `kind` / `rule` 一律 FAIL。`tests/test_eval_harness.py` 用变异测试证明它真的会失败。
- **伪迁移**：`0001_initial` 使用 `create_all` / `drop_all`，同一 revision 在不同代码版本下
  产出不同 schema。已重写为显式 DDL 冻结快照（20 表 / 35 索引），
  `tests/test_migrations.py` 断言「升级后 schema == ORM 声明」「downgrade 不留残余」
  「升降级往返可复现」。
- **状态回退**：`PATCH /api/v1/inquiries/{id}` 会把已订舱（`booked`）的询价倒回 `ready`。
  新增 `app/state_machine.py`（显式迁移图 + 幂等且只进不退的 `advance_status()`），
  七处无条件状态写入改为经其校验；终态询价内容冻结（409 `inquiry_locked`）。

### 修复（P1 / P2）

- `main.py` 覆盖率 0% → 66%：新增 `tests/test_api_routes.py`（鉴权边界、跨租户 404、409 映射）。
- `worker/scheduler.py` 0% → 94%：轮询体抽为 `run_once()`，新增 `--once`（cron 场景）。
- 补全 `.env.example`（此前**完全不存在**，README 却让人复制它）；
  `tests/test_config_docs.py` 双向断言与 `config.py` 同步。
- `maf_workflows.py` 标注 `NOT WIRED` 并加可操作的 `ImportError`；AST 测试固化其孤儿状态。
- CI 增加 lint / tests / packaging / `alembic upgrade head` / smoke boot 五步。
- `pyproject.toml` 显式声明 ruff 规则集，全仓零告警（此前 CI 不跑 ruff，约 660 条告警淹没真实问题）。
- `_mock_parse` 补 ETD 解析——此前 ETD 永不解析，而它是必填项，导致经真实投递入口进入的
  询价**永远**停在 `needs_clarification`，流水线无法从入口走到闸门。
- 新增 `tests/test_architecture_asset.py`：AST 检测导入环、适配层反向依赖、HTTP 层反向依赖。
- 初始化 Git 仓库。

### 基线变化

| 指标 | rc1 | rc2 |
|---|---|---|
| 测试数 | 99 | 179 |
| 覆盖率 | 71% | 83% |
| 0% 覆盖模块 | 2 | 0 |
| 行为评测 | 5 用例（3 个假通过） | 6 用例（全部真实执行，且证明会失败） |

## 1.0.0-rc1

- 完成 Freight Forwarding 12-stage business lifecycle skeleton.
- 加入 Microsoft Agent Framework runtime adapter + deterministic mock runtime.
- 加入 Inquiry parsing、RFQ fan-out、quote optimization、HITL、Booking、Tracking、Follow-up。
- 加入 multi-tenant boundary、audit、AgentRun、Prometheus、Worker、Docker Compose、tests/eval。
- 将 Project 2 完整架构图加入 `docs/architecture/`。
- 加入主动获客链路（Prospecting + Enrichment + Cold Outreach，含三道合规门禁）。
- 加入 AIS 船位与主动延误预警。
- 加入统一编排（10 阶段 + 2 人工闸门）。
