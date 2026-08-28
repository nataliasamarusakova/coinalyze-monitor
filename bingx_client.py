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
CLOSE_CLIENT_ORDER_PREFIX = "CM_CLOSE_"
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
    """HTTP helper with retries only for idempotent reads.

    Non-idempotent writes (POST) are executed exactly once per call. If a
    write response is lost, the caller must reconcile using a stable
    clientOrderId / exchange state instead of blindly replaying the write.
    """
    base_params = dict(params or {})
    method_upper = str(method).upper()
    retry_count = max(int(retries), 1) if method_upper in {"GET", "HEAD", "OPTIONS"} else 1
    url = BASE_URL + path
    last_err = "unknown"

    for attempt in range(retry_count):
        request_params = dict(base_params)
        if signed:
            if not API_KEY or not SECRET_KEY:
                return {
                    "code": -1,
                    "msg": "BINGX_API_KEY / BINGX_SECRET_KEY не заданы в env",
                }
            request_params["timestamp"] = str(int(time.time() * 1000))
            request_params["signature"] = _sign(request_params)

        headers = {"X-BX-APIKEY": API_KEY} if signed else {}
        try:
            resp = requests.request(
                method_upper, url, headers=headers, params=request_params, timeout=10
            )
            return resp.json()
        except Exception as e:
            last_err = str(e)
            if attempt + 1 < retry_count:
                time.sleep(1 + attempt)

    return {"code": -1, "msg": f"network error: {last_err}"}


def _lookup_order_by_client_order_id(
    bx_symbol: str, client_order_id: str,
) -> dict:
    """Tri-state lookup: found / not_found / error. Never collapse API error into absence."""
    if not client_order_id or not bx_symbol:
        return {"status": "error", "error": "missing symbol/clientOrderId", "order": None}
    resp = _request(
        "GET",
        "/openApi/swap/v2/trade/allOrders",
        {"symbol": bx_symbol},
    )
    if resp.get("code") != 0:
        return {
            "status": "error",
            "error": f"code={resp.get('code')} msg={resp.get('msg', 'unknown')}",
            "order": None,
        }
    wanted = str(client_order_id).upper()
    for order in _normalize_orders_list(resp):
        if str(order.get("clientOrderId", "")).upper() == wanted:
            return {"status": "found", "order": order}
    return {"status": "not_found", "order": None}


def _lookup_order_by_id(bx_symbol: str, order_id: str) -> dict:
    """Tri-state lookup by exchange orderId."""
    if not bx_symbol or not order_id:
        return {"status": "error", "error": "missing symbol/orderId", "order": None}
    resp = _request(
        "GET",
        "/openApi/swap/v2/trade/allOrders",
        {"symbol": bx_symbol},
    )
    if resp.get("code") != 0:
        return {
            "status": "error",
            "error": f"code={resp.get('code')} msg={resp.get('msg', 'unknown')}",
            "order": None,
        }
    wanted = str(order_id)
    for order in _normalize_orders_list(resp):
        if str(order.get("orderId", "")) == wanted:
            return {"status": "found", "order": order}
    return {"status": "not_found", "order": None}


def _delete_order_and_verify(
    bx_symbol: str,
    order_id: str,
    verify_open_orders,
) -> dict:
    """Cancel one order and verify the exchange no longer lists it as open.

    DELETE responses can be lost or rejected after the exchange has already
    processed the cancellation. Never replay DELETE blindly; verification is
    the source of truth.
    """
    order_id = str(order_id or "")
    if not order_id:
        return {"status": "invalid", "order_id": order_id}

    resp = _request(
        "DELETE",
        ORDER_PATH,
        {"symbol": bx_symbol, "orderId": order_id},
    )

    if resp.get("code") == 0:
        response_status = "acknowledged"
    else:
        response_status = "error"

    verify = verify_open_orders()
    if verify.get("status") != "ok":
        return {
            "status": "unknown",
            "order_id": order_id,
            "response_status": response_status,
            "response_error": resp.get("msg"),
            "verification_error": verify.get("error", "open-order verification failed"),
        }

    remaining = [
        o for o in (verify.get("orders") or [])
        if str(o.get("orderId", "")) == order_id
    ]
    if remaining:
        return {
            "status": "still_open",
            "order_id": order_id,
            "response_status": response_status,
            "response_error": resp.get("msg"),
        }

    # Absence from openOrders is not enough to prove cancellation: the order
    # may have filled between DELETE and the verification request. Query order
    # history and distinguish a terminal cancellation from a fill.
    history_lookup = _lookup_order_by_id(bx_symbol, order_id)
    if history_lookup.get("status") == "error":
        return {
            "status": "unknown",
            "order_id": order_id,
            "response_status": response_status,
            "response_error": resp.get("msg"),
            "verification_error": history_lookup.get("error", "allOrders query failed"),
        }
    history_order = history_lookup.get("order")
    if history_lookup.get("status") == "not_found" or not history_order:
        return {
            "status": "unknown",
            "order_id": order_id,
            "response_status": response_status,
            "response_error": resp.get("msg"),
            "verification_error": "order disappeared from openOrders but was not found in allOrders",
        }

    history_status = str(history_order.get("status", "")).upper()
    if history_status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
        return {
            "status": "cancelled",
            "order_id": order_id,
            "response_status": response_status,
            "history_status": history_status,
        }

    if history_status in {"FILLED", "PARTIALLY_FILLED", "PARTIALLYFILLED"}:
        return {
            "status": "filled",
            "order_id": order_id,
            "response_status": response_status,
            "history_status": history_status,
            "executed_qty": float(history_order.get("executedQty", 0) or 0),
            "avg_price": float(history_order.get("avgPrice", 0) or 0),
        }

    return {
        "status": "unknown",
        "order_id": order_id,
        "response_status": response_status,
        "history_status": history_status,
        "response_error": resp.get("msg"),
        "verification_error": "order disappeared from openOrders in a non-terminal/unknown state",
    }


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
    # Protection orders are recreated after partial/manual closes and trailing
    # updates.  BingX may reject a reused clientOrderId even after the previous
    # order was cancelled, so every NEW TP submission gets a unique revision.
    key = _trade_id_hash(trade_id) if trade_id else uuid.uuid4().hex[:8]
    revision = uuid.uuid4().hex[:8]
    return f"{TP_CLIENT_ORDER_PREFIX}{key}_{leg}_{revision}"


