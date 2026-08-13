"""
bingx_client.py — BingX USDT-M Perpetual Swap, демо-счёт (VST).
"""

import os, json, time, hmac, hashlib, math, logging, uuid
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode
import requests

log = logging.getLogger("bingx")

API_KEY = os.environ.get("BINGX_API_KEY", "").strip()
SECRET_KEY = os.environ.get("BINGX_SECRET_KEY", "").strip()
BASE_URL = os.environ.get("BINGX_BASE_URL", "https://open-api-vst.bingx.com").rstrip("/")
MARGIN_USDT = float(os.environ.get("BINGX_MARGIN_USDT", "1"))
LEVERAGE = int(os.environ.get("BINGX_LEVERAGE", "10"))
MAX_LEVERAGE = int(os.environ.get("BINGX_MAX_LEVERAGE", "50"))

ORDER_PATH = "/openApi/swap/v2/trade/order"
POSITION_PATH = os.environ.get("BINGX_POSITIONS_PATH", "/openApi/swap/v2/user/positions")
CONTRACTS_PATH = "/openApi/swap/v2/quote/contracts"
LEVERAGE_PATH = "/openApi/swap/v2/trade/leverage"

ORDERS_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bingx_orders.jsonl")

TP_CLIENT_ORDER_PREFIX = "CM_TP_"
SL_CLIENT_ORDER_PREFIX = "CM_SL_"
OPEN_CLIENT_ORDER_PREFIX = "CM_OPEN_"
VALID_TP_LEGS = {"tp1", "tp2", "tp3"}

try:
    SYMBOL_MAP = json.loads(os.environ.get("BINGX_SYMBOL_MAP", "{}"))
except json.JSONDecodeError:
    SYMBOL_MAP = {}

_CONTRACT_CACHE = {
    "ts": 0.0,
    "data": {},
    "by_display_name": {},
}
_CONTRACT_TTL = 3600

def _tp_belongs_to_trade(parsed: dict | None, trade_id: str = None) -> bool:
    """Проверка что TP ордер принадлежит конкретной сделке.
    TP-parsed может содержать либо явный trade_id (старый формат
    clientOrderId), либо только trade_hash (текущий детерминированный
    формат без timestamp).
    """
    if parsed is None:
        return False
    if not trade_id:
        return True
    tid = str(trade_id)
    if parsed.get("trade_id"):
        return parsed["trade_id"] in (tid, tid.replace("_", ""))
    if parsed.get("trade_hash"):
        return parsed["trade_hash"] == _trade_id_hash(tid)
    return False

# ============================================================
# LOW-LEVEL API
# ============================================================
def _sign(params: dict) -> str:
    query_string = urlencode(params)
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


