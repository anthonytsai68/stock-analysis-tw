#!/usr/bin/env python3
"""StockGPT multi-user monitor - reads users from stock.wow.to and sends alerts"""
import json, urllib.request, urllib.error, os, sys
from datetime import datetime

API_URL = "https://stock.wow.to/api.php"

def get_users():
    try:
        resp = urllib.request.urlopen(f"{API_URL}?action=get_active_users", timeout=10)
        return json.loads(resp.read()).get("users", [])
    except:
        return []

def send_tg_alert(token, chat_id, name, code, price, chg):
    msg = f"⚠️ <b>{name}</b> ({code}) 跌 {chg:.1f}% 至 {price}"
    data = json.dumps({"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data, headers={"Content-Type": "application/json"}
    )
    try: urllib.request.urlopen(req, timeout=10)
    except: pass

import yfinance as yf

MAIN_TOKEN = "8923022318:AAF_5KuqtRn1XaKk80CspPi_7fkMSfyZfXQ"
MAIN_CHAT = "1023118254"

# Get all active users
users = get_users()
print(f"Active users: {len(users)}")

for user in users:
    tg_id = user["telegram_id"]
    stocks = user["stocks"].split(",")
    threshold = user.get("alert_threshold", 3.0)
    plan = user["plan"]
    
    # Limit stocks by plan
    limit = 30 if plan == "pro" else 7
    stocks = stocks[:limit]
    
    for code in stocks:
        code = code.strip()
        if not code: continue
        try:
            t = yf.Ticker(code)
            info = t.info
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            prev = info.get("previousClose", 0)
            if price and prev:
                chg = round((price - prev) / prev * 100, 2)
                if chg <= -threshold:
                    name = info.get("shortName") or code
                    send_tg_alert(MAIN_TOKEN, tg_id, name, code, price, chg)
        except:
            pass

print("Done")
