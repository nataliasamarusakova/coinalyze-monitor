"""
unentered_tracker.py — детектор упущенных движений.

Обнаруживает монеты, которые прошли discovery-фильтр Coinalyze и показали
значимый рост, но по которым НЕ была открыта сделка. Записывает кандидата,
ждёт окно финализации (6 часов), затем классифицирует движение по
форвардному исходу и определяет fail_point.

ВАЖНО: «упущенное движение» (unentered) ≠ «MISSED» (exit_reason в close_trade).
MISSED — сделка БЫЛА открыта, монета выпала из discovery.
Unentered — сделка НЕ БЫЛА открыта вообще.

Запуск: python unentered_tracker.py  (после monitor.py в том же прогоне)
"""

import json
import time
from pathlib import Path
from typing import Optional

from conditions import (
    check_confirmed_path_a, check_confirmed_path_b,
    check_early_move, check_accumulation, closest_miss_for_confirmed,
    safe
)

try:
    from monitor import classify_asset_class, SIGNAL_LOGIC_VERSION
except Exception:
    def classify_asset_class(r):
        return "crypto"
    SIGNAL_LOGIC_VERSION = 1

BASE = Path(__file__).resolve().parent
MARKET_HISTORY = BASE / "market_history.jsonl"
WATCHLIST = BASE / "watchlist.json"
TRADES = BASE / "trades.jsonl"
CANDIDATES_FILE = BASE / "unentered_candidates.jsonl"
ANALYSIS_FILE = BASE / "unentered_analysis.jsonl"

CANDIDATES_TTL_DAYS = 7
ANALYSIS_TTL_DAYS = 90

MISSED_THRESHOLD_PCT = 5.0
FINALIZATION_WINDOW_H = 6
FORWARD_HORIZONS = [60, 120]
FORWARD_MAX_LAG_MIN = 15


def now_ts() -> int:
    return int(time.time())


def load_jsonl(path: Path) -> list[dict]:
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