def parse_tp_client_order_id(client_id: str) -> dict | None:
    if not client_id:
        return None
    parts = client_id.upper().split("_")
    valid_legs_upper = {leg.upper() for leg in VALID_TP_LEGS}
    if (
        len(parts) != 5
        or parts[0] != "CM"
        or parts[1] != "TP"
        or not _is_hex8(parts[2])
        or parts[3] not in valid_legs_upper
        or not _is_hex8(parts[4])
    ):
        return None
    return {
        "trade_id": None,
        "trade_hash": parts[2].lower(),
        "leg": parts[3].lower(),
        "revision": parts[4].lower(),
    }


def build_sl_client_order_id(trade_id: str = None) -> str:
    # Same uniqueness rule as TP: each new protection order must have its own
    # clientOrderId because BingX can enforce uniqueness across order history.
    key = _trade_id_hash(trade_id) if trade_id else uuid.uuid4().hex[:8]
    revision = uuid.uuid4().hex[:8]
    return f"{SL_CLIENT_ORDER_PREFIX}{key}_{revision}"


def build_open_client_order_id(symbol: str, trade_id: str = None) -> str:
    raw = f"{symbol}:{trade_id}" if trade_id else symbol
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{OPEN_CLIENT_ORDER_PREFIX}{key}"


def build_close_client_order_id(
    symbol: str, trade_id: str = None, attempt: int = 0
) -> str:
    """Return a deterministic clientOrderId for one MARKET-close attempt.

    The same attempt keeps the same ID across retries/restarts, preserving
    idempotency after a lost response. A later attempt for a position that
    remains open after a previously FILLED close gets a different deterministic
    ID, so the remainder can be closed without replaying the already-filled
    order.
    """
    try:
        attempt = max(0, int(attempt))
    except (TypeError, ValueError):
        attempt = 0
    raw_base = f"{symbol}:{trade_id}" if trade_id else f"ORPHAN:{symbol}"
    raw = f"{raw_base}:close_attempt:{attempt}"
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{CLOSE_CLIENT_ORDER_PREFIX}{key}"


def parse_sl_client_order_id(client_id: str) -> dict | None:
    if not client_id:
        return None
    parts = client_id.upper().split("_")
    if (
        len(parts) != 4
        or parts[0] != "CM"
        or parts[1] != "SL"
        or not _is_hex8(parts[2])
        or not _is_hex8(parts[3])
    ):
        return None
    return {
        "trade_hash": parts[2].lower(),
        "revision": parts[3].lower(),
    }


def _sl_belongs_to_trade(parsed: dict | None, trade_id: str = None) -> bool:
    if parsed is None:
        return False
    if not trade_id:
        return True
    return parsed.get("trade_hash") == _trade_id_hash(trade_id)


# ============================================================
# POSITION QUERIES
# ============================================================
def _position_amt(bx_symbol: str) -> float | None:
    """Return confirmed LONG/BOTH position amount; None means exchange state unknown."""
    resp = _request("GET", POSITION_PATH, {"symbol": bx_symbol})
    if resp.get("code") != 0:
        log.error(
            f"[{bx_symbol}] position amount query failed: "
            f"code={resp.get('code')} msg={resp.get('msg')}"
        )
        return None
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


