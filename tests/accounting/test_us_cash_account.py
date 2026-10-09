"""Financial behavior of the strict US settled-cash profile."""

from dataclasses import replace
from datetime import UTC, date, datetime

import polars as pl
import pytest

from ml4t.backtest import Broker, ContractSpec, OrderStatus, OrderType, Strategy
from ml4t.backtest.accounting.settlement import us_equity_settlement_date
from ml4t.backtest.config import BacktestConfig, DataFrequency, ExecutionPrice, ShareType
from ml4t.backtest.engine import run_backtest
from ml4t.backtest.execution.limits import VolumeParticipationLimit
from ml4t.backtest.types import ExecutionMode


@pytest.mark.parametrize(
    ("trade", "settlement"),
    [
        ("2026-10-09", "2026-10-13"),  # Columbus Day trades but does not settle
        ("2026-11-10", "2026-11-12"),  # Veterans Day
        ("2026-04-02", "2026-04-06"),  # Good Friday is NOT a settlement day
        ("2026-12-24", "2026-12-28"),
        ("2024-05-24", "2024-05-29"),  # T+2 before transition, Memorial Day
        ("2024-05-28", "2024-05-29"),  # T+1 from transition
        ("2017-09-01", "2017-09-07"),  # T+3 before transition, Labor Day
        ("2017-09-05", "2017-09-07"),  # T+2 from transition
        ("2021-12-30", "2022-01-03"),  # Dec 31 is OPEN when New Year is Saturday
        ("2021-06-17", "2021-06-21"),  # Juneteenth not yet a settlement holiday
        ("2022-06-17", "2022-06-22"),
        ("2023-11-08", "2023-11-10"),  # Saturday Veterans Day: Friday is OPEN
        ("2012-10-25", "2012-10-30"),  # Sandy: closed exchange, open settlement
    ],
)
def test_historical_settlement_dates(trade, settlement):
    assert us_equity_settlement_date(date.fromisoformat(trade)) == date.fromisoformat(settlement)


def test_extra_official_closure_and_supported_range():
    assert us_equity_settlement_date(date(2026, 10, 9), frozenset({date(2026, 10, 13)})) == date(
        2026, 10, 14
    )
    with pytest.raises(ValueError, match="1995-06-07"):
        us_equity_settlement_date(date(1995, 6, 6))


def _config(**changes):
    config = BacktestConfig.from_preset("us_cash_equities")
    return replace(
        config,
        initial_cash=200.0,
        execution_mode=ExecutionMode.SAME_BAR,
        execution_price=ExecutionPrice.CLOSE,
        **changes,
    )


def _broker(cash=200.0, **changes):
    config = _config(**changes)
    config.initial_cash = cash
    return Broker.from_config(config)


def _tick(broker, timestamp=datetime(2026, 10, 9, 10), price=100.0, volume=10000.0):
    broker._update_time(
        timestamp=timestamp,
        prices={"TEST": price},
        opens={"TEST": price},
        highs={"TEST": price},
        lows={"TEST": price},
        volumes={"TEST": volume},
        signals={},
    )


def _trade(broker, quantity):
    order = broker.submit_order("TEST", quantity)
    assert order is not None
    broker._process_orders()
    return order


def test_profile_roundtrip():
    cfg = BacktestConfig.from_preset("us_cash_equities")
    assert cfg.us_cash_account and not cfg.allow_short_selling and not cfg.allow_leverage
    assert cfg.share_type is ShareType.FRACTIONAL
    cfg.settlement_holidays = ("2026-10-13",)
    restored = BacktestConfig.from_dict(cfg.to_dict(), strict=True)
    assert restored.us_cash_account and restored.settlement_holidays == cfg.settlement_holidays
    assert not BacktestConfig().us_cash_account  # Upstream defaults retained


@pytest.mark.parametrize(
    "change",
    [
        {"allow_short_selling": True},
        {"allow_leverage": True},
        {"skip_cash_validation": True},
        {"reject_on_insufficient_cash": False},
        {"settlement_reduces_buying_power": False},
        {"settlement_delay": 1},
        {"buying_power_reservation": True},
        {"next_bar_queue_shadow_validation": True},
        {"next_bar_submission_precheck": True},
        {"initial_cash": -1},
        {"cash_buffer_pct": 1},
        {"settlement_holidays": ("bad-date",)},
    ],
)
def test_unsafe_config_is_rejected_before_execution(change):
    config = replace(BacktestConfig.from_preset("us_cash_equities"), **change)
    with pytest.raises(ValueError, match="Invalid BacktestConfig"):
        Broker.from_config(config)


