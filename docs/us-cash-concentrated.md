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

防御配置可以对单次完整目标传 `defensive_allocation=True`，或显式启用 `allow_defensive_underinvested`。这允许 0–3 只及全现金目标，仍保留单票范围、最多 6 只、90% 投资预算、现金储备和行业上限。不会补足持仓数量，也不生成防御信号；禁止买入的市场仍允许合法减仓。

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

`submit_bracket()` 仅接受多头父单。两个保护子单先注册为休眠订单，按父单实际成交数量启用；止盈与止损是 OCO，不会重复占用可卖股份。父单拒绝或无成交取消会取消子单；部分成交后取消父单仍保留已成交仓位的保护，保护子单开始成交会取消未成交父单余量。禁止单独取消已有仓位的保护子单；独立风险清仓可以显式替代整个保护组。价格跳空仍按真实观测成交，不保证止损价格。

仅接受带时区的 1/5/15/30 分钟或明确时间的 irregular 事件；日线和小时线明确报错。日线 OHLC 无法证明 10:00 / 15:30 的执行顺序。需要正确的 FeedSpec 时间戳语义，实际风险 deadline 前需要有可交易的数据事件。不要用 `enforce_sessions=True` 丢弃用于监测盘外风险的观测。

## 财报的时间边界

事件包含证券、事件 ID、带时区公告时间、BMO/AMC/DURING/UNKNOWN、信息实际可用时间、来源、缺失与取消标记。同一 ID 的修订按 `available_at` 生效，只读取当时可见的版本。未来公告日期可以作为已公开日程使用，未来才取得的日程不能回填历史。

`EarningsCoverage` 必须在当时可见并覆盖至少前瞻 2 个交易日，含该日结束。空事件列表仅在有可信覆盖声明时代表“未发现财报”；缺失日历对新增买入关闭准入。普通减仓和风险卖出不受财报买入限制。

当时可信分类为 `plain_sector_etf` 的普通行业 ETF 豁免单一公司财报覆盖、禁买/清仓窗口及财报半仓上限；不会对股票全局放宽日历规则。ETF 仍检查价差、正成交量与流动性可用时间，以及时段、已结算现金、行业、单票和市场门槛。未知/杠杆/反向/大盘类型不能获得豁免。持仓分类缺失或不受支持时产生保守风险退出，不能推断它是 ETF；数据提供方必须维护历史分类。

- 前 2 个交易日从首日开盘起，直到公告前，禁止新建和加仓。AMC 包含公告当天的 RTH。
- 默认 `hold_through_earnings: false`。BMO 在前一交易日收盘前 30 分钟请求清仓，AMC 在公告当天收盘前 30 分钟请求清仓。DURING/UNKNOWN 按前一交易日处理；非交易日公告在前一合法交易日清仓。缺失覆盖对现有仓位产生保守清仓请求。
- 若公告信息到达时已错过窗口，只对公告前已持有的仓位在当前/下一合法观测点处理并留下审计记录；财报后新仓不会重用财报前清仓计划。缺少建仓时间的迁移持仓保守视作公告前仓位。不伪造前一天或盘后的平仓。
- 公告后第一个受影响 RTH 的前 60 分钟禁止外部买入；随后默认持续 1 个交易日检查相对价差 ≤0.15%、同一时刻历史 RVOL ≥1.8、正成交量及流动性实际可用时间。缺字段、未来字段或不满足阈值均拒绝。
- 此事件期的新目标上限为通常单票上限的 0.5 倍（默认 12.5%）。它只是外部买入许可，不生成买单。`event_statistics()` 单独报告财报准入触发次数和比率。

## 仓位、调仓与风控

默认新建/显式目标为 10%–25%，股票目标 90%，现金 10%，行业上限 40%，建议完整分配 4–6 只。普通持仓跌到 10% 以下不会自动补仓。未知行业默认拒绝，`warn` 会留下“未验证行业”的允许记录，不宣称通过行业校验。挂单按已承诺买入和行业计入，不预支待卖股票的现金。

