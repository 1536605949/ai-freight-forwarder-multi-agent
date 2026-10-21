# GitHub 同类项目调研：结论与可借鉴清单

> 检索日期 2026-10-21。方法：`gh search repos` 按关键词扫仓库描述，再读命中仓库的 README；
> 补充一次 WebSearch 做交叉验证。**方法有明确局限，写在最后一节**，请连同结论一起读。

---

## 一、结论（先说最重要的三条）

1. **「货代 + AI Agent」在 GitHub 上基本是空白。** `freight forwarder` 命中的 8 个仓库里，
   最高星的是一个**分发 Docker 镜像**的工具（9★），其余是 CRUD 管理后台或营销页；
   `logistics agent langgraph`、`shipping agent llm cargo`、`customs clearance ai` 命中为 0。
   没有可以对照的参考实现，也就没有「抄一个改改」的路。
2. **最接近的参考在获客侧，不在货代侧。** `yangshun2005/Codex-native-Lead-Hunter`（MIT）
   做的事情与我们的 `主动获客` 模块高度重叠：LangGraph 流水线、发送前人工确认、
   每条线索带来源、按分数决定是否进入外联。它是唯一值得逐条对照的仓库。
3. **架构模式类的命中全是低星衍生品，不值得抄。** `agent approval gate` 最高 6★ 且多个仓库
   同名（像是同一模板被反复 fork）；`llm observability tracing`、`langgraph production`
   的命中几乎全是 0–1★。这一类唯一有内容的是 `Azure-ai-foundry-erp-swarm`（55★）。

**对本项目的意义**：差异化是真实的，但**不能靠「别人没做过」来证明自己做得好**。
空白只说明没人公开做过，不说明我们做对了。所以下面的动作是「借鉴能借鉴的」，
而不是「宣布没有竞品」。

---

## 二、检索记录

| 检索词 | 命中 | 最高星 | 判断 |
|---|---|---|---|
| `freight forwarder` | 8 | 15（监控面板） | 无 AI 项目 |
| `logistics agent langgraph` | 0 | — | — |
| `shipping agent llm cargo` | 0 | — | — |
| `customs clearance ai` | 0 | — | — |
| `tms llm` | 2 | 0 | 无关（同名缩写） |
| `supply chain multi-agent` | 5 | 55 | **值得看 1 个** |
| `ai lead generation agent` | 8 | 60 | **值得看 1 个** |
| `agent approval gate` | 6 | 6 | 同名衍生，不值得 |
| `llm observability tracing` | 6 | 1 | 不值得 |
| `langgraph production` | 6 | 1 | 不值得 |

WebSearch 交叉验证（`open source AI agent freight forwarding quotation automation`）：
返回的全是通用 Agent 平台（Suna、Open Flow、Copilot cloud agent），没有一个货代业务项目。

---

## 三、值得看的两个仓库

### 3.1 `yangshun2005/Codex-native-Lead-Hunter` — 获客流程的参照

MIT，LangGraph + FastAPI + React，DeepSeek-first，3★。它的自我定位是
「开源、本地优先、发送前必须人工确认的自动获客 Agent」。

值得借鉴的四点：

| 它的做法 | 我们的现状 | 结论 |
|---|---|---|
| `fit_score >= 7` 才进入外联队列，用数值门槛把外联量压小 | 有 `score`，只用来排序和决定 Lead 初始状态，**没有门禁** | **已借鉴** → `OUTREACH_MIN_SCORE` |
| 每条线索带可打开的来源 URL（"evidence over guesses"） | 有 `source` / `source_ref` / `raw_payload`，但没暴露成「能不能核验」的判断 | **已借鉴** → `/prospects/{id}/evidence` 四级 |
| `SAFETY.md` 独立成文，明确「任何渠道都不自动发送」 | `docs/SECURITY.md` **完全没提获客与外联** | **已借鉴** → `docs/OUTREACH_COMPLIANCE.md` |
| 失败返回 `manual_required` / `needs_input`，不静默 | 我们抛带码的 `ValueError`，语义相同 | 不抄（已等价） |

