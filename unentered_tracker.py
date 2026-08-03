"""unentered_tracker.py — отслеживание упущенных движений (unentered).

Терминология (не путать с exit_reason="MISSED" в monitor.py!):
  - exit_reason="MISSED" — сделка БЫЛА открыта, монета выпала из discovery.
  - unentered — сделка НЕ БЫЛА открыта вообще. Это другой концепт.

Двухфазная схема:
  1. Детект: монета с price_chg24 > порога, без сделки → unentered_candidates.jsonl.
  2. Финализация (через FINALIZE_DELAY_H): классификация по форвардному исходу
     → unentered_analysis.jsonl.

Запускается каждый прогон после monitor.py.
"""
import json
import time
from pathlib import Path
from bisect import bisect_left

from conditions import (check_confirmed, check_early_move, check_accumulation,
                         MIN_SNAPS_LIFECYCLE)

try:
    from monitor import SIGNAL_LOGIC_VERSION
except Exception:
    SIGNAL_LOGIC_VERSION = 0

BASE = Path(__file__).resolve().parent
MARKET_HISTORY = BASE / "market_history.jsonl"
TRADES = BASE / "trades.jsonl"
WATCHLIST = BASE / "watchlist.json"
CANDIDATES_FILE = BASE / "unentered_candidates.jsonl"
ANALYSIS_FILE = BASE / "unentered_analysis.jsonl"

MISSED_THRESHOLD = 5.0        # % рост за 24ч для "значимого движения"
FINALIZE_DELAY_H = 6          # часов ожидания перед финализацией
FORWARD_HORIZONS = [60, 120]  # минуты для форвардного исхода
CANDIDATES_TTL_DAYS = 7
ANALYSIS_TTL_DAYS = 90
GOOD_RETURN_THRESHOLD = 2.0   # % форвардный рост для "good"


