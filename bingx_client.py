"""
bingx_client.py — BingX USDT-M Perpetual Swap, демо-счёт (VST) / Live.
"""

import os, json, time, hmac, hashlib, math, logging, uuid, re
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
    "index": {},
}
_CONTRACT_TTL = 3600


def _clean_ticker(s: str) -> str:
    """Очищает тикер от разделителей и суффикса USDT/USD/PERP."""
    if not s:
        return ""
    t = str(s).strip().upper().replace("/", "").replace("-", "").replace("_", "")
    for suffix in ("USDT", "USD", "PERP"):
        if t.endswith(suffix) and len(t) > len(suffix):
            t = t[:-len(suffix)]
            break
    return t


def _strip_multiplier(t: str) -> str:
    """Удаляет числовые множители (1000, 1000000, 1M, 1K, 10000, 100 и т.д.)."""
    m = re.match(r"^(1000000|100000|10000|1000|100|1M|1K|M|K)([A-Z0-9]+)$", t)
    if m:
        return m.group(2)
    return t


def _clean_and_extract_tokens(raw_str: str) -> set:
    """
    Извлекает все варианты тикера из одной строки:
    - очищенный тикер (FLOKI-USDT -> FLOKI)
    - содержимое скобок: GOLD(XAU)-USDT -> XAU
    - имя без скобок: ANTHROPIC(Pre-IPO)-USDT -> ANTHROPIC
    - биржевые суффиксы акций: AMDUS -> AMD, NOKUS -> NOK
    """
    if not raw_str:
        return set()
    s = str(raw_str).strip().upper()
    tokens = set()
    tokens.add(s)

    clean_basic = _clean_ticker(s)
    if clean_basic:
        tokens.add(clean_basic)

    # 1. Извлечение содержимого скобок: GOLD(XAU) -> XAU
    inside_paren = re.findall(r"\((.*?)\)", s)
    for p in inside_paren:
        p_clean = _clean_ticker(p)
        if p_clean and len(p_clean) >= 2:
            tokens.add(p_clean)

    # 2. Очистка от скобок: ANTHROPIC(Pre-IPO)-USDT -> ANTHROPIC
    without_paren = _clean_ticker(re.sub(r"\(.*?\)", "", s))
    if without_paren:
        tokens.add(without_paren)

    # 3. Суффиксы акций US: AMDUS -> AMD, NOKUS -> NOK
    for t in list(tokens):
        if t.endswith("US") and len(t) > 3:
            tokens.add(t[:-2])

    return tokens


def _index_contract(c: dict, index: dict, exact_data: dict):
    """
    Автоматически генерирует полную матрицу ключей для контракта BingX.
    Гарантирует поиск O(1) для любых форматов Coinalyze/Binance.
    """
    sym = str(c.get("symbol", "")).strip().upper()
    asset = str(c.get("asset", "")).strip().upper()
    display_name = str(c.get("displayName", "")).strip().upper()

    if sym:
        exact_data[sym] = c

    keys = set()
    for raw in (sym, asset, display_name):
        extracted = _clean_and_extract_tokens(raw)
        for tok in extracted:
            base = _strip_multiplier(tok)
            keys.add(tok)
            keys.add(f"{tok}-USDT")
            keys.add(f"{tok}USDT")
            keys.add(base)
            keys.add(f"{base}-USDT")
            keys.add(f"{base}USDT")

            for mult in ("1000", "1000000", "1M", "1K", "10000", "100"):
                keys.add(f"{mult}{base}")
                keys.add(f"{mult}{base}-USDT")
                keys.add(f"{mult}{base}USDT")

    for k in keys:
        if k not in index:
            index[k] = c


def _refresh_contracts() -> dict:
    """Загружает все контракты BingX и строит полный динамический индекс."""
    now = time.time()
    resp = _request("GET", CONTRACTS_PATH, signed=False)

    if resp.get("code") != 0:
        err = f"code={resp.get('code')} msg={resp.get('msg')}"
        log.error(f"contracts refresh failed: {err}")
        return {"status": "error", "error": err}

    data = {}
    index = {}

    for c in resp.get("data", []) or []:
        _index_contract(c, index, data)

    _CONTRACT_CACHE.update({
        "ts": now,
        "data": data,
        "index": index,
    })

    log.info(f"contracts refresh OK: {len(data)} contracts, {len(index)} indexed keys")
    return {"status": "ok", "count": len(data)}


def _contracts() -> dict:
    now = time.time()
    if _CONTRACT_CACHE["data"] and now - _CONTRACT_CACHE["ts"] < _CONTRACT_TTL:
        return _CONTRACT_CACHE["data"]

    refresh = _refresh_contracts()
    if refresh.get("status") == "ok":
        return _CONTRACT_CACHE["data"]

    return _CONTRACT_CACHE["data"]


