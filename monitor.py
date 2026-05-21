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
ASSIGNMENT_ZONE_PCT = 2.0   # alert if strike is within 2% of spot
WARN_ZONE_PCT       = 4.0   # warning if strike is within 4% of spot
ROLL_DTE_TRIGGER    = 21    # alert when days-to-expiry <= this
LOSS_MULTIPLE       = 2.0   # alert if current option ask >= X times premium sold
BUY_BACK_PCT        = 50.0  # alert when call has lost this % of value (profit lock-in)
DROP_ALERT_PCT      = 5.0   # alert if AMZN drops this % in one day
EARNINGS_DATE       = date(2026, 7, 30)
EARNINGS_WARN_DAYS  = 14    # start warning this many days before earnings

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

    return {"price": price, "prev_close": prev_close, "change_pct": change_pct, "iv": iv}

# ── OPTION QUOTES (Yahoo Finance) ─────────────────────────────────────────
def get_option_quotes(positions):
    """Fetch bid/ask/mid for each position from Yahoo Finance option chains.
    Data is delayed (~15 min) but sufficient for monitoring purposes.
    Returns {option_symbol: {bid, ask, mid}} or {} on failure."""
    result    = {}
    ticker    = yf.Ticker("AMZN")
    available = set(ticker.options)

    # Group by expiry to minimise API calls (one chain fetch per expiry date)
    by_expiry = {}
    for pos in positions:
        if pos.get("option_symbol"):
            by_expiry.setdefault(pos["expiry"], []).append(pos)

    for exp, legs in by_expiry.items():
        if exp not in available:
            print(f"Warning: expiry {exp} not in Yahoo Finance chain — skipping option quotes.")
            continue
        try:
            calls = ticker.option_chain(exp).calls
            for pos in legs:
                row = calls[calls["strike"] == float(pos["strike"])]
                if row.empty:
                    print(f"Warning: strike ${pos['strike']} not found in {exp} chain.")
                    continue
                bid = round(float(row["bid"].values[0]), 2)
                ask = round(float(row["ask"].values[0]), 2)
                mid = round((bid + ask) / 2, 2)
                result[pos["option_symbol"]] = {"bid": bid, "ask": ask, "mid": mid}
        except Exception as e:
            print(f"Warning: could not fetch option chain for {exp}: {e}")

    return result

# ── DAYS TO EXPIRY ────────────────────────────────────────────────────────
def days_to_expiry(expiry_str):
    exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    return max(0, (exp - date.today()).days)

