# 主动获客与外联：规则、强制点、证据

> 这份文档存在的理由很具体：`docs/SECURITY.md` 通篇没有提到**获客与外联**，
> 而主动给陌生人发邮件是整个系统里唯一会对外产生副作用、且可能违法的动作。
> 一份没人读的安全声明没有价值；所以下面每一条规则都写明**在哪一行代码强制**、
> **哪个测试会红**。三列对不上的规则，等于没有这条规则。

---

## 一、四道门禁

发送一条冷邮件要同时穿过四道门。任何一道不过就发不出去，**提示词无法绕过**。

| # | 门禁 | 回答的问题 | 强制点 | 失败码 | 会红的测试 |
|---|---|---|---|---|---|
| 1 | 人工审批 | 我们**有没有资格**做这件事 | `services/prospecting.py::send_outreach` | `approval_required` | `test_outreach_requires_approval` |
| 2 | 合法依据 | 我们**允不允许**联系这个人 | 同上（`OUTREACH_ALLOWED_BASIS`） | `compliance_basis_required` | `test_outreach_requires_lawful_basis` |
| 3 | 评分下限 | 这条线索**值不值得**占用陌生人的收件箱 | 同上（`OUTREACH_MIN_SCORE`，默认 70） | `score_below_outreach_threshold` | `test_a_low_score_blocks_the_send_but_not_the_draft` |
| 4 | 频率上限 | 这个月是不是**已经打扰够了** | 同上（`_frequency_block`） | `frequency_cap_exceeded` | `test_frequency_cap` |

另有两条不在发送路径上、但同样由代码强制的规则：

| 规则 | 强制点 | 测试 |
|---|---|---|
| 退订立即生效，跨候选池 / Lead / 外联记录三处 | `mark_opt_out` | `test_opt_out_blocks_everything` |
| 第三方数据转正式客户**必须由人给合规结论**，模型不得自行断言同意 | `promote_to_lead` 的 `consent_status` 参数 | `test_promote_requires_human_consent_decision`、`test_promote_rejects_bad_consent_status` |

### 顺序为什么是 1 → 2 → 3 → 4

顺序不是随意的，它决定了操作者看到哪条错误：

- **1、2 是授权与合法性**，3、4 是**反滥发的运营策略**。把策略排在授权前面，会让
  「你还没审批」被报成「分数不够」，把人引到错误的修复路径上。
  `test_authorization_is_checked_before_quality` 守的就是这个顺序。
- **3 在 4 前面**：分数低是「这条不值得发」，额度用完是「这个月不能再发」。前者是选材问题，
  后者是节奏问题，先报选材问题。

### 门禁 3 为什么在「发送」而不在「起草」

起草不产生任何外部副作用，发送才会。所以低分线索**仍然可以生成草稿**——
运营可以先看看信写成什么样，再决定要不要重新搜一批线索。
把门禁放在起草，等于让一个可逆的动作承担不可逆动作的责任。

这一条有测试直接断言：`test_a_low_score_blocks_the_send_but_not_the_draft` 先确认草稿
`status=='draft'`，再确认发送被拒，最后确认被拒后记录**仍是 `approved` 而不是 `sent`**
——被拦下的发送不能把行标成已发。

---

## 二、数据可核验性：四级，不是一个布尔值

新增只读端点 `GET /api/v1/prospects/{id}/evidence`。它回答的不是「这条数据从哪来」
（那是 `source` / `source_ref` 的事），而是**「一个评审者现在能不能自己打开看」**。

| 等级 | 含义 | 人能自己核验吗 |
|---|---|---|
| `synthetic` | 演示用合成数据 | 否，任何链接都打不开 |
| `placeholder` | 有网址，但域名是保留域（`.example` / `.test` / `.invalid` / `.localhost`），永远解析不出来 | 否 |
| `traceable` | 有供应商记录号可回溯，但没有公开链接 | 否，需向供应商调档 |
| `openable` | 有可公开打开的链接 | 是 |

### 为什么要区分 `placeholder` 和 `openable`