def get_contract(symbol: str) -> dict | None:
    """
    Полностью динамический поиск контракта BingX.
    Автоматически находит FLOKI, 1000PEPE, 1MBABYDOGE, ANTHROPIC, 龙虾 и любые новые листинги.
    """
    s = (symbol or "").strip().upper()
    if not s:
        return None

    # Опциональный ручной оверрайд из ENV
    if s in SYMBOL_MAP:
        mapped = str(SYMBOL_MAP[s]).strip().upper()
        if mapped:
            c = _contracts().get(mapped) or _CONTRACT_CACHE.get("index", {}).get(mapped)
            if c:
                return c

    _contracts()  # гарантирует актуальность кэша
    index = _CONTRACT_CACHE.get("index", {})

    # 1. Прямой поиск в авто-индексе
    if s in index:
        return index[s]

    clean = _clean_ticker(s)
    if not clean:
        return None

    variants = [clean]
    for sfx in ("CTO", "OLD", "NEW", "2"):
        if clean.endswith(sfx) and len(clean) > len(sfx) + 2:
            variants.append(clean[:-len(sfx)])

    for var in variants:
        base = _strip_multiplier(var)
        candidates = (
            var,
            f"{var}-USDT",
            f"{var}USDT",
            base,
            f"{base}-USDT",
            f"{base}USDT",
        )
        for cand in candidates:
            if cand in index:
                return index[cand]

    # 2. Если монета только что залистилась и кэш старше 60с — обновляем
    if time.time() - _CONTRACT_CACHE["ts"] > 60:
        refresh = _refresh_contracts()
        if refresh.get("status") == "ok":
            index = _CONTRACT_CACHE.get("index", {})
            for var in variants:
                base = _strip_multiplier(var)
                candidates = (
                    var,
                    f"{var}-USDT",
                    f"{var}USDT",
                    base,
                    f"{base}-USDT",
                    f"{base}USDT",
                )
                for cand in candidates:
                    if cand in index:
                        return index[cand]

    return None


def to_bx_symbol(symbol: str) -> str:
    """Возвращает канонический тикер BingX (например, FLOKI-USDT или 1000PEPE-USDT)."""
    s = (symbol or "").strip().upper()
    if not s:
        return symbol

    if s in SYMBOL_MAP:
        return str(SYMBOL_MAP[s]).strip().upper()

    contract = get_contract(s)
    if contract:
        return str(contract.get("symbol", "")).strip().upper()

    clean = _clean_ticker(s)
    return f"{clean}-USDT" if clean else symbol


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


def _tp_belongs_to_trade(parsed: dict | None, trade_id: str = None) -> bool:
    """Проверка что TP ордер принадлежит конкретной сделке."""
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
    return hmac.new(
        SECRET_KEY.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _request(
    method: str,
    path: str,
    params: dict | None = None,
    signed: bool = True,
    retries: int = 2,
):
    params = dict(params or {})
    if signed:
        if not API_KEY or not SECRET_KEY:
            return {
                "code": -1,
                "msg": "BINGX_API_KEY / BINGX_SECRET_KEY не заданы в env",
            }
        params["timestamp"] = str(int(time.time() * 1000))
        params["signature"] = _sign(params)
    headers = {"X-BX-APIKEY": API_KEY} if signed else {}
    url = BASE_URL + path
    last_err = "unknown"
    for attempt in range(retries):
        try:
            resp = requests.request(
                method, url, headers=headers, params=params, timeout=10
            )
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


def _normalize_orders_list(resp: dict) -> list:
    raw = resp.get("data", []) or []
    if isinstance(raw, dict):
        raw = raw.get("orders", []) or []
    if not isinstance(raw, list):
        return []
    return [o for o in raw if isinstance(o, dict)]


# ============================================================
# SYMBOL / CONTRACT UTILS & QUANTUM ALLOCATION
# ============================================================
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


def _format_qty(qty: float, precision: int) -> str:
    """Безопасное форматирование объёма: исключает scientific notation (1e-05) и лишние нули."""
    if precision <= 0:
        return str(int(round(qty)))
    return f"{qty:.{precision}f}"


def _format_price(price: float, precision: int) -> str:
    """Форматирует цену триггера строго по pricePrecision контракта."""
    if precision <= 0:
        return str(int(round(price)))
    return f"{price:.{precision}f}"


def allocate_tp_quanta(
    position_qty: float,
    tp_levels: list[dict],
    precision: int,
) -> dict[str, float]:
    """
    Дискретное квантование объёмов TP (Largest Remainder Method).
    Гарантирует:
    1. Каждому TP выделяется >= 1 кванта (step).
    2. Сумма TP <= position_qty (сохраняя Runner).
    3. Работает для любых цен: от PEPE (0.00001) до BTC (100k).
    """
    step = 10**-precision if precision > 0 else 1.0
    total_quanta = int(round(position_qty / step))
    k = len(tp_levels)

    if total_quanta <= 0 or k == 0:
        return {tp.get("leg", f"tp{i+1}"): 0.0 for i, tp in enumerate(tp_levels)}

    if total_quanta < k:
        sorted_levels = sorted(
            enumerate(tp_levels),
            key=lambda x: float(x[1].get("close_fraction", 0.0)),
            reverse=True,
        )
        quanta_alloc = [0] * k
        for idx in range(total_quanta):
            orig_i = sorted_levels[idx][0]
            quanta_alloc[orig_i] = 1
        return {
            tp.get("leg", f"tp{i+1}"): (
                int(quanta_alloc[i]) if precision <= 0 else round(quanta_alloc[i] * step, precision)
            )
            for i, tp in enumerate(tp_levels)
        }

    sum_fractions = sum(float(tp.get("close_fraction", 0.0)) for tp in tp_levels)
    target_tp_quanta = int(round(total_quanta * sum_fractions))
    target_tp_quanta = max(k, min(total_quanta, target_tp_quanta))

    quanta_alloc = [1] * k
    remaining_quanta = target_tp_quanta - k

    ideal_extras = []
    for tp in tp_levels:
        frac = float(tp.get("close_fraction", 0.0))
        ideal_total = total_quanta * frac
        ideal_extras.append(max(0.0, ideal_total - 1.0))

    sum_ideal_extras = sum(ideal_extras)

    if sum_ideal_extras > 0 and remaining_quanta > 0:
        floored_extras = []
        remainders = []
        for i, extra in enumerate(ideal_extras):
            weight = extra / sum_ideal_extras
            alloc_exact = remaining_quanta * weight
            alloc_floor = int(math.floor(alloc_exact))
            floored_extras.append(alloc_floor)
            remainders.append((alloc_exact - alloc_floor, i))

        for i in range(k):
            quanta_alloc[i] += floored_extras[i]

        leftover = remaining_quanta - sum(floored_extras)
        remainders.sort(
            key=lambda x: (x[0], float(tp_levels[x[1]].get("close_fraction", 0.0))),
            reverse=True,
        )
        for i in range(leftover):
            quanta_alloc[remainders[i][1]] += 1

    return {
        tp.get("leg", f"tp{i+1}"): (
            int(quanta_alloc[i]) if precision <= 0 else round(quanta_alloc[i] * step, precision)
        )
        for i, tp in enumerate(tp_levels)
    }


def _qty_for(c: dict, price: float, leverage: int):
    mult = float(c.get("multiplier") or 1)
    prec = int(c.get("quantityPrecision") or 0)
    step = 10**-prec if prec > 0 else 1.0
    min_qty = float(c.get("tradeMinQuantity") or c.get("minQty") or 0)
    min_notional = float(c.get("tradeMinUSDT") or c.get("minNotional") or 0)

    min_notional_qty = (min_notional / (price * mult)) if (price > 0 and mult > 0) else 0.0
    min_required_qty = max(min_qty, 3 * step, min_notional_qty)

    if not price or price <= 0 or mult <= 0:
        return None, prec, min_required_qty

    raw = (MARGIN_USDT * leverage) / (price * mult)
    if prec > 0:
        qty = round(math.floor(raw / step) * step, prec)
    else:
        qty = float(int(raw))
    return qty, prec, min_required_qty


# ============================================================
# TRADE ID HASH / CLIENT ORDER ID
# ============================================================
def _trade_id_hash(trade_id: str) -> str:
    return hashlib.sha256(str(trade_id).encode("utf-8")).hexdigest()[:8]


def _is_hex8(s: str) -> bool:
    return (
        isinstance(s, str)
        and len(s) == 8
        and all(ch in "0123456789abcdef" for ch in s.lower())
    )


def build_tp_client_order_id(leg: str, trade_id: str = None) -> str:
    key = _trade_id_hash(trade_id) if trade_id else uuid.uuid4().hex[:8]
    return f"{TP_CLIENT_ORDER_PREFIX}{key}_{leg}"


def parse_tp_client_order_id(client_id: str) -> dict | None:
    if not client_id:
        return None
    parts = client_id.upper().split("_")
    valid_legs_upper = {leg.upper() for leg in VALID_TP_LEGS}
    if (
        len(parts) != 4
        or parts[0] != "CM"
        or parts[1] != "TP"
        or not _is_hex8(parts[2])
        or parts[3] not in valid_legs_upper
    ):
        return None
    return {
        "trade_id": None,
        "trade_hash": parts[2].lower(),
        "leg": parts[3].lower(),
    }


def build_sl_client_order_id(trade_id: str = None) -> str:
    key = _trade_id_hash(trade_id) if trade_id else uuid.uuid4().hex[:8]
    return f"{SL_CLIENT_ORDER_PREFIX}{key}"


def build_open_client_order_id(symbol: str, trade_id: str = None) -> str:
    raw = f"{symbol}:{trade_id}" if trade_id else symbol
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{OPEN_CLIENT_ORDER_PREFIX}{key}"


def parse_sl_client_order_id(client_id: str) -> dict | None:
    if not client_id:
        return None
    parts = client_id.upper().split("_")
    if (
        len(parts) != 3
        or parts[0] != "CM"
        or parts[1] != "SL"
        or not _is_hex8(parts[2])
    ):
        return None
    return {"trade_hash": parts[2].lower()}


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
    resp = _request("GET", POSITION_PATH)
    if resp.get("code") != 0:
        return {
            "status": "error",
            "error": f"code={resp.get('code')} msg={resp.get('msg')}",
        }
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
    return _position_amt(to_bx_symbol(symbol))


def get_position(symbol: str) -> dict:
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
                    "entryPrice": float(p.get("entryPrice", 0) or avg_price),
                }
    return {"status": "not_found", "symbol": bx_symbol}


