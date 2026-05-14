import os
import io
import csv
import html as html_lib
import zipfile
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
fred             = Fred(api_key=FRED_API_KEY) if FRED_API_KEY else None


# ─────────────────────────────────────────
# LAYER 1 — ECONOMIC CALENDAR (Finnhub)
# ─────────────────────────────────────────
def fetch_economic_calendar():
    today    = datetime.now(ET).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(ET) + timedelta(days=1)).strftime("%Y-%m-%d")

    url    = f"https://finnhub.io/api/v1/calendar/economic?from={today}&to={tomorrow}&token={FINNHUB_API_KEY}"
    resp   = requests.get(url, timeout=10)
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
            snapshot[name] = {
                "price":      round(price, 2) if price else "N/A",
                "change_pct": round(chg, 2) if chg else 0
            }
        except Exception as e:
            logging.warning(f"Finnhub quote error for {name}: {e}")
            snapshot[name] = {"price": "N/A", "change_pct": 0}

    # DXY via Finnhub forex
    try:
        url  = f"https://finnhub.io/api/v1/quote?symbol=OANDA:US30_USD&token={FINNHUB_API_KEY}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        snapshot["DXY"] = {
            "price":      round(data.get("c", 0), 2) if data.get("c") else "N/A",
            "change_pct": round(data.get("dp", 0), 2) if data.get("dp") else 0
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
# LAYER 5 — COT REPORT (CFTC)
# Tracks hedge fund + asset manager positioning
# in S&P 500 and Nasdaq 100 futures
# ─────────────────────────────────────────
def fetch_cot_data():
    year = datetime.now(ET).year
    url  = f"https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip"

    TARGET_MARKETS = {
        "S&P 500 STOCK INDEX":   "ES",
        "NASDAQ-100 STOCK INDEX": "NQ",
    }

    cot = {}
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            logging.warning(f"COT fetch failed: status {resp.status_code}")
            return {}

        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            filename = [f for f in z.namelist() if f.endswith(".txt")][0]
            with z.open(filename) as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))

                # Collect all rows then pick latest per market
                latest = {}
                for row in reader:
                    market = row.get("Market and Exchange Names", "")
                    for target, code in TARGET_MARKETS.items():
                        if target in market.upper():
                            latest[code] = row  # overwrites → keeps last (most recent)

                for code, row in latest.items():
                    try:
                        # Asset managers (institutional longs) — smart money
                        am_long  = int(row.get("Asset Mgr Positions-Long All",  "0").replace(",", ""))
                        am_short = int(row.get("Asset Mgr Positions-Short All", "0").replace(",", ""))
                        am_net   = am_long - am_short

                        # Leveraged funds (hedge funds) — speculative
                        lf_long  = int(row.get("Lev Money Positions-Long All",  "0").replace(",", ""))
                        lf_short = int(row.get("Lev Money Positions-Short All", "0").replace(",", ""))
                        lf_net   = lf_long - lf_short

                        am_bias = "BULLISH" if am_net > 0 else "BEARISH"
                        lf_bias = "BULLISH" if lf_net > 0 else "BEARISH"

                        cot[code] = {
                            "report_date":    row.get("As of Date in Form YYYY-MM-DD", "unknown"),
                            "asset_mgr_net":  am_net,
                            "asset_mgr_bias": am_bias,
                            "hedge_fund_net":  lf_net,
                            "hedge_fund_bias": lf_bias,
                        }
                        logging.info(f"COT {code}: AM={am_bias}({am_net:+,}) HF={lf_bias}({lf_net:+,})")
                    except Exception as e:
                        logging.warning(f"COT parse error for {code}: {e}")

    except Exception as e:
        logging.warning(f"COT fetch error: {e}")

    return cot


