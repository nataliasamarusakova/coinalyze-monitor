#!/usr/bin/env python3
"""
Диагностика BingX DEMO: Все ордера Swap за 1 день.
"""

import os
import sys
import time
import json
import hmac
import hashlib
from urllib.parse import urlencode
import requests

# !!! ВАЖНО: URL для ДЕМО-счета (VST) !!!
BASE_URL = os.environ.get("BINGX_BASE_URL", "https://open-api-vst.bingx.com")
RECV_WINDOW = 5000

def get_credentials():
    api_key = os.environ.get("BINGX_API_KEY")
    api_secret = os.environ.get("BINGX_SECRET_KEY") or os.environ.get("BINGX_API_SECRET")
    if not api_key or not api_secret:
        print("❌ ERROR: Ключи не найдены в env.")
        sys.exit(1)
    print(f"✅ API Key loaded: {api_key[:4]}...{api_key[-4:] if len(api_key)>8 else ''}")
    print(f"✅ Target URL: {BASE_URL} (DEMO)")
    return api_key, api_secret

def sign_params(params: dict, api_secret: str) -> str:
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = RECV_WINDOW
    sorted_params = sorted(params.items(), key=lambda item: str(item[0]))
    query = urlencode(sorted_params)
    signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{query}&signature={signature}"

def bingx_get(path: str, params: dict, api_key: str, api_secret: str):
    query = sign_params(params, api_secret)
    url = f"{BASE_URL}{path}?{query}"
    headers = {"X-BX-APIKEY": api_key}
    
    print(f"🔗 Запрос: GET {path}")
    response = requests.get(url, headers=headers, timeout=30)
    
    if response.status_code != 200:
        print(f"❌ HTTP {response.status_code}: {response.text[:500]}")
        return None
    try:
        return response.json()
    except Exception:
        print(f"❌ Invalid JSON: {response.text[:500]}")
        return None

def main():
    print("=" * 80)
    print("🚀 BingX DEMO Diagnostics: ALL SWAP ORDERS (Last 24 Hours)")
    print("=" * 80)
    api_key, api_secret = get_credentials()

    # Диапазон: 1 день (24 часа)
    end_time = int(time.time() * 1000)
    start_time = end_time - (1 * 24 * 60 * 60 * 1000)

    params = {
        "limit": 100, 
        "startTime": start_time,
        "endTime": end_time,
    }

    print(f"\n📅 Период: {time.strftime('%Y-%m-%d %H:%M', time.localtime(start_time/1000))} — {time.strftime('%Y-%m-%d %H:%M', time.localtime(end_time/1000))}")

    print("\n" + "=" * 80)
    print("DEMO SWAP: /openApi/swap/v2/trade/allOrders")
    print("=" * 80)
    
    resp = bingx_get("/openApi/swap/v2/trade/allOrders", params, api_key, api_secret)
    
    if not resp:
        print("❌ Нет ответа от сервера.")
        return

    print("\n📦 FULL RAW RESPONSE:")
    print(json.dumps(resp, indent=2, default=str))
    
    data = resp.get("data", {})
    orders = data.get("orders", []) if isinstance(data, dict) else []
    
    print(f"\n👉 Всего ордеров в ответе: {len(orders)}")
    
    if orders:
        print("\n--- ПРИМЕРЫ ПОСЛЕДНИХ 10 ОРДЕРОВ (для анализа type/status) ---")
        for o in orders[-10:]:
            t = str(o.get('type')).upper()
            s = str(o.get('status')).upper()
            sym = o.get('symbol')
            exec_qty = o.get('executedQty')
            
            # Подсветка исполненных ордеров
            marker = "🔥" if s == "FILLED" and float(exec_qty or 0) > 0 else "  "
            print(f"{marker} Symbol: {sym:<12} | Type: {t:<20} | Status: {s:<15} | Executed: {exec_qty}")
            
        print("\n💡 Ищите ордер с executedQty > 0 и статусом FILLED.")
        print("   Посмотрите на поле 'type'. Если там 'MARKET' — гипотеза подтверждена!")
    else:
        print("\n⚠️ Список ордеров пуст за последние 24 часа.")

    print("\n" + "=" * 80)
    print("✅ Диагностика завершена")
    print("=" * 80)

if __name__ == "__main__":
    main()