**没抄**：Langfuse 成本追踪（我们有 `/metrics/agent`）、SSE 实时进度（轮询够用）、
多维评分字段（见 `OUTREACH_COMPLIANCE.md` 的「没借鉴什么」）。

一处**刻意做得不一样**：它靠爬公开网页，所以能给出「可打开的证据」；
我们的数据是持牌数据或合成数据，**给不出公开链接**。于是我们不做「假装能核验」，
而是把「不可核验」明确标出来（`synthetic` / `placeholder` / `traceable` / `openable`）。
在一份要经得起面试追问的作品里，**承认不能核验比伪造一个 404 链接更值钱**。

### 3.2 `nithin42/Azure-ai-foundry-erp-swarm` — 企业系统写操作的安全模式

55★，SAP S/4HANA + ServiceNow 的多智能体，核心卖点是
**「Two-Phase Commit Human Approval Gate」：草稿采购单暂存，直到 Teams 里签字才落库**。

值得借鉴的三点：

| 它的做法 | 我们的对应物 | 结论 |
|---|---|---|
| 两阶段提交：草稿暂存 → 签字 → 提交 | 已经是这个形状：`draft → approved → sent`，报价是 `pending → approved → sent` | **只是没有命名**。已在文档里正式称为两阶段提交 |
| ADR（架构决策记录）目录 | 有 `docs/ARCHITECTURE_REVIEW.md`，但没有 ADR 格式 | 不抄（审查文档已覆盖「为什么这么选」） |
| OWASP LLM Top 10 威胁矩阵 | 无；提示词注入是已知未处理项 | **不抄**，改为在文档里列已知缺口——没验证过的威胁矩阵只是装饰 |

---

## 四、技能（Skill）：没有可直接用的

- 技能市场检索接口本次返回 **HTTP 404**，无法检索可安装技能。
- 本地已装技能里没有对口的：`github-ai-trends` 是泛 AI 榜单（按星数排序，
  与「找同类项目」不是一回事，本次没有用它，而是直接用 `gh search` 做定向检索）；
  `find-skills` 是元工具；`marketing-skills` 下有一批文案类技能
  （`email-sequence`、`copy-editing`），能打磨开发信措辞，但**不解决工程问题**，
  与本次要补的门禁和证据链无关。

结论：这一类工作靠**定向检索 + 读源码**，没有现成技能可套。

---

## 五、方法局限（必须一起读）

1. **`gh search repos` 的多词查询是 AND 匹配。** 命中为 0 只说明「仓库描述里没有同时出现
   这些词」，**不等于「不存在这样的项目」**。所以「空白」这个结论的强度是
   「没有容易被搜到的公开实现」，不是「绝对没有」。
2. **只看仓库描述和 README，没有读源码。** 星数低、README 写得漂亮但实现是壳的项目
   也会被算进「值得看」。本次只对两个仓库做了 README 级核对，没有做代码级审计。
3. **星数不等于质量。** 55★ 的 ERP swarm 之所以值得看，是因为它把「两阶段提交」讲清楚了，
   不是因为 55★。反过来，0★ 也可能有好东西——只是无法在有限时间里逐个验证。
4. **中文生态没有覆盖。** Gitee、掘金、公众号里的货代 AI 项目没有检索。
   如果目标是国内市场，这一块需要单独补一轮。

---

## 六、下一步（如果要继续挖）

| 方向 | 怎么做 | 预期收获 |
|---|---|---|
| 补中文生态 | 检索 Gitee / 掘金「货代 大模型」「订舱 自动化」 | 可能找到真实业务方的实践 |
| 读源码而非 README | 对 Lead-Hunter 的 `Evaluate` 节点做代码级对照 | 看它的评分提示词怎么防幻觉 |
| 商业产品反推 | CargoWise、Raft、Vizion 的产品页与公开 API 文档 | 知道真实买家的验收标准 |