def append_jsonl(path: Path, rec: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def save_jsonl(path: Path, recs: list[dict]):
    with open(path, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def cleanup_jsonl(path: Path, ttl_days: int, ts_field: str = "detect_ts"):
    if not path.exists():
        return
    cutoff = now_ts() - ttl_days * 86400
    recs = load_jsonl(path)
    fresh = [r for r in recs if r.get(ts_field, 0) > cutoff]
    removed = len(recs) - len(fresh)
    if removed:
        save_jsonl(path, fresh)
        print(f"cleanup {path.name}: -{removed}")


def load_market_history_snaps() -> dict[str, list[dict]]:
    recs = load_jsonl(MARKET_HISTORY)
    grouped = {}
    for r in recs:
        sym = r.get("symbol")
        if not sym:
            continue
        if sym not in grouped:
            grouped[sym] = []
        grouped[sym].append(r)
    for sym in grouped:
        grouped[sym].sort(key=lambda x: x.get("ts", 0))
    return grouped


def get_active_symbols() -> set:
    now = now_ts()
    cutoff_24h = now - 24 * 3600
    active = set()
    if WATCHLIST.exists():
        try:
            wl = json.loads(WATCHLIST.read_text(encoding="utf-8"))
            for sym, rec in wl.items():
                if rec.get("open_trade"):
                    active.add(sym)
        except json.JSONDecodeError:
            pass
    for t in load_jsonl(TRADES):
        if t.get("entry_ts") and t["entry_ts"] >= cutoff_24h:
            active.add(t.get("symbol"))
    return active


def compute_forward_returns(snaps: list[dict], detect_ts: int,
                            entry_price: float) -> dict:
    """[FIX M1] Ищем ближайший снимок (abs lag), а не только после target_ts."""
    result = {}
    for h in FORWARD_HORIZONS:
        target_ts = detect_ts + h * 60
        best_price = None
        best_lag = float("inf")
        for s in snaps:
            ts = s.get("ts", 0)
            lag = abs(ts - target_ts)
            if lag < best_lag:
                best_lag = lag
                best_price = s.get("price")
        if best_price is not None and best_lag <= FORWARD_MAX_LAG_MIN * 60 and entry_price:
            result[f"forward_{h}m"] = round((best_price - entry_price) / entry_price * 100, 3)
            result[f"forward_{h}m_available"] = True
        else:
            result[f"forward_{h}m"] = None
            result[f"forward_{h}m_available"] = False
    return result


def classify_quality(forward_returns: dict, movement_snaps: list[dict],
                     detect_ts: int) -> dict:
    f60 = forward_returns.get("forward_60m")
    f120 = forward_returns.get("forward_120m")
    if f60 is None and f120 is None:
        return {"label": "undetermined", "reason": "нет форвардных данных"}
    best_forward = max([v for v in [f60, f120] if v is not None], default=None)
    if best_forward is None:
        return {"label": "undetermined", "reason": "нет форвардных данных"}

    if len(movement_snaps) >= 3:
        oi_start = safe(movement_snaps[0].get("oi_chg24_pct"))
        oi_end = safe(movement_snaps[-1].get("oi_chg24_pct"))
        cvd_start = safe(movement_snaps[0].get("cvd24"))
        cvd_end = safe(movement_snaps[-1].get("cvd24"))
        oi_rising = oi_end > oi_start
        cvd_rising = cvd_end > cvd_start
    else:
        oi_rising = None
        cvd_rising = None

    if best_forward >= 2.0 and oi_rising and cvd_rising:
        return {"label": "good", "reason": "форвардный рост + OI↑ + CVD↑"}
    elif best_forward >= 2.0:
        return {"label": "good", "reason": "форвардный рост (OI/CVD не подтверждены)"}
    elif best_forward < 0:
        return {"label": "noise", "reason": "форвардный исход отрицательный"}
    elif f60 is not None and f60 > 0 and f120 is not None and f120 < f60 - 2:
        return {"label": "late", "reason": "рост затухает (f120 < f60)"}
    else:
        return {"label": "undetermined", "reason": "смешанные сигналы"}


def determine_fail_point(sym: str, movement_snaps: list[dict],
                         lifecycle_state: Optional[str],
                         cvd_momentum: float) -> dict:
    confidence = "observed" if lifecycle_state in ("ACCUMULATION", "EARLY_MOVE") else "estimated"
    cm = closest_miss_for_confirmed(movement_snaps, cvd_momentum)

    if lifecycle_state in ("ACCUMULATION", "EARLY_MOVE"):
        stage = lifecycle_state
        if cm["condition"] is not None:
            return {"stage": stage, "condition": cm["condition"], "path": cm["path"],
                    "deficit": cm["deficit"], "value": cm["value"],
                    "threshold": cm["threshold"], "confidence": confidence}
        return {"stage": stage, "condition": "conditions_met_but_not_confirmed",
                "path": None, "deficit": 0, "value": None, "threshold": None,
                "confidence": confidence}

    result_a = check_confirmed_path_a(movement_snaps)
    result_em = check_early_move(movement_snaps)
    result_acc = check_accumulation(movement_snaps)

    if result_a.get("passed") or result_a.get("insufficient_data"):
        stage_note = "estimated: условия CONFIRMED выполнены или мало данных"
    elif result_em.get("passed"):
        stage_note = "estimated: прошла EARLY_MOVE, не дошла до CONFIRMED"
    elif result_acc.get("passed"):
        stage_note = "estimated: прошла ACCUMULATION, не дошла до EARLY_MOVE"
    else:
        stage_note = "estimated: не прошла даже ACCUMULATION"

    return {"stage": stage_note, "condition": cm["condition"], "path": cm["path"],
            "deficit": cm["deficit"], "value": cm["value"], "threshold": cm["threshold"],
            "confidence": confidence}


def compute_cvd_momentum(snaps: list[dict]) -> float:
    if len(snaps) >= 4:
        return safe(snaps[-1].get("cvd24")) - safe(snaps[-4].get("cvd24"))
    elif len(snaps) >= 2:
        return safe(snaps[-1].get("cvd24")) - safe(snaps[0].get("cvd24"))
    return 0.0


def run():
    print("═══ unentered_tracker ═══")
    now = now_ts()
    market_snaps = load_market_history_snaps()
    active_symbols = get_active_symbols()
    existing_candidates = load_jsonl(CANDIDATES_FILE)
    existing_candidate_syms = {c.get("symbol") for c in existing_candidates
                               if c.get("detect_ts", 0) > now - 24 * 3600}

    # ── Шаг 1: Обнаружение новых кандидатов ──
    new_candidates = 0
    for sym, snaps in market_snaps.items():
        if not snaps or sym in active_symbols or sym in existing_candidate_syms:
            continue
        last = snaps[-1]
        chg = last.get("price_chg24")
        if chg is None or chg <= MISSED_THRESHOLD_PCT:
            continue
        lifecycle_state = last.get("lifecycle_state")
        entry_price = last.get("price")
        cvd_mom = compute_cvd_momentum(snaps)
        candidate = {
            "detect_ts": now,
            "symbol": sym,
            "name": last.get("name", sym),
            "price_chg24_at_detect": chg,
            "price_at_detect": entry_price,
            "lifecycle_state_at_detect": lifecycle_state,
            "cvd_momentum_at_detect": round(cvd_mom, 2),
            "signal_logic_version": SIGNAL_LOGIC_VERSION,
            "status": "pending_finalization",
        }
        append_jsonl(CANDIDATES_FILE, candidate)
        new_candidates += 1

    # ── Шаг 2: Финализация кандидатов ──
    finalized = 0
    remaining = []
    already_finalized = {(r.get("detect_ts"), r.get("symbol"))
                         for r in load_jsonl(ANALYSIS_FILE)}

    for cand in existing_candidates:
        detect_ts = cand.get("detect_ts", 0)
        sym = cand.get("symbol")
        if cand.get("status") == "finalized":
            continue
        if now - detect_ts < FINALIZATION_WINDOW_H * 3600:
            remaining.append(cand)
            continue
        if (detect_ts, sym) in already_finalized:
            continue

        # [FIX S4] try/except для устойчивости
        try:
            snaps = market_snaps.get(sym, [])
            entry_price = cand.get("price_at_detect")
            forward_returns = compute_forward_returns(snaps, detect_ts, entry_price)
            movement_snaps = [s for s in snaps if s.get("ts", 0) >= detect_ts - 4 * 3600]
            quality = classify_quality(forward_returns, movement_snaps, detect_ts)
            cvd_mom = cand.get("cvd_momentum_at_detect", 0)
            lifecycle_state = cand.get("lifecycle_state_at_detect")
            fail_point = determine_fail_point(sym, movement_snaps, lifecycle_state, cvd_mom)

            analysis_rec = {
                "detect_ts": detect_ts,
                "finalize_ts": now,
                "symbol": sym,
                "name": cand.get("name", sym),
                "price_chg24_at_detect": cand.get("price_chg24_at_detect"),
                "price_at_detect": entry_price,
                "lifecycle_state_at_detect": lifecycle_state,
                "cvd_momentum_at_detect": cvd_mom,
                "signal_logic_version": cand.get("signal_logic_version", SIGNAL_LOGIC_VERSION),
                "asset_class": classify_asset_class({"symbol": sym, "name": cand.get("name", "")}),
                "movement_snaps_count": len(movement_snaps),
                **forward_returns,
                "quality": quality,
                "fail_point": fail_point,
            }
            append_jsonl(ANALYSIS_FILE, analysis_rec)
            finalized += 1
        except Exception as e:
            print(f"ERROR: финализация {sym} упала: {e}")
            remaining.append(cand)

    # [FIX C1] Перезаписываем candidates без финализированных
    save_jsonl(CANDIDATES_FILE, remaining)

    # ── Шаг 3: Cleanup ──
    cleanup_jsonl(CANDIDATES_FILE, CANDIDATES_TTL_DAYS, "detect_ts")
    cleanup_jsonl(ANALYSIS_FILE, ANALYSIS_TTL_DAYS, "detect_ts")

    pending_count = len(load_jsonl(CANDIDATES_FILE))
    print(f"  новых кандидатов: {new_candidates}")
    print(f"  финализировано: {finalized}")
    print(f"  ожидают классификации: {pending_count}")
    print("═══ done ═══")


if __name__ == "__main__":
    run()
