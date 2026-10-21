# Changelog

## 2.0.0-rc1 — 从业务流水线到 Agent 系统

1.0 是一个**有 Agent 角色的业务流程引擎**：状态机、确定性定价、审批闸门都在，但编排是固定
循环，没有检索，没有模型选工具。这一版把 JD 点名的三件事接进了真实执行路径。

### 新增：LangGraph 条件路由与可恢复闸门（`app/graph/`）

- `builder.py` 把 11 个节点组织成状态图，在四个位置使用条件边：字段不全 → `clarify`、
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

### 新增：把这些字段读回来（`app/services/metrics.py`、`GET /api/v1/metrics/agent`）

上一节写的 NULL 口径，如果没人读，就只是列名。`agent_runs` 的 `latency_ms` /
`prompt_tokens` / `cost_usd` 与 `pipeline_graph_runs.steps` 里的节点耗时此前**只写不读**，
「我们度量了成本」因此是一句关于列的话，不是一个能报出来的数。

- `agent_metrics`：每次运行耗时 p50/p95/max、token 汇总、成本、按 agent 拆分的失败数；
- `graph_metrics`：每个节点 p50/p95、`stop_reason` 分布、`node:status` 计数；
- 三条口径有测试守着：**未上报的运行计入 `unreported_runs` 并排除在均值之外**（分母是
  `reported_runs`）；**百分位是 nearest-rank，不插值**（空样本返回 `null` 不返回 0）；
  **窗口上限 90 天**（否则一个开着不关的看板能扫全表）。
- 节点耗时记在 `_guarded` 包装器里而不是各节点内部：新增节点不会忘记加计时，
  **失败路径也被计时**——跑 30 秒才失败的节点才是最该看见的。

### 新增：文档数字的守卫（`tests/test_doc_claims.py`）

README 里的路由数、表/索引数、只读工具数，CHANGELOG 里的节点数，现在都是断言。
这一条是被自己的错误逼出来的：v2.0 的提交信息写「12 个节点」，CHANGELOG 写「10 个」，
而 `NODES` 实际是 **11**。文档里的数字是对代码的断言，没有东西守着它就只是修辞。
（测试数量刻意不守：在数自己的套件里断言自己，只会变成自指。）

### 新增：演示界面（`demo_client/`）

v2.0 的后端长出了四个能力面，界面上却只看得到一个 v1 的流程条。这不是「页面不够漂亮」，
是**能力不可见**：图编排、拒答、工具轨迹、延迟与成本，一个都演不出来。
补页面的同时补了它们的证据，设计与评估口径见 `docs/UI_DESIGN.md`。

- `theme.css`：把配色与组件抽成共享外壳。**没有**去改 `overview.html` / `prospecting.html`
  内联的令牌副本——那两个文件有契约测试守着，且没人要求改它们，机械重写只产生 diff 噪声；
  只给它们注入统一顶栏。
- `graph.html`：11 节点轨道 + 执行轨迹 + 三种真实闸门交互（缺字段追问 / 报价不足录入 /
  人工放行）。**没有自动审批路径**，合成报价开关默认关且显式传参。
  刷新不丢进度（从 `/graph/status/{id}` 读持久化游标）。
- `knowledge.html`：三个页签（SOP 问答 / 工具调用 / 运行度量）。拒答写成「这是正确结果」
  并给出阈值推理，同时清空上一次的命中——留着旧命中会让拒答看起来像「查到了不敢说」。
  成本为 `null` 时印 `null` 并解释，不印 `0`。
- `index.html` 重写为四张入口卡，每张写明前置条件；原来的单表单页面看不到项目边界。
- 新增两个**只读**端点：`GET /api/v1/inquiries`（下拉框数据源，`limit` 夹 1..100）、
  `GET /api/v1/knowledge/documents`（区分「语料为空」与「没有覆盖这个问题」）。
