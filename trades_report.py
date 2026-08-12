"""
trades_report.py
"""

import json
import re
import time
import statistics as st

from pathlib import Path
from collections import defaultdict


BASE = Path(__file__).resolve().parent

TRADES = BASE / "trades.jsonl"

# Финализированные missed candidates.
UNENTERED_ANALYSIS = BASE / "unentered_analysis.jsonl"

# Только pending / ещё не финализированные candidates.
UNENTERED_CANDIDATES = BASE / "unentered_candidates.jsonl"

# История рынка для post-exit analysis.
MARKET_HISTORY = BASE / "market_history.jsonl"


WIN_LEVELS = [0.0, 0.5, 1.0, 2.0]

MOMENTUM_BUCKETS = [3, 5, 7]
CVD_BUCKETS = [0, 3, 6, 10]
PRICE_BUCKETS = [3, 8, 15]

HORIZONS = [60, 120, 240]

POST_EXIT_HORIZONS = [30, 60, 120]

LOW_SAMPLE_WARNING = 20
TOP_N = 10


# ============================================================
# OPERATIONAL MOVEMENT CAPTURE
# ============================================================

# Одинаковое определение для ENTERED и MISSED.
CAPTURE_GOOD_RETURN = 1.0

# Не считаем capture устойчивым при малой выборке.
MIN_CAPTURE_SAMPLE = 20


# Максимально допустимое отклонение timestamp при поиске
# post-exit market price.
#
# История собирается примерно каждые 5 минут, поэтому 10 минут
# достаточно для нормального поиска ближайшего snapshot.
MAX_HISTORY_GAP_SEC = 10 * 60


# ============================================================
# LOADERS
# ============================================================

def load_jsonl(path):
    """
    Загружает JSONL.

    Возвращает:
        rows,
        skipped_bad_lines,
        exists
    """

    if not path.exists():
        return [], 0, False

    out = []
    skipped = 0

    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as fh:
            for ln in fh:
                ln = ln.strip()

                if not ln:
                    continue

                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    skipped += 1

    except Exception as exc:
        print(
            f"⚠ Не удалось прочитать "
            f"{path.name}: {exc}"
        )
        return [], 0, True

    return out, skipped, True


def load():
    return load_jsonl(TRADES)


def load_unentered_analysis():
    return load_jsonl(UNENTERED_ANALYSIS)


def load_unentered_candidates():
    return load_jsonl(UNENTERED_CANDIDATES)


# ============================================================
# GENERIC STATS
# ============================================================

def pct(data, p):
    if not data:
        return None

    s = sorted(data)

    k = (len(s) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(s) - 1)

    if f == c:
        return s[f]

    return s[f] + (s[c] - s[f]) * (k - f)


def winrates(data, levels=WIN_LEVELS):
    if not data:
        return {
            lvl: 0.0
            for lvl in levels
        }

    return {
        lvl: sum(
            1
            for x in data
            if x >= lvl
        ) / len(data) * 100
        for lvl in levels
    }


def wr_str(wr):
    return " ".join(
        f"≥{lvl:g}%:{wr[lvl]:4.0f}"
        for lvl in WIN_LEVELS
    )


def iso_week(ts):
    return time.strftime(
        "%G-W%V",
        time.gmtime(ts),
    )


def bucket(v, edges):
    if v is None:
        return "n/a"

    for e in edges:
        if v < e:
            return f"<{e}"

    return f">={edges[-1]}"


_BUCKET_LT_RE = re.compile(
    r"^<(-?\d+(?:\.\d+)?)$"
)

_BUCKET_GE_RE = re.compile(
    r"^>=(-?\d+(?:\.\d+)?)$"
)


def sort_key(k):
    s = str(k)

    if s == "n/a":
        return (3, 0.0, s)

    m = _BUCKET_LT_RE.match(s)

    if m:
        return (
            0,
            float(m.group(1)),
            0,
        )

    m = _BUCKET_GE_RE.match(s)

    if m:
        return (
            0,
            float(m.group(1)),
            1,
        )

    try:
        return (
            0,
            float(s),
            0,
        )
    except (
        ValueError,
        TypeError,
    ):
        pass

    return (
        2,
        0.0,
        s,
    )


def fnum(v, d=1):
    if v is None:
        return "—"

    return f"{v:+.{d}f}%"


def median_or_none(values):
    return (
        st.median(values)
        if values
        else None
    )


def mean_or_none(values):
    return (
        st.mean(values)
        if values
        else None
    )


# ============================================================
# SAFE FIELD HELPERS
# ============================================================

def numeric(v):
    if v is None:
        return None

    if isinstance(v, bool):
        return None

    try:
        return float(v)
    except (
        TypeError,
        ValueError,
    ):
        return None


def first_numeric(row, *keys):
    for key in keys:
        value = numeric(
            row.get(key)
        )

        if value is not None:
            return value

    return None


def first_value(row, *keys):
    for key in keys:
        value = row.get(key)

        if value is not None:
            return value

    return None


def get_forward_return(row, horizon):
    """
    Поддерживает:

        return_60m
        return_120m
        return_240m

    Для unentered:

        forward_60m
        forward_120m
        forward_240m

    Дополнительные варианты существуют только для
    исследовательской устойчивости loader-а.
    """

    return first_numeric(
        row,
        f"return_{horizon}m",
        f"forward_{horizon}m",
        f"forward_return_{horizon}m",
    )


def get_mfe(r):
    value = numeric(
        r.get("mfe_pct")
    )

    if value is None:
        value = numeric(
            r.get("max_pnl_pct")
        )

    return value


def get_mae(r):
    value = numeric(
        r.get("mae_pct")
    )

    if value is None:
        value = numeric(
            r.get("min_pnl_pct")
        )

    return value


# ============================================================
# DATA / TRADING CLASSIFICATION
# ============================================================

DATA_EXIT_REASONS = {
    "DATA_STALE",
    "MISSED",
}

DATA_EXIT_CLASSES = {
    "DATA",
}


TRADING_EXIT_REASONS = {
    "TIMEOUT",
    "SIGNAL_DECAY",
    "EXHAUSTION",
    "STOP_LOSS",
    "INVALIDATED",
    "DISTRIBUTION",
    "NEUTRAL",
    "EXCHANGE_CLOSED",
}


SIGNAL_EXIT_REASONS = {
    "EXHAUSTION",
    "INVALIDATED",
    "DISTRIBUTION",
}


def is_data_outcome(r):
    reason = r.get(
        "exit_reason"
    )

    exit_class = r.get(
        "exit_class"
    )

    if reason in DATA_EXIT_REASONS:
        return True

    if exit_class in DATA_EXIT_CLASSES:
        return True

    if r.get("outcome_unknown"):
        return True

    return False


def is_trading_outcome(r):
    if (
        numeric(
            r.get(
                "strategy_pnl_pct"
            )
        )
        is None
    ):
        return False

    return not is_data_outcome(r)


def strategy_population(rows):
    """
    Population для анализа реального поведения strategy.

    Исключаем:

        DATA_STALE
        MISSED
        outcome_unknown
        отсутствующий strategy PnL
    """

    return [
        r
        for r in rows
        if (
            numeric(
                r.get(
                    "strategy_pnl_pct"
                )
            )
            is not None
            and not is_data_outcome(r)
        )
    ]


# ============================================================
# COVERAGE
# ============================================================

def coverage(rows, label):
    total = len(rows)

    print(
        f"\nCOVERAGE — {label} "
        f"(полнота данных по горизонтам)"
    )

    for h in HORIZONS:
        avail = sum(
            1
            for r in rows
            if get_forward_return(
                r,
                h,
            ) is not None
        )

        p = (
            avail / total * 100
            if total
            else 0
        )

        print(
            f"  return_{h}m: "
            f"{avail:>4}/{total} = "
            f"{p:5.1f}%"
        )

    reasons = defaultdict(int)

    for r in rows:
        reasons[
            r.get(
                "pending_finalize_reason"
            )
            or "UNKNOWN"
        ] += 1

    if total:
        print(
            "  причина финализации pending:"
        )

        for k in (
            "COMPLETE",
            "WAIT_TIMEOUT",
            "MISSING_PRICE",
            "UNKNOWN",
        ):
            if reasons[k]:
                print(
                    f"    {k:14} "
                    f"{reasons[k]:>4} "
                    f"({reasons[k] / total * 100:4.1f}%)"
                )

    stale = [
        r
        for r in rows
        if r.get(
            "exit_price_source"
        ) == "last_seen"
    ]

    have_field = sum(
        1
        for r in rows
        if "exit_price_source" in r
    )

    if have_field:
        print(
            "\nSTALE EXIT PRICE:"
        )

        print(
            f"  с полем exit_price_source: "
            f"{have_field}/{total}"
        )

        if stale:
            stale_min = [
                numeric(
                    r.get(
                        "exit_price_stale_min"
                    )
                )
                for r in stale
                if numeric(
                    r.get(
                        "exit_price_stale_min"
                    )
                ) is not None
            ]

            print(
                f"  exit_price_source=last_seen: "
                f"{len(stale)} "
                f"({len(stale) / have_field * 100:4.1f}% "
                f"от записей с полем)"
            )

            if stale_min:
                print(
                    f"  stale_min: "
                    f"median={st.median(stale_min):.1f} "
                    f"max={max(stale_min):.1f}"
                )
        else:
            print(
                "  exit_price_source=last_seen: 0"
            )


# ============================================================
# SIGNAL / TRADING / DATA OUTCOMES
# ============================================================

def outcome_summary(crypto):
    data_rows = [
        r
        for r in crypto
        if is_data_outcome(r)
    ]

    trading_rows = [
        r
        for r in crypto
        if is_trading_outcome(r)
    ]

    print(
        "\nOUTCOME CLASSIFICATION"
    )

    print(
        f"  DATA outcome:      "
        f"{len(data_rows):>4} "
        f"({len(data_rows) / len(crypto) * 100:5.1f}%)"
        if crypto
        else
        "  DATA outcome: 0"
    )

    print(
        f"  TRADING outcome:   "
        f"{len(trading_rows):>4} "
        f"({len(trading_rows) / len(crypto) * 100:5.1f}%)"
        if crypto
        else
        "  TRADING outcome: 0"
    )

    if data_rows:
        data_pnl = [
            numeric(
                r.get(
                    "strategy_pnl_pct"
                )
            )
            for r in data_rows
            if numeric(
                r.get(
                    "strategy_pnl_pct"
                )
            ) is not None
        ]

        if data_pnl:
            print(
                "\nDATA exits:"
            )

            print(
                f"  DATA_STALE / DATA "
                f"n={len(data_pnl)} "
                f"median={st.median(data_pnl):+.2f}% "
                f"mean={st.mean(data_pnl):+.2f}%"
            )

    by_reason = defaultdict(list)

    for r in trading_rows:
        reason = (
            r.get("exit_reason")
            or "UNKNOWN"
        )

        pnl = numeric(
            r.get(
                "strategy_pnl_pct"
            )
        )

        if pnl is not None:
            by_reason[reason].append(
                pnl
            )

    if by_reason:
        print(
            "\nTRADING exits:"
        )

        order = [
            "TIMEOUT",
            "SIGNAL_DECAY",
            "EXHAUSTION",
            "STOP_LOSS",
            "INVALIDATED",
            "DISTRIBUTION",
            "NEUTRAL",
            "EXCHANGE_CLOSED",
        ]

        used = set()

        for reason in order:
            values = by_reason.get(
                reason
            )

            if not values:
                continue

            used.add(reason)

            print(
                f"  {reason:16} "
                f"n={len(values):>3} "
                f"median={st.median(values):+6.2f}% "
                f"mean={st.mean(values):+6.2f}%"
            )

        for reason in sorted(
            set(by_reason) - used
        ):
            values = by_reason[reason]

            print(
                f"  {reason:16} "
                f"n={len(values):>3} "
                f"median={st.median(values):+6.2f}% "
                f"mean={st.mean(values):+6.2f}%"
            )