def _log_event(event: dict):
    event["ts"] = int(time.time())
    try:
        with open(ORDERS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error(f"bingx_orders.jsonl write failed: {e}")


def _contracts() -> dict:
    now = time.time()

    if (
        _CONTRACT_CACHE["data"]
        and now - _CONTRACT_CACHE["ts"] < _CONTRACT_TTL
    ):
        return _CONTRACT_CACHE["data"]

    resp = _request("GET", CONTRACTS_PATH, signed=False)

    if resp.get("code") == 0:
        data = {}
        by_display_name = {}

        for c in resp.get("data", []) or []:
            sym = str(c.get("symbol", "")).strip().upper()
            display_name = str(c.get("displayName", "")).strip().upper()

            if sym:
                data[sym] = c

            if display_name:
                by_display_name[display_name] = c

        _CONTRACT_CACHE.update({
            "ts": now,
            "data": data,
            "by_display_name": by_display_name,
        })

        return data

    log.error(
        f"contracts fetch failed: {resp.get('code')} {resp.get('msg')}"
    )

    return _CONTRACT_CACHE["data"]

def get_contract(symbol: str) -> dict | None:
    """
    Найти контракт BingX по тикеру из Coinalyze.

    Сначала ищем обычный API symbol:
        CRV -> CRV-USDT

    Затем ищем по displayName:
        ORCL -> ORCL-USDT -> NCSKORCL2USD-USDT
    """
    s = (symbol or "").strip().upper()

    if not s:
        return None

    # Учитываем существующий ручной SYMBOL_MAP.
    if s in SYMBOL_MAP:
        mapped = str(SYMBOL_MAP[s]).strip().upper()
        if mapped:
            contract = _contracts().get(mapped)
            if contract:
                return contract

    s = s.replace("-", "")

    if s.endswith("USDT"):
        s = s[:-4]

    target = f"{s}-USDT"

    contracts = _contracts()

    # 1. Обычные symbols, например CRV-USDT.
    contract = contracts.get(target)
    if contract:
        return contract

    # 2. TradFi: displayName=ORCL-USDT,
    #    symbol=NCSKORCL2USD-USDT.
    return _CONTRACT_CACHE["by_display_name"].get(target)

def classify_bingx_contract(contract: dict | None) -> str:
    if not contract:
        return "unknown"

    bx_symbol = str(contract.get("symbol", "")).strip().upper()

    if bx_symbol.startswith(("NCSK", "NCSI")):
        return "equity"

    if bx_symbol.startswith("NCCO"):
        return "commodity"

    if bx_symbol.startswith("NCFX"):
        return "forex"

    return "crypto"

def _normalize_orders_list(resp: dict) -> list:
    """Нормализация поля data из ответа BingX API.
    BingX может вернуть список словарей, словарь {"orders": [...]},
    строку / None. Возвращает только список словарей.
    """
    raw = resp.get("data", []) or []
    if isinstance(raw, dict):
        raw = raw.get("orders", []) or []
    if not isinstance(raw, list):
        return []
    return [o for o in raw if isinstance(o, dict)]


# ============================================================
# SYMBOL / CONTRACT UTILS
# ============================================================
def to_bx_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()

    if not s:
        return symbol

    # Явный пользовательский mapping имеет высший приоритет.
    if s in SYMBOL_MAP:
        return str(SYMBOL_MAP[s]).strip().upper()

    # Пытаемся найти реальный BingX execution symbol.
    contract = get_contract(s)
    if contract:
        return str(contract.get("symbol", "")).strip().upper()

    # Fallback: старое поведение, если контракт не найден.
    s = s.replace("-", "")
    if s.endswith("USDT"):
        s = s[:-4]

    if not s:
        return symbol

    return f"{s}-USDT"


def contract_exists(symbol: str) -> bool:
    contract = get_contract(symbol)

    if not contract:
        return False

    return (
        contract.get("status") == 1
        and str(contract.get("apiStateOpen", "")).lower() == "true"
    )


def contract_limits(symbol: str) -> dict:
    bx_symbol = to_bx_symbol(symbol)
    c = _contracts().get(bx_symbol) or {}
    return {
        "quantity_precision": int(c.get("quantityPrecision") or 0),
        "min_qty": float(c.get("minQty") or 0),
        "min_notional": float(c.get("tradeMinUSDT") or c.get("minNotional") or 0),
        "multiplier": float(c.get("multiplier") or 1),
        "found": bool(c),
    }


def _qty_for(c: dict, price: float, leverage: int):
    mult = float(c.get("multiplier") or c.get("size") or 1)
    prec = int(c.get("quantityPrecision") or 0)
    min_qty = float(c.get("tradeMinQuantity") or c.get("minQty") or 0)
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
    if qty is None or qty <= 0:
        return 0.0
    try:
        q = Decimal(str(qty))
        if precision <= 0:
            return float(q.quantize(Decimal("1"), rounding=ROUND_DOWN))
        step = Decimal("1").scaleb(-precision)
        return float(q.quantize(step, rounding=ROUND_DOWN))
    except (TypeError, ValueError, ArithmeticError):
        return 0.0


def _is_reduce_only(value) -> bool:
    """Flexible проверка reduceOnly (может быть bool, str, int).
    Используется только для MARKET close, НЕ для TP/SL в Hedge Mode."""
    return value in (True, "true", "TRUE", 1, "1")


# ============================================================
# TRADE ID HASH / CLIENT ORDER ID
# ============================================================
def _trade_id_hash(trade_id: str) -> str:
    """Короткий детерминированный hash trade_id (8 hex) для clientOrderId."""
    return hashlib.sha256(str(trade_id).encode("utf-8")).hexdigest()[:8]


def _is_hex8(s: str) -> bool:
    return isinstance(s, str) and len(s) == 8 and all(ch in "0123456789abcdef" for ch in s.lower())


def build_tp_client_order_id(leg: str, trade_id: str = None) -> str:
    """Собрать clientOrderId ≤ 40 символов.
    v3.1: ID полностью детерминирован (без timestamp) — CM_TP_<hash8>_<leg>.
    Это даёт естественную идемпотентность: повторный вызов с тем же
    trade_id+leg генерирует ТОТ ЖЕ ID, и биржа должна отклонить дубликат
    сама, вместо того чтобы принять его как новый ордер.
    """
    key = _trade_id_hash(trade_id) if trade_id else uuid.uuid4().hex[:8]
    return f"{TP_CLIENT_ORDER_PREFIX}{key}_{leg}"


def parse_tp_client_order_id(client_id: str) -> dict | None:
    """Парсинг clientOrderId для TP ордеров.
    Форматы:
    - v3.1 deterministic: CM_TP_<hash8>_<leg>              (4 части)
    - v2.6 hash+ts:       CM_TP_<hash8>_<leg>_<ts>          (5 частей)
    - v2.4 full:          CM_TP_<tradeid>_<symbol>_<leg>_<ts> (6+ частей, leg idx 4)
    - legacy:             CM_TP_<symbol>_<leg>_<ts>[_<uuid>]  (leg idx 3)
    Поддержка старых форматов оставлена для распознавания ордеров,
    созданных до этого фикса (переходный период).
    """
    if not client_id:
        return None
    upper_id = client_id.upper()
    if not upper_id.startswith(TP_CLIENT_ORDER_PREFIX):
        return None
    parts = upper_id.split("_")
    valid_legs_upper = {leg.upper() for leg in VALID_TP_LEGS}
    if len(parts) < 4 or parts[0] != "CM" or parts[1] != "TP":
        return None
    if len(parts) == 4 and _is_hex8(parts[2]) and parts[3] in valid_legs_upper:
        return {"trade_id": None, "trade_hash": parts[2].lower(), "leg": parts[3].lower()}
    if len(parts) == 5 and _is_hex8(parts[2]) and parts[3] in valid_legs_upper:
        return {"trade_id": None, "trade_hash": parts[2].lower(), "leg": parts[3].lower()}
    if len(parts) >= 6 and parts[4] in valid_legs_upper:
        return {"trade_id": parts[2], "trade_hash": None, "leg": parts[4].lower()}
    if parts[3] in valid_legs_upper:
        return {"trade_id": None, "trade_hash": None, "leg": parts[3].lower()}
    return None


def build_sl_client_order_id(trade_id: str = None) -> str:
    """v3.1: ID полностью детерминирован (без timestamp) — CM_SL_<hash8>."""
    key = _trade_id_hash(trade_id) if trade_id else uuid.uuid4().hex[:8]
    return f"{SL_CLIENT_ORDER_PREFIX}{key}"


def build_open_client_order_id(symbol: str, trade_id: str = None) -> str:
    """Детерминированный clientOrderId для OPEN-ордера (MARKET BUY).
    Формат: CM_OPEN_<hash8> — ≤ 40 символов.
    Обеспечивает идемпотентность: повторный вызов _request() с тем же
    trade_id отправит ТОТ ЖЕ clientOrderId, и биржа отклонит дубликат.
    """
    raw = f"{symbol}:{trade_id}" if trade_id else symbol
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{OPEN_CLIENT_ORDER_PREFIX}{key}"


def parse_sl_client_order_id(client_id: str) -> dict | None:
    """Парсинг clientOrderId для SL ордеров.
    v3.1 deterministic: CM_SL_<hash8>        (3 части)
    v2.9 hash+ts:        CM_SL_<hash8>_<ts>   (4 части, обратная совместимость)
    """
    if not client_id:
        return None
    upper_id = client_id.upper()
    if not upper_id.startswith(SL_CLIENT_ORDER_PREFIX):
        return None
    parts = upper_id.split("_")
    if len(parts) == 3 and parts[0] == "CM" and parts[1] == "SL":
        return {"trade_hash": parts[2].lower()}
    if len(parts) == 4 and parts[0] == "CM" and parts[1] == "SL":
        return {"trade_hash": parts[2].lower()}
    return None


def _sl_belongs_to_trade(parsed: dict | None, trade_id: str = None) -> bool:
    if parsed is None:
        return False
    if not trade_id:
        return True
    return parsed.get("trade_hash") == _trade_id_hash(trade_id)


# ============================================================
# POSITION QUERIES
# ============================================================
def _position_amt(bx_symbol: str) -> float:
    resp = _request("GET", POSITION_PATH, {"symbol": bx_symbol})
    if resp.get("code") != 0:
        return 0.0
    total = 0.0
    for p in _normalize_orders_list(resp):
        if p.get("positionSide") in ("LONG", "BOTH"):
            try:
                amt = float(p.get("positionAmt", 0) or 0)
            except (TypeError, ValueError):
                amt = 0.0
            if amt > 0:
                total += amt
    return total


def list_positions() -> dict:
    """Все открытые LONG-позиции счёта: {bx_symbol: qty}."""
    resp = _request("GET", POSITION_PATH)
    if resp.get("code") != 0:
        return {"status": "error", "error": f"code={resp.get('code')} msg={resp.get('msg')}"}
    out = {}
    for p in _normalize_orders_list(resp):
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
    """Объём LONG по нашему тикеру."""
    return _position_amt(to_bx_symbol(symbol))


def get_position(symbol: str) -> dict:
    """Получить позицию с фактической ценой входа (avgPrice)."""
    bx_symbol = to_bx_symbol(symbol)
    resp = _request("GET", POSITION_PATH, {"symbol": bx_symbol})
    if resp.get("code") != 0:
        log.error(f"get_position failed: code={resp.get('code')} msg={resp.get('msg')}")
        return {"status": "error", "error": resp.get("msg", "unknown")}
    for p in _normalize_orders_list(resp):
        if p.get("positionSide") in ("LONG", "BOTH"):
            try:
                amt = float(p.get("positionAmt", 0) or 0)
                avg_price = float(p.get("avgPrice", 0) or p.get("entryPrice", 0) or 0)
            except (TypeError, ValueError):
                continue
            if amt > 0 and avg_price > 0:
                return {
                    "status": "found",
                    "symbol": p.get("symbol", bx_symbol),
                    "avgPrice": avg_price,
                    "positionAmt": amt,
                    "entryPrice": float(p.get("entryPrice", 0) or avg_price)
                }
    return {"status": "not_found", "symbol": bx_symbol}


def wait_for_position_fill(symbol: str, timeout_sec: int = 30, poll_interval: float = 1.0) -> dict:
    """После MARKET order ждать появления позиции с ненулевым avgPrice."""
    bx_symbol = to_bx_symbol(symbol)
    start = time.time()
    while time.time() - start < timeout_sec:
        pos = get_position(symbol)
        if pos.get("status") == "found":
            log.info(f"[{symbol}] позиция появилась: avgPrice={pos.get('avgPrice')} qty={pos.get('positionAmt')}")
            return pos
        time.sleep(poll_interval)
    log.warning(f"[{symbol}] позиция не появилась за {timeout_sec}с")
    return {"status": "timeout", "symbol": bx_symbol}


# ============================================================
# TP ORDER QUERIES
# ============================================================
def get_open_tp_orders(symbol: str) -> dict:
    """Получить все открытые TP ордера для символа (только наши).
    v2.8: фильтр по полю clientOrderId (биржа возвращает именно так,
    а не clientOrderID). Главный маркер "наш" — clientOrderId начинается
    с CM_TP_.
    """
    bx_symbol = to_bx_symbol(symbol)
    resp = _request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": bx_symbol})
    if resp.get("code") != 0:
        return {"status": "error", "error": resp.get("msg", "unknown"), "orders": []}
    tp_orders = []
    for o in _normalize_orders_list(resp):
        is_our_tp = (
            o.get("type") in ("TAKE_PROFIT", "TAKE_PROFIT_MARKET")
            and o.get("positionSide") == "LONG"
            and str(o.get("clientOrderId", "")).upper().startswith(TP_CLIENT_ORDER_PREFIX)
        )
        if is_our_tp:
            tp_orders.append(o)
    return {"status": "ok", "orders": tp_orders, "count": len(tp_orders)}


def get_filled_tp_orders(symbol: str, opened_ts: int = None, trade_id: str = None) -> dict:
    """Получить исполненные TP ордера из order history.
    trade_id — основной фильтр (через hash), opened_ts — fallback защита.
    v2.8: читаем clientOrderId (маленькая d) — реальное имя поля в ответе BingX.
    """
    bx_symbol = to_bx_symbol(symbol)
    resp = _request("GET", "/openApi/swap/v2/trade/allOrders", {"symbol": bx_symbol})
    if resp.get("code") != 0:
        return {"status": "error", "error": resp.get("msg", "unknown"), "orders": []}
    filled_tp = []
    for o in _normalize_orders_list(resp):
        client_id = str(o.get("clientOrderId", ""))
        status = o.get("status")
        order_type = o.get("type")
        order_time = o.get("time", 0)
        if not (
            status in ("FILLED", "PARTIALLY_FILLED", "PARTIALLYFILLED")
            and order_type in ("TAKE_PROFIT", "TAKE_PROFIT_MARKET")
        ):
            continue
        parsed = parse_tp_client_order_id(client_id)
        if not parsed:
            continue
        if trade_id and not _tp_belongs_to_trade(parsed, trade_id):
            continue
        if opened_ts and order_time < opened_ts * 1000:
            continue
        filled_tp.append({
            "leg": parsed.get("leg"),
            "trade_id": parsed.get("trade_id"),
            "trade_hash": parsed.get("trade_hash"),
            "order_id": str(o.get("orderId", "")),
            "client_order_id": client_id,
            "status": status,
            "executed_qty": float(o.get("executedQty", 0) or 0),
            "avg_price": float(o.get("avgPrice", 0) or 0),
            "time": order_time
        })
    return {"status": "ok", "orders": filled_tp, "count": len(filled_tp)}


def get_existing_tp_legs(symbol: str, tp_levels: list, trade_id: str = None) -> dict:
    """Проверить какие TP legs уже существуют (для данной сделки)."""
    result = get_open_tp_orders(symbol)
    if result.get("status") == "error":
        return {
            "legs": {tp.get("leg"): False for tp in tp_levels},
            "missing": [tp.get("leg") for tp in tp_levels],
            "all_present": False,
            "existing_qty": 0,
            "orders": []
        }
    existing_legs = {}
    existing_qty_total = 0.0
    for order in result.get("orders", []):
        parsed = parse_tp_client_order_id(str(order.get("clientOrderId", "")))
        if not parsed:
            continue
        if trade_id and not _tp_belongs_to_trade(parsed, trade_id):
            continue
        leg = parsed.get("leg")
        if leg:
            existing_legs[leg] = True
            qty = float(order.get("origQty", 0) or order.get("quantity", 0) or 0)
            existing_qty_total += qty
    legs_status = {}
    missing = []
    for tp in tp_levels:
        leg = tp.get("leg")
        present = existing_legs.get(leg, False)
        legs_status[leg] = present
        if not present:
            missing.append(leg)
    return {
        "legs": legs_status,
        "missing": missing,
        "all_present": len(missing) == 0,
        "existing_qty": existing_qty_total,
        "orders": result.get("orders", [])
    }


# ============================================================
# TP ORDER CREATION / CANCELLATION
# ============================================================
def place_take_profit_orders(symbol: str, avg_price: float, position_qty: float, tp_levels: list = None, trade_id: str = None) -> dict:
    """Создать BingX TP ордера через Trigger Order API.
    v2.7: БЕЗ reduceOnly (BingX Hedge Mode запрещает это поле).
    Привязка к LONG через positionSide=LONG.
    clientOrderId ≤ 40 символов (hash trade_id).
    Atomic rollback при partial failure.
    """
    if tp_levels is None:
        tp_levels = [
            {"leg": "tp1", "pnl_pct": 3.0, "close_fraction": 0.25},
            {"leg": "tp2", "pnl_pct": 6.0, "close_fraction": 0.25},
            {"leg": "tp3", "pnl_pct": 9.0, "close_fraction": 0.25}
        ]
    bx_symbol = to_bx_symbol(symbol)
    existing_check = get_existing_tp_legs(symbol, tp_levels, trade_id=trade_id)
    if existing_check.get("all_present"):
        log.info(f"[{symbol}] все TP legs уже существуют, пропускаем создание")
        return {
            "status": "already_exists",
            "legs": existing_check.get("legs"),
            "orders": [],
            "existing_qty": existing_check.get("existing_qty")
        }
    missing_legs = existing_check.get("missing", [])
    existing_qty = existing_check.get("existing_qty", 0)
    c = _contracts().get(bx_symbol)
    if not c:
        return {"status": "error", "error": f"контракт {bx_symbol} не найден"}
    prec = int(c.get("quantityPrecision") or 0)
    min_qty = float(c.get("tradeMinQuantity") or c.get("minQty") or 0)
    available_qty = position_qty - existing_qty
    if available_qty <= 0:
        log.warning(f"[{symbol}] вся qty уже зарезервирована существующими TP")
        return {"status": "already_exists", "legs": existing_check.get("legs"), "orders": []}
    orders_created = []
    orders_to_rollback = []
    remaining_qty = available_qty
    for i, tp in enumerate(tp_levels):
        leg = tp.get("leg", f"tp{i + 1}")
        if leg not in missing_legs:
            continue
        pnl_pct = tp.get("pnl_pct", 0)
        tp_price = avg_price * (1 + pnl_pct / 100)
        close_fraction = float(tp.get("close_fraction", 0.0) or 0.0)
        tp_qty = position_qty * close_fraction
        tp_qty = _round_qty(tp_qty, prec)
        if tp_qty < min_qty:
            log.warning(f"[{symbol}] {leg}: qty={tp_qty} < minQty={min_qty}, пропускаем")
            continue
        if tp_qty <= 0:
            log.warning(f"[{symbol}] {leg}: qty<=0 после округления, пропускаем")
            continue
        remaining_qty = remaining_qty - tp_qty
        client_order_id = build_tp_client_order_id(leg, trade_id)
        params = {
            "symbol": bx_symbol,
            "side": "SELL",
            "positionSide": "LONG",
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": str(round(tp_price, 8)),
            "quantity": str(tp_qty),
            "clientOrderId": client_order_id
            # "reduceOnly" намеренно НЕ передаём для Hedge Mode
        }
        resp = _request("POST", ORDER_PATH, params)
        if resp.get("code") == 0:
            order = (resp.get("data") or {}).get("order") or {}
            oid = str(order.get("orderId", ""))
            orders_created.append({
                "leg": leg,
                "order_id": oid,
                "client_order_id": client_order_id,
                "price": tp_price,
                "pnl_pct": pnl_pct,
                "qty": tp_qty,
                "trade_id": trade_id
            })
            orders_to_rollback.append(oid)
            _log_event({
                "event": "tp_created",
                "symbol": symbol,
                "bx_symbol": bx_symbol,
                "leg": leg,
                "order_id": oid,
                "client_order_id": client_order_id,
                "trade_id": trade_id,
                "trade_hash": _trade_id_hash(trade_id) if trade_id else None,
                "avg_price": avg_price,
                "tp_price": tp_price,
                "pnl_pct": pnl_pct,
                "qty": tp_qty,
                "position_qty": position_qty,
                "available_qty": available_qty,
                "remaining_after": remaining_qty
            })
            log.info(f"[{symbol}] {leg} создан: orderId={oid} price={tp_price:.6f} qty={tp_qty}")
        else:
            err = f"code={resp.get('code')} msg={resp.get('msg')}"
            log.error(f"[{symbol}] {leg} creation failed: {err}, rolling back {len(orders_to_rollback)} orders")
            for rollback_oid in orders_to_rollback:
                rollback_resp = _request("DELETE", "/openApi/swap/v2/trade/order",
                                         {"symbol": bx_symbol, "orderId": rollback_oid})
                if rollback_resp.get("code") == 0:
                    log.info(f"[{symbol}] rollback: отменён ордер {rollback_oid}")
                else:
                    log.error(f"[{symbol}] rollback failed для {rollback_oid}: {rollback_resp.get('msg')}")
            _log_event({
                "event": "tp_creation_failed",
                "symbol": symbol,
                "bx_symbol": bx_symbol,
                "leg": leg,
                "client_order_id": client_order_id,
                "trade_id": trade_id,
                "error": err,
                "tp_price": tp_price,
                "qty": tp_qty,
                "rolled_back_count": len(orders_to_rollback)
            })
            return {
                "status": "error",
                "error": err,
                "failed_leg": leg,
                "rolled_back": len(orders_to_rollback)
            }
    if not orders_created:
        return {"status": "error", "error": "ни один TP не создан"}
    return {
        "status": "created",
        "orders": orders_created,
        "avg_price": avg_price,
        "position_qty": position_qty,
        "available_qty": available_qty,
        "remaining_qty": remaining_qty,
        "missing_legs_created": missing_legs,
        "trade_id": trade_id
    }


def cancel_take_profit_orders(symbol: str) -> dict:
    """Безопасно отменяет все наши открытые TP.
    Безопасные результаты:
      - no_orders
      - cancelled
    Небезопасные:
      - partial_or_failed
      - error
    После DELETE обязательно выполняется повторная проверка биржи.
    """
    bx_symbol = to_bx_symbol(symbol)
    result = get_open_tp_orders(symbol)
    if result.get("status") == "error":
        return {
            "status": "error",
            "error": result.get("error", "TP query failed"),
            "cancelled_count": 0,
            "total_found": None,
            "remaining_count": None,
        }
    tp_orders = result.get("orders", []) or []
    if not tp_orders:
        return {
            "status": "no_orders",
            "cancelled_count": 0,
            "total_found": 0,
            "remaining_count": 0,
        }
    total_found = len(tp_orders)
    cancelled = 0
    for order in tp_orders:
        oid = order.get("orderId")
        client_oid = str(order.get("clientOrderId", ""))
        if not oid:
            continue
        parsed = parse_tp_client_order_id(client_oid)
        if not parsed:
            log.warning(
                f"[{symbol}] TP order {oid} "
                f"не имеет нашего clientOrderId — НЕ отменяем"
            )
            continue
        resp = _request(
            "DELETE",
            "/openApi/swap/v2/trade/order",
            {
                "symbol": bx_symbol,
                "orderId": oid,
            },
        )
        if resp.get("code") == 0:
            cancelled += 1
            _log_event({
                "event": "tp_cancelled",
                "symbol": symbol,
                "bx_symbol": bx_symbol,
                "order_id": str(oid),
                "client_order_id": client_oid,
                "leg": parsed.get("leg", "unknown"),
                "trade_id": parsed.get("trade_id"),
                "trade_hash": parsed.get("trade_hash"),
                "type": order.get("type"),
            })
            log.info(
                f"[{symbol}] TP {parsed.get('leg')} "
                f"отменён orderId={oid}"
            )
        else:
            log.warning(
                f"[{symbol}] отмена TP {oid} failed: "
                f"{resp.get('msg')}"
            )
    verify = get_open_tp_orders(symbol)
    if verify.get("status") == "error":
        return {
            "status": "error",
            "error": (
                "TP cancellation verification failed: "
                f"{verify.get('error', 'unknown')}"
            ),
            "cancelled_count": cancelled,
            "total_found": total_found,
            "remaining_count": None,
        }
    remaining_orders = verify.get("orders", []) or []
    remaining_count = len(remaining_orders)
    if remaining_count == 0:
        return {
            "status": "cancelled",
            "cancelled_count": cancelled,
            "total_found": total_found,
            "remaining_count": 0,
        }
    return {
        "status": "partial_or_failed",
        "cancelled_count": cancelled,
        "total_found": total_found,
        "remaining_count": remaining_count,
        "remaining_orders": [
            {
                "order_id": str(o.get("orderId", "")),
                "client_order_id": str(o.get("clientOrderId", "")),
            }
            for o in remaining_orders
        ],
        "error": (
            f"После отмены остаются TP: "
            f"{remaining_count}/{total_found}"
        ),
    }


# ============================================================
# SL ORDER QUERIES / CREATION / CANCELLATION
# ============================================================
def get_open_sl_orders(symbol: str) -> dict:
    """Получить все открытые STOP_LOSS ордера для символа (только наши)."""
    bx_symbol = to_bx_symbol(symbol)
    resp = _request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": bx_symbol})
    if resp.get("code") != 0:
        return {"status": "error", "error": resp.get("msg", "unknown"), "orders": []}
    sl_orders = []
    for o in _normalize_orders_list(resp):
        is_our_sl = (
            o.get("type") in ("STOP", "STOP_MARKET")
            and o.get("positionSide") == "LONG"
            and str(o.get("clientOrderId", "")).upper().startswith(SL_CLIENT_ORDER_PREFIX)
        )
        if is_our_sl:
            sl_orders.append(o)
    return {"status": "ok", "orders": sl_orders, "count": len(sl_orders)}


def get_filled_sl_orders(symbol: str, opened_ts: int = None, trade_id: str = None) -> dict:
    """Получить исполненные STOP_LOSS ордера из order history."""
    bx_symbol = to_bx_symbol(symbol)
    resp = _request("GET", "/openApi/swap/v2/trade/allOrders", {"symbol": bx_symbol})
    if resp.get("code") != 0:
        return {"status": "error", "error": resp.get("msg", "unknown"), "orders": []}
    filled_sl = []
    for o in _normalize_orders_list(resp):
        client_id = str(o.get("clientOrderId", ""))
        status = o.get("status")
        order_type = o.get("type")
        order_time = o.get("time", 0)
        if not (
            status in ("FILLED", "PARTIALLY_FILLED", "PARTIALLYFILLED")
            and order_type in ("STOP", "STOP_MARKET")
        ):
            continue
        parsed = parse_sl_client_order_id(client_id)
        if not parsed:
            continue
        if trade_id and not _sl_belongs_to_trade(parsed, trade_id):
            continue
        if opened_ts and order_time < opened_ts * 1000:
            continue
        filled_sl.append({
            "trade_hash": parsed.get("trade_hash"),
            "order_id": str(o.get("orderId", "")),
            "client_order_id": client_id,
            "status": status,
            "executed_qty": float(o.get("executedQty", 0) or 0),
            "avg_price": float(o.get("avgPrice", 0) or 0),
            "time": order_time
        })
    return {"status": "ok", "orders": filled_sl, "count": len(filled_sl)}


def place_stop_loss_order(symbol: str, avg_price: float, qty: float, stop_loss_pct: float, trade_id: str = None) -> dict:
    """Создать биржевой STOP_LOSS (STOP_MARKET) для LONG-позиции.
    Гарантированное исполнение на бирже независимо от того, работает ли
    программная проверка внутри monitor.py на момент прогона.
    """
    bx_symbol = to_bx_symbol(symbol)
    if not avg_price or avg_price <= 0:
        return {"status": "error", "error": "некорректная avg_price"}
    c = _contracts().get(bx_symbol)
    if not c:
        return {"status": "error", "error": f"контракт {bx_symbol} не найден"}
    prec = int(c.get("quantityPrecision") or 0)
    min_qty = float(c.get("tradeMinQuantity") or c.get("minQty") or 0)
    qty_r = _round_qty(qty, prec)
    if qty_r <= 0 or qty_r < min_qty:
        return {"status": "error", "error": f"qty={qty_r} < minQty={min_qty}"}
    stop_price = avg_price * (1 - stop_loss_pct / 100)
    client_order_id = build_sl_client_order_id(trade_id)
    params = {
        "symbol": bx_symbol,
        "side": "SELL",
        "positionSide": "LONG",
        "type": "STOP_MARKET",
        "stopPrice": str(round(stop_price, 8)),
        "quantity": str(qty_r),
        "clientOrderId": client_order_id,
        # "reduceOnly" намеренно НЕ передаём для Hedge Mode (как и для TP)
    }
    resp = _request("POST", ORDER_PATH, params)
    if resp.get("code") == 0:
        order = (resp.get("data") or {}).get("order") or {}
        oid = str(order.get("orderId", ""))
        _log_event({
            "event": "sl_created",
            "symbol": symbol,
            "bx_symbol": bx_symbol,
            "order_id": oid,
            "client_order_id": client_order_id,
            "trade_id": trade_id,
            "trade_hash": _trade_id_hash(trade_id) if trade_id else None,
            "avg_price": avg_price,
            "stop_price": stop_price,
            "qty": qty_r,
        })
        log.info(f"[{symbol}] SL создан: orderId={oid} stopPrice={stop_price:.6f} qty={qty_r}")
        return {
            "status": "created",
            "order_id": oid,
            "client_order_id": client_order_id,
            "stop_price": stop_price,
            "qty": qty_r,
        }
    err = f"code={resp.get('code')} msg={resp.get('msg')}"
    _log_event({
        "event": "sl_creation_failed",
        "symbol": symbol,
        "bx_symbol": bx_symbol,
        "client_order_id": client_order_id,
        "trade_id": trade_id,
        "error": err,
        "stop_price": stop_price,
        "qty": qty_r,
    })
    log.error(f"[{symbol}] SL creation failed: {err}")
    return {"status": "error", "error": err}


def cancel_stop_loss_orders(symbol: str) -> dict:
    """Безопасно отменяет все наши открытые SL.
    Безопасные результаты: no_orders, cancelled.
    Небезопасные: partial_or_failed, error.
    После DELETE обязательно выполняется повторная проверка биржи.
    """
    bx_symbol = to_bx_symbol(symbol)
    result = get_open_sl_orders(symbol)
    if result.get("status") == "error":
        return {
            "status": "error",
            "error": result.get("error", "SL query failed"),
            "cancelled_count": 0,
            "total_found": None,
            "remaining_count": None,
        }
    sl_orders = result.get("orders", []) or []
    if not sl_orders:
        return {
            "status": "no_orders",
            "cancelled_count": 0,
            "total_found": 0,
            "remaining_count": 0,
        }
    total_found = len(sl_orders)
    cancelled = 0
    for order in sl_orders:
        oid = order.get("orderId")
        client_oid = str(order.get("clientOrderId", ""))
        if not oid:
            continue
        parsed = parse_sl_client_order_id(client_oid)
        if not parsed:
            log.warning(f"[{symbol}] SL order {oid} не имеет нашего clientOrderId — НЕ отменяем")
            continue
        resp = _request("DELETE", "/openApi/swap/v2/trade/order", {"symbol": bx_symbol, "orderId": oid})
        if resp.get("code") == 0:
            cancelled += 1
            _log_event({
                "event": "sl_cancelled",
                "symbol": symbol,
                "bx_symbol": bx_symbol,
                "order_id": str(oid),
                "client_order_id": client_oid,
                "trade_hash": parsed.get("trade_hash"),
                "type": order.get("type"),
            })
            log.info(f"[{symbol}] SL отменён orderId={oid}")
        else:
            log.warning(f"[{symbol}] отмена SL {oid} failed: {resp.get('msg')}")
    verify = get_open_sl_orders(symbol)
    if verify.get("status") == "error":
        return {
            "status": "error",
            "error": f"SL cancellation verification failed: {verify.get('error', 'unknown')}",
            "cancelled_count": cancelled,
            "total_found": total_found,
            "remaining_count": None,
        }
    remaining_orders = verify.get("orders", []) or []
    remaining_count = len(remaining_orders)
    if remaining_count == 0:
        return {
            "status": "cancelled",
            "cancelled_count": cancelled,
            "total_found": total_found,
            "remaining_count": 0,
        }
    return {
        "status": "partial_or_failed",
        "cancelled_count": cancelled,
        "total_found": total_found,
        "remaining_count": remaining_count,
        "remaining_orders": [
            {
                "order_id": str(o.get("orderId", "")),
                "client_order_id": str(o.get("clientOrderId", "")),
            }
            for o in remaining_orders
        ],
        "error": f"После отмены остаются SL: {remaining_count}/{total_found}",
    }


# ============================================================
# FILL DETECTION HELPERS (чистые функции, без side-effects
# на журналы/telegram/watchlist — это забота monitor.py)
# ============================================================
def compute_new_tp_fills(symbol: str, trade_id: str, opened_ts: int, processed_fills: dict) -> dict:
    """Сравнивает исполненные TP-ордера на бирже с уже обработанными
    (processed_fills — словарь order_id -> executed_qty, известный вызывающей
    стороне). Возвращает только НОВЫЕ/увеличившиеся fills. Ничего не пишет,
    не мутирует processed_fills — коммит делает monitor.py после успешной
    записи в журнал (crash-safety).
    """
    result = get_filled_tp_orders(symbol, opened_ts=opened_ts, trade_id=trade_id)
    if result.get("status") != "ok":
        return {"status": "error", "error": result.get("error"), "fills": []}
    orders = sorted(result.get("orders", []), key=lambda x: int(x.get("time", 0) or 0))
    fills = []
    for filled in orders:
        order_id = str(filled.get("order_id", "") or "")
        leg = filled.get("leg")
        if not order_id or not leg:
            continue
        try:
            executed_qty = float(filled.get("executed_qty", 0) or 0)
        except Exception:
            continue
        if executed_qty <= 0:
            continue
        previous_qty = float(processed_fills.get(order_id, 0.0) or 0.0)
        delta_qty = executed_qty - previous_qty
        if delta_qty <= 1e-12:
            continue
        if delta_qty < 0:
            log.warning(
                f"[{symbol}] TP {leg}: executedQty rollback "
                f"(prev={previous_qty:.8f} new={executed_qty:.8f})"
            )
            continue
        avg_price = filled.get("avg_price")
        try:
            avg_price = float(avg_price)
        except Exception:
            avg_price = None
        fills.append({
            "order_id": order_id,
            "leg": leg,
            "client_order_id": filled.get("client_order_id"),
            "status": str(filled.get("status", "")).upper(),
            "executed_qty_delta": delta_qty,
            "executed_qty_total": executed_qty,
            "avg_price": avg_price,
            "fill_time_ms": int(filled.get("time", 0) or 0),
        })
    return {"status": "ok", "fills": fills}


def get_last_filled_sl(symbol: str, opened_ts: int = None, trade_id: str = None) -> dict:
    """Возвращает последний по времени исполненный STOP_LOSS ордер (или None)."""
    result = get_filled_sl_orders(symbol, opened_ts=opened_ts, trade_id=trade_id)
    if result.get("status") != "ok":
        return {"status": result.get("status", "error"), "order": None, "error": result.get("error")}
    orders = result.get("orders", [])
    if not orders:
        return {"status": "ok", "order": None}
    last = sorted(orders, key=lambda o: int(o.get("time", 0) or 0))[-1]
    return {"status": "ok", "order": last}


# ============================================================
# LEVERAGE / OPEN / CLOSE
# ============================================================
def _set_leverage(bx_symbol: str, leverage: int) -> bool:
    resp = _request("POST", LEVERAGE_PATH, {"symbol": bx_symbol, "side": "LONG", "leverage": str(leverage)})
    if resp.get("code") == 0:
        return True
    resp = _request("POST", LEVERAGE_PATH, {"symbol": bx_symbol, "side": "BOTH", "leverage": str(leverage)})
    if resp.get("code") == 0:
        return True
    log.warning(f"[{bx_symbol}] set leverage {leverage}x failed: {resp.get('code')} {resp.get('msg')}")
    return False


def open_long(symbol: str, price: float, trade_id: str = None) -> dict:
    """Открывает LONG.
    Если LONG уже существует на бирже, новый research-entry НЕ усыновляет
    существующую позицию. Ownership существующей позиции должен идти через
    уже существующий local open_trade/reconcile state.
    """
    bx_symbol = to_bx_symbol(symbol)
    amt = _position_amt(bx_symbol)
    if amt > 0:
        log.warning(
            f"[{symbol}] EXISTING LONG detected: "
            f"qty={amt}. New entry will NOT adopt this position."
        )
        _log_event({
            "event": "open_skipped_existing_position",
            "symbol": symbol,
            "bx_symbol": bx_symbol,
            "existing_qty": amt,
            "trade_id": trade_id,
        })
        return {
            "status": "foreign_position",
            "order_id": None,
            "qty": amt,
            "existing_qty": amt,
            "symbol": bx_symbol,
            "trade_id": trade_id,
        }
    c = _contracts().get(bx_symbol)
    if not c:
        return {
            "status": "error",
            "error": f"контракт {bx_symbol} не найден в /quote/contracts",
        }
    leverage = LEVERAGE
    qty, prec, min_qty = _qty_for(c, price, leverage)
    if qty is None:
        return {
            "status": "error",
            "error": "некорректная цена",
        }
    max_lev = int(c.get("maxLongLeverage") or c.get("maxLeverage") or MAX_LEVERAGE)
    max_lev = min(max_lev, MAX_LEVERAGE)
    if qty < min_qty and leverage < max_lev:
        need_lev = math.ceil(
            (min_qty * (price or 0) * float(c.get("multiplier") or c.get("size") or 1))
            / MARGIN_USDT
        )
        leverage = min(max(need_lev, leverage), max_lev)
        qty, prec, min_qty = _qty_for(c, price, leverage)
    if qty is None or qty <= 0 or qty < min_qty:
        return {
            "status": "error",
            "error": (
                f"маржа {MARGIN_USDT}$ слишком мала: "
                f"qty={qty} < minQty={min_qty} "
                f"(подними BINGX_LEVERAGE/BINGX_MARGIN_USDT)"
            ),
        }
    _set_leverage(bx_symbol, leverage)
    client_order_id = build_open_client_order_id(bx_symbol, trade_id)
    params = {
        "symbol": bx_symbol,
        "side": "BUY",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": str(qty),
        "clientOrderId": client_order_id,
    }
    resp = _request("POST", ORDER_PATH, params)
    if resp.get("code") != 0:
        err_msg = str(resp.get("msg", "")).lower()
        if "positionside" in err_msg or "position side" in err_msg:
            log.error(
                f"[{symbol}] BingX не принимает positionSide=LONG: "
                f"{resp.get('msg')}"
            )
            _log_event({
                "event": "open_failed",
                "symbol": symbol,
                "bx_symbol": bx_symbol,
                "error": f"Hedge Mode not supported: {resp.get('msg')}",
                "trade_id": trade_id,
            })
            return {
                "status": "error",
                "error": (
                    f"Hedge Mode (positionSide=LONG) "
                    f"не поддерживается: {resp.get('msg')}"
                ),
            }
        err = f"code={resp.get('code')} msg={resp.get('msg')}"
        _log_event({
                "event": "open_failed",
                "symbol": symbol,
                "bx_symbol": bx_symbol,
                "error": err,
                "trade_id": trade_id,
        })
        return {
            "status": "error",
            "error": err,
        }
    order = (resp.get("data") or {}).get("order") or {}
    oid = str(order.get("orderId", ""))
    _log_event({
        "event": "open",
        "symbol": symbol,
        "bx_symbol": bx_symbol,
        "order_id": oid,
        "client_order_id": client_order_id,
        "qty": qty,
        "price": price,
        "leverage": leverage,
        "margin_usdt": MARGIN_USDT,
        "trade_id": trade_id,
    })
    return {
        "status": "opened",
        "order_id": oid,
        "client_order_id": client_order_id,
        "qty": qty,
        "symbol": bx_symbol,
        "leverage": leverage,
        "margin_usdt": MARGIN_USDT,
        "trade_id": trade_id,
    }

def open_position(symbol: str, price: float, trade_id: str = None, fill_timeout_sec: int = 30) -> dict:
    """Оркестровка входа: проверка контракта → open_long → дождаться
    подтверждения позиции на бирже.
    НЕ размещает TP/SL — это отдельный шаг (attach_protection), чтобы
    вызывающая сторона (monitor.py) успела сохранить crash-safe checkpoint
    между подтверждением открытия позиции и размещением защиты.
    """
    bx_symbol = to_bx_symbol(symbol)
    contract = get_contract(symbol)
    
    if not contract:
        return {
            "status": "skipped",
            "reason": "contract_not_found",
            "symbol": bx_symbol,
        }

    if contract.get("status") != 1:
        return {
            "status": "skipped",
            "reason": "contract_unavailable",
            "symbol": bx_symbol,
        }

    if str(contract.get("apiStateOpen", "")).lower() != "true":
        return {
            "status": "skipped",
            "reason": "api_open_disabled",
            "symbol": bx_symbol,
        }

    asset_class = classify_bingx_contract(contract)
    open_res = open_long(symbol, price, trade_id=trade_id)
    status = open_res.get("status")
    if status == "foreign_position":
        return {
            "status": "foreign_position",
            "symbol": bx_symbol,
            "asset_class": asset_class,
            "open": open_res,
        }
    if status not in ("opened", "already_open"):
        return {"status": "error", "error": open_res.get("error"), "symbol": bx_symbol, "open": open_res}
    pos = wait_for_position_fill(symbol, timeout_sec=fill_timeout_sec)
    if pos.get("status") != "found":
        final_pos = get_position(symbol)
        if final_pos.get("status") == "found" and final_pos.get("positionAmt"):
            confirmed_qty = float(final_pos["positionAmt"])
            log.info(
                f"[{symbol}] позиция подтверждена после timeout: "
                f"positionAmt={confirmed_qty}"
            )
            return {
                "status": "found",
                "symbol": bx_symbol,
                "asset_class": asset_class,
                "open": open_res,
                "position": pos,
                "avg_price": pos.get("avgPrice"),
                "qty_initial": pos.get("positionAmt"),
                "qty_remaining": pos.get("positionAmt"),
            }
        qty_opened = float(open_res.get("qty") or 0.0)
        log.warning(
            f"[{symbol}] позиция не подтверждена биржей — "
            f"используется запрошенный qty={qty_opened} (не подтверждён)"
        )
        return {
            "status": "open_no_tp",
            "symbol": bx_symbol,
            "asset_class": asset_class,
            "open": open_res,
            "position": pos,
            "qty_initial": qty_opened,
            "qty_remaining": qty_opened,
            "qty_initial_uncertain": True,
        }
    return {
        "status": "found",
        "symbol": bx_symbol,
        "asset_class": asset_class,
        "open": open_res,
        "position": pos,
        "avg_price": pos.get("avgPrice"),
        "qty_initial": pos.get("positionAmt"),
        "qty_remaining": pos.get("positionAmt"),
    }


def attach_protection(symbol: str, avg_price: float, qty: float, tp_levels: list,
                       stop_loss_pct: float, trade_id: str = None) -> dict:
    """Размещает TP-ордера и STOP_LOSS для уже открытой и подтверждённой позиции.
    Вызывается ПОСЛЕ того как monitor.py сохранил checkpoint по open_position().
    """
    tp_result = place_take_profit_orders(symbol, avg_price, qty, tp_levels, trade_id=trade_id)
    tp_status = "TP_PLACED" if tp_result.get("status") in ("created", "already_exists") else "TP_FAILED"
    sl_result = place_stop_loss_order(symbol, avg_price, qty, stop_loss_pct, trade_id=trade_id)
    return {
        "tp_result": tp_result,
        "tp_status": tp_status,
        "tp_orders": tp_result.get("orders", []) if tp_status == "TP_PLACED" else [],
        "sl_result": sl_result,
    }


def close_long(
    symbol: str,
    qty: float,
    cancel_tp: bool = True,
    client_order_id: str = None,
    trade_id: str = None,
) -> dict:
    """Закрывает LONG рыночным ордером.
    cancel_tp=True:
        Полное закрытие.
        Перед SELL TP и SL должны быть подтверждённо удалены.
    cancel_tp=False:
        Частичное сокращение.
        TP и SL НЕ отменяются.
    """
    if not qty or float(qty) <= 0:
        return {
            "status": "error",
            "error": "qty <= 0",
        }
    if cancel_tp:
        cancel_result = cancel_take_profit_orders(symbol)
        cancel_sl_result = cancel_stop_loss_orders(symbol)
        cancel_status = cancel_result.get("status")
        cancel_sl_status = cancel_sl_result.get("status")
        if cancel_status not in ("cancelled", "no_orders") or cancel_sl_status not in ("cancelled", "no_orders"):
            log.error(
                f"[{symbol}] FULL CLOSE BLOCKED: "
                f"TP/SL cancellation not confirmed: "
                f"tp_status={cancel_status} "
                f"tp_cancelled={cancel_result.get('cancelled_count')} "
                f"tp_total={cancel_result.get('total_found')} "
                f"tp_remaining={cancel_result.get('remaining_count')} "
                f"sl_status={cancel_sl_status} "
                f"sl_cancelled={cancel_sl_result.get('cancelled_count')} "
                f"sl_total={cancel_sl_result.get('total_found')} "
                f"sl_remaining={cancel_sl_result.get('remaining_count')}"
            )
            _log_event({
                "event": "close_blocked_tp_sl_not_safe",
                "symbol": symbol,
                "trade_id": trade_id,
                "qty_requested": float(qty),
                "tp_cancel_status": cancel_status,
                "tp_cancel_result": cancel_result,
                "sl_cancel_status": cancel_sl_status,
                "sl_cancel_result": cancel_sl_result,
            })
            return {
                "status": "blocked",
                "error": (
                    "full close blocked: "
                    "TP/SL cancellation not safely confirmed"
                ),
                "tp_cancel_result": cancel_result,
                "sl_cancel_result": cancel_sl_result,
            }
        log.info(
            f"[{symbol}] TP/SL safe before FULL CLOSE: "
            f"tp={cancel_status} sl={cancel_sl_status}"
        )
    return _close_position(
        to_bx_symbol(symbol),
        float(qty),
        client_order_id,
        trade_id,
    )


def _close_position(bx_symbol: str, qty: float, client_order_id: str = None, trade_id: str = None) -> dict:
    real_amt = _position_amt(bx_symbol)
    if qty > real_amt:
        if real_amt <= 0:
            return {"status": "skipped", "error": f"нет LONG позиции для {bx_symbol}"}
        log.warning(f"[{bx_symbol}] qty={qty} > real_amt={real_amt} — ограничиваем до {real_amt}")
        qty = real_amt
    c = _contracts().get(bx_symbol)
    if c:
        prec = int(c.get("quantityPrecision") or 0)
        min_qty = float(c.get("tradeMinQuantity") or c.get("minQty") or 0)
        rounded = _round_qty(qty, prec)
        if rounded <= 0:
            log.warning(f"[{bx_symbol}] округление {qty}→0 — отправляем исходный qty")
        elif min_qty and rounded < min_qty:
            log.warning(f"[{bx_symbol}] qty={rounded}<minQty={min_qty} — отправляем исходный qty")
        else:
            qty = rounded
    else:
        log.warning(f"[{bx_symbol}] данные контракта недоступны — закрываем без округления qty={qty}")
    if qty <= 0:
        return {"status": "skipped", "error": f"qty<=0 ({qty})"}
    params = {"symbol": bx_symbol, "side": "SELL", "positionSide": "LONG",
              "type": "MARKET", "quantity": str(qty)}
    if client_order_id:
        params["clientOrderId"] = client_order_id
    resp = _request("POST", ORDER_PATH, params)
    msg = str(resp.get("msg", "")).lower()
    if resp.get("code") != 0 and ("positionside" in msg or "position side" in msg):
        log.error(f"[{bx_symbol}] BingX не принимает positionSide=LONG при закрытии: {resp.get('msg')}")
        _log_event({"event": "close_failed", "bx_symbol": bx_symbol, "qty": qty,
                    "error": f"Hedge Mode not supported: {resp.get('msg')}", "trade_id": trade_id})
        return {"status": "error", "error": f"Hedge Mode не поддерживается: {resp.get('msg')}"}
    if resp.get("code") == 0:
        order = (resp.get("data") or {}).get("order") or {}
        oid = str(order.get("orderId", ""))
        _log_event({"event": "close", "bx_symbol": bx_symbol, "order_id": oid, "qty": qty,
                    "trade_id": trade_id})
        return {"status": "closed", "order_id": oid, "qty": qty, "symbol": bx_symbol}
    err = f"code={resp.get('code')} msg={resp.get('msg')}"
    _log_event({"event": "close_failed", "bx_symbol": bx_symbol, "qty": qty, "error": err,
                "trade_id": trade_id})
    return {"status": "error", "error": err}
