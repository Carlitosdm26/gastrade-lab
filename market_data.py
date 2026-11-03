from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


SOURCE_NAME = "Yahoo Finance chart API"
USER_AGENT = os.getenv(
    "MARKET_DATA_USER_AGENT",
    "Mozilla/5.0 (compatible; GastradeLab/1.0; +https://github.com)",
)


class MarketDataError(RuntimeError):
    pass


def fetch_chart(symbol: str, interval: str, period: str = "1mo") -> dict[str, Any]:
    safe_symbol = quote(symbol, safe="")
    query = urlencode({"interval": interval, "range": period, "includePrePost": "true"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{safe_symbol}?{query}"
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise MarketDataError(f"No se pudieron consultar datos de {symbol}: {error}") from error

    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise MarketDataError(f"Yahoo Finance rechazó {symbol}: {chart['error'].get('description', 'error desconocido')}")
    results = chart.get("result") or []
    if not results:
        raise MarketDataError(f"Yahoo Finance no devolvió cotizaciones para {symbol}.")

    result = results[0]
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quote_data = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote_data.get("open") or []
    highs = quote_data.get("high") or []
    lows = quote_data.get("low") or []
    closes = quote_data.get("close") or []
    volumes = quote_data.get("volume") or []
    candles = []
    for index, timestamp in enumerate(timestamps):
        if index >= len(closes) or closes[index] is None:
            continue
        close = float(closes[index])
        if close <= 0:
            continue
        candles.append({
            "timestamp": float(timestamp),
            "open": float(opens[index] if index < len(opens) and opens[index] is not None else close),
            "high": float(highs[index] if index < len(highs) and highs[index] is not None else close),
            "low": float(lows[index] if index < len(lows) and lows[index] is not None else close),
            "close": close,
            "volume": float(volumes[index] if index < len(volumes) and volumes[index] is not None else 0),
        })

    latest_candle = candles[-1] if candles else None
    market_price = meta.get("regularMarketPrice")
    market_time = float(meta.get("regularMarketTime") or 0)
    if latest_candle and (not market_time or latest_candle["timestamp"] > market_time):
        market_price = latest_candle["close"]
        market_time = latest_candle["timestamp"]
    if market_price is None and latest_candle:
        market_price = latest_candle["close"]
    if market_price is None or float(market_price) <= 0:
        raise MarketDataError(f"Yahoo Finance no devolvió un precio válido para {symbol}.")

    return {
        "symbol": meta.get("symbol", symbol),
        "currency": meta.get("currency", "USD"),
        "exchange": meta.get("exchangeName", ""),
        "market_state": meta.get("marketState", "UNKNOWN"),
        "market_time": market_time or (latest_candle["timestamp"] if latest_candle else time.time()),
        "price_native": float(market_price),
        "candles": candles,
    }


def fetch_eurusd() -> float:
    data = fetch_chart("EURUSD=X", "1d", "5d")
    if data["currency"] != "USD" or data["price_native"] <= 0:
        raise MarketDataError("No se pudo obtener el cambio EUR/USD.")
    return data["price_native"]
