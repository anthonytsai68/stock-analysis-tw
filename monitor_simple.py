#!/usr/bin/env python3
"""Simple Taiwan stock price monitor using yfinance"""
import os, sys, json, urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STOCKS = {
    "2330.TW": "台積電", "2317.TW": "鴻海", "2454.TW": "聯發科",
    "3008.TW": "大立光", "2308.TW": "台達電", "2881.TW": "富邦金",
    "3711.TW": "日月光"
}

import yfinance as yf

def send_tg(msg):
    if not TOKEN or not CHAT_ID: return
    data = json.dumps({"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data=data, headers={"Content-Type": "application/json"}
    )
    try: urllib.request.urlopen(req, timeout=10)
    except: pass

if __name__ == "__main__":
    now = datetime.now().strftime("%H:%M")
    alerts = []
    
    for code, name in STOCKS.items():
        try:
            t = yf.Ticker(code)
            info = t.info
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            prev = info.get("previousClose", 0)
            if price and prev:
                chg = round((price - prev) / prev * 100, 2)
                print(f"  {code} {name}: {price} ({chg:+.2f}%)")
                
                if chg <= -3:
                    alerts.append(f"⚠️ <b>{name}</b> 跌 {chg:.1f}% 至 {price}")
                elif chg >= 5:
                    alerts.append(f"🚀 <b>{name}</b> 漲 {chg:.1f}% 至 {price}")
            else:
                print(f"  {code} {name}: N/A")
        except:
            print(f"  {code} {name}: error")
    
    if alerts:
        msg = f"📊 台股告警 {now}\n" + "\n".join(alerts)
        send_tg(msg)
    else:
        print(f"  [{now}] 無觸發")
