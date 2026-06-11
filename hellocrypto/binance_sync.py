"""Idempotent import of the real Binance account history.

Reconstructs the authoritative trade log + funding base from Binance so the
dashboard reflects the *actual* account, not only the agent's own trades:

  - pre-existing / manual fills are inserted (``session_id=None``,
    ``reason="Trade manuel (importé Binance)"``) so they show up in the real
    catch-all view and feed position/PnL/cash reconstruction;
  - the agent's own historical trades (recorded before order-id capture) are
    fuzzy-matched and tagged with their Binance ``orderId`` so re-runs dedupe;
  - net USDC deposits − withdrawals become the real capital base.

Dedupe is keyed on ``binance_order_id`` (one per order, shared by its fills) —
``myTrades`` returns one row per fill, which we aggregate per order to mirror
the single row the agent records per order. Re-running imports nothing new.

The live trading path is intentionally untouched: order ids are assigned only
here, at import time.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

log = logging.getLogger(__name__)

_QTY_TOL   = 0.005           # 0.5% relative qty tolerance for fuzzy match
_TIME_TOL  = 10 * 60 * 1000  # 10 min, in ms, between DB ts and fill time


def _aggregate_fills(fills: list[dict]) -> dict[str, dict]:
    """Group raw ``myTrades`` fills into one logical order each (keyed orderId).

    Sums qty / quote value / commission across fills; a market order can fill in
    several pieces but the agent records it as a single trade.
    """
    orders: dict[str, dict] = {}
    for f in fills:
        oid = str(f.get("orderId"))
        g = orders.get(oid)
        if g is None:
            g = orders[oid] = {
                "order_id":         oid,
                "qty":              0.0,
                "quote":            0.0,
                "commission":       0.0,
                "commission_asset": f.get("commissionAsset", "USDC"),
                "is_buyer":         bool(f.get("isBuyer")),
                "time":             int(f.get("time", 0)),
            }
        g["qty"]        += float(f.get("qty", 0) or 0)
        g["quote"]      += float(f.get("quoteQty", 0) or 0)
        g["commission"] += float(f.get("commission", 0) or 0)
        g["time"]        = max(g["time"], int(f.get("time", 0)))
    return orders


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).replace(tzinfo=None).isoformat()


def _iso_to_ms(iso: str) -> int | None:
    # DB timestamps are naive UTC (datetime.utcnow); interpret them as UTC so
    # comparisons against Binance epoch-ms fill times don't drift by the local
    # offset.
    try:
        dt = datetime.fromisoformat(iso.replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _find_match(unmatched: list[dict], symbol: str, order: dict) -> dict | None:
    """Find an already-recorded agent trade that this Binance order corresponds
    to (same symbol, same side, qty within tolerance, close in time)."""
    want_buy = order["is_buyer"]
    best = None
    best_dt = None
    for t in unmatched:
        if t.get("symbol") != symbol:
            continue
        is_buy = "BUY" in str(t.get("action", "")).upper()
        if is_buy != want_buy:
            continue
        q = float(t.get("qty") or 0)
        if q <= 0:
            continue
        if abs(q - order["qty"]) / max(q, 1e-9) > _QTY_TOL:
            continue
        t_ms = _iso_to_ms(str(t.get("timestamp", "")))
        if t_ms is None:
            continue
        dt = abs(t_ms - order["time"])
        if dt > _TIME_TOL:
            continue
        if best_dt is None or dt < best_dt:
            best, best_dt = t, dt
    return best


def _fee_usdc(commission: float, asset: str, price: float, symbol: str) -> float:
    """Best-effort conversion of a fill commission to USDC."""
    if not commission:
        return 0.0
    if asset in ("USDC", "USDT", "BUSD"):
        return round(commission, 6)
    base = symbol.replace("USDC", "").replace("USDT", "").replace("BUSD", "")
    if asset == base and price:
        return round(commission * price, 6)
    if asset == "BNB":
        try:
            from .api import get_ticker
            return round(commission * get_ticker("BNBUSDC"), 6)
        except Exception:
            return 0.0
    return 0.0


def _candidate_symbols(watchlist: list[str]) -> list[str]:
    """Watchlist ∪ symbols for any currently-held base asset, so manual buys of
    a coin outside the watchlist are still captured."""
    syms = set(watchlist or [])
    try:
        from .api import api_get
        balances = api_get("/api/v3/account", signed=True).get("balances", [])
        for b in balances:
            asset = b.get("asset", "")
            if asset in ("USDC", "USDT", "BUSD"):
                continue
            if (float(b.get("free", 0) or 0) + float(b.get("locked", 0) or 0)) > 0:
                syms.add(f"{asset}USDC")
    except Exception:
        log.warning("/account scan failed; importing watchlist symbols only",
                    exc_info=True)
    return sorted(syms)


def import_trades(watchlist: list[str]) -> dict:
    """Import/reconcile real fills from Binance. Returns a counts summary."""
    from db.store import load_history, save_trade, update_trade_binance_id

    existing = load_history(mode="real", limit=5000)
    seen_oids = {str(t["binance_order_id"]) for t in existing
                 if t.get("binance_order_id")}
    unmatched = [t for t in existing
                 if not t.get("binance_order_id") and t.get("id") is not None]

    inserted = backfilled = skipped = 0
    for symbol in _candidate_symbols(watchlist):
        try:
            from .api import get_my_trades
            fills = get_my_trades(symbol)
        except Exception:
            log.warning("myTrades fetch failed for %s", symbol, exc_info=True)
            continue
        for oid, g in _aggregate_fills(fills).items():
            if oid in seen_oids:
                skipped += 1
                continue
            match = _find_match(unmatched, symbol, g)
            if match is not None:
                update_trade_binance_id(match["id"], oid)
                unmatched.remove(match)
                seen_oids.add(oid)
                backfilled += 1
                continue
            qty   = g["qty"]
            price = g["quote"] / qty if qty else 0.0
            save_trade(
                action="BUY" if g["is_buyer"] else "SELL",
                symbol=symbol,
                amount=round(g["quote"], 2),
                price=round(price, 8),
                reason="Trade manuel (importé Binance)",
                fee=_fee_usdc(g["commission"], g["commission_asset"], price, symbol),
                fee_asset=g["commission_asset"],
                qty=round(qty, 8),
                mode="real",
                session_id=None,
                binance_order_id=oid,
                timestamp=_ms_to_iso(g["time"]),
            )
            seen_oids.add(oid)
            inserted += 1
    return {"inserted": inserted, "backfilled": backfilled, "skipped": skipped}


def sync_funding() -> dict:
    """Refresh the real capital base from USDC deposits/withdrawals and persist
    it in ``agent_state.real_net_deposits``. Returns the funding breakdown."""
    from .api import get_usdc_funding
    funding = get_usdc_funding()
    try:
        from db.store import set_state
        set_state("real_net_deposits", funding)
    except Exception:
        log.warning("Could not persist real_net_deposits", exc_info=True)
    return funding


def real_capital_base() -> float | None:
    """Net USDC deposited (deposits − withdrawals), the real-mode PnL baseline.

    None when the Binance funding sync has never run — callers then fall back to
    the legacy manual budget.
    """
    try:
        from db.store import get_state
        d = get_state("real_net_deposits")
        if isinstance(d, dict) and d.get("net") is not None:
            return float(d["net"])
    except Exception:
        log.warning("Could not read real_net_deposits", exc_info=True)
    return None


def sync_all(watchlist: list[str]) -> dict:
    """Full reconcile: import trades, then refresh the funding base."""
    trades = import_trades(watchlist)
    funding = sync_funding()
    log.info("[BINANCE SYNC] trades=%s funding=%s", trades, funding)
    return {"trades": trades, "funding": funding}
