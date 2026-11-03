from strategy import crossover_signal, execute_trade, moving_average, portfolio_value


def test_moving_average_uses_last_values():
    assert moving_average([2, 4, 6], 2) == 5
    assert moving_average([2, 4], 3) is None


def test_crossover_signal_detects_confirmed_crosses():
    assert crossover_signal([10, 10, 9, 8, 7, 9, 11], 2, 3) == "buy"
    assert crossover_signal([11, 9, 8, 9, 10, 8, 6], 2, 3) == "sell"
    assert crossover_signal([1, 2, 3], 2, 3) == "hold"


def test_buy_and_sell_include_fees_in_cash_and_pnl():
    portfolio = {"cash": 1_000.0, "holdings": {}, "realized_pnl": 0.0, "total_fees": 0.0, "trades": []}
    buy = execute_trade(portfolio, "AAPL", "buy", 100.0, 0.01, 500.0, "t1")
    assert buy is not None
    assert portfolio["cash"] == 500.0
    assert buy["fee"] == 5.0
    assert abs(portfolio["holdings"]["AAPL"]["quantity"] - 4.95) < 1e-10

    sell = execute_trade(portfolio, "AAPL", "sell", 120.0, 0.01, timestamp="t2")
    assert sell is not None
    assert abs(portfolio["cash"] - 1_088.06) < 1e-8
    assert abs(portfolio["realized_pnl"] - 88.06) < 1e-8
    assert portfolio["holdings"] == {}
    assert abs(portfolio["total_fees"] - 10.94) < 1e-8


def test_portfolio_value_includes_cash_and_open_positions():
    portfolio = {"cash": 500.0, "holdings": {"AAPL": {"quantity": 2.0}}}
    assert portfolio_value(portfolio, {"AAPL": 10.0}) == 520.0