# ─────────────────────────────────────────
# LAYER 6 — CBOE PUT/CALL RATIO
# Equity P/C > 0.8 = fear (contrarian bullish)
# Equity P/C < 0.5 = greed (contrarian bearish)
# ─────────────────────────────────────────
def fetch_put_call_ratio():
    try:
        url  = "https://cdn.cboe.com/api/global/us_indices/daily_prices/PC_NEW.csv"
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            logging.warning(f"CBOE P/C fetch failed: {resp.status_code}")
            return {}

        lines  = resp.text.strip().split("\n")
        reader = csv.DictReader(lines)
        rows   = list(reader)

        if not rows:
            return {}

        latest = rows[-1]
        total  = float(latest.get("TOTAL PUT/CALL RATIO", 0) or 0)
        equity = float(latest.get("EQUITY PUT/CALL RATIO", 0) or 0)
        index  = float(latest.get("INDEX PUT/CALL RATIO", 0) or 0)
        date   = latest.get("DATE", "unknown")

        # Sentiment interpretation
        if equity > 0.8:
            sentiment = "FEAR — contrarian bullish signal"
        elif equity > 0.65:
            sentiment = "ELEVATED — cautious market"
        elif equity < 0.5:
            sentiment = "GREED — contrarian bearish signal"
        else:
            sentiment = "NEUTRAL"

        result = {
            "date":      date,
            "total":     round(total, 2),
            "equity":    round(equity, 2),
            "index":     round(index, 2),
            "sentiment": sentiment,
        }
        logging.info(f"CBOE P/C: equity={equity} — {sentiment}")
        return result

    except Exception as e:
        logging.warning(f"Put/call ratio fetch error: {e}")
        return {}


# ─────────────────────────────────────────
# CLAUDE SYNTHESIS (all layers)
# ─────────────────────────────────────────
def synthesize_with_claude(calendar_events, macro_snapshot, fred_data, cot_data, put_call):
    def fmt(key):
        d     = macro_snapshot.get(key, {})
        price = d.get("price", "N/A")
        chg   = d.get("change_pct", 0)
        arrow = "UP" if chg > 0 else "DN" if chg < 0 else "--"
        return f"{price} ({arrow} {abs(chg)}%)"

    calendar_text = "\n".join(
        [f"  - {e.get('time','?')} ET: {e.get('event','?')} | Forecast: {e.get('estimate','?')} | Prior: {e.get('prev','?')}"
         for e in calendar_events]
    ) if calendar_events else "  No high-impact events today."

    fred_text = "\n".join(
        [f"  {k}: {v}" for k, v in fred_data.items()]
    ) if fred_data else "  FRED data unavailable."

    # COT text
    cot_lines = []
    for code, data in cot_data.items():
        cot_lines.append(
            f"  {code}: Asset Mgrs {data['asset_mgr_bias']} ({data['asset_mgr_net']:+,}) | "
            f"Hedge Funds {data['hedge_fund_bias']} ({data['hedge_fund_net']:+,}) | "
            f"as of {data['report_date']}"
        )
    cot_text = "\n".join(cot_lines) if cot_lines else "  COT data unavailable."

    # Put/call text
    if put_call:
        pc_text = (
            f"  Total P/C: {put_call.get('total','N/A')} | "
            f"Equity P/C: {put_call.get('equity','N/A')} | "
            f"Index P/C: {put_call.get('index','N/A')}\n"
            f"  Sentiment: {put_call.get('sentiment','N/A')} | as of {put_call.get('date','?')}"
        )
    else:
        pc_text = "  Put/call data unavailable."

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

COT POSITIONING (CFTC — weekly):
{cot_text}

CBOE PUT/CALL RATIO (daily):
{pc_text}

Generate a daily brief for Chandler covering:
1. MACRO BIAS - Bullish / Bearish / Neutral for MES and MNQ today and why
2. ORDER FLOW EDGE - What COT positioning and put/call ratio tell us about institutional intent
3. RISK EVENTS - Any events that could spike volatility during the 9:30am-3pm session
4. TRADE POSTURE - Aggressive, Normal, or Cautious and why