- 三层守卫：`tests/test_demo_contract.py`（阶段名）→ `tests/test_demo_pages.py`（节点名、
  元素 id、端点名）→ `scripts/page_smoke.js` + `tests/test_page_runtime.py`（jsdom 里真执行
  内联 JS、真点按钮、真读 DOM）。前两层是静态解析，只能证明页面**提到**了 `hitl_review`；
  第三层才能证明它**渲染得出来**——这个区别本仓库已经付过一次代价。
  冒烟在 node/jsdom 缺失时 **skip 并说明原因**，不伪造绿勾；单页检查数少于 12 项即判为
  「脚本可能坏了」而不是通过。

### 新增：外联评分闸门与来源可核验性（`services/prospecting.py`）

调研同类项目后补的两处，来源与取舍见 `docs/GITHUB_LANDSCAPE.md`，
规则 → 强制点 → 测试的对照见 `docs/OUTREACH_COMPLIANCE.md`。

- **第四道门禁：匹配度下限**（`OUTREACH_MIN_SCORE`，默认 70）。此前 `score` 只用于排序和
  决定 Lead 初始状态，**没有任何门禁作用**，一封写给低分线索的信和写给高分线索的信
  走的是同一条路。借鉴 `Codex-native-Lead-Hunter` 的 `fit_score >= 7`。
  放在**发送**而非起草：起草不产生外部副作用，低分线索仍可出草稿；被拒后记录停在
  `approved` 而不是 `sent`。顺序排在审批与合法依据之后——「你还没审批」是可行动的信息，
  「分数不够」不是，先报前者。未知分数**失败即拒**（`outreach_score_unavailable`）。
- **来源可核验性**：`GET /api/v1/prospects/{id}/evidence` 返回四级判断
  `synthetic` / `placeholder` / `traceable` / `openable`。Mock 源写的是 `.example`
  保留域，**永远解析不出来**，所以它被判成 `synthetic` 且不给出链接——
  一个点开是 404 的「来源」会通过评审而不被检查，比明说「无法核验」更糟。
  `_is_publicly_openable()` 按 RFC 2606 拦保留域，`test_a_reserved_domain_is_not_evidence`
  直接对它做单元断言。可核验性**不是**门禁：大陆报关数据持牌而非公开，
  把它做成门禁会让真实部署里所有合法线索都发不出去。
- 两处都做了变异验证：把 `OUTREACH_MIN_SCORE` 调成 0，
  `test_a_low_score_blocks_the_send_but_not_the_draft` 立刻以 `DID NOT RAISE` 变红。

### 新增：真实名单的导入路径（`POST /prospects/import`、`POST /prospects/{id}/score`）

上一节补了「不能随便发」的门禁，但没解决「数据从哪来」。此前获客模块只有
`MockProspectingSource` 一个实现，`LicensedVendorProspectingSource` 是
`raise NotImplementedError` 的骨架——**「可插拔数据源」描述的是一个只有一个实现、
而且没有任何入口能喂数据的接口**。生产上这条链路根本跑不起来。

- **导入**：`POST /api/v1/prospects/import`，JSON 行数组或 CSV 文本。
  `source` / `data_origin` 必填——一份说不出从哪来的名单，正是不能合法发信的那份，
  在门口就 422，而不是先存下来以后再想。`contract_ref` 记录合同号。
- **表头映射**：`Est Volume TEU` / `teu` / `货量` / `annual teu` 归一成同一字段。
  不做映射的话货量静默变 `None`，再被打成「低货量线索」——
  一个看起来像数据问题、实际是映射问题的错误答案。
- **双键去重**：`(source, source_ref)` 防同一份导出重导；`(归一化公司名, 邮箱)`
  防两家数据商买到同一家货主。同名不同邮箱**保留**（那是第二个联系人，不是重复）。
- **逐条评分**：`POST /api/v1/prospects/{id}/score`。导入落库是 `status='imported'`
  且 `score=0`，**导入不等于合格**；评分下限（默认 70）因此直接挡住「买完就群发」。
  Agent 判定 `excluded` 记 `status='rejected'` 而不是留个 0 分——0 分看起来永远像
  「还没评分」，没人会去清理。Agent 没给判定则抛 `agent_returned_no_verdict`，
  不把「模型没回答」洗成「模型说它不行」。
