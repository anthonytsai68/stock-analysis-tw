#!/usr/bin/env python3
"""Taiwan stock price monitor - outputs to stdout for cron delivery"""
import os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import yfinance as yf

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
            results.append((code, name, price, chg))
        else:
            results.append((code, name, "N/A", 0))
    except:
        results.append((code, name, "err", 0))

now = datetime.now().strftime("%H:%M")
print(f"📊 台股即時報價 ({now})")
print()
for code, name, price, chg in results:
    icon = "🔴" if chg < -2 else "🟢" if chg > 2 else "⚪"
    print(f"{icon} {name} ({code}): {price} ({chg:+.2f}%)")

print()
alerts = [r for r in results if isinstance(r[2], (int, float)) and r[3] <= -3]
if alerts:
    print(f"⚠️ 跌逾 3% 告警:")
    for _, name, price, chg in alerts:
        print(f"  🔻 {name} 跌 {chg:.1f}% 至 {price}")
