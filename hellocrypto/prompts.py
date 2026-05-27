"""Claude AI prompts for the HelloCrypto trading agent.

All text sent to the model lives here so tweaking strategy
never requires touching business logic.
"""

from __future__ import annotations

# JSON schema for a trading decision — used by structured-output adapters
# (Gemini response_schema, Claude tool calling). Keeping it here lets the
# prompt text and the schema stay in sync.
DECISION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "market_sentiment": {
            "type": "string", "enum": ["bullish", "neutral", "bearish"],
        },
        "summary":   {"type": "string"},
        "reasoning": {
            "type": "array", "items": {"type": "string"},
            "description": "3 to 5 short bullets describing aligned/diverging signals.",
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type":        {"type": "string", "enum": ["buy", "sell", "hold"]},
                    "symbol":      {"type": "string"},
                    "usdc_amount": {"type": "number"},
                    "qty":         {"type": "number"},
                    "score":       {"type": "integer"},
                    "confidence":  {"type": "number"},
                    "horizon":     {"type": "string", "enum": ["short", "medium", "long"]},
                    "reason":      {"type": "string"},
                },
                "required": ["type", "symbol", "confidence", "reason"],
            },
        },
    },
    "required": ["market_sentiment", "summary", "actions"],
}

# System prompt: defines the model's persona and output contract.
SYSTEM = (
    "Tu es un agent de trading crypto quantitatif sur Binance spot, "
    "spécialisé dans la confluence de signaux techniques et de sentiment. "
    "Règles : (1) préservation du capital > performance, (2) un signal isolé "
    "ne déclenche jamais un trade — exiger ≥2 indicateurs alignés, (3) chaque "
    "frais (0.1%) doit être justifié par un edge attendu > 0.3%. "
    "Tu réponds UNIQUEMENT en JSON valide, sans markdown ni commentaire."
)


