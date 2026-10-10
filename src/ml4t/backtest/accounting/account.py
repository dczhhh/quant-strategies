"""Account state management.

This module provides the AccountState class that tracks cash, positions, and
delegates validation to the appropriate AccountPolicy.
"""

from collections import deque
from datetime import date
from heapq import heappop, heappush

from ..types import Position
from .policy import AccountPolicy


class AccountState:
    """Account state ledger with policy-based constraints.

    AccountState is the central ledger that tracks:
    - Cash balance
    - Open positions
    - Account policy (cash vs margin)

    It delegates all validation and constraint checking to the AccountPolicy,
    making it easy to support different account types.

    Example:
        >>> from ml4t.backtest.accounting import AccountState, CashAccountPolicy
        >>> policy = CashAccountPolicy()
        >>> account = AccountState(initial_cash=100000.0, policy=policy)
        >>> account.buying_power
        100000.0
    """

    def __init__(self, initial_cash: float, policy: AccountPolicy):
        """Initialize account state.

        Args:
            initial_cash: Starting cash balance
            policy: AccountPolicy instance (CashAccountPolicy or MarginAccountPolicy)
        """
        self.cash = initial_cash
        self._lock_notional_free_cash = initial_cash
        self._lock_notional_short_basis: dict[str, float] = {}
        self.positions: dict[str, Position] = {}
        # Non-cash entitlements belong to this ledger, never to buying power.
        # Empty for native accounts; opt-in corporate actions populate it.
        self._receivables: dict[str, float] = {}
        self.policy = policy

        # Settlement tracking: holds are (settle_bar, amount) pairs
        self._settlement_holds: deque[tuple[int, float]] = deque()
        self._dated_settlement_holds: list[tuple[date, float]] = []
        self._total_held: float = 0.0

    @property
    def total_equity(self) -> float:
        """Calculate total account equity (Net Liquidating Value).

        For both cash and margin accounts:
            NLV = Cash + non-cash receivables + Σ(position.market_value)

        Returns:
            Total account equity
        """
        return (
            self.cash
            + self._receivable_value
            + sum(p.market_value for p in self.positions.values())
        )

    @property
    def _receivable_value(self) -> float:
        """Recognized non-cash income; excluded from cash and settlement holds."""
        return sum(self._receivables.values())

    @property
    def buying_power(self) -> float:
        """Calculate available buying power for new long positions.

        Delegates to policy:
        - Cash account: buying_power = max(0, cash)
        - Margin account: buying_power = (NLV - MM) / initial_margin_rate

        Returns:
            Available buying power in dollars
        """
        return self.policy.calculate_buying_power(self.cash, self.positions)

    def allows_short_selling(self) -> bool:
        """Check if short selling is allowed.

        Delegates to policy:
        - Cash account: False
        - Margin account: True

        Returns:
            True if short selling allowed, False otherwise
        """
        return self.policy.allows_short_selling()

    def mark_to_market(self, current_prices: dict[str, float]) -> None:
        """Update positions with current market prices.

        This is called at the end of each bar to update unrealized P&L.

        Args:
            current_prices: Dictionary mapping asset -> current_price
        """
        for asset, position in self.positions.items():
            if asset in current_prices:
                position.current_price = current_prices[asset]

    def get_position(self, asset: str) -> Position | None:
        """Get position for a specific asset.

        Args:
            asset: Asset identifier

        Returns:
            Position object if exists, None otherwise
        """
        return self.positions.get(asset)

    def get_position_quantity(self, asset: str) -> float:
        """Get quantity for a specific asset (0 if no position).

        Args:
            asset: Asset identifier

        Returns:
            Position quantity (positive=long, negative=short, 0=flat)
        """
        pos = self.positions.get(asset)
        return pos.quantity if pos else 0.0

    @property
    def unsettled_cash(self) -> float:
        """Total cash held in unsettled transactions."""
        return self._total_held

    @property
    def settled_cash(self) -> float:
        """Cash excluding receivable sale proceeds; reservations are held by orders."""
        return self.cash - self.unsettled_cash

    def add_dated_settlement_hold(self, settlement_date: date, amount: float) -> None:
        if amount > 0:
            heappush(self._dated_settlement_holds, (settlement_date, amount))
            self._total_held += amount

    def release_settled_on(self, current_date: date) -> None:
        """Release matured receivables even when intermediate data bars are absent."""
        while self._dated_settlement_holds and self._dated_settlement_holds[0][0] <= current_date:
            _, amount = heappop(self._dated_settlement_holds)
            self._total_held -= amount
        if not self._dated_settlement_holds and not self._settlement_holds:
            self._total_held = 0.0

    def add_settlement_hold(self, bar_index: int, delay: int, amount: float) -> None:
        """Hold cash from a sale until settlement completes.

        Args:
            bar_index: Current bar index when the fill occurred.
            delay: Number of bars until proceeds are spendable.
            amount: Positive cash amount to hold.
        """
        if amount <= 0 or delay <= 0:
            return
        settle_bar = bar_index + delay
        self._settlement_holds.append((settle_bar, amount))
        self._total_held += amount

    def release_settled(self, current_bar: int) -> None:
        """Release holds whose settlement bar has been reached.

        Args:
            current_bar: Current bar index.
        """
        while self._settlement_holds and self._settlement_holds[0][0] <= current_bar:
            _, amount = self._settlement_holds.popleft()
            self._total_held -= amount
        # Guard against floating-point drift
        if not self._settlement_holds and not self._dated_settlement_holds:
            self._total_held = 0.0

    def __repr__(self) -> str:
        """String representation for debugging."""
        policy_name = self.policy.__class__.__name__
        num_positions = len(self.positions)
        return (
            f"AccountState("
            f"cash=${self.cash:,.2f}, "
            f"equity=${self.total_equity:,.2f}, "
            f"positions={num_positions}, "
            f"policy={policy_name})"
        )