def wait_for_position_fill(
    symbol: str, timeout_sec: int = 30, poll_interval: float = 1.0
) -> dict:
    bx_symbol = to_bx_symbol(symbol)
    start = time.time()
    while time.time() - start < timeout_sec:
        pos = get_position(symbol)
        if pos.get("status") == "found":
            log.info(
                f"[{symbol}] позиция появилась: avgPrice={pos.get('avgPrice')} qty={pos.get('positionAmt')}"
            )
            return pos
        time.sleep(poll_interval)
    log.warning(f"[{symbol}] позиция не появилась за {timeout_sec}с")
    return {"status": "timeout", "symbol": bx_symbol}


# ============================================================
# TP ORDER QUERIES
# ============================================================
def get_open_tp_orders(symbol: str) -> dict:
    bx_symbol = to_bx_symbol(symbol)
    resp = _request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": bx_symbol})
    if resp.get("code") != 0:
        return {"status": "error", "error": resp.get("msg", "unknown"), "orders": []}
    tp_orders = []
    for o in _normalize_orders_list(resp):
        is_our_tp = (
            o.get("type") in ("TAKE_PROFIT", "TAKE_PROFIT_MARKET")
            and o.get("positionSide") == "LONG"
            and str(o.get("clientOrderId", ""))
            .upper()
            .startswith(TP_CLIENT_ORDER_PREFIX)
        )
        if is_our_tp:
            tp_orders.append(o)
    return {"status": "ok", "orders": tp_orders, "count": len(tp_orders)}


