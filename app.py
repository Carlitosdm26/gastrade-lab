from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from database import (
    EquitySnapshot,
    Experiment,
    MarketStatus,
    Position,
    Quote,
    QuoteHistory,
    SessionLocal,
    Trade,
    init_db,
)
from worker import ASSETS, INTERVAL_SECONDS, start_worker, stop_worker

BASE_DIR = Path(__file__).resolve().parent
VALID_INTERVALS = set(INTERVAL_SECONDS)


class SessionConfig(BaseModel):
    starting_balance: float = Field(ge=100, le=1_000_000)
    symbols: list[str] = Field(min_length=1, max_length=3)
    fast_period: int = Field(ge=2, le=100)
    slow_period: int = Field(ge=3, le=200)
    interval: str
    fee_rate: float = Field(ge=0, le=0.02)
    allocation_percent: float = Field(ge=10, le=100)
    duration_days: int = Field(ge=1, le=45, default=30)

    @model_validator(mode="after")
    def validate_strategy(self):
        if self.slow_period <= self.fast_period:
            raise ValueError("La media lenta debe ser mayor que la rápida.")
        if self.interval not in VALID_INTERVALS:
            raise ValueError("El intervalo de vela no es válido.")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("No se pueden repetir activos.")
        if any(symbol not in ASSETS for symbol in self.symbols):
            raise ValueError("Hay un activo no admitido.")
        return self


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    start_worker()
    yield
    stop_worker()


app = FastAPI(title="GastradeLab", version="1.0.0", lifespan=lifespan)


def _elapsed_seconds(experiment: Experiment, now: float) -> float:
    paused_total = experiment.paused_total_seconds
    if experiment.status == "paused" and experiment.paused_at:
        paused_total += max(0, now - experiment.paused_at)
    end = experiment.completed_at or now
    return max(0, end - experiment.started_at - paused_total)


def _snapshot_row(row: EquitySnapshot) -> dict:
    return {"timestamp": row.timestamp, "value": row.total_value, "cash": row.cash}


def _experiment_summary(db, experiment: Experiment, include_history: bool = True) -> dict:
    quotes = {quote.symbol: quote for quote in db.scalars(select(Quote)).all()}
    positions = db.scalars(select(Position).where(Position.experiment_id == experiment.id)).all()
    holdings_value = sum(position.quantity * quotes.get(position.symbol).price_eur for position in positions if quotes.get(position.symbol))
    total_value = experiment.cash + holdings_value
    profit = total_value - experiment.initial_balance
    history = []
    if include_history:
        rows = db.scalars(
            select(EquitySnapshot)
            .where(EquitySnapshot.experiment_id == experiment.id)
            .order_by(EquitySnapshot.timestamp)
        ).all()
        if len(rows) > 800:
            stride = (len(rows) - 1) / 799
            history = [_snapshot_row(rows[round(index * stride)]) for index in range(799)]
            if rows[-1].timestamp != history[-1]["timestamp"]:
                history.append(_snapshot_row(rows[-1]))
        else:
            history = [_snapshot_row(row) for row in rows]

    trades = db.scalars(
        select(Trade).where(Trade.experiment_id == experiment.id).order_by(Trade.executed_at.desc()).limit(30)
    ).all()
    return {
        "id": experiment.id,
        "status": experiment.status,
        "created_at": experiment.created_at,
        "started_at": experiment.started_at,
        "completed_at": experiment.completed_at,
        "elapsed_seconds": _elapsed_seconds(experiment, time.time()),
        "duration_days": experiment.config["duration_days"],
        "config": experiment.config,
        "initial_balance": experiment.initial_balance,
        "cash": experiment.cash,
        "holdings_value": holdings_value,
        "total_value": total_value,
        "realized_pnl": experiment.realized_pnl,
        "profit": profit,
        "return_percent": profit / experiment.initial_balance * 100 if experiment.initial_balance else 0,
        "total_fees": experiment.total_fees,
        "positions": [
            {
                "symbol": position.symbol,
                "quantity": position.quantity,
                "average_price": position.average_price,
                "price": quotes[position.symbol].price_eur if position.symbol in quotes else 0,
                "value": position.quantity * quotes[position.symbol].price_eur if position.symbol in quotes else 0,
                "pnl": position.quantity * (quotes[position.symbol].price_eur - position.average_price) if position.symbol in quotes else 0,
                "opened_at": position.opened_at,
            }
            for position in positions
        ],
        "trades": [
            {
                "id": trade.id,
                "symbol": trade.symbol,
                "side": trade.side,
                "quantity": trade.quantity,
                "price": trade.price,
                "notional": trade.notional,
                "fee": trade.fee,
                "timestamp": trade.executed_at,
            }
            for trade in trades
        ],
        "history": history,
        "error": experiment.error,
    }


