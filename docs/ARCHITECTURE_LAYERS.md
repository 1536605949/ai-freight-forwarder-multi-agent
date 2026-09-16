# 系统架构

本文档为投标/评审用架构说明，与 `docs/ARCHITECTURE.md`（业务飞轮视角）互补。
本文侧重**分层与责任边界**。

## 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│ 入口层                                                       │
│   Web 控制台（演示/操作）  REST API（JWT/角色）  Webhook     │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ 编排层                                                       │
│   Orchestrator 状态机（services/orchestrator.py）             │
│   10 个阶段串联，可逆步骤自动跑，遇闸门即停                     │
│   长流程持久化，不阻塞等待；Worker + Webhook 恢复             │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ Agent 层（只做理解 / 决策 / 生成）                            │
│   Prospecting    Enrichment    Cold Outreach                 │
│   Inquiry/Clarify   RFQ/Quotation   Followup/Growth          │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ 确定性服务层（金额 / 状态 / 权限 / 门禁）                     │
│   Pricing Engine   审批/状态机   合规门禁                     │
│   延误判定         幂等/频率限制  审计落库                    │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ 数据源适配层（全部可插拔）                                    │
│   潜客源(持牌数据商)  船期舱位(DCSA)  实时运价  船舶位置(AIS) │
└─────────────────────────────────────────────────────────────┘

                                    ┌──────────────────────┐
      贯穿所有层的治理能力 ──────────→│ 多租户隔离            │
                                    │ AuditEvent            │
                                    │ AgentRun 追踪         │
                                    │ Prometheus            │
                                    │ Outbox 事件           │
                                    │ PostgreSQL / SQLite   │
                                    └──────────────────────┘
```

## 两条贯穿性原则

**1. Agent 只做理解 / 决策 / 生成；金额、权限、状态迁移、审批、发送一律由确定性代码控制。**

这不是风格偏好，而是可审计性的前提。任何涉及钱和对外承诺的动作都必须落在可单测的纯函数或显式状态迁移上，模型不得参与。

**2. 长流程不阻塞。**

RFQ、HITL、Booking、Tracking、船位延误均以 DB 状态为准，由 Worker + Webhook 恢复推进。HTTP 请求不承担等待职责，因此不存在"Agent 挂着几小时等供应商回信"这种不可恢复状态。

## 编排层：排序，不授权

`services/orchestrator.py` 把 10 个阶段串成一条可一键执行的流水线，但它的权限被刻意压到最低：

| 阶段 | 类型 | 说明 |
|---|---|---|
| parse | 自动 | 校验询价完整性，缺字段即停并回问客户 |
| schedules | 自动 | 查船期（DCSA 适配层） |
| rates | 自动 | 查运价（合约/指数适配层） |
| rfq | 自动 | 向供应商发询价 |
| supplier_quotes | 自动 | 收集供应商报价 |
| optimize | 自动 | Pricing Engine 比价、加价、生成报价单草稿 |
| **hitl_review** | **闸门** | 人工审批，未批不得继续 |
| **quote_send** | **闸门** | 对外发报价，必须 `status='approved'` |
| booking | 自动 | 订舱，必须报价已发送 |
| vessel_tracking | 自动 | 船位刷新 + 主动延误预警 |

**为什么闸门是"停止"而不是"询问"**：编排层无法用参数说服门禁。`_stage_quote_send` 调用的仍是
`services/approval.py::send_quote`，那里写着 `if q.status!='approved': raise ValueError('approval_required')`。
即使编排层被改错，门禁依旧生效——这正是把门禁放在服务层而非编排层的原因。

`auto_approve` 仅为演示/测试存在，是 API 上的显式布尔参数，不读环境变量，因此不可能在生产配置中被误开。

`resume_after_approval` 与 `run_pipeline` 共用同一条代码路径（前者只是以 `start_stage='hitl_review'` 重入），
避免出现"继续"逻辑与"首次运行"逻辑两套实现各自漂移的经典缺陷。

## 硬门禁清单（代码级，提示词无法绕过）

| 门禁 | 位置 | 失败行为 |
|---|---|---|
| 报价发送必须已审批 | `services/approval.py` | `approval_required` |
| 订舱必须基于已发送报价 | `services/booking.py` | `customer_quote_must_be_sent_before_booking` |
| 冷启动外发必须有人工批准 | `services/prospecting.py` | `approval_required` |
| 冷启动外发必须有合法依据 | `services/prospecting.py` | `compliance_basis_required` |
| 触达频率上限 | `services/prospecting.py` | `frequency_cap_exceeded` |
| 退订即时生效 | `services/prospecting.py` | 候选/Lead/草稿三处同步抑制 |
| 延误预警按订舱去重 | `services/vessel.py` | `deduplicated` |
| 无订舱不生成客户预警 | `services/vessel.py` | 返回 `alert: null` |
| 编排不得越过审批闸门 | `services/orchestrator.py` | 停在 `hitl_review`，返回 `waiting_approval` |
| 未审批时 continue 不生效 | `services/orchestrator.py` | 停在 `hitl_review`，返回 `waiting_approval` |

## 数据源适配层

四类外部数据全部以 Adapter 形式接入，Mock 实现用于演示与测试，真实厂商实现为骨架，业务层代码不因数据源替换而改动。

| 能力 | 接口 | Mock | 生产接入 | 授权要求 |
|---|---|---|---|---|
| 潜客搜索 | `ProspectingSource` | 合成企业 | 持牌贸易/提单数据商 | 需商业合同 |
| 船期舱位 | `ScheduleProvider` | 合成船期 | DCSA 兼容端点 | 需船司/平台授权 |
| 实时运价 | `RateProvider` | 合成运价（确定性） | `ContractRateProvider` 合约价 / `IndexRateProvider` 运价指数 | 需合约账号 |
| 船舶位置 | `VesselPositionProvider` | 合成 AIS 轨迹 | AIS 服务商 | 需商业合同 |

**运价适配层的两条硬约束**（`app/providers/rates.py`）：

1. **Provider 只返回成本，不返回售价。** 卖价由 `PricingEngine` 统一加价，避免数据商隐式决定公司毛利。
2. **来源与有效期随数字同行。** `source` 区分合约价 / 指数价 / 演示合成价（`demo_` 前缀 + `synthetic=True`），
   `valid_until` + `is_expired()` 拦住过期价。报价单上的每个金额都能回答「从哪来、还有效吗」。

**合规声明**：中国大陆海关提单数据不对普通企业开放，仅可通过持牌数据商获取；AIS 为付费授权数据，不得爬取。所有外部来源在数据落库时记录于 `data_origin` / `source` 字段，供审计追溯。
