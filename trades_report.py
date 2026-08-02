"""trades_report.py — исследовательская статистика по trades.jsonl (schema v2).

Принципы:
  - НЕ зашиваем один критерий win: перцентили + win-rate по нескольким уровням.
  - Срезы по return_60m (signal outcome), НЕ по strategy_pnl (strategy outcome).
  - Не-крипто исключаются из win-rate, но видны в счётчиках.
  - Цензура видна: coverage по горизонтам + причина финализации pending.
  - LOW_SAMPLE_WARNING — эвристика (не правило): помечает группы, по которым
    рано делать выводы; при малой общей выборке это нормально, не паника.
  - Периоды — ISO-недели (стабильны во времени).
"""
import json
import time
import statistics as st
from pathlib import Path
from collections import defaultdict

BASE = Path(__file__).resolve().parent
TRADES = BASE / "trades.jsonl"

WIN_LEVELS       = [0.0, 0.5, 1.0, 2.0]
MOMENTUM_BUCKETS = [3, 5, 7]
CVD_BUCKETS      = [0, 3, 6, 10]
PRICE_BUCKETS    = [3, 8, 15]
HORIZONS         = [60, 120, 240]
LOW_SAMPLE_WARNING = 20   # эвристика: ниже — помечать, выводов не делать
TOP_N            = 10


def load():
    if not TRADES.exists():
        return []
    out = []
    for ln in TRADES.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
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
    for k in sorted(g, key=lambda x: str(x)):
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
    """Отличный вход (signal), плохой выход (strategy) — диагностика качества выхода."""
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


def main():
    rows = load()
    if not rows:
        print("trades.jsonl пуст — копим сделки.")
        return

    crypto = [r for r in rows if r.get("asset_class") == "crypto"]
    with60 = [r for r in crypto if r.get("return_60m") is not None]
    miss60 = len(crypto) - len(with60)
    closed = [r for r in crypto if r.get("strategy_pnl_pct") is not None]

    lv = defaultdict(int)
    for r in crypto:
        lv[r.get("signal_logic_version") or "?"] += 1

    print("ВЫБОРКА")
    print(f"  сделок всего:        {len(rows)}")
    print(f"  из них crypto:       {len(crypto)}  "
          f"(equity/commodity: {len(rows) - len(crypto)} — исключены из win-rate)")
    print(f"  с return_60m:        {len(with60)}")
    print(f"  без return_60m:      {miss60}  ← горизонт не наступил / нет цены / история очищена")
    print(f"  закрытых (strategy): {len(closed)}")
    print(f"  закрытых раньше 60м: "
          f"{sum(1 for r in crypto if r.get('closed_before_60m'))}")
    print(f"  signal_logic_version:  "
          + ", ".join(f"v{k}={lv[k]}" for k in sorted(lv)))

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


if __name__ == "__main__":
    main()