def build_analysis(
    market_data: str,
    positions: dict,
    cash: float,
    budget: float,
    risk_level: int = 3,
    recent_decisions: list | None = None,
    fear_greed: dict | None = None,
    btc_dominance: float | None = None,
    scores: dict | None = None,
    prices: dict | None = None,
    peak_prices: dict | None = None,
    cooldown_map: dict | None = None,
    total_fees: float = 0.0,
    cycle: int = 0,
    playbook_section: str | None = None,
    behavior_section: str | None = None,
    regime_overlay: str | None = None,
) -> str:
    """Return the user-turn prompt for a market analysis cycle.

    The prompt is intentionally dense:
    - No indicator tutorials — the model already knows RSI/MACD/Bollinger.
    - Compact market-data table when callers pass the compact format.
    - Profile descriptions and rules condensed to the discriminant bits.
    - The decision schema is the JSON contract — must include confidence.
    """
    prices = prices or {}
    peak_prices = peak_prices or {}
    cooldown_map = cooldown_map or {}

    max_pct    = 5 + risk_level * 4
    max_assets = max(2, min(risk_level // 2 + 2, 5))

    # ── Positions with unrealized P&L ────────────────────────────────────────
    if positions:
        pos_lines = []
        for sym, pos in positions.items():
            entry = pos["avg_price"]
            qty   = pos["qty"]
            cur   = prices.get(sym)
            if cur:
                pnl_pct  = (cur - entry) / entry * 100
                pnl_usd  = (cur - entry) * qty
                peak     = peak_prices.get(sym, cur)
                from_peak = (cur - peak) / peak * 100 if peak > 0 else 0
                pos_lines.append(
                    f"  {sym}: qty={qty:.6f} entry=${entry:,.4f} now=${cur:,.4f} "
                    f"PnL={pnl_pct:+.2f}% (${pnl_usd:+.2f}) "
                    f"pic=${peak:,.4f} ({from_peak:+.1f}% depuis pic)"
                )
            else:
                pos_lines.append(f"  {sym}: qty={qty:.6f} entry=${entry:,.4f} (prix actuel indisponible)")
        pos_str = "\n".join(pos_lines)
    else:
        pos_str = "  Aucune position ouverte."

    # ── Portfolio health ─────────────────────────────────────────────────────
    portfolio_val = sum(
        pos["qty"] * prices.get(sym, pos["avg_price"])
        for sym, pos in (positions or {}).items()
    )
    total_val    = cash + portfolio_val
    cash_pct     = (cash / total_val * 100) if total_val > 0 else 100
    exposure_pct = 100 - cash_pct
    global_pnl   = total_val - budget
    global_pnl_pct = (global_pnl / budget * 100) if budget > 0 else 0

    portfolio_section = (
        f"\nSANTÉ DU PORTEFEUILLE :\n"
        f"  Valeur totale: ${total_val:,.2f} | PnL global: {global_pnl_pct:+.2f}% (${global_pnl:+.2f})\n"
        f"  Cash: ${cash:,.2f} ({cash_pct:.0f}%) | Exposition: {exposure_pct:.0f}%\n"
        f"  Frais cumulés: ${total_fees:,.4f} | Cycle: {cycle}\n"
    )

    # ── Cooldown info ────────────────────────────────────────────────────────
    if cooldown_map and cycle > 0:
        active_cooldowns = {
            sym: cycle - last_sell
            for sym, last_sell in cooldown_map.items()
            if cycle - last_sell < 5  # only show recent cooldowns
        }
        if active_cooldowns:
            cd_lines = [f"  {sym}: vendu il y a {ago} cycles" for sym, ago in active_cooldowns.items()]
            cooldown_section = "\nCOOLDOWNS ACTIFS (ne pas racheter ces symboles) :\n" + "\n".join(cd_lines) + "\n"
        else:
            cooldown_section = ""
    else:
        cooldown_section = ""

    # ── Risk profile (concis) ───────────────────────────────────────────────
    if risk_level <= 3:
        profile_name = "PRUDENT"
        profile_desc = (
            f"- Univers : BTC/ETH/BNB uniquement. Pas d'altcoin volatil.\n"
            f"- Horizon : LONG (≥3-5j). Ne pas ouvrir si tu ne tiendrais pas.\n"
            f"- Entrée : score≥8 + tendance daily haussière + ≥3 signaux alignés (RSI+trend+MACD/BB).\n"
            f"- Sizing : max {max_pct}% cash/trade ; cash > 60% par défaut.\n"
            f"- Sortie : stop déclenché OU retournement clair (MACD↓ + RSI>70)."
        )
    elif risk_level <= 6:
        profile_name = "MODÉRÉ"
        profile_desc = (
            f"- Univers : large caps + altcoins mid-cap liquides.\n"
            f"- Horizon : MEDIUM (1-3j) prioritaire ; SHORT si signal court fort.\n"
            f"- Entrée : score≥7 + ≥2 signaux alignés.\n"
            f"- Sizing : max {max_pct}% cash/trade ; diversifier 2-{max_assets} actifs.\n"
            f"- Sortie : évite le sur-trading, chaque round-trip coûte 0.2% de frais."
        )
    else:
        profile_name = "AGRESSIF"
        profile_desc = (
            f"- Univers : toutes les cryptos de la watchlist.\n"
            f"- Horizon : SHORT (scalping) et MEDIUM ; réagir vite aux retournements.\n"
            f"- Entrée : score≥6 + momentum clair (MACD hist > 0, RSI court ascendant).\n"
            f"- Sizing : max {max_pct}% cash/trade.\n"
            f"- Sortie : TP rapide dès RSI>75 ou MACD hist se retourne ; SL serré (≈1×ATR)."
        )

    # ── Recent decisions history (enriched) ──────────────────────────────────
    if recent_decisions:
        history_lines = []
        for d in recent_decisions:
            sentiment = d.get("market_sentiment", "?")
            summary   = d.get("summary", "")
            actions_parts = []
            for a in d.get("actions", []):
                atype = a.get("type", "")
                if atype not in ("buy", "sell"):
                    continue
                sym    = a.get("symbol", "")
                score  = a.get("score", "?")
                horizon = a.get("horizon", "")
                reason  = a.get("reason", "")
                if atype == "buy":
                    amt = a.get("usdc_amount", "?")
                    actions_parts.append(f"BUY {sym} ${amt} (score:{score}, {horizon}) — {reason}")
                else:
                    qty = a.get("qty", "?")
                    actions_parts.append(f"SELL {sym} qty:{qty} (score:{score}) — {reason}")
            if not actions_parts:
                actions_parts = ["HOLD (aucune action)"]
            history_lines.append(f"- [{sentiment}] {summary}")
            for ap in actions_parts:
                history_lines.append(f"    {ap}")
        decisions_section = (
            "\nDERNIÈRES DÉCISIONS (du plus ancien au plus récent) :\n"
            + "\n".join(history_lines)
            + "\n\nCONSIGNE : Analyse si tes décisions précédentes étaient judicieuses au vu des "
            "données actuelles. Ajuste ta stratégie en conséquence. Ne répète pas une erreur "
            "(ex: achat suivi d'une baisse = signal trop faible). Ne vends pas par panique "
            "si ta thèse reste valide.\n"
        )
    else:
        decisions_section = ""

    # ── Market context (Fear & Greed, BTC dominance) ─────────────────────────
    ctx_parts = []
    if fear_greed:
        fng_val = fear_greed["value"]
        fng_lbl = fear_greed["label"]
        if fng_val < 20:
            fng_hint = "→ PEUR EXTRÊME — les marchés surréagissent, cherche des opportunités contrarian"
        elif fng_val < 35:
            fng_hint = "→ peur — les vendeurs dominent, réduis l'exposition sauf sur signaux très forts"
        elif fng_val < 55:
            fng_hint = "→ neutre — pas de biais macro, concentre-toi sur les signaux techniques"
        elif fng_val < 75:
            fng_hint = "→ avidité — momentum haussier mais risque de correction, taille tes positions"
        else:
            fng_hint = "→ AVIDITÉ EXTRÊME — risque élevé de retournement, protège tes gains"
        ctx_parts.append(f"Fear & Greed: {fng_val}/100 ({fng_lbl}) {fng_hint}")
    if btc_dominance is not None:
        if btc_dominance > 55:
            dom_hint = "→ dominance BTC forte, altcoins sous pression — favorise BTC/ETH"
        elif btc_dominance < 45:
            dom_hint = "→ dominance BTC faible, rotation altcoins probable — opportunités sur altcoins"
        else:
            dom_hint = "→ dominance neutre, pas de rotation marquée"
        ctx_parts.append(f"Dominance BTC: {btc_dominance:.1f}% {dom_hint}")
    ctx_section = ("\nCONTEXTE MARCHÉ GLOBAL :\n" + "\n".join(f"- {p}" for p in ctx_parts) + "\n") if ctx_parts else ""

    # ── Playbook lessons (from past 12mo, regime-conditional) ────────────────
    # Injected right after the regime-defining context so the LLM reads the
    # rules of *this* regime before evaluating the per-symbol signals below.
    playbook_block = f"\n{playbook_section}\n" if playbook_section else ""
    # Behavior lessons (from the agent's own past trades in this regime)
    behavior_block = f"\n{behavior_section}\n" if behavior_section else ""
    # Regime stance overlay — modulates the static risk profile by macro regime
    # (deploy in bull, preserve in bear). Placed right after the profile block.
    regime_block = f"\n{regime_overlay}\n" if regime_overlay else ""

    # ── Pre-computed scores ──────────────────────────────────────────────────
    if scores:
        score_lines = [f"  {sym}: {s}/10" for sym, s in scores.items()]
        scores_section = "\nSCORES DE SIGNAL (calculés automatiquement) :\n" + "\n".join(score_lines) + "\n"
        buy_thr  = max(6, 8 - risk_level // 3)
        sell_thr = min(4, 1 + risk_level // 3)
    else:
        scores_section = ""
        buy_thr  = 7
        sell_thr = 3

    return f"""\
Analyse le marché et décide.

DONNÉES (trend = trend1h/short/d, BB-pos = position du prix dans les Bollinger) :
{market_data}
{ctx_section}{playbook_block}{behavior_block}{cooldown_section}{scores_section}
GRILLE :
- score≥{buy_thr} → candidat BUY | score≤{sell_thr} → candidat SELL | sinon HOLD
- Confluence requise : ≥2 signaux alignés (RSI, MACD hist, BB-pos, trend, score).
- Frais round-trip 0.2 % → ne trade que si l'edge attendu > 0.3 %.

POSITIONS OUVERTES :
{pos_str}
{portfolio_section}{decisions_section}
PROFIL {profile_name} (risk_level {risk_level}/10) :
{profile_desc}
{regime_block}
CONFIDENCE : flottant [0–1] — ton degré de certitude. Sera utilisé pour gater
les trades (seuil applicatif côté serveur, < 0.5 ignoré par défaut) et pour
moduler la taille (×confidence). Sois honnête : 0.9 = setup textbook avec ≥3
signaux convergents ; 0.5 = signal moyen ; <0.5 = doute → préfère HOLD.

Réponds UNIQUEMENT en JSON (structure exacte) :
{{
  "market_sentiment": "bullish|neutral|bearish",
  "summary": "1 phrase",
  "reasoning": ["3-5 bullets décrivant les signaux convergents/divergents"],
  "actions": [
    {{"type":"buy",  "symbol":"BTCUSDC","usdc_amount":20,"score":8,"confidence":0.82,"horizon":"short|medium|long","reason":"RSI 33 + MACD hist+ + BB↓lo + trend H/H/H"}},
    {{"type":"sell", "symbol":"SOLUSDC","qty":0.5,"score":2,"confidence":0.74,"reason":"RSI 78 + MACD hist- + BB↑hi"}},
    {{"type":"hold", "symbol":"ETHUSDC","score":5,"confidence":0.4,"reason":"signaux divergents"}}
  ]
}}

Pour les BUY, "horizon" est obligatoire :
  short=heures (scalping/RSI court), medium=1-3j (trend+MACD), long=>1sem (fondamentaux+trend daily)."""


SYSTEM_ANALYSIS = (
    "Tu es un analyste crypto senior avec 10 ans d'expérience. "
    "Tu fournis des analyses techniques et fondamentales objectives. "
    "Tu réponds UNIQUEMENT en JSON valide, sans markdown, sans commentaire. "
    "Tes projections sont des estimations techniques, pas des conseils financiers."
)


def build_market_analysis_single(
    symbol: str,
    data_line: str,
    fear_greed: dict | None = None,
    btc_dominance: float | None = None,
    score: int | None = None,
) -> str:
    """Lightweight single-symbol prompt -- reliable with small local models."""
    ctx = []
    if fear_greed:
        ctx.append(f"Fear&Greed {fear_greed['value']}/100 ({fear_greed['label']})")
    if btc_dominance is not None:
        ctx.append(f"BTC dom {btc_dominance:.1f}%")
    if score is not None:
        ctx.append(f"score technique {score}/10")
    ctx_str = " | ".join(ctx)

    return f"""Analyse {symbol}. Données: {data_line}
Contexte: {ctx_str}

Tu DOIS produire les 3 scénarios (bear/base/bull) avec des prix projetés
réalistes basés sur le prix actuel de {symbol} (jamais 0), des probabilités
qui somment à 100, et des supports / résistances tirés des données. Le
champ trigger est obligatoire (1 phrase).

Réponds en JSON valide UNIQUEMENT, structure exacte (les nombres ci-dessous
sont des exemples — remplace-les par tes propres estimations chiffrées) :
{{"symbol":"{symbol}","sentiment":"bullish|neutral|bearish","confidence":7,"summary":"1 phrase","support":100.0,"resistance":120.0,"action":"buy|sell|hold","action_reason":"justification en 1 phrase","scenarios":[{{"name":"bear","probability":25,"price_24h":98.0,"price_7j":92.0,"price_30j":85.0,"trigger":"..."}},{{"name":"base","probability":50,"price_24h":102.0,"price_7j":105.0,"price_30j":112.0,"trigger":"..."}},{{"name":"bull","probability":25,"price_24h":108.0,"price_7j":118.0,"price_30j":135.0,"trigger":"..."}}]}}"""
