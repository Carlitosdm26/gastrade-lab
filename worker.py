from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from strategy import TWO_CANDLE_MIN_NET_PROFIT_PERCENT, crossover_signal, threshold_entry_signal, two_candle_signal

ASSETS = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "AMZN": "Amazon",
    "TSLA": "Tesla",
}
INTERVAL_SECONDS = {"1m": 60, "2m": 120, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "1d": 86400}
TWO_CANDLE_BUY_ALLOCATION_PERCENT = 20
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


def _get_running_experiments(db) -> list[Experiment]:
    return db.scalars(
        select(Experiment).where(Experiment.status == "running").order_by(Experiment.created_at.desc())
    ).all()


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
                for experiment in _get_running_experiments(db):
                    experiment.error = str(error)[:1000]
                db.commit()
        _stop_event.wait(POLL_SECONDS)

def _poll_market() -> None:
    with SessionLocal() as db:
        experiments = _get_running_experiments(db)
        active_requests = {
            (symbol, experiment.config["interval"])
            for experiment in experiments
            for symbol in experiment.config["symbols"]
        }
    requests = active_requests or {(symbol, "5m") for symbol in ASSETS}

    ordered_requests = sorted(requests, key=lambda item: (INTERVAL_SECONDS[item[1]], item[0]))
    snapshots = {}
    snapshot_errors = {}
    with ThreadPoolExecutor(max_workers=min(5, len(ordered_requests)), thread_name_prefix="market-fetch") as executor:
        future_requests = {
            executor.submit(fetch_chart, symbol, interval, history_period(interval)): (symbol, interval)
            for symbol, interval in ordered_requests
        }
        for future in as_completed(future_requests):
            request = future_requests[future]
            try:
                snapshots[request] = future.result()
            except Exception as error:
                snapshot_errors[request] = str(error)[:300]

    for request, snapshot in list(snapshots.items()):
        if snapshot.get("currency") not in {"USD", "EUR"}:
            snapshot_errors[request] = f"Divisa no soportada: {snapshot.get('currency')}"
            del snapshots[request]

    usd_requests = [request for request, snapshot in snapshots.items() if snapshot["currency"] == "USD"]
    fx_error = None
    if usd_requests:
        try:
            eurusd = fetch_eurusd()
        except Exception as error:
            eurusd = 1.0
            fx_error = str(error)[:300]
            for request in usd_requests:
                snapshot_errors[request] = f"No se pudo convertir a EUR: {fx_error}"
                del snapshots[request]
    else:
        eurusd = 1.0
    now = time.time()
    quote_intervals = {}
    for symbol, interval in snapshots:
        if symbol not in quote_intervals or INTERVAL_SECONDS[interval] < INTERVAL_SECONDS[quote_intervals[symbol]]:
            quote_intervals[symbol] = interval
    quote_snapshots = {
        symbol: snapshots[(symbol, interval)]
        for symbol, interval in quote_intervals.items()
    }

    with SessionLocal() as db:
        current_requests = {
            (symbol, experiment.config["interval"])
            for experiment in _get_running_experiments(db)
            for symbol in experiment.config["symbols"]
        }
        for (symbol, interval), snapshot in snapshots.items():
            currency = snapshot["currency"]
            if currency not in {"USD", "EUR"}:
                raise MarketDataError(f"Divisa no soportada para {symbol}: {currency}.")
            if (symbol, interval) not in current_requests:
                continue
            existing_timestamps = set(db.scalars(
                select(Candle.timestamp).where(Candle.symbol == symbol, Candle.interval == interval)
            ).all())
            interval_seconds = INTERVAL_SECONDS[interval]
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

        for symbol, snapshot in quote_snapshots.items():
            currency = snapshot["currency"]
            price_eur = snapshot["price_native"] / eurusd if currency == "USD" else snapshot["price_native"]
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
        db.flush()

        for symbol in quote_snapshots:
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
        if snapshots:
            market_status.last_success_at = now
        error_messages = [
            f"{symbol} ({interval}): {message}"
            for (symbol, interval), message in sorted(snapshot_errors.items())
        ]
        market_status.last_error = "; ".join(error_messages)[:1000] or None
        market_status.source = SOURCE_NAME

        for experiment in _get_running_experiments(db):
            config = experiment.config
            missing_requests = [
                (symbol, config["interval"])
                for symbol in config["symbols"]
                if (symbol, config["interval"]) not in snapshots
            ]
            if missing_requests:
                experiment.error = "; ".join(
                    f"{symbol}: {snapshot_errors.get((symbol, interval), 'datos no disponibles')}"
                    for symbol, interval in missing_requests
                )[:1000]
                continue
            _process_signals(db, experiment, now)
            elapsed = _elapsed_seconds(experiment, now)
            if elapsed >= config["duration_days"] * 86400:
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
    slow_period = max(
        config.get("slow_period", 21),
        config.get("entry_lookback_candles", 3) + 2,
    )
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
        if config.get("strategy") == "two_candle_profit":
            _process_two_candle_profit(db, experiment, symbol, history, candle, now)
            cursor.candle_timestamp = candle.timestamp
            db.flush()
            continue
        if config.get("strategy", "moving_average_grid") == "momentum_target":
            _process_momentum_target(db, experiment, symbol, history, candle, now)
            cursor.candle_timestamp = candle.timestamp
            db.flush()
            continue

        closes_native = [item.close for item in history]
        signal = crossover_signal(closes_native, config["fast_period"], config["slow_period"])
        quote_rate = candle.eurusd if candle.currency == "USD" else 1.0
        price_eur = candle.close / quote_rate
        position = db.get(Position, {"experiment_id": experiment.id, "symbol": symbol})
        stop_triggered = False
        if position is not None:
            stop_price = position.average_price * (1 - float(config.get("stop_loss_percent", 5)) / 100)
            stop_candle = next((item for item in new_candles if (
                item.low / (item.eurusd if item.currency == "USD" else 1.0) <= stop_price
            )), None)
            if stop_candle is not None:
                stop_quote_rate = stop_candle.eurusd if stop_candle.currency == "USD" else 1.0
                stop_open_eur = stop_candle.open / stop_quote_rate
                _execute_trade(
                    db,
                    experiment,
                    symbol,
                    "sell",
                    min(stop_open_eur, stop_price),
                    config["fee_rate"],
                    0,
                    now,
                )
                stop_triggered = True
        prices = {
            item.symbol: item.price_eur
            for item in db.scalars(select(Quote).where(Quote.symbol.in_(config["symbols"]))).all()
        }
        value = experiment.cash + sum(
            item.quantity * prices.get(item.symbol, price_eur if item.symbol == symbol else 0)
            for item in db.scalars(select(Position).where(Position.experiment_id == experiment.id)).all()
        )
        target_value = value * config["allocation_percent"] / 100 / assets_count
        grid_step_percent = float(config.get("grid_step_percent", 2))
        take_profit_percent = float(config.get("take_profit_percent", 2))
        grid_tranches = max(2, int(config.get("grid_tranches", 4)))
        tranche_value = target_value / grid_tranches

        if not stop_triggered and signal == "sell" and position is not None:
            _execute_trade(db, experiment, symbol, "sell", price_eur, config["fee_rate"], 0, now)
        elif not stop_triggered and signal == "buy" and position is None and experiment.cash > 0:
            _execute_trade(db, experiment, symbol, "buy", price_eur, config["fee_rate"], min(tranche_value, experiment.cash), now)
        elif not stop_triggered and position is not None:
            last_trade = db.scalar(
                select(Trade)
                .where(Trade.experiment_id == experiment.id, Trade.symbol == symbol)
                .order_by(Trade.executed_at.desc(), Trade.id.desc())
                .limit(1)
            )
            reference_price = last_trade.price if last_trade is not None else position.average_price
            buy_level = reference_price * (1 - grid_step_percent / 100)
            sell_level = reference_price * (1 + take_profit_percent / 100)
            grid_candle = next((item for item in new_candles if (
                item.low / (item.eurusd if item.currency == "USD" else 1.0) <= buy_level
                or item.high / (item.eurusd if item.currency == "USD" else 1.0) >= sell_level
            )), None)
            if grid_candle is not None:
                grid_rate = grid_candle.eurusd if grid_candle.currency == "USD" else 1.0
                grid_open_eur = grid_candle.open / grid_rate
                grid_low_eur = grid_candle.low / grid_rate
                grid_high_eur = grid_candle.high / grid_rate
                buy_triggered = grid_low_eur <= buy_level
                sell_triggered = grid_high_eur >= sell_level

                # OHLC cannot reveal which level came first when both are crossed.
                if buy_triggered and not sell_triggered and experiment.cash > 0:
                    buy_price = min(grid_open_eur, buy_level)
                    exposure = position.quantity * buy_price
                    remaining_allocation = max(0, target_value - exposure)
                    notional = min(tranche_value, remaining_allocation, experiment.cash)
                    if notional > 0:
                        _execute_trade(db, experiment, symbol, "buy", buy_price, config["fee_rate"], notional, now)
                elif sell_triggered and not buy_triggered:
                    sell_price = max(grid_open_eur, sell_level)
                    sell_quantity = min(position.quantity, tranche_value / sell_price)
                    _execute_trade(db, experiment, symbol, "sell", sell_price, config["fee_rate"], 0, now, sell_quantity)
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


