# AMZN Covered Call Monitor

Automated alert system for your AMZN covered call ladder.
Runs on GitHub Actions — no server, no computer left on.

## What it does

- Checks AMZN price every 30 minutes during market hours (Mon–Fri 9:30am–4:30pm ET)
- Sends Gmail alerts to zgkmail@gmail.com when:
  - AMZN is within 2% of any strike (assignment risk)
  - AMZN is within 4% of any strike (early warning)
  - A leg hits 21 days to expiry (roll trigger)
  - Earnings are within 14 days and a leg spans the date
  - AMZN drops more than 5% in a day
- Sends a daily summary email at ~4pm ET regardless

## Setup (one-time, ~10 minutes)

### Step 1 — Create Gmail App Password
1. Go to myaccount.google.com → Security → 2-Step Verification (must be enabled)
2. Search "App passwords" → create one named "AMZN Monitor"
3. Copy the 16-character password shown (you won't see it again)

### Step 2 — Add GitHub Secrets
In your repo: Settings → Secrets and variables → Actions → New repository secret

| Secret name     | Value                                      |
|-----------------|--------------------------------------------|
| `GMAIL_FROM`    | Your Gmail address (e.g. you@gmail.com)    |
| `GMAIL_APP_PASS`| The 16-char app password from Step 1       |

### Step 3 — Enable Actions
Go to the Actions tab in your repo → click "I understand my workflows, go ahead and enable them"

### Step 4 — Test it
Actions tab → "AMZN Covered Call Monitor" → "Run workflow" → Run
Check your inbox within 1–2 minutes.

## Managing positions

Edit `positions.json` directly on GitHub (click the file → pencil icon):

### Add a new leg
```json
{
  "leg": "1m",
  "contracts": 4,
  "strike": 275,
  "expiry": "2026-06-20",
  "premium": 4.20,
  "option_symbol": "AMZN260620C00275000",
  "notes": "Optional note"
}
```

### Roll a leg
Find the entry in positions.json → update `strike`, `expiry`, and `premium` → commit.

### Close / expire a leg
Delete that entry from the array → commit.

## Adjusting alert thresholds

Edit the constants at the top of `monitor.py`:

| Variable             | Default | Meaning                              |
|----------------------|---------|--------------------------------------|
| `ASSIGNMENT_ZONE_PCT`| 2.0     | % OTM that triggers red alert        |
| `WARN_ZONE_PCT`      | 4.0     | % OTM that triggers yellow warning   |
| `ROLL_DTE_TRIGGER`   | 21      | Days to expiry that triggers roll alert |
| `LOSS_MULTIPLE`      | 2.0     | Call value / premium sold for loss alert |
| `DROP_ALERT_PCT`     | 5.0     | AMZN daily drop % to alert           |
| `EARNINGS_WARN_DAYS` | 14      | Days before earnings to start warning |

## Upgrading to Tradier (real-time data)

When you have your Tradier API token:
1. Add `TRADIER_TOKEN` to GitHub Secrets
2. The monitor.py file has a commented Tradier section ready to swap in

## Files

```
amzn-call-monitor/
├── positions.json              ← edit this to manage your legs
├── monitor.py                  ← alert logic and email sender
├── .github/
│   └── workflows/
│       └── monitor.yml         ← GitHub Actions schedule
└── README.md
```