# ============================================================
# SLICE TABLES
# ============================================================

def slice_table(
    rows,
    key,
    label,
    edges=None,
):
    groups = defaultdict(
        lambda: {
            "rets": [],
            "missing": 0,
        }
    )

    for r in rows:
        v = r.get(key)

        k = (
            bucket(v, edges)
            if edges
            else (
                v
                if v is not None
                else "n/a"
            )
        )

        ret = get_forward_return(
            r,
            60,
        )

        if ret is None:
            groups[k]["missing"] += 1
        else:
            groups[k]["rets"].append(
                ret
            )

    print(
        f"\n=== {label} "
        f"(signal outcome @60m, crypto) ==="
    )

    print(
        f"  {'группа':20} "
        f"{'n':>4} "
        f"{'miss':>4} "
        f"{'median':>8} "
        f"{'win≥0':>6} "
        f"{'win≥1':>6}"
    )

    low = []

    for k in sorted(
        groups,
        key=sort_key,
    ):
        d = groups[k]

        n = len(d["rets"])

        med = (
            st.median(d["rets"])
            if d["rets"]
            else 0
        )

        wr = winrates(
            d["rets"]
        )

        mark = ""

        if (
            0 < n
            < LOW_SAMPLE_WARNING
        ):
            mark = "  *low*"

            low.append(
                f"{k}(n={n})"
            )

        print(
            f"  {str(k):20} "
            f"{n:>4} "
            f"{d['missing']:>4} "
            f"{med:+7.2f}% "
            f"{wr[0.0]:5.0f}% "
            f"{wr[1.0]:5.0f}%"
            f"{mark}"
        )

    if low:
        print(
            f"  ⚠ low sample "
            f"(<{LOW_SAMPLE_WARNING}, эвристика): "
            f"{', '.join(low)} — "
            f"выводов пока не делать"
        )


# ============================================================
# MFE / MAE
# ============================================================

def analyze_mfe_mae(rows):
    """
    Только TRADING population.

    DATA_STALE не должен влиять
    на оценку качества входа/выхода.
    """

    pop = [
        r
        for r in rows
        if (
            is_trading_outcome(r)
            and get_mfe(r) is not None
            and get_mae(r) is not None
        )
    ]

    if not pop:
        print(
            "\nMFE/MAE: недостаточно данных."
        )
        return

    mfe = [
        get_mfe(r)
        for r in pop
    ]

    mae = [
        get_mae(r)
        for r in pop
    ]

    print(
        f"\n=== MFE / MAE "
        f"(TRADING population) ==="
    )

    print(
        f"n={len(pop)}"
    )

    print(
        f"MFE: "
        f"median={st.median(mfe):+.2f}% "
        f"mean={st.mean(mfe):+.2f}% "
        f"p25={pct(mfe, 25):+.2f}% "
        f"p75={pct(mfe, 75):+.2f}% "
        f"max={max(mfe):+.2f}%"
    )

    print(
        f"MAE: "
        f"median={st.median(mae):+.2f}% "
        f"mean={st.mean(mae):+.2f}% "
        f"p25={pct(mae, 25):+.2f}% "
        f"p75={pct(mae, 75):+.2f}% "
        f"min={min(mae):+.2f}%"
    )


# ============================================================
# MFE REALIZATION
# ============================================================

def analyze_capture(rows):
    """
    Анализ реализации движения.

    raw realization:
        strategy_pnl / MFE * 100

    clipped realization:
        raw realization, ограниченный 0..100

    negative realization:
        strategy_pnl < 0 при MFE > 0

    giveback:
        MFE - strategy_pnl

    ВАЖНО:

    MFE <= 0 не используется как denominator.
    """

    pop = [
        r
        for r in rows
        if (
            is_trading_outcome(r)
            and get_mfe(r) is not None
            and numeric(
                r.get(
                    "strategy_pnl_pct"
                )
            ) is not None
        )
    ]

    positive_mfe = [
        r
        for r in pop
        if get_mfe(r) > 0
    ]

    print(
        "\n=== EXIT / REALIZATION QUALITY ==="
    )

    print(
        f"positive MFE population: "
        f"{len(positive_mfe)}"
    )

    if not positive_mfe:
        print(
            "Недостаточно данных."
        )
        return

    raw = []
    clipped = []
    givebacks = []

    negative_realization = []
    positive_realization = []

    for r in positive_mfe:
        mfe = get_mfe(r)

        strategy = numeric(
            r.get(
                "strategy_pnl_pct"
            )
        )

        if (
            mfe is None
            or mfe <= 0
            or strategy is None
        ):
            continue

        value = (
            strategy / mfe * 100
        )

        raw.append(value)

        clipped.append(
            max(
                0.0,
                min(
                    100.0,
                    value,
                ),
            )
        )

        givebacks.append(
            mfe - strategy
        )

        if strategy < 0:
            negative_realization.append(
                value
            )
        else:
            positive_realization.append(
                value
            )

    if not raw:
        print(
            "Недостаточно данных "
            "для realization."
        )
        return

    print(
        f"raw realization: "
        f"n={len(raw)} "
        f"median={st.median(raw):.1f}% "
        f"mean={st.mean(raw):.1f}% "
        f"p25={pct(raw, 25):.1f}% "
        f"p75={pct(raw, 75):.1f}%"
    )

    print(
        f"clipped realization 0..100: "
        f"median={st.median(clipped):.1f}% "
        f"mean={st.mean(clipped):.1f}%"
    )

    print(
        f"giveback (MFE - strategy): "
        f"median={st.median(givebacks):+.2f}% "
        f"mean={st.mean(givebacks):+.2f}%"
    )

    negative_n = len(
        negative_realization
    )

    print(
        f"negative realization: "
        f"{negative_n}/{len(raw)} "
        f"({negative_n / len(raw) * 100:.1f}%)"
    )

    positive_n = len(
        positive_realization
    )

    print(
        f"non-negative realization: "
        f"{positive_n}/{len(raw)} "
        f"({positive_n / len(raw) * 100:.1f}%)"
    )

    low_capture = sum(
        1
        for x in positive_realization
        if x < 40
    )

    if positive_realization:
        print(
            f"positive realization <40%: "
            f"{low_capture}/"
            f"{len(positive_realization)} "
            f"({low_capture / len(positive_realization) * 100:.1f}%)"
        )


# ============================================================
# ENTRY / EXIT DIAGNOSTICS
# ============================================================

def analyze_entry_exit_quality(rows):
    pop = [
        r
        for r in rows
        if (
            is_trading_outcome(r)
            and get_mfe(r) is not None
            and get_mae(r) is not None
        )
    ]

    if not pop:
        return None

    deep_mae_wins = [
        r
        for r in pop
        if (
            get_forward_return(r, 60)
            is not None
            and get_forward_return(r, 60) >= 1.0
            and get_mae(r) <= -2.0
        )
    ]

    bad_entry = [
        r
        for r in pop
        if (
            get_mfe(r) < 1.0
            and get_mae(r) <= -1.5
        )
    ]

    no_follow = [
        r
        for r in pop
        if (
            get_mfe(r) < 1.0
            and get_mae(r) > -1.0
        )
    ]

    good_entry_poor_realization = []

    for r in pop:
        mfe = get_mfe(r)

        strategy = numeric(
            r.get(
                "strategy_pnl_pct"
            )
        )

        if (
            mfe is None
            or mfe < 1.5
            or strategy is None
            or mfe <= 0
        ):
            continue

        realization = (
            strategy / mfe
        )

        if realization < 0.40:
            good_entry_poor_realization.append(
                r
            )

    good_entry_good_hold = []

    for r in pop:
        mfe = get_mfe(r)

        strategy = numeric(
            r.get(
                "strategy_pnl_pct"
            )
        )

        if (
            mfe is None
            or mfe < 1.5
            or strategy is None
            or mfe <= 0
        ):
            continue

        realization = (
            strategy / mfe
        )

        if realization >= 0.40:
            good_entry_good_hold.append(
                r
            )

    print(
        "\n=== ENTRY / EXIT QUALITY ==="
    )

    print(
        f"TRADING population: "
        f"{len(pop)}"
    )

    print(
        f"DEEP MAE WIN "
        f"(r60 >= +1% AND MAE <= -2%): "
        f"{len(deep_mae_wins)}"
    )

    print(
        f"BAD ENTRY "
        f"(MFE < +1% AND MAE <= -1.5%): "
        f"{len(bad_entry)}"
    )

    print(
        f"NO FOLLOW-THROUGH "
        f"(MFE < +1% AND MAE > -1%): "
        f"{len(no_follow)}"
    )

    print(
        f"GOOD ENTRY / POOR REALIZATION "
        f"(MFE >= +1.5%, raw realization <40%): "
        f"{len(good_entry_poor_realization)}"
    )

    print(
        f"GOOD ENTRY / GOOD REALIZATION "
        f"(MFE >= +1.5%, raw realization >=40%): "
        f"{len(good_entry_good_hold)}"
    )

    return {
        "deep_mae_wins": deep_mae_wins,
        "bad_entry": bad_entry,
        "no_follow": no_follow,
        "good_entry_poor_realization":
            good_entry_poor_realization,
        "good_entry_good_hold":
            good_entry_good_hold,
    }


# ============================================================
# EXIT REASON DIAGNOSTICS
# ============================================================

