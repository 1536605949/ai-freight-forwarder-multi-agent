# 编排架构升级：从「调几个 Agent」到「可控制、可恢复、可审计」

> 目标架构来自评审意见（LangGraph Orchestrator + 四个 Agent + Harness + HITL + 确定性服务）。
> 本文只做一件事：把目标架构的每一格，对到仓库里的**具体文件和具体断言**上，
> 并明确写出**这一轮没做什么、为什么**。
> 文末所有数字都是在本机起服务实测得到的，不是设计目标。

## 一、对照表

| 目标架构 | 现状 | 落地位置 | 守它的测试 |
|---|---|---|---|
| LangGraph Orchestrator | ✅ 已有，本轮补齐 Checkpoint | `app/graph/builder.py`（11 节点）、`app/graph/acquisition.py`（3 节点） | `tests/test_graph_pipeline.py` |
| State / Node / Router | ✅ `PipelineState` + 条件边 + 条件入口点 | 同上 | 同上 |
| **Checkpointer** | ✅ **本轮新增**，SQL 持久化 | `app/graph/checkpoint.py` | `tests/test_graph_checkpoint.py` |
| 获客 Agent | ✅ **本轮新增**为图 | `app/graph/acquisition.py` | `tests/test_acquisition_graph.py` |
| 询价 Agent | ✅ 已有节点，本轮补身份 | `rfq` / `supplier_quotes` | `tests/test_agent_registry.py` |
| 报价 Agent | ✅ 已有节点，本轮补身份 | `optimize` / `hitl_review` / `quote_send` | 同上 |
| 跟踪 Agent | ✅ 已有节点，本轮补身份 | `vessel_tracking` | 同上 |
| **Harness 治理层** | ✅ **本轮新增**（此前只有一个 `read_only` 布尔） | `app/harness/` | `tests/test_harness.py` |
| · 工具白名单 | ✅ 动作表即白名单，未声明即拒绝 | `app/harness/actions.py` | `test_an_undeclared_action_is_denied_not_allowed` |
| · Pydantic | ✅ 请求体/参数校验 | `app/schemas.py` + 新增 `requestBody` 守卫 | `test_every_post_route_that_takes_a_model_declares_a_request_body` |
| · 权限校验 | ✅ Agent 归属 + 角色 + 租户 | `app/harness/guard.py` | `test_a_missing_role_is_reported_as_a_role_problem...` |
| · 风险分级 | ✅ 四级有序量表 | `app/harness/risk.py` | `test_a_commitment_action_cannot_be_declared_approval_free` |
| Human-in-the-loop | ✅ 闸门节点 + 从停机点续跑 | `hitl_review` + `set_conditional_entry_point` | `test_resume_after_approval_sends_and_books` |
| · 中断 | ✅ 四种停机状态 | `HALT_STATUSES` | `test_graph_stops_at_the_human_gate` |
| · 恢复 | ✅ 续跑点由**持久化状态**推导 | `runner.resume_graph` | `test_resume_derives_the_node_from_persisted_state` |
| · 审计 | ✅ 允许与拒绝都落 `audit_events` | `harness/guard.py::_record` | `test_a_refusal_survives_the_rollback_that_follows_it` |
| 确定性业务服务 | ✅ 金额/状态/权限/事务都在服务层 | `app/services/` | 全量测试 |

## 二、这一轮真正改了什么

### 1. Harness：把「Agent 能做什么」从注释变成表

升级前，一个工具能不能被模型调用，取决于 `ToolSpec.read_only` 这一个布尔。
它无法区分「写一条草稿」和「把价格发给客户」——而这两件事的风险差了两个数量级。

现在每个副作用都要先**声明**：

```python
ActionSpec('quote.send', 'quoting', Risk.EXTERNAL, '向客户发送报价单', requires_approval=True)
ActionSpec('booking.create', 'quoting', Risk.COMMITMENT, '向船公司订舱（不可逆承诺）')
```

四级量表按**业务后果**分，不按「有没有发出网络请求」分：

