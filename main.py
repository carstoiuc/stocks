"""
Hourly stock check script.
Runs once per invocation — a GitHub Actions schedule calls this every hour;
this script checks whether NYSE is currently open before doing any real work,
so it's a no-op (and doesn't burn API calls) outside trading hours.

Each run appends a snapshot to docs/data.json, which the dashboard at
stocks.carstoiuc.org (served via GitHub Pages from the docs/ folder) reads
directly — no separate backend or database needed.
"""

import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

DATA_FILE = Path(__file__).parent / "docs" / "data.json"
MAX_HISTORY = 500  # keep the file small; oldest snapshots roll off

# ---- Config (all pulled from environment variables / GitHub Actions secrets) ----
TICKERS = os.environ.get("TICKERS", "AAPL,MSFT,NVDA").split(",")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO")

# Simple example threshold: alert if a stock moves more than this % from previous close
MOVE_THRESHOLD_PCT = float(os.environ.get("MOVE_THRESHOLD_PCT", "2.0"))

NY_TZ = ZoneInfo("America/New_York")

def is_nyse_open(now_ny: datetime) -> bool:
    """Basic regular-hours check: Mon-Fri, 9:30am-4:00pm ET.
    Does NOT account for market holidays (e.g. Thanksgiving, July 4th) —
    good enough to start, but add a holiday calendar check before relying on this."""
    if now_ny.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_ny <= market_close


def fetch_quote(ticker: str) -> dict:
    """Fetch current quote from Finnhub. Docs: https://finnhub.io/docs/api/quote"""
    url = "https://finnhub.io/api/v1/quote"
    resp = requests.get(url, params={"symbol": ticker, "token": FINNHUB_API_KEY}, timeout=10)
    resp.raise_for_status()
    return resp.json()  # {c: current, pc: previous close, ...}


def analyze(ticker: str, quote: dict) -> str | None:
    """Return an alert message if the move exceeds the threshold, else None."""
    current, prev_close = quote.get("c"), quote.get("pc")
    if not current or not prev_close:
        return None
    pct_change = (current - prev_close) / prev_close * 100
    if abs(pct_change) >= MOVE_THRESHOLD_PCT:
        direction = "up" if pct_change > 0 else "down"
        return f"{ticker}: {direction} {abs(pct_change):.2f}% (${prev_close:.2f} -> ${current:.2f})"
    return None

def load_history() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def save_snapshot(snapshot: dict) -> None:
    history = load_history()
    history.append(snapshot)
    history = history[-MAX_HISTORY:]
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(history, indent=2))


def send_email(subject: str, body: str) -> None:
    if not (SMTP_USER and SMTP_PASSWORD and ALERT_EMAIL_TO):
        print("Email not configured — skipping send. Would have sent:")
        print(subject)
        print(body)
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_EMAIL_TO

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [ALERT_EMAIL_TO], msg.as_string())

def main() -> None:
    now_ny = datetime.now(NY_TZ)

    if not is_nyse_open(now_ny):
        print(f"{now_ny.isoformat()} — market closed, skipping.")
        return

    if not FINNHUB_API_KEY:
        print("FINNHUB_API_KEY not set — cannot fetch quotes.", file=sys.stderr)
        sys.exit(1)

    alerts = []
    ticker_snapshots = []
    for ticker in TICKERS:
        ticker = ticker.strip()
        try:
            quote = fetch_quote(ticker)
        except requests.RequestException as e:
            print(f"Failed to fetch {ticker}: {e}", file=sys.stderr)
            continue

        current, prev_close = quote.get("c"), quote.get("pc")
        pct_change = ((current - prev_close) / prev_close * 100) if current and prev_close else None
        message = analyze(ticker, quote)
        if message:
            alerts.append(message)

        ticker_snapshots.append({
            "ticker": ticker,
            "price": current,
            "prev_close": prev_close,
            "pct_change": round(pct_change, 2) if pct_change is not None else None,
            "alert": message is not None,
        })

    save_snapshot({
        "timestamp": now_ny.isoformat(),
        "tickers": ticker_snapshots,
    })

    if alerts:
        body = "\n".join(alerts)
        print("Sending alert:\n" + body)
        send_email(f"Stock alert — {len(alerts)} move(s) detected", body)
    else:
        print(f"{now_ny.isoformat()} — checked {TICKERS}, no moves over {MOVE_THRESHOLD_PCT}%.")


if __name__ == "__main__":
    main()
