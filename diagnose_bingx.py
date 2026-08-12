#!/usr/bin/env python3
"""
Диагностика BingX: Swap + Spot за последние 7 дней.
"""

import os
import sys
import time
import json
import hmac
import hashlib
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

BASE_URL = os.environ.get("BINGX_BASE_URL", "https://open-api.bingx.com").rstrip("/")
SYMBOL = os.environ.get("DIAGNOSE_SYMBOL", "PROM-USDT")
RECV_WINDOW = 5000

def get_credentials():
    api_key = os.environ.get("BINGX_API_KEY")
    api_secret = os.environ.get("BINGX_SECRET_KEY") or os.environ.get("BINGX_API_SECRET")
    if not api_key or not api_secret:
        print("❌ ERROR: Ключи не найдены в env.")
        sys.exit(1)
    print(f"✅ API Key loaded: {api_key[:4]}...{api_key[-4:] if len(api_key)>8 else ''}")
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
    print("🚀 BingX Diagnostics: Swap + Spot (Last 7 Days)")
    print("=" * 80)
    api_key, api_secret = get_credentials()
    print(f"SYMBOL: {SYMBOL}")

    # Ровно 7 дней, чтобы не получить ошибку 109400
    end_time = int(time.time() * 1000)
    start_time = end_time - (7 * 24 * 60 * 60 * 1000)

    params = {
        "symbol": SYMBOL,
        "limit": 100,
        "startTime": start_time,
        "endTime": end_time,
    }

    print(f"\n📅 Период: {time.strftime('%Y-%m-%d', time.localtime(start_time/1000))} — {time.strftime('%Y-%m-%d', time.localtime(end_time/1000))}")

    # 1. SWAP ALL ORDERS
    print("\n" + "=" * 80)
    print("1. SWAP: /openApi/swap/v2/trade/allOrders")
    print("=" * 80)
    resp_swap = bingx_get("/openApi/swap/v2/trade/allOrders", params, api_key, api_secret)
    if resp_swap:
        print(json.dumps(resp_swap, indent=2, default=str))
        data = resp_swap.get("data", {})
        orders = data.get("orders", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        print(f"👉 Найдено ордеров: {len(orders)}")

    # 2. SWAP FILL HISTORY
    print("\n" + "=" * 80)
    print("2. SWAP: /openApi/swap/v2/trade/fillHistory")
    print("=" * 80)
    resp_fill = bingx_get("/openApi/swap/v2/trade/fillHistory", params, api_key, api_secret)
    if resp_fill:
        print(json.dumps(resp_fill, indent=2, default=str))
        data = resp_fill.get("data", {})
        fills = data.get("fill_history_orders", []) if isinstance(data, dict) else []
        print(f"👉 Найдено сделок (fills): {len(fills)}")

    # 3. SPOT HISTORY ORDERS (на случай, если это был спот)
    print("\n" + "=" * 80)
    print("3. SPOT: /openApi/spot/v1/trade/historyOrders")
    print("=" * 80)
    resp_spot = bingx_get("/openApi/spot/v1/trade/historyOrders", params, api_key, api_secret)
    if resp_spot:
        print(json.dumps(resp_spot, indent=2, default=str))
        data = resp_spot.get("data", {})
        spot_orders = data.get("orders", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        print(f"👉 Найдено спотовых ордеров: {len(spot_orders)}")

    print("\n" + "=" * 80)
    print("✅ Диагностика завершена.")
    print("⚠️  Если ВЕЗДЕ 0, проверьте: 1) Те ли это API-ключи (сравните с UI). 2) Не был ли это копитрейдинг.")
    print("=" * 80)

if __name__ == "__main__":
    main()
