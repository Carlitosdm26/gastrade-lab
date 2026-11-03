import io
import json
from urllib.parse import parse_qs, urlparse

import market_data


def test_fetch_chart_uses_newer_candle_than_stale_market_metadata(monkeypatch):
    stale_market_time = 1_700_000_000
    current_candle_time = stale_market_time + 60
    payload = {
        "chart": {
            "result": [{
                "meta": {
                    "symbol": "NVDA",
                    "currency": "USD",
                    "regularMarketPrice": 100,
                    "regularMarketTime": stale_market_time,
                },
                "timestamp": [current_candle_time],
                "indicators": {"quote": [{
                    "open": [110],
                    "high": [112],
                    "low": [109],
                    "close": [111],
                    "volume": [500],
                }]},
            }],
        }
    }
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(market_data, "urlopen", fake_urlopen)

    result = market_data.fetch_chart("NVDA", "1m", "7d")

    query = parse_qs(urlparse(requests[0].full_url).query)
    assert query["includePrePost"] == ["true"]
    assert result["price_native"] == 111
    assert result["market_time"] == current_candle_time