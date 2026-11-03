import time

from sqlalchemy import select

import worker
from database import Candle, Experiment, Position, SessionLocal, SignalCursor, Trade, init_db


def test_history_period_respects_yahoo_interval_limits():
    assert worker.history_period("1m") == "7d"
    assert worker.history_period("5m") == "1mo"
    assert worker.history_period("1d") == "1y"


def test_worker_trades_only_after_a_new_completed_candle(monkeypatch):
    init_db()
    now = time.time()
    interval = 300
    current_slot = int(now // interval) * interval
    closes = [10.0, 9.0, 8.0, 7.0, 8.0]
    candles = [
        {"timestamp": current_slot - (len(closes) - index + 1) * interval, "open": close, "high": close, "low": close, "close": close, "volume": 1000}
        for index, close in enumerate(closes)
    ]
    snapshot = {
        "symbol": "AAPL",
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
                "symbols": ["AAPL"],
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
        position = db.get(Position, {"experiment_id": "worker-test", "symbol": "AAPL"})
        assert len(trades) == 1
        assert trades[0].side == "buy"
        assert position is not None
        assert abs(position.quantity - 79.92) < 1e-8
        assert abs(db.get(Experiment, "worker-test").cash - 200) < 1e-8
        assert db.scalar(select(Candle.id).where(Candle.symbol == "AAPL", Candle.timestamp == next_candle["timestamp"])) is not None


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
