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
fees = engine.broker.fee_records
fee_totals = engine.broker.fee_statistics()
slippage = engine.broker.slippage_records
cost_totals = engine.broker.execution_cost_statistics()
```

`earnings_provider` 实现 `EarningsProvider.snapshot(asset, asof)`，返回 `EarningsCoverage` 和 `EarningsEvent`。`context_provider(asof, asset, phase)` 返回 `MarketContext`，必须使用同一个带时区的 `asof`。生产数据需提供可信的证券类型、行业、历史财报日程版本和行情可用时间。测试中的人工覆盖声明不是生产数据。

## 公司行为：原始执行行情与 PIT 事件

长历史回测不能忽略拆股、反向拆股和现金股息。新增独立
`quant_constraints.corporate_actions`，默认不启用，以保持既有基座行为。
仅在数据来源完成原始行情真实性验证后，研究真实股票历史才可显式设置
`corporate_actions_enabled=True` 并向
`constrained_engine(..., corporate_action_provider=provider)` 或 `broker_factory`
提供 `CorporateActionProvider`，二者缺一或误传均报错。不提供实盘连接和买卖信号。

Provider 的 `snapshot(security_id, asof)` 返回 `CorporateActionCoverage` 和最新可见
`CorporateAction`，包含取消版本。事件必须有稳定证券 ID（不能只用 ticker）、事件 ID、
有效/除息日期、实际 `available_at`、来源、正整数版本、币种和经济条款；记录日/付款日是
可选元数据。覆盖声明需有开始/结束日期、可用时间和 `point_in_time=True`。
仅有事后整理的数据不能冒充 PIT 覆盖；覆盖缺失或存在无法重建的迟到事件时停止运行。
人工 InMemory 覆盖仅用于测试，不是历史行情/事件供应商。

`MarketContext` 在启用时必须声明 `security_id`、`execution_data_mode='raw_execution'`、
`risk_data_mode='raw_execution'` 和与配置一致的 `signal_data_mode`。订单撮合、估值、
绝对止损/ATR、佣金、滑点及成交量一律使用原始价格和当时股份单位。因子输入可以选择
`raw_execution`、`split_adjusted_signal` 或 `total_return_signal`；不会生成因子/信号，
也不会用总回报行情估值后再加现金股息。声明为复权的执行数据明确拒绝，未实现隐式反向转换。
这些标签只校验声明一致性，**不能证明 OHLCV 实际未复权**，也不能识别供应商误标。

**真实历史策略研究目前阻塞于 [Issue #5](https://github.com/dczhhh/quant-strategies/issues/5)。**
本模块尚无数据接入真实性验证器，不能直接接入未经验证的历史行情进行公司行为回放。
验收前仅可使用明确构造的人工 fixture 验证账户语义；不得把配置标签、自报 raw/verified
或价格跳变猜测当作证据。后续数据入口必须验证供应商调整因子、价格/股份/成交量单位，
将数据哈希、来源/调整版本、PIT 事件 provenance 与验证结果绑定并审计；证据缺失或冲突
须在回放和账户变化前拒绝。Issue #5 的验收实现完成前，不宣称该数据入口已受保护。

事件在有效日期首个可见 bar、订单/财报/风控检查之前处理；拆股及除息日期按美东 NYSE
有效交易日定位。首个观测可以是盘前，事件不因此创建盘前交易。跨缺失 bar 时，已知事件
在下一观测恢复并标记 `recovered_after_gap`、`missing_asset_bar`；资产缺行情时转换历史
估值缓存，不凭空生成成交。有效开盘之后才发现的事件，如存在持仓、挂单或该日以后的
成交，则拒绝并要求回到前序 checkpoint 对账/重放，不能用当前仓位猜历史资格。

| 事件/策略 | 唯一账本行为 |
|---|---|
| `SPLIT`，`r=new/old` | 数量/初始数量/未成交余量乘 r；成本、当前价、water marks、绝对订单/止损/止盈/追踪距离、每股滑点、缓存 ATR、调仓参考价除 r |
| 比例与累计费用 | 目标权重、百分比阈值、MFE/MAE、累计入场佣金、持仓起点不变；拆股不形成 Fill、Trade、已实现盈亏、周新仓或 IBKR 月度成交量 |
| `split_order_policy=adjust` | 原子转换挂单及部分成交后的 Bracket/OCO 父子数量，重新报价预约费；不会改写历史 Fill 或重新收取修改最低费 |
| `split_order_policy=cancel` | 撤销普通/父单未成交余量；已成交裸露敞口的保护子单仍转换并保留，不允许撤保护留下裸仓 |
| 反向拆股 | 仅支持来源声明的 `fractional_policy=retain` 和 `quantity_precision`（0–12）；不丢弃碎股，无法表示的资格报错；现金替代暂不支持，明确阻断，不伪造卖出 |
| `CASH_DIVIDEND` | 以除息日开始前已持有、经过同日拆股转换的股数计资格；当日新买无资格，当日卖出不取消应收；仅支持明确的 `dividend_basis=post_split` 普通 USD 股息 |
| 股息应收 | 毛额、预扣税、费用及净额分别审计；净额进入 AccountState 的非现金应收及权益，不进入 cash、settled_cash 或 buying power |
| `CASH_CREDIT` | 来源确认的实际信用事件，`parent_event_id` 指向股息资格，`effective_date` 是实际入账日、`available_at` 是确认可用时间；可提供本账户实际 `credited_net`，与预估差额单独对账；非交易日到账不按交易日历前移 |

例如 100 股 × $100，除息后原价 $99：$9,900 股票加 $100 应收仍为税前 $10,000。
付款仅把应收转现金，不再增加一次权益。`payable_date` **不会自动释放现金**；未确定
真实到账的股息保持应收，需独立信用事件才能交易。`dividend_withholding_rate=0.0` 与
`dividend_tax_scenario='gross_no_withholding'` 是旧构造的税前情景，不是所有投资者的税率。
项目 YAML 使用下述显式国家税率情景。
非零税率必须给情景命名；来源提供的本账户预扣率优先，费用按明确每股费用计算。

事件键 `(security_id, event_id)`、已处理经济版本、应收/信用资格都保存在同一个
AccountState checkpoint 中。重复 bar、处理器重建、恢复后重放不会重复加股/加钱。
处理前可见修订选最新。每个事件保存 `observed_only` 或 `economically_applied` 的历史作用域：
当事件只被观察、没有改动持仓/挂单/保护单/待执行风险退出/活动调仓授权或非零股息资格时，
同有效日期、同事件类型的可见修订只更新已观察版本及独立修订审计，不重放经济变换，
也不影响事后买入的新仓。改变有效日期或类型不能沿用旧无敞口证明，须对账重放。
曾产生经济影响的事件即使如今已经平仓，经济条款改变或取消仍停止要求对账；纯来源/版本
元数据更新不重复执行。缺少作用域证明的旧 checkpoint 保守视为已产生经济影响。
持仓/挂单期间 ticker 对应证券 ID 改变会报错；并购、分拆、退市、
改名迁移、特殊股息/due bill、非 USD、未知自定义风控价格语义均显式阻断受影响敞口，
不把资产归零或悄悄丢弃。未持有的复杂事件只记 `ignored_unexposed`。
基座 Canonical pre-open intent 的股份目标和授权迁移暂不支持与本公司行为适配器混用，
存在此类意图时明确停止；使用本约束层的目标/调仓计划接口。实际信用事件表示一笔资格的
完整最终到账，分期/部分到账需独立条款实现，不能当作普通全额信用事件；实际净额不能
超过已确认资格毛额，也不能给零股资格凭空加钱。到账资格只引用稳定证券 ID 和旧
`parent_event_id` 锁定的除息股数/金额，与到账当天是否持仓、何时重买和重买数量无关。
旧资格卖出后仍保留；新仓不扩大旧资格。同资格的第二笔信用确认被拒绝。

除息时实际资格股数为 0 的事件只保留 `observed_only` / `observed_no_entitlement`
观察记录及版本，不建立应收或待付款 entitlement，当日随后买入也不会取得旧权益。
无资格的信用通知统一 fail closed，包括明确 0、正金额和未给金额的通知，记录
`Unknown/already credited dividend entitlement` 诊断，不能创建现金。实际股数大于 0、
因预扣税或费用使净额恰好 0 的资格仍有效，保留直到来源确认完整到账并维持一次性校验。
只有真正未支付的有效资格才让已退出观察池的证券继续要求 PIT 公司行为覆盖；已付款
资格只保留一次性支付历史，不再单独要求覆盖。

旧 checkpoint 的未支付零股资格在只读预检中证明股数、毛额、净额和对应应收均为 0，
随后在同一 bar 的原子写入中清理；保留原有事件、收入、费用及审计，不改变现金或权益。
缺少已锁定股数时，只对旧格式读取一次股息历史数量索引，并将证明的数量补回账户
checkpoint；新格式信用读取已有数量，不扫描历史记录。缺少数量证明或零股对应非零金额
时明确停止并要求对账；不会仅凭净额为 0 删除真实资格。恢复旧 checkpoint 后重放清理
仍幂等，后续事件失败会连同清理一起回滚。

`broker.corporate_action_evidence()` 和结果 `metrics['corporate_actions_v1']` 包含版本、
逐事件来源/时点/锁定股数/状态/历史作用域、应收、现金增量、收入与失败诊断；
`observation_revisions` 单独保存无敞口修订的前后条款和来源版本，不重复增加事件收入，
可随结果 Parquet 保存。
原有 cash/exposure 字段含义不变：`equity = cash + net_exposure + outstanding_receivables`。
交易 P&L 与股息收入分开；统一终值不变量检查 `initial + trading P&L + funding + income`，
不豁免公司行为回测的会计校验。

依据：[SEC 除息日与股息资格](https://www.investor.gov/introduction-investing/investing-basics/glossary/ex-dividend-dates-when-are-you-entitled-stock-and)、
[IBKR 股息日期和拆股](https://www.interactivebrokers.com/campus/trading-lessons/dividend-dates-and-stock-splits/)。
具体事件条款必须来自实际历史来源；上述普通事件实现不推导特殊事件资格。

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

## IBKR Pro Tiered 费用（默认启用）

`ConstraintConfig.pricing_plan=ibkr_pro_tiered` 是本项目的默认费用计划，不是上游通用的按成交金额百分比 `TieredCommission`。`broker_factory` 安装独立 `IBKRProTieredUSStock` 和只读费用适配器，不修改上游 `CommissionModel` 签名或未配置的 Broker。`cash_backtest_config()` 单独使用时也不再为零佣金：它提供 $0.0035/股、每单最低 $0.35 的**非零首档简化后备**；只有约束工厂安装的模型才有完整月度档位、订单生命周期和第三方费用。需要研究其他费用情景时显式选择 `pricing_plan=custom` 并配置自己的费用模型，不能把后备模型称作精确 IBKR 计费。

依据 [IBKR 美股官方定价及脚注](https://www.interactivebrokers.com/en/pricing/commissions-stocks.php)，按美东日历月的已确认成交股数边际分档：前 300,000 股 $0.0035，至 3,000,000 股 $0.0020，至 20,000,000 股 $0.0015，至 100,000,000 股 $0.0010，其上 $0.0005。例如月初累计 299,900 股后成交 200 股，基础佣金为 `100×0.0035+100×0.002=$0.55`，而不是整单使用下一档。

计量池包括同月美/加股票及 ETF、买入和卖出，只计 Tiered 且未触发佣金上限的股份。仿真默认**一个直接客户账户、每月未知外部成交量为零**，由 `fee_initial_monthly_volume=0.0` 明示；中途开始且已有累计量时设置该值和 `fee_initial_month=YYYY-MM`。其他已确认美/加股票、ETF 成交可通过 `fee_model.record_external_volume()` 按时间顺序加入同一计量池；必须提供唯一成交 ID、市场、类型、Tiered/上限标识。此接口不模拟加拿大佣金、汇率或其他账户现金；不凭空聚合机构/顾问账户。月度计量保留历史月份，美东新月重新从零（或该月显式初始值）开始，不以 UTC 午夜切换。

常规整数股基础佣金按订单累计实际成交计算，每单最低 $0.35，采用**美股专表及脚注 8 的交易价值 1% 上限**，不把第三方费用纳入此上限。低价股若上限低于最低则上限优先；达到上限的整数部分不累计月度计量。部分成交同一日、同一未修改订单共享最低收费；成功修改按保守的 cancel/replace 生命周期重新适用最低收费，失败修改不改变生命周期。隔夜订单在新的美东日期重新适用最低收费。小数部分按官方脚注 11 的 `max(交易价值×1%, $0.01)`，不四舍五入为零。混合整股/小数股按**每次实际成交分别拆分**，这是缺少券商内部拆单明细时的显式仿真假设，碎片化成交可能更贵。已成交费用不因剩余订单撤销而退还，零成交撤单无费用。动态订单上限资格按当时累计成交在线调整，未用未来成交提前获得低档费率，不能宣称与券商月底追溯处理完全一致。

每次成交的 `FeeRecord` 包含资产、方向、订单 ID、修改代次、时间、数量、真实价格，以及 `broker_commission`、`exchange_ecn_fees_or_rebates`、`clearing_fees`、`regulatory_fees`（另列 SEC/TAF/CAT）、`pass_through_fees`、`total_fees`、累计股数、资格变化、生效版本、假设和来源。总费用复用基座 `Fill.commission`，进入唯一现金、预约、成交、交易盈亏、净值和统计，不另建现金/持仓账本。卖出结算的是扣除总费用后的净款；预约和实际费用都包含在 10% 储备校验中。费用上调、滑点或余额不足导致的拒单仍保留准入原因，拒单不累计档位。

**预估和入账严格分离**：`estimate()`、`quote()`、上游 `calculate()`、deepcopy 预估、提交、修改、现金预约和调仓预检查均不改变月度股数或费用记录。预约用首档费率和新订单最低收费保守估算，不提前依赖未来低档或 rebate；每个真实部分成交仍重新验证费用及现金，因此碎片数量未知时不保证全部余量可成交。只有基座记录并扣除被接受的真实成交后才 `commit(execution_id, quote)`，重复相同成交幂等、冲突/陈旧报价拒绝；撤销、到期、拒单或 DEFER 不入账。部分成交余量重新预约但不重复扣除费用。

第三方费用采用以下**可审计情景，不是券商对账单保证**：

- 执行场所/流动性仅在 `MarketContext.execution_venue`、`execution_liquidity` 和 `fee_metadata_available_at<=asof` 都可验证时使用；未来或缺失元数据按未知处理。
- 已知 NASDAQ/ARCA/IEX 使用官网显示的普通 RTH displayed 情景。添加流动性不计 rebate；移除流动性按场所/价格计算。没有 auction、非显示单、复杂路由或交易所等级建模，不能仅凭 limit 单推断 maker。直接 API 路由不属于本模型 Tiered 范围，明确报错。
- 未知场所/流动性采用 ARCA routed 的保守情景：价格至少 $1 时 $0.0035/股，低于 $1 时交易价值×0.0035，由 `fee_unknown_venue_per_share` / `fee_unknown_venue_rate` 显式可调。它不是所有可能路由的数学上界，压力研究可上调；绝不计入推测 rebate。
- 清算为 $0.0002/股、上限交易价值 0.5%；NYSE/FINRA pass-through 为基础佣金×0.000175/0.00056。税费、介绍经纪商/顾问加价、特殊账户及优惠项目未包含；不提供实盘连接。

项目默认 `fee_history_mode=current_snapshot_backcast`、`fee_snapshot_date='2026-10-10'`：把同一套当前经纪佣金档位和明确的第三方费用情景回套整个历史区间。这是反事实成本假设，不声称还原 2005/2015/2025 年的真实账单。每笔 `FeeBreakdown` 明示 `historical_fee_proxy=true`、快照日期、模式、版本、来源和假设。历史成交仍按其美东月份累计档位，按历史交易日期结算，快照日期不改写交易日历或结算周期。当前仅支持这份有来源的快照；更换快照须补充对应资料和实现。

回套采用**正常收费、非假期监管代理**：SEC 卖出交易价值×0.0000206（[SEC 2026 生效文件](https://www.sec.gov/files/rules/other/2026/34-104909.pdf)）、TAF $0.000195/卖出股且每笔最高 $9.79（[FINRA 费率文件](https://www.finra.org/sites/default/files/2024-11/sr-finra-2024-019.pdf)）、CAT $0.000003/成交股。2026 年第四季度的 TAF 假期**不回套**全部历史交易，因此这份快照情景连 2026 年第四季度也保留正常 TAF。这些费率与场所代理都不是完整历史档案，也不做券商特定分币取整。费用敏感性可以显式传入 `IBKRProTieredUSStock(history_mode='current_snapshot_backcast', backcast_regulatory_rate=...)`，用带版本和来源的 `RegulatoryRate` 上调 SEC/TAF 等代理。

`fee_history_mode=strict_historical` 则按真实成交日期选择明确监管版本，缺失覆盖立即报错。目前内置监管覆盖只到 2026 年：SEC 4 月 4 日前为零，之后按上述费率；TAF 1–9 月正常，10–12 月按已记录假期为零。其他日期需显式提供不重叠的 `regulatory_rates`。该模式只保证监管版本日期覆盖，基础佣金、场所费用和 CAT 仍是披露的快照代理，不能称为完整历史费用还原。独立构造 `IBKRProTieredUSStock()` 保持严格模式；项目工厂根据配置显式选择回套模式。传入自定义 `fee_model` 时使用该模型声明的模式和参数。

## 按成交状态选择滑点（默认启用）

`slippage_mode=regime` 安装独立 `RegimeSlippage`，不改动上游 `SlippageModel.calculate(asset, quantity, price, volume)`。执行器绑定成交当时的上下文；动态滑点替换简化滑点，不叠加固定 2 bps。正常成交买入价为 `reference_price × (1 + bps/10000)`，卖出价为 `reference_price × (1 - bps/10000)`；第三方费用随后按该真实成交价计算。

| 成交状态 | 默认 | 配置 |
|---|---|---|
| 正常 RTH | 2 bps | `slippage_regular_bps` |
| NYSE 提前收盘/半日市 | 3 bps | `slippage_early_close_bps` |
| 公司公告后的首个实际受影响 RTH | 5 bps | `slippage_earnings_bps` |
| 财报压力敏感性 | 10 bps，仅显式情景 | `slippage_earnings_bps=10` |

重叠状态取适用 bps 的**最大值**，半日市与财报默认为 5，不相加为 8。各值可设为 1/2/3/5/10 做敏感性；0 仅用于隔离测试或调试。行业 ETF 必须当时明确分类为 `plain_sector_etf` 才不使用发行公司财报状态，仍使用半日市滑点。单独使用 `cash_backtest_config()` 的后备为统一 2 bps，动态状态须通过约束工厂接入。

财报状态同时要求 `announcement_at<=asof` 与 `available_at<=asof`，只读取 `snapshot(asset, asof)` 当时可见且未取消的版本：已知未来日程不会提前升档，未来修订不会改变过去成交。BMO 在公告当天升档；AMC 公告前仍使用当日正常/半日市档，公告后在下一交易日升档；DURING 在真实公告且信息已可用之后升档。实际公告落在周末、休市或半日市收盘后，顺延至第一个真实交易时段；日期转换使用纽约时区并处理夏令时。默认只覆盖该受影响交易日，盘外不会成交。财报前的例行清仓按当时正常/半日市档，风险卖出也按真实成交时状态计费。

`slippage_missing_earnings=stress` 是缺失公司分类/当时可见覆盖时的默认保守 5 bps 后备；可明确选择 `regular` 或 `error`，审计记录具体后备依据。它独立于买入准入的缺失配置，不能放宽证券或财报门禁。无当前 NYSE session 的**预估**使用保守日历后备并记录依据，实际执行仍受 RTH 门禁约束，不能把无数据当成正常可交易日。

预约采用适用的配置最大值（股票通常 5 bps，明确行业 ETF 通常 3 bps），加上按预约价格计算的保守费用；部分成交余量保留同一预约价格，不重复加滑点或重复扣费。每笔实际成交重新计算当天状态、现金储备、扣费后的单票/行业上限和真实价格。外部目标数量仍按已观测参考价格校验，成本价用于限额及现金检查。真实开盘跳变或新财报资料导致越界时，拒绝成交并释放余量，不计入月度股数、费用或滑点。成交量参与上限仍在滑点计算前执行，5 bps 不赋予无限成交量。

动态模型拒绝同时使用 bid/ask 执行价、spread/fixed/volume slippage、stop 附加滑点或 market impact，以防重复计算 spread/impact。已有专门报价/冲击模型时应显式用 `slippage_mode=configured`，由基座使用指定模型；统一 1/2/5 bps 可通过此模式配合 `cash_backtest_config(slippage_rate=...)` 比较。

`slippage_records` 和增量成交审计记录 `slippage_regime`、`slippage_bps`、`slippage_amount`（本次股数×每股滑点）及事件/日历/缺失后备依据。`slippage_statistics()` 分状态统计成交次数、数量和金额；`execution_cost_statistics()` 将费用明细和滑点分开，另以 `fee_scenarios` 汇总费用模式、快照日期、代理标记、版本、来源和假设。自定义佣金无法拆分时按真实成交记录列入 `custom_unclassified_fees`，不捏造第三方明细。滑点已体现在成交价，不重复计入 `Fill.commission`。这些 bps 是流动性良好的大中票成本情景，不是流动性或成交价格保证。

回归中的固定参考价 $100、整股 100 股买入再卖出、财报受影响日、未知场所的敏感性对照：

| 情景 | 财报 bps | 两笔滑点合计 | 两笔费用合计（约） |
|---|---|---|---|
| 默认 2/3/5 | 5 | $10 | $1.666512 |
| 全状态统一 1 | 1 | $2 | $1.666594 |
| 财报压力 10 | 10 | $20 | $1.666409 |

这只是成本对照，没有策略收益结论；卖出 SEC 代理随真实成交价略变，费用与滑点始终分别列示。

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

## 中国大陆个人股息税情景

`ConstraintConfig()` 保留旧 `legacy` 模式；项目 YAML 显式选择
`cn_mainland_individual_treaty`，按**派息主体的税源国**匹配版本化规则：US 10%、CA 15%。
依据为[中美协定 Article 9](https://www.irs.gov/pub/irs-trty/china.pdf)与
[中加协定 Article 10(2)(b)](https://www.canada.ca/en/department-finance/programs/tax-policy/tax-treaties/country/china-agreement-1986.html)。
该情景仅适用于普通 USD 股票股息、中国大陆税收居民个人、非美加税收居民、受益所有人且文书有效的显式假设；
不能用国籍、上市交易所、SMART 执行场所或 USD 货币判断来源国，也不认定用户真实税收身份。

`DividendTaxQualification` 必须显式提供居民地、`individual`、`non_us_ca_tax_resident`、
`beneficial_owner`、`treaty_documents_valid`、有效日期、已知时间和来源。YAML 的资格为 `null`，
确认前拒绝协定表应用。规则使用 `[effective_from, effective_to)`、`known_at`、`verified_at`、
`source/version`；样例仅记录本次 2026-10-10 核查并在 2027-01-01 前失效待复核，
不是为早年历史补造当时已知的证据。早期输入须提供独立 PIT 规则，否则拒绝。

`CorporateAction` 增加 `tax_source_country/tax_source/tax_source_known_at` 和
`distribution_type=ordinary_stock`。缺字段、未知国别、无有效期规则或冲突规则均拒绝。
REIT、ETF/RIC 分配、MLP、资本返还、特殊股息、未知 ADR 分类及非 USD 仍不支持。
同一美股市场上市的加拿大派息主体使用 CA 规则。真实历史发行人身份/来源证明归 Issue #5D。

可见的账户/事件 `withholding_rate + withholding_source + withholding_known_at` 优先于情景表。
与表不符时 `dividend_tax_conflict_policy=record_actual` 保存差异并采用实际信息；`reject` 要求对账。
未确认协定资格者可显式选 `explicit_rules`，提供有版本/来源的法定税率情景，不能静默回退零税率。
其他国别须显式加普通股息规则；接口字段声明仍不是外部证据认证。

20 股、每股 USD 1：US 毛额20、预提2、净应收18；CA 毛额20、预提3、净应收17。
除息锁定股数和税额，不增加 settled cash；独立信用到账仅转净应收一次，不重复扣税。
实际 `credited_net` 的差额单独审计，不能仅凭到账差额猜测实际税或费用。
税证据、预提金额保存在 canonical entitlement/checkpoint、records、结果与 Parquet 中；
零股观察无权益，正股数且净额零仍有资格；旧模式 checkpoint 结构保持兼容。
`capital_gains_tax_mode=none` 不从盈亏卖出或调仓扣资本利得税，佣金/滑点照旧。
这只限定本回测模型，不表示境外价差收入依法免税。

## 宏观事件门禁与滑点压力

旧构造默认 `macro_events_enabled=false`；项目 YAML 启用，并要求 PIT 覆盖。
给 `ConstraintController(config, earnings, macro=provider)` 提供 `MacroEventProvider.snapshot(asof)`。
首版 `MacroEvent` 覆盖 CPI、NFP、PCE、PPI、FOMC_STATEMENT、FOMC_PRESS、ISM，包含稳定事件 ID、
精确计划 timestamp、`scheduled_known_at`、source/revision、可选实际发布及取消/缺失标记。
`MacroEventCoverage` 明确覆盖范围、事件集合、当时可见时间、PIT/version；空事件列表必须仍有覆盖证明。
合成 `InMemoryMacroEventProvider` 只供应 fixture，不下载今日最终版日历回填历史。
官方计划来自 [BLS CPI](https://www.bls.gov/schedule/news_release/cpi.htm)、
[BEA](https://www.bea.gov/news/schedule)、[Fed](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)，
逐事件读取精确时刻，代码不制造固定发布日或把全部发布硬编码在 08:30。

| 已知计划事件 | 默认主动买入限制（与实际 RTH 求交） |
|---|---|
| 盘前 CPI/NFP/PCE | 开盘起60分钟，正常日09:30–10:30 |
| 盘前 PPI | 开盘起45分钟，正常日09:30–10:15 |
| FOMC statement | 发布前30分钟至当日收盘；14:00计划对应13:30起 |
| FOMC press conference | 发布前60分钟至收盘；14:30计划对应13:30起 |
| 其他 RTH 发布/ISM | 前15分钟至后30分钟 |

窗口都是含起点、不含终点，重叠取并集并输出安全 `resume_at`；半日市裁剪、假期/周末事件不自动移到下一交易日。
`macro_fomc_next_session=true` 可加下一交易日开盘首60分钟；覆盖证明随之要求上一交易日。
未来修订/取消不可提前影响当下；晚获知突发事件仅在其真实可见/发布以后暂停，不向前回填。
未知覆盖默认 `macro_calendar_missing` 拒绝买入；`explicit_opt_out` 是显式放弃要求且记录警告，
已知 blackout 仍有效。日频 OHLC 拒绝精确分钟窗。DEFER 保存既有现金预约，按每个实际 bar 重审；
现有 DAY/GTD 到期/取消会释放预约。提交、改单、部分成交剩余量和最终成交均走共同门禁。
正常减仓、风险退出和已有 Bracket/OCO 保护继续遵守 RTH，不因宏观公布自动清仓或产生方向信号。
盘前发布不允许盘前成交，开盘跳空采用实际可用 RTH 价格。

宏观滑点 `macro_slippage_bps=10` 是压力假设，测试5/10/20三档；与2/3/5的既有档位取 max，绝不相加。
实际压力仅在发布前15/后30分钟与 RTH 的交集；盘前发布对应开盘首30分钟，非无条件全天。
预约采用配置上界；日历缺失的风险卖出用有记录的保守压力，不阻断合法风险退出。
quote basis 保存事件、已知时间和窗口，`macro_incremental_bps` 为相对既有最高档的增量。
统计 `macro_trigger_rate/macro_checks/deferred_entries` 记录真正到达门禁的检查和唯一延迟订单；
`slippage_statistics()` 输出 `macro_slippage_cost` 增量美元成本和 `event_exposure` 实际事件压力成交次数。

Engine A/B 回归重放**相同外部订单**以检查机会成本（人工价格和零执行成本，只隔离门禁，不是收益预测）：

| 模式 | 收益 | 最大回撤 | 成交金额/初始NAV | 执行成本 |
|---|---:|---:|---:|---:|
| 宏观关闭，100买/100卖 | 0% | 1.9608% | 40% | 0 |
| CPI启用，延至110买/100卖 | −2% | 2% | 42% | 0 |

开启门禁不保证改善收益或回撤；真实敏感性/收益比较仍需经过 Issue #5 的授权历史数据验证，不能用 fixture 解除阻塞。
PR #4 第九轮关于每 bar 全量快照的性能意见仍未关闭，本次税与宏观扩展不声称解决它。

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
