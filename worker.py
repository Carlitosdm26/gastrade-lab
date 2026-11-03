from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import delete, select

from database import (
    Candle,
    EquitySnapshot,
    Experiment,
    MarketStatus,
    Position,
    Quote,
    QuoteHistory,
    SessionLocal,
    SignalCursor,
    Trade,
)
from market_data import MarketDataError, SOURCE_NAME, fetch_chart, fetch_eurusd
from strategy import crossover_signal

ASSETS = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "AMZN": "Amazon",
    "TSLA": "Tesla",
}
INTERVAL_SECONDS = {"1m": 60, "2m": 120, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "1d": 86400}
POLL_SECONDS = max(30, int(os.getenv("MARKET_POLL_SECONDS", "60")))
QUOTE_HISTORY_LIMIT = 120

_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None


def history_period(interval: str) -> str:
    if interval == "1m":
        return "7d"
    return "1y" if interval == "1d" else "1mo"


def start_worker() -> None:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_worker_loop, name="gastradelab-market-worker", daemon=True)
    _worker_thread.start()


def stop_worker() -> None:
    _stop_event.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=3)


def _get_running_experiment(db) -> Experiment | None:
    return db.scalar(select(Experiment).where(Experiment.status == "running").order_by(Experiment.created_at.desc()))


def _worker_loop() -> None:
    while not _stop_event.is_set():
        try:
            _poll_market()
        except Exception as error:
            with SessionLocal() as db:
                status = db.get(MarketStatus, "primary")
                if status is None:
                    status = MarketStatus(key="primary", source=SOURCE_NAME)
                    db.add(status)
                status.last_error = str(error)[:1000]
                experiment = _get_running_experiment(db)
                if experiment:
                    experiment.error = str(error)[:1000]
                db.commit()
        _stop_event.wait(POLL_SECONDS)


def _poll_market() -> None:
    with SessionLocal() as db:
        experiment = _get_running_experiment(db)
        if experiment:
            config = experiment.config
            symbols = config["symbols"]
            interval = config["interval"]
        else:
            symbols = list(ASSETS)
            interval = "5m"

    period = history_period(interval)
    snapshots = {symbol: fetch_chart(symbol, interval, period) for symbol in symbols}
    eurusd = fetch_eurusd()
    now = time.time()
    interval_seconds = INTERVAL_SECONDS[interval]

    with SessionLocal() as db:
        experiment = _get_running_experiment(db)
        if experiment:
            config = experiment.config
            active_symbols = config["symbols"]
            active_interval = config["interval"]
            if active_interval != interval or active_symbols != symbols:
                return
        else:
            active_symbols = []
            active_interval = "5m"

        for symbol, snapshot in snapshots.items():
            currency = snapshot["currency"]
            if currency == "USD":
                price_eur = snapshot["price_native"] / eurusd
            elif currency == "EUR":
                price_eur = snapshot["price_native"]
            else:
                raise MarketDataError(f"Divisa no soportada para {symbol}: {currency}.")

            quote = db.get(Quote, symbol)
            if quote is None:
                quote = Quote(symbol=symbol)
                db.add(quote)
            quote.price_eur = price_eur
            quote.price_native = snapshot["price_native"]
            quote.currency = currency
            quote.eurusd = eurusd
            quote.market_state = snapshot["market_state"]
            quote.market_time = snapshot["market_time"]
            quote.fetched_at = now
            quote.source = SOURCE_NAME
            quote.error = None
            db.add(QuoteHistory(
                symbol=symbol,
                price_eur=price_eur,
                market_time=snapshot["market_time"],
                fetched_at=now,
            ))

            if experiment and symbol in active_symbols and active_interval == interval:
                existing_timestamps = set(db.scalars(
                    select(Candle.timestamp).where(Candle.symbol == symbol, Candle.interval == interval)
                ).all())
                for candle_data in snapshot["candles"]:
                    if candle_data["timestamp"] + interval_seconds > now:
                        continue
                    if candle_data["timestamp"] in existing_timestamps:
                        continue
                    db.add(Candle(
                        symbol=symbol,
                        interval=interval,
                        timestamp=candle_data["timestamp"],
                        open=candle_data["open"],
                        high=candle_data["high"],
                        low=candle_data["low"],
                        close=candle_data["close"],
                        volume=candle_data["volume"],
                        currency=currency,
                        eurusd=eurusd,
                        source=SOURCE_NAME,
                    ))
        db.flush()

        for symbol in snapshots:
            recent_ids = db.scalars(
                select(QuoteHistory.id)
                .where(QuoteHistory.symbol == symbol)
                .order_by(QuoteHistory.fetched_at.desc(), QuoteHistory.id.desc())
                .limit(QUOTE_HISTORY_LIMIT)
            ).all()
            db.execute(
                delete(QuoteHistory).where(
                    QuoteHistory.symbol == symbol,
                    QuoteHistory.id.not_in(recent_ids),
                )
            )

        market_status = db.get(MarketStatus, "primary")
        if market_status is None:
            market_status = MarketStatus(key="primary", source=SOURCE_NAME)
            db.add(market_status)
        market_status.last_success_at = now
        market_status.last_error = None
        market_status.source = SOURCE_NAME

        experiment = _get_running_experiment(db)
        if experiment:
            _process_signals(db, experiment, now)
            elapsed = _elapsed_seconds(experiment, now)
            if elapsed >= experiment.config["duration_days"] * 86400:
                experiment.status = "complete"
                experiment.completed_at = now
            experiment.error = None

        db.commit()


