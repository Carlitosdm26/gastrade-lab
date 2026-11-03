from fastapi.testclient import TestClient
from sqlalchemy import inspect

import app as app_module
from database import Quote, QuoteHistory, SessionLocal, engine


def test_api_creates_schema_and_controls_experiment(monkeypatch):
    monkeypatch.setattr(app_module, "start_worker", lambda: None)
    monkeypatch.setattr(app_module, "stop_worker", lambda: None)
    payload = {
        "starting_balance": 10_000,
        "symbols": ["AAPL", "MSFT"],
        "fast_period": 7,
        "slow_period": 21,
        "interval": "5m",
        "fee_rate": 0.001,
        "allocation_percent": 80,
        "duration_days": 30,
    }

    with TestClient(app_module.app) as client:
        names = set(inspect(engine).get_table_names())
        assert {"experiments", "candles", "quotes", "quote_history", "positions", "trades", "equity_snapshots"} <= names
        assert client.get("/api/health").json() == {"status": "ok", "database": "connected"}

        started = client.post("/api/sessions", json=payload)
        assert started.status_code == 200
        experiment = started.json()["experiment"]
        assert experiment["status"] == "running"
        assert experiment["total_value"] == 10_000
        experiment_id = experiment["id"]

        paused = client.post(f"/api/sessions/{experiment_id}/pause")
        assert paused.json()["experiment"]["status"] == "paused"
        resumed = client.post(f"/api/sessions/{experiment_id}/resume")
        assert resumed.json()["experiment"]["status"] == "running"
        stopped = client.post(f"/api/sessions/{experiment_id}/stop")
        assert stopped.json()["experiment"]["status"] == "stopped"
        history = client.get("/api/sessions").json()[0]
        assert history["status"] == "stopped"
        assert history["config"]["symbols"] == ["AAPL", "MSFT"]
        assert history["total_value"] == 10_000


def test_api_rejects_fast_period_not_less_than_slow():
    with TestClient(app_module.app) as client:
        response = client.post("/api/sessions", json={
            "starting_balance": 1_000,
            "symbols": ["AAPL"],
            "fast_period": 21,
            "slow_period": 21,
            "interval": "5m",
            "fee_rate": 0.001,
            "allocation_percent": 80,
            "duration_days": 30,
        })
    assert response.status_code == 422


def test_api_includes_recent_quote_history():
    with TestClient(app_module.app) as client:
        with SessionLocal() as db:
            db.add(Quote(
                symbol="HIST",
                price_eur=12.5,
                price_native=13.0,
                currency="USD",
                eurusd=1.04,
                market_state="REGULAR",
                market_time=1_700_000_060,
                fetched_at=1_700_000_060,
                source="test",
            ))
            db.add(QuoteHistory(
                symbol="HIST",
                price_eur=12.5,
                market_time=1_700_000_060,
                fetched_at=1_700_000_060,
            ))
            db.commit()

        response = client.get("/api/state")

    quote = next(item for item in response.json()["quotes"] if item["symbol"] == "HIST")
    assert quote["history"] == [{
        "price": 12.5,
        "market_time": 1_700_000_060,
        "fetched_at": 1_700_000_060,
    }]
