#!/usr/bin/env python3
"""
Диагностика BingX swap orders.
Выводит результат ПРЯМО В ЛОГИ GitHub Actions.

Секреты читаются из environment variables:
    BINGX_API_KEY
    BINGX_SECRET_KEY
"""

import os
import sys
import time
import json
import hmac
import hashlib
import traceback
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    print("ERROR: requests is not installed.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("BINGX_BASE_URL", "https://open-api.bingx.com").rstrip("/")
SYMBOL = os.environ.get("DIAGNOSE_SYMBOL", "PROM-USDT")
PAGE_SIZE = int(os.environ.get("DIAGNOSE_PAGE_SIZE", "50"))
RECV_WINDOW = int(os.environ.get("BINGX_RECV_WINDOW", "5000"))

INTERESTING_STATUSES = {"FILLED", "PARTIALLY_FILLED", "CLOSED", "CANCELED", "PARTIALLY_CANCELED"}

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def get_credentials():
    api_key = os.environ.get("BINGX_API_KEY")
    api_secret = os.environ.get("BINGX_SECRET_KEY") or os.environ.get("BINGX_API_SECRET")

    if not api_key:
        print("❌ ERROR: BINGX_API_KEY is not set.")
        sys.exit(1)
    if not api_secret:
        print("❌ ERROR: BINGX_SECRET_KEY is not set.")
        sys.exit(1)

    # Маскируем ключ для логов, чтобы не светить его полностью
    print(f"✅ API Key loaded: {api_key[:4]}...{api_key[-4:] if len(api_key)>8 else ''}")
    return api_key, api_secret

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_float(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0

def sign_params(params: dict, api_secret: str) -> str:
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = RECV_WINDOW

    sorted_params = sorted(params.items(), key=lambda item: str(item[0]))
    query = urlencode(sorted_params)

    signature = hmac.new(
        api_secret.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return f"{query}&signature={signature}"

def bingx_get(path: str, params: dict, api_key: str, api_secret: str):
    query = sign_params(params, api_secret)
    url = f"{BASE_URL}{path}?{query}"

    headers = {"X-BX-APIKEY": api_key}
    response = requests.get(url, headers=headers, timeout=30)

    if response.status_code != 200:
        print(f"❌ HTTP ERROR: {response.status_code}")
        print("Response body:")
        print(response.text[:2000])
        response.raise_for_status()

    try:
        return response.json()
    except Exception:
        print("❌ ERROR: response is not valid JSON.")
        print("Response body:")
        print(response.text[:2000])
        sys.exit(1)

def extract_orders(resp):
    if isinstance(resp, list):
        return resp
    if not isinstance(resp, dict):
        return []
    
    data = resp.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        orders = data.get("orders")
        if isinstance(orders, list):
            return orders
    return []

def print_order(index: int, order: dict):
    status = str(order.get("status", "")).upper()
    executed_qty = safe_float(order.get("executedQty"))

    print(f"\n🔹 ORDER #{index} 🔹")
    print(f"  status       = {status}")
    print(f"  executedQty  = {executed_qty}")
    print("\n  FULL ORDER JSON:")
    print(json.dumps(order, ensure_ascii=False, indent=2, default=str))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 80)
    print("🚀 BingX Orders Diagnostics (Direct Log Output)")
    print("=" * 80)
    
    api_key, api_secret = get_credentials()

    print(f"BASE_URL   = {BASE_URL}")
    print(f"SYMBOL     = {SYMBOL}")
    print(f"PAGE_SIZE  = {PAGE_SIZE}")

    # 1. Проверка символа
    print("\n>>> Проверяем точное название символа на бирже...")
    try:
        contracts_resp = bingx_get("/openApi/swap/v2/quote/contracts", {}, api_key, api_secret)
        contracts = contracts_resp.get("data", []) if isinstance(contracts_resp, dict) else []
        
        prom_symbols = [
            c.get("symbol") for c in contracts 
            if isinstance(c, dict) and "PROM" in str(c.get("symbol", "")).upper()
        ]
        
        if prom_symbols:
            print(f"    ✅ Найдены символы с PROM: {prom_symbols}")
            if SYMBOL not in prom_symbols:
                print(f"    ⚠️  ВНИМАНИЕ: запрошенный SYMBOL='{SYMBOL}' НЕ совпадает с найденными!")
        else:
            print("    ⚠️  Символы с 'PROM' не найдены в списке контрактов.")
    except Exception as e:
        print(f"    ⚠️  Не удалось получить список контрактов: {e}")

    # 2. Запрос истории ордеров (30 дней)
    end_time = int(time.time() * 1000)
    start_time = end_time - (30 * 24 * 60 * 60 * 1000)  # 30 дней назад

    params = {
        "symbol": SYMBOL,
        "pageSize": PAGE_SIZE,
        "startTime": start_time,
        "endTime": end_time,
    }

    print(f"\n>>> Запрашиваем ордера за период:")
    print(f"    startTime = {start_time}")
    print(f"    endTime   = {end_time}")

    try:
        resp = bingx_get("/openApi/swap/v2/trade/allOrders", params, api_key, api_secret)
    except Exception:
        print("❌ Unexpected error during request.")
        traceback.print_exc()
        sys.exit(1)

    print("\n" + "=" * 80)
    print("📦 FULL RAW RESPONSE")
    print("=" * 80)
    print(json.dumps(resp, ensure_ascii=False, indent=2, default=str))

    if isinstance(resp, dict) and resp.get("code") not in (0, "0"):
        print(f"\n⚠️  WARNING: BingX returned non-zero code: {resp.get('code')}")
        print(f"    msg: {resp.get('msg')}")

    orders = extract_orders(resp)

    print("\n" + "=" * 80)
    print(f"📊 Orders found in response: {len(orders)}")
    print("=" * 80)

    if not orders:
        print("❌ No orders found in response.")
        print("\nВозможные причины:")
        print("  1. Ордер был старше 30 дней")
        print("  2. Неверное название символа (см. проверку выше)")
        print("  3. На этом аккаунте действительно нет ордеров по этому символу")
        print("  4. Попробуем альтернативный эндпоинт: /openApi/swap/v2/trade/fillHistory")
        
        # Попытка проверить fillHistory, если allOrders пуст
        print("\n>>> Пробуем запросить fillHistory (история сделок)...")
        try:
            fill_resp = bingx_get("/openApi/swap/v2/trade/fillHistory", params, api_key, api_secret)
            print("📦 FILL HISTORY RAW RESPONSE:")
            print(json.dumps(fill_resp, ensure_ascii=False, indent=2, default=str))
        except Exception as e:
            print(f"    Ошибка при запросе fillHistory: {e}")
            
        return

    shown = 0
    for i, order in enumerate(orders, start=1):
        status = str(order.get("status", "")).upper()
        executed_qty = safe_float(order.get("executedQty"))

        if executed_qty > 0 or status in INTERESTING_STATUSES:
            print_order(i, order)
            shown += 1

    if shown == 0:
        print("⚠️  No executed / interesting orders found in the list.")

    print("\n" + "=" * 80)
    print("✅ Diagnostics finished")
    print("=" * 80)

if __name__ == "__main__":
    main()