def _elapsed_seconds(experiment: Experiment, now: float) -> float:
    paused_total = experiment.paused_total_seconds
    if experiment.status == "paused" and experiment.paused_at:
        paused_total += max(0, now - experiment.paused_at)
    return max(0, now - experiment.started_at - paused_total)


def _process_signals(db, experiment: Experiment, now: float) -> None:
    config = experiment.config
    interval = config["interval"]
    slow_period = config["slow_period"]
    assets_count = len(config["symbols"])

    for symbol in config["symbols"]:
        completed = db.scalars(
            select(Candle)
            .where(Candle.symbol == symbol, Candle.interval == interval)
            .order_by(Candle.timestamp.desc())
            .limit(slow_period + 500)
        ).all()
        candles = list(reversed(completed))
        if not candles:
            continue

        cursor = db.get(SignalCursor, {"experiment_id": experiment.id, "symbol": symbol})
        if cursor is None:
            db.add(SignalCursor(experiment_id=experiment.id, symbol=symbol, candle_timestamp=candles[-1].timestamp))
            continue

        new_candles = [candle for candle in candles if candle.timestamp > cursor.candle_timestamp]
        if not new_candles:
            continue

        # Never replay missed signals as a burst of trades after downtime.
        candle = new_candles[-1]
        history = [item for item in candles if item.timestamp <= candle.timestamp]
        closes_native = [item.close for item in history]
        signal = crossover_signal(closes_native, config["fast_period"], config["slow_period"])
        quote_rate = candle.eurusd if candle.currency == "USD" else 1.0
        price_eur = candle.close / quote_rate
        position = db.get(Position, {"experiment_id": experiment.id, "symbol": symbol})
        prices = {
            item.symbol: item.price_eur
            for item in db.scalars(select(Quote).where(Quote.symbol.in_(config["symbols"]))).all()
        }
        value = experiment.cash + sum(
            item.quantity * prices.get(item.symbol, price_eur if item.symbol == symbol else 0)
            for item in db.scalars(select(Position).where(Position.experiment_id == experiment.id)).all()
        )
        target_value = value * config["allocation_percent"] / 100 / assets_count

        if signal == "buy" and position is None and experiment.cash > 0:
            _execute_trade(db, experiment, symbol, "buy", price_eur, config["fee_rate"], min(target_value, experiment.cash), now)
        elif signal == "sell" and position is not None:
            _execute_trade(db, experiment, symbol, "sell", price_eur, config["fee_rate"], 0, now)
        cursor.candle_timestamp = candle.timestamp

        db.flush()

    prices = {
        item.symbol: item.price_eur
        for item in db.scalars(select(Quote).where(Quote.symbol.in_(config["symbols"]))).all()
    }
    positions = db.scalars(select(Position).where(Position.experiment_id == experiment.id)).all()
    holdings_value = sum(position.quantity * prices.get(position.symbol, 0) for position in positions)
    db.add(EquitySnapshot(
        experiment_id=experiment.id,
        timestamp=now,
        cash=experiment.cash,
        holdings_value=holdings_value,
        total_value=experiment.cash + holdings_value,
    ))


def _execute_trade(db, experiment: Experiment, symbol: str, side: str, price: float, fee_rate: float, target_value: float, now: float) -> None:
    position = db.get(Position, {"experiment_id": experiment.id, "symbol": symbol})
    if side == "buy":
        notional = min(experiment.cash, target_value)
        if notional <= 0:
            return
        fee = notional * fee_rate
        quantity = (notional - fee) / price
        old_quantity = position.quantity if position else 0.0
        old_cost = old_quantity * position.average_price if position else 0.0
        if position is None:
            position = Position(experiment_id=experiment.id, symbol=symbol, quantity=0, average_price=0, opened_at=now)
            db.add(position)
        position.quantity = old_quantity + quantity
        position.average_price = (old_cost + notional) / position.quantity
        experiment.cash -= notional
    else:
        if position is None or position.quantity <= 0:
            return
        quantity = position.quantity
        notional = quantity * price
        fee = notional * fee_rate
        experiment.cash += notional - fee
        experiment.realized_pnl += notional - fee - quantity * position.average_price
        db.delete(position)

    experiment.total_fees += fee
    db.add(Trade(
        experiment_id=experiment.id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        notional=notional,
        fee=fee,
        executed_at=now,
    ))
    db.flush()
