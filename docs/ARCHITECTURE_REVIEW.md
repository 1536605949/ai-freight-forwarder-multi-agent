# 架构审查报告

审查日期：2026-09-16　｜　审查范围：全仓库（`app/` 43 模块 · 20 数据表 · 43 路由 · 179 测试）
审查方式：静态依赖分析 + 覆盖率实测 + 关键路径动态复现（非纸面评审）

> **复核状态（2026-09-16 第二轮）**：3 个 P0 已全部修复并有回归测试；P1/P2 中 6 项已修复。
> 逐项对照见文末[第十节 修复记录](#十修复记录2026-09-16-复核)。
> 下文各条目的问题描述保留原始措辞，以便对照"发现什么"与"改了什么"。

---

## 一、总体结论

**架构方向正确，分层纪律在同类项目中属于上游水平；主要问题集中在"交付可信度"而非"业务设计"。**

三条核心设计决策经得起检验：

1. **Agent 与确定性代码的边界清晰且可验证。** `pricing` / `approval` / `booking` / `rfq` / `vessel` 五个关键模块语句覆盖 100%，说明"钱和权限不交给模型"不是口号而是有测试兜底的实现。
2. **门禁落在服务层而非编排层。** `services/approval.py::send_quote` 里写着 `if q.status!='approved': raise ValueError('approval_required')`。编排层即使被改错也无法绕过——这是把权限判断放在离数据最近的地方，正确的做法。
3. **依赖方向严格单向。** 实测 42 个模块、**0 个循环依赖**；适配层（`providers/*`）无一处反向依赖 `models` 或 `services`，是干净的叶子节点。

同时发现 **3 个 P0 级问题**，其中 1 个已动态复现，都会让"看起来完成了"与"实际完成了"产生偏差。

---

## 二、模块职责与分层

| 层 | 模块 | 职责 | 实测扇出 | 语句覆盖 |
|---|---|---|---|---|
| 入口 | `main.py` · `auth.py` · `schemas.py` | HTTP 路由、鉴权、入参校验 | 25 | 66% / 100% / 100% |
| 编排 | `orchestrator.py` | 10 阶段串联、闸门停止与恢复 | 10 | 90% |
| Agent | `specialists.py` → `runtime.py` · `prompts.py` | 理解 / 决策 / 生成（概率性） | 2 | 69% |
| 确定性服务 | `pricing` `approval` `booking` `rfq` `prospecting` `vessel` `rates` `schedule` `inquiry` `followup` `tracking` `crm` `growth` `lead` | 金额、权限、状态迁移、门禁、审计 | ≤7 | 89% |
| 数据源适配 | `providers/{rates,schedule,booking,tracking,email,prospecting,vessel}` | 外部系统归一化，Mock/真实同接口 | 1（仅 config） | 76% |
| 状态机 | `state_machine.py` | 迁移图 + `advance_status()` 幂等/只进不退 | 0 | 100% |
| 共享内核 | `models`(20 表) `config`(45 项) `common` `enums` `schemas` `db` | 被上层全部依赖，自身不依赖业务 | 1 | 97% |
| 后台 | `worker/scheduler.py` | 定时处理到期跟进任务（支持 `--once`） | 1 | 94% |

**扇入排行**：`models`(19) → `config`(16) → `common`(14)。三个模块被过半代码依赖，是稳定的共享内核，未出现"什么都往 utils 塞"的失控。

---

## 三、数据流向

```
客户来信 → main.py(inbound)
   → 意图识别 → 询价解析 → 落库 Inquiry（缺失字段则回问，不猜测）
   → [可逆自动化] 船期 → 运价 → RFQ → 收报价 → 比价定价
   → 落库 CustomerQuote(status=waiting_approval)
   → ⛔ 闸门 1：人工放行（编排层只能停止，无法自行放行）
   → ⛔ 闸门 2：报价外发（服务层校验 status=='approved'）
   → [不可逆自动化] 订舱 → 船位跟踪 → 延误预警生成 FollowUpTask
   → Worker 轮询到期任务 → 发跟进邮件
```

**关键性质**：每个阶段的产出即时落库（`inquiry` / `rfq` / `quote` / `booking` 均可独立查询），HTTP 请求不承担等待职责。实测长流程无"挂起等供应商回信"的不可恢复状态。

---

## 四、P0 级问题（影响正确性或交付可信度）

### P0-1　评测脚本 5 个用例中 3 个是假通过　【已修复】

`evaluation/run_eval.py` 只实现了 `kind=='parser'` 分支：

```python
ok=True; detail='static rule documented'     # ← 默认值，非 parser 用例直接通过
if c['kind']=='parser':
    ...  # 只有这里真正执行
```

`golden_dataset.jsonl` 中 3 个 critical 用例的 `kind` 是 `safety` / `security`，全部落入默认分支。实测报告：

```
inq1       pass=True  executed
inq2       pass=True  executed
hitl1      pass=True  *** FAKE (never executed) ***
pricing1   pass=True  *** FAKE (never executed) ***
tenant1    pass=True  *** FAKE (never executed) ***
```

**危害**：`HOW_TO_RUN.txt` 把「critical eval 0 fail」列为上线门槛，而该门槛永远为绿。`hitl1`（未审批不得发送）、`pricing1`（售价≥成本+最低毛利）、`tenant1`（租户隔离）这三条恰好是最需要回归保护的安全属性，却完全没有被验证。

**修复**：为 `safety` / `security` 用例补真实断言——`hitl1` 调用 `send_quote` 期望抛 `approval_required`；`pricing1` 断言 `sell_amount >= cost_amount + minimum_margin_usd`；`tenant1` 用另一租户身份请求并断言 404。约 40 行代码即可，且这些断言在 `tests/` 中已有现成写法可复用。

### P0-2　迁移脚本是伪迁移，且 downgrade 会删全库　【已修复】

`migrations/versions/0001_initial.py`：

```python
def upgrade():   Base.metadata.create_all(bind=op.get_bind())
def downgrade(): Base.metadata.drop_all(bind=op.get_bind())
```

三个后果：

- **同一 revision 在不同代码版本下 schema 不同**，迁移失去版本意义；
- `alembic downgrade` 会 **drop 掉数据库里所有表**，不只是本 revision 涉及的表；
- 真正需要 schema 演进时（如给 `inquiries` 加字段），没有可用的迁移路径。

**修复**：用 `alembic revision --autogenerate` 生成真实迁移（`create_table` / `add_column`），并把 `0001_initial` 改为显式建表。或明确降级定位——删除 `migrations/`，改为文档声明「本项目用 `create_all` 管理 schema，不提供迁移」，避免给出虚假的迁移承诺。

### P0-3　`PATCH /inquiries` 可回退已订舱状态（已复现）　【已修复】

`patch_inquiry` 只做字段写入 + 重算 `missing_fields`，无任何状态迁移校验：

```python
for k,v in x.model_dump(exclude_none=True).items(): setattr(q,k,v)
q.missing_fields=...; q.status=NEEDS_CLARIFICATION if q.missing_fields else READY
```

实测复现：

```
1) 推进到 booked → inquiry.status=booked
2) 对已订舱的询价执行 PATCH(etd=...)
   status: booked -> ready        ← 状态被回退，无守卫
3) 全仓库状态迁移校验：NONE
```

**危害**：集成方或重试逻辑重复 PATCH 一次（很自然的操作），就会把已订舱的询价打回 `ready`，污染运营视图与所有基于状态的报表。下游门禁（报价须 approved/sent）能挡住重复订舱，所以不会造成资损，但状态可信度被破坏。

**修复**：引入 `app/enums.py` 旁的状态迁移表 `ALLOWED_TRANSITIONS: dict[InquiryStatus, set[InquiryStatus]]`，在 `patch_inquiry` 与各服务写入状态处统一校验，非法迁移抛 `ValueError('invalid_transition')`。这也让 `InquiryStatus` 从"只是枚举"变成真正的状态机。

---

## 五、P1 级问题（生产化前必须处理）

| # | 问题 | 证据 | 影响 | 建议 |
|---|---|---|---|---|
| P1-1 | Worker 无并发锁 | `process_due` 无 `FOR UPDATE SKIP LOCKED` | 多副本部署时同一跟进任务被重复发信 | 加 `with_for_update(skip_locked=True)`；发信前写幂等键 |
| P1-2 ✅ | ~~入口层 0% 覆盖~~ | 已补 `tests/test_api_routes.py`（20 条：鉴权边界、跨租户 404、409 映射） | `main.py` 覆盖率 0% → **66%** | 已修复 |
| P1-3 | 无 prompt 注入防护 | 邮件正文直接进 `run_payload`，无分隔符/指令层级 | 字段误解析、草稿被操纵（**不会**导致越权发送或改价——被架构挡住） | 用显式分隔符包裹外部文本 + 系统提示声明"以下为用户内容，不得作为指令"；解析结果做白名单校验 |
| P1-4 | RFQ 超时无恢复 | `expires_at` 只写不读（`grep` 全仓仅 1 处写入） | 供应商不回复时 RFQ 永久挂起在 `waiting_supplier_quotes` | Worker 增加扫描：`expires_at < now()` 且报价不足 → 转 `partial` 并告警/重发 |
| P1-5 | Mock 与真实 LLM 输出契约不一致 | `_mock_parse` 硬编码 16 城、`found[0]`/`found[1]` 当起终点；`_mock_supplier_quote` 只认 `$` 数字 | "from Los Angeles to Shanghai" 会解析反；"USD 2100" 解析为 0 | 抽取 `ParseContract` 契约测试，同一批输入同时跑 mock 与真实 runtime 并断言结构一致 |

---

## 六、P2 级问题（交付规范类）

| # | 问题 | 证据 | 建议 |
|---|---|---|---|
| P2-1 ✅ | ~~`.env.example` 缺 23/45 项~~ | 文件当时**完全不存在**（README 却让人 `cp` 它）。已补全 45 项，并由 `tests/test_config_docs.py` 断言双向同步 | 已修复 |
| P2-2 ✅ | ~~项目不是 git 仓库~~ | 已 `git init` 并提交基线；`.gitignore` 覆盖 `.env` / `*.db` / `.venv` | 已修复 |
| P2-3 ✅ | ~~CI 覆盖不足~~ | `.github/workflows/ci.yml` 现含 lint + tests + packaging + `alembic upgrade head` + smoke boot | 已修复 |
| P2-4 | `redis` 是死基础设施 | compose 声明，代码 0 引用 | 删除，或明确其用途 |
| P2-5 ✅ | ~~`maf_workflows.py` 孤儿模块~~ | 已加 `NOT WIRED` 标注 + 可操作的 `ImportError`；`tests/test_maf_workflows.py` 用 AST 断言"无人 import"，接线时会主动失败 | 已修复 |
| P2-6 | `OutboxEvent` 只写不读 | 唯一写入点 `inquiry.py:28`，无消费者 | 实现 dispatcher，或改措辞为"预留"（`DATA_MODEL.md` 已诚实，但 `ARCHITECTURE_LAYERS.md` 把它画进了运行时图） |
| P2-7 | `orchestrator.py` 443 行 / 10 handler 内联 | 全仓最大模块，扇出 10 | 按"可逆阶段 / 闸门阶段"拆两个模块，handler 注册表外移 |
| P2-8 | `supplier_score` 权重未标定 | `total + transit×12 − free×8 − reliability×150`，量纲混合（USD 与天） | 归一化后加权，或改为可配置权重表并补标定说明 |
| P2-9 | 核心服务测试偏薄 | `pricing_hitl` / `booking` / `tenant_isolation` / `followup_compliance` 各仅 1 条 | 补边界：恰好等于最低毛利、reject 路径、部分报价恢复、并发订舱 |

---

## 七、进度评估

### 已完成（13 个任务全部关闭）

| 模块 | 产出 |
|---|---|
| 数据模型 | 20 张表，含潜客/触达/船位快照 |
| 主动获客链路 | 数据源适配 + 3 个 Agent + 服务层 + **三道合规门禁** + Demo 页 |
| 船舶位置 | AIS 适配 + 位置历史 + 按订舱去重的主动延误预警 |
| 运价体系 | 合约价 / 指数 / 合成三实现，带来源与有效期 |
| 统一编排 | 10 阶段 + 2 闸门 + 4 条路由，17 条测试 |
| 演示体系 | 总览页（含闸门交互）+ 获客页 + 最简页，11 条契约测试 |
| 启动与验收 | `bootstrap_dev.py`（幂等）+ `delivery_check.py`（40 项检查） |
| 质量基线 | ~~99 测试 / 71% / ruff 新文件零告警~~ → **179 测试全绿，83% 覆盖，ruff 全仓零告警**（规则集显式声明于 `pyproject.toml`） |

### 进行中

无。Task #13（端到端验收）已完成，40/40 项检查通过。

### 未开始 / 仅骨架

| 项 | 状态 | 性质 |
|---|---|---|
| 真实数据商对接（提单/海关/AIS/合约价） | 骨架就绪，待商业合同 | 非技术 |
| 生产 LLM 端到端（`AGENT_PROVIDER=maf_openai`） | runtime 存在，未端到端验证 | 需 Key |
| 分布式 Worker / 并发锁 | 未开始 | 技术 |
| prompt 注入防护 | 未开始 | 技术 |
| 迁移体系正规化 | 伪迁移 | 技术 |
| 多租户 RBAC / SSO | 仅 `require_role` 雏形 | 技术 |
| 可观测性深化（tracing / 告警规则） | 有 Prometheus 指标，无告警 | 技术 |
| 灾备 / RPO / RTO / SLO | 未开始 | 技术 |

---

## 八、建议执行顺序

**第一批（本周，修 P0——直接决定"能不能信"）**

1. 补齐 `run_eval.py` 的 3 个假用例 → 让上线门槛真正生效
2. 状态迁移表 + `patch_inquiry` 校验 → 修掉已复现的状态回退
3. 迁移正规化（或明确降级声明）→ 消除虚假承诺

**第二批（下一迭代，修 P1——生产化前置）**

4. `tests/test_routes.py`：43 路由 × 双租户隔离断言 → 补上最大的覆盖盲区
5. Worker 加 `SKIP LOCKED` + 发信幂等键
6. RFQ 超时扫描
7. prompt 注入防护（分隔符 + 白名单校验）

**第三批（收尾，修 P2——交付规范）**

8. `.env.example` 与 `config.py` 同步（加断言防漂移）
9. CI 补 ruff / delivery_check / docker build
10. 清理死基础设施与孤儿模块；拆分 `orchestrator.py`

---

## 九、值得保留的架构资产

以下几点在评审中应当主动展示，它们是这个项目区别于"套壳 Demo"的地方：

- **门禁在服务层**：编排层改错也绕不过 `approval_required`。
- **可逆/不可逆阶段分离**：自动化多干活但不越权，边界由"是否对外产生承诺"划定，而非由代码复杂度划定。
- **演示价自我标识**：`demo_` 前缀 + `synthetic=True`，审计中不可能与真实合约价混淆。
- **确定性 Mock**：同输入同输出，演示可复现、测试可断言，且无需任何 API Key 就能跑通全流程。
- **未核实数据物理隔离**：潜客存 `prospect_candidates`，不污染 `leads`/`customers` 的审计链。
- **合规门禁有独立测试**：`pytest.raises` 断言 `approval_required` / `compliance_basis_required` / `frequency_cap_exceeded`，而非依赖提示词。

---

## 十、修复记录（2026-09-16 复核）

第二轮以"能否经得起面试级追问"为标准复核，逐项修复并补齐回归测试。
每一条都给出**修复前的失败证据**与**修复后拦住它的测试**。

### P0-1　评测脚本假通过 → 已修复

`evaluation/run_eval.py` 重写：每个用例按 `kind` → `rule` 派发到真实断言，且结果必须带
`executed` 标记——**没执行的用例不算通过**。上线门槛同时要求「critical 无失败」与
「critical 全部真实执行」。未知 `kind` / `rule` 一律判 FAIL（fail-closed）。

三个原假用例的真实断言：

| 用例 | 修复后实际执行的动作 |
|---|---|
| `hitl1` | 投递询价 → 跑流水线到闸门 → 对未审批报价调 `POST /quotes/{id}/send` → 断言 409 且响应含 `approval_required` |
| `pricing1` | 从 `optimize` 阶段产出读 `cost/margin/sell` → 断言 `sell == cost+margin`、`margin >= 180`、`sell > cost` |
| `tenant1` | 属主读 200；另一租户读同一 `inquiry_id` 断言 404 |

**并且证明它真的会失败。** 两层证据：

1. `tests/test_eval_harness.py`（12 条）用注入式假客户端做变异测试：分别打断审批门禁、
   压低毛利、放开跨租户读取、让流水线不停在闸门——对应用例必须翻红。
2. **对运行中的服务做真实变异**：把 `send_quote()` 的审批检查注释掉后重启，评测返回

   ```
   FAIL hitl1   refusal did not cite approval_required:
                {'detail': 'illegal_status_transition: waiting_approval -> sent (quote.send)'}
   5/6 passed; critical: 5/6 ok, 1 failed, 0 not executed
   EXIT=1
   ```

   注意这里拦住它的是**状态机**而不是评测脚本——见下方"双层防御"。

同时补了正向用例 `inq3`（消息含 ETD → 缺失字段应为空），它顺带守住一个此前存在的缺陷：
`_mock_parse` **从不解析 ETD**，而 ETD 是必填项，导致通过真实投递入口进入系统的询价
**永远**停在 `needs_clarification`，整条流水线无法从入口走到闸门。已补 ETD 解析。

### P0-2　伪迁移 → 已修复

`migrations/versions/0001_initial.py` 重写为**显式 DDL 冻结快照**：20 张表、35 个索引，
`upgrade()` 逐表 `create_table`，`downgrade()` 逆序 `drop_table`。`create_all` / `drop_all`
不再出现在迁移中。

`tests/test_migrations.py`（5 条）守住真正重要的不变量：

- 空库 `upgrade head` 后的 schema 必须与 ORM 声明**逐列一致**（列名、可空性、类型族、主键、唯一约束）；
- `downgrade base` 不留残余表；
- 升降级往返后 schema 指纹完全一致（可复现性——这正是 `create_all` 破坏的性质）；
- 用 AST 断言迁移里**不存在** `create_all`/`drop_all` 调用（文档字符串里的提及不会误判）。

### P0-3　状态回退 → 已修复

新增 `app/state_machine.py`（`enums.py` 保持纯词汇表，迁移规则单独成模块）：

- `INQUIRY_TRANSITIONS`：显式迁移图，`CLOSED` 为唯一汇点，**没有任何回边**；
- `advance_status()`：幂等（重复投递静默 no-op）且**只进不退**（回退抛 `IllegalTransition`）；
- `CONTENT_EDITABLE` / `LOCKED`：区分"还在确认需求"与"承诺已成立"两个阶段。

七处无条件状态写入改为经 `advance_status()`：`schedule` / `rfq` / `pricing` / `approval`(×2) /
`booking`，加上 `patch_inquiry` 本身。

`tests/test_state_machine.py`（20 条）里最有价值的一条是**仪表化重放**：给所有会写状态的
服务模块装上 spy，跑一遍真实流水线，断言实际发生的迁移序列**恰好等于** `HAPPY_PATH`
（`ready→scheduled→waiting_supplier_quotes→waiting_approval→approved→sent→booked`）。
将来谁改了阶段顺序，测试会直接指出非法的那条边。

### 双层防御（本轮最重要的发现）

打断服务层的审批检查后，系统**仍然**没有把报价发出去——状态机独立发现询价想从
`waiting_approval` 跳到 `sent`，而中间缺了 `approved`。

这说明门禁不是单点防御：**即使有人改错了显式检查，状态图仍会拒绝这次非法跃迁。**
两层各自独立，且评测脚本让 CI 失败。

### P1 / P2 已修复项

| 项 | 修复 |
|---|---|
| P1-2 入口层 0% 覆盖 | 新增 `tests/test_api_routes.py`（20 条）：JWT 模式下的 401/403 边界、跨租户 404（而非 403，避免确认 id 存在）、`inquiry_locked` 映射为 409 而非 500、token 租户不可被 demo 头放大。`main.py` 0% → 66% |
| P2-1 配置样例缺失 | 补全 `.env.example`（45 项，与 `config.py` 逐项对齐）；`tests/test_config_docs.py` 双向断言，并验证示例可被 `Settings` 解析。`deployment/.env.example` 改为 compose 覆盖层，不再做第二份真相来源 |
| P2-2 非 git 仓库 | `git init` + 基线提交 |
| P2-3 CI 不足 | CI 增加 lint / tests / packaging / `alembic upgrade head` / smoke boot 五步 |
| P2-5 孤儿模块 | `maf_workflows.py` 顶部标注 `NOT WIRED`，缺依赖时抛可操作的 `ImportError`；`tests/test_maf_workflows.py` 用 AST 断言无人 import——真被接线时该测试会失败，提醒补集成测试并更新本文档 |
| 新增：ruff 不是有效门禁 | `pyproject.toml` 显式声明规则集（忽略 `E401/E701/E702/E703` 这类本仓库刻意的紧凑风格）。此前默认规则集会报约 660 条风格告警而 CI 根本不跑 ruff，真实的 `F401/F403/F405` 会淹没在噪声里。现已全部修净，`ruff check` 全仓零告警 |
| 新增：`worker/scheduler.py` 不可测 | 轮询体抽为 `run_once()`，`main(iterations=n)` 复用同一路径并新增 `--once`（cron 场景）。0% → 94% |
| 新增：状态回退的 409 映射 | `PATCH /inquiries` 原先只捕获 `KeyError`，`InquiryLocked` 会变成 500。已补 `ValueError` → 409 |

### 本轮修复后的实测基线

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 测试数 | 99 | **179** |
| 覆盖率（`app`+`worker`+`evaluation`） | 71% | **83%** |
| `main.py` 覆盖率 | 0% | **66%** |
| 0% 覆盖模块数 | 2（`main.py` / `worker`） | **0** |
| 行为评测 | 5 用例，其中 3 个假通过 | **6 用例，全部真实执行，且证明会失败** |
| 交付验收 | 40/40 | 40/40 |
| ruff | 未纳入 CI，全仓约 660 告警 | **全仓零告警，CI 强制** |
| 迁移 | `create_all` 伪迁移 | **显式冻结快照 + 漂移测试** |

### 仍未处理（诚实清单）

以下问题在本轮**有意未修**，因为它们要么需要外部条件，要么属于产品决策而非缺陷：

- **Worker 并发锁**（P1-1）：`process_due()` 仍无 `SELECT ... FOR UPDATE SKIP LOCKED`。单副本无影响，多副本部署前必须补。
- **RFQ 超时恢复**（P1-4）：`expires_at` 仍只写不读。
- **提示词注入防护**（P1-3）：当前把 LLM 输出限制在"生成措辞"，金额与状态不经过模型，但这不等于攻击面为零。
- **Mock 与真实 LLM 输出契约**（P1-5）：`_mock_parse` 的 16 城字典仍是演示级实现，未做契约测试。
- **`supplier_score` 权重未标定**（P2-8）：量纲混合（USD 与天）未归一化。
- **`redis` 死基础设施**（P2-4）：compose 仍声明，代码 0 引用。
- **`orchestrator.py` 443 行**（P2-7）：仍未拆分。
- **`InquiryPatch` 无法清空字段**：用 `None` 表示"未提供"，因此清空只能传空字符串。这是 PATCH 契约的真实缺口，已记录在 `tests/test_state_machine.py` 的注释里而非掩盖。

这些在 README 的「生产边界（诚实清单）」中同样列出——**知道边界在哪，比假装没有边界更重要。**

---

## 十一、第三次复核（同日）：完整性制品与 schema 归属

第二轮修完后按「**交付物自己能不能验证自己**」再走一遍，又抓到两个问题。它们的共同形态是
**声明与执行不一致**：某个文件声称保证一件事，实际并不保证。这类问题比明显的 bug 更危险，
因为它会让人停止检查。

### P0-4　`SHA256SUMS.txt` 在全新 clone 上校验失败

**现象**：`sha256sum -c SHA256SUMS.txt` 在开发机上 128/128 通过，但在 `git clone` 出来的
Linux 工作区上，**11 个文件失败**。

**根因**：校验和是用 Windows 工作区字节算的（CRLF），而 `.gitattributes` 里的
`* text=auto eol=lf` 让仓库实际存储 LF。**哈希算的不是 clone 会拿到的东西。**
换句话说：这个文件的全部意义就是"别人拿到后能验"，而它恰好做不到这件事。

**修复**：新增 `scripts/gen_checksums.py`，按 clone 真正收到的字节计算——
含 NUL 的按二进制原样处理（与 git `text=auto` 的判定一致），文本先归一化为 LF；
两个文件均以 LF 写出，跨平台幂等。CI 增加 `sha256sum -c SHA256SUMS.txt`
与 `python scripts/gen_checksums.py --check` 两步。

**为什么这次能抓到**：`tests/test_packaging_manifest.py` 用
`git cat-file blob HEAD:<path>`（= clone 的字节）作为基准做交叉校验，而不是拿磁盘文件
自我比对——**用同一个来源比对同一个来源，是永远不会失败的测试**。
该测试在实现过程中也暴露了自身一个误判：`git status --porcelain` 会因 stat cache 过期
把内容未变的文件标成 `M`（行尾归一化恰好触发），因此脏文件判定改用内容口径的
`git diff --name-only HEAD`。

### P0-5　`bootstrap_dev.py` 让 schema 有了两个真相来源

**现象**：按 README 执行 `python scripts/bootstrap_dev.py` 建库，随后
`alembic upgrade head`（CI 与生产都走这条）直接失败：
`sqlite3.OperationalError: table agent_runs already exists`。

**根因**：`init_db()` 用的是 `Base.metadata.create_all()`，它建出全部 20 张表，
但**不写 `alembic_version` 行**。数据库于是处于一个迁移系统无法接管的状态：
表都在，迁移系统却认为自己什么都没做过，于是从零重放 `0001_initial` 并撞表。
这不是"本地环境脏了"，而是**引导脚本和迁移脚本对"谁拥有 schema"给出了两个不同答案**，
而先漂移的那个永远是没被测的那个。

**修复**：`init_db()` 改为执行 `alembic upgrade head`——与生产同一条代码路径。
`migrations/env.py` 从 `get_settings().database_url` 取 URL，因此两条路径必然指向同一个库。
`--reset-db` 除 `drop_all` 外额外清掉 `alembic_version`（它不属于 ORM metadata，
`drop_all` 不认识它，留着会让下次升级变成 no-op）。

老库会被**明确识别**并给出两条出路，而不是抛一句原始 alembic 报错：

```
ERROR: this database was created before alembic owned the schema
       (its tables exist but there is no alembic_version row)
HINT : ...rebuild it: `python scripts/bootstrap_dev.py --skip-install --reset-db`.
       Otherwise, having confirmed the schema is current, adopt it with `alembic stamp head`.
```

诊断区分两种情形：**没有 `alembic_version` 表**（旧 `create_all` 建的库）与
**表存在但为空**（上次升级建了记账表、却在 stamp 之前就死了）。对操作者而言两者含义相同。
刻意**不**自动 stamp：只有操作者能判断那个 schema 是否真的等于 head，自动 stamp 会把真实漂移
悄悄掩盖过去——这与整个项目「宁可停下来问，也不要猜」的取向一致。

`tests/test_migrations.py` 用 AST 固化这条边界：断言 `bootstrap_dev.py` 中不存在
`create_all` 调用（AST 口径，因此解释性注释不会误伤），并断言它确实 shell out 到
`alembic upgrade head`；另有两条测试实跑 `_alembic_revision()`，验证"未接管"与
"已接管"两种数据库都能被正确判定。

### 本轮基线

| 指标 | 第二轮后 | 第三轮后 |
|---|---|---|
| 测试数 | 179 | **189** |
| 覆盖率 | 83% | **85%** |
| 完整性校验 | 未纳入 CI（且实际会失败） | **130/130，CI 强制** |
| 建库路径 | `create_all`，与迁移脱节 | **`alembic upgrade head`，与生产一致** |
| CI 步骤 | 5 | **7** |

### 仍未处理

第十节的「仍未处理（诚实清单）」**全部继续有效**，本轮未触碰。本轮新增的两项均已修复并有回归测试。

> 三轮复核的共同结论：这个项目真正的风险不在"功能没做完"，而在**"声称做了的事，实际没做到"**。
> 假通过的评测、伪迁移、算错口径的校验和、与迁移脱节的建库脚本，都属于同一类。
> 面试官最容易击穿的也正是这一类——所以每一处「保证」现在都有一个**能失败的测试**在后面。

