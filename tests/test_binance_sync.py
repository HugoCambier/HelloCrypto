"""Unit tests for the Binance import pure core (no DB / no network)."""
from hellocrypto import binance_sync as bs


def test_aggregate_fills_groups_by_order():
    fills = [
        {"orderId": 1, "qty": "0.01", "quoteQty": "500", "commission": "0.0001",
         "commissionAsset": "BTC", "isBuyer": True, "time": 1700000000000},
        {"orderId": 1, "qty": "0.005", "quoteQty": "250", "commission": "0.00005",
         "commissionAsset": "BTC", "isBuyer": True, "time": 1700000001000},
        {"orderId": 2, "qty": "0.004", "quoteQty": "210", "commission": "0.21",
         "commissionAsset": "USDC", "isBuyer": False, "time": 1700000500000},
    ]
    agg = bs._aggregate_fills(fills)
    assert set(agg) == {"1", "2"}
    assert round(agg["1"]["qty"], 4) == 0.015
    assert round(agg["1"]["quote"], 2) == 750.0
    assert agg["1"]["is_buyer"] is True
    assert agg["1"]["time"] == 1700000001000  # latest fill time wins
    assert agg["2"]["is_buyer"] is False


def test_iso_ms_roundtrip_is_utc():
    ms = 1700000000000
    iso = bs._ms_to_iso(ms)
    assert bs._iso_to_ms(iso) == ms


def test_find_match_by_side_qty_time():
    order = {"qty": 0.015, "is_buyer": True, "time": bs._iso_to_ms("2023-11-14T22:13:20")}
    db = [{"id": 7, "symbol": "BTCUSDC", "action": "BUY", "qty": 0.015,
           "timestamp": "2023-11-14T22:13:20"}]
    assert bs._find_match(db, "BTCUSDC", order) is db[0]


def test_find_match_rejects_wrong_side():
    order = {"qty": 0.015, "is_buyer": False, "time": bs._iso_to_ms("2023-11-14T22:13:20")}
    db = [{"id": 7, "symbol": "BTCUSDC", "action": "BUY", "qty": 0.015,
           "timestamp": "2023-11-14T22:13:20"}]
    assert bs._find_match(db, "BTCUSDC", order) is None


def test_find_match_rejects_far_qty_and_time():
    t = bs._iso_to_ms("2023-11-14T22:13:20")
    far_qty = {"qty": 0.030, "is_buyer": True, "time": t}
    far_time = {"qty": 0.015, "is_buyer": True, "time": t + 3600_000}
    db = [{"id": 7, "symbol": "BTCUSDC", "action": "BUY", "qty": 0.015,
           "timestamp": "2023-11-14T22:13:20"}]
    assert bs._find_match(db, "BTCUSDC", far_qty) is None
    assert bs._find_match(db, "BTCUSDC", far_time) is None


def test_fee_usdc_conversions():
    assert bs._fee_usdc(0.5, "USDC", 100.0, "BTCUSDC") == 0.5
    # base-asset fee converts via fill price
    assert bs._fee_usdc(0.001, "BTC", 50000.0, "BTCUSDC") == 50.0
    # unknown asset → 0 (best effort, never crashes)
    assert bs._fee_usdc(1.0, "DOGE", 1.0, "BTCUSDC") == 0.0
