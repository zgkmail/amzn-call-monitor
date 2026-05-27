"""
AMZN Covered Call Dashboard
Flask web app that serves the monitor data as a live dashboard.
Run:  python webapp.py   →  http://localhost:5000
"""

import os
import re
import sys
from datetime import datetime
from pathlib import Path

# Resolve to repo directory so monitor.py's relative "positions.json" path works
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)

from flask import Flask, jsonify, send_from_directory

from monitor import (
    days_to_expiry,
    get_market_data,
    get_option_quotes,
    load_positions,
    run_alerts,
)

app = Flask(__name__)


def _leg_from_title(title: str) -> str | None:
    """Return the leg tag ('1M', '2M', …) from a position-specific alert title, or None."""
    # Position alerts end with "(1M)" or "(1M, 21 DTE)"; global alerts don't match.
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


@app.route("/")
def index():
    return send_from_directory(str(HERE), "index.html")


@app.route("/api/status")
def api_status():
    try:
        positions = load_positions()
        mkt       = get_market_data()
        quotes    = get_option_quotes(positions)
        alerts    = run_alerts(positions, mkt, quotes)

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
            "ok":           True,
            "as_of":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "market":       {k: mkt[k] for k in ("price", "prev_close", "change_pct", "iv")},
            "positions":    enriched,
            "mkt_alerts":   mkt_alerts,
            "alert_counts": {
                "risk":  sum(1 for a in alerts if a["level"] == "RISK"),
                "warn":  sum(1 for a in alerts if a["level"] == "WARN"),
                "total": len(alerts),
            },
        })

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Dashboard → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
