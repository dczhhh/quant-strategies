# 美股现金账户约束层（Issue #3）

`quant_constraints` 是可选扩展，检查外部提供的订单和目标配置，不生成选股、入场、加仓或财报后买入信号。未启用时，原有 `Broker` / `Engine` 行为保持不变。

## 接入

```python
from quant_constraints import (
    ConstraintConfig, ConstraintController, cash_backtest_config, constrained_engine,
)

preferences = ConstraintConfig.from_yaml("config/us_cash_concentrated.yaml")
controller = ConstraintController(preferences, earnings_provider)
engine = constrained_engine(
    feed, external_strategy, controller, context_provider,
    cash_backtest_config(initial_cash=100_000),
)
result = engine.run()
audit = controller.audit_records()
statistics = controller.event_statistics()
```

`earnings_provider` 实现 `EarningsProvider.snapshot(asset, asof)`，返回 `EarningsCoverage` 和 `EarningsEvent`。`context_provider(asof, asset, phase)` 返回 `MarketContext`，必须使用同一个带时区的 `asof`。生产数据需提供可信的证券类型、行业、历史财报日程版本和行情可用时间。测试中的人工覆盖声明不是生产数据。

纯检查接口为 `controller.check(intent, state, market_context)`，返回 `ALLOW / REJECT / DEFER / RESIZE`、稳定原因码及数据。`submit_intent()` 接收外部意图；也支持原有 `submit_order()`、`update_order()`、`order_target_percent()`、`rebalance_to_weights()`。目标接口表示外部调仓请求，受调仓日期与 3 个百分点死区约束；普通减仓使用 `Kind.REDUCE`，风控使用 `Kind.RISK`。完整目标分配还通过 `check_targets()` 检查 4–6 个标的、单票范围、行业和股票预算。逐步建仓可以暂时少于 4 个持仓，不会为凑齐数量自动下单。

## 账户规则与策略偏好

| 分类 | 行为 |
|---|---|
| 账户硬限制 | 只做多、禁止融资和负现金；只使用已结算资金；未结算卖出款和其他挂单资金不能重复使用 |
| 结算 | `historical` 按交易日期选择 T+3（1995-06-07 起）、T+2（2017-09-05 起）、T+1（2024-05-28 起）；可覆盖为 `T+1` / `T+2` |
| 现金偏好 | 买入后保留权益的 10% 已结算现金，包含费用与挂单；卖出不受现金储备限制，但费用仍不能导致负现金 |
| 持有期偏好 | 默认 0 个交易日；已用结算资金付清的股票允许当天卖出。可配置最少持有交易日，风险卖出豁免 |
| 证券范围 | 要求显式 `equity` 或 `plain_sector_etf` 元数据；未知、大盘、反向、杠杆和衍生 ETF 类型拒绝。元数据真实性由数据提供方负责 |

结算日使用基座的 SIFMA 美国工作日并排除 Good Friday，加上 `BacktestConfig.settlement_holidays` 的额外官方关闭日期。结算与交易日历不同：Columbus Day / Veterans Day 可以交易却不结算。不能简单按收到的 bar 数结算。

## 时段与真实成交

使用 NYSE 实际日历和 `America/New_York`，处理夏令时、休市及提前收盘。RTH 为 `[开盘, 收盘)`；常规买入窗口为 `[10:00, 15:30)`，13:00 收盘的半日市为 `[10:00, 12:30)`。减仓与风险清仓可以使用整个 RTH。普通盘外提交拒绝；盘外风险请求记录 `DEFER` 并等待下一次合法行情。已存在的盘外挂单也不能成交。

盘外风险触发价不作为下一时段成交保证。延后风险订单在下一合法观测点使用该 bar 的实际开盘价，再应用基座的滑点、费用和成交量限制；若没有该行情则继续等待。数据缺口意味着可能错过理想清仓时间，不会补造历史成交。保留原风控原因。

默认用 `NEXT_BAR` / `OPEN`，`exit_first` 成交顺序，风险卖出先于普通卖出和买入。原有 StopLoss、TakeProfit、TrailingStop、TimeExit 和 RiskManager / MaxDrawdownLimit 可继续使用其自身参数和动作；扩展不设置止损止盈百分比。

仅接受带时区的 1/5/15/30 分钟或明确时间的 irregular 事件；日线和小时线明确报错。日线 OHLC 无法证明 10:00 / 15:30 的执行顺序。需要正确的 FeedSpec 时间戳语义，实际风险 deadline 前需要有可交易的数据事件。不要用 `enforce_sessions=True` 丢弃用于监测盘外风险的观测。

