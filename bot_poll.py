#!/usr/bin/env python3
"""StockGPT Bot - one-shot poll, process commands, exit. Run via cron every minute."""
import json, urllib.request

BOT_TOKEN = "8923022318:AAF_5KuqtRn1XaKk80CspPi_7fkMSfyZfXQ"
API_URL = "https://stock.wow.to/api.php"
STATE_FILE = "/Users/apple/stock-analysis/data/bot_last_update.txt"

def send_message(chat_id, text):
    data = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=data, headers={"Content-Type": "application/json"})
    try: urllib.request.urlopen(req, timeout=10)
    except: pass

def api_call(action, **data):
    import urllib.parse
    params = urllib.parse.urlencode({'action': action, **data}).encode()
    req = urllib.request.Request(API_URL, data=params)
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())

def api_call_safe(action, **data):
    """api_call with error handling — returns {} on failure."""
    try:
        return api_call(action, **data)
    except Exception:
        return {}

# Get last processed update_id
try:
    with open(STATE_FILE) as f:
        last_id = int(f.read().strip())
except:
    last_id = 0

# Poll pending updates
updates = json.load(urllib.request.urlopen(
    f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_id + 1}&timeout=2"
))

if updates.get("ok") and updates.get("result"):
    for update in updates["result"]:
        last_id = max(last_id, update["update_id"])
        msg = update.get("message")
        if not msg: continue
        
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "")
        name = msg["from"].get("first_name", "User")
        
        try:
            if text == "/start":
                api_call_safe("register", telegram_id=chat_id, name=name, plan="free")
                send_message(chat_id, f"""👋 歡迎 {name}！

我是 <b>StockGPT</b> 台股小幫手 📈

📋 <b>指令：</b>
/info — 查看帳戶方案
/stocks — 管理自選股  
/upgrade — 升級 Pro

⭐ <b>Pro 即時告警</b> NT$99/月
https://stock.wow.to/subscribe.php?plan=pro""")
        
            elif text == "/info":
                users = api_call_safe("get_active_users").get("users", [])
                user = next((u for u in users if u["telegram_id"] == chat_id), None)
                if user:
                    send_message(chat_id, f"""📊 <b>你的帳戶</b>
方案：{user['plan'].upper()}
自選股：{user['stocks']}
告警：跌 {user['alert_threshold']}%""")
                else:
                    send_message(chat_id, "未註冊。請輸入 /start")
        
            elif text.startswith("/stocks"):
                new = text.replace("/stocks", "").strip()
                if new:
                    result = api_call_safe("update_stocks", telegram_id=chat_id, stocks=new)
                    send_message(chat_id, f"✅ {result.get('message', '已更新')}")
                else:
                    send_message(chat_id, "用法：/stocks 2330.TW,2317.TW,2454.TW")
        
            elif text == "/upgrade":
                send_message(chat_id, """⭐ <b>升級方案</b>
Pro NT$99/月 → https://stock.wow.to/subscribe.php?plan=pro
Business NT$299/月 → https://stock.wow.to/subscribe.php?plan=business""")

            elif text.startswith("/admin_activate") and chat_id == "1023118254":
                parts = text.split()
                if len(parts) >= 3:
                    api_call_safe("subscribe", telegram_id=parts[1], plan=parts[2])
                    send_message(chat_id, "✅ 已開通")
        except Exception:
            pass  # don't let one bad update block others

    # Save last processed ID
    with open(STATE_FILE, "w") as f:
        f.write(str(last_id))
