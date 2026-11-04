import time
import threading

import pytest
from sqlalchemy import select

import worker
from database import Candle, Experiment, MarketStatus, Position, QuoteHistory, SessionLocal, SignalCursor, Trade, init_db


def test_history_period_respects_yahoo_interval_limits():
    assert worker.history_period("1m") == "7d"
    assert worker.history_period("5m") == "1mo"
    assert worker.history_period("1d") == "1y"


def test_worker_trades_only_after_a_new_completed_candle(monkeypatch):
    init_db()
    now = time.time()
    interval = 300
    symbol = "TESTQUOTE"
    current_slot = int(now // interval) * interval
    closes = [10.0, 9.0, 8.0, 7.0, 8.0]
    candles = [
        {"timestamp": current_slot - (len(closes) - index + 1) * interval, "open": close, "high": close, "low": close, "close": close, "volume": 1000}
        for index, close in enumerate(closes)
    ]
    snapshot = {
        "symbol": symbol,
        "currency": "USD",
        "market_state": "REGULAR",
        "market_time": now,
        "price_native": closes[-1],
        "candles": candles,
    }
    monkeypatch.setattr(worker, "fetch_chart", lambda *_args: {**snapshot, "candles": list(snapshot["candles"])})
    monkeypatch.setattr(worker, "fetch_eurusd", lambda: 1.0)

    with SessionLocal() as db:
        db.add(Experiment(
            id="worker-test",
            status="running",
            created_at=now,
            started_at=now,
            initial_balance=1000,
            cash=1000,
            realized_pnl=0,
            total_fees=0,
            config={
                "symbols": [symbol],
                "interval": "5m",
                "fast_period": 2,
                "slow_period": 3,
                "fee_rate": 0.001,
                "allocation_percent": 80,
                "duration_days": 30,
            },
            paused_total_seconds=0,
        ))
        db.commit()

    worker._poll_market()
    with SessionLocal() as db:
        assert db.scalars(select(Trade).where(Trade.experiment_id == "worker-test")).all() == []

    next_close = 10.0
    next_candle = {
        "timestamp": current_slot - interval,
        "open": next_close,
        "high": next_close,
        "low": next_close,
        "close": next_close,
        "volume": 1200,
    }
    snapshot["candles"].append(next_candle)
    snapshot["price_native"] = next_close
    monkeypatch.setattr(worker, "fetch_chart", lambda *_args: {**snapshot, "candles": list(snapshot["candles"])})
    worker._poll_market()

    with SessionLocal() as db:
        trades = db.scalars(select(Trade).where(Trade.experiment_id == "worker-test")).all()
        position = db.get(Position, {"experiment_id": "worker-test", "symbol": symbol})
        assert len(trades) == 1
        assert trades[0].side == "buy"
        assert trades[0].notional == 200
        assert position is not None
        assert abs(position.quantity - 19.98) < 1e-8
        assert abs(db.get(Experiment, "worker-test").cash - 800) < 1e-8
        assert db.scalar(select(Candle.id).where(Candle.symbol == symbol, Candle.timestamp == next_candle["timestamp"])) is not None
        quote_history = db.scalars(
            select(QuoteHistory).where(QuoteHistory.symbol == symbol).order_by(QuoteHistory.fetched_at)
        ).all()
        assert [item.price_eur for item in quote_history] == [8.0, 10.0]


def test_worker_processes_multiple_active_experiments_independently(monkeypatch):
    init_db()
    now = time.time()
    interval = 300
    current_slot = int(now // interval) * interval
    symbol = "MULTIQUOTE"
    closes = [10.0, 9.0, 8.0, 7.0, 8.0]
    candles = [
        {
            "timestamp": current_slot - (len(closes) - index + 1) * interval,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1000,
        }
        for index, close in enumerate(closes)
    ]
    snapshot = {
        "symbol": symbol,
        "currency": "EUR",
        "market_state": "REGULAR",
        "market_time": now,
        "price_native": closes[-1],
        "candles": candles,
    }
    fetched = []
    fetch_barrier = threading.Barrier(2)

    def fake_fetch_chart(requested_symbol, requested_interval, _period):
        fetched.append((requested_symbol, requested_interval))
        if requested_symbol == symbol:
            fetch_barrier.wait(timeout=1)
        return {**snapshot, "candles": list(snapshot["candles"])}

    monkeypatch.setattr(worker, "fetch_chart", fake_fetch_chart)
    monkeypatch.setattr(worker, "fetch_eurusd", lambda: 1.0)

    with SessionLocal() as db:
        for index, (experiment_id, balance) in enumerate((("multi-worker-a", 1000), ("multi-worker-b", 2000))):
            db.add(Experiment(
                id=experiment_id,
                status="running",
                created_at=now + index,
                started_at=now,
                initial_balance=balance,
                cash=balance,
                realized_pnl=0,
                total_fees=0,
                config={
                    "symbols": [symbol],
                    "interval": "5m" if index == 0 else "1m",
                    "fast_period": 2,
                    "slow_period": 3,
                    "fee_rate": 0.001,
                    "allocation_percent": 80,
                    "stop_loss_percent": 5,
                    "duration_days": 30,
                },
                paused_total_seconds=0,
            ))
        db.commit()

    worker._poll_market()
    with SessionLocal() as db:
        cursors = [db.get(SignalCursor, {"experiment_id": experiment_id, "symbol": symbol}) for experiment_id in ("multi-worker-a", "multi-worker-b")]
        assert all(cursor is not None for cursor in cursors)

    next_candle = {
        "timestamp": current_slot - interval,
        "open": 10.0,
        "high": 10.0,
        "low": 10.0,
        "close": 10.0,
        "volume": 1200,
    }
    snapshot["candles"].append(next_candle)
    snapshot["price_native"] = next_candle["close"]
    worker._poll_market()

    with SessionLocal() as db:
        trades = {
            experiment_id: db.scalars(select(Trade).where(Trade.experiment_id == experiment_id)).all()
            for experiment_id in ("multi-worker-a", "multi-worker-b")
        }
        assert len(trades["multi-worker-a"]) == 1
        assert len(trades["multi-worker-b"]) == 1
        assert trades["multi-worker-a"][0].side == "buy"
        assert trades["multi-worker-b"][0].side == "buy"
        assert trades["multi-worker-a"][0].notional == 200
        assert trades["multi-worker-b"][0].notional == 400
        assert {("MULTIQUOTE", "5m"), ("MULTIQUOTE", "1m")} <= set(fetched)


def test_worker_continues_valid_experiments_when_one_market_request_fails(monkeypatch):
    init_db()
    now = time.time()
    interval = 300
    current_slot = int(now // interval) * interval
    valid_symbol = "VALIDQUOTE"
    failing_symbol = "FAILINGQUOTE"
    closes = [10.0, 9.0, 8.0, 7.0, 8.0]
    candles = [
        {
            "timestamp": current_slot - (len(closes) - index + 1) * interval,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1000,
        }
        for index, close in enumerate(closes)
    ]
    snapshot = {
        "symbol": valid_symbol,
        "currency": "EUR",
        "market_state": "REGULAR",
        "market_time": now,
        "price_native": closes[-1],
        "candles": candles,
    }

    def fake_fetch_chart(symbol, interval_name, _period):
        if symbol == failing_symbol:
            raise RuntimeError("Yahoo timeout de prueba")
        return {**snapshot, "candles": list(snapshot["candles"])}

    monkeypatch.setattr(worker, "fetch_chart", fake_fetch_chart)
    monkeypatch.setattr(worker, "fetch_eurusd", lambda: 1.0)

    with SessionLocal() as db:
        for experiment_id, symbol in (("worker-valid", valid_symbol), ("worker-failing", failing_symbol)):
            db.add(Experiment(
                id=experiment_id,
                status="running",
                created_at=now,
                started_at=now,
                initial_balance=1000,
                cash=1000,
                realized_pnl=0,
                total_fees=0,
                config={
                    "symbols": [symbol],
                    "interval": "5m",
                    "fast_period": 2,
                    "slow_period": 3,
                    "fee_rate": 0.001,
                    "allocation_percent": 80,
                    "stop_loss_percent": 5,
                    "duration_days": 30,
                },
                paused_total_seconds=0,
            ))
            db.add_all([
                *[
                    Candle(
                        symbol=symbol,
                        interval="5m",
                        timestamp=candle["timestamp"],
                        open=candle["open"],
                        high=candle["high"],
                        low=candle["low"],
                        close=candle["close"],
                        volume=candle["volume"],
                        currency="EUR",
                        eurusd=1.0,
                        source="test",
                    )
                    for candle in candles
                ],
                SignalCursor(
                    experiment_id=experiment_id,
                    symbol=symbol,
                    candle_timestamp=candles[-1]["timestamp"],
                ),
            ])
        db.commit()

    next_candle = {
        "timestamp": current_slot - interval,
        "open": 10.0,
        "high": 10.0,
        "low": 10.0,
        "close": 10.0,
        "volume": 1200,
    }
    snapshot["candles"].append(next_candle)
    snapshot["price_native"] = next_candle["close"]

    worker._poll_market()

    with SessionLocal() as db:
        valid_trades = db.scalars(select(Trade).where(Trade.experiment_id == "worker-valid")).all()
        failing_trades = db.scalars(select(Trade).where(Trade.experiment_id == "worker-failing")).all()
        failing = db.get(Experiment, "worker-failing")
        status = db.get(MarketStatus, "primary")
        assert len(valid_trades) == 1
        assert valid_trades[0].side == "buy"
        assert failing_trades == []
        assert "FAILINGQUOTE" in failing.error
        assert "FAILINGQUOTE" in status.last_error


@pytest.mark.parametrize(
    ("case_name", "open_price", "high_price", "low_price", "close_price", "expected_side", "expected_fill"),
    [
        ("dip-buy", 99.0, 100.0, 97.5, 98.0, "buy", 98.0),
        ("profit-sell", 101.5, 103.0, 100.5, 101.8, "sell", 102.0),
    ],
)
def test_worker_uses_grid_steps_and_partial_profit_taking(
    monkeypatch, case_name, open_price, high_price, low_price, close_price, expected_side, expected_fill
):
    init_db()
    now = time.time()
    interval = 300
    current_slot = int(now // interval) * interval
    symbol = f"GRID-{case_name}"
    experiment_id = f"grid-worker-{case_name}"
    previous_closes = [110.0, 105.0, 100.0]
    first_timestamp = current_slot - 4 * interval
    candles = [
        {
            "timestamp": first_timestamp + index * interval,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1000,
        }
        for index, close in enumerate(previous_closes)
    ]
    grid_candle = {
        "timestamp": current_slot - interval,
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "volume": 1200,
    }
    candles.append(grid_candle)
    snapshot = {
        "symbol": symbol,
        "currency": "EUR",
        "market_state": "REGULAR",
        "market_time": now,
        "price_native": close_price,
        "candles": candles,
    }
    monkeypatch.setattr(worker, "fetch_chart", lambda *_args: snapshot)
    monkeypatch.setattr(worker, "fetch_eurusd", lambda: 1.0)
    monkeypatch.setattr(
        worker,
        "_get_running_experiments",
        lambda db: db.scalars(select(Experiment).where(Experiment.id == experiment_id, Experiment.status == "running")).all(),
    )

    with SessionLocal() as db:
        db.add(Experiment(
            id=experiment_id,
            status="running",
            created_at=now,
            started_at=now,
            initial_balance=1000,
            cash=700,
            realized_pnl=0,
            total_fees=0.3,
            config={
                "symbols": [symbol],
                "interval": "5m",
                "fast_period": 2,
                "slow_period": 3,
                "fee_rate": 0.001,
                "allocation_percent": 80,
                "stop_loss_percent": 5,
                "grid_step_percent": 2,
                "take_profit_percent": 2,
                "grid_tranches": 4,
                "duration_days": 30,
            },
            paused_total_seconds=0,
        ))
        db.add_all([
            *[
                Candle(
                    symbol=symbol,
                    interval="5m",
                    timestamp=item["timestamp"],
                    open=item["open"],
                    high=item["high"],
                    low=item["low"],
                    close=item["close"],
                    volume=item["volume"],
                    currency="EUR",
                    eurusd=1.0,
                    source="test",
                )
                for item in candles[:-1]
            ],
            Position(
                experiment_id=experiment_id,
                symbol=symbol,
                quantity=3,
                average_price=100,
                opened_at=now - interval,
            ),
            Trade(
                experiment_id=experiment_id,
                symbol=symbol,
                side="buy",
                quantity=3,
                price=100,
                notional=300,
                fee=0.3,
                executed_at=now - interval,
            ),
            SignalCursor(
                experiment_id=experiment_id,
                symbol=symbol,
                candle_timestamp=candles[-2]["timestamp"],
            ),
        ])
        db.commit()

    worker._poll_market()

    with SessionLocal() as db:
        trades = db.scalars(
            select(Trade).where(Trade.experiment_id == experiment_id).order_by(Trade.executed_at, Trade.id)
        ).all()
        position = db.get(Position, {"experiment_id": experiment_id, "symbol": symbol})
        assert len(trades) == 2
        assert trades[-1].side == expected_side
        assert trades[-1].price == expected_fill
        assert trades[-1].quantity < 3
        assert position is not None
        if expected_side == "buy":
            assert position.quantity > 3
        else:
            assert 0 < position.quantity < 3


def test_momentum_target_only_sells_after_net_profit_target():
    init_db()
    now = time.time()
    symbol = "MOMENTUM-EXIT"
    experiment_id = "momentum-target-exit"
    with SessionLocal() as db:
        experiment = Experiment(
            id=experiment_id,
            status="running",
            created_at=now,
            started_at=now,
            initial_balance=1_000,
            cash=1_000,
            realized_pnl=0,
            total_fees=0,
            config={
                "symbols": [symbol],
                "interval": "5m",
                "fee_rate": 0.001,
                "allocation_percent": 80,
                "strategy": "momentum_target",
                "entry_trigger_type": "percent",
                "entry_change_percent": 1,
                "entry_lookback_candles": 2,
                "profit_target_percent": 2,
                "stop_loss_percent": 5,
            },
            paused_total_seconds=0,
        )
        db.add(experiment)
        db.flush()
        history = [
            Candle(
                symbol=symbol, interval="5m", timestamp=now - (3 - index) * 300,
                open=100, high=100, low=100, close=100, volume=1000,
                currency="EUR", eurusd=1, source="test",
            )
            for index in range(3)
        ]
        entry_candle = Candle(
            symbol=symbol, interval="5m", timestamp=now, open=100, high=102,
            low=99, close=102, volume=1000, currency="EUR", eurusd=1, source="test",
        )
        worker._process_momentum_target(db, experiment, symbol, [*history, entry_candle], entry_candle, now)
        position = db.get(Position, {"experiment_id": experiment_id, "symbol": symbol})
        assert position is not None
        assert db.scalar(select(Trade).where(Trade.experiment_id == experiment_id, Trade.side == "buy")) is not None

        below_target = Candle(
            symbol=symbol, interval="5m", timestamp=now + 300, open=position.average_price * 0.9,
            high=position.average_price * 1.01, low=position.average_price * 0.9,
            close=position.average_price * 0.95, volume=1000,
            currency="EUR", eurusd=1, source="test",
        )
        worker._process_momentum_target(db, experiment, symbol, [*history, below_target], below_target, now + 300)
        assert len(db.scalars(select(Trade).where(Trade.experiment_id == experiment_id)).all()) == 1
        assert db.get(Position, {"experiment_id": experiment_id, "symbol": symbol}) is not None

        sell_target = position.average_price * 1.02 / (1 - experiment.config["fee_rate"])
        at_target = Candle(
            symbol=symbol, interval="5m", timestamp=now + 600, open=sell_target - 1,
            high=sell_target + 1, low=sell_target - 2, close=sell_target,
            volume=1000, currency="EUR", eurusd=1, source="test",
        )
        worker._process_momentum_target(db, experiment, symbol, [*history, at_target], at_target, now + 600)
        trades = db.scalars(select(Trade).where(Trade.experiment_id == experiment_id).order_by(Trade.id)).all()
        assert [trade.side for trade in trades] == ["buy", "sell"]
        assert trades[-1].price >= sell_target
        assert experiment.realized_pnl > 0
        assert db.get(Position, {"experiment_id": experiment_id, "symbol": symbol}) is None


def test_two_candle_profit_buys_on_rise_and_sells_only_net_profitable_fall():
    init_db()
    now = time.time()
    symbol = "TWO-CANDLE"
    experiment_id = "two-candle-profit"

    def candle(timestamp, close):
        return Candle(
            symbol=symbol, interval="1m", timestamp=timestamp,
            open=close, high=close, low=close, close=close, volume=1000,
            currency="EUR", eurusd=1, source="test",
        )

    with SessionLocal() as db:
        experiment = Experiment(
            id=experiment_id,
            status="running",
            created_at=now,
            started_at=now,
            initial_balance=1_000,
            cash=1_000,
            realized_pnl=0,
            total_fees=0,
            config={
                "symbols": [symbol],
                "interval": "1m",
                "fee_rate": 0.001,
                "allocation_percent": 80,
                "strategy": "two_candle_profit",
            },
            paused_total_seconds=0,
        )
        db.add(experiment)
        db.flush()

        first = candle(now - 60, 100)
        rising = candle(now, 101)
        worker._process_two_candle_profit(db, experiment, symbol, [first, rising], rising, now)
        position = db.get(Position, {"experiment_id": experiment_id, "symbol": symbol})
        assert position is not None
        first_buy = db.scalar(select(Trade).where(Trade.experiment_id == experiment_id, Trade.side == "buy"))
        assert first_buy is not None
        assert abs(first_buy.notional - 160) < 1e-8

        next_rise = candle(now + 60, 102)
        worker._process_two_candle_profit(
            db, experiment, symbol, [rising, next_rise], next_rise, now + 60
        )
        position = db.get(Position, {"experiment_id": experiment_id, "symbol": symbol})
        trades = db.scalars(select(Trade).where(Trade.experiment_id == experiment_id).order_by(Trade.id)).all()
        assert position is not None
        assert [trade.side for trade in trades] == ["buy", "buy"]
        assert 0 < trades[-1].notional < 200

        below_cost = candle(now + 120, position.average_price * 0.999)
        worker._process_two_candle_profit(
            db, experiment, symbol,
            [candle(now + 60, position.average_price * 1.001), below_cost], below_cost, now + 120
        )
        assert len(db.scalars(select(Trade).where(Trade.experiment_id == experiment_id)).all()) == 2
        assert db.get(Position, {"experiment_id": experiment_id, "symbol": symbol}) is not None

        small_profit_price = position.average_price * 1.003 / (1 - experiment.config["fee_rate"])
        small_profit_fall = candle(now + 180, small_profit_price)
        worker._process_two_candle_profit(
            db, experiment, symbol,
            [candle(now + 120, small_profit_price * 1.01), small_profit_fall], small_profit_fall, now + 180
        )
        assert len(db.scalars(select(Trade).where(Trade.experiment_id == experiment_id)).all()) == 2
        assert db.get(Position, {"experiment_id": experiment_id, "symbol": symbol}) is not None

        minimum_net_sale = position.average_price * (1 + worker.TWO_CANDLE_MIN_NET_PROFIT_PERCENT / 100) / (1 - experiment.config["fee_rate"])
        profitable_fall = candle(now + 240, minimum_net_sale * 1.001)
        worker._process_two_candle_profit(
            db, experiment, symbol,
            [candle(now + 180, minimum_net_sale * 1.01), profitable_fall], profitable_fall, now + 240
        )
        trades = db.scalars(select(Trade).where(Trade.experiment_id == experiment_id).order_by(Trade.id)).all()
        assert [trade.side for trade in trades] == ["buy", "buy", "sell"]
        assert trades[-1].price * (1 - experiment.config["fee_rate"]) >= position.average_price * (1 + worker.TWO_CANDLE_MIN_NET_PROFIT_PERCENT / 100)
        assert experiment.realized_pnl > 0
        assert db.get(Position, {"experiment_id": experiment_id, "symbol": symbol}) is None


@pytest.mark.parametrize(
    ("open_price", "low_price", "close_price", "expected_fill", "catch_up"),
    [
        (195.0, 189.0, 192.0, 190.0, False),
        (180.0, 175.0, 178.0, 180.0, False),
        (195.0, 191.0, 192.0, None, False),
        (195.0, 189.0, 192.0, 190.0, True),
    ],
)
def test_worker_applies_protective_stop_and_respects_gaps(
    monkeypatch, open_price, low_price, close_price, expected_fill, catch_up
):
    init_db()
    now = time.time()
    interval = 300
    current_slot = int(now // interval) * interval
    test_suffix = f"{int(open_price * 10)}-{int(low_price * 10)}-{int(catch_up)}"
    symbol = f"STOP-{test_suffix}"
    experiment_id = f"worker-stop-{test_suffix}"
    previous_timestamp = current_slot - (3 if catch_up else 2) * interval
    stop_candle_timestamp = previous_timestamp + interval
    candles = [{
        "timestamp": previous_timestamp,
        "open": 200.0,
        "high": 201.0,
        "low": 199.0,
        "close": 200.0,
        "volume": 1000,
    }, {
        "timestamp": stop_candle_timestamp,
        "open": open_price,
        "high": max(open_price, close_price),
        "low": low_price,
        "close": close_price,
        "volume": 1200,
    }]
    if catch_up:
        candles.append({
            "timestamp": stop_candle_timestamp + interval,
            "open": close_price,
            "high": close_price + 3,
            "low": close_price,
            "close": close_price + 2,
            "volume": 1300,
        })
    snapshot = {
        "symbol": symbol,
        "currency": "EUR",
        "market_state": "REGULAR",
        "market_time": now,
        "price_native": candles[-1]["close"],
        "candles": candles,
    }
    monkeypatch.setattr(worker, "fetch_chart", lambda *_args: snapshot)
    monkeypatch.setattr(worker, "fetch_eurusd", lambda: 1.0)

    with SessionLocal() as db:
        db.add(Experiment(
            id=experiment_id,
            status="running",
            created_at=now,
            started_at=now,
            initial_balance=3000,
            cash=1000,
            realized_pnl=0,
            total_fees=0,
            config={
                "symbols": [symbol],
                "interval": "5m",
                "fast_period": 2,
                "slow_period": 3,
                "fee_rate": 0.001,
                "allocation_percent": 80,
                "stop_loss_percent": 5,
                "grid_step_percent": 10,
                "duration_days": 30,
            },
            paused_total_seconds=0,
        ))
        db.add(Candle(
            symbol=symbol,
            interval="5m",
            timestamp=previous_timestamp,
            open=200,
            high=201,
            low=199,
            close=200,
            volume=1000,
            currency="EUR",
            eurusd=1.0,
            source="test",
        ))
        db.add(Position(
            experiment_id=experiment_id,
            symbol=symbol,
            quantity=10,
            average_price=200,
            opened_at=now - interval,
        ))
        db.add(SignalCursor(
            experiment_id=experiment_id,
            symbol=symbol,
            candle_timestamp=previous_timestamp,
        ))
        db.commit()

    worker._poll_market()

    with SessionLocal() as db:
        trades = db.scalars(select(Trade).where(Trade.experiment_id == experiment_id)).all()
        position = db.get(Position, {"experiment_id": experiment_id, "symbol": symbol})
        if expected_fill is None:
            assert trades == []
            assert position is not None
        else:
            assert len(trades) == 1
            assert trades[0].side == "sell"
            assert trades[0].price == expected_fill
            assert position is None


def test_worker_uses_only_latest_candle_after_catch_up(monkeypatch):
    init_db()
    now = time.time()
    interval = 300
    current_slot = int(now // interval) * interval
    symbol = "CATCHUP"
    closes = [10.0, 10.0, 10.0, 10.0, 11.0, 8.0, 14.0]
    first_timestamp = current_slot - len(closes) * interval
    candle_data = [
        {
            "timestamp": first_timestamp + index * interval,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1000,
        }
        for index, close in enumerate(closes)
    ]
    cursor_timestamp = candle_data[3]["timestamp"]
    monkeypatch.setattr(worker, "fetch_chart", lambda *_args: {
        "symbol": symbol,
        "currency": "EUR",
        "market_state": "REGULAR",
        "market_time": now,
        "price_native": closes[-1],
        "candles": candle_data,
    })
    monkeypatch.setattr(worker, "fetch_eurusd", lambda: 1.0)

    with SessionLocal() as db:
        db.add(Experiment(
            id="worker-catchup-test",
            status="running",
            created_at=now,
            started_at=now,
            initial_balance=1000,
            cash=1000,
            realized_pnl=0,
            total_fees=0,
            config={
                "symbols": [symbol],
                "interval": "5m",
                "fast_period": 2,
                "slow_period": 4,
                "fee_rate": 0.001,
                "allocation_percent": 80,
                "duration_days": 30,
            },
            paused_total_seconds=0,
        ))
        db.add_all([
            Candle(
                symbol=symbol,
                interval="5m",
                timestamp=candle["timestamp"],
                open=candle["open"],
                high=candle["high"],
                low=candle["low"],
                close=candle["close"],
                volume=candle["volume"],
                currency="EUR",
                eurusd=1.0,
                source="test",
            )
            for candle in candle_data[:4]
        ])
        db.add(SignalCursor(
            experiment_id="worker-catchup-test",
            symbol=symbol,
            candle_timestamp=cursor_timestamp,
        ))
        db.commit()

    worker._poll_market()

    with SessionLocal() as db:
        trades = db.scalars(
            select(Trade)
            .where(Trade.experiment_id == "worker-catchup-test")
            .order_by(Trade.executed_at)
        ).all()
        position = db.get(Position, {"experiment_id": "worker-catchup-test", "symbol": symbol})
        assert [trade.side for trade in trades] == ["buy"]
        assert trades[0].price == 14.0
        assert position is not None