def get_filled_tp_orders(
    symbol: str, opened_ts: int = None, trade_id: str = None
) -> dict:
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
        filled_tp.append(
            {
                "leg": parsed.get("leg"),
                "trade_id": parsed.get("trade_id"),
                "trade_hash": parsed.get("trade_hash"),
                "order_id": str(o.get("orderId", "")),
                "client_order_id": client_id,
                "status": status,
                "executed_qty": float(o.get("executedQty", 0) or 0),
                "avg_price": float(o.get("avgPrice", 0) or 0),
                "time": order_time,
            }
        )
    return {"status": "ok", "orders": filled_tp, "count": len(filled_tp)}


def get_existing_tp_legs(symbol: str, tp_levels: list, trade_id: str = None) -> dict:
    result = get_open_tp_orders(symbol)
    if result.get("status") == "error":
        return {
            "status": "error",
            "error": result.get("error", "TP query failed"),
            "legs": {tp.get("leg"): False for tp in tp_levels},
            "missing": [tp.get("leg") for tp in tp_levels],
            "all_present": False,
            "existing_qty": 0,
            "orders": [],
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
        "orders": result.get("orders", []),
    }


# ============================================================
# TP ORDER CREATION / CANCELLATION
# ============================================================
def place_take_profit_orders(
    symbol: str,
    avg_price: float,
    position_qty: float,
    tp_levels: list = None,
    trade_id: str = None,
) -> dict:
    """Создать сетку BingX TP ордеров через дискретное квантование объёмов."""
    if tp_levels is None:
        return {
            "status": "error",
            "error": "tp_levels must be explicitly supplied",
            "orders": [],
        }

    if not isinstance(tp_levels, list) or len(tp_levels) != 3:
        return {
            "status": "error",
            "error": "tp_levels must contain exactly 3 levels",
            "orders": [],
        }

    expected_legs = {"tp1", "tp2", "tp3"}
    actual_legs = {
        str(tp.get("leg"))
        for tp in tp_levels
        if isinstance(tp, dict)
    }

    if actual_legs != expected_legs:
        return {
            "status": "error",
            "error": (
                f"invalid TP legs: expected={sorted(expected_legs)} "
                f"got={sorted(actual_legs)}"
            ),
            "orders": [],
        }

    for tp in tp_levels:
        try:
            pnl_pct = float(tp.get("pnl_pct"))
            close_fraction = float(tp.get("close_fraction"))
        except (TypeError, ValueError):
            return {
                "status": "error",
                "error": f"invalid TP definition: {tp}",
                "orders": [],
            }

        if pnl_pct <= 0:
            return {
                "status": "error",
                "error": f"TP pnl_pct must be > 0: {pnl_pct}",
                "orders": [],
            }

        if close_fraction <= 0 or close_fraction >= 1:
            return {
                "status": "error",
                "error": f"TP close_fraction invalid: {close_fraction}",
                "orders": [],
            }

    bx_symbol = to_bx_symbol(symbol)
    existing_check = get_existing_tp_legs(symbol, tp_levels, trade_id=trade_id)
    if existing_check.get("all_present"):
        log.info(f"[{symbol}] все TP legs уже существуют, пропускаем создание")
        return {
            "status": "already_exists",
            "legs": existing_check.get("legs"),
            "orders": [],
            "existing_qty": existing_check.get("existing_qty"),
        }

    missing_legs = existing_check.get("missing", [])
    c = _contracts().get(bx_symbol)
    if not c:
        return {"status": "error", "error": f"контракт {bx_symbol} не найден"}

    prec = int(c.get("quantityPrecision") or 0)
    price_prec = int(c.get("pricePrecision") or 4)

    # Дискретное квантованное распределение (все TP >= 1 кванта)
    allocated_qtys = allocate_tp_quanta(position_qty, tp_levels, prec)

    orders_created = []
    orders_to_rollback = []

    for i, tp in enumerate(tp_levels):
        leg = tp.get("leg", f"tp{i + 1}")
        if leg not in missing_legs:
            continue

        pnl_pct = float(tp.get("pnl_pct", 0))
        tp_price = avg_price * (1 + pnl_pct / 100)
        tp_qty = allocated_qtys.get(leg, 0.0)

        if tp_qty <= 0:
            err = f"[{symbol}] {leg}: квантованный объём <= 0 (position_qty={position_qty})"
            log.error(err)
            for rollback_oid in orders_to_rollback:
                _request("DELETE", ORDER_PATH, {"symbol": bx_symbol, "orderId": rollback_oid})
            return {"status": "error", "error": err, "failed_leg": leg}

        client_order_id = build_tp_client_order_id(leg, trade_id)
        params = {
            "symbol": bx_symbol,
            "side": "SELL",
            "positionSide": "LONG",
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": _format_price(tp_price, price_prec),
            "quantity": _format_qty(tp_qty, prec),
            "clientOrderId": client_order_id,
        }

        resp = _request("POST", ORDER_PATH, params)
        if resp.get("code") == 0:
            order = (resp.get("data") or {}).get("order") or {}
            oid = str(order.get("orderId", ""))
            orders_created.append(
                {
                    "leg": leg,
                    "order_id": oid,
                    "client_order_id": client_order_id,
                    "price": tp_price,
                    "pnl_pct": pnl_pct,
                    "qty": tp_qty,
                    "trade_id": trade_id,
                }
            )
            orders_to_rollback.append(oid)
            _log_event(
                {
                    "event": "tp_created",
                    "symbol": symbol,
                    "bx_symbol": bx_symbol,
                    "leg": leg,
                    "order_id": oid,
                    "client_order_id": client_order_id,
                    "trade_id": trade_id,
                    "avg_price": avg_price,
                    "tp_price": tp_price,
                    "pnl_pct": pnl_pct,
                    "qty": tp_qty,
                    "position_qty": position_qty,
                }
            )
            log.info(
                f"[{symbol}] {leg} создан: orderId={oid} price={tp_price:.6f} qty={tp_qty}"
            )
        else:
            err = f"code={resp.get('code')} msg={resp.get('msg')}"
            log.error(
                f"[{symbol}] {leg} creation failed: {err}, rolling back {len(orders_to_rollback)} orders"
            )
            for rollback_oid in orders_to_rollback:
                _request(
                    "DELETE",
                    ORDER_PATH,
                    {"symbol": bx_symbol, "orderId": rollback_oid},
                )
            _log_event(
                {
                    "event": "tp_creation_failed",
                    "symbol": symbol,
                    "bx_symbol": bx_symbol,
                    "leg": leg,
                    "client_order_id": client_order_id,
                    "trade_id": trade_id,
                    "error": err,
                    "tp_price": tp_price,
                    "qty": tp_qty,
                    "rolled_back_count": len(orders_to_rollback),
                }
            )
            return {
                "status": "error",
                "error": err,
                "failed_leg": leg,
                "rolled_back": len(orders_to_rollback),
            }

    if len(orders_created) != len(missing_legs):
        err = f"создано {len(orders_created)} из {len(missing_legs)} недостающих TP"
        for rollback_oid in orders_to_rollback:
            _request("DELETE", ORDER_PATH, {"symbol": bx_symbol, "orderId": rollback_oid})
        return {"status": "error", "error": err}

    return {
        "status": "created",
        "orders": orders_created,
        "avg_price": avg_price,
        "position_qty": position_qty,
        "missing_legs_created": missing_legs,
        "trade_id": trade_id,
    }


