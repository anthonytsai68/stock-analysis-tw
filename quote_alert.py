#!/usr/bin/env python3
"""Taiwan stock monitor + Telegram alerts via @Antjony_StockBot"""
import json, urllib.request
import yfinance as yf

TOKEN = "8923022318:AAF_5KuqtRn1XaKk80CspPi_7fkMSfyZfXQ"
CHAT_ID = "1023118254"

# Only run during Taiwan trading hours (Mon-Fri 09:00-13:30)
import datetime as dt
_now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
if _now.weekday() >= 5 or _now.hour < 9 or _now.hour >= 14:
    raise SystemExit(0)

STOCKS = {
    "2330.TW": "台積電", "2317.TW": "鴻海", "2454.TW": "聯發科",
    "3008.TW": "大立光", "2308.TW": "台達電", "2881.TW": "富邦金",
    "3711.TW": "日月光"
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
    lines = ["⚠️ <b>台股告警</b>"]
    for name, code, price, chg in alerts:
        lines.append(f"🔻 {name} ({code}) 跌 {chg:.1f}% 至 {price}")
    data = json.dumps({"chat_id": CHAT_ID, "text": "\n".join(lines), "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)
