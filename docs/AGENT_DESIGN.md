# Multi-Agent Design

| Agent | 输入 | 输出 | 禁止事项 |
|---|---|---|---|
| Orchestrator | inbound message | intent + route | 不执行金额/审批 |
| **Prospecting Agent** | 航线 + 候选企业列表 | 达标企业 + fit_score + 理由 | 不虚构企业信息、不推测未提供的经营数据 |
| **Enrichment Agent** | 企业公开信息 | 决策人角色 + 画像 + 触达就绪度 | 不编造人名/电话/邮箱 |
| **Cold Outreach Agent** | 企业画像 + 航线 | 首次开发信正文 | 不承诺运价舱位、不使用绝对化用语、必须含退订 |
| Lead Agent | company/enrichment | score rationale/outreach suggestion | 不绕过 consent |
| Inquiry Parser | email/chat | structured inquiry | 不猜缺失字段 |
| Clarification Agent | missing fields | customer question | 不添加不存在要求 |
| Supplier RFQ Agent | complete inquiry | supplier email draft | 不承诺舱位 |
| Quote Comparison Agent | normalized supplier quotes | explanation | 不改变 Pricing Engine 数字 |
| Quotation Agent | approved price structure | customer quote draft | 不声称 booking completed |
| Follow-up Agent | CRM context | follow-up draft | 不对 DNC 联系 |
| Growth Agent | customer history | repeat-sales opportunity | 不做无许可 mass spam |

## 主动获客链路（Prospecting）

```
ProspectingSource (Adapter)  →  Prospecting Agent  →  ProspectCandidate
        ↓                                                    ↓
   Mock / 持牌数据商                              Enrichment Agent
                                                             ↓
                                                    Cold Outreach Agent
                                                             ↓
                                            审批 → 三道合规门禁 → 发送
                                                             ↓
                                                    Lead → 询价流程
```

**设计要点**：候选企业持久化在 `prospect_candidates`，与 `leads` / `customers` 物理隔离。
第三方未核实数据在通过富化与合规复核前，不会进入 CRM，也不会污染真实客户对象的审计链。

**合规门禁（代码级，非提示词）**，见 `app/services/prospecting.py`：
1. `send_outreach` 要求已记录人工批准（`outreach_requires_human_approval`）
2. 必须有合法处理依据（`opt_in` / `legitimate_interest_reviewed`），否则 `compliance_basis_required`
3. 触达频率上限（`outreach_max_per_window` / `outreach_window_days`）
4. `mark_opt_out` 立即在候选、Lead、未发出草稿三处生效

**数据来源合规**：中国大陆海关提单数据不对普通企业开放，仅可通过持牌数据商获取。真实部署须在 `data_origin` 记录合法来源。Mock 数据仅用于演示，标记为 `mock_synthetic`。

## 船舶位置与主动延误预警（Vessel Position）

与 Track & Trace 的区别：T&T 回答「这个货发生了什么事件」，船位回答「船现在在哪、还能不能按时到」。
前者是被动查询，后者用于**主动预警**——在客户自己发现之前先通知他。

```
VesselPositionProvider (AIS Adapter)
        ↓
VesselPositionSnapshot  ← 每次抓取落库，保留历史
        ↓
延误判定 (delay_hours >= vessel_delay_alert_hours)
        ↓
FollowUpTask(kind='vessel_delay') → 人工/Worker 联系客户
```

**设计要点**：
- **留历史才能算趋势**：单次查询只能说「现在在哪」；判断延误是否在扩大必须有历史快照（`vessel_positions`）。
- **一个订舱一个未关闭预警**：船晚点会持续被轮询到，因此按 `booking_id` 去重，避免每次轮询都生成一条任务。
- **没有订舱就不预警**：无订舱意味着没有可通知的客户，系统不会凭空猜一个。
- **尊重 DNC**：客户设置 `do_not_contact` 时不生成通知任务，计为 `suppressed`。

**字段对齐 AIS 数据商语义**：MMSI / IMO / SOG / COG / nav_status / next_port，便于真实 vendor payload 直接映射。
**数据授权**：AIS 为付费授权数据，生产须接正式服务商（MarineTraffic / Spire 等），不得爬取。

Microsoft Agent Framework 只负责 Agent Runtime/Orchestration 能力；业务状态仍以数据库为事实来源。每个 HTTP 请求创建独立 Agent 执行，不共享一个正在运行的 Workflow 实例。