def cancel_take_profit_orders(symbol: str) -> dict:
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
            ORDER_PATH,
            {
                "symbol": bx_symbol,
                "orderId": oid,
            },
        )
        if resp.get("code") == 0:
            cancelled += 1
            _log_event(
                {
                    "event": "tp_cancelled",
                    "symbol": symbol,
                    "bx_symbol": bx_symbol,
                    "order_id": str(oid),
                    "client_order_id": client_oid,
                    "leg": parsed.get("leg", "unknown"),
                    "trade_id": parsed.get("trade_id"),
                    "trade_hash": parsed.get("trade_hash"),
                    "type": order.get("type"),
                }
            )
            log.info(f"[{symbol}] TP {parsed.get('leg')} отменён orderId={oid}")
        else:
            log.warning(f"[{symbol}] отмена TP {oid} failed: {resp.get('msg')}")

    verify = get_open_tp_orders(symbol)
    if verify.get("status") == "error":
        return {
            "status": "error",
            "error": f"TP cancellation verification failed: {verify.get('error', 'unknown')}",
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
        "error": f"После отмены остаются TP: {remaining_count}/{total_found}",
    }


# ============================================================
# SL ORDER QUERIES / CREATION / CANCELLATION
# ============================================================
def get_open_sl_orders(symbol: str) -> dict:
    bx_symbol = to_bx_symbol(symbol)
    resp = _request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": bx_symbol})
    if resp.get("code") != 0:
        return {"status": "error", "error": resp.get("msg", "unknown"), "orders": []}
    sl_orders = []
    for o in _normalize_orders_list(resp):
        is_our_sl = (
            o.get("type") in ("STOP", "STOP_MARKET")
            and o.get("positionSide") == "LONG"
            and str(o.get("clientOrderId", ""))
            .upper()
            .startswith(SL_CLIENT_ORDER_PREFIX)
        )
        if is_our_sl:
            sl_orders.append(o)
    return {"status": "ok", "orders": sl_orders, "count": len(sl_orders)}


def get_filled_sl_orders(
    symbol: str, opened_ts: int = None, trade_id: str = None
) -> dict:
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
        filled_sl.append(
            {
                "trade_hash": parsed.get("trade_hash"),
                "order_id": str(o.get("orderId", "")),
                "client_order_id": client_id,
                "status": status,
                "executed_qty": float(o.get("executedQty", 0) or 0),
                "avg_price": float(o.get("avgPrice", 0) or 0),
                "time": order_time,
            }
        )
    return {"status": "ok", "orders": filled_sl, "count": len(filled_sl)}


def place_stop_loss_order(
    symbol: str,
    avg_price: float,
    qty: float,
    stop_loss_pct: float,
    trade_id: str = None,
) -> dict:
    bx_symbol = to_bx_symbol(symbol)
    if not avg_price or avg_price <= 0:
        return {"status": "error", "error": "некорректная avg_price"}
    try:
        stop_loss_pct = float(stop_loss_pct)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "error": f"invalid stop_loss_pct={stop_loss_pct}",
        }

    if not (0 < stop_loss_pct <= 25):
        return {
            "status": "error",
            "error": f"stop_loss_pct out of safe range: {stop_loss_pct}",
        }

    c = _contracts().get(bx_symbol)
    if not c:
        return {"status": "error", "error": f"контракт {bx_symbol} не найден"}

    prec = int(c.get("quantityPrecision") or 0)
    price_prec = int(c.get("pricePrecision") or 4)
    min_qty = float(c.get("tradeMinQuantity") or c.get("minQty") or 0)
    qty_r = _round_qty(qty, prec)

    if qty_r <= 0 or (min_qty and qty_r < min_qty):
        return {"status": "error", "error": f"qty={qty_r} < minQty={min_qty}"}

    stop_price = avg_price * (1 - stop_loss_pct / 100)
    client_order_id = build_sl_client_order_id(trade_id)
    params = {
        "symbol": bx_symbol,
        "side": "SELL",
        "positionSide": "LONG",
        "type": "STOP_MARKET",
        "stopPrice": _format_price(stop_price, price_prec),
        "quantity": _format_qty(qty_r, prec),
        "clientOrderId": client_order_id,
    }

    resp = _request("POST", ORDER_PATH, params)
    if resp.get("code") == 0:
        order = (resp.get("data") or {}).get("order") or {}
        oid = str(order.get("orderId", ""))
        _log_event(
            {
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
            }
        )
        log.info(
            f"[{symbol}] SL создан: orderId={oid} stopPrice={stop_price:.6f} qty={qty_r}"
        )
        return {
            "status": "created",
            "order_id": oid,
            "client_order_id": client_order_id,
            "stop_price": stop_price,
            "qty": qty_r,
        }

    err = f"code={resp.get('code')} msg={resp.get('msg')}"
    _log_event(
        {
            "event": "sl_creation_failed",
            "symbol": symbol,
            "bx_symbol": bx_symbol,
            "client_order_id": client_order_id,
            "trade_id": trade_id,
            "error": err,
            "stop_price": stop_price,
            "qty": qty_r,
        }
    )
    log.error(f"[{symbol}] SL creation failed: {err}")
    return {"status": "error", "error": err}