def analyze_exit_reason(rows, reason):
    pop = [
        r
        for r in rows
        if (
            is_trading_outcome(r)
            and r.get(
                "exit_reason"
            ) == reason
        )
    ]

    if not pop:
        return

    pnl = [
        numeric(
            r.get(
                "strategy_pnl_pct"
            )
        )
        for r in pop
        if numeric(
            r.get(
                "strategy_pnl_pct"
            )
        ) is not None
    ]

    mfe = [
        get_mfe(r)
        for r in pop
        if get_mfe(r) is not None
    ]

    mae = [
        get_mae(r)
        for r in pop
        if get_mae(r) is not None
    ]

    print(
        f"\n=== EXIT: {reason} ==="
    )

    print(
        f"n={len(pop)}"
    )

    if pnl:
        print(
            f"strategy: "
            f"median={st.median(pnl):+.2f}% "
            f"mean={st.mean(pnl):+.2f}%"
        )

    if mfe:
        print(
            f"MFE: "
            f"median={st.median(mfe):+.2f}% "
            f"mean={st.mean(mfe):+.2f}%"
        )

    if mae:
        print(
            f"MAE: "
            f"median={st.median(mae):+.2f}% "
            f"mean={st.mean(mae):+.2f}%"
        )


# ============================================================
# MARKET HISTORY
# ============================================================

def parse_timestamp(value):
    """
    Приводит timestamp к Unix seconds.

    Поддерживает:
        seconds
        milliseconds
        ISO-8601 strings
    """

    n = numeric(value)

    if n is not None:
        # milliseconds
        if n > 10_000_000_000:
            return n / 1000.0

        return n

    if not isinstance(value, str):
        return None

    value = value.strip()

    if not value:
        return None

    # Попытка numeric string.
    n = numeric(value)

    if n is not None:
        if n > 10_000_000_000:
            return n / 1000.0

        return n

    # ISO-8601.
    try:
        from datetime import datetime

        text = value.replace(
            "Z",
            "+00:00",
        )

        dt = datetime.fromisoformat(
            text
        )

        return dt.timestamp()

    except Exception:
        return None


def history_symbol(row):
    return (
        row.get("symbol")
        or row.get("ticker")
        or row.get("pair")
        or row.get("instrument")
        or row.get("contract")
    )


def history_timestamp(row):
    for key in (
        "ts",
        "timestamp",
        "time",
        "timestamp_ms",
        "ts_ms",
        "datetime",
        "date",
    ):
        value = row.get(key)

        if value is None:
            continue

        parsed = parse_timestamp(
            value
        )

        if parsed is not None:
            return parsed

    return None


def history_price(row):
    for key in (
        "price",
        "close",
        "last_price",
        "mark_price",
        "mid_price",
    ):
        value = numeric(
            row.get(key)
        )

        if value is not None:
            return value

    return None


def load_market_history(path):
    """
    Загружает market_history.jsonl.

    В отличие от json.load/read_text целиком,
    файл обрабатывается построчно.

    Возвращает:

        {
            symbol: [
                (timestamp, price),
                ...
            ]
        }

    Некорректные строки пропускаются.
    """

    if not path.exists():
        return {}, 0, False

    history = defaultdict(list)
    skipped = 0
    total = 0

    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as fh:
            for ln in fh:
                ln = ln.strip()

                if not ln:
                    continue

                total += 1

                try:
                    row = json.loads(ln)
                except json.JSONDecodeError:
                    skipped += 1
                    continue

                if not isinstance(
                    row,
                    dict,
                ):
                    skipped += 1
                    continue

                symbol = history_symbol(
                    row
                )

                ts = history_timestamp(
                    row
                )

                price = history_price(
                    row
                )

                if (
                    not symbol
                    or ts is None
                    or price is None
                    or price <= 0
                ):
                    skipped += 1
                    continue

                history[
                    str(symbol)
                ].append(
                    (
                        ts,
                        price,
                    )
                )

    except Exception as exc:
        print(
            f"⚠ Не удалось прочитать "
            f"{path.name}: {exc}"
        )
        return {}, skipped, True

    for symbol in history:
        history[symbol].sort(
            key=lambda x: x[0]
        )

    print(
        "\nMARKET HISTORY LOADER"
    )

    print(
        f"  file: {path.name}"
    )

    print(
        f"  parsed rows: "
        f"{sum(len(v) for v in history.values())}"
    )

    print(
        f"  symbols: "
        f"{len(history)}"
    )

    if skipped:
        print(
            f"  skipped rows: {skipped}"
        )

    return (
        history,
        skipped,
        True,
    )


def nearest_history_price(
    history,
    symbol,
    target_ts,
    max_gap_sec=MAX_HISTORY_GAP_SEC,
):
    """
    Находит ближайший market snapshot к target_ts.

    Не использует цену, если ближайший snapshot
    слишком далеко от target.

    Возвращает:
        price
        actual_ts
        gap_sec

    либо:
        None, None, None
    """

    if (
        not history
        or symbol is None
        or target_ts is None
    ):
        return None, None, None

    rows = history.get(
        str(symbol)
    )

    if not rows:
        return None, None, None

    # Бинарный поиск без дополнительной зависимости.
    lo = 0
    hi = len(rows)

    while lo < hi:
        mid = (lo + hi) // 2

        if rows[mid][0] < target_ts:
            lo = mid + 1
        else:
            hi = mid

    candidates = []

    if lo < len(rows):
        candidates.append(
            rows[lo]
        )

    if lo > 0:
        candidates.append(
            rows[lo - 1]
        )

    if not candidates:
        return None, None, None

    best = min(
        candidates,
        key=lambda x:
        abs(x[0] - target_ts),
    )

    actual_ts, price = best

    gap = abs(
        actual_ts - target_ts
    )

    if gap > max_gap_sec:
        return None, None, None

    return (
        price,
        actual_ts,
        gap,
    )


def get_exit_ts(row):
    return first_numeric(
        row,
        "exit_ts",
        "closed_ts",
        "close_ts",
    )


def get_exit_price(row):
    return first_numeric(
        row,
        "exit_price",
        "close_price",
        "closed_price",
    )


def analyze_post_exit(rows, history):
    """
    Настоящий post-exit analysis.

    Для каждой trading-сделки:

        exit
          ↓
        +30m
        +60m
        +120m

    Считается return от фактической exit price:

        (future_price / exit_price - 1) * 100

    Для LONG.

    Это не заменяет return_60m от entry.

    Это отдельный вопрос:

        "Что произошло после того,
         как strategy уже вышла?"
    """

    print(
        "\n=== POST-EXIT ANALYSIS ==="
    )

    if not history:
        print(
            "  market_history.jsonl "
            "недоступен или пуст."
        )

        print(
            "  Post-exit analysis "
            "не рассчитывается."
        )

        return None

    trading = [
        r
        for r in rows
        if is_trading_outcome(r)
    ]

    if not trading:
        print(
            "  trading population = 0"
        )
        return None

    stats = {
        h: []
        for h in POST_EXIT_HORIZONS
    }

    details = []

    missing_exit_ts = 0
    missing_exit_price = 0
    missing_history = 0

    for r in trading:
        exit_ts = get_exit_ts(r)
        exit_price = get_exit_price(r)

        if exit_ts is None:
            missing_exit_ts += 1
            continue

        if (
            exit_price is None
            or exit_price <= 0
        ):
            missing_exit_price += 1
            continue

        symbol = (
            r.get("symbol")
            or r.get("ticker")
        )

        if not symbol:
            missing_history += 1
            continue

        item = {
            "row": r,
            "symbol": symbol,
            "exit_ts": exit_ts,
            "exit_price": exit_price,
            "post": {},
        }

        found_any = False

        for h in POST_EXIT_HORIZONS:
            target_ts = (
                exit_ts
                + h * 60
            )

            price, actual_ts, gap = (
                nearest_history_price(
                    history,
                    symbol,
                    target_ts,
                )
            )

            if (
                price is None
                or actual_ts is None
            ):
                continue

            ret = (
                price
                / exit_price
                - 1.0
            ) * 100.0

            item["post"][h] = {
                "price": price,
                "actual_ts": actual_ts,
                "gap_sec": gap,
                "return_pct": ret,
            }

            stats[h].append(
                ret
            )

            found_any = True

        if found_any:
            details.append(item)
        else:
            missing_history += 1

    print(
        f"  trading population: "
        f"{len(trading)}"
    )

    print(
        f"  missing exit_ts: "
        f"{missing_exit_ts}"
    )

    print(
        f"  missing exit_price: "
        f"{missing_exit_price}"
    )

    print(
        f"  no usable history: "
        f"{missing_history}"
    )

    print(
        "\n  POST-EXIT RETURNS"
    )

    for h in POST_EXIT_HORIZONS:
        values = stats[h]

        if not values:
            print(
                f"  +{h}m: n=0"
            )
            continue

        wr = winrates(
            values
        )

        print(
            f"  +{h}m: "
            f"n={len(values):>3} "
            f"median={st.median(values):+.2f}% "
            f"mean={st.mean(values):+.2f}% "
            f"win≥0={wr[0.0]:.0f}% "
            f"win≥1={wr[1.0]:.0f}%"
        )

    # --------------------------------------------------------
    # PREMATURE EXIT
    # --------------------------------------------------------

    premature = []

    for item in details:
        r = item["row"]

        post60 = (
            item["post"]
            .get(60)
        )

        strategy = numeric(
            r.get(
                "strategy_pnl_pct"
            )
        )

        if (
            post60 is None
            or strategy is None
        ):
            continue

        post60_ret = post60[
            "return_pct"
        ]

        if post60_ret < 1.0:
            continue

        # Сделка закрылась с результатом хуже,
        # чем последующее движение от exit.
        if post60_ret > strategy:
            premature.append(
                item
            )

    print(
        "\n  PREMATURE EXIT — "
        "evidence-based diagnostic"
    )

    print(
        "  criterion: "
        "post-exit +60m >= +1% "
        "AND post-exit return > strategy PnL"
    )

    print(
        f"  cases: "
        f"{len(premature)}"
    )

    if details:
        print(
            f"  history-covered trades: "
            f"{len(details)}"
        )

    # --------------------------------------------------------
    # EXAMPLES
    # --------------------------------------------------------

    if premature:
        print(
            "\n  TOP PREMATURE EXIT examples:"
        )

        ranked = sorted(
            premature,
            key=lambda x:
            x["post"][60]["return_pct"],
            reverse=True,
        )

        for item in ranked[:TOP_N]:
            r = item["row"]

            post30 = (
                item["post"]
                .get(30)
            )

            post60 = (
                item["post"]
                .get(60)
            )

            post120 = (
                item["post"]
                .get(120)
            )

            print(
                f"  {r.get('symbol', '?'):12} "
                f"strategy={fnum(r.get('strategy_pnl_pct')):>8} "
                f"exit={r.get('exit_reason')} "
                f"+30={fnum(post30['return_pct']) if post30 else '—':>8} "
                f"+60={fnum(post60['return_pct']) if post60 else '—':>8} "
                f"+120={fnum(post120['return_pct']) if post120 else '—':>8}"
            )

    return {
        "stats": stats,
        "details": details,
        "premature": premature,
    }


# ============================================================
# TOP TRADES
# ============================================================

