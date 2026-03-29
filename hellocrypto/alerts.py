"""Push notifications via Telegram.

Configure in .env:
    TELEGRAM_BOT_TOKEN  — from @BotFather
    TELEGRAM_CHAT_ID    — target chat / channel ID

Silently no-ops when credentials are absent.
"""
import logging
import os

import requests

log = logging.getLogger(__name__)

_MILESTONES = [-50, -20, -10, -5, 5, 10, 20, 50]
_seen_milestones: set[int] = set()


def _send(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as exc:
        log.warning("Telegram alert failed: %s", exc)


def stop_loss(symbol: str, loss_pct: float, usdc_received: float) -> None:
    _send(f"🔴 *STOP-LOSS* `{symbol}`\nPerte: `{loss_pct:.1f}%` | Reçu: `${usdc_received:.2f}` USDC")


def trailing_stop(symbol: str, drop_pct: float, usdc_received: float) -> None:
    _send(f"🟠 *TRAILING STOP* `{symbol}`\nChute depuis pic: `{drop_pct:.1f}%` | Reçu: `${usdc_received:.2f}` USDC")


def performance_milestone(total_value: float, budget: float, pnl_pct: float) -> None:
    global _seen_milestones
    for m in _MILESTONES:
        if m in _seen_milestones:
            continue
        if (m > 0 and pnl_pct >= m) or (m < 0 and pnl_pct <= m):
            sign = "🟢" if m > 0 else "🔴"
            _send(f"{sign} *Milestone PnL {m:+d}%*\nValeur: `${total_value:.2f}` | PnL: `{pnl_pct:+.2f}%`")
            _seen_milestones.add(m)
