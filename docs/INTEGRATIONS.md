# 外部集成

## Schedule Intelligence
- 开发：`MockScheduleProvider`
- 生产：`DCSAScheduleProvider` 或 Carrier/Aggregator Adapter
- 统一内部字段：carrier/service/vessel/voyage/origin/destination/ETD/ETA/transit/direct/co2e/source

## Rate Intelligence
- 开发：`MockRateProvider`（确定性合成运价，按航线+箱型+ETD 生成，标注 `synthetic=True`）
- 生产：`ContractRateProvider`（合约价/承运人 API）与 `IndexRateProvider`（付费即期指数）
- 统一内部字段：`base_rate` / `surcharges` 为两个规范金额字段，其余（`surcharge_breakdown` /
  `transit_days` / `free_days` / `min_volume` / `valid_until`）在适配层归一化后归入 `payload`

生产建议数据源优先级：
1. 自有合约价/历史成本数据库
2. Carrier pricing API/平台 API
3. Supplier RFQ 实时报价
4. 人工录入特殊价

**Rate/舱位不能由 LLM 猜测。**

两条硬约束：
- **运价是成本输入，不是售价。** Provider 只返回成本；加价、最低毛利、卖价一律由
  `PricingEngine` 决定。任何 Provider 直接返回卖价，都等于让数据商替公司定毛利。
- **每笔运价必须带来源（`source`）与有效期（`valid_until`）。** 合约价、指数价、演示合成价
  不可互换 —— 向客户报价时必须能追溯到「这个数字从哪来」。演示价以 `demo_` 前缀显式区分，
  不可能被误认为真实合约价。过期运价经 `is_expired()` 判定，不得直接用于生成报价单。

配置：`RATE_PROVIDER=mock|contract|index`，配套 `RATE_CONTRACT_BASE_URL/TOKEN`、
`RATE_INDEX_BASE_URL/TOKEN`。真实 Provider 在未配置时**启动即报错**，不会静默返回空列表。

## Booking
`DCSABookingProvider` 只作为通用标准 Adapter 示例；每家 carrier 的 endpoint、OAuth、字段映射必须在 sandbox 验证。

## Tracking
DCSA T&T / carrier webhook → normalized TrackingEvent → customer notification/exceptions.

## Email
开发可用 Mock 或 MailHog SMTP。生产建议 Microsoft Graph/Gmail API/OAuth，不要长期使用裸账号密码 SMTP。