def test_direct_broker_cannot_bypass_cash_rules():
    with pytest.raises(ValueError, match="allow_leverage"):
        Broker(us_cash_account=True, allow_leverage=True)
    with pytest.raises(ValueError, match="derivative"):
        Broker.from_config(_config(), contract_specs={"ES": ContractSpec("ES", multiplier=50)})


def test_short_and_oversell_reject_atomically():
    broker = _broker()
    _tick(broker)
    short = _trade(broker, -0.5)
    assert short.status is OrderStatus.REJECTED and short.rejection_code == "account_restriction"
    assert broker.cash == 200 and not broker.positions and not broker.fills
    assert _trade(broker, 1.25).status is OrderStatus.FILLED
    before = (broker.cash, broker.get_position("TEST").quantity, len(broker.fills))
    oversell = _trade(broker, -1.5)
    assert oversell.status is OrderStatus.REJECTED
    assert (broker.cash, broker.get_position("TEST").quantity, len(broker.fills)) == before


def test_paid_fractional_purchase_can_sell_same_day_without_pdt_limit():
    broker = _broker(cash=1000)
    _tick(broker)
    for _ in range(5):
        assert _trade(broker, 0.25).status is OrderStatus.FILLED
        assert _trade(broker, -0.25).status is OrderStatus.FILLED
    assert broker.cash == 1000 and broker.unsettled_cash == 125
    assert broker.settled_cash == 875 and broker.get_buying_power() == 875


def test_sale_receivable_counts_in_equity_but_cannot_fund_new_buy():
    broker = _broker(cash=150)
    _tick(broker)
    _trade(broker, 1.5)
    _trade(broker, -1.5)
    assert broker.get_account_value() == 150
    assert broker.cash == broker.unsettled_cash == 150
    assert broker.settled_cash == broker.get_buying_power() == 0
    assert _trade(broker, 0.01).status is OrderStatus.REJECTED
    # Hundreds of intraday bars do not turn T+1 into "one bar".
    for minute in range(60):
        _tick(broker, datetime(2026, 10, 9, 11, minute))
    _tick(broker, datetime(2026, 10, 12, 10))
    assert broker.unsettled_cash == 150
    assert _trade(broker, 0.01).status is OrderStatus.REJECTED
    _tick(broker, datetime(2026, 10, 13, 10))
    assert broker.unsettled_cash == 0 and broker.get_buying_power() == 150
    assert _trade(broker, 1.5).status is OrderStatus.FILLED


def test_missing_intermediate_bars_and_extra_closure():
    broker = _broker(cash=100, settlement_holidays=("2026-10-13",))
    _tick(broker)
    _trade(broker, 1)
    _trade(broker, -1)
    _tick(broker, datetime(2026, 10, 13))
    assert broker.get_buying_power() == 0
    _tick(broker, datetime(2026, 10, 16))
    assert broker.get_buying_power() == 100 and broker.unsettled_cash == 0


def test_intraday_utc_uses_new_york_trade_date():
    broker = _broker(cash=100, data_frequency=DataFrequency.MINUTE_1, timezone="UTC")
    # Friday NY evening, but Saturday in UTC.
    _tick(broker, datetime(2026, 10, 10, 0, 30, tzinfo=UTC))
    _trade(broker, 1)
    _trade(broker, -1)
    _tick(broker, datetime(2026, 10, 13, 0, 30, tzinfo=UTC))  # Monday NY
    assert broker.unsettled_cash == 100
    _tick(broker, datetime(2026, 10, 13, 14, 30, tzinfo=UTC))
    assert broker.unsettled_cash == 0


def test_pending_limit_reserves_cash_across_bars_and_cancel_releases():
    broker = _broker(cash=100, commission_per_trade=1)
    _tick(broker)
    first = broker.submit_order("TEST", 1, order_type=OrderType.LIMIT, limit_price=90)
    assert first.status is OrderStatus.PENDING
    assert broker.reserved_cash == 91 and broker.get_buying_power() == 9
    second = broker.submit_order("TEST", 0.2, order_type=OrderType.LIMIT, limit_price=90)
    assert second.status is OrderStatus.REJECTED and broker.reserved_cash == 91
    _tick(broker, datetime(2026, 10, 12, 10))
    broker._process_orders()
    assert broker.reserved_cash == 91
    assert broker.cancel_order(first.order_id)
    assert broker.reserved_cash == 0 and broker.get_buying_power() == 100


