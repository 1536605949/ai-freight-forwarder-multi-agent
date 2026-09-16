# 从 RC 到真实商业上线还必须完成的工作

本仓库的“生产级”指：架构边界、状态机、安全门禁、测试、部署与 Adapter 形态达到生产工程标准；**并不代表已经拥有真实船公司商业数据授权**。

上线前必须替换/补齐：
- 真实 CRM OAuth/字段映射；
- 真实邮箱 inbound webhook 与线程关联；
- Carrier/DCSA-compatible Schedule endpoint；
- 合约运价/附加费/币种/税费规则；
- 供应商 master data 与 SLA/reliability；
- Booking sandbox/conformance；
- Track & Trace webhook/subscription；
- 价格币种换算和有效期；
- 法务确认开发信/退订/隐私合规；
- 审批矩阵（利润低于阈值需要高级 Reviewer）；
- 多租户 RBAC 与企业 SSO；
- 数据保留/删除政策；
- 灾备、RPO/RTO 与 SLO。

## 主动获客链路（Prospecting）相关

- **潜客数据源**：`prospecting_provider=mock` 为合成数据，仅用于演示。生产必须接入持牌贸易/提单数据商，并在 `data_origin` 记录合法来源；中国大陆海关提单数据不对普通企业开放。
- **合法处理依据**：`legitimate_interest_reviewed` 需由法务确认适用性与评估文档留存方式；`opt_in` 需有可举证的用户同意记录。
- **触达频次与退订**：当前频次上限为租户级简单计数。生产建议加入发信域信誉管理、ISP 投诉回路（FBL）、退订链接与邮件头 `List-Unsubscribe`。
- **企业信息公开范围**：需确认抓取的公开信息不落入个人信息保护法的敏感范畴，联系人信息建议仅保留企业公开的职务邮箱。
- **发信通道**：冷启动外发建议使用独立子域与独立 IP，避免影响交易类邮件的送达率。

## 船舶位置（AIS）相关

- **AIS 数据授权**：`vessel_provider=mock` 为合成轨迹。生产须签约正式 AIS 服务商（MarineTraffic / Spire 等），遵守其授权范围与调用频率限制，不得爬取。
- **轮询成本**：当前每次 refresh 都是一次实时查询。生产应按航次与在途阶段动态调整轮询频率（远洋段降频、临近目的港升频），并做多租户配额。
- **延误判定口径**：目前用单一的 `delay_hours` 阈值。真实业务建议结合船公司 ETA 更新、目的港拥堵指数与历史准班率做综合判定，并区分「预计延误」与「确认延误」。
- **预警触达**：延误预警已生成 `FollowUpTask`，但尚未接入真实的客户通知模板与渠道（邮件/短信/客户门户），需补齐。