| 级别 | 含义 | 例子 |
|---|---|---|
| `READ` | 不改状态，可重试 | 刷新船舶位置 |
| `DRAFT` | 写内部状态，人还能丢弃 | 记供应商回盘、算价 |
| `EXTERNAL` | 外部方已知道，删行也收不回 | 发 RFQ、发报价、发开发信 |
| `COMMITMENT` | 钱或舱位已承诺，改行撤不回 | 订舱 |

判定顺序是契约的一部分，**每一步是下一步的前提**：

```
未声明 → 拒绝
不是归属 Agent → 拒绝
角色不足 → 拒绝
未审批 → 需审批（不是放行）
```

顺序为什么重要：告诉运维「需要审批」，他会去找审批人；如果真正的问题是「你不是复核人」，
他回来还是被拒，且没有任何新信息。所以 `test_a_missing_role_is_reported_as_a_role_problem_not_as_an_approval_problem`
断言的不是「被拒」，而是**拒绝的理由是角色而不是审批**。

### 2. 四个 Agent 有了身份，trace 能回答「谁在行动」

升级前 trace 只有节点名：`rfq` 跑了。现在每一步都带 `agent`、`action`、`risk`：

```
understand       orchestrator  -                   -
schedules        sourcing      -                   -
rates            sourcing      -                   -
rfq              sourcing      supplier.rfq.send   对外发送
supplier_quotes  sourcing      -                   -
optimize         quoting       quote.price        内部草稿
hitl_review      quoting      -                   -
```

两个不变量由测试守着，不是由文档保证：

- 每个图节点**恰好**一个归属 Agent（两个图一起查）；
- 每个 Agent 的 `max_risk` 覆盖它拥有的全部动作——所以「跟踪 Agent 只读」是代码性质。

编排器（`orchestrator`）只拥有 `READ`：它能让人去查，不能让人去承诺。这是编排器和超级用户的分界。

### 3. 获客 Agent 进了编排，但**不能发信**

获客发生在询价**之前**，而 `PipelineGraphRun.inquiry_id` 是非空外键。硬塞进报价图，
要么指向一个不存在的询价，要么把一列改成可空、让所有既有查询开始处理它从未处理过的 `null`。

所以是**两张图、一套 Harness、一张策略表**：

```
collect → score → qualify → 停机
```

`qualify` 只产出候选，不发信。发信是 `prospect.outreach.send`：`EXTERNAL` + 需复核人角色 + 需审批，
且只能走 `services/prospecting.send_outreach` 自己的四道闸门。
**一张图如果能同时找到陌生人并给他发邮件，那正是这层要防的事。**

无人可触达时，停机理由是 `no_reachable_prospect`，并附**缺口分解**：

```json
{"status": "no_reachable_prospect", "reachable": 0,
 "gaps": {"no_email": 50, "below_floor": 0, "suppressed": 0}}
```

`找不到人` 和 `数据源坏了` 必须长得不一样。一个静默的 0 分不清这两件事。

### 4. Checkpointer：从「自建游标」到真正的状态快照

`pipeline_graph_runs` 回答「停在哪」，它是图返回后写的一次性摘要。
它回答不了「停机那一刻状态长什么样」，因为它根本不是状态。

`app/graph/checkpoint.py` 实现了 `BaseCheckpointSaver`，把每个 super-step 的 channel 值写进
`graph_checkpoints`。两者不竞争：一个是业务记录，一个是执行记录。中间崩了，摘要没了、快照还在
——这是对的顺序，快照才是证据。

三个实现细节值得记下来：

- **不用 `asyncio.to_thread`**。`BaseCheckpointSaver.aput` 默认抛 `NotImplementedError`，
  顺手用 `to_thread` 会把 SQLAlchemy `Session` 搬到别的线程，SQLAlchemy 不支持。异步方法全部内联。
- **`metadata` 不能叫 `metadata`**。它是 SQLAlchemy 声明式基类的属性，遮蔽它会炸掉整个 mapper，
  所以列名是 `metadata_blob`。
