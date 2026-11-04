from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

TWO_CANDLE_ENTRY_RISE_PERCENT = 0.25
TWO_CANDLE_MIN_NET_PROFIT_PERCENT = 0.5


def moving_average(values: list[float], period: int) -> float | None:
    if period < 1 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def crossover_signal(closes: list[float], fast_period: int, slow_period: int) -> str:
    if fast_period < 1 or slow_period <= fast_period or len(closes) < slow_period + 1:
        return "hold"
    previous = closes[:-1]
    previous_fast = moving_average(previous, fast_period)
    previous_slow = moving_average(previous, slow_period)
    current_fast = moving_average(closes, fast_period)
    current_slow = moving_average(closes, slow_period)
    if None in (previous_fast, previous_slow, current_fast, current_slow):
        return "hold"
    if previous_fast <= previous_slow and current_fast > current_slow:
        return "buy"
    if previous_fast >= previous_slow and current_fast < current_slow:
        return "sell"
    return "hold"


def threshold_entry_signal(
    closes: list[float],
    trigger_type: str,
    trigger_value: float,
    lookback_period: int,
) -> bool:
    if trigger_value <= 0 or lookback_period < 1:
        return False
    if trigger_type == "price":
        return len(closes) >= 2 and closes[-2] < trigger_value <= closes[-1]
    if trigger_type != "percent" or len(closes) < lookback_period + 2:
        return False

    previous_change = (closes[-2] / closes[-2 - lookback_period] - 1) * 100
    current_change = (closes[-1] / closes[-1 - lookback_period] - 1) * 100
    return previous_change < trigger_value <= current_change


def two_candle_signal(closes: list[float]) -> str:
    if len(closes) < 2:
        return "hold"
    minimum_close = closes[-2] * (1 + TWO_CANDLE_ENTRY_RISE_PERCENT / 100)
    if closes[-2] > 0 and closes[-1] >= minimum_close:
        return "buy"
    if closes[-1] < closes[-2]:
        return "sell"
    return "hold"


def portfolio_value(portfolio: dict[str, Any], prices: dict[str, float]) -> float:
    holdings_value = sum(
        position["quantity"] * prices.get(symbol, 0.0)
        for symbol, position in portfolio["holdings"].items()
    )
    return portfolio["cash"] + holdings_value


def execute_trade(
    portfolio: dict[str, Any],
    symbol: str,
    side: str,
    price: float,
    fee_rate: float,
    target_value: float = 0.0,
    timestamp: str | None = None,
) -> dict[str, Any] | None:
    if price <= 0 or not 0 <= fee_rate < 1 or side not in {"buy", "sell"}:
        return None

    holdings = portfolio["holdings"]
    current = holdings.get(symbol)
    if side == "buy":
        notional = min(portfolio["cash"], target_value)
        if notional <= 0:
            return None
        fee = notional * fee_rate
        quantity = (notional - fee) / price
        old_quantity = current["quantity"] if current else 0.0
        old_cost = old_quantity * current["average_price"] if current else 0.0
        holdings[symbol] = {
            "quantity": old_quantity + quantity,
            "average_price": (old_cost + notional) / (old_quantity + quantity),
        }
        portfolio["cash"] -= notional
    else:
        if not current or current["quantity"] <= 0:
            return None
        quantity = current["quantity"]
        notional = quantity * price
        fee = notional * fee_rate
        portfolio["cash"] += notional - fee
        portfolio["realized_pnl"] += notional - fee - quantity * current["average_price"]
        del holdings[symbol]

    portfolio["total_fees"] += fee
    trade = {
        "symbol": symbol,
        "side": side,
        "price": price,
        "quantity": quantity,
        "notional": notional,
        "fee": fee,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
    }
    portfolio.setdefault("trades", []).insert(0, trade)
    return trade