def load_jsonl(path):
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def append_jsonl(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def rewrite_jsonl(path, recs):
    with open(path, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def cleanup_jsonl(path, ttl_days):
    if not path.exists():
        return
    cutoff = time.time() - ttl_days * 86400
    recs = load_jsonl(path)
    fresh = [r for r in recs if r.get("ts", r.get("detect_ts", 0)) > cutoff]
    if len(fresh) != len(recs):
        rewrite_jsonl(path, fresh)


def load_market_history_grouped():
    """Все снимки из market_history, сгруппированные по symbol, отсортированные по ts."""
    all_snaps = {}
    for r in load_jsonl(MARKET_HISTORY):
        sym = r.get("symbol")
        if not sym:
            continue
        all_snaps.setdefault(sym, []).append(r)
    for sym in all_snaps:
        all_snaps[sym].sort(key=lambda x: x.get("ts", 0))
    return all_snaps


def load_active_symbols():
    """Символы с открытой сделкой (watchlist) или закрытой за 24ч (trades)."""
    active = set()
    if WATCHLIST.exists():
        try:
            wl = json.loads(WATCHLIST.read_text(encoding="utf-8"))
            active.update(wl.keys())
        except json.JSONDecodeError:
            pass
    cutoff = time.time() - 24 * 3600
    for t in load_jsonl(TRADES):
        if t.get("entry_ts") and t["entry_ts"] >= cutoff:
            active.add(t["symbol"])
    return active


def get_watchlist_state(sym):
    """Текущая стадия монеты из watchlist.json (или None)."""
    if not WATCHLIST.exists():
        return None
    try:
        wl = json.loads(WATCHLIST.read_text(encoding="utf-8"))
        return wl.get(sym, {}).get("state")
    except json.JSONDecodeError:
        return None


def compute_cvd_momentum(snaps):
    if len(snaps) >= 4:
        return (snaps[-1].get("cvd24") or 0) - (snaps[-4].get("cvd24") or 0)
    elif len(snaps) >= 2:
        return (snaps[-1].get("cvd24") or 0) - (snaps[0].get("cvd24") or 0)
    return 0


def compute_trend(vals):
    clean = [v for v in vals if v is not None]
    if len(clean) < 2:
        return "flat"
    diff = clean[-1] - clean[0]
    base = abs(clean[0]) if abs(clean[0]) > 1 else 10.0
    if diff > base * 0.05:
        return "up"
    if diff < -base * 0.05:
        return "down"
    return "flat"


def compute_derived_lite(snaps):
    """Упрощённый derived для conditions.check_confirmed."""
    return {
        "cvd_momentum": compute_cvd_momentum(snaps),
        "oi_trend": compute_trend([s.get("oi_chg24_pct") for s in snaps]),
        "cvd_trend": compute_trend([s.get("cvd24") for s in snaps]),
        "price_trend": compute_trend([s.get("price_chg24") for s in snaps]),
    }


def find_price_at(snaps, target_ts, max_lag_sec=900):
    """Цена ближайшего снимка на/после target_ts (лаг ≤ max_lag_sec)."""
    if not snaps:
        return None
    ts_list = [s.get("ts", 0) for s in snaps]
    i = bisect_left(ts_list, target_ts)
    if i >= len(snaps):
        return None
    s = snaps[i]
    if s.get("ts", 0) - target_ts > max_lag_sec:
        return None
    return s.get("price")


def detect_fail_point(snaps, watchlist_state):
    """Определяет fail_point для упущенной монеты.
    confidence: 'observed' если монета сейчас в watchlist (ACCUMULATION/EARLY_MOVE),
                'estimated' иначе."""
    if len(snaps) < MIN_SNAPS_LIFECYCLE:
        return {
            "stage": "insufficient_data",
            "failing_conditions": [f"мало снимков ({len(snaps)})"],
            "closest_miss": None,
            "confidence": "estimated",
        }

    recent = snaps[-MIN_SNAPS_LIFECYCLE:]
    derived = compute_derived_lite(snaps)

    # Проверяем CONFIRMED
    passed, path, failing, closest_miss = check_confirmed(recent, derived)
    if passed:
        # Условия CONFIRMED выполнены сейчас, но сделки нет — редкий случай
        return {
            "stage": "CONFIRMED_conditions_met",
            "failing_conditions": [],
            "closest_miss": None,
            "confidence": "observed" if watchlist_state in ("ACCUMULATION", "EARLY_MOVE") else "estimated",
        }

    # Определяем, дошла ли монета до EARLY_MOVE / ACCUMULATION
    last3 = snaps[-3:]
    em_passed, em_reasons = check_early_move(last3)
    ac_passed, ac_reasons = check_accumulation(last3)

    if watchlist_state in ("ACCUMULATION", "EARLY_MOVE"):
        # Монета отслеживается, точный fail_point
        return {
            "stage": f"stopped_at_{watchlist_state}",
            "failing_conditions": failing,
            "closest_miss": closest_miss,
            "confidence": "observed",
        }
    elif watchlist_state is None:
        # Не отслеживалась — грубая оценка
        if not em_passed and not ac_passed:
            stage = "never_tracked"
            sub_reasons = em_reasons + ac_reasons
        else:
            stage = "estimated_early"
            sub_reasons = failing
        return {
            "stage": stage,
            "failing_conditions": failing if failing else sub_reasons,
            "closest_miss": closest_miss,
            "confidence": "estimated",
        }
    else:
        return {
            "stage": f"state_{watchlist_state}",
            "failing_conditions": failing,
            "closest_miss": closest_miss,
            "confidence": "estimated",
        }


def classify_quality(snaps, forward_returns, detect_price_chg24):
    """Классификация качества движения по форвардному исходу (постфактум).
    label: good / noise / late / undetermined."""
    r60 = forward_returns.get("return_60m")
    r120 = forward_returns.get("return_120m")
    fwd = r120 if r120 is not None else r60
    if fwd is None:
        return "undetermined"

    derived = compute_derived_lite(snaps)
    oi_up = derived["oi_trend"] == "up"
    cvd_up = derived["cvd_trend"] == "up"

    # Хороший лонг: форвардный рост + поддержка OI/CVD
    if fwd >= GOOD_RETURN_THRESHOLD and oi_up and cvd_up:
        return "good"
    # Поздний: был рост на детекте, но форвардный исход слабый/отрицательный
    if detect_price_chg24 and detect_price_chg24 > MISSED_THRESHOLD and fwd < 0:
        return "late"
    # Шум: рост без поддержки OI, или форвардный исход отрицательный
    if fwd < 0 or not oi_up:
        return "noise"
    return "undetermined"


def run():
    now = time.time()
    market_data = load_market_history_grouped()
    active_symbols = load_active_symbols()

    # Загрузка существующих кандидатов
    candidates = load_jsonl(CANDIDATES_FILE)
    candidates_by_sym = {c["symbol"]: c for c in candidates}

    # ── Обнаружение новых кандидатов ──
    new_count = 0
    for sym, snaps in market_data.items():
        if not snaps:
            continue
        last = snaps[-1]
        chg = last.get("price_chg24")
        if chg is None or chg <= MISSED_THRESHOLD:
            continue
        if sym in active_symbols:
            continue
        if sym in candidates_by_sym:
            continue
        cand = {
            "detect_ts": int(now),
            "symbol": sym,
            "name": last.get("name", sym),
            "detect_price_chg24": chg,
            "detect_price": last.get("price"),
            "signal_logic_version": SIGNAL_LOGIC_VERSION,
        }
        append_jsonl(CANDIDATES_FILE, cand)
        candidates_by_sym[sym] = cand
        new_count += 1

    # ── Финализация кандидатов (прошло >= FINALIZE_DELAY_H) ──
    finalized_syms = set()
    for sym, cand in candidates_by_sym.items():
        age_h = (now - cand["detect_ts"]) / 3600
        if age_h < FINALIZE_DELAY_H:
            continue
        snaps = market_data.get(sym, [])
        # Форвардные исходы от detect_ts
        forward_returns = {}
        for h in FORWARD_HORIZONS:
            target_ts = cand["detect_ts"] + h * 60
            fwd_price = find_price_at(snaps, target_ts)
            if fwd_price is not None and cand.get("detect_price"):
                forward_returns[f"return_{h}m"] = round(
                    (fwd_price - cand["detect_price"]) / cand["detect_price"] * 100, 2)
            else:
                forward_returns[f"return_{h}m"] = None

        watchlist_state = get_watchlist_state(sym)
        fail_point = detect_fail_point(snaps, watchlist_state)
        label = classify_quality(snaps, forward_returns, cand.get("detect_price_chg24"))

        rec = {
            "ts": int(now),
            "detect_ts": cand["detect_ts"],
            "finalize_ts": int(now),
            "symbol": sym,
            "name": cand.get("name", sym),
            "detect_price_chg24": cand.get("detect_price_chg24"),
            "peak_price_chg24": max((s.get("price_chg24") or 0) for s in snaps) if snaps else None,
            "fail_point": fail_point,
            "quality_label": label,
            "forward_returns": forward_returns,
            "signal_logic_version": cand.get("signal_logic_version"),
        }
        append_jsonl(ANALYSIS_FILE, rec)
        finalized_syms.add(sym)

    # Удалить финализированных из candidates
    if finalized_syms:
        remaining = [c for c in candidates if c["symbol"] not in finalized_syms]
        rewrite_jsonl(CANDIDATES_FILE, remaining)

    # Cleanup
    cleanup_jsonl(CANDIDATES_FILE, CANDIDATES_TTL_DAYS)
    cleanup_jsonl(ANALYSIS_FILE, ANALYSIS_TTL_DAYS)

    print(f"Unentered tracker: {new_count} новых кандидатов, "
          f"{len(finalized_syms)} финализировано, "
          f"{len(candidates_by_sym) - len(finalized_syms)} ожидают")


if __name__ == "__main__":
    run()
