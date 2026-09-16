# 完整架构

![Project 2 架构图](architecture/project2_architecture.png)

## 业务飞轮

```mermaid
flowchart LR
 A[Lead Intelligence] --> B[CRM]
 B --> C[Inquiry]
 C --> D[Schedule Intelligence]
 D --> E[Rate Intelligence]
 E --> F[Supplier RFQ]
 F --> G[Quote Optimization]
 G --> H{HITL}
 H -->|Approve| I[Booking]
 H -->|Reject| G
 I --> J[Tracking]
 J --> K[Follow-up]
 K --> L[Repeat Sales]
 L --> B
```

## 技术分层

```mermaid
flowchart TB
 U[邮件 / Web / CRM / Lead Sources] --> API[FastAPI API & Auth]
 API --> O[Orchestrator / State Machine]
 O --> A1[Inquiry Parser Agent]
 O --> A2[Supplier RFQ Agent]
 O --> A3[Quote Agent]
 O --> A4[Follow-up/Growth Agents]
 O --> S[Deterministic Services]
 S --> DB[(PostgreSQL)]
 S --> SCH[Schedule Provider]
 S --> RATE[Rate / Supplier Quotes]
 S --> MAIL[Email Provider]
 S --> BKG[Booking Provider]
 S --> TNT[Tracking Provider]
 H[Human Reviewer] --> API
 API --> OBS[Metrics / Audit / Agent Runs]
 W[Worker/Scheduler] --> DB
 W --> MAIL
```

### 为什么不是“所有模块都是 Agent”
- 价格公式、利润底线、状态机、权限、HITL、幂等：**确定性代码**。
- 解析非结构化消息、生成反问、写 RFQ、解释比价、生成报价文案、跟进文案：**Agent**。
- 船期/Track&Trace/Booking：**外部 API Provider**。
- 长时间等待：**DB 状态 + Worker + Webhook**。
