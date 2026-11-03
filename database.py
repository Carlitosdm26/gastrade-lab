from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import JSON


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/papertrade.db")
url = make_url(DATABASE_URL)
if url.get_backend_name() == "sqlite" and url.database not in (None, ":memory:"):
    Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

engine_options: dict = {"pool_pre_ping": True}
if url.get_backend_name() == "sqlite":
    engine_options["connect_args"] = {"check_same_thread": False, "timeout": 30}
    if url.database in (None, ":memory:"):
        engine_options["poolclass"] = StaticPool

engine = create_engine(DATABASE_URL, **engine_options)

if url.get_backend_name() == "sqlite":
    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


class Base(DeclarativeBase):
    pass


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[float] = mapped_column(Float)
    started_at: Mapped[float] = mapped_column(Float)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_balance: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0)
    total_fees: Mapped[float] = mapped_column(Float, default=0)
    config: Mapped[dict] = mapped_column(JSON)
    paused_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    paused_total_seconds: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Quote(Base):
    __tablename__ = "quotes"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    price_eur: Mapped[float] = mapped_column(Float)
    price_native: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8))
    eurusd: Mapped[float] = mapped_column(Float, default=1)
    market_state: Mapped[str] = mapped_column(String(24), default="UNKNOWN")
    market_time: Mapped[float] = mapped_column(Float, default=0)
    fetched_at: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40), default="Yahoo Finance chart API")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class QuoteHistory(Base):
    __tablename__ = "quote_history"
    __table_args__ = (Index("ix_quote_history_symbol_fetched", "symbol", "fetched_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16))
    price_eur: Mapped[float] = mapped_column(Float)
    market_time: Mapped[float] = mapped_column(Float)
    fetched_at: Mapped[float] = mapped_column(Float)


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", "timestamp", name="uq_candle_symbol_interval_time"),
        Index("ix_candles_symbol_interval_time", "symbol", "interval", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16))
    interval: Mapped[str] = mapped_column(String(8))
    timestamp: Mapped[float] = mapped_column(Float)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0)
    currency: Mapped[str] = mapped_column(String(8))
    eurusd: Mapped[float] = mapped_column(Float, default=1)
    source: Mapped[str] = mapped_column(String(40), default="Yahoo Finance chart API")


class Position(Base):
    __tablename__ = "positions"

    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    quantity: Mapped[float] = mapped_column(Float)
    average_price: Mapped[float] = mapped_column(Float)
    opened_at: Mapped[float] = mapped_column(Float)


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (Index("ix_trades_experiment_time", "experiment_id", "executed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    notional: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float)
    executed_at: Mapped[float] = mapped_column(Float)


class SignalCursor(Base):
    __tablename__ = "signal_cursors"

    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    candle_timestamp: Mapped[float] = mapped_column(Float)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (Index("ix_equity_experiment_time", "experiment_id", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), index=True)
    timestamp: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    holdings_value: Mapped[float] = mapped_column(Float)
    total_value: Mapped[float] = mapped_column(Float)


class MarketStatus(Base):
    __tablename__ = "market_status"

    key: Mapped[str] = mapped_column(String(24), primary_key=True, default="primary")
    last_success_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="Yahoo Finance chart API")


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


@contextmanager
def db_session() -> Iterator:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
