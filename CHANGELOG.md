# Changelog

## 1.0.0-rc3 — 第三次复核：完整性制品与 schema 归属

以「交付物自己能不能验证自己」为标准再复核一轮，又发现两个问题。它们的共同点是
**声明与执行不一致**：文件声称保证某件事，实际并不保证。

### 修复（P0）

- **`SHA256SUMS.txt` 在全新 clone 上校验失败**：该校验和是用 Windows 工作区字节（CRLF）算的，
  而 `.gitattributes`（`* text=auto eol=lf`）在仓库里存的是 LF。结果是 11 个文件在
  `git clone` 之后 `sha256sum -c` 直接报错——**完整性制品保护不了它声称要保护的东西**。
  新增 `scripts/gen_checksums.py`：按「clone 真正拿到的字节」计算（二进制按 NUL 判定后原样，
  文本先归一化为 LF），两个文件都以 LF 写出，因此跨平台幂等。
  `tests/test_packaging_manifest.py` 用 `git cat-file blob HEAD:<path>` 作为基准做交叉校验，
  正是这个测试抓出了最初的 CRLF 缺陷。CI 新增 `sha256sum -c SHA256SUMS.txt` 与
  `gen_checksums.py --check` 两步。
- **`bootstrap_dev.py` 让 schema 有了两个真相来源**：它用 `Base.metadata.create_all()` 建库，
  会建出全部表但**不写 `alembic_version` 行**，于是本地库看似健康、下一次
  `alembic upgrade head`（生产路径）却死在 `table agent_runs already exists`。
  已改为执行 `alembic upgrade head`，与生产同一条代码路径；
  `--reset-db` 会连带清掉 `alembic_version` 再重建。
  老库会被明确识别（区分「无 `alembic_version` 表」与「表存在但为空」）并给出两条出路，
  刻意不自动 stamp——自动 stamp 会掩盖真实漂移。

### 修复（P1）

- `scripts/gen_checksums.py` 自身修掉一个构造性缺陷：`MANIFEST.txt` 不能从磁盘读取后计算哈希
  （本次运行正要重写它，读到的是旧内容），改为对**新内容**取哈希。
- 10 个文本文件的工作区行尾从 CRLF 归一化为 LF，与仓库内实际存储一致；
  内容零变化（`git diff` 对这些文件为空）。

### 基线变化

| 指标 | rc2 | rc3 |
|---|---|---|
| 测试数 | 179 | 189 |
| 覆盖率 | 83% | 85% |
| 完整性校验 | 未纳入 CI | 130/130，CI 强制 |
| 建库路径 | `create_all`（与迁移脱节） | `alembic upgrade head`（与生产一致） |
| CI 步骤 | 5 | 7 |

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
