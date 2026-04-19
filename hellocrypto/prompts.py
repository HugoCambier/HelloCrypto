"""Claude AI prompts for the HelloCrypto trading agent.

All text sent to the model lives here so tweaking strategy
never requires touching business logic.
"""

# System prompt: defines the model's persona and output contract.
SYSTEM = (
    "Tu es un agent de trading crypto quantitatif expérimenté sur Binance. "
    "Tu combines analyse technique multi-timeframe (RSI, MACD, Bollinger, ATR, SMA) "
    "et analyse de sentiment (Fear & Greed, dominance BTC). "
    "Tu recherches la CONFLUENCE de signaux avant d'agir. "
    "Tu gères le risque en priorité : preservation du capital > rendement. "
    "Tu réponds UNIQUEMENT en JSON valide, sans markdown, sans commentaire."
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
) -> str:
    """Return the user-turn prompt for a market analysis cycle.

    Args:
        market_data:       Formatted string of current prices per symbol.
        positions:         Open positions {symbol: {qty, avg_price}}.
        cash:              Available USDC balance.
        budget:            Total initial budget (risk reference).
        risk_level:        Integer 1-10 controlling aggressiveness.
        recent_decisions:  Last N LLM decisions [{sentiment, summary, actions}].
        fear_greed:        Fear & Greed index dict {value, label} or None.
        btc_dominance:     BTC dominance % or None.
        scores:            Pre-computed signal scores {symbol: int} or None.
        prices:            Current market prices {symbol: float} or None.
        peak_prices:       Highest price since entry per symbol or None.
        cooldown_map:      {symbol: last_sell_cycle} or None.
        total_fees:        Cumulative trading fees.
        cycle:             Current cycle number.

    Returns:
        Prompt string ready to be sent as the ``user`` message.
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

    # ── Risk profile ─────────────────────────────────────────────────────────
    if risk_level <= 3:
        profile_name = "PRUDENT"
        profile_desc = (
            "Tu opères en mode PRUDENT.\n"
            "- Univers : concentre-toi sur les grandes capitalisations stables (BTC, ETH, BNB). "
            "Ignore les altcoins à faible capitalisation ou à forte volatilité.\n"
            "- Horizon : privilégie exclusivement les positions LONG (plusieurs jours/semaines). "
            "N'ouvre pas de trade si tu ne peux pas imaginer le tenir au moins 3-5 jours.\n"
            "- Entrée : n'achète QUE sur signal technique très solide (score >= 8/10, "
            "tendance daily haussière confirmée, RSI entre 35 et 55, MACD haussier).\n"
            "- Confluence requise : au moins 3 indicateurs alignés (RSI + tendance + MACD ou Bollinger).\n"
            "- Taille : petites positions (max {max_pct}% du cash). Préfère rester en cash plutôt "
            "que de forcer un trade incertain.\n"
            "- Vente : ne vends que si stop-loss atteint ou si le signal se retourne clairement "
            "(MACD cross baissier + RSI > 70)."
        )
    elif risk_level <= 6:
        profile_name = "MODÉRÉ"
        profile_desc = (
            "Tu opères en mode MODÉRÉ.\n"
            "- Univers : mix de grandes caps (BTC, ETH) et d'altcoins de mid-cap avec un historique "
            "de liquidité correct. Évite les micro-caps.\n"
            "- Horizon : priorité aux positions MEDIUM (1-3 jours), quelques SHORT acceptés si le "
            "signal est clairement à court terme (RSI micro-tendance fort).\n"
            "- Entrée : achat sur signal convaincant (score >= 7/10). Vérifie la confluence : "
            "au moins 2 indicateurs alignés.\n"
            "- Taille : positions raisonnables (max {max_pct}% du cash). Diversifie sur 2-{max_assets} actifs.\n"
            "- MACD : utilise le croisement MACD/Signal pour confirmer timing d'entrée.\n"
            "- Bollinger : achat près de la bande basse si tendance haussière, vente près de la bande haute.\n"
            "- Sur-trading : évite d'enchaîner les trades. Chaque transaction coûte des frais."
        )
    else:
        profile_name = "AGRESSIF"
        profile_desc = (
            "Tu opères en mode AGRESSIF.\n"
            "- Univers : toutes les cryptos du portefeuille sont éligibles, y compris les altcoins "
            "volatils à fort momentum.\n"
            "- Horizon : priorité aux positions SHORT (scalping intraday, quelques heures) et MEDIUM. "
            "Réagis vite aux signaux RSI micro-tendance et aux retournements de tendance court terme.\n"
            "- Entrée : achat dès que le score >= 6/10 avec un momentum clair.\n"
            "- MACD : entre quand histogramme passe positif, sors quand il repasse négatif.\n"
            "- Bollinger : joue les squeeze (largeur < 3%) pour anticiper les breakouts.\n"
            "- ATR : utilise l'ATR pour calibrer tes objectifs — TP à 1.5x ATR, SL à 1x ATR.\n"
            "- Taille : positions larges (jusqu'à {max_pct}% du cash).\n"
            "- Vente : prends tes profits rapidement dès que RSI micro-tendance > 75 "
            "ou histogramme MACD se retourne."
        )
    profile_desc = profile_desc.format(max_pct=max_pct, max_assets=max_assets)

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
Analyse les données de marché suivantes et décide des actions à effectuer.

DONNÉES MARCHÉ ACTUELLES :
{market_data}
{ctx_section}{cooldown_section}
GUIDE D'INTERPRÉTATION DES INDICATEURS :

RSI (Relative Strength Index) :
- RSI(1h) < 30 : zone de survente — signal d'achat potentiel SI confirmé par d'autres indicateurs
- RSI(1h) 30-45 : zone de faiblesse — surveiller rebond
- RSI(1h) 45-55 : neutre
- RSI(1h) 55-70 : zone de force — tendance haussière en cours
- RSI(1h) > 70 : surachat — signal de vente potentiel, attention au retournement
- RSI(court terme) : réactif, prioritaire pour le timing d'entrée/sortie

MACD (Moving Average Convergence Divergence) :
- MACD > Signal (histogramme > 0) : momentum haussier — confirme les achats
- MACD < Signal (histogramme < 0) : momentum baissier — confirme les ventes
- Croisement MACD/Signal haussier = signal d'achat | baissier = signal de vente
- Histogramme qui s'amplifie = momentum qui accélère | qui diminue = momentum qui faiblit

Bandes de Bollinger [lower | middle | upper] :
- Prix près de la bande basse + tendance haussière = rebond probable → achat
- Prix près de la bande haute + RSI > 70 = excès → vente
- Largeur < 3% = squeeze (compression de volatilité) → breakout imminent, prépare-toi
- Largeur > 8% = forte volatilité → élargis tes stops
- La bande du milieu (SMA20) sert de support/résistance dynamique

ATR (Average True Range) :
- ATR élevé = forte volatilité intraday → adapte la taille de position (réduis si trop élevé)
- ATR faible = marché calme → bon pour du range trading

SMA (Simple Moving Averages) :
- SMA7 > SMA25 = tendance haussière | SMA7 < SMA25 = tendance baissière
- Prix au-dessus de SMA25 = support haussier | en-dessous = résistance baissière
- TendanceJ (daily) est plus fiable que la tendance 1h pour la direction générale

Spread et Volume :
- Spread > 0.05% = liquidité faible, réduit le rendement net — évite les gros ordres
- Volume élevé confirme les mouvements de prix | volume faible = mouvement suspect

CONFLUENCE (CRUCIAL) :
Un signal isolé ne suffit JAMAIS. Cherche la confluence :
- ACHAT FORT : RSI < 40 + MACD croisement haussier + prix sur support Bollinger + tendanceJ haussière
- ACHAT MODÉRÉ : score >= {buy_thr} + au moins 2 signaux alignés
- VENTE FORTE : RSI > 70 + MACD croisement baissier + prix sur résistance + tendanceJ baissière
- HOLD : signaux contradictoires → ne fais rien, le meilleur trade est parfois de ne pas trader
{scores_section}
GRILLE DE DÉCISION :
- Score >= {buy_thr}/10 → candidat à l'achat | Score <= {sell_thr}/10 → candidat à la vente | Sinon HOLD
- Composantes du score : RSI (±3 pts) + tendance 1h (±1) + tendance daily (±2) + volatilité (±1)

POSITIONS OUVERTES :
{pos_str}
{portfolio_section}{decisions_section}
PROFIL DE TRADING ({profile_name} — {risk_level}/10) :
{profile_desc}

RÈGLES :
- Max {max_pct}% du cash par trade (ajusté par RSI)
- Max {max_assets} actifs en portefeuille simultanément
- Chaque transaction coûte 0.1% de frais — calcule si le gain potentiel justifie les frais
- Justifie chaque décision en mentionnant les indicateurs convergents (pas un seul)
- Pour les positions existantes : évalue si la thèse d'entrée est toujours valide

Réponds UNIQUEMENT en JSON valide (structure exacte ci-dessous) :
{{
  "actions": [
    {{"type": "buy",  "symbol": "BTCUSDC", "usdc_amount": 20, "score": 8, "horizon": "short|medium|long", "reason": "RSI survente + MACD croisement haussier + support Bollinger"}},
    {{"type": "sell", "symbol": "SOLUSDC", "qty": 0.5,        "score": 2, "reason": "RSI surachat + histogramme MACD négatif + bande haute atteinte"}},
    {{"type": "hold", "symbol": "ETHUSDC",                    "score": 5, "reason": "Signaux contradictoires, RSI neutre, MACD plat"}}
  ],
  "market_sentiment": "bullish|neutral|bearish",
  "summary": "Résumé de la situation en une phrase"
}}

Pour les actions "buy", le champ "horizon" est obligatoire :
- "short"  : trade de quelques heures (scalping, signal RSI micro-tendance)
- "medium" : trade de 1-3 jours (tendance daily + momentum MACD)
- "long"   : position de plusieurs jours/semaines (tendance forte + fondamentaux macro)"""