def _process_momentum_target(db, experiment: Experiment, symbol: str, history: list[Candle], candle: Candle, now: float) -> None:
    config = experiment.config
    currency_rate = candle.eurusd if candle.currency == "USD" else 1.0
    close_eur = candle.close / currency_rate
    position = db.get(Position, {"experiment_id": experiment.id, "symbol": symbol})

    if position is not None:
        fee_rate = config["fee_rate"]
        profit_target = float(config.get("profit_target_percent", 2)) / 100
        minimum_sell_price = position.average_price * (1 + profit_target) / (1 - fee_rate)
        high_eur = candle.high / currency_rate
        if high_eur >= minimum_sell_price:
            open_eur = candle.open / currency_rate
            _execute_trade(
                db,
                experiment,
                symbol,
                "sell",
                max(open_eur, minimum_sell_price),
                fee_rate,
                0,
                now,
                position.quantity,
            )
        return

    closes_eur = [
        item.close / (item.eurusd if item.currency == "USD" else 1.0)
        for item in history
    ]
    trigger_type = config.get("entry_trigger_type", "percent")
    trigger_value = (
        config.get("entry_price_eur") if trigger_type == "price"
        else config.get("entry_change_percent", 1)
    )
    if trigger_value is None or not threshold_entry_signal(
        closes_eur,
        trigger_type,
        float(trigger_value),
        int(config.get("entry_lookback_candles", 3)),
    ):
        return

    prices = {
        item.symbol: item.price_eur
        for item in db.scalars(select(Quote).where(Quote.symbol.in_(config["symbols"]))).all()
    }
    holdings_value = sum(
        item.quantity * prices.get(item.symbol, close_eur if item.symbol == symbol else 0)
        for item in db.scalars(select(Position).where(Position.experiment_id == experiment.id)).all()
    )
    target_value = (
        experiment.cash + holdings_value
    ) * config["allocation_percent"] / 100 / len(config["symbols"])
    _execute_trade(db, experiment, symbol, "buy", close_eur, config["fee_rate"], target_value, now)


