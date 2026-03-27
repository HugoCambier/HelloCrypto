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
) -> str:
    """Return the user-turn prompt for a market analysis cycle.

    Args:
        market_data:       Formatted string of current prices per symbol.
        positions:         Open positions {symbol: {qty, avg_price}}.
        cash:              Available USDC balance.
        budget:            Total initial budget (risk reference).
        risk_level:        Integer 1–10 controlling aggressiveness.
        recent_decisions:  Last N LLM decisions [{sentiment, summary, actions}].

    Returns:
        Prompt string ready to be sent as the ``user`` message.
    """
    pos_str = json.dumps(positions, indent=2) if positions else "Aucune position ouverte."

    # Derive concrete rule thresholds from risk level
    max_pct      = 5 + risk_level * 4          # 9 % (risk 1) → 45 % (risk 10)
    max_assets   = max(2, min(risk_level // 2 + 2, 5))   # 2–5 actifs
    if risk_level <= 3:
        stance = "très conservateur : n'achète que sur signal fort et clair, préfère le cash"
    elif risk_level <= 6:
        stance = "modéré : cherche un bon rapport risque/rendement, évite le sur-trading"
    else:
        stance = "agressif : maximise les opportunités, accepte plus de volatilité"

    # Format recent decisions history
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

    return f"""\
Analyse les données de marché suivantes et décide des actions à effectuer.

DONNÉES MARCHÉ ACTUELLES (prix | Δ1h | Δ24h | volume | RSI(14) | tendance SMA7/SMA25) :
{market_data}
{decisions_section}
INTERPRÉTATION DES INDICATEURS :
- RSI < 30 : survente (opportunité d'achat) | RSI > 70 : surachat (envisager vente)
- Tendance haussière : SMA7 > SMA25 | Tendance baissière : SMA7 < SMA25
- Volume élevé confirme la direction du mouvement
- Volatilité24h < 3% : marché calme (range étroit) | > 8% : forte volatilité (risque élevé)

POSITIONS OUVERTES :
{pos_str}

CASH DISPONIBLE : ${cash:.2f} USDC
BUDGET TOTAL : ${budget:.0f} USDC

NIVEAU DE RISQUE : {risk_level}/10 — profil {stance}

RÈGLES :
- N'investis jamais plus de {max_pct} % du cash disponible par trade
- Diversifie sur {max_assets} actifs maximum
- Ne fait pas de sur-trading : chaque transaction coûte des frais et les pertes s'accumulent
- Justifie chaque décision en une phrase concise, en mentionnant l'indicateur principal

Réponds UNIQUEMENT en JSON valide (structure exacte ci-dessous) :
{{
  "actions": [
    {{"type": "buy",  "symbol": "BTCUSDC", "usdc_amount": 20, "reason": "..."}},
    {{"type": "sell", "symbol": "SOLUSDC", "qty": 0.5,        "reason": "..."}},
    {{"type": "hold", "symbol": "ETHUSDC",                    "reason": "..."}}
  ],
  "market_sentiment": "bullish|neutral|bearish",
  "summary": "Résumé de la situation en une phrase"
}}"""
