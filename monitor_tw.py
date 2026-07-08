#!/usr/bin/env python3
"""Taiwan stock price monitor - checks prices and sends Telegram alerts"""
import os, sys, json, urllib.request, urllib.error
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Alert thresholds
ALERTS = {
    "2330.TW": {"name": "台積電", "drop_pct": 3, "rise_pct": 5},
    "2317.TW": {"name": "鴻海", "drop_pct": 4, "rise_pct": 6},
    "2454.TW": {"name": "聯發科", "drop_pct": 3, "rise_pct": 5},
    "3008.TW": {"name": "大立光", "drop_pct": 4, "rise_pct": 7},
}

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def get_tw_stock_price(code):
    """Fetch real-time price from TWSE API"""
    sym = code.replace(".TW", "").replace(".TWO", "")
    today = datetime.now().strftime("%Y%m%d")
    
    if ".TWO" in code:
        url = f"https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_download.php?l=zh-tw&stk={sym}&d={today}&s=0,asc,0"
    else:
        url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={today}&stockNo={sym}"
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        
        # For real-time, use TWSE's real-time API
        rt_url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{sym}.tw&json=1"
        rt_req = urllib.request.Request(rt_url, headers={"User-Agent": "Mozilla/5.0"})
        rt_resp = urllib.request.urlopen(rt_req, timeout=10)
        rt_data = json.loads(rt_resp.read())
        
        if rt_data.get("msgArray"):
            item = rt_data["msgArray"][0]
            price = float(item.get("z", "0"))  # 成交價
            if price == 0:
                price = float(item.get("y", "0"))  # 昨收
            prev = float(item.get("y", "0"))
            chg_pct = ((price - prev) / prev) * 100 if prev else 0
            return {"price": price, "prev_close": prev, "change_pct": round(chg_pct, 2)}
    except:
        return None
    return None

def check_alerts():
    triggered = []
    for code, info in ALERTS.items():
        result = get_tw_stock_price(code)
        if not result:
            print(f"  {code} {info['name']}: 無法取得報價")
            continue
        
        chg = result["change_pct"]
        price = result["price"]
        name = info["name"]
        
        print(f"  {code} {name}: {price} ({chg:+.2f}%)")
        
        if chg <= -info["drop_pct"]:
            msg = f"⚠️ <b>{name}</b> 下跌 {chg:.1f}%\n現價: {price} | 觸發: 跌{info['drop_pct']}% 告警"
            send_telegram(msg)
            triggered.append(f"{name}:{chg}%")
        
        if chg >= info["rise_pct"]:
            msg = f"🚀 <b>{name}</b> 上漲 {chg:.1f}%\n現價: {price} | 觸發: 漲{info['rise_pct']}% 告警"
            send_telegram(msg)
            triggered.append(f"{name}:+{chg}%")
    
    return triggered

if __name__ == "__main__":
    now = datetime.now().strftime("%H:%M")
    print(f"=== 台股監控 {now} ===")
    triggered = check_alerts()
    if triggered:
        print(f"觸發告警: {', '.join(triggered)}")
    else:
        print("無觸發")
