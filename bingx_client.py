"""
bingx_client.py — BingX USDT-M Perpetual Swap, демо-счёт (VST).

Все секреты читаются из ENV (GitHub Actions → Settings → Secrets):
  BINGX_API_KEY       API key демо-счёта
  BINGX_SECRET_KEY    secret демо-счёта
  ENABLE_BINGX        "true" / "false"  (по умолчанию false)
  BINGX_MARGIN_USDT   маржа на сделку, по умолчанию 1
  BINGX_LEVERAGE      плечо, по умолчанию 10
  BINGX_MAX_LEVERAGE  потолок авто-поднятия плеча, по умолчанию 50
  BINGX_BASE_URL      по умолчанию https://open-api-vst.bingx.com (демо)
  BINGX_SYMBOL_MAP    опциональный JSON-маппинг тикеров, напр. {"PEPEUSDT":"1000PEPE-USDT"}
"""
import os, json, time, hmac, hashlib, math, logging
from urllib.parse import urlencode
import requests

log = logging.getLogger("bingx")

API_KEY     = os.environ.get("BINGX_API_KEY", "").strip()
SECRET_KEY  = os.environ.get("BINGX_SECRET_KEY", "").strip()
BASE_URL    = os.environ.get("BINGX_BASE_URL", "https://open-api-vst.bingx.com").rstrip("/")
MARGIN_USDT = float(os.environ.get("BINGX_MARGIN_USDT", "1"))
LEVERAGE    = int(os.environ.get("BINGX_LEVERAGE", "10"))
MAX_LEVERAGE = int(os.environ.get("BINGX_MAX_LEVERAGE", "50"))

ORDER_PATH     = "/openApi/swap/v2/trade/order"
POSITION_PATH  = os.environ.get("BINGX_POSITIONS_PATH","/openApi/swap/v2/user/positions")
CONTRACTS_PATH = "/openApi/swap/v2/quote/contracts"
LEVERAGE_PATH  = "/openApi/swap/v2/trade/leverage"
ORDERS_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bingx_orders.jsonl")

try:
    SYMBOL_MAP = json.loads(os.environ.get("BINGX_SYMBOL_MAP", "{}"))
except json.JSONDecodeError:
    SYMBOL_MAP = {}

_CONTRACT_CACHE = {"ts": 0.0, "data": {}}
_CONTRACT_TTL = 3600


