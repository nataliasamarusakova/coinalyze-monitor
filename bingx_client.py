import os, time, hmac, hashlib, math, logging, requests
from urllib.parse import urlencode

log = logging.getLogger("bingx_client")

API_KEY = os.environ.get("BINGX_API_KEY", "")
SECRET_KEY = os.environ.get("BINGX_SECRET_KEY", "")
BASE_URL = "https://open-api-vst.bingx.com"  # ДЕМО (VST)

ORDER_PATH = "/openApi/swap/v2/trade/order"
CONTRACTS_PATH = "/openApi/swap/v2/quote/contracts"
POSITION_PATH = "/openApi/swap/v2/trade/position"
LEVERAGE_PATH = "/openApi/swap/v2/trade/leverage"

LEVERAGE = 10      # Плечо (можно любое для демо)
MARGIN_USDT = 1.0  # Маржа 1 бакс

def _sign(params: dict) -> str:
    query_string = urlencode(sorted(params.items()))
    return hmac.new(SECRET_KEY.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

def _request(method: str, path: str, params: dict, signed: bool = True):
    if signed:
        params["timestamp"] = str(int(time.time() * 1000))
        params["signature"] = _sign(params)
    headers = {"X-BX-APIKEY": API_KEY} if signed else {}
    url = BASE_URL + path
    try:
        resp = requests.request(method, url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"BingX request error {path}: {e}")
        return {"code": -1, "msg": str(e)}

def _to_bx_symbol(symbol: str) -> str:
    return symbol.replace("USDT", "-USDT") if not symbol.endswith("-USDT") else symbol

def ensure_leverage(bx_symbol: str, leverage: int = LEVERAGE):
    """Выставляет плечо перед первым ордером (обязательно для корректной маржи)."""
    resp = _request("POST", LEVERAGE_PATH, {
        "symbol": bx_symbol,
        "side": "LONG",
        "leverage": str(leverage),
    })
    if resp.get("code") != 0:
        log.warning(f"[{bx_symbol}] set leverage failed: {resp}")

def get_open_position(bx_symbol: str):
    """Проверка, есть ли уже открытая позиция (защита от дублей)."""
    resp = _request("GET", POSITION_PATH, {"symbol": bx_symbol})
    if resp.get("code") == 0:
        for p in resp.get("data", []):
            if p.get("positionSide") in ["LONG", "BOTH"] and float(p.get("positionAmt", 0)) > 0:
                return float(p.get("positionAmt", 0))
    return 0.0

def calc_quantity(bx_symbol: str, mark_price: float):
    """
    Рассчитывает количество КОНТРАКТОВ под маржу MARGIN_USDT.
    Использует multiplier, quantityPrecision и minQty из спецификации контракта.
    """
    if not mark_price or mark_price <= 0:
        log.error(f"[{bx_symbol}] invalid mark_price: {mark_price}")
        return None

    resp = _request("GET", CONTRACTS_PATH, {})
    if resp.get("code") != 0:
        log.error(f"[{bx_symbol}] failed to fetch contracts: {resp}")
        return None

    for c in resp.get("data", []):
        if c.get("symbol") == bx_symbol:
            q_precision = int(c.get("quantityPrecision", 0))
            min_qty = float(c.get("minQty", 1))
            multiplier = float(c.get("multiplier", 1))

            notional = MARGIN_USDT * LEVERAGE  # 1$ × 10 = 10 USDT
            raw_qty = notional / (mark_price * multiplier)

            step = 10 ** -q_precision
            qty = math.floor(raw_qty / step) * step
            qty = round(qty, q_precision)

            if qty < min_qty:
                log.error(f"[{bx_symbol}] qty {qty} < minQty {min_qty}. "
                          f"notional={notional}, price={mark_price}, multiplier={multiplier}")
                return None

            log.info(f"[{bx_symbol}] qty={qty} (notional={notional}$, "
                     f"price={mark_price}, mult={multiplier}, precision={q_precision})")
            return qty

    log.error(f"[{bx_symbol}] contract not found in /quote/contracts")
    return None

def open_long_market_order(symbol: str, current_price: float):
    """Возвращает (order_id, quantity) или (None, None)."""
    bx_symbol = _to_bx_symbol(symbol)

    # 1. Защита от дублей
    open_amt = get_open_position(bx_symbol)
    if open_amt > 0:
        log.info(f"[{symbol}] Позиция уже открыта (amt={open_amt}), пропускаем")
        return "ALREADY_OPEN", open_amt

    # 2. Плечо (один раз на тикер, но дёшево вызывать)
    ensure_leverage(bx_symbol, LEVERAGE)

    # 3. Расчет объема под 1$ маржи
    qty = calc_quantity(bx_symbol, current_price)
    if not qty:
        return None, None

    # 4. MARKET BUY LONG
    params = {
        "symbol": bx_symbol, "side": "BUY", "type": "MARKET",
        "quantity": str(qty), "positionSide": "LONG",
    }
    resp = _request("POST", ORDER_PATH, params)

    # Fallback для One-way Mode
    if resp.get("code") != 0 and "positionside" in resp.get("msg", "").lower():
        params["positionSide"] = "BOTH"
        resp = _request("POST", ORDER_PATH, params)

    if resp.get("code") == 0:
        order_id = resp.get("data", {}).get("order", {}).get("orderId")
        log.info(f"[{symbol}] ✅ LONG opened: orderId={order_id}, qty={qty}")
        return str(order_id), qty
    else:
        log.error(f"[{symbol}] ❌ BingX open failed: {resp}")
        return None, None

def close_long_market_order(symbol: str, qty: float):
    """Закрытие позиции (MARKET SELL, reduceOnly)."""
    bx_symbol = _to_bx_symbol(symbol)
    params = {
        "symbol": bx_symbol, "side": "SELL", "type": "MARKET",
        "quantity": str(qty), "reduceOnly": "true", "positionSide": "LONG",
    }
    resp = _request("POST", ORDER_PATH, params)

    if resp.get("code") != 0 and "positionside" in resp.get("msg", "").lower():
        params["positionSide"] = "BOTH"
        resp = _request("POST", ORDER_PATH, params)

    if resp.get("code") == 0:
        log.info(f"[{symbol}] ✅ LONG closed")
    else:
        log.error(f"[{symbol}] ❌ BingX close failed: {resp}")
