# 美股现金账户：第一步

启用 `BacktestConfig.from_preset("us_cash_equities")` 后，订单提交与实际成交都会执行现金账户约束。
原有默认配置及框架兼容配置保持原有行为。`us_cash_account=True` 是显式开关，不会将所有
上游现金账户回测自动改成美股结算模型。

```python
from ml4t.backtest import BacktestConfig, run_backtest

config = BacktestConfig.from_preset("us_cash_equities")
config.initial_cash = 10_000
# 示例成本，可替换成实际券商费率；不是预设的券商报价。
config.commission_per_trade = 1.0
config.slippage_rate = 0.0005

# result = run_backtest(prices=prices, strategy=strategy, config=config)
```

## 账户和订单约束

| 行为 | 实现 |
|---|---|
| 做空、卖出超过现有持仓 | 拒绝；不能形成负持仓 |
| 融资、跳过现金校验 | 启用现金模式时拒绝矛盾配置 |
| 买入 | 已结算现金须覆盖成交金额、佣金及其他挂单占用 |
| 买单提交 | 按当前参考价／买入限价、估计滑点及佣金预留资金 |
| 挂单持续、修改、撤销 | 跨 bar 保留预留；修改重新校验，失败保持原单；终止后释放 |
| 实际成交 | 按实际数量、含滑点与市场冲击的价格和佣金再次校验；失败不改变现金、持仓或成交账簿 |
| 部分成交 | 保留剩余数量所需资金，包括剩余成交可能发生的固定佣金 |
| 卖出 | 净款计入现金及资产净值，直到结算日期才成为可用资金 |
| 当天卖出已用已结算资金支付的持仓 | 允许；没有额外最低持有天数或 PDT 交易次数限制 |
| 小数股 | 默认允许；不把股价高低作为持仓资格限制 |

策略可查看 `broker.settled_cash`、`broker.unsettled_cash`、`broker.reserved_cash` 和
`broker.get_buying_power()`。现金余额 `broker.cash` 包含待结算卖出款，不能直接用它判断购买力。

```text
settled_cash = cash - unsettled_cash
buying_power = max(0, settled_cash * (1 - cash_buffer_pct) - reserved_cash)
```

初始现金视为已结算 USD 资金。`cash_buffer_pct` 是可选的研究缓冲比例，不是佣金，也不是
已验证的券商市场单专用保证金要求。市场单发生跳空时可能在成交时被拒绝，预留不保证成交。
本模式默认次 bar 开盘成交，避免用本 bar 完整收盘信息在同一价格成交。

## 日期与结算日历

按**实际成交日期**选择普通美股结算周期：

| 成交日期 | 周期 |
|---|---|
| 1995-06-07 至 2017-09-04 | T+3 |
| 2017-09-05 至 2024-05-27 | T+2 |
| 2024-05-28 起 | T+1 |

1995-06-07 之前的日期不支持。日期释放与数据频率独立：一分钟 bar 不会在一分钟后释放卖出款；
数据缺少中间日期时，在下一条达到结算日的数据到来时释放。

日线使用 session label 日期；日内事件根据数据时区转换为纽约日期。输入时间戳和
`data_frequency`／`FeedSpec.timestamp_semantics` 必须与数据实际含义一致。

日历基于锁定依赖中的 `pandas_market_calendars` 的 `SIFMA_US`，额外排除所有 Good Friday。
该日历独立于 NYSE 交易日历：哥伦布日、退伍军人节可开盘而不结算；Sandy 临时停市不自动
禁止结算。测试覆盖这些差异，以及周六元旦、2021/2022 Juneteenth、2023 周六退伍军人节。

特殊结算关闭日需要依据对应 NSCC／DTC 公告补充；没有把交易所全部临时停市日期直接当成
结算关闭日。日历不是完整历史公告档案，做长历史研究前应补齐目标期间的特殊公告。

```python
# 日期仅作配置形式示例；填写经官方公告核实的特殊结算关闭日。
config.settlement_holidays = ("2026-10-13",)
```

旧参数 `settlement_delay` 仍以 bar 计数，用于上游行为兼容；现金模式必须将其保持为 0。
`settlement_reduces_buying_power` 和现金拒单不能关闭，也不能混用上游影子账户购买力配置。

## 范围

这一阶段实现 USD 普通股票／普通 ETF 的账户资金约束，不包含换汇、入金到账延迟、外币现金、
保证金或衍生品合约；显式传入的合约必须是 USD equity、乘数 1 且无保证金参数。
禁止未结算资金买入的保守模型，避免依赖券商允许未结算款交易的违约处理流程。

股票池的流动性、市值筛选，以及大盘、杠杆、反向和复杂衍生 ETF 的准入限制属于后续证券池层。
证券代码本身不提供产品属性，不能据此声称已完成产品过滤。小数股精度和证券资格也需要在券商
适配层进一步核实。本阶段不实现策略、自动交易或个人账户读取。

## 官方依据与验证 {#official-sources}

- [SEC T+1 投资者公告](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins/new-t1-settlement-cycle-what-investors-need-know-investor-bulletin)：2024-05-28 起的周期。
- [SEC 2017 T+2 变更，固定文本副本](sources/sec-t2-2017.txt)：2017-09-05 的历史转换。
- [NSCC Good Friday 2026，a9732](https://www.dtcc.com/Globals/PDFs/2026/March/06/a9732)：普通股票交付／结算服务关闭。
- 历史 NSCC 元旦 2022 公告 a9053：既有回归采用2021-12-31正常处理。
- 历史 Juneteenth 公告：DTCC 2021 a9015、DTC 2022 16811-22。
- 历史 DTC 退伍军人节 2023 公告19155-23：既有回归采用2023-11-10可结算。
- [OCC Sandy 2012，31464，固定原文节选](sources/occ-sandy.txt)：交易停市时 DTC／NSCC 仍正常营业。节选不替代完整公告。

2026-10-10 的 GitHub runner 对 SEC、OCC、BLS 和加拿大协定页面返回403或超时。
涉及的五个引用使用本次直接从官方取得的固定文本副本，原始 URL、获取时间、原始响应与副本的 SHA-256
见[来源清单](sources/manifest.json)。加拿大文本注明政府来源与非商业转载条件；OCC 仅保留21词原文。
CI 同时校验审阅固定的清单哈希、每份副本与渲染副本，缺失或篡改即失败。
这证明引用副本一致，不证明实时 URL 已恢复、现行法规、税收身份或历史 PIT 可用性；其他外链继续实时检查。

2026-10-10 扩展项目文档外链检查时，上述四份历史 DTCC 公告旧地址返回404；
保留公告身份和旧地址供追溯，不把搜索缓存或链接存在声明当作当前官方源已核验。
后续应从[DTCC 官方公告索引](https://www.dtcc.com/legal/important-notices)取得可复核原件，
真实历史研究仍须补齐目标年份来源证据。此次没有改变既有结算日期规则或回归。

```text
unavailable_404: https://www.dtcc.com/Globals/PDFs/2021/September/21/a9053
unavailable_404: https://www.dtcc.com/Globals/PDFs/2021/June/17/a9015
unavailable_404: https://www.dtcc.com/Globals/PDFs/2022/April/27/16811-22
unavailable_404: https://www.dtcc.com/Globals/PDFs/2023/October/11/19155-23
```

针对性行为测试：`tests/accounting/test_us_cash_account.py`；旧 bar 结算兼容测试：
`tests/accounting/test_settlement_delay.py`。