- **`pending_writes` 必须是三元组**。返回 `(channel, value)` 类型检查过得去，然后在**渲染历史时**
  才报 `not enough values to unpack (expected 3, got 2)`——那时运行早已被报告为成功。

实测（`GET /api/v1/graph/runs/{run_id}/checkpoints`）：

```
checkpoints 9
  step= -1 input  status=None               nodes=0
  step=  0 loop   status=running            nodes=0
  step=  1 loop   status=running            nodes=1
  ...
  step=  7 loop   status=waiting_approval   nodes=7
```

## 三、治理层实测：被拒绝长什么样

直接命令图从 `quote_send` 起步，绕开 `hitl_review`：

```bash
curl -X POST localhost:8100/api/v1/graph/run \
  -H 'content-type: application/json' \
  -d '{"inquiry_id":"inq_...","start_node":"quote_send"}'
```

```json
{
  "status": "failed",
  "stop_reason": "quote_send:needs_approval: approval_required",
  "steps": [{
    "node": "quote_send", "agent": "quoting", "risk": "对外发送",
    "harness": {"outcome": "needs_approval", "action": "quote.send",
                "agent": "quoting", "risk": 2, "reason": "approval_required"}
  }]
}
```

拒绝**在服务被调用之前**发生，且拒绝本身单独 commit——否则调用方一抛异常、事务一回滚，
最该被审计的那条记录就消失了。

## 四、刻意没做的

写在这里是为了它们不被当成「已完成」：

1. **没有用 `interrupt()` / `Command(resume=...)`**。LangGraph 的原生中断需要长期存活的
   checkpointer 线程，而本项目的续跑单位是**节点边界**，由持久化状态推导续跑点。
   两者都能叫 HITL；换过来会推翻 13 个既有断言的语义，收益是「更 LangGraph 原生」。
   如果将来要做**节点中途**暂停，这是唯一正确的改法，届时必须连带重写 `test_graph_pipeline.py`。
2. **`prospect.outreach.send` 的 reviewer 角色只在 guard 层生效**，HTTP 层仍用 `require_role`
   把门。两处并存是刻意的纵深，但没有合并成一条——合并需要给 `send_outreach` 加 `roles` 参数，
   而它的签名被 9 个既有测试按位置调用。
3. **`ToolRegistry.invoke` 还没接 Harness**。聊天工具循环里的 5 个工具全是只读，
   接上去只多一条审计、不多一道门。等出现第一个写工具时必须接，否则白名单就有了一个洞。
4. **checkpoint 没有保留策略**。每节点一行，长流程会累积。生产上需要按
   `created_at` 清理或按 `run_id` 归档。

## 五、怎么自己验证

```bash
# 1. 治理表：谁能做什么，什么风险，要不要审批
curl -s localhost:8100/api/v1/harness/policy | python -m json.tool

# 2. 获客图：无人可触达时会说原因
curl -s -X POST localhost:8100/api/v1/graph/prospect \
  -H 'content-type: application/json' -d '{"sources":"mock|verify|"}'

# 3. 状态回放：每个节点的快照
curl -s localhost:8100/api/v1/graph/runs/<run_id>/checkpoints | python -m json.tool

# 4. 测试
pytest tests/test_harness.py tests/test_agent_registry.py \
       tests/test_graph_checkpoint.py tests/test_acquisition_graph.py -q
```

## 六、为什么这套东西值得展示

「调几个 Agent」不难，一周能搭出来。难的是回答这四个问题，且答案能被测试推翻：

- 模型想做的事，系统凭什么**允许**？（声明式动作表 + 四级风险）
- 不允许时，**谁**拒绝、**为什么**、证据在哪？（guard + 决策对象 + 拒绝也落审计）
- 一个跑了两小时的流程，**部署后**还能不能接着跑？（持久化 checkpointer + 由状态推导续跑点）
- 换一个行业，是改配置还是改代码？（`IndustryProfile` 的坑位已留，见 `ACQUISITION_ROADMAP.md`）

这四件事才是「Agent 进入真实业务系统」和「Demo 里调几个 Agent」的差别。
