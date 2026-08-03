"""trades_report.py — исследовательская статистика по trades.jsonl (schema v2)
+ анализ упущенных движений из unentered_analysis.jsonl.

Принципы:
  - НЕ зашиваем один критерий win: перцентили + win-rate по нескольким уровням.
  - Срезы по return_60m (signal outcome), НЕ по strategy_pnl (strategy outcome).
  - Не-крипто исключаются из win-rate, но видны в счётчиках.
  - Цензура видна: coverage по горизонтам + причина финализации pending +
    доля сделок, закрытых по устаревшей цене (stale exits).
  - LOW_SAMPLE_WARNING — эвристика (не правило): помечает группы, по которым
    рано делать выводы; при малой общей выборке это нормально, не паника.
  - Периоды — ISO-недели (стабильны во времени).
  - Упущенные хорошие лонги: коэффициент захвата, false negative rate по условию,
    near-miss распределение.

ИЗМЕНЕНИЯ (аудит, раунд фиксов):
  - sort_key(): числовая сортировка вместо лексикографической.
  - main(): счётчик excluded разбит на equity/commodity/unknown.
  - load(): считает и печатает skipped_bad_lines.
  - coverage(): добавлена секция STALE EXITS.
  - НОВОЕ: раздел упущенных хороших лонгов с коэффициентом захвата и fail_point.
"""
import json
import re
import time
import statistics as st
from pathlib import Path
from collections import defaultdict

BASE = Path(__file__).resolve().parent
TRADES = BASE / "trades.jsonl"
UNENTERED_ANALYSIS = BASE / "unentered_analysis.jsonl"

WIN_LEVELS       = [0.0, 0.5, 1.0, 2.0]
MOMENTUM_BUCKETS = [3, 5, 7]
CVD_BUCKETS      = [0, 3, 6, 10]
PRICE_BUCKETS    = [3, 8, 15]
HORIZONS         = [60, 120, 240]
LOW_SAMPLE_WARNING = 20
TOP_N            = 10


def load():
    if not TRADES.exists():
        return [], 0
    out, skipped = [], 0
    for ln in TRADES.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                skipped += 1
    return out, skipped


def load_unentered():
    if not UNENTERED_ANALYSIS.exists():
        return []
    out = []
    for ln in UNENTERED_ANALYSIS.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return out