def test_update_rechecks_reservation_and_failed_update_is_atomic():
    broker = _broker(cash=100)
    _tick(broker)
    order = broker.submit_order("TEST", 1, order_type=OrderType.LIMIT, limit_price=90)
    assert not broker.update_order(order.order_id, quantity=2)
    assert (
        order.quantity == 1 and order.status is OrderStatus.PENDING and broker.reserved_cash == 90
    )
    assert broker.update_order(order.order_id, quantity=0.5)
    assert broker.reserved_cash == 45 and broker.get_buying_power() == 55


def test_fees_buffer_and_estimated_slippage_are_reserved():
    broker = _broker(cash=100, commission_per_trade=1, slippage_fixed=1, cash_buffer_pct=0.05)
    _tick(broker)
    assert _trade(broker, 1).status is OrderStatus.REJECTED
    order = broker.submit_order("TEST", 0.5, order_type=OrderType.LIMIT, limit_price=90)
    assert order.status is OrderStatus.PENDING
    assert broker.reserved_cash == 46.5 and broker.get_buying_power() == 48.5


def test_gap_and_actual_slippage_recheck_does_not_mutate_ledger():
    broker = _broker(cash=101, slippage_fixed=1)
    _tick(broker)
    order = broker.submit_order("TEST", 1)
    assert broker.reserved_cash == 101
    _tick(broker, datetime(2026, 10, 12), price=101)
    broker._process_orders()  # Base-price check passes; actual cost 102 fails.
    assert order.status is OrderStatus.REJECTED and broker.reserved_cash == 0
    assert broker.cash == 101 and not broker.positions and not broker.fills


def test_partial_buy_retains_remaining_cash_including_another_fixed_fee():
    broker = Broker.from_config(
        _config(commission_per_trade=1),
        execution_limits=VolumeParticipationLimit(max_participation=0.5),
    )
    broker.cash = 102
    _tick(broker, volume=1)
    order = broker.submit_order("TEST", 1)
    broker._process_orders()
    assert order.filled_quantity == 0.5 and order.status is OrderStatus.PENDING
    assert broker.cash == broker.reserved_cash == 51 and broker.get_buying_power() == 0
    _tick(broker, datetime(2026, 10, 12), volume=1)
    broker._process_orders()
    assert order.status is OrderStatus.FILLED and order.filled_quantity == 1
    assert broker.cash == broker.reserved_cash == 0


def test_sale_commission_holds_net_proceeds():
    broker = _broker(cash=101, commission_per_trade=1)
    _tick(broker)
    _trade(broker, 1)
    _trade(broker, -1)
    assert broker.cash == broker.unsettled_cash == 99 and broker.settled_cash == 0


class _Rotate(Strategy):
    def __init__(self):
        self.orders = []
        self.cash = []

    def on_data(self, timestamp, data, context, broker):
        self.cash.append((broker.settled_cash, broker.unsettled_cash, broker.reserved_cash))
        actions = {9: 1.5, 12: -1.5, 13: 1.5, 14: 1.5}
        if timestamp.day in actions:
            self.orders.append(broker.submit_order("TEST", actions[timestamp.day]))


def test_engine_next_bar_uses_fill_date_and_exports_unchanged_results():
    rows = [
        {
            "timestamp": datetime(2026, 10, day),
            "asset": "TEST",
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 1000.0,
        }
        for day in [9, 12, 13, 14, 15]
    ]
    config = replace(BacktestConfig.from_preset("us_cash_equities"), initial_cash=150)
    strategy = _Rotate()
    result = run_backtest(prices=pl.DataFrame(rows), strategy=strategy, config=config)
    # Oct 12 fill buys, Oct 13 fill sells (settles Oct 14), Oct 13 re-entry rejected.
    assert [order.status for order in strategy.orders] == [
        OrderStatus.FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.FILLED,
    ]
    assert strategy.cash[2] == (0, 150, 0) and strategy.cash[3] == (150, 0, 0)
    assert len(result.fills) == 3