def _sign(params: dict) -> str:
    query_string = urlencode(params)  # порядок сохранения: подписываем то, что отправляем
    return hmac.new(SECRET_KEY.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()


def _request(method: str, path: str, params: dict | None = None, signed: bool = True, retries: int = 2):
    params = dict(params or {})
    if signed:
        if not API_KEY or not SECRET_KEY:
            return {"code": -1, "msg": "BINGX_API_KEY / BINGX_SECRET_KEY не заданы в env"}
        params["timestamp"] = str(int(time.time() * 1000))
        params["signature"] = _sign(params)
    headers = {"X-BX-APIKEY": API_KEY} if signed else {}
    url = BASE_URL + path
    last_err = "unknown"
    for attempt in range(retries):
        try:
            resp = requests.request(method, url, headers=headers, params=params, timeout=10)
            return resp.json()
        except Exception as e:
            last_err = str(e)
            time.sleep(1 + attempt)
    return {"code": -1, "msg": f"network error: {last_err}"}


def to_bx_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if s in SYMBOL_MAP:
        return SYMBOL_MAP[s]
    s = s.replace("-", "")
    if s.endswith("USDT"):
        s = s[:-4]
    if not s:
        return symbol
    return f"{s}-USDT"

def contract_exists(symbol: str) -> bool:
    """Проверяет, что контракт для символа существует в /quote/contracts."""
    bx_symbol = to_bx_symbol(symbol)
    return bx_symbol in _contracts()

def _log_event(event: dict):
    event["ts"] = int(time.time())
    try:
        with open(ORDERS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error(f"bingx_orders.jsonl write failed: {e}")


def _contracts() -> dict:
    now = time.time()
    if _CONTRACT_CACHE["data"] and now - _CONTRACT_CACHE["ts"] < _CONTRACT_TTL:
        return _CONTRACT_CACHE["data"]
    resp = _request("GET", CONTRACTS_PATH, signed=False)
    data = {}
    if resp.get("code") == 0:
        for c in resp.get("data", []) or []:
            sym = c.get("symbol")
            if sym:
                data[sym] = c
        _CONTRACT_CACHE.update({"ts": now, "data": data})
    else:
        log.error(f"contracts fetch failed: {resp.get('code')} {resp.get('msg')}")
    return data


def _position_amt(bx_symbol: str) -> float:
    """Открытый объём LONG (защита от дублей при перезапуске Actions)."""
    resp = _request("GET", POSITION_PATH, {"symbol": bx_symbol})
    if resp.get("code") != 0:
        return 0.0
    total = 0.0
    for p in resp.get("data", []) or []:
        if p.get("positionSide") in ("LONG", "BOTH"):
            try:
                amt = float(p.get("positionAmt", 0) or 0)
            except (TypeError, ValueError):
                amt = 0.0
            if amt > 0:
                total += amt
    return total


def list_positions() -> dict:
    """Все открытые LONG-позиции счёта: {bx_symbol: qty}.

    Нужно для сверки журнала с биржей. Раньше монитор вызывал только
    open_long/close_long и никогда не спрашивал, что реально открыто, — поэтому
    неудачное закрытие оставляло позицию на бирже навсегда без учёта.

    Возвращает {"status": "ok", "positions": {...}} либо {"status": "error"}.
    """
    resp = _request("GET", POSITION_PATH)          # без symbol → все позиции
    if resp.get("code") != 0:
        return {"status": "error", "error": f"code={resp.get('code')} msg={resp.get('msg')}"}
    out = {}
    for p in resp.get("data", []) or []:
        if p.get("positionSide") not in ("LONG", "BOTH"):
            continue
        try:
            amt = float(p.get("positionAmt", 0) or 0)
        except (TypeError, ValueError):
            continue
        if amt > 0:
            sym = p.get("symbol")
            if sym:
                out[sym] = out.get(sym, 0.0) + amt
    return {"status": "ok", "positions": out}


def position_amt(symbol: str) -> float:
    """Публичная обёртка над _position_amt: объём LONG по нашему тикеру."""
    return _position_amt(to_bx_symbol(symbol))


def _set_leverage(bx_symbol: str, leverage: int) -> bool:
    resp = _request("POST", LEVERAGE_PATH, {"symbol": bx_symbol, "side": "LONG", "leverage": str(leverage)})
    if resp.get("code") == 0:
        return True
    resp = _request("POST", LEVERAGE_PATH, {"symbol": bx_symbol, "side": "BOTH", "leverage": str(leverage)})
    if resp.get("code") == 0:
        return True
    log.warning(f"[{bx_symbol}] set leverage {leverage}x failed: {resp.get('code')} {resp.get('msg')}")
    return False


def _qty_for(c: dict, price: float, leverage: int):
    mult = float(c.get("multiplier") or 1)
    prec = int(c.get("quantityPrecision") or 0)
    min_qty = float(c.get("minQty") or 0)
    if not price or price <= 0 or mult <= 0:
        return None, prec, min_qty
    raw = (MARGIN_USDT * leverage) / (price * mult)
    if prec > 0:
        step = 10 ** -prec
        qty = round(math.floor(raw / step) * step, prec)
    else:
        qty = float(int(raw))
    return qty, prec, min_qty


def _round_qty(qty: float, precision: int) -> float:
    """Округляет qty вниз до шага точности контракта (floor — нельзя
    отправить больше, чем позволяет precision биржи)."""
    if qty is None or qty <= 0:
        return 0.0
    if precision > 0:
        step = 10 ** -precision
        return round(math.floor(qty / step) * step, precision)
    return float(int(qty))


def contract_limits(symbol: str) -> dict:
    """Лимиты контракта для округления/проверки перед reduce-only ордером.

    Раньше monitor.py использовал захардкоженные PARTIAL_MIN_QTY/
    PARTIAL_MIN_NOTIONAL глобально для всех символов — для дорогих монет
    это слишком грубо, для дешёвых — недостаточно строго."""
    bx_symbol = to_bx_symbol(symbol)
    c = _contracts().get(bx_symbol) or {}
    return {
        "quantity_precision": int(c.get("quantityPrecision") or 0),
        "min_qty": float(c.get("minQty") or 0),
        "min_notional": float(c.get("tradeMinUSDT") or c.get("minNotional") or 0),
        "multiplier": float(c.get("multiplier") or 1),
        "found": bool(c),
    }


def open_long(symbol: str, price: float) -> dict:
    """Открывает LONG на BINGX_MARGIN_USDT маржи. Возвращает статус-словарь."""
    bx_symbol = to_bx_symbol(symbol)

    amt = _position_amt(bx_symbol)
    if amt > 0:
        log.info(f"[{symbol}] позиция уже открыта (amt={amt}) — повторное открытие пропущено")
        return {"status": "already_open", "order_id": None, "qty": amt, "symbol": bx_symbol}

    c = _contracts().get(bx_symbol)
    if not c:
        return {"status": "error", "error": f"контракт {bx_symbol} не найден в /quote/contracts"}

    leverage = LEVERAGE
    qty, prec, min_qty = _qty_for(c, price, leverage)
    if qty is None:
        return {"status": "error", "error": "некорректная цена"}

    max_lev = int(c.get("maxLeverage") or MAX_LEVERAGE)
    max_lev = min(max_lev, MAX_LEVERAGE)
    if qty < min_qty and leverage < max_lev:
        need_lev = math.ceil((min_qty * (price or 0) * float(c.get("multiplier") or 1)) / MARGIN_USDT)
        leverage = min(max(need_lev, leverage), max_lev)
        qty, prec, min_qty = _qty_for(c, price, leverage)

    if qty is None or qty <= 0 or qty < min_qty:
        return {"status": "error",
                "error": f"маржа {MARGIN_USDT}$ слишком мала: qty={qty} < minQty={min_qty} "
                         f"(подними BINGX_LEVERAGE/BINGX_MARGIN_USDT)"}

    _set_leverage(bx_symbol, leverage)

    params = {"symbol": bx_symbol, "side": "BUY", "positionSide": "LONG",
              "type": "MARKET", "quantity": str(qty)}
    resp = _request("POST", ORDER_PATH, params)
    if resp.get("code") != 0 and "positionside" in str(resp.get("msg", "")).lower():
        params["positionSide"] = "BOTH"  # fallback на One-way mode
        resp = _request("POST", ORDER_PATH, params)

    if resp.get("code") == 0:
        order = (resp.get("data") or {}).get("order") or {}
        oid = str(order.get("orderId", ""))
        _log_event({"event": "open", "symbol": symbol, "bx_symbol": bx_symbol, "order_id": oid,
                    "qty": qty, "price": price, "leverage": leverage, "margin_usdt": MARGIN_USDT})
        return {"status": "opened", "order_id": oid, "qty": qty, "symbol": bx_symbol,
                "leverage": leverage, "margin_usdt": MARGIN_USDT}

    err = f"code={resp.get('code')} msg={resp.get('msg')}"
    _log_event({"event": "open_failed", "symbol": symbol, "bx_symbol": bx_symbol, "error": err})
    return {"status": "error", "error": err}


def close_long(symbol: str, qty: float, client_order_id: str = None) -> dict:
    """Закрывает LONG рыночным ордером."""
    if not qty or float(qty) <= 0:
        return {"status": "error", "error": "qty <= 0"}
    return _close_position(to_bx_symbol(symbol), float(qty), client_order_id)


def _close_position(bx_symbol: str, qty: float, client_order_id: str = None) -> dict:
    # Получаем реальную позицию на бирже
    real_amt = _position_amt(bx_symbol)

    # Защита: не закрываем больше, чем есть
    if qty > real_amt:
        if real_amt <= 0:
            return {"status": "skipped", "error": f"нет LONG позиции для {bx_symbol}"}
        log.warning(f"[{bx_symbol}] qty={qty} > real_amt={real_amt} — ограничиваем до {real_amt}")
        qty = real_amt

    # Округление вниз до шага точности контракта — без этого биржа может
    # отклонить ордер с "некруглым" qty (особенно частичные закрытия,
    # где qty = qty_initial * close_fraction почти всегда даёт лишние знаки).
    c = _contracts().get(bx_symbol) or {}
    prec = int(c.get("quantityPrecision") or 0)
    min_qty = float(c.get("minQty") or 0)
    qty = _round_qty(qty, prec)

    if qty <= 0:
        return {"status": "skipped", "error": f"qty=0 после округления (precision={prec})"}
    if min_qty and qty < min_qty:
        return {"status": "skipped", "error": f"qty={qty} < minQty={min_qty} после округления"}

    # Hedge mode: встречный SELL + positionSide=LONG.
    params = {"symbol": bx_symbol, "side": "SELL", "positionSide": "LONG",
              "type": "MARKET", "quantity": str(qty)}
    if client_order_id:
        params["clientOrderID"] = client_order_id
    resp = _request("POST", ORDER_PATH, params)

    # One-way mode fallback
    msg = str(resp.get("msg", "")).lower()
    if resp.get("code") != 0 and ("positionside" in msg or "position side" in msg):
        params = {"symbol": bx_symbol, "side": "SELL", "positionSide": "BOTH",
                  "type": "MARKET", "quantity": str(qty), "reduceOnly": "true"}
        if client_order_id:
            params["clientOrderID"] = client_order_id
        resp = _request("POST", ORDER_PATH, params)

    if resp.get("code") == 0:
        order = (resp.get("data") or {}).get("order") or {}
        oid = str(order.get("orderId", ""))
        _log_event({"event": "close", "bx_symbol": bx_symbol, "order_id": oid, "qty": qty})
        return {"status": "closed", "order_id": oid, "qty": qty, "symbol": bx_symbol}

    err = f"code={resp.get('code')} msg={resp.get('msg')}"
    _log_event({"event": "close_failed", "bx_symbol": bx_symbol, "qty": qty, "error": err})
    return {"status": "error", "error": err}
