# Changelog

## 1.0.0-rc2 — 交付可信度复核

第二轮以「能否经得起面试级追问」为标准复核，修复 3 个 P0 并补齐回归测试。
逐项对照见 [`docs/ARCHITECTURE_REVIEW.md`](docs/ARCHITECTURE_REVIEW.md) 第十节。

### 修复（P0）

- **评测脚本假通过**：`evaluation/run_eval.py` 中 3 个 critical 用例（`hitl1` / `pricing1` /
  `tenant1`）从未真正执行却恒返回通过，使「critical eval 0 fail」上线门槛形同虚设。
  已改为真实端到端断言，并引入 `executed` 标记——**未执行的用例不算通过**，未知
  `kind` / `rule` 一律 FAIL。`tests/test_eval_harness.py` 用变异测试证明它真的会失败。
- **伪迁移**：`0001_initial` 使用 `create_all` / `drop_all`，同一 revision 在不同代码版本下
  产出不同 schema。已重写为显式 DDL 冻结快照（20 表 / 35 索引），
  `tests/test_migrations.py` 断言「升级后 schema == ORM 声明」「downgrade 不留残余」
  「升降级往返可复现」。
- **状态回退**：`PATCH /api/v1/inquiries/{id}` 会把已订舱（`booked`）的询价倒回 `ready`。
  新增 `app/state_machine.py`（显式迁移图 + 幂等且只进不退的 `advance_status()`），
  七处无条件状态写入改为经其校验；终态询价内容冻结（409 `inquiry_locked`）。

### 修复（P1 / P2）

- `main.py` 覆盖率 0% → 66%：新增 `tests/test_api_routes.py`（鉴权边界、跨租户 404、409 映射）。
- `worker/scheduler.py` 0% → 94%：轮询体抽为 `run_once()`，新增 `--once`（cron 场景）。
- 补全 `.env.example`（此前**完全不存在**，README 却让人复制它）；
  `tests/test_config_docs.py` 双向断言与 `config.py` 同步。
- `maf_workflows.py` 标注 `NOT WIRED` 并加可操作的 `ImportError`；AST 测试固化其孤儿状态。
- CI 增加 lint / tests / packaging / `alembic upgrade head` / smoke boot 五步。
- `pyproject.toml` 显式声明 ruff 规则集，全仓零告警（此前 CI 不跑 ruff，约 660 条告警淹没真实问题）。
- `_mock_parse` 补 ETD 解析——此前 ETD 永不解析，而它是必填项，导致经真实投递入口进入的
  询价**永远**停在 `needs_clarification`，流水线无法从入口走到闸门。
- 新增 `tests/test_architecture_asset.py`：AST 检测导入环、适配层反向依赖、HTTP 层反向依赖。
- 初始化 Git 仓库。

### 基线变化

| 指标 | rc1 | rc2 |
|---|---|---|
| 测试数 | 99 | 179 |
| 覆盖率 | 71% | 83% |
| 0% 覆盖模块 | 2 | 0 |
| 行为评测 | 5 用例（3 个假通过） | 6 用例（全部真实执行，且证明会失败） |

## 1.0.0-rc1

- 完成 Freight Forwarding 12-stage business lifecycle skeleton.
- 加入 Microsoft Agent Framework runtime adapter + deterministic mock runtime.
- 加入 Inquiry parsing、RFQ fan-out、quote optimization、HITL、Booking、Tracking、Follow-up。
- 加入 multi-tenant boundary、audit、AgentRun、Prometheus、Worker、Docker Compose、tests/eval。
- 将 Project 2 完整架构图加入 `docs/architecture/`。
- 加入主动获客链路（Prospecting + Enrichment + Cold Outreach，含三道合规门禁）。
- 加入 AIS 船位与主动延误预警。
- 加入统一编排（10 阶段 + 2 人工闸门）。