def _process_two_candle_profit(db, experiment: Experiment, symbol: str, history: list[Candle], candle: Candle, now: float) -> None:
    config = experiment.config
    closes_eur = [
        item.close / (item.eurusd if item.currency == "USD" else 1.0)
        for item in history
    ]
    signal = two_candle_signal(closes_eur)
    currency_rate = candle.eurusd if candle.currency == "USD" else 1.0
    close_eur = candle.close / currency_rate
    position = db.get(Position, {"experiment_id": experiment.id, "symbol": symbol})

    fee_rate = config["fee_rate"]
    if signal == "sell":
        minimum_net_sale = position.average_price * (1 + TWO_CANDLE_MIN_NET_PROFIT_PERCENT / 100) if position else 0
        if position is not None and close_eur * (1 - fee_rate) >= minimum_net_sale:
            _execute_trade(
                db,
                experiment,
                symbol,
                "sell",
                close_eur,
                fee_rate,
                0,
                now,
                position.quantity,
            )
        return

    if signal != "buy" or experiment.cash <= 0:
        return

    prices = {
        item.symbol: item.price_eur
        for item in db.scalars(select(Quote).where(Quote.symbol.in_(config["symbols"]))).all()
    }
    holdings_value = sum(
        item.quantity * prices.get(item.symbol, close_eur if item.symbol == symbol else 0)
        for item in db.scalars(select(Position).where(Position.experiment_id == experiment.id)).all()
    )
    target_value = (
        experiment.cash + holdings_value
    ) * config["allocation_percent"] / 100 / len(config["symbols"])
    exposure = position.quantity * close_eur if position is not None else 0
    remaining_allocation = max(0, target_value - exposure)
    tranche_value = target_value * TWO_CANDLE_BUY_ALLOCATION_PERCENT / 100
    notional = min(tranche_value, remaining_allocation, experiment.cash)
    if notional > 0:
        _execute_trade(db, experiment, symbol, "buy", close_eur, fee_rate, notional, now)


def _execute_trade(
    db,
    experiment: Experiment,
    symbol: str,
    side: str,
    price: float,
    fee_rate: float,
    target_value: float,
    now: float,
    quantity: float | None = None,
) -> None:
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
        quantity = min(position.quantity, quantity if quantity is not None else position.quantity)
        if quantity <= 0:
            return
        old_quantity = position.quantity
        notional = quantity * price
        fee = notional * fee_rate
        experiment.cash += notional - fee
        experiment.realized_pnl += notional - fee - quantity * position.average_price
        remaining_quantity = old_quantity - quantity
        if remaining_quantity <= 1e-12:
            db.delete(position)
        else:
            position.quantity = remaining_quantity

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
