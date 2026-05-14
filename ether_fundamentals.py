import os
import html as html_lib
import requests
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
ET               = pytz.timezone("America/New_York")

# FRED is optional — only init if key exists
fred = Fred(api_key=FRED_API_KEY) if FRED_API_KEY else None


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
# LAYER 2 — MACRO SNAPSHOT (Finnhub quotes)
# ─────────────────────────────────────────
def fetch_macro_snapshot():
    symbols = {
        "VIX": "^VIX",
        "SPX": "^GSPC",
        "NDX": "^NDX",
        "TNX": "^TNX",
        "TYX": "^TYX",
    }

    snapshot = {}
    for name, symbol in symbols.items():
        try:
            url  = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            price = data.get("c", 0)
            chg   = data.get("dp", 0)
            if price and price != 0:
                snapshot[name] = {"price": round(price, 2), "change_pct": round(chg, 2)}
            else:
                snapshot[name] = {"price": "N/A", "change_pct": 0}
        except Exception as e:
            logging.warning(f"Finnhub quote error for {name}: {e}")
            snapshot[name] = {"price": "N/A", "change_pct": 0}

    # DXY via Finnhub forex
    try:
        url  = f"https://finnhub.io/api/v1/quote?symbol=OANDA:US30_USD&token={FINNHUB_API_KEY}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        price = data.get("c", 0)
        chg   = data.get("dp", 0)
        snapshot["DXY"] = {
            "price":      round(price, 2) if price else "N/A",
            "change_pct": round(chg, 2) if chg else 0
        }
    except Exception as e:
        logging.warning(f"DXY fetch error: {e}")
        snapshot["DXY"] = {"price": "N/A", "change_pct": 0}

    logging.info("Macro snapshot fetched via Finnhub")
    return snapshot


# ─────────────────────────────────────────
# LAYER 3 — FRED MACRO SERIES (optional)
# ─────────────────────────────────────────
def fetch_fred_data():
    if not fred:
        logging.warning("FRED_API_KEY not set — skipping FRED data")
        return {}

    series_map = {
        "Fed Funds Rate":    "FEDFUNDS",
        "CPI YoY":           "CPIAUCSL",
        "Core PCE":          "PCEPILFE",
        "Unemployment Rate": "UNRATE",
        "10Y-2Y Spread":     "T10Y2Y",
        "10Y-3M Spread":     "T10Y3M",
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
        d     = macro_snapshot.get(key, {})
        price = d.get("price", "N/A")
        chg   = d.get("change_pct", 0)
        arrow = "UP" if chg > 0 else "DN" if chg < 0 else "--"
        return f"{price} ({arrow} {abs(chg)}%)"

    calendar_text = "\n".join(
        [f"  - {e.get('time','?')} ET: {e.get('event','?')} | Forecast: {e.get('estimate','?')} | Prior: {e.get('prev','?')}"
         for e in calendar_events]
    ) if calendar_events else "No high-impact events today."

    fred_text = "\n".join(
        [f"  {k}: {v}" for k, v in fred_data.items()]
    ) if fred_data else "  FRED data unavailable."

    prompt = f"""You are ETHER, the market intelligence agent for a professional MES/MNQ micro futures trader named Chandler.

TODAY'S HIGH-IMPACT ECONOMIC EVENTS:
{calendar_text}

MACRO SNAPSHOT (pre-market):
  VIX  (Volatility)   : {fmt('VIX')}
  DXY  (Dollar Index) : {fmt('DXY')}
  SPX  (S&P 500)      : {fmt('SPX')}
  NDX  (Nasdaq)       : {fmt('NDX')}
  TNX  (10Y Yield)    : {fmt('TNX')}
  TYX  (30Y Yield)    : {fmt('TYX')}

FRED KEY INDICATORS:
{fred_text}

Generate a daily brief for Chandler covering:
1. MACRO BIAS - Bullish / Bearish / Neutral for MES and MNQ today and why
2. RISK EVENTS - Any events that could spike volatility during the 9:30am-3pm session
3. KEY LEVELS TO WATCH - Based on macro context
4. TRADE POSTURE - Aggressive, Normal, or Cautious and why

Keep it under 220 words. Plain text only, no special characters or symbols. Be direct. Speak like a sharp trading desk analyst."""

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )

    brief = response.content[0].text
    logging.info("Claude synthesis complete")
    return brief


# ─────────────────────────────────────────
# SYNAPSE MEMORY STORAGE
# ─────────────────────────────────────────
def store_in_synapse(brief, macro_snapshot):
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
        logging.info(f"SYNAPSE memory stored -- status {r.status_code}")
    except Exception as e:
        logging.error(f"SYNAPSE storage failed: {e}")


# ─────────────────────────────────────────
# TELEGRAM DELIVERY
# ─────────────────────────────────────────
def send_telegram(macro_snapshot, brief):
    def p(key):
        d     = macro_snapshot.get(key, {})
        price = d.get("price", "N/A")
        chg   = d.get("change_pct", 0)
        sign  = "+" if chg > 0 else ""
        return f"{price} ({sign}{chg}%)"

    now = datetime.now(ET).strftime("%b %d, %Y  %I:%M %p ET")

    # Escape any HTML special chars in the Claude brief
    safe_brief = html_lib.escape(brief)

    message = (
        f"<b>ETHER DAILY BRIEF</b>\n"
        f"<i>{now}</i>\n"
        f"--------------------\n"
        f"<b>MACRO SNAPSHOT</b>\n"
        f"VIX  {p('VIX')}\n"
        f"DXY  {p('DXY')}\n"
        f"SPX  {p('SPX')}\n"
        f"NDX  {p('NDX')}\n"
        f"10Y  {p('TNX')}\n"
        f"--------------------\n"
        f"<b>MARKET CONTEXT</b>\n"
        f"{safe_brief}\n"
        f"--------------------\n"
        f"<i>ETHER AI</i>"
    )

    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        logging.info(f"Telegram delivered -- status {resp.status_code}")
        if resp.status_code != 200:
            logging.error(f"Telegram error: {resp.text}")
    except Exception as e:
        logging.error(f"Telegram failed: {e}")


# ─────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────
def run_ether_fundamentals():
    logging.info("=== ETHER Fundamentals pipeline starting ===")
    try:
        calendar_events = fetch_economic_calendar()
        macro_snapshot  = fetch_macro_snapshot()
        fred_data       = fetch_fred_data()
        brief           = synthesize_with_claude(calendar_events, macro_snapshot, fred_data)

        store_in_synapse(brief, macro_snapshot)
        send_telegram(macro_snapshot, brief)

        logging.info("=== ETHER Fundamentals pipeline complete ===")
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
logging.info("Scheduler armed -- ETHER brief fires at 7:00 AM ET daily")


# ─────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