Keep it under 250 words. Plain text only, no special characters. Be direct. Speak like a sharp trading desk analyst."""

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )

    brief = response.content[0].text
    logging.info("Claude synthesis complete")
    return brief


# ─────────────────────────────────────────
# SYNAPSE MEMORY STORAGE
# ─────────────────────────────────────────
def store_in_synapse(brief, macro_snapshot, cot_data, put_call):
    tags = ["fundamentals", "daily-brief", "macro", "order-flow",
            f"vix-{macro_snapshot.get('VIX', {}).get('price', 'unknown')}"]

    if put_call.get("sentiment"):
        tags.append(put_call["sentiment"].split(" ")[0].lower())

    payload = {
        "agentId":    "ether",
        "memoryType": "market",
        "content":    brief,
        "importance": 8,
        "tags":       tags
    }
    try:
        r = requests.post(f"{SYNAPSE_URL}/memory", json=payload, timeout=10)
        logging.info(f"SYNAPSE memory stored -- status {r.status_code}")
    except Exception as e:
        logging.error(f"SYNAPSE storage failed: {e}")


# ─────────────────────────────────────────
# TELEGRAM DELIVERY
# ─────────────────────────────────────────
def send_telegram(macro_snapshot, cot_data, put_call, brief):
    def p(key):
        d    = macro_snapshot.get(key, {})
        price = d.get("price", "N/A")
        chg   = d.get("change_pct", 0)
        sign  = "+" if chg > 0 else ""
        return f"{price} ({sign}{chg}%)"

    now = datetime.now(ET).strftime("%b %d, %Y  %I:%M %p ET")
    safe_brief = html_lib.escape(brief)

    # COT block
    cot_block = ""
    for code, data in cot_data.items():
        am  = f"AM: {data['asset_mgr_bias']} ({data['asset_mgr_net']:+,})"
        hf  = f"HF: {data['hedge_fund_bias']} ({data['hedge_fund_net']:+,})"
        cot_block += f"{code}  {am} | {hf}\n"
    if not cot_block:
        cot_block = "COT data unavailable\n"

    # Put/call block
    if put_call:
        pc_block = (
            f"Equity P/C: {put_call.get('equity','N/A')}  |  "
            f"Total P/C: {put_call.get('total','N/A')}\n"
            f"{put_call.get('sentiment','N/A')}"
        )
    else:
        pc_block = "Put/call data unavailable"

    message = (
        f"<b>ETHER DAILY BRIEF</b>\n"
        f"<i>{now}</i>\n"
        f"--------------------\n"
        f"<b>MACRO</b>\n"
        f"VIX  {p('VIX')}\n"
        f"DXY  {p('DXY')}\n"
        f"SPX  {p('SPX')}\n"
        f"NDX  {p('NDX')}\n"
        f"10Y  {p('TNX')}\n"
        f"--------------------\n"
        f"<b>ORDER FLOW</b>\n"
        f"{html_lib.escape(cot_block)}"
        f"{html_lib.escape(pc_block)}\n"
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
    logging.info("=== ETHER pipeline starting ===")
    try:
        calendar_events = fetch_economic_calendar()
        macro_snapshot  = fetch_macro_snapshot()
        fred_data       = fetch_fred_data()
        cot_data        = fetch_cot_data()
        put_call        = fetch_put_call_ratio()
        brief           = synthesize_with_claude(calendar_events, macro_snapshot, fred_data, cot_data, put_call)

        store_in_synapse(brief, macro_snapshot, cot_data, put_call)
        send_telegram(macro_snapshot, cot_data, put_call, brief)

        logging.info("=== ETHER pipeline complete ===")
        return {
            "status":       "ok",
            "events":       len(calendar_events),
            "cot_markets":  list(cot_data.keys()),
            "put_call":     put_call.get("equity", "N/A"),
            "brief_length": len(brief)
        }
    except Exception as e:
        logging.error(f"Pipeline error: {e}")
        return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────
# FLASK ENDPOINTS
# ─────────────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "live", "service": "ETHER Intelligence", "agent": "ether",
                    "layers": ["economic_calendar", "macro_snapshot", "fred", "cot", "put_call"]})

@app.route("/run", methods=["POST"])
def manual_run():
    result = run_ether_fundamentals()
    return jsonify(result)

@app.route("/snapshot", methods=["GET"])
def snapshot():
    return jsonify(fetch_macro_snapshot())

@app.route("/calendar", methods=["GET"])
def calendar():
    events = fetch_economic_calendar()
    return jsonify({"count": len(events), "events": events})

@app.route("/cot", methods=["GET"])
def cot():
    return jsonify(fetch_cot_data())

@app.route("/putcall", methods=["GET"])
def putcall():
    return jsonify(fetch_put_call_ratio())


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
