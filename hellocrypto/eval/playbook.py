"""Playbook — distill the journal report into regime-keyed trading guidance.

Input  : raw journal output (pattern → regime → horizon stats)
Output : ``data/playbook.json``, regime-keyed, with two ranked lists per regime:
         - ``favored`` : patterns whose net edge (gross mean − round-trip fees)
                          exceeds the configured threshold with enough samples
         - ``avoid``   : patterns the model would be tempted to take but whose
                          net edge is negative OR whose MAE dwarfs the MFE
                          (asymmetric drawdown — the typical trap)

Plus a ``format_playbook_section`` helper that turns one regime's slice into
a few prompt-ready lines for the decision LLM.

The playbook is intentionally small: <10 patterns per regime, each one line.
The size budget is "10 lines of prompt at most" — anything fancier dilutes
the signal the model can act on.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime
from pathlib import Path

from .journal import run_full_analysis
from .patterns import PATTERN_SIDES

log = logging.getLogger(__name__)

DEFAULT_PLAYBOOK_PATH = Path("data/playbook.json")

# Tunable thresholds — surfaced in the playbook metadata for traceability.
DEFAULTS = {
    "fee_pct":        0.2,    # Binance spot round-trip
    "min_edge_pct":   0.3,    # net edge floor to qualify as 'favored'
    "min_samples":    50,     # bucket dropped if smaller
    "favored_winrate": 0.53,  # min win rate to qualify as favored (vs ~0.50 random)
    "horizon_h":      24,     # primary horizon used for ranking
    "trap_mae_threshold": -3.0,  # MAE worse than this flags an asymmetric trap
}


# ── Edge computation ──────────────────────────────────────────────────────────

def _net_edge(mean_pct: float, side: str, fee_pct: float) -> float:
    """Net expected return after round-trip fees, sign-flipped for shorts.

    For a long pattern: gross +1.2% gain - 0.2% fees = +1.0% net edge.
    For a short pattern: gross -0.8% (price drop) treated as +0.8% gain - fees.
    """
    gross = mean_pct if side == "long" else -mean_pct
    return round(gross - fee_pct, 3)


def _conviction(net_edge: float, n: int) -> float:
    """Composite ranking score — edge weighted by sample size.

    Used only for sorting within favored/avoid lists; not exposed to the LLM.
    Higher n shrinks the standard error → more confidence in the edge.
    """
    if n <= 0:
        return -math.inf
    return net_edge * math.sqrt(n / 100)


# ── Classification ────────────────────────────────────────────────────────────

def _classify(
    pattern_name: str,
    side: str,
    regime_stats: dict,
    cfg: dict,
) -> str | None:
    """Return 'favored', 'avoid', or None for the (pattern, regime) cell."""
    h_key = f"h{cfg['horizon_h']}"
    h     = regime_stats.get(h_key, {})
    n     = h.get("n", 0)
    if n < cfg["min_samples"]:
        return None

    mean_pct = h.get("mean_pct", 0.0)
    win_rate = h.get("win_rate", 0.0)
    mae      = regime_stats.get("mae_24h_mean")
    mfe      = regime_stats.get("mfe_24h_mean")

    net_edge = _net_edge(mean_pct, side, cfg["fee_pct"])

    # For 'short' patterns, win = price drops. So win_rate >= threshold still
    # means the same thing: pattern fires AND outcome happens.
    if net_edge >= cfg["min_edge_pct"] and win_rate >= cfg["favored_winrate"]:
        return "favored"

    # 'Avoid' = the pattern would tempt the LLM to act, but data says no.
    # Two ways to flag this:
    #   (a) negative net edge after fees
    #   (b) MAE deeper than ~2× MFE (asymmetric drawdown trap)
    if net_edge < 0:
        return "avoid"
    if mae is not None and mfe is not None and mae < cfg["trap_mae_threshold"] and abs(mae) > 2 * mfe:
        return "avoid"
    return None


def _build_lesson(
    pattern_name: str,
    side: str,
    regime: str,
    regime_stats: dict,
    classification: str,
    cfg: dict,
) -> str:
    """One-line natural-language lesson, prompt-ready."""
    h_key = f"h{cfg['horizon_h']}"
    h     = regime_stats[h_key]
    n     = h["n"]
    mean_pct = h["mean_pct"]
    win_rate = h["win_rate"]
    mae      = regime_stats.get("mae_24h_mean")
    mfe      = regime_stats.get("mfe_24h_mean")
    net      = _net_edge(mean_pct, side, cfg["fee_pct"])

    side_tag = "LONG" if side == "long" else "SHORT"
    base = (
        f"{pattern_name} ({side_tag}) in {regime}: "
        f"n={n}, h{cfg['horizon_h']} mean {mean_pct:+.2f}% "
        f"(net {net:+.2f}%), win {win_rate*100:.0f}%"
    )
    if mae is not None and mfe is not None:
        base += f", MAE {mae:+.2f}% / MFE {mfe:+.2f}%"
    if classification == "favored":
        base += " → favored"
    else:
        base += " → trap (avoid)"
    return base


# ── Per-regime distillation ───────────────────────────────────────────────────

def build_playbook(
    journal_report: dict,
    cfg: dict | None = None,
) -> dict:
    """Convert raw journal output into the regime-keyed playbook."""
    cfg = {**DEFAULTS, **(cfg or {})}

    # First pass: classify every (pattern, regime) cell. Group by regime.
    by_regime: dict[str, dict[str, list]] = {}
    for pattern_name, pattern_data in journal_report.items():
        side = PATTERN_SIDES.get(pattern_name, "long")
        for regime, regime_stats in pattern_data["by_regime"].items():
            if regime.startswith("_"):
                continue  # skip _all and _thin aggregates
            cls = _classify(pattern_name, side, regime_stats, cfg)
            if cls is None:
                continue

            h_key = f"h{cfg['horizon_h']}"
            h     = regime_stats[h_key]
            net   = _net_edge(h["mean_pct"], side, cfg["fee_pct"])

            entry = {
                "pattern":   pattern_name,
                "side":      side,
                "n":         h["n"],
                "mean_pct":  h["mean_pct"],
                "net_edge_pct": net,
                "win_rate":  h["win_rate"],
                "mae_24h":   regime_stats.get("mae_24h_mean"),
                "mfe_24h":   regime_stats.get("mfe_24h_mean"),
                "conviction": round(_conviction(net, h["n"]), 3),
                "lesson":    _build_lesson(pattern_name, side, regime, regime_stats, cls, cfg),
            }

            slot = by_regime.setdefault(regime, {"favored": [], "avoid": []})
            slot[cls].append(entry)

    # Second pass: rank each list by conviction (descending magnitude)
    for slot in by_regime.values():
        slot["favored"].sort(key=lambda e: -e["conviction"])
        slot["avoid"].sort(key=lambda e:  e["conviction"])  # most-negative first

    n_total = sum(p["n_matches"] for p in journal_report.values())
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "config":       cfg,
        "n_pattern_matches_total": n_total,
        "regimes":      sorted(by_regime.keys()),
        "by_regime":    by_regime,
    }


# ── Live regime derivation ────────────────────────────────────────────────────

def fng_bucket(value: int | None) -> str | None:
    """F&G value → bucket label. None when value unknown."""
    if value is None:
        return None
    if value < 35:
        return "fear"
    if value > 65:
        return "greed"
    return "neutral"


def btc_trend_bucket(trend_1d: str | None) -> str | None:
    """French trend label → bucket. None when unknown."""
    if trend_1d == "haussier":
        return "bull"
    if trend_1d == "baissier":
        return "bear"
    if trend_1d == "neutre":
        return "range"
    return None


def current_regime(
    fear_greed: dict | None,
    btc_trend_1d: str | None,
) -> str:
    """Map live F&G + BTC daily trend to a playbook regime key (e.g. 'fear+bear').

    Returns 'na+na' when either input is missing — the playbook lookup will
    miss and ``format_playbook_section`` will return '' so the prompt stays
    clean (no spurious lessons).
    """
    fng_b = fng_bucket(fear_greed.get("value") if fear_greed else None) or "na"
    btc_b = btc_trend_bucket(btc_trend_1d) or "na"
    return f"{fng_b}+{btc_b}"


# ── Prompt-ready formatter ────────────────────────────────────────────────────

def format_playbook_section(
    playbook: dict,
    regime: str,
    max_favored: int = 4,
    max_avoid: int = 4,
) -> str:
    """Compact text block for the decision LLM prompt.

    Returns an empty string when the regime has no actionable patterns —
    callers should fall back to the regime-agnostic prompt in that case.
    """
    slot = playbook.get("by_regime", {}).get(regime)
    if not slot or (not slot.get("favored") and not slot.get("avoid")):
        return ""

    lines = [f"LEÇONS PLAYBOOK pour régime [{regime}] (12mo backfill, n={playbook['n_pattern_matches_total']}):"]
    if slot["favored"]:
        lines.append("À FAVORISER :")
        for e in slot["favored"][:max_favored]:
            lines.append(f"  ✓ {e['lesson']}")
    if slot["avoid"]:
        lines.append("À ÉVITER (pièges identifiés) :")
        for e in slot["avoid"][:max_avoid]:
            lines.append(f"  ✗ {e['lesson']}")
    return "\n".join(lines)


# ── Persistence ───────────────────────────────────────────────────────────────

def save_playbook(playbook: dict, path: Path = DEFAULT_PLAYBOOK_PATH) -> Path:
    """Persist playbook to DB (authoritative) AND file (local artefact).

    DB is the source of truth in prod (Vercel functions have a read-only
    FS), but writing the file in parallel keeps local dev/inspection
    ergonomic. DB-write failures don't abort — the file copy is enough
    for local-only setups.
    """
    payload = json.dumps(playbook, indent=2, ensure_ascii=False, default=str)
    # DB write — best effort
    try:
        from db.store import set_state
        set_state("playbook", playbook)
    except Exception:
        log.exception("save_playbook: DB write failed, file-only persistence")
    # File write — atomic via tmp+rename
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload)
        tmp.replace(path)
    except Exception:
        log.exception("save_playbook: file write failed, DB-only persistence")
    return path


def load_playbook(path: Path = DEFAULT_PLAYBOOK_PATH) -> dict | None:
    """Return the playbook — DB first (prod authoritative), file as fallback."""
    try:
        from db.store import get_state
        db_pb = get_state("playbook")
        if db_pb is not None:
            return db_pb
    except Exception:
        log.debug("load_playbook: DB read failed, falling back to file", exc_info=True)
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except Exception:
        log.exception("Failed to read playbook at %s", path)
        return None


# Module-level cache. The decision cycle is the hot path; we don't want to
# refetch the playbook every cycle. Re-validate at most once per minute by
# comparing the ``generated_at`` of the stored copy vs the cached one.
_PLAYBOOK_CACHE: dict | None = None
_PLAYBOOK_LAST_CHECK: float = 0.0
_PLAYBOOK_REVALIDATE_SEC = 60.0


def _cached_playbook(path: Path = DEFAULT_PLAYBOOK_PATH) -> dict | None:
    global _PLAYBOOK_CACHE, _PLAYBOOK_LAST_CHECK
    import time
    now = time.time()
    if _PLAYBOOK_CACHE is not None and now - _PLAYBOOK_LAST_CHECK < _PLAYBOOK_REVALIDATE_SEC:
        return _PLAYBOOK_CACHE
    pb = load_playbook(path)
    _PLAYBOOK_CACHE = pb
    _PLAYBOOK_LAST_CHECK = now
    return _PLAYBOOK_CACHE


def regime_aware_min_confidence(
    playbook: dict | None,
    regime: str,
    base_min: float,
    *,
    bounds: float = 0.2,
    min_pattern_matches: int = 1000,
    edge_strong_pct: float = 1.0,
) -> float:
    """Adjust the confidence gate based on the playbook strength of a regime.

    The intuition:
      - Regime with **0 favored patterns AND substantial data** → harder gate
        (the playbook says nothing works here, demand higher conviction)
      - Regime with **a favored pattern of net edge ≥ ``edge_strong_pct``** →
        softer gate (the playbook says strong edges exist, don't choke them)
      - Anything else → no change

    Safeguards against overfitting:
      - Requires ``min_pattern_matches`` total pattern matches in the playbook
        (not in the regime — the whole playbook) before applying any change
      - Variation is bounded to ±``bounds`` around ``base_min``
      - Returns ``base_min`` unchanged when playbook is absent or thin

    Returns the adjusted ``min_confidence`` value in [0, 1].
    """
    if not playbook:
        return base_min
    total_matches = playbook.get("n_pattern_matches_total", 0)
    if total_matches < min_pattern_matches:
        return base_min
    slot = playbook.get("by_regime", {}).get(regime)
    if slot is None:
        return base_min

    favored = slot.get("favored", [])
    if not favored:
        # No edge in this regime → tighten the gate
        return min(1.0, base_min + bounds)

    best_edge = max(e.get("net_edge_pct", 0) for e in favored)
    if best_edge >= edge_strong_pct:
        # Strong edge available → loosen the gate (don't reject good setups)
        return max(0.0, base_min - bounds)

    return base_min


def section_for_cycle(
    fear_greed: dict | None,
    market_raw: dict | None,
    path: Path = DEFAULT_PLAYBOOK_PATH,
) -> str:
    """Convenience entry point for the decision cycle.

    Loads (cached) playbook → derives the current regime from F&G + BTC daily
    trend → returns the prompt-ready section. Returns '' on any miss so the
    caller can chain it directly into ``build_analysis(..., playbook_section=...)``.
    """
    pb = _cached_playbook(path)
    if not pb:
        return ""
    btc_trend_1d = None
    if market_raw and "BTCUSDC" in market_raw:
        btc_trend_1d = market_raw["BTCUSDC"].get("trend_1d")
    regime = current_regime(fear_greed, btc_trend_1d)
    return format_playbook_section(pb, regime)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate the trading playbook from backfilled snapshots.")
    parser.add_argument("--source",       default="backfill", choices=("backfill", "live"))
    parser.add_argument("--symbols",      default="", help="Comma-list; default = all")
    parser.add_argument("--min-samples",  type=int,   default=DEFAULTS["min_samples"])
    parser.add_argument("--min-edge",     type=float, default=DEFAULTS["min_edge_pct"])
    parser.add_argument("--fee-pct",      type=float, default=DEFAULTS["fee_pct"])
    parser.add_argument("--out",          default=str(DEFAULT_PLAYBOOK_PATH))
    parser.add_argument("--log-level",    default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = {
        **DEFAULTS,
        "min_samples":  args.min_samples,
        "min_edge_pct": args.min_edge,
        "fee_pct":      args.fee_pct,
    }
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or None

    log.info("Running journal analysis (this loads & enriches the backfill)…")
    journal = run_full_analysis(symbols=symbols, source=args.source,
                                min_samples=cfg["min_samples"])

    log.info("Distilling journal into playbook…")
    playbook = build_playbook(journal, cfg=cfg)

    out_path = Path(args.out)
    save_playbook(playbook, out_path)
    log.info("Playbook written to %s — %d regimes covered",
             out_path, len(playbook["by_regime"]))

    # Sanity dump: how many favored / avoid per regime
    for regime in sorted(playbook["by_regime"]):
        slot = playbook["by_regime"][regime]
        log.info("  [%s] favored=%d, avoid=%d",
                 regime, len(slot["favored"]), len(slot["avoid"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
