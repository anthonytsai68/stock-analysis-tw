#!/usr/bin/env python3
"""StockGPT Telegram Bot - handles user onboarding and commands"""

import os, sys, json, urllib.request, urllib.parse
from datetime import datetime

BOT_TOKEN = "8923022318:AAF_5KuqtRn1XaKk80CspPi_7fkMSfyZfXQ"
API_URL = "https://stock.wow.to/api.php"

def api_call(action, **data):
    """Call the stock.wow.to API"""
    params = urllib.parse.urlencode({'action': action, **data}).encode()
    req = urllib.request.Request(API_URL, data=params)
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())

def send_message(chat_id, text):
    data = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=data, headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=10)

def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    if offset:
        url += f"?offset={offset}"
    resp = urllib.request.urlopen(url, timeout=30)
    return json.loads(resp.read())

def handle_update(msg):
    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "")
    name = msg["from"].get("first_name", "User")
    
    if text == "/start":
        # Register user
        result = api_call("register", telegram_id=chat_id, name=name, plan="free")
        welcome = f"""👋 歡迎 {name}！

我是 <b>StockGPT</b> 台股小幫手 📈

<b>目前功能：</b>
• 每日 AI 分析報告
• 即時價格告警

<b>升級 Pro 方案：</b>
https://stock.wow.to

<b>指令：</b>
/stocks — 查看/修改自選股
/info — 查看帳戶資訊
/help — 求助
"""
        send_message(chat_id, welcome)
    
    elif text == "/help":
        help_text = """📋 <b>可用指令：</b>

/info — 查看你的方案與自選股
/stocks 2330.TW,2317.TW — 更新自選股
/upgrade — 升級方案

🆓 Free：7 檔台股每日報告
⭐ Pro：30 檔 + 即時告警 (NT$99/月)
💼 Business：全市場無限 (NT$299/月)
"""
        send_message(chat_id, help_text)
    
    elif text == "/info":
        users = api_call("get_active_users").get("users", [])
        user = next((u for u in users if u["telegram_id"] == chat_id), None)
        if user:
            info = f"""📊 <b>你的帳戶</b>

方案：{user['plan'].upper()}
自選股：{user['stocks']}
告警門檻：跌 {user['alert_threshold']}%
"""
            send_message(chat_id, info)
        else:
            send_message(chat_id, "尚未註冊。請輸入 /start")
    
    elif text.startswith("/stocks"):
        new_stocks = text.replace("/stocks", "").strip()
        if not new_stocks:
            send_message(chat_id, "用法：/stocks 2330.TW,2317.TW,2454.TW")
            return
        try:
            result = api_call("update_stocks", telegram_id=chat_id, stocks=new_stocks)
            send_message(chat_id, f"✅ {result.get('message', '自選股已更新')}")
        except Exception as e:
            send_message(chat_id, f"❌ 更新失敗：{e}")
    
    elif text == "/upgrade":
        send_message(chat_id, """⭐ <b>升級方案：</b>

<b>Pro NT$99/月</b>
https://stock.wow.to/subscribe.php?plan=pro

<b>Business NT$299/月</b>
https://stock.wow.to/subscribe.php?plan=business

付款後回傳訂單編號，24 小時內開通！""")
    
    elif text.startswith("/admin_activate"):
        # Admin command: activate a subscription
        # Usage: /admin_activate telegram_id plan
        parts = text.split()
        if len(parts) >= 3:
            tg_id = parts[1]
            plan = parts[2]
            result = api_call("subscribe", telegram_id=tg_id, plan=plan)
            send_message(chat_id, f"✅ {result.get('message', 'Done')}")

if __name__ == "__main__":
    last_update = 0
    while True:
        try:
            updates = get_updates(offset=None)
            for update in updates.get("result", []):
                uid = update["update_id"]
                if uid > last_update:
                    last_update = uid
                    if "message" in update:
                        handle_update(update["message"])
            # Mark updates as read
            get_updates(offset=last_update + 1)
        except Exception as e:
            pass
