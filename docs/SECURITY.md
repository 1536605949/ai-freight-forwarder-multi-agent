# Security & Safety

- JWT 生产认证；dev 模式仅用于本地。
- tenant_id 强制资源隔离。
- Webhook secret/signature 校验。
- 报价发送必须 `status=approved`。
- Booking 只能基于已发送报价。
- 价格由 rule engine 计算，不接受 Agent 自报金额。
- Lead/Follow-up 尊重 opt-in / do-not-contact 与适用法律、平台条款。
- Secrets 仅环境变量/Secret Manager；禁止进入 Git。
- AuditEvent 保存报价审批、发送、订舱、tracking 等关键动作。
- 真实外部 API 使用最小权限 service account + rotation。