def cancel_stop_loss_orders(symbol: str) -> dict:
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
            log.warning(
                f"[{symbol}] SL order {oid} не имеет нашего clientOrderId — НЕ отменяем"
            )
            continue
        resp = _request(
            "DELETE",
            ORDER_PATH,
            {"symbol": bx_symbol, "orderId": oid},
        )
        if resp.get("code") == 0:
            cancelled += 1
            _log_event(
                {
                    "event": "sl_cancelled",
                    "symbol": symbol,
                    "bx_symbol": bx_symbol,
                    "order_id": str(oid),
                    "client_order_id": client_oid,
                    "trade_hash": parsed.get("trade_hash"),
                    "type": order.get("type"),
                }
            )
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


def update_stop_loss_order(
    symbol: str,
    new_stop_price: float,
    qty: float,
    trade_id: str = None,
) -> dict:
    bx_symbol = to_bx_symbol(symbol)
    if not new_stop_price or new_stop_price <= 0:
        return {"status": "error", "error": "некорректный new_stop_price"}

    cancel_res = cancel_stop_loss_orders(symbol)
    if cancel_res.get("status") in ("error", "partial_or_failed"):
        log.warning(
            f"[{symbol}] update_stop_loss_order: отмена старых SL вернула "
            f"{cancel_res.get('status')}: {cancel_res.get('error')}"
        )

    c = _contracts().get(bx_symbol)
    if not c:
        return {"status": "error", "error": f"контракт {bx_symbol} не найден"}

    prec = int(c.get("quantityPrecision") or 0)
    price_prec = int(c.get("pricePrecision") or 4)
    min_qty = float(c.get("tradeMinQuantity") or c.get("minQty") or 0)
    qty_r = _round_qty(qty, prec)

    if qty_r <= 0 or (min_qty and qty_r < min_qty):
        return {"status": "error", "error": f"qty={qty_r} < minQty={min_qty}"}

    client_order_id = build_sl_client_order_id(trade_id)
    params = {
        "symbol": bx_symbol,
        "side": "SELL",
        "positionSide": "LONG",
        "type": "STOP_MARKET",
        "stopPrice": _format_price(new_stop_price, price_prec),
        "quantity": _format_qty(qty_r, prec),
        "clientOrderId": client_order_id,
    }

    resp = _request("POST", ORDER_PATH, params)
    if resp.get("code") == 0:
        order = (resp.get("data") or {}).get("order") or {}
        oid = str(order.get("orderId", ""))
        _log_event(
            {
                "event": "sl_trailing_updated",
                "symbol": symbol,
                "bx_symbol": bx_symbol,
                "order_id": oid,
                "client_order_id": client_order_id,
                "trade_id": trade_id,
                "stop_price": new_stop_price,
                "qty": qty_r,
            }
        )
        log.info(
            f"[{symbol}] Trailing SL обновлён: orderId={oid} stopPrice={new_stop_price:.6f} qty={qty_r}"
        )
        return {
            "status": "created",
            "order_id": oid,
            "client_order_id": client_order_id,
            "stop_price": new_stop_price,
            "qty": qty_r,
        }

    err = f"code={resp.get('code')} msg={resp.get('msg')}"
    _log_event(
        {
            "event": "sl_trailing_failed",
            "symbol": symbol,
            "bx_symbol": bx_symbol,
            "client_order_id": client_order_id,
            "trade_id": trade_id,
            "error": err,
            "stop_price": new_stop_price,
            "qty": qty_r,
        }
    )
    log.error(f"[{symbol}] Trailing SL failed: {err}")
    return {"status": "error", "error": err}


# ============================================================
# FILL DETECTION HELPERS
# ============================================================
def compute_new_tp_fills(
    symbol: str, trade_id: str, opened_ts: int, processed_fills: dict
) -> dict:
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
        fills.append(
            {
                "order_id": order_id,
                "leg": leg,
                "client_order_id": filled.get("client_order_id"),
                "status": str(filled.get("status", "")).upper(),
                "executed_qty_delta": delta_qty,
                "executed_qty_total": executed_qty,
                "avg_price": avg_price,
                "fill_time_ms": int(filled.get("time", 0) or 0),
            }
        )
    return {"status": "ok", "fills": fills}


def get_last_filled_sl(
    symbol: str, opened_ts: int = None, trade_id: str = None
) -> dict:
    result = get_filled_sl_orders(symbol, opened_ts=opened_ts, trade_id=trade_id)
    if result.get("status") != "ok":
        return {
            "status": result.get("status", "error"),
            "order": None,
            "error": result.get("error"),
        }
    orders = result.get("orders", [])
    if not orders:
        return {"status": "ok", "order": None}
    last = sorted(orders, key=lambda o: int(o.get("time", 0) or 0))[-1]
    return {"status": "ok", "order": last}