def pct(data, p):
    if not data:
        return None
    s = sorted(data)
    k = (len(s) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def winrates(data, levels=WIN_LEVELS):
    if not data:
        return {lvl: 0.0 for lvl in levels}
    return {lvl: sum(1 for x in data if x >= lvl) / len(data) * 100 for lvl in levels}


def wr_str(wr):
    return " ".join(f"≥{lvl:g}%:{wr[lvl]:4.0f}" for lvl in WIN_LEVELS)


def iso_week(ts):
    return time.strftime("%G-W%V", time.gmtime(ts))


def bucket(v, edges):
    if v is None:
        return "n/a"
    for e in edges:
        if v < e:
            return f"<{e}"
    return f">={edges[-1]}"


_BUCKET_LT_RE = re.compile(r'^<(-?\d+(?:\.\d+)?)$')
_BUCKET_GE_RE = re.compile(r'^>=(-?\d+(?:\.\d+)?)$')


def sort_key(k):
    s = str(k)
    if s == "n/a":
        return (3, 0.0, s)
    m = _BUCKET_LT_RE.match(s)
    if m:
        return (0, float(m.group(1)), 0)
    m = _BUCKET_GE_RE.match(s)
    if m:
        return (0, float(m.group(1)), 1)
    try:
        return (0, float(s), 0)
    except ValueError:
        pass
    return (2, 0.0, s)


def fnum(v, d=1):
    return "—" if v is None else f"{v:+.{d}f}%"


def coverage(rows, label):
    total = len(rows)
    print(f"\nCOVERAGE — {label} (полнота данных по горизонтам)")
    for h in HORIZONS:
        avail = sum(1 for r in rows if r.get(f"return_{h}m") is not None)
        p = avail / total * 100 if total else 0
        print(f"  return_{h}m: {avail:>4}/{total} = {p:5.1f}%")
    fr = defaultdict(int)
    for r in rows:
        fr[r.get("pending_finalize_reason") or "UNKNOWN"] += 1
    if total:
        print("  причина финализации pending:")
        for k in ("COMPLETE", "WAIT_TIMEOUT", "MISSING_PRICE", "UNKNOWN"):
            if fr[k]:
                print(f"    {k:14} {fr[k]:>4}  ({fr[k]/total*100:4.1f}%)")
    stale = [r for r in rows if r.get("exit_price_source") == "last_seen"]
    have_field = sum(1 for r in rows if "exit_price_source" in r)
    if have_field:
        print(f"\n  STALE EXITS (цена выхода устарела на момент закрытия):")
        print(f"    с полем exit_price_source: {have_field}/{total} записей")
        if stale:
            stale_min = [r.get("exit_price_stale_min") for r in stale
                        if r.get("exit_price_stale_min") is not None]
            print(f"    exit_price_source=last_seen: {len(stale)} "
                  f"({len(stale)/have_field*100:4.1f}% от записей с полем)")
            if stale_min:
                print(f"    stale_min: median={st.median(stale_min):.1f}  "
                      f"max={max(stale_min):.1f}")
        else:
            print(f"    exit_price_source=last_seen: 0 — все выходы по свежей цене")


def slice_table(rows, key, label, edges=None):
    g = defaultdict(lambda: {"rets": [], "missing": 0})
    for r in rows:
        v = r.get(key)
        k = bucket(v, edges) if edges else (v if v is not None else "n/a")
        ret = r.get("return_60m")
        if ret is None:
            g[k]["missing"] += 1
        else:
            g[k]["rets"].append(ret)
    print(f"\n=== {label}  (signal outcome @60m, crypto) ===")
    print(f"  {'группа':20} {'n':>4} {'miss':>4} {'median':>8} {'win≥0':>6} {'win≥1':>6}")
    low = []
    for k in sorted(g, key=sort_key):
        d = g[k]
        n = len(d["rets"])
        med = st.median(d["rets"]) if d["rets"] else 0
        wr = winrates(d["rets"])
        mark = ""
        if 0 < n < LOW_SAMPLE_WARNING:
            mark = "  *low*"
            low.append(f"{k}(n={n})")
        print(f"  {str(k):20} {n:>4} {d['missing']:>4} {med:+7.2f}% "
              f"{wr[0.0]:5.0f}% {wr[1.0]:5.0f}%{mark}")
    if low:
        print(f"  ⚠ low sample (<{LOW_SAMPLE_WARNING}, эвристика): "
              f"{', '.join(low)} — выводов пока не делать")


def top_signals(rows, label, key, reverse=True):
    have = [r for r in rows if r.get(key) is not None]
    have.sort(key=lambda r: r[key], reverse=reverse)
    print(f"\n=== {label} (по {key}) ===")
    for r in have[:TOP_N]:
        print(f"  {r['symbol']:12} r60={fnum(r.get('return_60m')):>8}  "
              f"strat={fnum(r.get('strategy_pnl_pct')):>8}  "
              f"mom={r.get('entry_momentum')}  cvd_m={r.get('entry_cvd_momentum')}  "
              f"path={r.get('entry_path')}  {r.get('entry_pattern')}  "
              f"{r.get('entry_earliness_label')}  exit={r.get('exit_reason')}/"
              f"{r.get('exit_state')}  <60m={r.get('closed_before_60m')}")


def top_divergence(rows):
    have = [r for r in rows
            if r.get("return_60m") is not None and r.get("strategy_pnl_pct") is not None]
    have.sort(key=lambda r: r["return_60m"] - r["strategy_pnl_pct"], reverse=True)
    print(f"\n=== TOP расхождение: хороший вход, плохой выход (r60 − strategy) ===")
    for r in have[:TOP_N]:
        diff = r["return_60m"] - r["strategy_pnl_pct"]
        peak = r.get("max_pnl_pct")
        peak_s = "—" if peak is None else f"{peak:+.1f}%"
        print(f"  {r['symbol']:12} r60={r['return_60m']:+6.1f}%  "
              f"strat={r['strategy_pnl_pct']:+6.1f}%  Δ={diff:+6.1f}%  "
              f"hold={r.get('hold_min')}м  exit={r.get('exit_reason')}  peak={peak_s}")


def analyze_unentered(unentered, trades, cutoff_h=24):
    """Анализ упущенных хороших лонгов."""
    now = time.time()
    cutoff = now - cutoff_h * 3600

    # Хорошие лонги, которые поймали
    caught_good = []
    for t in trades:
        if t.get("entry_ts") and t["entry_ts"] >= cutoff:
            r60 = t.get("return_60m")
            strat = t.get("strategy_pnl_pct")
            if (r60 is not None and r60 >= 1.0) or (strat is not None and strat > 0):
                caught_good.append(t)

    # Хорошие лонги, которые упустили
    missed_good = [u for u in unentered
                   if u.get("detect_ts", 0) >= cutoff
                   and u.get("quality", {}).get("label") == "good"]

    total_good = len(caught_good) + len(missed_good)
    capture_rate = len(caught_good) / total_good * 100 if total_good > 0 else 0

    print(f"\nУПУЩЕННЫЕ ХОРОШИЕ ЛОНГИ (за последние {cutoff_h}ч)")
    print(f"  поймали: {len(caught_good)}")
    print(f"  упустили: {len(missed_good)}")
    print(f"  всего хороших: {total_good}")
    print(f"  коэффициент захвата: {capture_rate:.1f}%")

    if total_good < LOW_SAMPLE_WARNING:
        print(f"  ⚠ low sample (<{LOW_SAMPLE_WARNING}) — выводов пока не делать")
        return

    # Агрегация по fail_point
    by_condition = defaultdict(lambda: {"count": 0, "deficits": []})
    by_stage = defaultdict(int)
    for u in missed_good:
        fp = u.get("fail_point", {})
        stage = fp.get("stage", "unknown")
        condition = fp.get("condition", "unknown")
        deficit = fp.get("deficit")

        key = f"{stage}:{condition}"
        by_condition[key]["count"] += 1
        by_condition[key]["stage"] = stage
        by_condition[key]["condition"] = condition
        if deficit is not None and deficit != float("inf"):
            by_condition[key]["deficits"].append(deficit)

        by_stage[stage] += 1

    print(f"\n  False negative rate по условию (топ-10):")
    print(f"    {'условие':40} {'count':>6} {'avg_deficit':>12}")
    sorted_cond = sorted(by_condition.values(), key=lambda x: x["count"], reverse=True)[:10]
    for item in sorted_cond:
        cond_name = f"{item['stage']}:{item['condition']}"
        avg_def = None
        if item["deficits"]:
            avg_def = sum(item["deficits"]) / len(item["deficits"])
        avg_str = f"{avg_def:11.3f}" if avg_def is not None else "        n/a"
        print(f"    {cond_name:40} {item['count']:>6} {avg_str}")
        if item["count"] < LOW_SAMPLE_WARNING:
            print(f"      ⚠ low sample (n={item['count']})")

    print(f"\n  False negative rate по стадии:")
    for stage, count in sorted(by_stage.items(), key=lambda x: x[1], reverse=True):
        print(f"    {stage:30} {count:>6}")

    # Near-miss распределение
    near_miss = [u for u in missed_good
                 if u.get("fail_point", {}).get("deficit") is not None
                 and u.get("fail_point", {}).get("deficit") != float("inf")]
    if near_miss:
        deficits = [u["fail_point"]["deficit"] for u in near_miss]
        print(f"\n  Near-miss распределение (deficit):")
        print(f"    n: {len(deficits)}")
        print(f"    median: {st.median(deficits):.3f}")
        print(f"    p25: {pct(deficits, 25):.3f}")
        print(f"    p75: {pct(deficits, 75):.3f}")
        print(f"    min: {min(deficits):.3f}")
        print(f"    max: {max(deficits):.3f}")


def main():
    rows, skipped_bad_lines = load()
    unentered = load_unentered()

    if not rows:
        print("trades.jsonl пуст — копим сделки.")
        if skipped_bad_lines:
            print(f"⚠ обнаружено {skipped_bad_lines} битых строк при чтении.")
        return

    crypto = [r for r in rows if r.get("asset_class") == "crypto"]
    non_crypto = [r for r in rows if r.get("asset_class") != "crypto"]
    equity_n    = sum(1 for r in non_crypto if r.get("asset_class") == "equity")
    commodity_n = sum(1 for r in non_crypto if r.get("asset_class") == "commodity")
    unknown_n   = len(non_crypto) - equity_n - commodity_n

    with60 = [r for r in crypto if r.get("return_60m") is not None]
    miss60 = len(crypto) - len(with60)
    closed = [r for r in crypto if r.get("strategy_pnl_pct") is not None]

    lv = defaultdict(int)
    for r in crypto:
        lv[r.get("signal_logic_version") or "?"] += 1

    print("ВЫБОРКА")
    print(f"  сделок всего:        {len(rows)}")
    if skipped_bad_lines:
        print(f"  ⚠ битых строк пропущено при чтении: {skipped_bad_lines}")
    print(f"  из них crypto:       {len(crypto)}")
    print(f"  исключены из win-rate: equity={equity_n}  commodity={commodity_n}"
          + (f"  unknown/missing={unknown_n}" if unknown_n else ""))
    print(f"  с return_60m:        {len(with60)}")
    print(f"  без return_60m:      {miss60}  ← горизонт не наступил / нет цены / история очищена")
    print(f"  закрытых (strategy): {len(closed)}")
    print(f"  закрытых раньше 60м: "
          f"{sum(1 for r in crypto if r.get('closed_before_60m'))}")
    print(f"  signal_logic_version:  "
          + ", ".join(f"v{k}={lv[k]}" for k in sorted(lv, key=sort_key)))

    coverage(crypto, "crypto")

    rets60 = [r["return_60m"] for r in with60]
    if not rets60:
        print("\nНет сделок с return_60m — нечего анализировать.")
        return

    print("\nSIGNAL OUTCOME @60m (crypto)")
    print(f"  n={len(rets60)}  mean={st.mean(rets60):+.2f}%  median={st.median(rets60):+.2f}%")
    print(f"  p25={pct(rets60,25):+.2f}%  p75={pct(rets60,75):+.2f}%  "
          f"p90={pct(rets60,90):+.2f}%  min={min(rets60):+.1f}%  max={max(rets60):+.1f}%")
    print(f"  win-rate:  {wr_str(winrates(rets60))}")

    rets120 = [r["return_120m"] for r in crypto if r.get("return_120m") is not None]
    if rets120:
        print(f"\nSIGNAL OUTCOME @120m  n={len(rets120)}  "
              f"median={st.median(rets120):+.2f}%  win-rate: {wr_str(winrates(rets120))}")

    peaks = [r["max_pnl_pct"] - r["strategy_pnl_pct"]
             for r in closed
             if r.get("max_pnl_pct") is not None and r.get("strategy_pnl_pct") is not None]
    if peaks:
        print(f"\nSTRATEGY OUTCOME — недобор от пика (max_pnl − strategy_pnl):")
        print(f"  median={st.median(peaks):.2f}%  mean={st.mean(peaks):.2f}%  "
              f"(много → выход запаздывает, кандидат на TP/earlier-exit)")

    wg = defaultdict(list)
    for r in with60:
        wg[iso_week(r["entry_ts"])].append(r["return_60m"])
    print("\n=== По ISO-неделям (regime bias) ===")
    print(f"  {'неделя':10} {'n':>4} {'median':>8} {'win≥0':>6} {'win≥1':>6}")
    for wk in sorted(wg):
        d = wg[wk]
        wr = winrates(d)
        low = "  *low*" if 0 < len(d) < LOW_SAMPLE_WARNING else ""
        print(f"  {wk:10} {len(d):>4} {st.median(d):+7.2f}% "
              f"{wr[0.0]:5.0f}% {wr[1.0]:5.0f}%{low}")

    slice_table(crypto, "entry_momentum",        "MOMENTUM на входе",      MOMENTUM_BUCKETS)
    slice_table(crypto, "entry_cvd_momentum",    "CVD_MOMENTUM на входе",  CVD_BUCKETS)
    slice_table(crypto, "entry_price_chg24",     "PRICE_CHG24 на входе",   PRICE_BUCKETS)
    slice_table(crypto, "entry_earliness_label", "РАННОСТЬ входа")
    slice_table(crypto, "entry_path",            "ПУТЬ входа (classic/early)")
    slice_table(crypto, "entry_pattern",         "ПАТТЕРН на входе")
    slice_table(crypto, "entry_divergence",      "ДИВЕРГЕНЦИЯ на входе")
    slice_table(crypto, "entry_market_phase",    "ФАЗА рынка на входе")
    slice_table(crypto, "closed_before_60m",     "ЖИЗНЬ СДЕЛКИ (<60м / ≥60м)")
    slice_table(crypto, "signal_logic_version",  "ВЕРСИЯ логики сигнала (v1 vs v2)")

    top_signals(crypto, "TOP 10 BEST SIGNAL",    "return_60m",       reverse=True)
    top_signals(crypto, "TOP 10 WORST SIGNAL",   "return_60m",       reverse=False)
    top_signals(crypto, "TOP 10 BEST STRATEGY",  "strategy_pnl_pct", reverse=True)
    top_signals(crypto, "TOP 10 WORST STRATEGY", "strategy_pnl_pct", reverse=False)
    top_divergence(crypto)

    analyze_unentered(unentered, rows, cutoff_h=24)


if __name__ == "__main__":
    main()