def top_signals(
    rows,
    label,
    key,
    reverse=True,
):
    have = [
        r
        for r in rows
        if numeric(
            r.get(key)
        ) is not None
    ]

    have.sort(
        key=lambda r:
        numeric(
            r.get(key)
        ),
        reverse=reverse,
    )

    print(
        f"\n=== {label} "
        f"(по {key}) ==="
    )

    for r in have[:TOP_N]:
        print(
            f"  {r.get('symbol', '?'):12} "
            f"r60={fnum(get_forward_return(r, 60)):>8}  "
            f"strat={fnum(r.get('strategy_pnl_pct')):>8}  "
            f"MFE={fnum(get_mfe(r)):>8}  "
            f"MAE={fnum(get_mae(r)):>8}  "
            f"mom={r.get('entry_momentum')}  "
            f"cvd_m={r.get('entry_cvd_momentum')}  "
            f"path={r.get('entry_path')}  "
            f"{r.get('entry_pattern')}  "
            f"{r.get('entry_earliness_label')}  "
            f"exit={r.get('exit_reason')}/"
            f"{r.get('exit_state')}  "
            f"<60m={r.get('closed_before_60m')}"
        )


def top_divergence(rows):
    have = [
        r
        for r in rows
        if (
            get_forward_return(
                r,
                60,
            ) is not None
            and numeric(
                r.get(
                    "strategy_pnl_pct"
                )
            ) is not None
        )
    ]

    have.sort(
        key=lambda r:
        get_forward_return(
            r,
            60,
        )
        - numeric(
            r.get(
                "strategy_pnl_pct"
            )
        ),
        reverse=True,
    )

    print(
        "\n=== TOP расхождение: "
        "signal outcome vs strategy ==="
    )

    for r in have[:TOP_N]:
        r60 = get_forward_return(
            r,
            60,
        )

        strat = numeric(
            r.get(
                "strategy_pnl_pct"
            )
        )

        diff = (
            r60
            - strat
        )

        print(
            f"  {r.get('symbol', '?'):12} "
            f"r60={r60:+6.1f}%  "
            f"strat={strat:+6.1f}%  "
            f"Δ={diff:+6.1f}%  "
            f"MFE={fnum(get_mfe(r)):>8}  "
            f"MAE={fnum(get_mae(r)):>8}  "
            f"hold={r.get('hold_min')}м  "
            f"exit={r.get('exit_reason')}"
        )


# ============================================================
# EXAMPLES
# ============================================================

def print_examples(title, rows):
    print(title)

    if not rows:
        print("  нет")
        return

    for r in rows[:TOP_N]:
        print(
            f"  {r.get('symbol', '?'):12} "
            f"r60={fnum(get_forward_return(r, 60)):>8} "
            f"MFE={fnum(get_mfe(r)):>8} "
            f"MAE={fnum(get_mae(r)):>8} "
            f"strat={fnum(r.get('strategy_pnl_pct')):>8} "
            f"exit={r.get('exit_reason')}"
        )


# ============================================================
# RE-ENTRY / CHURN
# ============================================================

def analyze_reentries(crypto):
    rows = [
        r
        for r in crypto
        if r.get("entry_ts") is not None
    ]

    by_symbol = defaultdict(list)

    for r in rows:
        by_symbol[
            r.get("symbol")
        ].append(r)

    repeated_symbols = []

    for symbol, trades in by_symbol.items():
        trades.sort(
            key=lambda x:
            x.get(
                "entry_ts",
                0,
            )
        )

        if len(trades) >= 2:
            repeated_symbols.append(
                (
                    symbol,
                    trades,
                )
            )

    print(
        "\n=== RE-ENTRY / CHURN ==="
    )

    print(
        f"symbols with >=2 trades: "
        f"{len(repeated_symbols)}"
    )

    if not repeated_symbols:
        return

    intervals = []

    for symbol, trades in repeated_symbols:
        for prev, curr in zip(
            trades,
            trades[1:],
        ):
            prev_exit = prev.get(
                "exit_ts"
            )

            curr_entry = curr.get(
                "entry_ts"
            )

            if (
                prev_exit is not None
                and curr_entry is not None
                and curr_entry >= prev_exit
            ):
                intervals.append(
                    (
                        curr_entry
                        - prev_exit
                    ) / 60
                )

    if intervals:
        print(
            f"re-entry interval: "
            f"median={st.median(intervals):.1f}м "
            f"mean={st.mean(intervals):.1f}м "
            f"min={min(intervals):.1f}м"
        )

    print(
        "TOP repeated symbols:"
    )

    for symbol, trades in sorted(
        repeated_symbols,
        key=lambda x:
        len(x[1]),
        reverse=True,
    )[:10]:
        print(
            f"  {symbol:12} "
            f"trades={len(trades)}"
        )


# ============================================================
# UNENTERED HELPERS
# ============================================================

def unentered_quality_label(row):
    quality = row.get(
        "quality"
    )

    if isinstance(
        quality,
        dict,
    ):
        return quality.get(
            "label"
        )

    return row.get(
        "quality_label"
    )


def unentered_asset_class(row):
    return row.get(
        "asset_class",
        "crypto",
    )


def unentered_detection_ts(row):
    return first_numeric(
        row,
        "detect_ts",
        "entry_ts",
        "ts",
    )


def unentered_symbol(row):
    return (
        row.get("symbol")
        or row.get("ticker")
        or "?"
    )


def unentered_forward_60(row):
    return get_forward_return(
        row,
        60,
    )


def unentered_forward_120(row):
    return get_forward_return(
        row,
        120,
    )


def unentered_quality_good(row):
    """
    Отдельная diagnostic classification.

    ВАЖНО:

    quality.label == good

    НЕ является capture-rate criterion.
    """

    label = unentered_quality_label(
        row
    )

    return label == "good"


# ============================================================
# CAPTURE TIME / ELIGIBILITY AUDIT
# ============================================================

def get_signal_ts(row):
    """
    Наиболее ранняя timestamp, которая может представлять
    момент формирования/обнаружения сигнала.

    Приоритет:

        signal_ts
        detect_ts
        signal_detect_ts
        candidate_ts
        entry_ts

    ВАЖНО:

    Это только AUDIT helper.

    Он НЕ изменяет существующий operational capture rate.
    """

    return first_numeric(
        row,
        "signal_ts",
        "detect_ts",
        "signal_detect_ts",
        "candidate_ts",
        "entry_ts",
    )


def get_timestamp_source(row, entered=True):
    """
    Возвращает название поля, из которого фактически
    взят timestamp для capture-time audit.
    """

    if entered:
        keys = (
            "signal_ts",
            "detect_ts",
            "signal_detect_ts",
            "candidate_ts",
            "entry_ts",
        )
    else:
        keys = (
            "detect_ts",
            "signal_ts",
            "entry_ts",
            "ts",
        )

    for key in keys:
        if numeric(row.get(key)) is not None:
            return key

    return "missing"


def analyze_capture_time_symmetry(
    trades,
    unentered_analysis,
    cutoff_h=24,
):
    """
    Проверяет симметрию временной точки между ENTERED
    и MISSED.

    Это AUDIT.

    Он НЕ меняет operational capture rate.

    ENTERED:

        signal_ts / detect_ts / candidate_ts / entry_ts
            ↓
        return_60m

    MISSED:

        detect_ts / signal_ts / entry_ts / ts
            ↓
        forward_60m

    Основной вопрос:

        одинаково ли определена исходная временная точка?

    Дополнительно измеряем:

        signal -> actual entry delay
    """

    now = time.time()

    cutoff = (
        now
        - cutoff_h * 3600
    )

    entered = []
    missed = []

    for r in trades:
        if (
            r.get(
                "asset_class",
                "crypto",
            )
            != "crypto"
        ):
            continue

        ts = get_signal_ts(r)

        if ts is None or ts < cutoff:
            continue

        entered.append(r)

    for r in unentered_analysis:
        if (
            unentered_asset_class(r)
            != "crypto"
        ):
            continue

        ts = unentered_detection_ts(r)

        if ts is None or ts < cutoff:
            continue

        missed.append(r)

    print(
        "\n=== CAPTURE TIME / ELIGIBILITY AUDIT ==="
    )

    print(
        f"  analysis window: "
        f"last {cutoff_h}h"
    )

    print(
        f"  ENTERED crypto: "
        f"{len(entered)}"
    )

    print(
        f"  MISSED crypto:  "
        f"{len(missed)}"
    )

    # --------------------------------------------------------
    # ENTERED timestamp sources
    # --------------------------------------------------------

    entered_sources = defaultdict(int)

    for r in entered:
        source = get_timestamp_source(
            r,
            entered=True,
        )

        entered_sources[source] += 1

    print(
        "\n  ENTERED timestamp source:"
    )

    for key, count in sorted(
        entered_sources.items(),
        key=lambda x: (
            -x[1],
            x[0],
        ),
    ):
        print(
            f"    {key:20} "
            f"{count:>4}"
        )

    # --------------------------------------------------------
    # MISSED timestamp sources
    # --------------------------------------------------------

    missed_sources = defaultdict(int)

    for r in missed:
        source = get_timestamp_source(
            r,
            entered=False,
        )

        missed_sources[source] += 1

    print(
        "\n  MISSED timestamp source:"
    )

    for key, count in sorted(
        missed_sources.items(),
        key=lambda x: (
            -x[1],
            x[0],
        ),
    ):
        print(
            f"    {key:20} "
            f"{count:>4}"
        )

    # --------------------------------------------------------
    # +60m coverage
    # --------------------------------------------------------

    entered_forward = [
        r
        for r in entered
        if get_forward_return(
            r,
            60,
        ) is not None
    ]

    missed_forward = [
        r
        for r in missed
        if unentered_forward_60(r)
        is not None
    ]

    print(
        "\n  +60m coverage:"
    )

    if entered:
        print(
            f"    ENTERED: "
            f"{len(entered_forward)}/"
            f"{len(entered)} "
            f"("
            f"{len(entered_forward) / len(entered) * 100:.1f}%"
            f")"
        )
    else:
        print(
            "    ENTERED: 0"
        )

    if missed:
        print(
            f"    MISSED:  "
            f"{len(missed_forward)}/"
            f"{len(missed)} "
            f"("
            f"{len(missed_forward) / len(missed) * 100:.1f}%"
            f")"
        )
    else:
        print(
            "    MISSED: 0"
        )

    # --------------------------------------------------------
    # Entry delay
    #
    # Если отдельно существует signal/detect/candidate
    # timestamp и entry_ts, измеряем задержку.
    # --------------------------------------------------------

    delays = []

    delay_sources = defaultdict(int)

    for r in entered:
        signal_ts = first_numeric(
            r,
            "signal_ts",
            "detect_ts",
            "signal_detect_ts",
            "candidate_ts",
        )

        entry_ts = numeric(
            r.get("entry_ts")
        )

        if (
            signal_ts is None
            or entry_ts is None
        ):
            continue

        if entry_ts < signal_ts:
            continue

        delays.append(
            (
                entry_ts
                - signal_ts
            ) / 60.0
        )

        if r.get("signal_ts") is not None:
            delay_sources["signal_ts"] += 1
        elif r.get("detect_ts") is not None:
            delay_sources["detect_ts"] += 1
        elif r.get("signal_detect_ts") is not None:
            delay_sources["signal_detect_ts"] += 1
        elif r.get("candidate_ts") is not None:
            delay_sources["candidate_ts"] += 1

    print(
        "\n  ENTERED signal → execution delay:"
    )

    if delays:
        print(
            f"    n={len(delays)} "
            f"median={st.median(delays):.2f}m "
            f"mean={st.mean(delays):.2f}m "
            f"p25={pct(delays, 25):.2f}m "
            f"p75={pct(delays, 75):.2f}m "
            f"max={max(delays):.2f}m"
        )

        print(
            "    source:"
        )

        for key, count in sorted(
            delay_sources.items(),
            key=lambda x: (
                -x[1],
                x[0],
            ),
        ):
            print(
                f"      {key:18} "
                f"{count:>4}"
            )

    else:
        print(
            "    signal_ts отдельно от entry_ts "
            "не найден."
        )

    # --------------------------------------------------------
    # Same timestamp semantics
    # --------------------------------------------------------

    entered_signal_count = sum(
        1
        for r in entered
        if (
            r.get("signal_ts") is not None
            or r.get("detect_ts") is not None
            or r.get("signal_detect_ts") is not None
            or r.get("candidate_ts") is not None
        )
    )

    missed_detect_count = sum(
        1
        for r in missed
        if (
            r.get("detect_ts") is not None
            or r.get("signal_ts") is not None
        )
    )

    print(
        "\n  TIMESTAMP SYMMETRY:"
    )

    print(
        f"    ENTERED with pre-entry signal timestamp: "
        f"{entered_signal_count}/{len(entered)}"
        if entered
        else
        "    ENTERED: 0"
    )

    print(
        f"    MISSED with detection timestamp: "
        f"{missed_detect_count}/{len(missed)}"
        if missed
        else
        "    MISSED: 0"
    )

    # --------------------------------------------------------
    # Verdict
    # --------------------------------------------------------

    print(
        "\n  VERDICT:"
    )

    if (
        entered
        and missed
        and entered_signal_count == len(entered)
        and missed_detect_count == len(missed)
    ):
        print(
            "    У всех текущих записей есть "
            "отдельная signal/detection timestamp."
        )

        print(
            "    Следующий шаг — проверить семантику "
            "этих timestamp в producer-коде."
        )

        print(
            "    operational capture пока НЕ меняем."
        )

    elif (
        entered_signal_count > 0
        or missed_detect_count > 0
    ):
        print(
            "    Частично есть отдельные "
            "signal/detection timestamp."
        )

        print(
            "    Нужна проверка producer-кода "
            "перед изменением capture."
        )

        print(
            "    operational capture пока НЕ меняем."
        )

    else:
        print(
            "    НЕДОСТАТОЧНО ДАННЫХ для "
            "симметричного capture."
        )

        print(
            "    Текущий operational capture "
            "не менять."
        )

    return {
        "entered": entered,
        "missed": missed,
        "entered_forward": entered_forward,
        "missed_forward": missed_forward,
        "delays": delays,
        "entered_sources": dict(
            entered_sources
        ),
        "missed_sources": dict(
            missed_sources
        ),
    }


