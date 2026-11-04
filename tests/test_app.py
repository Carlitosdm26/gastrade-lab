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
        "name": "Prueba AAPL y MSFT",
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
        assert experiment["name"] == "Prueba AAPL y MSFT"
        assert experiment["config"]["name"] == "Prueba AAPL y MSFT"
        assert experiment["config"]["stop_loss_percent"] == 5
        assert experiment["config"]["grid_step_percent"] == 2
        assert experiment["config"]["take_profit_percent"] == 2
        assert experiment["config"]["grid_tranches"] == 4
        experiment_id = experiment["id"]

        paused = client.post(f"/api/sessions/{experiment_id}/pause")
        assert paused.json()["experiment"]["status"] == "paused"
        resumed = client.post(f"/api/sessions/{experiment_id}/resume")
        assert resumed.json()["experiment"]["status"] == "running"
        stopped = client.post(f"/api/sessions/{experiment_id}/stop")
        assert stopped.json()["experiment"]["status"] == "stopped"
        history = client.get("/api/sessions").json()[0]
        assert history["status"] == "stopped"
        assert history["name"] == "Prueba AAPL y MSFT"
        assert history["config"]["symbols"] == ["AAPL", "MSFT"]
        assert history["total_value"] == 10_000


def test_api_allows_multiple_independent_active_experiments(monkeypatch):
    monkeypatch.setattr(app_module, "start_worker", lambda: None)
    monkeypatch.setattr(app_module, "stop_worker", lambda: None)
    payload = {
        "starting_balance": 1_000,
        "symbols": ["AAPL"],
        "fast_period": 7,
        "slow_period": 21,
        "interval": "5m",
        "fee_rate": 0.001,
        "allocation_percent": 80,
        "duration_days": 7,
    }

    with TestClient(app_module.app) as client:
        first = client.post("/api/sessions", json=payload)
        second = client.post("/api/sessions", json={**payload, "starting_balance": 2_000})

        assert first.status_code == 200
        assert second.status_code == 200
        assert len(second.json()["experiments"]) == 2
        first_id = first.json()["experiment"]["id"]
        second_id = second.json()["experiment"]["id"]
        sessions = {session["id"]: session for session in client.get("/api/sessions").json()}
        assert sessions[first_id]["status"] == "running"
        assert sessions[second_id]["status"] == "running"

        paused = client.post(f"/api/sessions/{first_id}/pause")
        sessions = {session["id"]: session for session in client.get("/api/sessions").json()}
        assert paused.status_code == 200
        assert sessions[first_id]["status"] == "paused"
        assert sessions[second_id]["status"] == "running"

        resumed = client.post(f"/api/sessions/{first_id}/resume")
        stopped = client.post(f"/api/sessions/{second_id}/stop")
        assert resumed.status_code == 200
        assert stopped.status_code == 200
        sessions = {session["id"]: session for session in client.get("/api/sessions").json()}
        assert sessions[first_id]["status"] == "running"
        assert sessions[second_id]["status"] == "stopped"

        additional_ids = []
        for index in range(app_module.MAX_ACTIVE_EXPERIMENTS - 1):
            additional = client.post("/api/sessions", json={**payload, "starting_balance": 1_000 + index * 100})
            assert additional.status_code == 200
            additional_ids.append(additional.json()["experiment"]["id"])
        overflow = client.post("/api/sessions", json=payload)
        assert overflow.status_code == 409
        assert "límite" in overflow.json()["detail"]
        for experiment_id in [first_id, *additional_ids]:
            stopped = client.post(f"/api/sessions/{experiment_id}/stop")
            assert stopped.status_code == 200


def test_api_rejects_fast_period_not_less_than_slow(monkeypatch):
    monkeypatch.setattr(app_module, "start_worker", lambda: None)
    monkeypatch.setattr(app_module, "stop_worker", lambda: None)
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


def test_api_accepts_momentum_target_strategy(monkeypatch):
    monkeypatch.setattr(app_module, "start_worker", lambda: None)
    monkeypatch.setattr(app_module, "stop_worker", lambda: None)
    payload = {
        "starting_balance": 1_000,
        "symbols": ["AAPL"],
        "strategy": "momentum_target",
        "entry_trigger_type": "price",
        "entry_price_eur": 200,
        "interval": "5m",
        "fee_rate": 0.001,
        "allocation_percent": 80,
        "profit_target_percent": 3,
        "duration_days": 7,
    }

    with TestClient(app_module.app) as client:
        response = client.post("/api/sessions", json=payload)
        assert response.status_code == 200
        assert response.json()["experiment"]["config"]["strategy"] == "momentum_target"
        assert response.json()["experiment"]["config"]["profit_target_percent"] == 3


def test_api_requires_price_for_price_entry_trigger(monkeypatch):
    monkeypatch.setattr(app_module, "start_worker", lambda: None)
    monkeypatch.setattr(app_module, "stop_worker", lambda: None)
    with TestClient(app_module.app) as client:
        response = client.post("/api/sessions", json={
            "starting_balance": 1_000,
            "symbols": ["AAPL"],
            "strategy": "momentum_target",
            "entry_trigger_type": "price",
            "interval": "5m",
            "fee_rate": 0.001,
            "allocation_percent": 80,
            "duration_days": 7,
        })
    assert response.status_code == 422


def test_api_accepts_two_candle_profit_only_with_one_minute_interval(monkeypatch):
    monkeypatch.setattr(app_module, "start_worker", lambda: None)
    monkeypatch.setattr(app_module, "stop_worker", lambda: None)
    payload = {
        "starting_balance": 1_000,
        "symbols": ["AAPL"],
        "strategy": "two_candle_profit",
        "interval": "1m",
        "fee_rate": 0.001,
        "allocation_percent": 80,
        "duration_days": 7,
    }

    with TestClient(app_module.app) as client:
        accepted = client.post("/api/sessions", json=payload)
        rejected = client.post("/api/sessions", json={**payload, "interval": "5m"})

    assert accepted.status_code == 200
    assert accepted.json()["experiment"]["config"]["strategy"] == "two_candle_profit"
    assert "Tendencia 1m" in accepted.json()["experiment"]["name"]
    assert "two_candle_buy_percent" not in accepted.json()["experiment"]["config"]
    assert rejected.status_code == 422


def test_favicon_svg_is_served(monkeypatch):
    monkeypatch.setattr(app_module, "start_worker", lambda: None)
    monkeypatch.setattr(app_module, "stop_worker", lambda: None)
    with TestClient(app_module.app) as client:
        response = client.get("/favicon.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in response.content


def test_api_includes_recent_quote_history(monkeypatch):
    monkeypatch.setattr(app_module, "start_worker", lambda: None)
    monkeypatch.setattr(app_module, "stop_worker", lambda: None)
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
