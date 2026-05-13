import os
import requests
import yfinance as yf
from fredapi import Fred
from anthropic import Anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify
import pytz
from datetime import datetime, timedelta
import logging

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [ETHER] %(message)s")

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
FRED_API_KEY    = os.getenv("FRED_API_KEY")
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
CHAT_ID         = os.getenv("CHAT_ID")
SYNAPSE_URL     = os.getenv("SYNAPSE_URL", "https://synapse-production-583f.up.railway.app")

anthropic_client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
fred             = Fred(api_key=FRED_API_KEY)
ET               = pytz.timezone("America/New_York")


# ─────────────────────────────────────────
# LAYER 1 — ECONOMIC CALENDAR (Finnhub)
# ─────────────────────────────────────────
def fetch_economic_calendar():
    today    = datetime.now(ET).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(ET) + timedelta(days=1)).strftime("%Y-%m-%d")

    url  = f"https://finnhub.io/api/v1/calendar/economic?from={today}&to={tomorrow}&token={FINNHUB_API_KEY}"
    resp = requests.get(url, timeout=10)
    events = resp.json().get("economicCalendar", [])

    HIGH_IMPACT = ["Fed", "FOMC", "CPI", "NFP", "GDP", "PPI",
                   "Unemployment", "Retail Sales", "PCE", "ISM", "Powell",
                   "Jobless", "Durable", "Housing"]

    filtered = [
        e for e in events
        if any(kw.lower() in e.get("event", "").lower() for kw in HIGH_IMPACT)
    ]

    logging.info(f"Economic calendar: {len(filtered)} high-impact events found")
    return filtered


# ─────────────────────────────────────────
# LAYER 2 — MACRO SNAPSHOT (yfinance)
# ─────────────────────────────────────────
def fetch_macro_snapshot():
    tickers = {
        "DXY": "DX-Y.NYB",
        "VIX": "^VIX",
        "SPX": "^GSPC",
        "NDX": "^NDX",
        "TNX": "^TNX",   # 10Y yield
        "TYX": "^TYX",   # 30Y yield
    }

    snapshot = {}
    for name, symbol in tickers.items():
        try:
            hist = yf.Ticker(symbol).history(period="2d")
            if not hist.empty:
                latest = hist["Close"].iloc[-1]
                prev   = hist["Close"].iloc[-2] if len(hist) > 1 else latest
                chg    = ((latest - prev) / prev) * 100
                snapshot[name] = {"price": round(latest, 2), "change_pct": round(chg, 2)}
            else:
                snapshot[name] = {"price": "N/A", "change_pct": 0}
        except Exception as e:
            logging.warning(f"yfinance error for {name}: {e}")
            snapshot[name] = {"price": "N/A", "change_pct": 0}

    logging.info("Macro snapshot fetched")
    return snapshot


# ─────────────────────────────────────────
# LAYER 3 — FRED MACRO SERIES
# ─────────────────────────────────────────
def fetch_fred_data():
    series_map = {
        "Fed Funds Rate":     "FEDFUNDS",
        "CPI YoY":            "CPIAUCSL",
        "Core PCE":           "PCEPILFE",
        "Unemployment Rate":  "UNRATE",
        "10Y-2Y Spread":      "T10Y2Y",
        "10Y-3M Spread":      "T10Y3M",
    }

    fred_data = {}
    for name, series_id in series_map.items():
        try:
            data = fred.get_series(series_id, limit=1)
            fred_data[name] = round(float(data.iloc[-1]), 2) if not data.empty else "N/A"
        except Exception as e:
            logging.warning(f"FRED error for {name}: {e}")
            fred_data[name] = "N/A"

    logging.info("FRED data fetched")
    return fred_data


# ─────────────────────────────────────────
# LAYER 4 — CLAUDE SYNTHESIS
# ─────────────────────────────────────────
def synthesize_with_claude(calendar_events, macro_snapshot, fred_data):
    def fmt(key):
        d = macro_snapshot.get(key, {})
        price = d.get("price", "N/A")
        chg   = d.get("change_pct", 0)
        arrow = "▲" if chg > 0 else "▼" if chg < 0 else "─"
        return f"{price} {arrow}{abs(chg)}%"

    calendar_text = "\n".join(
        [f"  • {e.get('time','?')} ET — {e.get('event','?')} | Forecast: {e.get('estimate','?')} | Prior: {e.get('prev','?')}"
         for e in calendar_events]
    ) if calendar_events else "No high-impact events today."

    prompt = f"""You are ETHER, the market intelligence agent for a professional MES/MNQ micro futures trader named Chandler.

TODAY'S HIGH-IMPACT ECONOMIC EVENTS:
{calendar_text}

MACRO SNAPSHOT (pre-market):
  DXY  (Dollar Index) : {fmt('DXY')}
  VIX  (Volatility)   : {fmt('VIX')}
  SPX  (S&P 500)      : {fmt('SPX')}
  NDX  (Nasdaq)       : {fmt('NDX')}
  TNX  (10Y Yield)    : {fmt('TNX')}
  TYX  (30Y Yield)    : {fmt('TYX')}

FRED KEY INDICATORS:
  Fed Funds Rate     : {fred_data.get('Fed Funds Rate')}%
  CPI YoY            : {fred_data.get('CPI YoY')}
  Core PCE           : {fred_data.get('Core PCE')}
  Unemployment Rate  : {fred_data.get('Unemployment Rate')}%
  10Y-2Y Spread      : {fred_data.get('10Y-2Y Spread')}%
  10Y-3M Spread      : {fred_data.get('10Y-3M Spread')}%

Generate a daily brief for Chandler covering:
1. MACRO BIAS — Bullish / Bearish / Neutral for MES and MNQ today and why
2. RISK EVENTS — Any events that could spike volatility during the 9:30am–3pm session
3. KEY LEVELS TO WATCH — Based on macro context (VIX regime, yield direction, dollar strength)
4. TRADE POSTURE — Aggressive, Normal, or Cautious and why

Keep it under 220 words. Be direct. No fluff. Speak like a sharp trading desk analyst."""

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )

    brief = response.content[0].text
    logging.info("Claude synthesis complete")
    return brief