# ── ALERT ENGINE ──────────────────────────────────────────────────────────
def run_alerts(positions, mkt, option_quotes=None):
    alerts = []
    price  = mkt["price"]
    if option_quotes is None:
        option_quotes = {}

    # ── Earnings proximity ──────────────────────────────────────────────
    days_to_earn = (EARNINGS_DATE - date.today()).days
    if 0 < days_to_earn <= EARNINGS_WARN_DAYS:
        earn_move   = price * 0.06
        earn_urgent = days_to_earn <= 7
        alerts.append({
            "level": "RISK" if earn_urgent else "WARN",
            "emoji": "🔴" if earn_urgent else "📅",
            "title": f"Earnings in {days_to_earn} days (Jul 30)",
            "detail": (
                f"AMZN reports Q2 earnings on Jul 30. The options market is pricing in a "
                f"±6% implied move (~${earn_move:.0f} per share). IV typically spikes into "
                f"the event and collapses immediately after (IV crush), making short calls "
                f"temporarily more expensive to buy back. Any leg whose expiry falls after "
                f"Jul 30 carries full earnings risk — a bullish surprise could rapidly push "
                f"AMZN through your strikes."
            ),
            "reco": (
                f"  A) Roll to post-earnings expiry BEFORE Jul 30: sell the same (or "
                f"higher) strike on the next monthly cycle to sidestep the event.\n"
                f"  B) Close the leg 1–2 weeks before earnings while IV is still elevated "
                f"— you'll pay more to close, but eliminate binary risk.\n"
                f"  C) If keeping through earnings, buy a protective call $5–10 above "
                f"each strike to cap upside loss (converts short call to a spread).\n"
                f"  D) Do nothing only if AMZN is comfortably OTM and you are prepared "
                f"to roll/defend quickly the morning after earnings."
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
        itm_dollars = abs(price - strike)
        if otm_pct < 0:
            intrinsic = itm_dollars * shares
            alerts.append({
                "level": "RISK",
                "emoji": "🔴",
                "title": f"IN THE MONEY — ${strike} Call ({leg})",
                "detail": (
                    f"AMZN at ${price} has breached your ${strike} strike by "
                    f"${itm_dollars:.2f}/sh (${intrinsic:,.0f} intrinsic value across "
                    f"{contracts} contracts). The call is now deep-in-the-money and delta "
                    f"is near 1.0 — every $1 AMZN rises costs you ~$1/sh to buy back. "
                    f"Early assignment is possible on American-style options even before "
                    f"expiry ({expiry}, {dte} DTE). You collected ${premium}/sh "
                    f"(${premium * shares:,.0f} total) upfront — that partially offsets "
                    f"the current loss, but the position needs immediate attention."
                ),
                "reco": (
                    f"  A) Buy to close NOW: limits further damage. Net loss so far is "
                    f"approximately ${max(0, itm_dollars - premium):.2f}/sh "
                    f"(${max(0, (itm_dollars - premium) * shares):,.0f} total).\n"
                    f"  B) Roll up & out: buy back this ${strike} call and sell a higher "
                    f"strike (${strike + 5}–${strike + 15}) on a later expiry for a net "
                    f"credit or small debit — buys time and moves the ceiling higher.\n"
                    f"  C) Accept assignment only if you want to sell {shares} shares at "
                    f"${strike} and are happy with that exit price. You keep all premium "
                    f"collected but cap any further AMZN upside."
                ),
            })
        elif otm_pct < ASSIGNMENT_ZONE_PCT:
            gap_dollars = strike - price
            alerts.append({
                "level": "RISK",
                "emoji": "🔴",
                "title": f"Assignment risk — ${strike} Call ({leg})",
                "detail": (
                    f"AMZN at ${price} is only {otm_pct:.1f}% (${gap_dollars:.2f}/sh) "
                    f"below your ${strike} strike ({shares} shares, {dte} DTE). "
                    f"At this distance the call delta is typically 0.45–0.55, meaning the "
                    f"market assigns roughly 45–55% probability of finishing ITM. A single "
                    f"strong session could push AMZN through the strike. You still have "
                    f"${premium}/sh of premium cushion (${premium * shares:,.0f} total), "
                    f"but that buffer is nearly consumed."
                ),
                "reco": (
                    f"  A) Roll up & out today: buy back the ${strike} call and sell a "
                    f"higher strike (${strike + 5}–${strike + 10}) on the next monthly "
                    f"expiry. Target a net credit or at worst a small debit.\n"
                    f"  B) Buy to close and wait: eliminates risk entirely; re-sell a new "
                    f"covered call when AMZN settles or IV normalises.\n"
                    f"  C) Hold but set a hard stop: if AMZN crosses ${strike - 1:.0f} "
                    f"(1 point below strike), commit to rolling immediately — don't wait "
                    f"for expiry to force the decision."
                ),
            })
        elif otm_pct < WARN_ZONE_PCT:
            gap_dollars = strike - price
            alerts.append({
                "level": "WARN",
                "emoji": "🟡",
                "title": f"Strike proximity warning — ${strike} Call ({leg})",
                "detail": (
                    f"AMZN at ${price} is {otm_pct:.1f}% (${gap_dollars:.2f}/sh) from "
                    f"your ${strike} strike ({shares} shares, {dte} DTE). The call delta "
                    f"is likely 0.30–0.45 — meaningful but not yet critical. You still have "
                    f"${premium}/sh of premium collected (${premium * shares:,.0f} total) "
                    f"as a buffer. The position needs active monitoring; another 2% move "
                    f"triggers a red alert."
                ),
                "reco": (
                    f"  A) No action required yet, but watch the tape. Check back at the "
                    f"next 30-minute interval.\n"
                    f"  B) If IV is elevated today (see header), this is a decent time to "
                    f"roll up preemptively — you'll get a better credit for the new leg "
                    f"than you would after further upside.\n"
                    f"  C) Tighten your mental stop: plan the specific roll trade you'd "
                    f"execute if AMZN reaches ${strike * 0.98:.2f} (2% OTM threshold) so "
                    f"you can act fast without deliberating under pressure."
                ),
            })

        # Roll trigger
        if 0 < dte <= ROLL_DTE_TRIGGER:
            alerts.append({
                "level": "WARN",
                "emoji": "🟡",
                "title": f"Roll window — ${strike} Call ({leg}, {dte} DTE)",
                "detail": (
                    f"At {dte} DTE, theta decay is in its steepest phase — roughly 50–60% "
                    f"of remaining extrinsic value will evaporate over the next two weeks. "
                    f"You originally collected ${premium}/sh (${premium * shares:,.0f} "
                    f"total). The current buyback cost is likely much lower than that, "
                    f"meaning the bulk of your profit is already locked in. Holding to "
                    f"expiry captures the last few cents of extrinsic value but introduces "
                    f"gamma risk — the call becomes increasingly sensitive to sudden price "
                    f"moves as expiry nears."
                ),
                "reco": (
                    f"  A) Roll now (preferred at 21 DTE): buy back the ${strike} call "
                    f"and sell the same strike (or higher if AMZN has rallied) for the "
                    f"next monthly expiry. A net credit of $0.50–$1.50/sh is typical.\n"
                    f"  B) Close and re-evaluate: buy back today, wait a few sessions for "
                    f"AMZN to move, then sell a fresh covered call at a better strike or "
                    f"higher IV.\n"
                    f"  C) Hold to expiry only if the strike is comfortably OTM (>5%) and "
                    f"you have no near-term catalyst risk (check earnings overlap above). "
                    f"Commit to rolling immediately if AMZN presses the strike."
                ),
            })

        # Live option P&L checks
        symbol = pos.get("option_symbol")
        if symbol and symbol in option_quotes:
            oq          = option_quotes[symbol]
            current_ask = oq["ask"]
            current_mid = oq["mid"]

            if current_ask and current_ask >= premium * LOSS_MULTIPLE:
                loss_per_sh = round(current_ask - premium, 2)
                total_loss  = round(loss_per_sh * shares, 2)
                multiple    = round(current_ask / premium, 2)
                alerts.append({
                    "level": "RISK",
                    "emoji": "🔴",
                    "title": f"Call loss ×{multiple:.1f} — ${strike} Call ({leg})",
                    "detail": (
                        f"The {symbol} call you sold for ${premium}/sh is now quoted at "
                        f"${current_ask:.2f}/sh (ask) — {multiple:.1f}× your original "
                        f"premium. Buying back now costs ${loss_per_sh:.2f}/sh more than "
                        f"you received, a net loss of ${total_loss:,.2f} across {contracts} "
                        f"contracts. This typically signals AMZN has moved strongly toward "
                        f"or through your ${strike} strike. The longer you wait, the more "
                        f"delta and gamma will compound the loss if AMZN keeps rising."
                    ),
                    "reco": (
                        f"  A) Buy to close immediately: cap the loss at ${total_loss:,.2f}. "
                        f"Re-evaluate before selling a new covered call.\n"
                        f"  B) Roll up & out: buy back this call and sell a higher strike "
                        f"(${strike + 5}–${strike + 15}) on the next monthly expiry for a "
                        f"net credit or small debit — moves your ceiling higher and buys "
                        f"time for AMZN to pull back.\n"
                        f"  C) Do NOT hold and hope without a defined exit plan — losses "
                        f"on short calls are theoretically uncapped above the strike."
                    ),
                })
            elif current_mid is not None:
                profit_pct = (premium - current_mid) / premium * 100
                if profit_pct >= BUY_BACK_PCT:
                    locked_per_sh = round(premium - current_mid, 2)
                    total_locked  = round(locked_per_sh * shares, 2)
                    alerts.append({
                        "level": "WARN",
                        "emoji": "💰",
                        "title": f"Profit lock-in opportunity — ${strike} Call ({leg})",
                        "detail": (
                            f"The {symbol} call you sold for ${premium}/sh is now worth "
                            f"~${current_mid:.2f}/sh (mid). You can close for "
                            f"${locked_per_sh:.2f}/sh profit (${total_locked:,.2f} total), "
                            f"locking in {profit_pct:.0f}% of the maximum possible gain "
                            f"with {dte} DTE still remaining. The last few cents of "
                            f"extrinsic value are not worth the continued assignment risk."
                        ),
                        "reco": (
                            f"  A) Buy to close now: pocket ${total_locked:,.2f} and free "
                            f"up the position. Re-sell a new covered call at the same or "
                            f"higher strike if AMZN has pulled back.\n"
                            f"  B) Place a GTC buy-to-close order at ${current_mid * 0.5:.2f} "
                            f"(50% of current mid) to auto-close if the call decays further "
                            f"without requiring you to watch it.\n"
                            f"  C) Hold only if you expect a near-term move in your favour "
                            f"and are comfortable with assignment risk for {dte} more DTE."
                        ),
                    })

        # Earnings overlap
        if days_to_earn > 0 and dte >= days_to_earn:
            earn_move    = price * 0.06
            breach_price = price + earn_move
            alerts.append({
                "level": "WARN",
                "emoji": "📅",
                "title": f"Earnings within leg window — ${strike} Call ({leg})",
                "detail": (
                    f"Jul 30 earnings falls before your {expiry} expiry, so this leg "
                    f"carries full binary earnings risk. The options market's implied move "
                    f"is ±6% (~${earn_move:.0f}/sh), which would put AMZN at "
                    f"~${breach_price:.0f} on a bullish print — "
                    f"{'ABOVE' if breach_price >= strike else f'still {strike - breach_price:.0f} pts below'} "
                    f"your ${strike} strike. Additionally, IV typically spikes 30–50% in "
                    f"the week before earnings, making the call more expensive to buy back "
                    f"right now. After earnings, IV collapses, so the call rapidly loses "
                    f"extrinsic value — but by then the damage from intrinsic value "
                    f"(if ITM) is already done."
                ),
                "reco": (
                    f"  A) Roll to post-earnings expiry (e.g., Aug/Sep) at the same or "
                    f"higher strike before Jul 28 — you'll capture elevated pre-earnings "
                    f"IV in the credit you receive.\n"
                    f"  B) Close the leg 1–2 weeks before Jul 30: yes, you pay elevated "
                    f"IV, but you eliminate the gap-risk entirely.\n"
                    f"  C) Convert to a spread: buy a call at ${strike + 10} to cap your "
                    f"maximum loss. Costs ~$0.50–$1.50/sh but limits a runaway scenario.\n"
                    f"  D) Hold only if ${strike} is >8% OTM AND you have a roll plan "
                    f"ready to execute on the open on Jul 31."
                ),
            })

    # ── Daily drop ──────────────────────────────────────────────────────
    if mkt["change_pct"] <= -DROP_ALERT_PCT:
        drop_dollars = mkt["prev_close"] - price
        alerts.append({
            "level": "WARN",
            "emoji": "📉",
            "title": f"AMZN down {mkt['change_pct']:.1f}% today",
            "detail": (
                f"AMZN dropped ${drop_dollars:.2f}/sh ({mkt['change_pct']:.1f}%) from "
                f"${mkt['prev_close']} to ${price}. Because you are short calls, this move "
                f"works in your favour — the calls you sold are now worth significantly "
                f"less, so your unrealised P&L on the short legs has improved. "
                f"However, sharp drops can be followed by fast recoveries (dead-cat "
                f"bounces), and elevated IV from the sell-off makes calls temporarily "
                f"pricier to buy back than they'll be once volatility subsides."
            ),
            "reco": (
                f"  A) Buy back to lock in profits: if the call has lost 50–80% of its "
                f"value since you sold it, closing now captures most of the premium "
                f"without waiting for expiry. Re-sell at a higher strike once AMZN "
                f"stabilises.\n"
                f"  B) Hold if conviction is low: the drop may be a temporary move. "
                f"Monitor the next session — if AMZN bounces hard, your calls will "
                f"regain value quickly.\n"
                f"  C) Check your strikes: re-verify all OTM percentages in the "
                f"Positions section below. A big drop widens your safety margin — "
                f"confirm you are still comfortable with each strike level."
            ),
        })

    return alerts

# ── EMAIL ─────────────────────────────────────────────────────────────────
def _wrap(text, width=72, indent="     "):
    import textwrap
    out = []
    for paragraph in text.split("\n"):
        if paragraph.strip() == "":
            out.append("")
        else:
            out.append(textwrap.fill(paragraph, width=width,
                                     initial_indent=indent,
                                     subsequent_indent=indent + "  "))
    return "\n".join(out)


def build_email(alerts, positions, mkt, option_quotes=None):
    price      = mkt["price"]
    change_pct = mkt["change_pct"]
    iv         = mkt["iv"]
    sign       = "+" if change_pct >= 0 else ""
    oq         = option_quotes or {}

    risk_alerts = [a for a in alerts if a["level"] == "RISK"]
    warn_alerts = [a for a in alerts if a["level"] == "WARN"]

    subject = (
        f"[CALL/DESK] {'🔴 ACTION REQUIRED — ' if risk_alerts else '🟡 ' if warn_alerts else '✅ All clear — '}"
        f"AMZN ${price} ({sign}{change_pct}%) · {len(alerts)} alert{'s' if len(alerts) != 1 else ''}"
    )

    divider      = "─" * 72
    thin_divider = "·" * 72

    lines = [
        "AMZN COVERED CALL MONITOR",
        divider,
        f"  Price:       ${price}  ({sign}{change_pct}%)",
        f"  Prev close:  ${mkt['prev_close']}",
        f"  30D IV:      {iv}%" if iv else "  30D IV:      n/a",
        f"  As of:       {datetime.now().strftime('%Y-%m-%d %H:%M ET')}",
        divider,
    ]

    def render_alert(a):
        block = [
            f"  {a['emoji']}  {a['title']}",
            "",
            "     WHAT'S HAPPENING",
            _wrap(a["detail"]),
        ]
        if a.get("reco"):
            block += [
                "",
                "     RECOMMENDED ACTIONS",
                _wrap(a["reco"]),
            ]
        block.append("")
        return block

    if not alerts:
        lines.append("  ✅  No alerts — all positions within normal parameters.")
    else:
        if risk_alerts:
            lines += ["", f"  🔴  {len(risk_alerts)} ACTION REQUIRED", divider]
            for a in risk_alerts:
                lines += render_alert(a)
                lines.append(thin_divider)
        if warn_alerts:
            lines += ["", f"  🟡  {len(warn_alerts)} WARNING{'S' if len(warn_alerts)>1 else ''}", divider]
            for a in warn_alerts:
                lines += render_alert(a)
                lines.append(thin_divider)

    lines += ["", divider, "  OPEN POSITIONS", divider]

    for pos in positions:
        dte     = days_to_expiry(pos["expiry"])
        otm     = (pos["strike"] - price) / price * 100
        premium = pos["premium"]
        shares  = pos["contracts"] * 100
        total   = premium * shares
        itm_tag = "  ⚠ ITM" if otm < 0 else ""
        lines += [
            f"  ${pos['strike']} Call · {pos['leg'].upper()} · {pos['contracts']} contracts "
            f"({shares} shares){itm_tag}",
            f"     Expiry: {pos['expiry']} ({dte} DTE)  |  "
            f"Sold: ${premium}/sh (${total:,.0f} total)  |  OTM: {otm:+.1f}%",
        ]
        sym = pos.get("option_symbol")
        if sym and sym in oq:
            q          = oq[sym]
            mid        = q["mid"]
            ask        = q["ask"]
            pnl_per_sh = round(premium - (mid if mid is not None else ask), 2)
            pnl_total  = round(pnl_per_sh * shares, 2)
            pnl_pct    = round(pnl_per_sh / premium * 100, 1) if premium else 0
            pnl_sign   = "+" if pnl_per_sh >= 0 else ""
            lines.append(
                f"     Current: bid ${q['bid']:.2f} / ask ${ask:.2f} / mid ${mid:.2f}  |  "
                f"P&L: {pnl_sign}${pnl_per_sh:.2f}/sh ({pnl_sign}{pnl_pct}%)  =  "
                f"{pnl_sign}${pnl_total:,.0f} total  (Yahoo delayed)"
            )
        lines.append("")

    lines += [
        divider,
        "  To update positions: edit positions.json in your GitHub repo.",
        "  To silence an alert type: edit thresholds in monitor.py.",
        divider,
        "  CALL/DESK · github.com/zgkmail/amzn-call-monitor",
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

    option_quotes = get_option_quotes(positions)
    if option_quotes:
        print(f"Option quotes fetched for: {list(option_quotes.keys())}")
    else:
        print("No option quotes available.")

    alerts = run_alerts(positions, mkt, option_quotes)
    print(f"Alerts triggered: {len(alerts)} ({sum(1 for a in alerts if a['level']=='RISK')} risk, {sum(1 for a in alerts if a['level']=='WARN')} warn)")

    hour             = datetime.now().hour
    is_daily_summary = (hour >= 20)  # ~4pm ET = 20:00 UTC

    if alerts or is_daily_summary:
        subject, body = build_email(alerts, positions, mkt, option_quotes)
        send_email(subject, body)
    else:
        print("No alerts and not daily summary time — skipping email.")

if __name__ == "__main__":
    main()
