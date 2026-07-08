#!/usr/bin/env python3
"""US stock monitor - only sends alerts during US market hours"""
import json, urllib.request
import yfinance as yf

TOKEN = "8923022318:AAF_5KuqtRn1XaKk80CspPi_7fkMSfyZfXQ"
CHAT_ID = "1023118254"

# Only check US stocks during US market hours (Mon-Fri, 21:30-04:00 Taiwan time)
# Skip if it's before 21:30 Taiwan time on weekdays (weekend in US)
import datetime as dt
_now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
if _now.weekday() == 0 and _now.hour < 21:  # Monday before 21:00 = Sunday US
    raise SystemExit(0)
if _now.weekday() == 5 or _now.weekday() == 6:  # Weekend in Taiwan = weekend in US
    raise SystemExit(0)

STOCKS = {
    "NVDA": "輝達", "TSM": "台積電ADR", "AAPL": "蘋果",
    "META": "Meta", "GOOG": "Google", "SPCX": "SpaceX", "MU": "美光"
}

results = []
for code, name in STOCKS.items():
    try:
        t = yf.Ticker(code)
        info = t.info
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        prev = info.get("previousClose", 0)
        if price and prev:
            chg = round((price - prev) / prev * 100, 2)
            results.append((name, code, price, chg))
    except:
        pass

alerts = [r for r in results if r[3] <= -3]
if alerts:
    lines = ["⚠️ <b>美股告警</b>"]
    for name, code, price, chg in alerts:
        lines.append(f"🔻 {name} ({code}) 跌 {chg:.1f}% 至 {price}")
    data = json.dumps({"chat_id": CHAT_ID, "text": "\n".join(lines), "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)