超过 25% 的价格漂移会阻止加仓并记录。默认请求在下一交易日合法行情减仓；可改为调仓日减仓或仅监测。请求数量按观测时权益计算，因此价格跳变或部分成交可能继续漂移；没有“瞬间保证权重”的假设。

每 N 个交易日默认为 N=10，以第一次观测到的交易日为锚点（第 0 天允许），不依赖首次订单或输入方式；可用 ISO 日期 `rebalance_anchor` 固定锚点。纯检查接口须提供 `State.anchor` 或显式配置。也可用 `monthly` 每月首/末交易日，或 `semi_monthly` 每月 1 日、16 日及之后的首个交易日（节假日顺延）。日期仅许可外部调整，零交易合法。`submit_intent`、目标百分比、完整权重及标记为调仓的基座子订单共享日期规则。变动小于 3 个百分点忽略。每周按纽约时间 ISO 周计数，最多接受 4 次新建仓订单，未成交订单立即占用额度；取消仍保守计入该周，成交与修改不重复计数。加仓、普通减仓和风险卖出不消费新建额度。

## 跨结算日调仓计划

默认仍使用单次调仓接口。启用 `rebalance_plan_enabled=True` 后，`rebalance_to_weights()` 创建可延续的外部配置计划；也可显式调用以下接口（计划仅接受市价单）：

```python
plan = broker.create_rebalance_plan(
    external_weights, rebalance_id="allocation-2026-10",
    defensive_allocation=False,
)
current_plan = broker.rebalance_plans[plan.plan_id]
# 可显式撤销剩余部分；保留已实际成交的持仓。
broker.cancel_rebalance_plan(plan.plan_id)
```

计划只能在合法调仓日的 RTH 创建。记录不可改写的 ID、目标、创建时间、有效期与参考价格，状态为 `selling / waiting_cash / buying / completed / canceled / expired`。先完成所有减仓，再用真正已结算现金提交买入；有未成交卖单或价格缺口时不提前进入买入阶段。交易日与 T+1/T+2 结算日分别计算。每个新增子单仍遵守原有 `NEXT_BAR` 时间顺序、账户、财报、RTH、流动性、仓位和周建仓额度。

只有有效计划里的授权资产可以在非调仓日延续；不绕过其他门槛。`order_target_percent(..., rebalance_id=...)` 可检查/继续已授权目标；`Intent.rebalance_id` 引用同一授权。重复 ID 返回原计划及原订单，不重复开仓；不能改变目标或延长旧计划，同资产只保留一个待成交子单，同时只允许一个活动计划。其他普通订单与活动计划冲突时拒绝；风险退出可以取消剩余计划。

默认有效期为创建日及之后共 `rebalance_plan_sessions=5` 个真实交易日，截止最后一天实际收盘；`valid_until` 可进一步缩短。目标分类、财报、行业或市场变得不合法时取消剩余计划。当前观测开盘/收盘或含费用模型的实际执行价偏离参考价格超过 `rebalance_plan_max_price_change=0.05` 时取消；允许范围内重新按当前权益/价格计算剩余申请，绝不按参考价成交。若最终现金预算已不足且没有待结算款，取消并记录 `rebalance_plan_cash_budget_changed`；不削减现金储备、追单或自动填仓。原有死区可以使保留仓位偏离理想权重，费用和价格变化也可能使目标无法完成。

计划只保存授权及订单 ID、完成标的，实际股份、成交、费用、预约现金和结算均读取基座的唯一账本。部分成交保留剩余预约；取消/到期只释放未成交预约，已经成交的卖出款继续按实际结算日期释放。已完成计划不因后续漂移自动重启。

## 延后订单的有效期

普通买卖单默认 `buy_time_in_force=DAY`，截止提交日实际收盘（包含半日市）。可用 `GTD` 延续，但最多覆盖 `max_defer_sessions=2` 个真实交易日；订单/意图可传带时区的 `valid_until` 缩短有效期。计划子单继承计划有效期。股票财报后准入订单还受对应事件期最后收盘限制，不能等到事件失效后才补单。

