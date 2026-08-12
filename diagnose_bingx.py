def main():
    api_key, api_secret = get_credentials()

    print("=" * 80)
    print("BingX orders diagnostics")
    print("=" * 80)
    print(f"BASE_URL   = {BASE_URL}")
    print(f"SYMBOL     = {SYMBOL}")
    print(f"PAGE_SIZE  = {PAGE_SIZE}")

    # -----------------------------------------------------------------------
    # ПРОВЕРКА СИМВОЛА
    # -----------------------------------------------------------------------
    print("\n>>> Проверяем точное название символа на бирже...")
    try:
        contracts_resp = bingx_get(
            "/openApi/swap/v2/quote/contracts",
            {},
            api_key,
            api_secret,
        )

        contracts = contracts_resp.get("data", []) if isinstance(contracts_resp, dict) else []
        prom_symbols = [
            c.get("symbol")
            for c in contracts
            if isinstance(c, dict) and "PROM" in str(c.get("symbol", "")).upper()
        ]

        if prom_symbols:
            print(f"    Найдены символы с PROM: {prom_symbols}")
            if SYMBOL not in prom_symbols:
                print(f"    ⚠️  ВНИМАНИЕ: запрошенный SYMBOL='{SYMBOL}' НЕ совпадает с найденными!")
                print(f"    Попробуйте перезапустить workflow с одним из этих символов.")
        else:
            print("    ⚠️  Символы с 'PROM' не найдены в списке контрактов.")
            print("    Возможно, торговля этой парой приостановлена или символ называется иначе.")

    except Exception as e:
        print(f"    Не удалось получить список контрактов: {e}")
        print("    Продолжаем диагностику с текущим SYMBOL...")

    # -----------------------------------------------------------------------
    # ЗАПРОС ИСТОРИИ ОРДЕРОВ (30 дней)
    # -----------------------------------------------------------------------
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
        print("\nВозможные причины:")
        print("  1. Ордер был старше 30 дней")
        print("  2. Неверное название символа (см. проверку выше)")
        print("  3. На этом аккаунте действительно нет ордеров по этому символу")
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
