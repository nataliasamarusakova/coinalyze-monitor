```python
"""unentered_tracker.py — детектор упущенных движений."""
import json, time
from bisect import bisect_left
from pathlib import Path
from typing import Optional
from conditions import (
    check_confirmed_path_a,
    check_confirmed_path_b,
    check_early_move,
    check_accumulation,
    closest_miss_for_confirmed,
    safe,
)

try:
    from monitor import SIGNAL_LOGIC_VERSION, classify_asset_class, TRADE_WIN_PCT
except Exception:
    SIGNAL_LOGIC_VERSION = 1
    TRADE_WIN_PCT = 1.0
    def classify_asset_class(r):
        return "crypto"


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


def now_ts():
    return int(time.time())


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


def cleanup_jsonl(path, ttl_days, ts_field="detect_ts"):
    if not path.exists():
        return

    cutoff = now_ts() - ttl_days * 86400
    recs = load_jsonl(path)
    fresh = [r for r in recs if r.get(ts_field, 0) > cutoff]
    removed = len(recs) - len(fresh)

    if removed:
        with open(path, "w", encoding="utf-8") as f:
            for r in fresh:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"cleanup {path.name}: -{removed}")


def load_market_history_snaps():
    recs = load_jsonl(MARKET_HISTORY)
    grouped = {}

    for r in recs:
        sym = r.get("symbol")
        if not sym:
            continue
        grouped.setdefault(sym, []).append(r)

    for sym in grouped:
        grouped[sym].sort(key=lambda x: x.get("ts", 0))

    return grouped


def get_active_symbols():
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


def compute_forward_returns(snaps, detect_ts, entry_price):
    result = {}

    if detect_ts is None or entry_price in (None, 0):
        for h in FORWARD_HORIZONS:
            result[f"forward_{h}m"] = None
            result[f"forward_{h}m_available"] = False
        return result

    try:
        detect_ts = int(detect_ts)
        entry_price = float(entry_price)
    except (TypeError, ValueError):
        for h in FORWARD_HORIZONS:
            result[f"forward_{h}m"] = None
            result[f"forward_{h}m_available"] = False
        return result

    # Та же семантика, что и в monitor.py:
    # берём первый snapshot с ts >= target_ts.
    # Допустимая задержка после target — максимум 15 минут.
    max_lag_sec = 15 * 60

    ts_list = [s.get("ts", 0) for s in snaps]

    for h in FORWARD_HORIZONS:
        target_ts = detect_ts + h * 60

        i = bisect_left(ts_list, target_ts)

        if i >= len(snaps):
            result[f"forward_{h}m"] = None
            result[f"forward_{h}m_available"] = False
            continue

        found_ts = ts_list[i]

        if found_ts - target_ts > max_lag_sec:
            result[f"forward_{h}m"] = None
            result[f"forward_{h}m_available"] = False
            continue

        best_price = snaps[i].get("price")

        if best_price is None:
            result[f"forward_{h}m"] = None
            result[f"forward_{h}m_available"] = False
            continue

        try:
            best_price = float(best_price)
        except (TypeError, ValueError):
            result[f"forward_{h}m"] = None
            result[f"forward_{h}m_available"] = False
            continue

        result[f"forward_{h}m"] = round(
            (best_price - entry_price) / entry_price * 100,
            3,
        )
        result[f"forward_{h}m_available"] = True

    return result


def classify_quality(forward_returns, movement_snaps):
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

    # [FIX] TRADE_WIN_PCT вместо хардкода 2.0
    if best_forward >= TRADE_WIN_PCT and oi_rising and cvd_rising:
        return {"label": "good", "reason": "форвардный рост + OI↑ + CVD↑"}

    elif best_forward >= TRADE_WIN_PCT:
        return {"label": "good", "reason": "форвардный рост (OI/CVD не подтверждены)"}

    elif best_forward < 0:
        return {"label": "noise", "reason": "форвардный исход отрицательный"}

    elif f60 is not None and f60 > 0 and f120 is not None and f120 < f60 - 2:
        return {"label": "late", "reason": "рост затухает (f120 < f60)"}

    else:
        return {"label": "undetermined", "reason": "смешанные сигналы"}


def determine_fail_point(sym, movement_snaps, lifecycle_state, cvd_momentum):
    confidence = (
        "observed" if lifecycle_state in ("ACCUMULATION", "EARLY_MOVE") else "estimated"
    )

    cm = closest_miss_for_confirmed(movement_snaps, cvd_momentum)

    if lifecycle_state in ("ACCUMULATION", "EARLY_MOVE"):
        stage = lifecycle_state

        if cm["condition"] is not None:
            return {
                "stage": stage,
                "condition": cm["condition"],
                "path": cm["path"],
                "deficit": cm["deficit"],
                "value": cm["value"],
                "threshold": cm["threshold"],
                "confidence": confidence,
            }

        return {
            "stage": stage,
            "condition": "conditions_met_but_not_confirmed",
            "path": None,
            "deficit": 0,
            "value": None,
            "threshold": None,
            "confidence": confidence,
        }

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

    return {
        "stage": stage_note,
        "condition": cm["condition"],
        "path": cm["path"],
        "deficit": cm["deficit"],
        "value": cm["value"],
        "threshold": cm["threshold"],
        "confidence": confidence,
    }


def compute_cvd_momentum(snaps):
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

    # Берём ВСЕХ кандидатов, не только за 24ч.
    existing_candidate_syms = {c.get("symbol") for c in existing_candidates}

    new_candidates = 0

    # FIX:
    # Новые кандидаты НЕ записываются сразу в CANDIDATES_FILE.
    # Они сначала собираются в памяти, потому что ниже файл
    # полностью переписывается через `remaining`.
    new_pending = []

    for sym, snaps in market_snaps.items():
        if not snaps:
            continue

        if sym in active_symbols:
            continue

        if sym in existing_candidate_syms:
            continue

        last = snaps[-1]

        chg = last.get("price_chg24")
        if chg is None or chg <= MISSED_THRESHOLD_PCT:
            continue

        lifecycle_state = last.get("lifecycle_state")

        # ВАЖНО:
        # detect_ts и price_at_detect должны относиться
        # к одному и тому же market snapshot.
        detect_ts = last.get("ts")
        entry_price = last.get("price")

        if detect_ts is None or entry_price is None:
            continue

        cvd_mom = compute_cvd_momentum(snaps)

        candidate = {
            "detect_ts": detect_ts,
            "symbol": sym,
            "name": last.get("name", sym),
            "price_chg24_at_detect": chg,
            "price_at_detect": entry_price,
            "lifecycle_state_at_detect": lifecycle_state,
            "cvd_momentum_at_detect": round(cvd_mom, 2),
            "signal_logic_version": SIGNAL_LOGIC_VERSION,
            "status": "pending_finalization",
        }

        # FIX:
        # Было:
        # append_jsonl(CANDIDATES_FILE,candidate)
        #
        # Это приводило к тому, что кандидат сначала записывался,
        # а затем уничтожался финальной перезаписью файла.
        new_pending.append(candidate)

        # Сразу добавляем symbol в set, чтобы в рамках этого же запуска
        # не создать второй candidate для того же symbol.
        existing_candidate_syms.add(sym)

        new_candidates += 1

    finalized = 0

    # FIX:
    # Новые кандидаты должны войти в итоговый список pending.
    remaining = list(new_pending)

    # Финализируем только кандидатов, существовавших ДО текущего запуска.
    for cand in existing_candidates:
        detect_ts = cand.get("detect_ts", 0)
        sym = cand.get("symbol")

        if cand.get("status") == "finalized":
            continue

        if now - detect_ts < FINALIZATION_WINDOW_H * 3600:
            remaining.append(cand)
            continue

        try:
            snaps = market_snaps.get(sym, [])
            entry_price = cand.get("price_at_detect")

            forward_returns = compute_forward_returns(
                snaps,
                detect_ts,
                entry_price,
            )

            movement_snaps = [
                s for s in snaps if s.get("ts", 0) >= detect_ts - 4 * 3600
            ]

            quality = classify_quality(forward_returns, movement_snaps)

            cvd_mom = cand.get("cvd_momentum_at_detect", 0)
            lifecycle_state = cand.get("lifecycle_state_at_detect")

            fail_point = determine_fail_point(
                sym,
                movement_snaps,
                lifecycle_state,
                cvd_mom,
            )

            asset_class = classify_asset_class(
                {"symbol": sym, "name": cand.get("name", "")}
            )

            analysis_rec = {
                "detect_ts": detect_ts,
                "finalize_ts": now,
                "symbol": sym,
                "name": cand.get("name", sym),
                "price_chg24_at_detect": cand.get("price_chg24_at_detect"),
                "price_at_detect": entry_price,
                "lifecycle_state_at_detect": lifecycle_state,
                "cvd_momentum_at_detect": cvd_mom,
                "signal_logic_version": cand.get(
                    "signal_logic_version",
                    SIGNAL_LOGIC_VERSION,
                ),
                "movement_snaps_count": len(movement_snaps),
                "asset_class": asset_class,
                **forward_returns,
                "quality": quality,
                "fail_point": fail_point,
            }

            append_jsonl(ANALYSIS_FILE, analysis_rec)

            finalized += 1

        except Exception as e:
            print(f"ERROR: финализация {sym} упала: {e}")
            remaining.append(cand)

    # FIX:
    # Теперь при перезаписи сохраняются:
    # 1. новые кандидаты текущего запуска;
    # 2. старые pending-кандидаты;
    # 3. кандидаты, финализация которых завершилась ошибкой.
    with open(CANDIDATES_FILE, "w", encoding="utf-8") as f:
        for cand in remaining:
            f.write(json.dumps(cand, ensure_ascii=False) + "\n")

    cleanup_jsonl(CANDIDATES_FILE, CANDIDATES_TTL_DAYS, "detect_ts")

    cleanup_jsonl(ANALYSIS_FILE, ANALYSIS_TTL_DAYS, "detect_ts")

    pending_count = len(
        [
            c
            for c in load_jsonl(CANDIDATES_FILE)
            if c.get("status") != "finalized"
        ]
    )

    print(
        f"  новых: {new_candidates} · "
        f"финализировано: {finalized} · "
        f"ожидают: {pending_count}"
    )

    print("═══ done ═══")


if __name__ == "__main__":
    run()
```
