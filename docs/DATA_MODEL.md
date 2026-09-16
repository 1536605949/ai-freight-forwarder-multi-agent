# 核心数据模型

`Lead → Customer → Inquiry → ScheduleOption → RFQ → SupplierQuote → CustomerQuote → Approval → Booking → TrackingEvent → FollowUpTask`

另外：`AgentRun` 记录 Agent 执行，`AuditEvent` 记录高价值业务动作，`OutboxEvent` 为异步事件/可靠投递预留。