# ============================================================
# UNENTERED / MISSED
# ============================================================

def analyze_unentered(
    unentered_analysis,
    unentered_candidates,
    trades,
    cutoff_h=24,
):
    """
    UNENTERED / MISSED analysis.

    finalized:
        finalized missed candidates.

    pending:
        current/pending candidates.

    pending НЕ участвуют в capture.

    Operational capture:

        ENTERED GOOD:
            return_60m >= +1%

        MISSED GOOD:
            forward_60m >= +1%

    То есть ENTERED и MISSED используют
    одинаковое performance definition.

    quality.label == good выводится отдельно
    как diagnostic quality classification.
    """

    now = time.time()

    cutoff = (
        now
        - cutoff_h * 3600
    )

    finalized_exists = (
        UNENTERED_ANALYSIS.exists()
    )

    candidates_exists = (
        UNENTERED_CANDIDATES.exists()
    )

    print(
        f"\n=== UNENTERED / MISSED "
        f"(последние {cutoff_h}ч) ==="
    )

    print(
        f"  finalized analysis file: "
        f"{'YES' if finalized_exists else 'NO'}"
    )

    print(
        f"  pending candidates file: "
        f"{'YES' if candidates_exists else 'NO'}"
    )

    finalized = [
        u
        for u in unentered_analysis
        if (
            unentered_detection_ts(u)
            is not None
            and unentered_detection_ts(u)
            >= cutoff
            and unentered_asset_class(u)
            == "crypto"
        )
    ]

    pending = [
        u
        for u in unentered_candidates
        if (
            unentered_detection_ts(u)
            is not None
            and unentered_detection_ts(u)
            >= cutoff
            and unentered_asset_class(u)
            == "crypto"
        )
    ]

    quality_good = [
        u
        for u in finalized
        if unentered_quality_good(u)
    ]

    print(
        f"  finalized candidates: "
        f"{len(finalized)}"
    )

    print(
        f"  quality.label='good': "
        f"{len(quality_good)}"
    )

    print(
        f"  pending candidates:   "
        f"{len(pending)}"
    )

    if pending:
        print(
            "  ℹ pending candidates "
            "НЕ участвуют в capture rate."
        )

    # --------------------------------------------------------
    # ENTERED PERFORMANCE
    # --------------------------------------------------------

    entered_with_forward = []
    entered_good = []

    for t in trades:
        entry_ts = numeric(
            t.get("entry_ts")
        )

        if entry_ts is None:
            continue

        if entry_ts < cutoff:
            continue

        if (
            t.get(
                "asset_class",
                "crypto",
            )
            != "crypto"
        ):
            continue

        r60 = get_forward_return(
            t,
            60,
        )

        if r60 is None:
            continue

        entered_with_forward.append(
            t
        )

        if (
            r60
            >= CAPTURE_GOOD_RETURN
        ):
            entered_good.append(
                t
            )

    # --------------------------------------------------------
    # MISSED PERFORMANCE
    # --------------------------------------------------------

    missed_with_forward = []
    missed_good = []

    for u in finalized:
        forward60 = (
            unentered_forward_60(u)
        )

        if forward60 is None:
            continue

        missed_with_forward.append(
            u
        )

        if (
            forward60
            >= CAPTURE_GOOD_RETURN
        ):
            missed_good.append(
                u
            )

    # --------------------------------------------------------
    # FILE CHECK
    # --------------------------------------------------------

    if not finalized_exists:
        print(
            "\nOPERATIONAL MOVEMENT CAPTURE:"
        )

        print(
            "  НЕДОСТАТОЧНО ДАННЫХ."
        )

        print(
            "  unentered_analysis.jsonl "
            "отсутствует."
        )

        return

    # --------------------------------------------------------
    # OPERATIONAL CAPTURE
    # --------------------------------------------------------

    total_good = (
        len(entered_good)
        + len(missed_good)
    )

    print(
        "\nOPERATIONAL MOVEMENT CAPTURE"
    )

    print(
        "  Единое определение GOOD:"
    )

    print(
        f"    ENTERED: "
        f"return_60m >= "
        f"+{CAPTURE_GOOD_RETURN:.1f}%"
    )

    print(
        f"    MISSED:  "
        f"forward_60m >= "
        f"+{CAPTURE_GOOD_RETURN:.1f}%"
    )

    print(
        "  ВНИМАНИЕ: это descriptive "
        "operational capture,"
    )

    print(
        "  НЕ causal signal capture."
    )

    print(
        f"  entered с forward_60m: "
        f"{len(entered_with_forward)}"
    )

    print(
        f"  entered GOOD: "
        f"{len(entered_good)}"
    )

    print(
        f"  missed с forward_60m: "
        f"{len(missed_with_forward)}"
    )

    print(
        f"  missed GOOD: "
        f"{len(missed_good)}"
    )

    if total_good <= 0:
        print(
            "  НЕДОСТАТОЧНО ДАННЫХ."
        )
        return

    capture_rate = (
        len(entered_good)
        / total_good
        * 100
    )

    print(
        f"  всего GOOD: "
        f"{total_good}"
    )

    print(
        f"  operational capture rate: "
        f"{capture_rate:.1f}%"
    )

    if (
        len(entered_good)
        < MIN_CAPTURE_SAMPLE
        or len(missed_good)
        < MIN_CAPTURE_SAMPLE
    ):
        print(
            f"  ⚠ low sample: "
            f"желательно >= "
            f"{MIN_CAPTURE_SAMPLE} "
            f"GOOD в каждой группе."
        )

    # --------------------------------------------------------
    # QUALITY LABEL ISOLATED
    # --------------------------------------------------------

    print(
        "\nMISSED QUALITY LABEL "
        "(diagnostic only):"
    )

    print(
        f"  quality.label='good': "
        f"{len(quality_good)}"
    )

    if finalized:
        print(
            f"  доля среди finalized: "
            f"{len(quality_good) / len(finalized) * 100:.1f}%"
        )

    print(
        "  ⚠ quality.label НЕ входит "
        "в denominator capture."
    )

    # --------------------------------------------------------
    # MISSED FORWARD PERFORMANCE
    # --------------------------------------------------------

    missed60 = [
        unentered_forward_60(u)
        for u in missed_good
        if unentered_forward_60(u)
        is not None
    ]

    missed120 = [
        unentered_forward_120(u)
        for u in missed_good
        if unentered_forward_120(u)
        is not None
    ]

    print(
        "\nMISSED GOOD — "
        "forward outcome:"
    )

    print(
        f"  forward_60m: "
        f"{len(missed60)}/"
        f"{len(missed_good)}"
    )

    if missed60:
        print(
            f"    median={st.median(missed60):+.2f}% "
            f"mean={st.mean(missed60):+.2f}% "
            f"win≥1="
            f"{sum(x >= 1 for x in missed60) / len(missed60) * 100:.1f}%"
        )

    print(
        f"  forward_120m: "
        f"{len(missed120)}/"
        f"{len(missed_good)}"
    )

    if missed120:
        print(
            f"    median={st.median(missed120):+.2f}% "
            f"mean={st.mean(missed120):+.2f}% "
            f"win≥1="
            f"{sum(x >= 1 for x in missed120) / len(missed120) * 100:.1f}%"
        )

    # --------------------------------------------------------
    # FAIL POINT
    # --------------------------------------------------------

    by_condition = defaultdict(
        lambda: {
            "count": 0,
            "deficits": [],
            "estimated": 0,
            "observed": 0,
        }
    )

    by_stage = defaultdict(int)

    for u in missed_good:
        fp = u.get(
            "fail_point",
            {},
        )

        if not isinstance(
            fp,
            dict,
        ):
            fp = {}

        stage = fp.get(
            "stage",
            "unknown",
        )

        condition = fp.get(
            "condition",
            "unknown",
        )

        deficit = numeric(
            fp.get("deficit")
        )

        key = (
            f"{stage}:{condition}"
        )

        item = by_condition[key]

        item["count"] += 1

        if deficit is not None:
            item["deficits"].append(
                deficit
            )

        if (
            fp.get("confidence")
            == "observed"
        ):
            item["observed"] += 1
        else:
            item["estimated"] += 1

        by_stage[stage] += 1

    print(
        "\nFAIL-POINT — диагностический, "
        "НЕ причинный:"
    )

    if not by_condition:
        print(
            "  Нет fail_point данных."
        )

    else:
        print(
            f"  {'условие':45} "
            f"{'count':>6} "
            f"{'obs':>5} "
            f"{'est':>5} "
            f"{'avg_deficit':>12}"
        )

        sorted_cond = sorted(
            by_condition.items(),
            key=lambda x:
            x[1]["count"],
            reverse=True,
        )

        for key, item in sorted_cond[:20]:
            avg_def = (
                st.mean(
                    item["deficits"]
                )
                if item["deficits"]
                else None
            )

            avg_str = (
                f"{avg_def:11.3f}"
                if avg_def is not None
                else "        n/a"
            )

            print(
                f"  {key:45} "
                f"{item['count']:>6} "
                f"{item['observed']:>5} "
                f"{item['estimated']:>5} "
                f"{avg_str}"
            )

            if (
                item["count"]
                < LOW_SAMPLE_WARNING
            ):
                print(
                    f"      ⚠ low sample "
                    f"(n={item['count']})"
                )

    print(
        "\nПо стадии:"
    )

    for stage, count in sorted(
        by_stage.items(),
        key=lambda x:
        x[1],
        reverse=True,
    ):
        print(
            f"  {stage:45} "
            f"{count:>6}"
        )


