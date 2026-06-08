"""
AMZN Covered Call Dashboard
Flask web app that serves the monitor data as a live dashboard.
Run:  python webapp.py   →  http://localhost:5000
Set ANTHROPIC_API_KEY env var to enable the Claude chat panel.
"""

import os
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)

from flask import Flask, jsonify, request, send_from_directory

from monitor import (
    MAX_LEGS,
    days_to_expiry,
    get_leg_recommendation,
    get_market_data,
    get_option_quotes,
    get_roll_quotes,
    load_positions,
    run_alerts,
)

app = Flask(__name__)


def _leg_from_title(title: str) -> str | None:
    """Return leg tag (e.g. '1M') from position-specific alert titles, or None."""
    m = re.search(r'\((\dM)[,)]', title)
    return m.group(1).upper() if m else None


def _enrich(pos: dict, mkt: dict, quotes: dict) -> dict:
    price   = mkt["price"]
    premium = pos["premium"]
    shares  = pos["contracts"] * 100
    dte     = days_to_expiry(pos["expiry"])
    otm_pct = round((pos["strike"] - price) / price * 100, 2)

    sym = pos.get("option_symbol") or ""
    oq  = quotes.get(sym, {})
    bid = oq.get("bid")
    ask = oq.get("ask")
    mid = oq.get("mid")

    pps  = round(premium - mid, 2) if mid is not None else None
    ppct = round(pps / premium * 100, 1) if pps is not None else None
    tpnl = round(pps * shares, 2) if pps is not None else None

    return {
        "leg":                     pos["leg"].upper(),
        "strike":                  pos["strike"],
        "expiry":                  pos["expiry"],
        "contracts":               pos["contracts"],
        "shares":                  shares,
        "premium":                 premium,
        "total_premium_collected": round(premium * shares, 2),
        "option_symbol":           sym or None,
        "notes":                   pos.get("notes", ""),
        "dte":                     dte,
        "otm_pct":                 otm_pct,
        "current_bid":             bid,
        "current_ask":             ask,
        "current_mid":             mid,
        "profit_per_sh":           pps,
        "profit_pct":              ppct,
        "total_pnl":               tpnl,
        "alerts":                  [],
    }


def _build_chat_system(mkt: dict, positions: list, alerts: list, rec) -> str:
    s = lambda n: "+" if n >= 0 else ""

    pos_lines = []
    for p in positions:
        q = (f"mid ${p['current_mid']}/sh · P&L {s(p['profit_pct'] or 0)}{p['profit_pct'] or '—'}%"
             if p["current_mid"] is not None else "no live quote")
        pos_lines.append(
            f"  • Leg {p['leg']}: ${p['strike']} Call | {p['expiry']} | {p['dte']} DTE | "
            f"{s(p['otm_pct'])}{p['otm_pct']}% OTM | premium ${p['premium']}/sh "
            f"(${p['total_premium_collected']} total) | {q}"
        )

    alert_lines = [f"  • [{a['level']}] {a['title']}" for a in alerts] or ["  • None"]

    rec_block = ""
    if rec:
        earn = " (⚠ spans Jul 30 earnings)" if rec["earnings_overlap"] else ""
        rec_block = (
            f"\nOpen leg opportunity ({rec['open_legs']}/{MAX_LEGS} legs open):\n"
            f"  ${rec['strike']:.0f} Call · {rec['expiry']} · {rec['dte']} DTE · "
            f"{s(rec['otm_pct'])}{rec['otm_pct']}% OTM · mid ${rec['mid']}/sh{earn}"
        )

    pos_text   = "\n".join(pos_lines) if pos_lines else "  • No open positions"
    alert_text = "\n".join(alert_lines)

    return (
        f"You are a concise options trading assistant helping manage AMZN covered calls.\n\n"
        f"Live snapshot ({datetime.now().strftime('%Y-%m-%d %H:%M UTC')}):\n"
        f"  AMZN ${mkt['price']}  ({s(mkt['change_pct'])}{mkt['change_pct']}% today)"
        f" | prev close ${mkt['prev_close']} | 30D IV {mkt['iv'] or 'n/a'}%\n\n"
        f"Open positions ({len(positions)}/{MAX_LEGS}):\n{pos_text}\n\n"
        f"Active alerts:\n{alert_text}"
        f"{rec_block}\n\n"
        f"The user sells 4 contracts (400 shares) per leg. "
        f"Keep answers concise and action-oriented. Reference live data above when relevant."
    )


@app.route("/")
def index():
    return send_from_directory(str(HERE), "index.html")


@app.route("/api/status")
def api_status():
    try:
        positions   = load_positions()
        mkt         = get_market_data()
        quotes      = get_option_quotes(positions)
        roll_quotes = get_roll_quotes(positions)
        alerts      = run_alerts(positions, mkt, quotes, roll_quotes)
        rec         = get_leg_recommendation(positions, mkt)

        enriched   = [_enrich(p, mkt, quotes) for p in positions]
        by_leg     = {ep["leg"]: ep for ep in enriched}
        mkt_alerts = []

        for a in alerts:
            leg = _leg_from_title(a["title"])
            payload = {k: a.get(k, "") for k in ("level", "emoji", "title", "detail", "reco")}
            if leg and leg in by_leg:
                by_leg[leg]["alerts"].append(payload)
            else:
                mkt_alerts.append(payload)

        return jsonify({
            "ok":             True,
            "as_of":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "market":         {k: mkt[k] for k in ("price", "prev_close", "change_pct", "iv")},
            "positions":      enriched,
            "mkt_alerts":     mkt_alerts,
            "recommendation": rec,
            "alert_counts": {
                "risk":  sum(1 for a in alerts if a["level"] == "RISK"),
                "warn":  sum(1 for a in alerts if a["level"] == "WARN"),
                "total": len(alerts),
            },
            "chat_enabled":   bool(os.environ.get("ANTHROPIC_API_KEY")),
        })

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/chat", methods=["POST"])
def api_chat():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"ok": False, "error": "ANTHROPIC_API_KEY not set — restart webapp.py with it exported"}), 400

    req_data = request.json or {}
    messages = req_data.get("messages", [])
    if not messages:
        return jsonify({"ok": False, "error": "No messages provided"}), 400

    try:
        import anthropic as _anthropic

        positions   = load_positions()
        mkt         = get_market_data()
        quotes      = get_option_quotes(positions)
        roll_quotes = get_roll_quotes(positions)
        alerts      = run_alerts(positions, mkt, quotes, roll_quotes)
        rec         = get_leg_recommendation(positions, mkt)
        enriched    = [_enrich(p, mkt, quotes) for p in positions]

        system = _build_chat_system(mkt, enriched, alerts, rec)

        client = _anthropic.Anthropic(api_key=api_key)
        resp   = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=messages,
        )

        return jsonify({
            "ok":      True,
            "content": resp.content[0].text,
            "as_of":   datetime.now().strftime("%H:%M:%S"),
        })

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Dashboard → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