def _build_state(db) -> dict:
    experiment = db.scalar(select(Experiment).order_by(Experiment.created_at.desc()))
    quotes = db.scalars(select(Quote).order_by(Quote.symbol)).all()
    market_status = db.get(MarketStatus, "primary")
    quote_history = {
        quote.symbol: db.scalars(
            select(QuoteHistory)
            .where(QuoteHistory.symbol == quote.symbol)
            .order_by(QuoteHistory.fetched_at.desc(), QuoteHistory.id.desc())
            .limit(12)
        ).all()
        for quote in quotes
    }
    return {
        "source": "Yahoo Finance chart API",
        "market": {
            "last_success_at": market_status.last_success_at if market_status else None,
            "last_error": market_status.last_error if market_status else None,
            "source": market_status.source if market_status else "Yahoo Finance chart API",
        },
        "quotes": [
            {
                "symbol": quote.symbol,
                "name": ASSETS.get(quote.symbol, quote.symbol),
                "price": quote.price_eur,
                "native_price": quote.price_native,
                "currency": quote.currency,
                "market_state": quote.market_state,
                "market_time": quote.market_time,
                "fetched_at": quote.fetched_at,
                "source": quote.source,
                "error": quote.error,
                "history": [
                    {
                        "price": item.price_eur,
                        "market_time": item.market_time,
                        "fetched_at": item.fetched_at,
                    }
                    for item in quote_history[quote.symbol]
                ],
            }
            for quote in quotes
        ],
        "experiment": _experiment_summary(db, experiment) if experiment else None,
    }


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/styles.css")
def styles():
    return FileResponse(BASE_DIR / "styles.css", media_type="text/css")


@app.get("/appClient.js")
def frontend_script():
    return FileResponse(BASE_DIR / "appClient.js", media_type="text/javascript")


@app.get("/api/health")
def health():
    with SessionLocal() as db:
        db.execute(select(1))
    return {"status": "ok", "database": "connected"}


@app.get("/api/state")
def get_state():
    with SessionLocal() as db:
        return _build_state(db)


@app.get("/api/sessions")
def get_sessions():
    with SessionLocal() as db:
        experiments = db.scalars(select(Experiment).order_by(Experiment.created_at.desc()).limit(100)).all()
        history = []
        for item in experiments:
            summary = _experiment_summary(db, item, include_history=False)
            history.append({
                "id": item.id,
                "status": item.status,
                "started_at": item.started_at,
                "completed_at": item.completed_at,
                "initial_balance": item.initial_balance,
                "total_value": summary["total_value"],
                "profit": summary["profit"],
                "return_percent": summary["return_percent"],
                "config": item.config,
                "duration_days": item.config["duration_days"],
            })
        return history


@app.get("/api/sessions/{experiment_id}")
def get_session(experiment_id: str):
    with SessionLocal() as db:
        experiment = db.get(Experiment, experiment_id)
        if experiment is None:
            raise HTTPException(status_code=404, detail="No se encontró la simulación.")
        return _experiment_summary(db, experiment)


@app.post("/api/sessions")
def start_session(config: SessionConfig):
    with SessionLocal() as db:
        active = db.scalar(select(Experiment).where(Experiment.status.in_(["running", "paused"])))
        if active:
            raise HTTPException(status_code=409, detail="Ya existe una simulación activa.")
        now = time.time()
        experiment = Experiment(
            id=str(uuid.uuid4()),
            status="running",
            created_at=now,
            started_at=now,
            initial_balance=config.starting_balance,
            cash=config.starting_balance,
            realized_pnl=0,
            total_fees=0,
            config=config.model_dump(),
            paused_total_seconds=0,
        )
        db.add(experiment)
        db.flush()
        db.add(EquitySnapshot(
            experiment_id=experiment.id,
            timestamp=now,
            cash=config.starting_balance,
            holdings_value=0,
            total_value=config.starting_balance,
        ))
        db.commit()
        db.refresh(experiment)
        return _build_state(db)


@app.post("/api/sessions/{experiment_id}/pause")
def pause_session(experiment_id: str):
    with SessionLocal() as db:
        experiment = db.get(Experiment, experiment_id)
        if experiment is None:
            raise HTTPException(status_code=404, detail="No se encontró la simulación.")
        if experiment.status != "running":
            raise HTTPException(status_code=409, detail="Solo se puede pausar una simulación en curso.")
        experiment.status = "paused"
        experiment.paused_at = time.time()
        db.commit()
        return _build_state(db)


@app.post("/api/sessions/{experiment_id}/resume")
def resume_session(experiment_id: str):
    with SessionLocal() as db:
        experiment = db.get(Experiment, experiment_id)
        if experiment is None:
            raise HTTPException(status_code=404, detail="No se encontró la simulación.")
        if experiment.status != "paused":
            raise HTTPException(status_code=409, detail="Solo se puede reanudar una simulación en pausa.")
        experiment.paused_total_seconds += max(0, time.time() - experiment.paused_at)
        experiment.paused_at = None
        experiment.status = "running"
        db.commit()
        return _build_state(db)


@app.post("/api/sessions/{experiment_id}/stop")
def stop_session(experiment_id: str):
    with SessionLocal() as db:
        experiment = db.get(Experiment, experiment_id)
        if experiment is None:
            raise HTTPException(status_code=404, detail="No se encontró la simulación.")
        if experiment.status not in {"running", "paused"}:
            raise HTTPException(status_code=409, detail="La simulación ya está finalizada.")
        now = time.time()
        if experiment.status == "paused" and experiment.paused_at:
            experiment.paused_total_seconds += max(0, now - experiment.paused_at)
            experiment.paused_at = None
        experiment.status = "stopped"
        experiment.completed_at = now
        db.commit()
        return _build_state(db)