# ============================================================
# DATA QUALITY AUDIT
# ============================================================

def analyze_data_quality(
    rows,
    unentered_analysis,
    unentered_candidates,
):
    print(
        "\n=== DATA QUALITY AUDIT ==="
    )

    if not rows:
        print(
            "  trades: 0"
        )
        return

    duplicate_ids = 0
    seen_ids = set()

    for r in rows:
        rid = (
            r.get("trade_id")
            or r.get("id")
        )

        if rid is None:
            continue

        if rid in seen_ids:
            duplicate_ids += 1
        else:
            seen_ids.add(rid)

    if seen_ids:
        print(
            f"  trade ids: "
            f"{len(seen_ids)}"
        )

        print(
            f"  duplicate trade ids: "
            f"{duplicate_ids}"
        )

    else:
        print(
            "  trade ids: "
            "нет поля trade_id/id"
        )

    missing_entry_ts = sum(
        1
        for r in rows
        if r.get(
            "entry_ts"
        ) is None
    )

    missing_strategy = sum(
        1
        for r in rows
        if numeric(
            r.get(
                "strategy_pnl_pct"
            )
        ) is None
    )

    missing_return60 = sum(
        1
        for r in rows
        if get_forward_return(
            r,
            60,
        ) is None
    )

    print(
        f"  missing entry_ts: "
        f"{missing_entry_ts}/{len(rows)}"
    )

    print(
        f"  missing strategy_pnl_pct: "
        f"{missing_strategy}/{len(rows)}"
    )

    print(
        f"  missing return_60m: "
        f"{missing_return60}/{len(rows)}"
    )

    print(
        f"  unentered_analysis rows: "
        f"{len(unentered_analysis)}"
    )

    print(
        f"  unentered_candidates rows: "
        f"{len(unentered_candidates)}"
    )



# ============================================================
# EXTENDED EXIT / ENTRY / CHURN AUDITS
# ============================================================

TRAJECTORY_POINTS_MIN = [5, 10, 15, 30, 45, 60, 90, 120]
CHURN_INTERVAL_BUCKETS = [30, 60, 120, 240]


def get_entry_ts(row):
    return first_numeric(
        row,
        "entry_ts",
        "open_ts",
        "opened_ts",
    )


def get_entry_price(row):
    return first_numeric(
        row,
        "entry_price",
        "open_price",
        "opened_price",
        "price_at_entry",
    )


def history_return_at(history, symbol, base_ts, base_price, minutes):
    if base_ts is None or base_price is None or base_price <= 0:
        return None
    price, actual_ts, gap = nearest_history_price(
        history,
        symbol,
        base_ts + minutes * 60,
    )
    if price is None:
        return None
    return (price / base_price - 1.0) * 100.0


def history_price_at(history, symbol, ts):
    price, actual_ts, gap = nearest_history_price(
        history,
        symbol,
        ts,
    )
    return price


def get_symbol(row):
    return row.get("symbol") or row.get("ticker") or row.get("pair")


def analyze_stop_loss_trajectory(rows, history):
    """
    STOP_LOSS trajectory audit.

    Это исследовательский блок: он не меняет exit logic.
    Если entry_price отсутствует, цена входа берётся из market_history
    ближайшим snapshot к entry_ts.

    Смотрим:
      - путь цены от entry до STOP_LOSS;
      - MFE/MAE, независимо от записанного mfe/mae;
      - время до локального MFE/MAE;
      - наличие восстановления после stop.
    """
    print("\n=== STOP_LOSS TRAJECTORY AUDIT ===")

    stops = [
        r for r in rows
        if is_trading_outcome(r)
        and r.get("exit_reason") == "STOP_LOSS"
    ]

    print(f"  STOP_LOSS trades: {len(stops)}")
    if not stops:
        print("  Недостаточно данных.")
        return
    if not history:
        print("  market_history недоступен — trajectory не рассчитывается.")
        return

    reconstructed = []
    for r in stops:
        symbol = get_symbol(r)
        entry_ts = get_entry_ts(r)
        exit_ts = get_exit_ts(r)
        entry_price = get_entry_price(r)

        if entry_price is None and symbol and entry_ts is not None:
            entry_price = history_price_at(history, symbol, entry_ts)

        if not symbol or entry_ts is None or exit_ts is None or entry_price is None:
            continue
        if exit_ts < entry_ts or entry_price <= 0:
            continue

        points = []
        series = history.get(str(symbol), [])
        if not series:
            continue

        # Include snapshots in [entry, exit].
        for ts, price in series:
            if ts < entry_ts:
                continue
            if ts > exit_ts:
                break
            ret = (price / entry_price - 1.0) * 100.0
            points.append((ts, ret))

        if not points:
            continue

        max_point = max(points, key=lambda x: x[1])
        min_point = min(points, key=lambda x: x[1])
        reconstructed.append({
            "row": r,
            "points": points,
            "mfe": max_point[1],
            "mae": min_point[1],
            "time_to_mfe": (max_point[0] - entry_ts) / 60.0,
            "time_to_mae": (min_point[0] - entry_ts) / 60.0,
        })

    print(f"  history-covered STOP_LOSS: {len(reconstructed)}/{len(stops)}")
    if not reconstructed:
        print("  Недостаточно данных для trajectory.")
        return

    mfe = [x["mfe"] for x in reconstructed]
    mae = [x["mae"] for x in reconstructed]
    tmfe = [x["time_to_mfe"] for x in reconstructed]
    tmae = [x["time_to_mae"] for x in reconstructed]

    print(
        f"  reconstructed MFE: median={st.median(mfe):+.2f}% mean={st.mean(mfe):+.2f}%"
    )
    print(
        f"  reconstructed MAE: median={st.median(mae):+.2f}% mean={st.mean(mae):+.2f}%"
    )
    print(
        f"  time to MFE: median={st.median(tmfe):.1f}m mean={st.mean(tmfe):.1f}m"
    )
    print(
        f"  time to MAE: median={st.median(tmae):.1f}m mean={st.mean(tmae):.1f}m"
    )

    print("\n  STOP path by trajectory point:")
    for minute in TRAJECTORY_POINTS_MIN:
        values = []
        for item in reconstructed:
            r = item["row"]
            entry_ts = get_entry_ts(r)
            entry_price = get_entry_price(r)
            symbol = get_symbol(r)
            if entry_price is None and symbol and entry_ts is not None:
                entry_price = history_price_at(history, symbol, entry_ts)
            v = history_return_at(history, symbol, entry_ts, entry_price, minute)
            if v is not None:
                # Do not use a point after the actual stop.
                exit_ts = get_exit_ts(r)
                if exit_ts is not None and entry_ts + minute * 60 <= exit_ts:
                    values.append(v)
        if values:
            print(
                f"    +{minute:>3}m: n={len(values):>2} "
                f"median={st.median(values):+6.2f}% mean={st.mean(values):+6.2f}% "
                f"win≥0={winrates(values)[0.0]:4.0f}% win≥1={winrates(values)[1.0]:4.0f}%"
            )

    print("\n  Individual STOP_LOSS paths:")
    for item in reconstructed:
        r = item["row"]
        symbol = get_symbol(r) or "?"
        strategy = numeric(r.get("strategy_pnl_pct"))
        print(
            f"    {str(symbol):12} "
            f"strat={fnum(strategy):>8} "
            f"MFE={item['mfe']:+7.2f}% "
            f"MAE={item['mae']:+7.2f}% "
            f"tMFE={item['time_to_mfe']:6.1f}m "
            f"tMAE={item['time_to_mae']:6.1f}m"
        )