# ─────────────────────────────────────────
# SYNAPSE MEMORY STORAGE
# ─────────────────────────────────────────
def store_in_synapse(brief, macro_snapshot, calendar_events):
    payload = {
        "agentId":    "ether",
        "memoryType": "market",
        "content":    brief,
        "importance": 8,
        "tags":       ["fundamentals", "daily-brief", "macro",
                       f"vix-{macro_snapshot.get('VIX', {}).get('price', 'unknown')}"]
    }
    try:
        r = requests.post(f"{SYNAPSE_URL}/memory", json=payload, timeout=10)
        logging.info(f"SYNAPSE memory stored — status {r.status_code}")
    except Exception as e:
        logging.error(f"SYNAPSE storage failed: {e}")


# ─────────────────────────────────────────
# TELEGRAM DELIVERY
# ─────────────────────────────────────────
def send_telegram(macro_snapshot, brief):
    def p(key):
        d = macro_snapshot.get(key, {})
        price = d.get("price", "N/A")
        chg   = d.get("change_pct", 0)
        arrow = "🟢" if chg > 0 else "🔴" if chg < 0 else "⚪"
        return f"{arrow} {price} ({'+' if chg > 0 else ''}{chg}%)"

    now = datetime.now(ET).strftime("%b %d, %Y · %I:%M %p ET")

    message = f"""⚡ <b>ETHER DAILY BRIEF</b>
<i>{now}</i>
━━━━━━━━━━━━━━━━━━━━
📊 <b>MACRO SNAPSHOT</b>
VIX    {p('VIX')}
DXY    {p('DXY')}
SPX    {p('SPX')}
NDX    {p('NDX')}
10Y    {p('TNX')}
━━━━━━━━━━━━━━━━━━━━
🧠 <b>MARKET CONTEXT</b>
{brief}
━━━━━━━━━━━━━━━━━━━━
<i>Powered by ETHER AI · SYNAPSE</i>"""

    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
        logging.info(f"Telegram delivered — status {resp.status_code}")
    except Exception as e:
        logging.error(f"Telegram failed: {e}")


# ─────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────
def run_ether_fundamentals():
    logging.info("━━━ ETHER Fundamentals pipeline starting ━━━")
    try:
        calendar_events = fetch_economic_calendar()
        macro_snapshot  = fetch_macro_snapshot()
        fred_data       = fetch_fred_data()
        brief           = synthesize_with_claude(calendar_events, macro_snapshot, fred_data)

        store_in_synapse(brief, macro_snapshot, calendar_events)
        send_telegram(macro_snapshot, brief)

        logging.info("━━━ ETHER Fundamentals pipeline complete ━━━")
        return {"status": "ok", "events": len(calendar_events), "brief_length": len(brief)}
    except Exception as e:
        logging.error(f"Pipeline error: {e}")
        return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────
# FLASK ENDPOINTS
# ─────────────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "live", "service": "ETHER Fundamentals", "agent": "ether"})

@app.route("/run", methods=["POST"])
def manual_run():
    result = run_ether_fundamentals()
    return jsonify(result)

@app.route("/snapshot", methods=["GET"])
def snapshot():
    macro = fetch_macro_snapshot()
    return jsonify(macro)

@app.route("/calendar", methods=["GET"])
def calendar():
    events = fetch_economic_calendar()
    return jsonify({"count": len(events), "events": events})


# ─────────────────────────────────────────
# SCHEDULER — 7:00 AM ET DAILY
# ─────────────────────────────────────────
scheduler = BackgroundScheduler(timezone=ET)
scheduler.add_job(run_ether_fundamentals, "cron", hour=7, minute=0, id="ether_daily_brief")
scheduler.start()
logging.info("Scheduler armed — ETHER brief fires at 7:00 AM ET daily")


# ─────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