def position_amt(symbol: str) -> float | None:
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
            except (TypeError, ValueError):
                continue
            if amt <= 0:
                continue

            # Position existence is established by positionAmt alone.
            # avgPrice may be temporarily absent/zero while the exchange
            # is still converging after a market fill. Never report such a
            # live position as NOT_FOUND, because callers use NOT_FOUND as
            # authoritative proof that a position no longer exists.
            try:
                avg_price = float(p.get("avgPrice", 0) or p.get("entryPrice", 0) or 0)
            except (TypeError, ValueError):
                avg_price = 0.0

            entry_price = p.get("entryPrice")
            try:
                entry_price = float(entry_price) if entry_price is not None else None
            except (TypeError, ValueError):
                entry_price = None
            if not entry_price and avg_price > 0:
                entry_price = avg_price

            return {
                "status": "found",
                "symbol": p.get("symbol", bx_symbol),
                "avgPrice": avg_price or None,
                "positionAmt": amt,
                "entryPrice": entry_price,
                "price_ready": avg_price > 0,
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
            if pos.get("price_ready") and pos.get("avgPrice", 0):
                log.info(
                    f"[{symbol}] позиция появилась: avgPrice={pos.get('avgPrice')} qty={pos.get('positionAmt')}"
                )
                return pos
            log.info(
                f"[{symbol}] позиция уже существует, но avgPrice ещё не готов: "
                f"qty={pos.get('positionAmt')}"
            )
        elif pos.get("status") == "error":
            log.warning(f"[{symbol}] ожидание позиции: exchange state unknown: {pos.get('error')}")
        time.sleep(poll_interval)
    log.warning(f"[{symbol}] позиция не появилась с готовой avgPrice за {timeout_sec}с")
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
        executed_qty = float(o.get("executedQty", 0) or 0)
        orig_qty = float(o.get("origQty", 0) or o.get("quantity", 0) or 0)
        filled_tp.append(
            {
                "leg": parsed.get("leg"),
                "trade_id": parsed.get("trade_id"),
                "trade_hash": parsed.get("trade_hash"),
                "order_id": str(o.get("orderId", "")),
                "client_order_id": client_id,
                "status": status,
                "executed_qty": executed_qty,
                "orig_qty": orig_qty,
                "remaining_qty": max(0.0, orig_qty - executed_qty) if orig_qty > 0 else None,
                "is_fully_filled": status == "FILLED" or (orig_qty > 0 and executed_qty >= orig_qty - 1e-12),
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
    orders_by_leg = {}
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
            orders_by_leg.setdefault(leg, []).append(order)
            orig_qty = float(order.get("origQty", 0) or order.get("quantity", 0) or 0)
            executed_qty = float(order.get("executedQty", 0) or 0)
            qty = max(0.0, orig_qty - executed_qty) if orig_qty > 0 else 0.0
            existing_qty_total += qty
    legs_status = {}
    missing = []
    for tp in tp_levels:
        leg = tp.get("leg")
        present = existing_legs.get(leg, False)
        legs_status[leg] = present
        if not present:
            missing.append(leg)
    duplicate_legs = sorted(
        leg for leg, orders in orders_by_leg.items() if len(orders) > 1
    )
    return {
        "legs": legs_status,
        "missing": missing,
        "duplicate_legs": duplicate_legs,
        "all_present": len(missing) == 0 and not duplicate_legs,
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
    allocation_base_qty: float = None,
    completed_legs: set[str] | None = None,
    filled_qty_by_leg: dict[str, float] | None = None,
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
    if existing_check.get("status") == "error":
        # A failed exchange read must never be interpreted as "no TP orders".
        # Creating a new TP set while the existing order state is unknown can
        # create duplicate protection after a transient exchange/API failure.
        return {
            "status": "error",
            "error": (
                "cannot determine existing TP state before creation: "
                f"{existing_check.get('error', 'TP query failed')}"
            ),
            "orders": [],
            "state_unknown": True,
        }
    if existing_check.get("all_present"):
        log.info(f"[{symbol}] все TP legs уже существуют, пропускаем создание")
        return {
            "status": "already_exists",
            "legs": existing_check.get("legs"),
            "orders": [],
            "existing_qty": existing_check.get("existing_qty"),
        }

    completed = {str(x).lower() for x in (completed_legs or set()) if x}
    filled_by_leg = {}
    for key, value in (filled_qty_by_leg or {}).items():
        try:
            filled_by_leg[str(key).lower()] = max(0.0, float(value or 0.0))
        except (TypeError, ValueError):
            continue
    missing_legs = [
        str(leg) for leg in existing_check.get("missing", [])
        if str(leg).lower() not in completed
    ]
    if not missing_legs:
        return {
            "status": "already_exists",
            "legs": existing_check.get("legs"),
            "orders": [],
            "existing_qty": existing_check.get("existing_qty"),
            "completed_legs": sorted(completed),
        }
    c = _contracts().get(bx_symbol)
    if not c:
        return {"status": "error", "error": f"контракт {bx_symbol} не найден"}

    prec = int(c.get("quantityPrecision") or 0)
    price_prec = int(c.get("pricePrecision") or 4)

    # Для первичного OPEN allocation_base_qty == position_qty.
    # При recovery после частичных TP allocation_base_qty должен быть
    # исходным exchange-confirmed qty, а position_qty — текущим остатком.
    # Это позволяет сохранить исходные close_fraction и не перераспределять
    # уже исполненные legs как новые.
    allocation_base_qty = float(allocation_base_qty or position_qty)
    if allocation_base_qty <= 0:
        return {"status": "error", "error": "allocation_base_qty <= 0", "orders": []}

    existing_qty = float(existing_check.get("existing_qty", 0.0) or 0.0)
    available_for_missing = max(0.0, float(position_qty) - existing_qty)
    missing_defs = [
        tp for tp in tp_levels
        if str(tp.get("leg", "")).lower() in {str(x).lower() for x in missing_legs}
    ]
    desired_missing = {}
    for tp in missing_defs:
        leg_name = str(tp.get("leg"))
        target_qty = max(0.0, allocation_base_qty * float(tp.get("close_fraction", 0.0)))
        already_executed = filled_by_leg.get(leg_name.lower(), 0.0)
        desired_missing[leg_name] = max(0.0, target_qty - already_executed)
    desired_total = sum(desired_missing.values())
    if desired_total > available_for_missing + 1e-12 and desired_total > 0:
        scale = available_for_missing / desired_total
        log.warning(
            f"[{symbol}] TP_RECOVERY allocation scaled: "
            f"desired={desired_total:.12f} available={available_for_missing:.12f} scale={scale:.6f}"
        )
        desired_missing = {leg: qty * scale for leg, qty in desired_missing.items()}

    allocated_qtys = {}
    for leg, desired_qty in desired_missing.items():
        # ROUND_DOWN по шагу: quantity не должна превышать реально доступный остаток.
        allocated_qtys[leg] = _round_qty(desired_qty, prec)

    orders_created = []
    orders_to_rollback = []

    def _rollback_created_tp_orders() -> dict:
        failed = []
        filled = []
        unknown = []
        for rollback_oid in orders_to_rollback:
            delete_result = _delete_order_and_verify(
                bx_symbol,
                rollback_oid,
                lambda: get_open_tp_orders(symbol),
            )
            if delete_result.get("status") != "cancelled":
                record = {
                    "order_id": str(rollback_oid),
                    "status": delete_result.get("status"),
                    "error": delete_result.get("response_error") or delete_result.get("verification_error"),
                }
                if delete_result.get("status") == "filled":
                    filled.append(record)
                else:
                    failed.append(record)
                    if delete_result.get("status") == "unknown":
                        unknown.append(record)

        # Never claim rollback succeeded until the exchange confirms that
        # none of the newly-created order ids are still open or filled.
        verify = get_open_tp_orders(symbol)
        if verify.get("status") != "ok":
            return {
                "ok": False,
                "failed_deletes": failed,
                "verification_error": verify.get("error", "TP rollback verification failed"),
            }

        remaining_ids = {
            str(o.get("orderId", ""))
            for o in (verify.get("orders") or [])
            if o.get("orderId")
        }
        still_open = [oid for oid in orders_to_rollback if str(oid) in remaining_ids]
        return {
            "ok": not failed and not filled and not still_open,
            "failed_deletes": failed,
            "filled_during_rollback": filled,
            "unknown_deletes": unknown,
            "still_open": [str(x) for x in still_open],
        }

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
            rollback = _rollback_created_tp_orders()
            return {
                "status": "error",
                "error": err,
                "failed_leg": leg,
                "rolled_back": rollback.get("ok", False),
                "rollback": rollback,
            }

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
            if not oid:
                lookup = _lookup_order_by_client_order_id(bx_symbol, client_order_id)
                if lookup.get("status") == "found":
                    order = lookup.get("order") or {}
                    oid = str(order.get("orderId", ""))
                if not oid:
                    return {
                        "status": "error",
                        "error": "TP creation acknowledged but orderId is missing/unrecoverable",
                        "state_unknown": True,
                        "client_order_id": client_order_id,
                        "orders": orders_created,
                    }
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
            if oid:
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
        elif int(resp.get("code", 0) or 0) == -1:
            # A transport failure after the TP POST can mean the exchange
            # accepted/created the order but the response was lost. Reconcile
            # the exact clientOrderId generated for THIS attempt before
            # creating anything else or rolling back earlier legs.
            recovery = _recover_order_after_write_failure(bx_symbol, client_order_id)
            if recovery.get("status") == "error":
                err = f"code={resp.get('code')} msg={resp.get('msg')} ; recovery lookup failed: {recovery.get('error')}"
                return {
                    "status": "error",
                    "error": err,
                    "state_unknown": True,
                    "client_order_id": client_order_id,
                    "orders": orders_created,
                }
            recovered = recovery.get("order") if recovery.get("status") == "found" else None
            if recovered:
                recovered_status = str(recovered.get("status", "")).upper()
                if recovered_status not in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                    recovered_oid = str(recovered.get("orderId", ""))
                    recovered_qty = float(
                        recovered.get("origQty")
                        or recovered.get("quantity")
                        or tp_qty
                        or 0.0
                    )
                    recovered_price = float(
                        recovered.get("stopPrice")
                        or recovered.get("triggerPrice")
                        or tp_price
                    )
                    recovered_record = {
                        "leg": leg,
                        "order_id": recovered_oid,
                        "client_order_id": client_order_id,
                        "price": recovered_price,
                        "pnl_pct": pnl_pct,
                        "qty": recovered_qty,
                        "trade_id": trade_id,
                        "recovered": True,
                        "exchange_status": recovered_status,
                    }
                    orders_created.append(recovered_record)
                    if recovered_oid:
                        orders_to_rollback.append(recovered_oid)
                    _log_event(
                        {
                            "event": "tp_recovered_by_client_order_id",
                            "symbol": symbol,
                            "bx_symbol": bx_symbol,
                            "leg": leg,
                            "order_id": recovered_oid,
                            "client_order_id": client_order_id,
                            "trade_id": trade_id,
                            "status": recovered_status,
                            "qty": recovered_qty,
                            "tp_price": recovered_price,
                        }
                    )
                    log.warning(
                        f"[{symbol}] {leg} POST response lost; recovered existing order "
                        f"orderId={recovered_oid} status={recovered_status}"
                    )
                    continue
            err = f"code={resp.get('code')} msg={resp.get('msg')}"
            log.error(
                f"[{symbol}] {leg} creation failed: {err}, rolling back {len(orders_to_rollback)} orders"
            )
            rollback = _rollback_created_tp_orders()
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
                    "rollback_ok": rollback.get("ok", False),
                    "rollback_failed_deletes": rollback.get("failed_deletes", []),
                    "rollback_still_open": rollback.get("still_open", []),
                    "rollback_verification_error": rollback.get("verification_error"),
                }
            )
            return {
                "status": "error",
                "error": err,
                "failed_leg": leg,
                "rolled_back": rollback.get("ok", False),
                "rollback": rollback,
            }

    if len(orders_created) != len(missing_legs):
        err = f"создано {len(orders_created)} из {len(missing_legs)} недостающих TP"
        rollback = _rollback_created_tp_orders()
        return {
            "status": "error",
            "error": err,
            "rolled_back": rollback.get("ok", False),
            "rollback": rollback,
        }

    return {
        "status": "created",
        "orders": orders_created,
        "avg_price": avg_price,
        "position_qty": position_qty,
        "missing_legs_created": missing_legs,
        "trade_id": trade_id,
    }


def cancel_take_profit_orders(symbol: str, trade_id: str = None) -> dict:
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
    filled_during_cancel = []
    unknown_during_cancel = []
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
        if trade_id and not _tp_belongs_to_trade(parsed, trade_id):
            continue

        result = _delete_order_and_verify(
            bx_symbol,
            oid,
            lambda: get_open_tp_orders(symbol),
        )
        if result.get("status") == "cancelled":
            cancelled += 1
        elif result.get("status") == "filled":
            filled_during_cancel.append({
                "order_id": str(oid),
                "executed_qty": result.get("executed_qty", 0),
                "avg_price": result.get("avg_price"),
            })
        elif result.get("status") == "unknown":
            unknown_during_cancel.append({
                "order_id": str(oid),
                "error": result.get("verification_error"),
            })
            _log_event(
                {
                    "event": "tp_cancellation_unknown",
                    "symbol": symbol,
                    "bx_symbol": bx_symbol,
                    "order_id": str(oid),
                    "client_order_id": client_oid,
                    "leg": parsed.get("leg", "unknown"),
                    "trade_id": trade_id or parsed.get("trade_id"),
                    "trade_hash": parsed.get("trade_hash"),
                    "type": order.get("type"),
                    "cancel_response": result.get("response_status"),
                    "verification_error": result.get("verification_error"),
                }
            )
            log.warning(f"[{symbol}] TP {parsed.get('leg')} cancellation UNKNOWN orderId={oid}")
        else:
            log.warning(
                f"[{symbol}] отмена TP {oid} не подтверждена: "
                f"status={result.get('status')} error={result.get('response_error') or result.get('verification_error')}"
            )

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
    if trade_id:
        owned_remaining = []
        for order in remaining_orders:
            parsed = parse_tp_client_order_id(str(order.get("clientOrderId", "")))
            if parsed and _tp_belongs_to_trade(parsed, trade_id):
                owned_remaining.append(order)
        remaining_orders = owned_remaining
    remaining_count = len(remaining_orders)
    if filled_during_cancel:
        return {
            "status": "filled_during_cancel",
            "cancelled_count": cancelled,
            "total_found": total_found,
            "remaining_count": remaining_count,
            "filled_orders": filled_during_cancel,
            "unknown_orders": unknown_during_cancel,
            "error": "TP order filled during cancellation attempt",
        }
    if unknown_during_cancel:
        return {
            "status": "unknown",
            "cancelled_count": cancelled,
            "total_found": total_found,
            "remaining_count": remaining_count,
            "unknown_orders": unknown_during_cancel,
            "error": "TP cancellation state could not be conclusively verified",
        }
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
        executed_qty = float(o.get("executedQty", 0) or 0)
        orig_qty = float(o.get("origQty", 0) or o.get("quantity", 0) or 0)
        filled_sl.append(
            {
                "trade_hash": parsed.get("trade_hash"),
                "order_id": str(o.get("orderId", "")),
                "client_order_id": client_id,
                "status": status,
                "executed_qty": executed_qty,
                "orig_qty": orig_qty,
                "remaining_qty": max(0.0, orig_qty - executed_qty) if orig_qty > 0 else None,
                "is_fully_filled": status == "FILLED" or (orig_qty > 0 and executed_qty >= orig_qty - 1e-12),
                "avg_price": float(o.get("avgPrice", 0) or 0),
                "time": order_time,
            }
        )
    return {"status": "ok", "orders": filled_sl, "count": len(filled_sl)}


def _recover_order_after_write_failure(bx_symbol: str, client_order_id: str) -> dict:
    """Resolve a lost POST response with explicit found/not_found/error state."""
    return _lookup_order_by_client_order_id(bx_symbol, client_order_id)


def place_stop_loss_order(
    symbol: str,
    avg_price: float,
    qty: float,
    stop_loss_pct: float,
    trade_id: str = None,
    stop_price_override: float = None,
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

    if stop_price_override is not None:
        try:
            stop_price = float(stop_price_override)
        except (TypeError, ValueError):
            return {"status": "error", "error": f"invalid stop_price_override={stop_price_override}"}
        if stop_price <= 0:
            return {"status": "error", "error": f"invalid stop_price_override={stop_price}"}
    else:
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
        if not oid:
            lookup = _lookup_order_by_client_order_id(bx_symbol, client_order_id)
            if lookup.get("status") == "found":
                order = lookup.get("order") or {}
                oid = str(order.get("orderId", ""))
            if not oid:
                return {
                    "status": "error",
                    "error": "SL creation acknowledged but orderId is missing/unrecoverable",
                    "state_unknown": True,
                    "client_order_id": client_order_id,
                }
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

    if int(resp.get("code", 0) or 0) == -1:
        recovery = _recover_order_after_write_failure(bx_symbol, client_order_id)
        if recovery.get("status") == "error":
            return {
                "status": "error",
                "error": str(recovery.get("error", "order lookup failed")),
                "state_unknown": True,
                "client_order_id": client_order_id,
            }
        recovered = recovery.get("order") if recovery.get("status") == "found" else None
        if recovered:
            recovered_status = str(recovered.get("status", "")).upper()
            recovered_oid = str(recovered.get("orderId", ""))
            if recovered_status not in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                recovered_qty = float(recovered.get("origQty") or recovered.get("quantity") or qty_r or 0.0)
                recovered_stop = float(recovered.get("stopPrice") or recovered.get("triggerPrice") or stop_price)
                _log_event({
                    "event": "sl_recovered_by_client_order_id",
                    "symbol": symbol,
                    "bx_symbol": bx_symbol,
                    "order_id": recovered_oid,
                    "client_order_id": client_order_id,
                    "trade_id": trade_id,
                    "status": recovered_status,
                    "qty": recovered_qty,
                    "stop_price": recovered_stop,
                })
                return {
                    "status": "created",
                    "order_id": recovered_oid,
                    "client_order_id": client_order_id,
                    "stop_price": recovered_stop,
                    "qty": recovered_qty,
                    "recovered": True,
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


def cancel_stop_loss_orders(symbol: str, trade_id: str = None) -> dict:
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
    filled_during_cancel = []
    unknown_during_cancel = []
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
        if trade_id and not _sl_belongs_to_trade(parsed, trade_id):
            continue

        result = _delete_order_and_verify(
            bx_symbol,
            oid,
            lambda: get_open_sl_orders(symbol),
        )
        if result.get("status") == "cancelled":
            cancelled += 1
        elif result.get("status") == "filled":
            filled_during_cancel.append({
                "order_id": str(oid),
                "executed_qty": result.get("executed_qty", 0),
                "avg_price": result.get("avg_price"),
            })
        elif result.get("status") == "unknown":
            unknown_during_cancel.append({
                "order_id": str(oid),
                "error": result.get("verification_error"),
            })
            _log_event(
                {
                    "event": "sl_cancellation_unknown",
                    "symbol": symbol,
                    "bx_symbol": bx_symbol,
                    "order_id": str(oid),
                    "client_order_id": client_oid,
                    "trade_hash": parsed.get("trade_hash"),
                    "trade_id": trade_id,
                    "type": order.get("type"),
                    "cancel_response": result.get("response_status"),
                    "verification_error": result.get("verification_error"),
                }
            )
            log.warning(f"[{symbol}] SL cancellation UNKNOWN orderId={oid}")
        else:
            log.warning(
                f"[{symbol}] отмена SL {oid} не подтверждена: "
                f"status={result.get('status')} error={result.get('response_error') or result.get('verification_error')}"
            )

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
    if trade_id:
        owned_remaining = []
        for order in remaining_orders:
            parsed = parse_sl_client_order_id(str(order.get("clientOrderId", "")))
            if parsed and _sl_belongs_to_trade(parsed, trade_id):
                owned_remaining.append(order)
        remaining_orders = owned_remaining
    remaining_count = len(remaining_orders)
    if filled_during_cancel:
        return {
            "status": "filled_during_cancel",
            "cancelled_count": cancelled,
            "total_found": total_found,
            "remaining_count": remaining_count,
            "filled_orders": filled_during_cancel,
            "unknown_orders": unknown_during_cancel,
            "error": "SL order filled during cancellation attempt",
        }
    if unknown_during_cancel:
        return {
            "status": "unknown",
            "cancelled_count": cancelled,
            "total_found": total_found,
            "remaining_count": remaining_count,
            "unknown_orders": unknown_during_cancel,
            "error": "SL cancellation state could not be conclusively verified",
        }
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

    cancel_res = cancel_stop_loss_orders(symbol, trade_id=trade_id)
    cancel_status = cancel_res.get("status")
    # A new SL may only be created after the previous SL cancellation is
    # conclusively confirmed.  `unknown` and `filled_during_cancel` are not
    # safe states: in both cases the old protection may still exist or may
    # already have consumed part of the position.
    if cancel_status not in ("cancelled", "no_orders"):
        err = (
            f"не удалось безопасно отменить старый SL перед обновлением: "
            f"status={cancel_status} error={cancel_res.get('error')}"
        )
        log.error(f"[{symbol}] {err}")
        _log_event(
            {
                "event": "sl_update_blocked_cancel_failed",
                "symbol": symbol,
                "bx_symbol": bx_symbol,
                "trade_id": trade_id,
                "cancel_status": cancel_status,
                "error": cancel_res.get("error"),
            }
        )
        return {
            "status": "blocked",
            "error": err,
            "cancel_result": cancel_res,
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
        if not oid:
            lookup = _lookup_order_by_client_order_id(bx_symbol, client_order_id)
            if lookup.get("status") == "found":
                order = lookup.get("order") or {}
                oid = str(order.get("orderId", ""))
            if not oid:
                return {
                    "status": "error",
                    "error": "trailing SL creation acknowledged but orderId is missing/unrecoverable",
                    "state_unknown": True,
                    "client_order_id": client_order_id,
                }
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

    if int(resp.get("code", 0) or 0) == -1:
        recovery = _recover_order_after_write_failure(bx_symbol, client_order_id)
        if recovery.get("status") == "error":
            return {
                "status": "error",
                "error": str(recovery.get("error", "order lookup failed")),
                "state_unknown": True,
                "client_order_id": client_order_id,
            }
        recovered = recovery.get("order") if recovery.get("status") == "found" else None
        if recovered:
            recovered_status = str(recovered.get("status", "")).upper()
            recovered_oid = str(recovered.get("orderId", ""))
            if recovered_status not in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                recovered_qty = float(recovered.get("origQty") or recovered.get("quantity") or qty_r or 0.0)
                recovered_stop = float(recovered.get("stopPrice") or recovered.get("triggerPrice") or new_stop_price)
                _log_event({
                    "event": "sl_trailing_recovered_by_client_order_id",
                    "symbol": symbol,
                    "bx_symbol": bx_symbol,
                    "order_id": recovered_oid,
                    "client_order_id": client_order_id,
                    "trade_id": trade_id,
                    "status": recovered_status,
                    "qty": recovered_qty,
                    "stop_price": recovered_stop,
                })
                return {
                    "status": "created",
                    "order_id": recovered_oid,
                    "client_order_id": client_order_id,
                    "stop_price": recovered_stop,
                    "qty": recovered_qty,
                    "recovered": True,
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
    symbol: str,
    trade_id: str,
    opened_ts: int,
    processed_fills: dict,
    processed_fill_meta: dict | None = None,
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

        # BingX allOrders exposes a cumulative executedQty and cumulative
        # avgPrice for an order. For a later partial fill, cumulative avgPrice
        # is NOT the price of the new delta. Recover the incremental execution
        # price from the previous cumulative quantity/average whenever both
        # are available:
        #   p_delta = (p_now*q_now - p_prev*q_prev) / (q_now-q_prev)
        # This keeps TP PnL correct across multiple partial fills of one leg.
        delta_avg_price = avg_price
        previous_meta = (processed_fill_meta or {}).get(order_id) or {}
        try:
            prev_meta_qty = float(previous_meta.get("executed_qty_total", 0.0) or 0.0)
            prev_meta_avg = float(previous_meta.get("avg_price"))
            if (
                avg_price is not None
                and prev_meta_qty > 0
                and prev_meta_avg > 0
                and executed_qty > prev_meta_qty
            ):
                delta_avg_price = (
                    avg_price * executed_qty - prev_meta_avg * prev_meta_qty
                ) / delta_qty
                if delta_avg_price <= 0:
                    delta_avg_price = avg_price
        except (TypeError, ValueError, ZeroDivisionError):
            delta_avg_price = avg_price

        fills.append(
            {
                "order_id": order_id,
                "leg": leg,
                "client_order_id": filled.get("client_order_id"),
                "status": str(filled.get("status", "")).upper(),
                "executed_qty_delta": delta_qty,
                "executed_qty_total": executed_qty,
                "avg_price": avg_price,
                "delta_avg_price": delta_avg_price,
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

    # A partially-filled STOP order only proves that part of the position was
    # closed by the stop. It must not be treated as the cause of a fully
    # missing position; the remaining quantity may have been closed manually
    # or by another mechanism.
    fully_filled = [
        o for o in orders
        if bool(o.get("is_fully_filled"))
    ]
    if not fully_filled:
        return {"status": "ok", "order": None}

    last = sorted(fully_filled, key=lambda o: int(o.get("time", 0) or 0))[-1]
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

    # Do not treat an API failure as "no position". Opening a new market
    # position while the current exchange position is unknown can create an
    # unintended duplicate/excess position.
    pos_check = get_position(bx_symbol)
    if pos_check.get("status") == "error":
        err = str(pos_check.get("error", "get_position failed"))[:500]
        _log_event(
            {
                "event": "open_blocked_position_check_failed",
                "symbol": symbol,
                "bx_symbol": bx_symbol,
                "trade_id": trade_id,
                "error": err,
            }
        )
        log.error(f"[{symbol}] OPEN blocked: cannot verify existing position: {err}")
        return {
            "status": "error",
            "error": f"cannot verify existing position: {err}",
            "symbol": bx_symbol,
            "trade_id": trade_id,
        }

    if pos_check.get("status") == "found":
        try:
            amt = float(pos_check.get("positionAmt", 0) or 0)
        except (TypeError, ValueError):
            amt = 0.0
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

    if pos_check.get("status") != "not_found":
        err = f"unexpected position check status={pos_check.get('status')}"
        log.error(f"[{symbol}] OPEN blocked: {err}")
        return {
            "status": "error",
            "error": err,
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

    leverage_ok = _set_leverage(bx_symbol, leverage)
    if not leverage_ok:
        err = f"не удалось установить плечо {leverage}x для {bx_symbol}"
        _log_event(
            {
                "event": "open_blocked_leverage_failed",
                "symbol": symbol,
                "bx_symbol": bx_symbol,
                "leverage": leverage,
                "trade_id": trade_id,
                "error": err,
            }
        )
        log.error(f"[{symbol}] OPEN blocked: {err}")
        return {
            "status": "error",
            "error": err,
            "symbol": bx_symbol,
            "leverage": leverage,
            "trade_id": trade_id,
        }

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
        # IMPORTANT: a network/transport failure after POST can mean the
        # exchange executed the order but the response never reached us.
        # Never replay the MARKET POST. Reconcile the stable clientOrderId
        # first, then fall back to the existing position check in open_position().
        if int(resp.get("code", 0) or 0) == -1:
            recovery = _lookup_order_by_client_order_id(bx_symbol, client_order_id)
            if recovery.get("status") == "error":
                return {
                    "status": "error",
                    "error": f"open response lost and order lookup failed: {recovery.get('error')}",
                    "state_unknown": True,
                    "symbol": bx_symbol,
                    "trade_id": trade_id,
                }
            recovered = recovery.get("order") if recovery.get("status") == "found" else None
            if recovered:
                oid = str(recovered.get("orderId", ""))
                recovered_qty = float(recovered.get("executedQty") or recovered.get("origQty") or qty or 0.0)
                recovered_status = str(recovered.get("status", "")).upper()
                if oid and recovered_status not in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                    _log_event(
                        {
                            "event": "open_recovered_by_client_order_id",
                            "symbol": symbol,
                            "bx_symbol": bx_symbol,
                            "order_id": oid,
                            "client_order_id": client_order_id,
                            "exchange_status": recovered_status,
                            "qty": recovered_qty,
                            "trade_id": trade_id,
                        }
                    )
                    return {
                        "status": "opened",
                        "order_id": oid,
                        "client_order_id": client_order_id,
                        "qty": recovered_qty or qty,
                        "symbol": bx_symbol,
                        "leverage": leverage,
                        "margin_usdt": MARGIN_USDT,
                        "trade_id": trade_id,
                        "recovered": True,
                    }
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
    if not oid:
        lookup = _lookup_order_by_client_order_id(bx_symbol, client_order_id)
        if lookup.get("status") == "error":
            return {
                "status": "error",
                "error": f"OPEN acknowledged but orderId missing and lookup failed: {lookup.get('error')}",
                "state_unknown": True,
                "symbol": bx_symbol,
                "trade_id": trade_id,
            }
        recovered = lookup.get("order") if lookup.get("status") == "found" else None
        if recovered:
            oid = str(recovered.get("orderId", ""))
        if not oid:
            return {
                "status": "error",
                "error": "OPEN acknowledged but orderId is missing/unrecoverable",
                "state_unknown": True,
                "symbol": bx_symbol,
                "trade_id": trade_id,
            }
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
            confirmed_qty = float(pos_check.get("positionAmt"))
            confirmed_avg_price = pos_check.get("avgPrice")
            confirmed_open = dict(open_res)
            # Exchange independently confirmed a live LONG position.
            # Normalize the nested execution status so downstream close/TP
            # reconciliation cannot mistake it for a failed opening.
            confirmed_open["status"] = "opened"
            confirmed_open["symbol"] = bx_symbol
            confirmed_open["qty"] = confirmed_qty
            if confirmed_avg_price is not None:
                confirmed_open["avg_price"] = confirmed_avg_price

            # A live position can become visible before the exchange exposes
            # avgPrice. Treat that exactly like the timeout/recovery path:
            # preserve the real position, but do not enter the `found` branch
            # in monitor.py, because that branch immediately calculates TP/SL
            # from avg_price. The next protection reconciliation will retry
            # once a usable entry price is available.
            if not pos_check.get("price_ready") or not confirmed_avg_price:
                return {
                    "status": "open_no_tp",
                    "symbol": bx_symbol,
                    "asset_class": asset_class,
                    "open": confirmed_open,
                    "position": pos_check,
                    "qty_initial": confirmed_qty,
                    "qty_remaining": confirmed_qty,
                    "qty_initial_uncertain": False,
                    "price_not_ready": True,
                }

            return {
                "status": "found",
                "symbol": bx_symbol,
                "asset_class": asset_class,
                "open": confirmed_open,
                "position": pos_check,
                "avg_price": confirmed_avg_price,
                "qty_initial": confirmed_qty,
                "qty_remaining": confirmed_qty,
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
            confirmed_avg_price = final_pos.get("avgPrice")
            log.info(
                f"[{symbol}] позиция подтверждена после timeout: "
                f"positionAmt={confirmed_qty} avgPrice={confirmed_avg_price}"
            )
            confirmed_open = dict(open_res)
            confirmed_open["status"] = "opened"
            confirmed_open["symbol"] = bx_symbol
            confirmed_open["qty"] = confirmed_qty
            if confirmed_avg_price is not None:
                confirmed_open["avg_price"] = confirmed_avg_price

            # If the exchange confirms a live position but its average price
            # is not ready yet, keep the position recoverable without inventing
            # a price for TP/SL. The next protection reconciliation will retry
            # once the exchange exposes avgPrice.
            if not final_pos.get("price_ready") or not confirmed_avg_price:
                return {
                    "status": "open_no_tp",
                    "symbol": bx_symbol,
                    "asset_class": asset_class,
                    "open": confirmed_open,
                    "position": final_pos,
                    "qty_initial": confirmed_qty,
                    "qty_remaining": confirmed_qty,
                    "qty_initial_uncertain": False,
                    "price_not_ready": True,
                }

            return {
                "status": "found",
                "symbol": bx_symbol,
                "asset_class": asset_class,
                "open": confirmed_open,
                "position": final_pos,
                "avg_price": confirmed_avg_price,
                "qty_initial": confirmed_qty,
                "qty_remaining": confirmed_qty,
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
    close_attempt: int = 0,
) -> dict:
    if not qty or float(qty) <= 0:
        return {
            "status": "error",
            "error": "qty <= 0",
        }
    if cancel_tp:
        cancel_result = cancel_take_profit_orders(symbol, trade_id=trade_id)
        cancel_sl_result = cancel_stop_loss_orders(symbol, trade_id=trade_id)
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
        close_attempt=close_attempt,
    )


def _close_position(
    bx_symbol: str,
    qty: float,
    client_order_id: str = None,
    trade_id: str = None,
    is_full_close: bool = False,
    close_attempt: int = 0,
) -> dict:
    pos = get_position(bx_symbol)
    if pos.get("status") == "error":
        err = str(pos.get("error", "get_position failed"))[:500]
        _log_event({
            "event": "close_position_check_failed",
            "bx_symbol": bx_symbol,
            "qty_requested": float(qty),
            "trade_id": trade_id,
            "error": err,
        })
        return {
            "status": "error",
            "error": f"cannot verify position before close: {err}",
        }
    if pos.get("status") == "not_found":
        return {
            "status": "already_closed",
            "error": f"нет LONG позиции для {bx_symbol}",
        }
    if pos.get("status") != "found":
        return {
            "status": "error",
            "error": f"unexpected get_position status={pos.get('status')}",
        }

    try:
        real_amt = float(pos.get("positionAmt", 0) or 0)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "error": f"invalid exchange positionAmt={pos.get('positionAmt')!r}",
        }
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
    if client_order_id is None:
        client_order_id = build_close_client_order_id(
            bx_symbol, trade_id, attempt=close_attempt
        )
    if client_order_id:
        params["clientOrderId"] = client_order_id

    # Never submit a second close order while a previous close with the same
    # stable clientOrderId is still known to the exchange. This handles a
    # restart after a lost response without duplicating a MARKET SELL.
    if client_order_id:
        existing_lookup = _lookup_order_by_client_order_id(bx_symbol, client_order_id)
        if existing_lookup.get("status") == "error":
            return {
                "status": "close_pending",
                "symbol": bx_symbol,
                "client_order_id": client_order_id,
                "recovery_check": "order_lookup_failed",
                "error": str(existing_lookup.get("error", "order lookup failed"))[:500],
            }
        existing = existing_lookup.get("order") if existing_lookup.get("status") == "found" else None
        if existing:
            existing_status = str(existing.get("status", "")).upper()
            existing_oid = str(existing.get("orderId", ""))
            existing_exec_qty = float(
                existing.get("executedQty") or existing.get("origQty") or 0.0
            )
            if existing_status == "FILLED":
                remaining_check = get_position(bx_symbol)
                if remaining_check.get("status") == "error":
                    return {
                        "status": "close_pending",
                        "order_id": existing_oid,
                        "qty": existing_exec_qty,
                        "symbol": bx_symbol,
                        "client_order_id": client_order_id,
                        "recovery_check": "position_status_unknown",
                        "error": str(remaining_check.get("error", "position check failed"))[:500],
                    }
                if remaining_check.get("status") == "not_found":
                    return {
                        "status": "closed",
                        "order_id": existing_oid,
                        "qty": existing_exec_qty or qty,
                        "symbol": bx_symbol,
                        "avg_price": float(existing.get("avgPrice") or existing.get("price") or 0.0) or None,
                        "recovered": True,
                    }
                if remaining_check.get("status") == "found":
                    try:
                        remaining_qty = float(remaining_check.get("positionAmt", 0) or 0)
                    except (TypeError, ValueError):
                        remaining_qty = 0.0
                    # A FILLED close order may have reduced the position only
                    # partially. Its clientOrderId must never be replayed.
                    # Wait for the caller/reconciliation to decide whether a
                    # new close with a new id is required for the remainder.
                    return {
                        "status": "close_pending",
                        "order_id": existing_oid,
                        "qty": existing_exec_qty,
                        "remaining_qty": remaining_qty,
                        "symbol": bx_symbol,
                        "client_order_id": client_order_id,
                        "recovered": True,
                        "recovery_check": "position_still_open_after_filled_close",
                    }
                return {
                    "status": "close_pending",
                    "order_id": existing_oid,
                    "qty": existing_exec_qty,
                    "symbol": bx_symbol,
                    "client_order_id": client_order_id,
                    "recovered": True,
                    "recovery_check": f"unexpected_position_status:{remaining_check.get('status')}",
                }
            elif existing_status in {"NEW", "PARTIALLY_FILLED", "PARTIALLYFILLED"}:
                return {
                    "status": "close_pending",
                    "order_id": existing_oid,
                    "qty": existing_exec_qty,
                    "symbol": bx_symbol,
                    "client_order_id": client_order_id,
                }
            elif existing_status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                return {
                    "status": "close_retryable",
                    "order_id": existing_oid,
                    "qty": existing_exec_qty,
                    "symbol": bx_symbol,
                    "client_order_id": client_order_id,
                    "previous_order_status": existing_status,
                    "next_close_attempt": close_attempt + 1,
                }

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

        # A successful POST response only proves that BingX accepted the
        # MARKET order. It does not prove that the position has already
        # disappeared from the account. Confirm the resulting position state
        # before marking the trade locally as closed.
        remaining_check = get_position(bx_symbol)
        if remaining_check.get("status") == "error":
            _log_event({
                "event": "close_accepted_position_unknown",
                "bx_symbol": bx_symbol,
                "order_id": oid,
                "qty": qty,
                "trade_id": trade_id,
                "error": remaining_check.get("error"),
            })
            return {
                "status": "close_pending",
                "order_id": oid,
                "qty": qty,
                "symbol": bx_symbol,
                "avg_price": avg_p,
                "recovery_check": "position_status_unknown",
                "error": str(remaining_check.get("error", "position check failed"))[:500],
            }

        if remaining_check.get("status") == "not_found":
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

        if remaining_check.get("status") == "found":
            try:
                remaining_qty = float(remaining_check.get("positionAmt", 0) or 0)
            except (TypeError, ValueError):
                remaining_qty = 0.0
            return {
                "status": "close_pending",
                "order_id": oid,
                "qty": qty,
                "remaining_qty": remaining_qty,
                "symbol": bx_symbol,
                "avg_price": avg_p,
            }

        return {
            "status": "close_pending",
            "order_id": oid,
            "qty": qty,
            "symbol": bx_symbol,
            "avg_price": avg_p,
            "recovery_check": f"unexpected_position_status:{remaining_check.get('status')}",
        }

    # Transport failure after MARKET SELL: the order may have executed.
    # Reconcile by the stable clientOrderId before treating the close as failed.
    if int(resp.get("code", 0) or 0) == -1 and client_order_id:
        recovery = _lookup_order_by_client_order_id(bx_symbol, client_order_id)
        if recovery.get("status") == "error":
            return {
                "status": "close_pending",
                "symbol": bx_symbol,
                "client_order_id": client_order_id,
                "recovered": False,
                "recovery_check": "order_lookup_failed",
                "error": str(recovery.get("error", "order lookup failed"))[:500],
            }
        recovered = recovery.get("order") if recovery.get("status") == "found" else None
        if recovered:
            recovered_status = str(recovered.get("status", "")).upper()
            recovered_oid = str(recovered.get("orderId", ""))
            recovered_qty = float(
                recovered.get("executedQty") or recovered.get("origQty") or qty or 0.0
            )
            if recovered_status == "FILLED":
                remaining_check = get_position(bx_symbol)
                if remaining_check.get("status") == "error":
                    return {
                        "status": "close_pending",
                        "order_id": recovered_oid,
                        "qty": recovered_qty,
                        "symbol": bx_symbol,
                        "client_order_id": client_order_id,
                        "recovered": True,
                        "recovery_check": "position_status_unknown",
                        "error": str(remaining_check.get("error", "position check failed"))[:500],
                    }
                if remaining_check.get("status") == "not_found":
                    _log_event({
                        "event": "close_recovered_by_client_order_id",
                        "bx_symbol": bx_symbol,
                        "order_id": recovered_oid,
                        "client_order_id": client_order_id,
                        "qty": recovered_qty,
                        "trade_id": trade_id,
                    })
                    return {
                        "status": "closed",
                        "order_id": recovered_oid,
                        "qty": recovered_qty,
                        "symbol": bx_symbol,
                        "avg_price": float(recovered.get("avgPrice") or recovered.get("price") or 0.0) or None,
                        "recovered": True,
                    }
            elif recovered_status in {"NEW", "PARTIALLY_FILLED", "PARTIALLYFILLED"}:
                return {
                    "status": "close_pending",
                    "order_id": recovered_oid,
                    "qty": recovered_qty,
                    "symbol": bx_symbol,
                    "client_order_id": client_order_id,
                    "recovered": True,
                }
            elif recovered_status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                return {
                    "status": "close_retryable",
                    "order_id": recovered_oid,
                    "qty": recovered_qty,
                    "symbol": bx_symbol,
                    "client_order_id": client_order_id,
                    "recovered": True,
                    "previous_order_status": recovered_status,
                    "next_close_attempt": close_attempt + 1,
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
