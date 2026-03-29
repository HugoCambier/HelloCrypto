"""Claude AI prompts for the HelloCrypto trading agent.

All text sent to the model lives here so tweaking strategy
never requires touching business logic.
"""

import json

# System prompt: defines the model's persona and output contract.
SYSTEM = (
    "Tu es un agent de trading crypto expérimenté sur Binance. "
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
) -> str:
    """Return the user-turn prompt for a market analysis cycle.

    Args:
        market_data:       Formatted string of current prices per symbol.
        positions:         Open positions {symbol: {qty, avg_price}}.
        cash:              Available USDC balance.
        budget:            Total initial budget (risk reference).
        risk_level:        Integer 1–10 controlling aggressiveness.
        recent_decisions:  Last N LLM decisions [{sentiment, summary, actions}].
        fear_greed:        Fear & Greed index dict {value, label} or None.
        btc_dominance:     BTC dominance % or None.
        scores:            Pre-computed signal scores {symbol: int} or None.

    Returns:
        Prompt string ready to be sent as the ``user`` message.
    """
    pos_str = json.dumps(positions, indent=2) if positions else "Aucune position ouverte."
    max_pct      = 5 + risk_level * 4
    max_assets   = max(2, min(risk_level // 2 + 2, 5))
    if risk_level <= 3:
        stance = "très conservateur : n'achète que sur signal fort et clair, préfère le cash"
    elif risk_level <= 6:
        stance = "modéré : cherche un bon rapport risque/rendement, évite le sur-trading"
    else:
        stance = "agressif : maximise les opportunités, accepte plus de volatilité"

    # Recent decisions history
    if recent_decisions:
        history_lines = []
        for d in recent_decisions:
            actions_str = ", ".join(
                f"{a['type'].upper()} {a.get('symbol','')}" + (f" ${a['usdc_amount']}" if a.get('usdc_amount') else "")
                for a in d.get("actions", []) if a.get("type") in ("buy", "sell")
            ) or "HOLD"
            history_lines.append(f"- [{d.get('sentiment','?')}] {d.get('summary','')} → {actions_str}")
        decisions_section = "\nDERNIÈRES DÉCISIONS (du plus ancien au plus récent) :\n" + "\n".join(history_lines) + "\n"
    else:
        decisions_section = ""

    # Market context (Fear & Greed, BTC dominance)
    ctx_parts = []
    if fear_greed:
        fng_val = fear_greed["value"]
        fng_lbl = fear_greed["label"]
        fng_hint = "→ peur extrême, opportunité potentielle" if fng_val < 25 else ("→ avidité extrême, prudence" if fng_val > 75 else "")
        ctx_parts.append(f"Fear & Greed: {fng_val}/100 ({fng_lbl}) {fng_hint}".strip())
    if btc_dominance is not None:
        dom_hint = "→ altcoins défavorisés" if btc_dominance > 55 else ("→ rotation altcoins probable" if btc_dominance < 45 else "")
        ctx_parts.append(f"Dominance BTC: {btc_dominance:.1f}% {dom_hint}".strip())
    ctx_section = ("\nCONTEXTE MARCHÉ GLOBAL :\n" + "\n".join(f"- {p}" for p in ctx_parts) + "\n") if ctx_parts else ""

    # Pre-computed scores
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

DONNÉES MARCHÉ ACTUELLES (prix | Δ1h | Δ24h | volume | RSI(1h) | tendance 1h | volatilité | RSI(court terme) | tendance court terme | TendanceJ | spread) :
{market_data}
{ctx_section}{decisions_section}
INTERPRÉTATION DES INDICATEURS :
- RSI(1h) < 30 : survente | RSI(1h) > 70 : surachat
- RSI(court terme) : signal micro-tendance en temps réel — prioritaire sur RSI(1h) pour les entrées/sorties
- Tendance court terme haussière + RSI(court terme) < 40 → signal d'achat fort
- Tendance court terme baissière + RSI(court terme) > 60 → signal de vente fort
- TendanceJ = tendance journalière (plus fiable pour la direction générale)
- Spread élevé (> 0.05%) : liquidité faible, spread réduit ton rendement net
- Volatilité24h < 3% : marché calme | > 8% : forte volatilité (risque élevé)
{scores_section}
GRILLE DE DÉCISION :
- Score ≥ {buy_thr}/10 → candidat à l'achat | Score ≤ {sell_thr}/10 → candidat à la vente | Sinon HOLD
- Composantes du score : RSI (±3 pts) + tendance 1h (±1) + tendance daily (±2) + volatilité (±1)

POSITIONS OUVERTES :
{pos_str}

CASH DISPONIBLE : ${cash:.2f} USDC
BUDGET TOTAL : ${budget:.0f} USDC

NIVEAU DE RISQUE : {risk_level}/10 — profil {stance}

RÈGLES :
- N'investis jamais plus de {max_pct} % du cash disponible par trade (ajusté par RSI dynamiquement)
- Diversifie sur {max_assets} actifs maximum
- Ne fait pas de sur-trading : chaque transaction coûte des frais et les pertes s'accumulent
- Justifie chaque décision en une phrase concise, en mentionnant l'indicateur principal
- Inclure le score calculé (ou estimé) dans chaque action

Réponds UNIQUEMENT en JSON valide (structure exacte ci-dessous) :
{{
  "actions": [
    {{"type": "buy",  "symbol": "BTCUSDC", "usdc_amount": 20, "score": 8, "reason": "..."}},
    {{"type": "sell", "symbol": "SOLUSDC", "qty": 0.5,        "score": 2, "reason": "..."}},
    {{"type": "hold", "symbol": "ETHUSDC",                    "score": 5, "reason": "..."}}
  ],
  "market_sentiment": "bullish|neutral|bearish",
  "summary": "Résumé de la situation en une phrase"
}}"""


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
    """Lightweight single-symbol prompt — reliable with small local models."""
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
