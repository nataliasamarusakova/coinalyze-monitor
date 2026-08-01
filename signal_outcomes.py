"""
signal_outcomes.py
==================
Считает исход КАЖДОГО реального сигнала из calibration.jsonl:
цена в момент входа vs цена через 30/60/120 мин (forward return по price
из market_history.jsonl). Дедуплицирует мигание (сигналы одной монеты
в пределах 45 мин = одно движение).

Запуск:  python signal_outcomes.py   (рядом с monitor.py и *.jsonl)
Зависимостей не требует — только стандартная библиотека.
"""

import json
from pathlib import Path
from bisect import bisect_left

BASE = Path(__file__).resolve().parent
CALIB = BASE / "calibration.jsonl"
HIST  = BASE / "market_history.jsonl"

HORIZONS = [30, 60, 120]      # минуты
WIN_PCT  = 1.0                # «win», если через 60 мин цена +1% и выше
DEDUP_MIN = 45                # сигналы одной монеты ближе 45 мин = одно движение


def load_jsonl(p: Path):
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def main():
    signals = [s for s in load_jsonl(CALIB)
               if s.get("state") in ("CONFIRMED_TREND", "ACCELERATION")]
    if not signals:
        print("В calibration.jsonl нет сигналов CONFIRMED/ACCELERATION — пока нечего мерить.")
        return

    # цена по времени для каждой монеты
    price_idx: dict[str, list[tuple[int, float]]] = {}
    for r in load_jsonl(HIST):
        sym, ts, p = r.get("symbol"), r.get("ts"), r.get("price")
        if sym and ts and p:
            price_idx.setdefault(sym, []).append((ts, p))
    for sym in price_idx:
        price_idx[sym].sort(key=lambda x: x[0])

    def price_after(sym, ts_target):
        idx = price_idx.get(sym, [])
        if not idx:
            return None
        i = bisect_left([t for t, _ in idx], ts_target)
        return idx[i][1] if i < len(idx) else None

    # дедупликация мигания: на монету оставляем первый сигнал в кластере 45 мин
    signals.sort(key=lambda s: (s["symbol"], s["ts"]))
    deduped, last = [], {}
    for s in signals:
        prev_ts = last.get(s["symbol"])
        if prev_ts is not None and s["ts"] - prev_ts < DEDUP_MIN * 60:
            continue
        deduped.append(s)
        last[s["symbol"]] = s["ts"]

    print(f"Сигналов всего: {len(signals)}  →  после дедупликации движений: {len(deduped)}\n")
    print("СИГНАЛ → ИСХОД (forward return по цене)")
    print("=" * 78)

    wins = {h: 0 for h in HORIZONS}
    cnt = {h: 0 for h in HORIZONS}
    rets60 = []
    win_m, loss_m = {"oi_accel": [], "cvd_momentum": [], "momentum": [], "confidence": []}, \
                    {"oi_accel": [], "cvd_momentum": [], "momentum": [], "confidence": []}

    for s in deduped:
        sym, ts = s["symbol"], s["ts"]
        p0 = price_after(sym, ts)
        parts = [f"{sym:10} {s['state'][:9]:9} mom={s.get('momentum')} "
                 f"conf={s.get('confidence')} oi_acc={s.get('oi_accel', 0):.1f} "
                 f"cvd_m={s.get('cvd_momentum', 0):.0f} {s.get('oi_trend')}/{s.get('cvd_trend')}"]
        rs = {}
        for h in HORIZONS:
            ph = price_after(sym, ts + h * 60)
            if p0 and ph:
                ret = (ph - p0) / p0 * 100
                rs[h] = ret
                cnt[h] += 1
                if ret >= WIN_PCT:
                    wins[h] += 1
            else:
                rs[h] = None
        if rs.get(60) is not None:
            rets60.append(rs[60])
            bucket = win_m if rs[60] >= WIN_PCT else loss_m
            for k in bucket:
                v = s.get(k)
                if v is not None:
                    bucket[k].append(v)
        ret_str = " ".join(f"{h}m={('—' if rs[h] is None else f'{rs[h]:+.1f}%')}" for h in HORIZONS)
        print(f"  {' '.join(parts)} || {ret_str}")

    print("\n" + "=" * 78)
    print("АГРЕГАТ")
    print("=" * 78)
    for h in HORIZONS:
        wr = wins[h] / cnt[h] * 100 if cnt[h] else 0
        print(f"  горизонт {h:3}м: оценено {cnt[h]:3}  win(>={WIN_PCT}%) {wins[h]:3}  win-rate {wr:5.1f}%")
    if rets60:
        rets60.sort()
        print(f"  return@60m: медиана {rets60[len(rets60)//2]:+.2f}%  "
              f"лучший {max(rets60):+.1f}%  худший {min(rets60):+.1f}%")

    def avg(xs):
        return sum(xs) / len(xs) if xs else 0
    if win_m["momentum"] or loss_m["momentum"]:
        print("\n  Разделение WIN vs LOSS (по return@60m) — куда двигать пороги:")
        for k in ["oi_accel", "cvd_momentum", "momentum", "confidence"]:
            print(f"    {k:13} win={avg(win_m[k]):7.2f}   loss={avg(loss_m[k]):7.2f}")


if __name__ == "__main__":
    main()