def analyze_exit_efficiency_by_time(rows, history):
    """
    Разделяет exit quality по hold-time и exit reason.
    Post-exit return здесь диагностический и не меняет strategy PnL.
    """
    print("\n=== EXIT EFFICIENCY BY HOLD TIME ===")
    trading = [r for r in rows if is_trading_outcome(r)]
    if not trading:
        print("  trading population = 0")
        return

    def hold_minutes(r):
        entry = get_entry_ts(r)
        exit_ts = get_exit_ts(r)
        if entry is None or exit_ts is None or exit_ts < entry:
            return None
        return (exit_ts - entry) / 60.0

    def hold_bucket(v):
        if v is None:
            return "n/a"
        for edge in CHURN_INTERVAL_BUCKETS:
            if v < edge:
                return f"<{edge}m"
        return f">={CHURN_INTERVAL_BUCKETS[-1]}m"

    groups = defaultdict(list)
    for r in trading:
        groups[(r.get("exit_reason") or "UNKNOWN", hold_bucket(hold_minutes(r)))].append(r)

    reasons = sorted({k[0] for k in groups})
    buckets = ["<30m", "<60m", "<120m", "<240m", ">=240m", "n/a"]
    print("  reason / hold bucket: n, strategy median/mean, MFE median, MAE median")
    for reason in reasons:
        for b in buckets:
            rs = groups.get((reason, b), [])
            if not rs:
                continue
            pnl = [numeric(r.get("strategy_pnl_pct")) for r in rs]
            pnl = [x for x in pnl if x is not None]
            mfe = [get_mfe(r) for r in rs if get_mfe(r) is not None]
            mae = [get_mae(r) for r in rs if get_mae(r) is not None]
            print(
                f"    {reason:16} {b:7} n={len(rs):>3} "
                f"strat_med={st.median(pnl):+6.2f}% "
                f"strat_mean={st.mean(pnl):+6.2f}% "
                f"MFE_med={st.median(mfe):+6.2f}% " if mfe else
                f"    {reason:16} {b:7} n={len(rs):>3} "
                f"strat_med={st.median(pnl):+6.2f}% "
                f"strat_mean={st.mean(pnl):+6.2f}% MFE_med=— ",
                end=""
            )
            print(f"MAE_med={st.median(mae):+6.2f}%" if mae else "MAE_med=—")

    if history:
        print("\n  Post-exit +60m by exit reason (where history exists):")
        post_by_reason = defaultdict(list)
        for r in trading:
            exit_ts = get_exit_ts(r)
            exit_price = get_exit_price(r)
            symbol = get_symbol(r)
            if exit_ts is None or exit_price is None or not symbol:
                continue
            price, _, _ = nearest_history_price(history, symbol, exit_ts + 60 * 60)
            if price is None or exit_price <= 0:
                continue
            post_by_reason[r.get("exit_reason") or "UNKNOWN"].append((price / exit_price - 1.0) * 100.0)
        for reason in sorted(post_by_reason):
            vals = post_by_reason[reason]
            print(
                f"    {reason:16} n={len(vals):>3} "
                f"median={st.median(vals):+6.2f}% mean={st.mean(vals):+6.2f}% "
                f"win≥1={winrates(vals)[1.0]:4.0f}%"
            )


def analyze_entry_quality_matrix(rows):
    """2D diagnostic matrix: PRICE_CHG24 x entry_momentum, plus CVD x price."""
    print("\n=== ENTRY-QUALITY MATRIX (diagnostic) ===")
    rows = [r for r in rows if get_forward_return(r, 60) is not None]
    if not rows:
        print("  Нет return_60m.")
        return

    price_edges = PRICE_BUCKETS
    mom_edges = MOMENTUM_BUCKETS
    cvd_edges = CVD_BUCKETS

    def key_for(r, field, edges):
        return bucket(first_numeric(r, field), edges)

    def cell(rs):
        vals = [get_forward_return(r, 60) for r in rs]
        vals = [v for v in vals if v is not None]
        if not vals:
            return "—"
        return f"{len(vals)}/{winrates(vals)[1.0]:.0f}%/{st.median(vals):+.2f}%"

    print("  cell = n / win≥1% / median r60")
    print("\n  PRICE_CHG24 × MOMENTUM")
    print(f"  {'price':12} " + " ".join(f"{bucket(None, [x]) if False else str(k):>16}" for k in []))
    mom_keys = sorted({key_for(r, "entry_momentum", mom_edges) for r in rows}, key=sort_key)
    print("  " + f"{'price':12}" + "".join(f"{k:>18}" for k in mom_keys))
    for pk in sorted({key_for(r, "entry_price_chg24", price_edges) for r in rows}, key=sort_key):
        print(
            "  " + f"{pk:12}" + "".join(
                f"{cell([r for r in rows if key_for(r, 'entry_price_chg24', price_edges) == pk and key_for(r, 'entry_momentum', mom_edges) == mk]):>18}"
                for mk in mom_keys
            )
        )

    print("\n  PRICE_CHG24 × CVD_MOMENTUM")
    cvd_keys = sorted({key_for(r, "entry_cvd_momentum", cvd_edges) for r in rows}, key=sort_key)
    print("  " + f"{'price':12}" + "".join(f"{k:>18}" for k in cvd_keys))
    for pk in sorted({key_for(r, "entry_price_chg24", price_edges) for r in rows}, key=sort_key):
        print(
            "  " + f"{pk:12}" + "".join(
                f"{cell([r for r in rows if key_for(r, 'entry_price_chg24', price_edges) == pk and key_for(r, 'entry_cvd_momentum', cvd_edges) == ck]):>18}"
                for ck in cvd_keys
            )
        )


def analyze_reentry_quality(crypto, history):
    """
    Расширенный churn audit.
    Не объявляет сделки causal continuation/new setup; только показывает
    temporal proximity, price movement and outcomes of consecutive trades.
    """
    print("\n=== RE-ENTRY QUALITY / SAME-WAVE AUDIT ===")
    by_symbol = defaultdict(list)
    for r in crypto:
        if get_entry_ts(r) is not None:
            by_symbol[get_symbol(r)].append(r)

    pairs = []
    for symbol, rs in by_symbol.items():
        rs = sorted(rs, key=get_entry_ts)
        for prev, curr in zip(rs, rs[1:]):
            prev_exit = get_exit_ts(prev)
            curr_entry = get_entry_ts(curr)
            if prev_exit is None or curr_entry is None or curr_entry < prev_exit:
                continue
            gap = (curr_entry - prev_exit) / 60.0
            prev_pnl = numeric(prev.get("strategy_pnl_pct"))
            curr_pnl = numeric(curr.get("strategy_pnl_pct"))
            price_move = None
            if history and symbol:
                p0 = get_exit_price(prev) or history_price_at(history, symbol, prev_exit)
                p1 = get_entry_price(curr) or history_price_at(history, symbol, curr_entry)
                if p0 and p1 and p0 > 0:
                    price_move = (p1 / p0 - 1.0) * 100.0
            pairs.append((symbol, prev, curr, gap, price_move, prev_pnl, curr_pnl))

    print(f"  consecutive re-entry pairs: {len(pairs)}")
    if not pairs:
        return

    for label, lo, hi in [("<30m", 0, 30), ("30–60m", 30, 60), ("60–120m", 60, 120), ("120–240m", 120, 240), (">=240m", 240, float('inf'))]:
        ps = [x for x in pairs if lo <= x[3] < hi]
        if not ps:
            continue
        curr = [x[6] for x in ps if x[6] is not None]
        moves = [x[4] for x in ps if x[4] is not None]
        print(
            f"  {label:8} n={len(ps):>3} "
            f"curr_strategy_med={st.median(curr):+6.2f}% " if curr else
            f"  {label:8} n={len(ps):>3} curr_strategy_med=— ",
            end=""
        )
        print(f"entry-vs-exit price median={st.median(moves):+6.2f}%" if moves else "entry-vs-exit price median=—")

    print("\n  Closest re-entry pairs:")
    for symbol, prev, curr, gap, move, prev_pnl, curr_pnl in sorted(pairs, key=lambda x: x[3])[:15]:
        print(
            f"    {str(symbol):12} gap={gap:6.1f}m "
            f"prev={fnum(prev_pnl):>8} curr={fnum(curr_pnl):>8} "
            f"price_exit→entry={fnum(move):>8} "
            f"prev_exit={prev.get('exit_reason')}"
        )


def analyze_capture_producer_audit(trades, unentered_analysis):
    """
    Schema-level producer audit.
    This deliberately does not change capture denominator or labels.
    """
    print("\n=== CAPTURE PRODUCER FIELD AUDIT ===")
    fields = ["signal_ts", "detect_ts", "signal_detect_ts", "candidate_ts", "entry_ts"]
    for population_name, rows in (("ENTERED", trades), ("MISSED", unentered_analysis)):
        rows = [r for r in rows if r.get("asset_class", "crypto") == "crypto"]
        print(f"  {population_name}: n={len(rows)}")
        for field in fields:
            n = sum(1 for r in rows if numeric(r.get(field)) is not None)
            print(f"    {field:18} {n:>5}/{len(rows)}")

    entered = [r for r in trades if r.get("asset_class", "crypto") == "crypto"]
    delays = {}
    for field in ("signal_ts", "detect_ts", "signal_detect_ts", "candidate_ts"):
        vals = []
        for r in entered:
            source_ts = numeric(r.get(field))
            entry_ts = get_entry_ts(r)
            if source_ts is None or entry_ts is None or entry_ts < source_ts:
                continue
            vals.append((entry_ts - source_ts) / 60.0)
        if vals:
            delays[field] = vals
    print("  ENTERED delay by raw producer field:")
    for field, vals in delays.items():
        print(
            f"    {field:18} n={len(vals):>4} "
            f"median={st.median(vals):.2f}m mean={st.mean(vals):.2f}m max={max(vals):.2f}m"
        )

    print("  VERDICT: producer semantics must be checked in monitor.py/unentered_tracker.py before capture logic is changed.")

# ============================================================
# MAIN
# ============================================================

