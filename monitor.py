"""
AMZN Covered Call Monitor
Checks alert conditions and sends Gmail notifications.
Runs via GitHub Actions on a schedule during market hours.
"""

import json
import os
import smtplib
import sys
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import yfinance as yf

# ── CONFIG ────────────────────────────────────────────────────────────────
GMAIL_TO      = "zgkmail@gmail.com"
GMAIL_FROM    = os.environ.get("GMAIL_FROM")      # set in GitHub Secrets
GMAIL_PASS    = os.environ.get("GMAIL_APP_PASS")  # set in GitHub Secrets
POSITIONS_FILE = "positions.json"

# Alert thresholds
ASSIGNMENT_ZONE_PCT   = 2.0   # alert if strike is within 2% of spot
WARN_ZONE_PCT         = 4.0   # warning if strike is within 4% of spot
ROLL_DTE_TRIGGER      = 21    # alert when days-to-expiry <= this
LOSS_MULTIPLE         = 2.0   # alert if call value > X times premium sold
DROP_ALERT_PCT        = 5.0   # alert if AMZN drops this % in one day
EARNINGS_DATE         = date(2026, 7, 30)
EARNINGS_WARN_DAYS    = 14    # start warning this many days before earnings

# ── LOAD POSITIONS ────────────────────────────────────────────────────────
def load_positions():
    with open(POSITIONS_FILE) as f:
        return json.load(f)

# ── MARKET DATA (Yahoo Finance) ───────────────────────────────────────────
def get_market_data():
    ticker = yf.Ticker("AMZN")
    info   = ticker.fast_info
    hist   = ticker.history(period="2d")

    price      = round(info.last_price, 2)
    prev_close = round(hist["Close"].iloc[-2], 2) if len(hist) >= 2 else price
    change_pct = round((price - prev_close) / prev_close * 100, 2)

    # IV approximation via options (nearest expiry ATM call)
    try:
        exp_dates = ticker.options
        if exp_dates:
            chain = ticker.option_chain(exp_dates[0])
            calls = chain.calls
            atm   = calls.iloc[(calls["strike"] - price).abs().argsort()[:1]]
            iv    = round(float(atm["impliedVolatility"].values[0]) * 100, 1)
        else:
            iv = None
    except Exception:
        iv = None

    return {
        "price":      price,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "iv":         iv,
    }

# ── DAYS TO EXPIRY ────────────────────────────────────────────────────────
def days_to_expiry(expiry_str):
    exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    return max(0, (exp - date.today()).days)

# ── ALERT ENGINE ──────────────────────────────────────────────────────────
def run_alerts(positions, mkt):
    alerts = []
    price  = mkt["price"]

    # ── Earnings proximity ──────────────────────────────────────────────
    days_to_earn = (EARNINGS_DATE - date.today()).days
    if 0 < days_to_earn <= EARNINGS_WARN_DAYS:
        alerts.append({
            "level":   "RISK" if days_to_earn <= 7 else "WARN",
            "emoji":   "🔴" if days_to_earn <= 7 else "📅",
            "title":   f"Earnings in {days_to_earn} days (Jul 30)",
            "detail":  (
                f"AMZN reports Q2 on Jul 30. Options market prices in a ±6% move (~${price * 0.06:.0f}). "
                f"Review any leg expiring after Jul 30 — especially Leg 3. "
                f"Consider closing or rolling before the event."
            ),
        })

    # ── Per-position checks ─────────────────────────────────────────────
    for pos in positions:
        strike    = pos["strike"]
        contracts = pos["contracts"]
        premium   = pos["premium"]
        expiry    = pos["expiry"]
        leg       = pos["leg"].upper()
        shares    = contracts * 100
        dte       = days_to_expiry(expiry)
        otm_pct   = (strike - price) / price * 100

        # Assignment risk
        if otm_pct < 0:
            alerts.append({
                "level":  "RISK",
                "emoji":  "🔴",
                "title":  f"IN THE MONEY — ${strike} Call ({leg})",
                "detail": (
                    f"AMZN at ${price} has moved above your ${strike} strike. "
                    f"{shares} shares ({contracts} contracts) face assignment at expiry ({expiry}, {dte} DTE). "
                    f"Act now: buy back the call or roll up/out to avoid forced sale."
                ),
            })
        elif otm_pct < ASSIGNMENT_ZONE_PCT:
            alerts.append({
                "level":  "RISK",
                "emoji":  "🔴",
                "title":  f"Assignment risk — ${strike} Call ({leg})",
                "detail": (
                    f"AMZN at ${price} is only {otm_pct:.1f}% below your ${strike} strike "
                    f"({shares} shares, {dte} DTE). "
                    f"High assignment risk — consider rolling up or buying back."
                ),
            })
        elif otm_pct < WARN_ZONE_PCT:
            alerts.append({
                "level":  "WARN",
                "emoji":  "🟡",
                "title":  f"Strike proximity warning — ${strike} Call ({leg})",
                "detail": (
                    f"AMZN at ${price} is {otm_pct:.1f}% from your ${strike} strike "
                    f"({shares} shares, {dte} DTE). Monitor closely."
                ),
            })

        # Roll trigger
        if 0 < dte <= ROLL_DTE_TRIGGER:
            alerts.append({
                "level":  "WARN",
                "emoji":  "🟡",
                "title":  f"Roll window — ${strike} Call ({leg}, {dte} DTE)",
                "detail": (
                    f"At {dte} days to expiry, theta decay is accelerating. "
                    f"Consider rolling to next month to capture more premium. "
                    f"Original premium: ${premium}/sh (${premium * shares:,.0f} total)."
                ),
            })

        # Earnings overlap
        if days_to_earn > 0 and dte >= days_to_earn:
            alerts.append({
                "level":  "WARN",
                "emoji":  "📅",
                "title":  f"Earnings within leg window — ${strike} Call ({leg})",
                "detail": (
                    f"Jul 30 earnings falls before your {expiry} expiry. "
                    f"A ±6% move at earnings (~${price * 0.06:.0f}) could breach your ${strike} strike."
                ),
            })

    # ── Daily drop ──────────────────────────────────────────────────────
    if mkt["change_pct"] <= -DROP_ALERT_PCT:
        alerts.append({
            "level":  "WARN",
            "emoji":  "📉",
            "title":  f"AMZN down {mkt['change_pct']:.1f}% today",
            "detail": (
                f"Sharp drop from ${mkt['prev_close']} to ${price}. "
                f"Your short calls have gained value (good for you). "
                f"Consider buying back to lock in gains if the move feels overdone."
            ),
        })

    return alerts

