#!/usr/bin/env python3
"""
Диагностика BingX swap orders.

Секреты читаются только из environment variables:
    BINGX_API_KEY
    BINGX_SECRET_KEY

Также поддерживается fallback:
    BINGX_API_SECRET

Запуск локально:
    export BINGX_API_KEY="..."
    export BINGX_SECRET_KEY="..."
    python diagnose_bingx.py

Запуск в GitHub Actions:
    см. .github/workflows/diagnose_bingx.yml
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
    print("Install it with: pip install requests")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get(
    "BINGX_BASE_URL",
    "https://open-api.bingx.com",
).rstrip("/")

SYMBOL = os.environ.get("DIAGNOSE_SYMBOL", "PROM-USDT")

PAGE_SIZE = int(
    os.environ.get("DIAGNOSE_PAGE_SIZE", "50")
)

RECV_WINDOW = int(
    os.environ.get("BINGX_RECV_WINDOW", "5000")
)

INTERESTING_STATUSES = {
    "FILLED",
    "PARTIALLY_FILLED",
    "CLOSED",
    "CANCELED",
    "PARTIALLY_CANCELED",
}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def get_credentials():
    api_key = os.environ.get("BINGX_API_KEY")

    api_secret = (
        os.environ.get("BINGX_SECRET_KEY")
        or os.environ.get("BINGX_API_SECRET")
    )

    if not api_key:
        print("ERROR: BINGX_API_KEY is not set.")
        print("Set it as environment variable.")
        sys.exit(1)

    if not api_secret:
        print("ERROR: BINGX_SECRET_KEY is not set.")
        print("Set it as environment variable.")
        sys.exit(1)

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
    """
    BingX signature:
    1. Add timestamp.
    2. Sort parameters alphabetically.
    3. Create query string.
    4. HMAC-SHA256 query string with secret.
    5. Append signature.
    """

    params = dict(params)

    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = RECV_WINDOW

    sorted_params = sorted(
        params.items(),
        key=lambda item: str(item[0]),
    )

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

    headers = {
        "X-BX-APIKEY": api_key,
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=30,
    )

    if response.status_code != 200:
        print(f"HTTP ERROR: {response.status_code}")
        print("Response body:")
        print(response.text[:20000])
        response.raise_for_status()

    try:
        return response.json()
    except Exception:
        print("ERROR: response is not valid JSON.")
        print("Response body:")
        print(response.text[:20000])
        sys.exit(1)


def extract_orders(resp):
    """
    BingX may return:
        {
            "code": 0,
            "data": [...]
        }

    or:

        {
            "code": 0,
            "data": {
                "orders": [...]
            }
        }
    """

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

    print(f"\n### ORDER #{index} ###")
    print(f"status       = {status}")
    print(f"executedQty  = {executed_qty}")

    print("\nFULL ORDER JSON:")
    print(
        json.dumps(
            order,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )

    print("\nKEY FIELDS:")
    important_fields = [
        "orderId",
        "clientOrderId",
        "symbol",
        "side",
        "positionSide",
        "type",
        "orderType",
        "status",
        "price",
        "avgPrice",
        "quantity",
        "executedQty",
        "stopPrice",
        "triggerPrice",
        "reduceOnly",
        "closePosition",
        "createTime",
        "updateTime",
    ]

    for field in important_fields:
        print(f"    {field:<16} = {order.get(field)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    api_key, api_secret = get_credentials()

    print("=" * 80)
    print("BingX orders diagnostics")
    print("=" * 80)
    print(f"BASE_URL   = {BASE_URL}")
    print(f"SYMBOL     = {SYMBOL}")
    print(f"PAGE_SIZE  = {PAGE_SIZE}")

    params = {
        "symbol": SYMBOL,
        "pageSize": PAGE_SIZE,
    }

    try:
        resp = bingx_get(
            "/openApi/swap/v2/trade/allOrders",
            params,
            api_key,
            api_secret,
        )
    except requests.exceptions.HTTPError:
        print("Request failed with HTTP error.")
        sys.exit(1)
    except Exception:
        print("Unexpected error during request.")
        traceback.print_exc()
        sys.exit(1)

    print("\n" + "=" * 80)
    print("FULL RAW RESPONSE")
    print("=" * 80)

    print(
        json.dumps(
            resp,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )

    if isinstance(resp, dict):
        code = resp.get("code")

        if code not in (0, "0"):
            print("\nWARNING: BingX returned non-zero code.")
            print(f"code = {code}")
            print(f"msg  = {resp.get('msg')}")

    orders = extract_orders(resp)

    print("\n" + "=" * 80)
    print(f"Orders found: {len(orders)}")
    print("=" * 80)

    if not orders:
        print("No orders found in response.")
        return

    shown = 0

    for i, order in enumerate(orders, start=1):
        status = str(order.get("status", "")).upper()
        executed_qty = safe_float(order.get("executedQty"))

        if executed_qty > 0 or status in INTERESTING_STATUSES:
            print_order(i, order)
            shown += 1

    if shown == 0:
        print("No executed / interesting orders found.")

    print("\n" + "=" * 80)
    print("Diagnostics finished")
    print("=" * 80)


if __name__ == "__main__":
    main()