# ============================================================
# LEVERAGE / OPEN / CLOSE
# ============================================================
def _set_leverage(bx_symbol: str, leverage: int) -> bool:
    resp = _request(
        "POST",
        LEVERAGE_PATH,
        {"symbol": bx_symbol, "side": "LONG", "leverage": str(leverage)},
    )
    if resp.get("code") == 0:
        return True
    resp = _request(
        "POST",
        LEVERAGE_PATH,
        {"symbol": bx_symbol, "side": "BOTH", "leverage": str(leverage)},
    )
    if resp.get("code") == 0:
        return True
    log.warning(
        f"[{bx_symbol}] set leverage {leverage}x failed: {resp.get('code')} {resp.get('msg')}"
    )
    return False


def open_long(symbol: str, price: float, trade_id: str = None) -> dict:
    bx_symbol = to_bx_symbol(symbol)
    amt = _position_amt(bx_symbol)
    if amt > 0:
        log.warning(
            f"[{symbol}] EXISTING LONG detected: "
            f"qty={amt}. New entry will NOT adopt this position."
        )
        _log_event(
            {
                "event": "open_skipped_existing_position",
                "symbol": symbol,
                "bx_symbol": bx_symbol,
                "existing_qty": amt,
                "trade_id": trade_id,
            }
        )
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
    qty, prec, min_required_qty = _qty_for(c, price, leverage)
    if qty is None:
        return {
            "status": "error",
            "error": "некорректная цена",
        }

    max_lev = int(c.get("maxLongLeverage") or c.get("maxLeverage") or MAX_LEVERAGE)
    max_lev = min(max_lev, MAX_LEVERAGE)

    # Автоподгон плеча, если лота не хватает на tradeMinUSDT, minQty или 3 кванта
    if qty < min_required_qty and leverage < max_lev:
        mult = float(c.get("multiplier") or 1)
        need_lev = math.ceil((min_required_qty * price * mult) / MARGIN_USDT)
        leverage = min(max(need_lev, leverage), max_lev)
        qty, prec, min_required_qty = _qty_for(c, price, leverage)

    if qty is None or qty <= 0 or qty < min_required_qty:
        return {
            "status": "error",
            "error": (
                f"маржа {MARGIN_USDT}$ слишком мала: "
                f"qty={qty} < minRequired={min_required_qty} "
                f"(поднимите BINGX_LEVERAGE или BINGX_MARGIN_USDT)"
            ),
        }

    _set_leverage(bx_symbol, leverage)
    client_order_id = build_open_client_order_id(bx_symbol, trade_id)
    params = {
        "symbol": bx_symbol,
        "side": "BUY",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": _format_qty(qty, prec),
        "clientOrderId": client_order_id,
    }

    log.info(
        f"[{symbol}] OPEN DEBUG: "
        f"bx={bx_symbol} price={price} "
        f"margin={MARGIN_USDT} leverage={leverage} "
        f"qty={qty} min_required_qty={min_required_qty} "
        f"quantityPrecision={prec} maxLongLeverage={max_lev}"
    )

    resp = _request("POST", ORDER_PATH, params)
    if resp.get("code") != 0:
        err_msg = str(resp.get("msg", "")).lower()
        if "positionside" in err_msg or "position side" in err_msg:
            log.error(
                f"[{symbol}] BingX не принимает positionSide=LONG: {resp.get('msg')}"
            )
            _log_event(
                {
                    "event": "open_failed",
                    "symbol": symbol,
                    "bx_symbol": bx_symbol,
                    "error": f"Hedge Mode not supported: {resp.get('msg')}",
                    "trade_id": trade_id,
                }
            )
            return {
                "status": "error",
                "error": f"Hedge Mode (positionSide=LONG) не поддерживается: {resp.get('msg')}",
            }
        err = f"code={resp.get('code')} msg={resp.get('msg')}"
        _log_event(
            {
                "event": "open_failed",
                "symbol": symbol,
                "bx_symbol": bx_symbol,
                "error": err,
                "trade_id": trade_id,
            }
        )
        return {
            "status": "error",
            "error": err,
        }

    order = (resp.get("data") or {}).get("order") or {}
    oid = str(order.get("orderId", ""))
    _log_event(
        {
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
        }
    )
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
    refresh = _refresh_contracts()

    if refresh.get("status") != "ok":
        return {
            "status": "skipped",
            "reason": "contract_state_unavailable",
            "symbol": str(symbol or "").strip().upper() or symbol,
            "error": refresh.get("error"),
        }

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

    asset_class = classify_bingx_contract(contract)

    if str(contract.get("apiStateOpen", "")).lower() != "true":
        return {
            "status": "skipped",
            "reason": "api_open_disabled",
            "symbol": bx_symbol,
            "asset_class": asset_class,
        }

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
        pos_check = get_position(symbol)
        if pos_check.get("status") == "found" and float(pos_check.get("positionAmt", 0) or 0) > 0:
            log.warning(
                f"[{symbol}] open_long вернул статус {status} ({open_res.get('error')}), "
                f"но позиция найдена на бирже (qty={pos_check.get('positionAmt')}). Безопасно подхватываем."
            )
            return {
                "status": "found",
                "symbol": bx_symbol,
                "asset_class": asset_class,
                "open": open_res,
                "position": pos_check,
                "avg_price": pos_check.get("avgPrice"),
                "qty_initial": float(pos_check.get("positionAmt")),
                "qty_remaining": float(pos_check.get("positionAmt")),
            }
        return {
            "status": "error",
            "error": open_res.get("error"),
            "symbol": bx_symbol,
            "open": open_res,
        }
    pos = wait_for_position_fill(symbol, timeout_sec=fill_timeout_sec)
    if pos.get("status") != "found":
        final_pos = get_position(symbol)
        if final_pos.get("status") == "found" and final_pos.get("positionAmt"):
            confirmed_qty = float(final_pos["positionAmt"])
            log.info(f"[{symbol}] позиция подтверждена после timeout: positionAmt={confirmed_qty}")
            return {
                "status": "found",
                "symbol": bx_symbol,
                "asset_class": asset_class,
                "open": open_res,
                "position": final_pos,
                "avg_price": final_pos.get("avgPrice"),
                "qty_initial": final_pos.get("positionAmt"),
                "qty_remaining": final_pos.get("positionAmt"),
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


def attach_protection(
    symbol: str,
    avg_price: float,
    qty: float,
    tp_levels: list,
    stop_loss_pct: float,
    trade_id: str = None,
) -> dict:
    tp_result = place_take_profit_orders(
        symbol, avg_price, qty, tp_levels, trade_id=trade_id
    )
    tp_status = (
        "TP_PLACED"
        if tp_result.get("status") in ("created", "already_exists")
        else "TP_FAILED"
    )
    sl_result = place_stop_loss_order(
        symbol, avg_price, qty, stop_loss_pct, trade_id=trade_id
    )
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
        if cancel_status not in ("cancelled", "no_orders") or cancel_sl_status not in (
            "cancelled",
            "no_orders",
        ):
            log.error(
                f"[{symbol}] FULL CLOSE BLOCKED: "
                f"TP/SL cancellation not confirmed: "
                f"tp_status={cancel_status} sl_status={cancel_sl_status}"
            )
            _log_event(
                {
                    "event": "close_blocked_tp_sl_not_safe",
                    "symbol": symbol,
                    "trade_id": trade_id,
                    "qty_requested": float(qty),
                    "tp_cancel_status": cancel_status,
                    "sl_cancel_status": cancel_sl_status,
                }
            )
            return {
                "status": "blocked",
                "error": "full close blocked: TP/SL cancellation not safely confirmed",
                "tp_cancel_result": cancel_result,
                "sl_cancel_result": cancel_sl_result,
            }
        log.info(
            f"[{symbol}] TP/SL safe before FULL CLOSE: tp={cancel_status} sl={cancel_sl_status}"
        )
    return _close_position(
        to_bx_symbol(symbol),
        float(qty),
        client_order_id,
        trade_id,
        is_full_close=cancel_tp,
    )


def _close_position(
    bx_symbol: str,
    qty: float,
    client_order_id: str = None,
    trade_id: str = None,
    is_full_close: bool = False,
) -> dict:
    real_amt = _position_amt(bx_symbol)
    if real_amt <= 0:
        return {
            "status": "already_closed",
            "error": f"нет LONG позиции для {bx_symbol}",
        }
    if is_full_close or qty > real_amt:
        if abs(qty - real_amt) > 1e-12:
            log.info(
                f"[{bx_symbol}] Full close: корректируем qty {qty} → real_amt {real_amt}"
            )
        qty = real_amt

    c = _contracts().get(bx_symbol)
    prec = int(c.get("quantityPrecision") or 0) if c else 0
    if c:
        min_qty = float(c.get("tradeMinQuantity") or c.get("minQty") or 0)
        rounded = _round_qty(qty, prec)
        if rounded <= 0:
            log.warning(f"[{bx_symbol}] округление {qty}→0 — отправляем исходный qty")
        elif min_qty and rounded < min_qty:
            log.warning(
                f"[{bx_symbol}] qty={rounded}<minQty={min_qty} — отправляем исходный qty"
            )
        else:
            qty = rounded
    else:
        log.warning(
            f"[{bx_symbol}] данные контракта недоступны — закрываем без округления qty={qty}"
        )

    if qty <= 0:
        return {"status": "skipped", "error": f"qty<=0 ({qty})"}

    params = {
        "symbol": bx_symbol,
        "side": "SELL",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": _format_qty(qty, prec),
    }
    if client_order_id:
        params["clientOrderId"] = client_order_id

    resp = _request("POST", ORDER_PATH, params)
    msg = str(resp.get("msg", "")).lower()
    if resp.get("code") != 0 and ("positionside" in msg or "position side" in msg):
        log.error(
            f"[{bx_symbol}] BingX не принимает positionSide=LONG при закрытии: {resp.get('msg')}"
        )
        _log_event(
            {
                "event": "close_failed",
                "bx_symbol": bx_symbol,
                "qty": qty,
                "error": f"Hedge Mode not supported: {resp.get('msg')}",
                "trade_id": trade_id,
            }
        )
        return {
            "status": "error",
            "error": f"Hedge Mode не поддерживается: {resp.get('msg')}",
        }

    if resp.get("code") == 0:
        order = (resp.get("data") or {}).get("order") or {}
        oid = str(order.get("orderId", ""))
        avg_p = float(order.get("avgPrice") or order.get("price") or 0.0) or None
        _log_event(
            {
                "event": "close",
                "bx_symbol": bx_symbol,
                "order_id": oid,
                "qty": qty,
                "trade_id": trade_id,
                "avg_price": avg_p,
            }
        )
        return {
            "status": "closed",
            "order_id": oid,
            "qty": qty,
            "symbol": bx_symbol,
            "avg_price": avg_p,
        }

    err = f"code={resp.get('code')} msg={resp.get('msg')}"
    _log_event(
        {
            "event": "close_failed",
            "bx_symbol": bx_symbol,
            "qty": qty,
            "error": err,
            "trade_id": trade_id,
        }
    )
    return {"status": "error", "error": err}