# ── EMAIL ─────────────────────────────────────────────────────────────────
def build_email(alerts, positions, mkt):
    price      = mkt["price"]
    change_pct = mkt["change_pct"]
    iv         = mkt["iv"]
    sign       = "+" if change_pct >= 0 else ""

    risk_alerts = [a for a in alerts if a["level"] == "RISK"]
    warn_alerts = [a for a in alerts if a["level"] == "WARN"]

    subject = (
        f"[CALL/DESK] {'🔴 ACTION REQUIRED — ' if risk_alerts else '🟡 ' if warn_alerts else '✅ All clear — '}"
        f"AMZN ${price} ({sign}{change_pct}%) · {len(alerts)} alert{'s' if len(alerts) != 1 else ''}"
    )

    # Plain text body
    divider = "─" * 52
    lines = [
        "AMZN COVERED CALL MONITOR",
        divider,
        f"  Price:       ${price}  ({sign}{change_pct}%)",
        f"  Prev close:  ${mkt['prev_close']}",
        f"  30D IV:      {iv}%" if iv else "  30D IV:      n/a",
        f"  As of:       {datetime.now().strftime('%Y-%m-%d %H:%M ET')}",
        divider,
    ]

    if not alerts:
        lines.append("  ✅  No alerts — all positions within normal parameters.")
    else:
        if risk_alerts:
            lines.append(f"\n  🔴  {len(risk_alerts)} ACTION REQUIRED\n")
            for a in risk_alerts:
                lines += [f"  {a['emoji']}  {a['title']}", f"     {a['detail']}", ""]
        if warn_alerts:
            lines.append(f"\n  🟡  {len(warn_alerts)} WARNING{'S' if len(warn_alerts)>1 else ''}\n")
            for a in warn_alerts:
                lines += [f"  {a['emoji']}  {a['title']}", f"     {a['detail']}", ""]

    lines += [
        divider,
        "  OPEN POSITIONS",
        divider,
    ]
    for pos in positions:
        dte    = days_to_expiry(pos["expiry"])
        otm    = (pos["strike"] - price) / price * 100
        total  = pos["premium"] * pos["contracts"] * 100
        lines += [
            f"  ${pos['strike']} Call · {pos['leg'].upper()} · {pos['contracts']} contracts ({pos['contracts']*100} shares)",
            f"     Expiry: {pos['expiry']} ({dte} DTE)  |  Premium: ${pos['premium']}/sh (${total:,.0f} total)  |  OTM: {otm:+.1f}%",
            "",
        ]

    lines += [
        divider,
        "  To update positions: edit positions.json in your GitHub repo.",
        "  To silence an alert type: edit thresholds in monitor.py.",
        divider,
        "  CALL/DESK · github.com/[your-repo]/amzn-call-monitor",
    ]

    return subject, "\n".join(lines)

# ── SEND EMAIL ────────────────────────────────────────────────────────────
def send_email(subject, body):
    if not GMAIL_FROM or not GMAIL_PASS:
        print("ERROR: GMAIL_FROM or GMAIL_APP_PASS secret not set in GitHub.")
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_FROM
    msg["To"]      = GMAIL_TO
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_FROM, GMAIL_PASS)
        server.sendmail(GMAIL_FROM, GMAIL_TO, msg.as_string())

    print(f"Email sent → {GMAIL_TO}")
    print(f"Subject: {subject}")

# ── MAIN ──────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Running AMZN covered call monitor...")

    positions = load_positions()
    print(f"Loaded {len(positions)} position(s).")

    mkt = get_market_data()
    print(f"AMZN: ${mkt['price']} ({mkt['change_pct']:+.2f}%)  IV: {mkt['iv']}%")

    alerts = run_alerts(positions, mkt)
    print(f"Alerts triggered: {len(alerts)} ({sum(1 for a in alerts if a['level']=='RISK')} risk, {sum(1 for a in alerts if a['level']=='WARN')} warn)")

    # Always send if there are alerts; send a daily summary at ~market close regardless
    hour = datetime.now().hour
    is_daily_summary = (hour >= 20)  # ~4pm ET = 20:00 UTC

    if alerts or is_daily_summary:
        subject, body = build_email(alerts, positions, mkt)
        send_email(subject, body)
    else:
        print("No alerts and not daily summary time — skipping email.")

if __name__ == "__main__":
    main()