- `docs/ACQUISITION_PLAYBOOK.md`：六类真实数据源（海关/提单持牌数据、工商信息、
  展会名录、B2B 平台、招标公告、招聘信号）、三条进入路径、买名单的四个坑与对策、
  四条已知缺口（跨供应商实体解析只做了归一化、无字段级来源追溯、无新鲜度衰减、无渠道归因）。

### 新增：名单的数据时点（`data_as_of`）

「我们每天更新」是供应商对**数据流**的声明，不是对这一行数据的声明。
系统此前记录了来源（`source` / `data_origin` / `contract_ref`），但**没有记录这批数据是哪天的**，
于是「更新频率」只能靠相信。

- 导入时可传 `data_as_of`（供应商声称的数据截止日），存进 `raw_payload`；
  `GET /prospects/{id}/evidence` 返回它和 `age_days`。
- 日期缺失、无法解析、**或在未来**，一律返回 `null` 而不是 0：
  「不知道多旧」和「今天刚更新」是两件事；负的 age 会让一次坏导入看起来比今天还新。
- `docs/ACQUISITION_PLAYBOOK.md` 增加「2026-10-21 价格与更新频率快照」：
  ImportGenius 官网现价（$229 / $449 / $1,999 每月起）、ImportYeti 免费、国内四家的量级区间，
  以及**哪些数字是官网读到的、哪些是二手来源**——不标来源的价格就是传言。

### 修复

- **中文行业名被打低 25 分，而理由说它「匹配」**：mock 评分只匹配英文行业词，
  导入一份中文名单后「机械」拿 57 分、「Machinery」拿 82 分，同时理由打印
  「行业 机械 与集装箱航线匹配」——**理由与分数互相矛盾**。
  这比分数错更糟：错分数会有人质疑，自相矛盾的理由不会。
  现在中英同义词一并匹配，且理由按是否真的匹配来选择措辞
  （`不是典型集装箱货类` 不再被写成 `与集装箱航线匹配`）。
  这个缺陷是**导入一份中文 CSV 时**发现的，读代码看不出来。
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
- **成本与延迟字段只写不读**：`latency_ms` / `prompt_tokens` / `cost_usd` 记了但没有出口，
  等于没有可观测性。补上 `app/services/metrics.py` 与 `/api/v1/metrics/agent`。
- **节点耗时缺失**：`steps` 只记「发生了什么」，不记「花了多久」，因此无法回答延迟问题。
  计时移到 `_guarded` 包装器，失败路径一并计时。
- **启动钩子仍在 `create_all`**：引导脚本改到 alembic 之后，`app/main.py` 的启动钩子
  还留着 `Base.metadata.create_all(engine)`。`create_all` 只补建缺失的表、从不给已有表
  加列，所以任何新增列的迁移都会让旧库静默缺列（症状是 `no such column: agent_runs.prompt_tokens`），
  并且它建出的库没有 `alembic_version` 行，使 `alembic upgrade head` 死在
  `table agent_runs already exists`——用来修的手段被造成问题的东西挡住。
  现在启动只做检查（`app/schema_guard.py`），不在 head 就拒绝启动并给出修复命令。
  `worker/scheduler.py` 与 `scripts/seed_demo.py` 同样在 `create_all`，一并改为守卫；
  `tests/test_schema_guard.py` 用 AST 扫过 `app/`、`worker/`、`scripts/` 全部文件，
  断言 `create_all`/`drop_all` 不再出现（已用变异测试验证会红）。
- **节点失败后图继续往下跑**：`understand` 失败后，`schedules`/`rates`/`rfq`/`supplier_quotes`
  依次继续执行并各自失败，最后上报的原因是**最后一个**失败（`supplier_quotes:rfq_missing`），
  于是一个「询价单不存在」被诊断成「RFQ 缺失」。中段四条普通边改为条件边，
  失败即在第一个失败节点终止并上报该节点。这个缺陷是**跑真实服务**时发现的，测试此前没有覆盖。

### 指标

| 项目 | 1.0.0-rc3 | 2.0.0-rc1 |
|---|---|---|
| 测试 | 189 | **318** |
| 覆盖率（整体） | 85% | **82%**（新增约 900 行 Agent 代码，新模块自身 88%） |
| API 路由 | 43 | **57** |
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