def main():
    rows, skipped_bad_lines, trades_exists = load()

    (
        unentered_analysis,
        skipped_unentered_analysis,
        analysis_exists,
    ) = load_unentered_analysis()

    (
        unentered_candidates,
        skipped_candidates,
        candidates_exists,
    ) = load_unentered_candidates()

    if not trades_exists:
        print(
            "trades.jsonl отсутствует."
        )

        print(
            "Копим сделки."
        )

        return

    if not rows:
        print(
            "trades.jsonl пуст — "
            "копим сделки."
        )

        if skipped_bad_lines:
            print(
                f"⚠ обнаружено "
                f"{skipped_bad_lines} "
                f"битых строк."
            )

        return

    crypto = [
        r
        for r in rows
        if r.get(
            "asset_class"
        ) == "crypto"
    ]

    non_crypto = [
        r
        for r in rows
        if r.get(
            "asset_class"
        ) != "crypto"
    ]

    equity_n = sum(
        1
        for r in non_crypto
        if r.get(
            "asset_class"
        ) == "equity"
    )

    commodity_n = sum(
        1
        for r in non_crypto
        if r.get(
            "asset_class"
        ) == "commodity"
    )

    unknown_n = (
        len(non_crypto)
        - equity_n
        - commodity_n
    )

    with60 = [
        r
        for r in crypto
        if get_forward_return(
            r,
            60,
        ) is not None
    ]

    miss60 = (
        len(crypto)
        - len(with60)
    )

    closed = [
        r
        for r in crypto
        if numeric(
            r.get(
                "strategy_pnl_pct"
            )
        ) is not None
    ]

    trading = strategy_population(
        crypto
    )

    data_outcome = [
        r
        for r in crypto
        if is_data_outcome(r)
    ]

    versions = defaultdict(int)

    for r in crypto:
        versions[
            r.get(
                "signal_logic_version",
                "?",
            )
        ] += 1

    # --------------------------------------------------------
    # SAMPLE
    # --------------------------------------------------------

    print(
        "=" * 72
    )

    print(
        "TRADES RESEARCH REPORT"
    )

    print(
        "=" * 72
    )

    print(
        "\nВЫБОРКА"
    )

    print(
        f"  trades.jsonl существует: "
        f"{'YES' if trades_exists else 'NO'}"
    )

    print(
        f"  сделок всего:        "
        f"{len(rows)}"
    )

    if skipped_bad_lines:
        print(
            f"  ⚠ битых строк trades: "
            f"{skipped_bad_lines}"
        )

    print(
        f"  из них crypto:       "
        f"{len(crypto)}"
    )

    print(
        f"  исключены из win-rate: "
        f"equity={equity_n} "
        f"commodity={commodity_n}"
        + (
            f" unknown/missing={unknown_n}"
            if unknown_n
            else ""
        )
    )

    print(
        f"  с return_60m:        "
        f"{len(with60)}"
    )

    print(
        f"  без return_60m:      "
        f"{miss60}"
    )

    print(
        f"  закрытых (strategy): "
        f"{len(closed)}"
    )

    print(
        f"  trading population:  "
        f"{len(trading)}"
    )

    print(
        f"  data outcome:        "
        f"{len(data_outcome)}"
    )

    print(
        f"  закрытых раньше 60м: "
        f"{sum(1 for r in crypto if r.get('closed_before_60m'))}"
    )

    print(
        "  signal_logic_version: "
        + ", ".join(
            f"v{k}={versions[k]}"
            for k in sorted(
                versions,
                key=sort_key,
            )
        )
    )

    # --------------------------------------------------------
    # FILE STATUS
    # --------------------------------------------------------

    print(
        "\nRESEARCH FILES"
    )

    print(
        f"  unentered_analysis.jsonl: "
        f"{'PRESENT' if analysis_exists else 'ABSENT'} "
        f"(rows={len(unentered_analysis)})"
    )

    print(
        f"  unentered_candidates.jsonl: "
        f"{'PRESENT' if candidates_exists else 'ABSENT'} "
        f"(rows={len(unentered_candidates)})"
    )

    print(
        f"  market_history.jsonl: "
        f"{'PRESENT' if MARKET_HISTORY.exists() else 'ABSENT'}"
    )

    if skipped_unentered_analysis:
        print(
            f"  ⚠ битых строк "
            f"unentered_analysis: "
            f"{skipped_unentered_analysis}"
        )

    if skipped_candidates:
        print(
            f"  ⚠ битых строк "
            f"unentered_candidates: "
            f"{skipped_candidates}"
        )

    # --------------------------------------------------------
    # DATA QUALITY
    # --------------------------------------------------------

    analyze_data_quality(
        rows,
        unentered_analysis,
        unentered_candidates,
    )

    # --------------------------------------------------------
    # COVERAGE
    # --------------------------------------------------------

    coverage(
        crypto,
        "crypto",
    )

    outcome_summary(
        crypto
    )

    # --------------------------------------------------------
    # SIGNAL OUTCOME
    # --------------------------------------------------------

    rets60 = [
        get_forward_return(
            r,
            60,
        )
        for r in with60
        if get_forward_return(
            r,
            60,
        ) is not None
    ]

    if rets60:
        print(
            "\nSIGNAL OUTCOME @60m "
            "(crypto)"
        )

        print(
            f"n={len(rets60)} "
            f"mean={st.mean(rets60):+.2f}% "
            f"median={st.median(rets60):+.2f}%"
        )

        print(
            f"p25={pct(rets60, 25):+.2f}% "
            f"p75={pct(rets60, 75):+.2f}% "
            f"p90={pct(rets60, 90):+.2f}% "
            f"min={min(rets60):+.1f}% "
            f"max={max(rets60):+.1f}%"
        )

        print(
            f"win-rate: "
            f"{wr_str(winrates(rets60))}"
        )

    for h in [120, 240]:
        values = [
            get_forward_return(
                r,
                h,
            )
            for r in crypto
            if get_forward_return(
                r,
                h,
            ) is not None
        ]

        if values:
            print(
                f"\nSIGNAL OUTCOME @{h}m"
            )

            print(
                f"n={len(values)} "
                f"mean={st.mean(values):+.2f}% "
                f"median={st.median(values):+.2f}% "
                f"win-rate: "
                f"{wr_str(winrates(values))}"
            )

    # --------------------------------------------------------
    # TRADING OUTCOME @60
    # --------------------------------------------------------

    trading60 = [
        get_forward_return(
            r,
            60,
        )
        for r in trading
        if get_forward_return(
            r,
            60,
        ) is not None
    ]

    if trading60:
        print(
            "\nTRADING OUTCOME @60m "
            "(DATA outcome excluded)"
        )

        print(
            f"n={len(trading60)} "
            f"mean={st.mean(trading60):+.2f}% "
            f"median={st.median(trading60):+.2f}%"
        )

        print(
            f"win-rate: "
            f"{wr_str(winrates(trading60))}"
        )

    # --------------------------------------------------------
    # MFE / MAE
    # --------------------------------------------------------

    analyze_mfe_mae(
        crypto
    )

    # --------------------------------------------------------
    # REALIZATION
    # --------------------------------------------------------

    analyze_capture(
        crypto
    )

    # --------------------------------------------------------
    # ENTRY / EXIT
    # --------------------------------------------------------

    quality = (
        analyze_entry_exit_quality(
            crypto
        )
    )

    if quality:
        print_examples(
            "\nTOP DEEP MAE WIN examples:",
            quality[
                "deep_mae_wins"
            ],
        )

        print_examples(
            "\nGOOD ENTRY / POOR REALIZATION examples:",
            quality[
                "good_entry_poor_realization"
            ],
        )

    # --------------------------------------------------------
    # EXIT REASONS
    # --------------------------------------------------------

    for reason in (
        "STOP_LOSS",
        "TIMEOUT",
        "SIGNAL_DECAY",
        "EXHAUSTION",
    ):
        analyze_exit_reason(
            crypto,
            reason,
        )

    # --------------------------------------------------------
    # MARKET HISTORY
    # --------------------------------------------------------

    history = {}

    if MARKET_HISTORY.exists():
        (
            history,
            _,
            _,
        ) = load_market_history(
            MARKET_HISTORY
        )
    else:
        print(
            "\nMARKET HISTORY"
        )

        print(
            "  market_history.jsonl "
            "отсутствует."
        )

    # --------------------------------------------------------
    # POST EXIT
    # --------------------------------------------------------

    analyze_post_exit(
        crypto,
        history,
    )

    # --------------------------------------------------------
    # EXTENDED EXIT AUDITS
    # --------------------------------------------------------

    analyze_stop_loss_trajectory(
        crypto,
        history,
    )

    analyze_exit_efficiency_by_time(
        crypto,
        history,
    )

    # --------------------------------------------------------
    # WEEKLY REGIME
    # --------------------------------------------------------

    weekly = defaultdict(list)

    for r in with60:
        ts = numeric(
            r.get(
                "entry_ts"
            )
        )

        if ts is None:
            continue

        ret = get_forward_return(
            r,
            60,
        )

        if ret is None:
            continue

        weekly[
            iso_week(ts)
        ].append(
            ret
        )

    print(
        "\n=== По ISO-неделям "
        "(regime bias) ==="
    )

    print(
        f"  {'неделя':10} "
        f"{'n':>4} "
        f"{'median':>8} "
        f"{'win≥0':>6} "
        f"{'win≥1':>6}"
    )

    for wk in sorted(
        weekly
    ):
        d = weekly[wk]

        wr = winrates(d)

        low = (
            "  *low*"
            if (
                0 < len(d)
                < LOW_SAMPLE_WARNING
            )
            else ""
        )

        print(
            f"  {wk:10} "
            f"{len(d):>4} "
            f"{st.median(d):+7.2f}% "
            f"{wr[0.0]:5.0f}% "
            f"{wr[1.0]:5.0f}%"
            f"{low}"
        )

    # --------------------------------------------------------
    # ENTRY FACTORS
    # --------------------------------------------------------

    slice_table(
        crypto,
        "entry_momentum",
        "MOMENTUM на входе",
        MOMENTUM_BUCKETS,
    )

    slice_table(
        crypto,
        "entry_cvd_momentum",
        "CVD_MOMENTUM на входе",
        CVD_BUCKETS,
    )

    slice_table(
        crypto,
        "entry_price_chg24",
        "PRICE_CHG24 на входе",
        PRICE_BUCKETS,
    )

    slice_table(
        crypto,
        "entry_earliness_label",
        "РАННОСТЬ входа",
    )

    slice_table(
        crypto,
        "entry_path",
        "ПУТЬ входа (classic/early)",
    )

    slice_table(
        crypto,
        "entry_pattern",
        "ПАТТЕРН на входе",
    )

    slice_table(
        crypto,
        "entry_divergence",
        "ДИВЕРГЕНЦИЯ на входе",
    )

    slice_table(
        crypto,
        "entry_market_phase",
        "ФАЗА рынка на входе",
    )

    slice_table(
        crypto,
        "closed_before_60m",
        "ЖИЗНЬ СДЕЛКИ (<60м / ≥60м)",
    )

    slice_table(
        crypto,
        "signal_logic_version",
        "ВЕРСИЯ логики сигнала",
    )

    analyze_entry_quality_matrix(
        crypto,
    )

    # --------------------------------------------------------
    # TOP SIGNALS
    # --------------------------------------------------------

    top_signals(
        crypto,
        "TOP 10 BEST SIGNAL",
        "return_60m",
        reverse=True,
    )

    top_signals(
        crypto,
        "TOP 10 WORST SIGNAL",
        "return_60m",
        reverse=False,
    )

    top_signals(
        crypto,
        "TOP 10 BEST STRATEGY",
        "strategy_pnl_pct",
        reverse=True,
    )

    top_signals(
        crypto,
        "TOP 10 WORST STRATEGY",
        "strategy_pnl_pct",
        reverse=False,
    )

    top_divergence(
        crypto
    )

    # --------------------------------------------------------
    # RE-ENTRY / CHURN
    # --------------------------------------------------------

    analyze_reentries(
        crypto
    )

    analyze_reentry_quality(
        crypto,
        history,
    )

    # --------------------------------------------------------
    # CAPTURE TIME AUDIT
    #
    # ВАЖНО:
    #
    # Это новый диагностический блок.
    # Сам operational capture rate здесь
    # НЕ изменяется.
    # --------------------------------------------------------

    analyze_capture_time_symmetry(
        crypto,
        unentered_analysis,
        cutoff_h=24,
    )

    analyze_capture_producer_audit(
        crypto,
        unentered_analysis,
    )

    # --------------------------------------------------------
    # MISSED / UNENTERED
    # --------------------------------------------------------

    analyze_unentered(
        unentered_analysis,
        unentered_candidates,
        rows,
        cutoff_h=24,
    )

    print(
        "\n" + "=" * 72
    )

    print(
        "КОНЕЦ REPORT"
    )

    print(
        "=" * 72
    )


if __name__ == "__main__":
    main()