SYSTEM_ANALYSIS = (
    "Tu es un analyste crypto senior avec 10 ans d'expérience. "
    "Tu fournis des analyses techniques et fondamentales objectives. "
    "Tu réponds UNIQUEMENT en JSON valide, sans markdown, sans commentaire. "
    "Tes projections sont des estimations techniques, pas des conseils financiers."
)


def build_market_analysis(
    market_data: str,
    fear_greed: dict | None = None,
    btc_dominance: float | None = None,
    scores: dict | None = None,
) -> str:
    """Return the user-turn prompt for a full market analysis with scenarios."""
    ctx_parts = []
    if fear_greed:
        ctx_parts.append(f"Fear & Greed: {fear_greed['value']}/100 ({fear_greed['label']})")
    if btc_dominance is not None:
        ctx_parts.append(f"Dominance BTC: {btc_dominance:.1f}%")
    ctx_section = ("\nCONTEXTE GLOBAL :\n" + "\n".join(f"- {p}" for p in ctx_parts) + "\n") if ctx_parts else ""

    scores_section = ""
    if scores:
        score_lines = [f"  {sym}: {s}/10" for sym, s in scores.items()]
        scores_section = "\nSCORES TECHNIQUES :\n" + "\n".join(score_lines) + "\n"

    return f"""\
Effectue une analyse complète de marché pour chacun des actifs suivants.

DONNÉES DE MARCHÉ (prix | Δ1h | Δ24h | volume | RSI(14) | tendance | volatilité) :
{market_data}
{ctx_section}{scores_section}
Pour CHAQUE actif, fournis :
1. Un sentiment (bullish / neutral / bearish) avec niveau de confiance 1-10
2. Un résumé de la situation en 1-2 phrases
3. Les niveaux clés (support / résistance proches)
4. Jusqu'à 3 scénarios (bear / base / bull) avec :
   - Probabilité estimée en % (les 3 doivent sommer à 100)
   - Projection de prix à 24h, 7 jours et 30 jours
   - Déclencheur principal du scénario (1 phrase)

Réponds UNIQUEMENT en JSON valide (structure exacte) :
{{
  "global_sentiment": "bullish|neutral|bearish",
  "market_summary": "Résumé du marché global en 2 phrases",
  "analyses": [
    {{
      "symbol": "BTCUSDC",
      "current_price": 95000,
      "sentiment": "bullish",
      "confidence": 8,
      "summary": "Résumé en 1-2 phrases",
      "support": 90000,
      "resistance": 100000,
      "action": "buy|sell|hold",
      "action_reason": "Justification concise de la recommandation en 1 phrase",
      "scenarios": [
        {{
          "name": "bear",
          "probability": 25,
          "price_24h": 90000,
          "price_7j": 85000,
          "price_30j": 80000,
          "trigger": "Rupture du support..."
        }},
        {{
          "name": "base",
          "probability": 50,
          "price_24h": 95000,
          "price_7j": 98000,
          "price_30j": 105000,
          "trigger": "Consolidation et reprise..."
        }},
        {{
          "name": "bull",
          "probability": 25,
          "price_24h": 100000,
          "price_7j": 110000,
          "price_30j": 125000,
          "trigger": "Breakout décisif au-dessus..."
        }}
      ]
    }}
  ]
}}"""


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

Réponds en JSON valide UNIQUEMENT, structure exacte:
{{"symbol":"{symbol}","sentiment":"bullish|neutral|bearish","confidence":7,"summary":"1 phrase","support":0,"resistance":0,"action":"buy|sell|hold","action_reason":"justification en 1 phrase","scenarios":[{{"name":"bear","probability":25,"price_24h":0,"price_7j":0,"price_30j":0,"trigger":"..."}},{{"name":"base","probability":50,"price_24h":0,"price_7j":0,"price_30j":0,"trigger":"..."}},{{"name":"bull","probability":25,"price_24h":0,"price_7j":0,"price_30j":0,"trigger":"..."}}]}}"""