到期在新的行情时间推进时、成交检查前执行：取消剩余订单、释放剩余预约，记录 `expired` 与原因。盘外和开盘/收盘买入缓冲内的有效旧订单等待下一合法窗口；缺失数据不会延长有效期。数据恢复后重检当时的财报修订、市场、现金和仓位，实际成交再用真实价格/费用/成交量检查。修改不能延长有效期。

`Kind.RISK` 与 Bracket 保护子单采用 GTC，不能赋予普通买单的 DAY/GTD 到期。风险退出缺价或盘外时继续等待下一个合法真实报价，不会无声消失，也不保证历史触发价。审计分别保留 `deferred`、显式取消、计划取消和 `expired` 原因。

## 缺失数据策略

配置显式区分日历、流动性、市场指标和价格缺口，不允许用未来数据补历史：

| 配置 | 默认 | 可选行为 |
|---|---|---|
| `missing_earnings` | `reject` | `allow` 允许缺失覆盖的买入并记录 `earnings_coverage_unverified`；已知事件的禁买窗口仍有效 |
| `missing_earnings_position` | `liquidate` | `hold` 不因覆盖缺失强制清仓；已知事件的清仓规则仍有效 |
| `missing_liquidity` | `reject` | `defer` 等待当时可用的财报后价差/RVOL/成交量数据 |
| `missing_market` | `reject` | `defer` 在启用市场过滤时等待当时可用的 VIX |
| `missing_price` | `error` | `defer` 对持仓缺失 mark 暂用历史有效价估值，暂停新增买入及依赖权重/回撤的减仓，不用陈旧价格成交 |

`defer` 保留挂单与已预约资金，数据恢复后重新检查全部约束；它不表示已经通过准入。证券类型与结算资金等硬限制不能通过缺失数据设置关闭。

可选 `market_gates` 默认关闭。启用后需要当时可见的 VIX：20–30 将单票上限乘 0.5，≥30 或账户回撤 ≥10% 禁止新增买入。回撤 ≥15% 仅使用显式 `drawdown_reduction_fraction` 生成预定义减仓请求；未配置时记录 `drawdown_reduction_plan_missing`，不会声称已执行。减仓计划每个持续回撤区间只请求一次，避免每个 bar 递归减半。

所有提交/修改和最终成交分别检查。提交可 `RESIZE`；实际价格/费用使计划越界时拒绝成交并释放预约资金，不偷偷更改外部目标。结构化审计包含时间、资产、订单 ID、种类、阶段、可用结算现金、所需资金和原因。常见码包括 `outside_rth`、`entry_window`、`settled_cash`、`cash_reserve`、`short_sale`、`earnings_blackout`、`earnings_liquidity_missing`、`overweight_drift`、`industry_missing`、`rebalance_schedule`、`weight_deadband` 和 `weekly_entries`。

审计还包括 `side`、申请数量、许可数量、单次实际成交量和最终状态。`submission` 是尝试，`order` 是基座订单，`execution` 是一次增量成交，`terminal` 是终止；不能把重复成交前检查当作成交。`buy_checks` 仅统计买入提交（卖出 `REBALANCE` 不计入），`earnings_buy_checks` 统计真正到达股票财报门禁的提交，财报触发率以此为分母；分别报告四种准入动作、唯一订单数、成交次数/数量、完整成交订单数和唯一财报事件数。ETF 的财报豁免不会虚增股票财报调用。DEFER 是等待而非最终成交许可。

## 验证与数据依赖

```bash
uv run pytest tests/constraints -q --no-cov
uv run pytest -q -m 'not benchmark and not upstream_evidence' --cov-report=term
uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check
uv run pre-commit run --all-files
```

未接入真实财报、证券分类、同时间 RVOL、报价或 VIX 服务，也没有实盘连接。上游冻结的 `upstream_evidence` 和历史性能报告不是本分支的再认证：CI 在导入的不可变提交上校验原始证据及其 27 项测试，同时检查本分支未改写报告或历史认证声明；当前源码另跑完整行为、兼容性、覆盖率、公共框架对照和性能检查。仓库不支持 GitHub Dependency Review 时，必须通过锁定依赖的漏洞审计及包含扩展的源代码扫描，不忽略扫描失败或降低阈值。