## 财报的时间边界

事件包含证券、事件 ID、带时区公告时间、BMO/AMC/DURING/UNKNOWN、信息实际可用时间、来源、缺失与取消标记。同一 ID 的修订按 `available_at` 生效，只读取当时可见的版本。未来公告日期可以作为已公开日程使用，未来才取得的日程不能回填历史。

`EarningsCoverage` 必须在当时可见并覆盖至少前瞻 2 个交易日，含该日结束。空事件列表仅在有可信覆盖声明时代表“未发现财报”；缺失日历对新增买入关闭准入。普通减仓和风险卖出不受财报买入限制。

- 前 2 个交易日从首日开盘起，直到公告前，禁止新建和加仓。AMC 包含公告当天的 RTH。
- 默认 `hold_through_earnings: false`。BMO 在前一交易日收盘前 30 分钟请求清仓，AMC 在公告当天收盘前 30 分钟请求清仓。DURING/UNKNOWN 按前一交易日处理；非交易日公告在前一合法交易日清仓。缺失覆盖对现有仓位产生保守清仓请求。
- 若公告信息到达时已错过窗口，在当前/下一合法观测点处理并留下审计记录；不伪造前一天或盘后的平仓。
- 公告后第一个受影响 RTH 的前 60 分钟禁止外部买入；随后默认持续 1 个交易日检查相对价差 ≤0.15%、同一时刻历史 RVOL ≥1.8、正成交量及流动性实际可用时间。缺字段、未来字段或不满足阈值均拒绝。
- 此事件期的新目标上限为通常单票上限的 0.5 倍（默认 12.5%）。它只是外部买入许可，不生成买单。`event_statistics()` 单独报告财报准入触发次数和比率。

## 仓位、调仓与风控

默认新建/显式目标为 10%–25%，股票目标 90%，现金 10%，行业上限 40%，建议完整分配 4–6 只。普通持仓跌到 10% 以下不会自动补仓。未知行业默认拒绝，`warn` 会留下“未验证行业”的允许记录，不宣称通过行业校验。挂单按已承诺买入和行业计入，不预支待卖股票的现金。

超过 25% 的价格漂移会阻止加仓并记录。默认请求在下一交易日合法行情减仓；可改为调仓日减仓或仅监测。请求数量按观测时权益计算，因此价格跳变或部分成交可能继续漂移；没有“瞬间保证权重”的假设。

每 N 个交易日默认为 N=10，以第一次外部提交的交易日为锚点（第 0 天允许），也可用每月首/末交易日。日期仅许可外部调整，零交易合法。变动小于 3 个百分点忽略。每周按纽约时间 ISO 周计数，最多接受 4 次新建仓订单，未成交订单立即占用额度；取消仍保守计入该周，成交与修改不重复计数。加仓、普通减仓和风险卖出不消费新建额度。

可选 `market_gates` 默认关闭。启用后需要当时可见的 VIX：20–30 将单票上限乘 0.5，≥30 或账户回撤 ≥10% 禁止新增买入。回撤 ≥15% 仅使用显式 `drawdown_reduction_fraction` 生成预定义减仓请求；未配置时记录 `drawdown_reduction_plan_missing`，不会声称已执行。减仓计划每个持续回撤区间只请求一次，避免每个 bar 递归减半。

所有提交/修改和最终成交分别检查。提交可 `RESIZE`；实际价格/费用使计划越界时拒绝成交并释放预约资金，不偷偷更改外部目标。结构化审计包含时间、资产、订单 ID、种类、阶段、可用结算现金、所需资金和原因。常见码包括 `outside_rth`、`entry_window`、`settled_cash`、`cash_reserve`、`short_sale`、`earnings_blackout`、`earnings_liquidity_missing`、`overweight_drift`、`industry_missing`、`rebalance_schedule`、`weight_deadband` 和 `weekly_entries`。

## 验证与数据依赖

```bash
uv run pytest tests/constraints -q --no-cov
uv run pytest -q -m 'not benchmark and not upstream_evidence' --cov-report=term
uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check
uv run pre-commit run --all-files
```

未接入真实财报、证券分类、同时间 RVOL、报价或 VIX 服务，也没有实盘连接。上游冻结的 `upstream_evidence` 和性能 benchmark 是原始源码的证据，不是本分支的再认证；其原始测试和报告保持可显式运行，日常回归分别排除它们。