Mock 数据源写的是 `https://www.<slug>.example`。**`.example` 是 RFC 2606 保留域，
永远不会解析**——它长得像一个链接，点开是空的。

如果代码只判断「website 字段非空 → 可核验」，这个字段就会在评审里被当成证据，
而实际上谁都没打开过。`_is_publicly_openable()` 存在的唯一目的就是拦住这种情况；
`test_a_reserved_domain_is_not_evidence` 直接对它做单元断言，不依赖 mock 源碰巧产出什么。

### 可核验性**不是**门禁

`prospect_evidence` 是只读投影，不参与放行判断。原因是中国大陆的报关 / 提单数据
不是公开数据，只能通过持牌数据商获得（见 `providers/prospecting.py` 的说明）。
把「可核验」做成门禁，会让真实部署里所有合法线索都发不出去。
门禁仍然是上面那四道，可核验性是**给人看的诚实标签**。

---

## 三、借鉴了什么，没借鉴什么

调研范围与结论见 `docs/GITHUB_LANDSCAPE.md`。这里只记与本模块直接相关的取舍。

| 借鉴点 | 来源 | 我们怎么落地 | 与来源的差别 |
|---|---|---|---|
| 数值评分下限挡住低质线索进入外联 | `yangshun2005/Codex-native-Lead-Hunter`（MIT，`fit_score >= 7`） | `OUTREACH_MIN_SCORE`，在发送时强制 | 对方是「进队列」的门槛；我们放在发送时，因为起草无害 |
| 每条线索有可核验的来源 | 同上（"evidence over guesses"） | `prospect_evidence` 四级 + 不可核验就明说 | 对方靠爬公开网页；我们的数据是持牌/合成，所以**主动声明不可核验**，而不是伪造一个能点开的链接 |
| 安全规则独立成文，与代码对齐 | 同上 `SAFETY.md`；`nithin42/Azure-ai-foundry-erp-swarm` 的 ADR + 威胁矩阵 | 本文档的三列表 | 对方文档与代码是两套；我们要求每一行都能指到测试 |
| 两阶段提交（草稿暂存 → 签字放行） | `Azure-ai-foundry-erp-swarm` | 已有实现（`draft → approved → sent`），本文档正式命名 | 我们多一道「合法依据」，对方没有这个概念 |

**明确没有抄的三件事：**

| 没抄 | 理由 |
|---|---|
| Langfuse / 第三方 LLM 追踪 | 我们已经自建 `/api/v1/metrics/agent`（p50/p95、token、成本、未上报单独计数）。为了追踪一个模块引入一整套外部依赖，收益低于成本。 |
| SSE 实时进度流 | 演示页轮询 + `/graph/status/{id}` 已够用。加 SSE 是为了好看，不是为了能力。 |
| 多维评分（`business_value` / `urgency` / `confidence` / `recommended_action`） | 需要加列、迁移、改 prompt 契约，而我们目前只有一个决策依赖分数：**要不要发**。一个维度足够回答一个问题；四个维度会让人以为我们在做优先级排序，其实没有。已记入遗留项。 |

---

## 四、诚实边界

- **提示词注入未处理**。`agents/runtime.py` 的字段解析对恶意输入没有防护。当前影响是
  「字段误解析」，不会越权（金额、权限、发送都由确定性代码控制），但这是一个已知缺口。
- **没有 OWASP LLM Top 10 威胁矩阵**。ERP swarm 那类项目有；我们没有，
  与其写一份没验证过的矩阵，不如在本文档里列出已知缺口。
- **频率上限是单库实现**。多副本部署时，两个实例可能各自看到「还没到上限」。
  与 `worker/scheduler.py` 的并发锁问题同源。
- **评分来自模型（mock 下是启发式）**。它是排序依据，不是资质判断；
  阈值是运营策略，不是模型输出——这句话写在 `config.py` 的注释里，因为它决定了
  「分数低」时该改配置还是该改 prompt。
- **`promote_to_lead` 只校验合规结论的取值合法**，不校验这个结论对不对。
  后者只能由人和法务负责，代码能做的是**不让模型替人做这个决定**。
