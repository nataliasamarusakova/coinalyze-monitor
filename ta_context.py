# ta_context.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import hashlib
import hmac
import math
import os
import time
from urllib.parse import urlencode

import pandas as pd
import requests
import pandas_ta_classic as ta


# ============================================================
# ENV
# ============================================================

API_KEY = os.environ.get("BINGX_API_KEY", "").strip()
SECRET_KEY = os.environ.get("BINGX_SECRET_KEY", "").strip()
BASE_URL = os.environ.get("BINGX_BASE_URL", "https://open-api-vst.bingx.com").rstrip("/")


# ============================================================
# CONFIG
# ============================================================

KLINE_PATH = "/openApi/swap/v3/quote/klines"

TIMEFRAMES = ("1h", "4h", "1d")

TIMEFRAME_WEIGHTS = {
    "1h": 1,
    "4h": 2,
    "1d": 2,
}

KLINE_LIMIT = 250
PERCENTILE_WINDOW = 200
REQUEST_TIMEOUT = 15

SOURCE_KEY = "BX-AI-SKILL"

TIMEFRAME_MS = {
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


# ============================================================
# PER-RUN CACHE
#
# Один monitor run не будет повторно получать TA
# для одного и того же symbol.
# ============================================================

_TA_CACHE: dict[str, dict] = {}


# ============================================================
# HELPERS
# ============================================================

def normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()

    if not s:
        raise ValueError("empty symbol")

    s = s.replace("/", "-")

    if s.endswith("-USDT"):
        return s

    if s.endswith("USDT"):
        return s[:-4] + "-USDT"

    return s + "-USDT"


def _safe_float(value) -> float | None:
    if value is None:
        return None

    try:
        value = float(value)

        if not math.isfinite(value):
            return None

        return value

    except (TypeError, ValueError):
        return None


def _sign_params(params: dict) -> str:
    query_string = urlencode(params)

    return hmac.new(
        SECRET_KEY.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _request_signed(
    method: str,
    path: str,
    params: dict | None = None,
) -> dict:

    if not API_KEY:
        raise RuntimeError("BINGX_API_KEY not configured")

    if not SECRET_KEY:
        raise RuntimeError("BINGX_SECRET_KEY not configured")

    if not BASE_URL:
        raise RuntimeError("BINGX_BASE_URL not configured")

    params = dict(params or {})

    params["timestamp"] = str(
        int(time.time() * 1000)
    )

    params["signature"] = _sign_params(
        params
    )

    response = requests.request(
        method=method,
        url=BASE_URL + path,
        headers={
            "X-BX-APIKEY": API_KEY,
            "X-SOURCE-KEY": SOURCE_KEY,
        },
        params=params,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    try:
        data = response.json()

    except ValueError as exc:

        raise RuntimeError(
            "BingX returned non-JSON response"
        ) from exc

    if data.get("code") != 0:

        raise RuntimeError(
            f"BingX API error: "
            f"code={data.get('code')} "
            f"msg={data.get('msg')}"
        )

    return data


# ============================================================
# KLINES
# ============================================================

def _parse_kline_row(
    row: object,
    interval: str,
) -> dict | None:

    duration = TIMEFRAME_MS.get(interval)

    if duration is None:
        return None

    # Actual VST format observed:
    #
    # {
    #   "open": "...",
    #   "close": "...",
    #   "high": "...",
    #   "low": "...",
    #   "volume": "...",
    #   "time": 1787234400000
    # }

    if isinstance(row, dict):

        try:
            open_time = int(row["time"])
            open_price = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            volume = float(row["volume"])

        except (KeyError, TypeError, ValueError):

            return None

        return {
            "open_time": open_time,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "close_time": open_time + duration,
        }

    # Fallback array format.
    if isinstance(row, (list, tuple)):

        if len(row) < 6:
            return None

        try:
            open_time = int(row[0])
            open_price = float(row[1])
            high = float(row[2])
            low = float(row[3])
            close = float(row[4])
            volume = float(row[5])

        except (TypeError, ValueError):

            return None

        if len(row) >= 7:

            try:
                close_time = int(row[6])

            except (TypeError, ValueError):

                close_time = (
                    open_time
                    + duration
                )

        else:

            close_time = (
                open_time
                + duration
            )

        return {
            "open_time": open_time,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "close_time": close_time,
        }

    return None


def _fetch_klines(
    symbol: str,
    interval: str,
) -> pd.DataFrame:

    response = _request_signed(
        "GET",
        KLINE_PATH,
        {
            "symbol": symbol,
            "interval": interval,
            "limit": KLINE_LIMIT,
        },
    )

    raw = response.get("data")

    if not isinstance(raw, list):

        raise RuntimeError(
            f"{symbol} {interval}: "
            "invalid kline data"
        )

    now_ms = int(
        time.time() * 1000
    )

    parsed = []

    for row in raw:

        item = _parse_kline_row(
            row,
            interval,
        )

        if item is None:
            continue

        # Только закрытые свечи.
        if item["close_time"] > now_ms:
            continue

        if (
            item["open"] <= 0
            or item["high"] <= 0
            or item["low"] <= 0
            or item["close"] <= 0
            or item["volume"] < 0
        ):
            continue

        parsed.append(item)

    if not parsed:

        raise RuntimeError(
            f"{symbol} {interval}: "
            "no closed candles"
        )

    df = pd.DataFrame(parsed)

    df = (
        df
        .sort_values("close_time")
        .drop_duplicates(
            subset=["close_time"],
            keep="last",
        )
        .reset_index(drop=True)
    )

    if len(df) < 100:

        raise RuntimeError(
            f"{symbol} {interval}: "
            f"too few closed candles: {len(df)}"
        )

    return df


# ============================================================
# PERCENTILE
# ============================================================

def _previous_history_percentile(
    series: pd.Series,
    window: int = PERCENTILE_WINDOW,
) -> float | None:

    s = pd.to_numeric(
        series,
        errors="coerce",
    ).dropna()

    if len(s) < 2:
        return None

    current = float(
        s.iloc[-1]
    )

    history = (
        s.iloc[:-1]
        .tail(window)
    )

    if len(history) < 10:
        return None

    rank = (
        history <= current
    ).sum()

    return round(
        rank / len(history) * 100.0,
        1,
    )


# ============================================================
# INDICATORS
# ============================================================

def _calculate_features(
    df: pd.DataFrame,
) -> dict:

    work = df.copy()

    # EMA
    work["ema20"] = ta.ema(
        work["close"],
        length=20,
    )

    work["ema50"] = ta.ema(
        work["close"],
        length=50,
    )

    work["ema200"] = ta.ema(
        work["close"],
        length=200,
    )

    # RSI
    work["rsi14"] = ta.rsi(
        work["close"],
        length=14,
    )

    # ADX / DI
    adx = ta.adx(
        work["high"],
        work["low"],
        work["close"],
        length=14,
    )

    if adx is not None and not adx.empty:

        work["adx14"] = adx.get(
            "ADX_14",
            float("nan"),
        )

        work["plus_di14"] = adx.get(
            "DMP_14",
            float("nan"),
        )

        work["minus_di14"] = adx.get(
            "DMN_14",
            float("nan"),
        )

    else:

        work["adx14"] = float("nan")
        work["plus_di14"] = float("nan")
        work["minus_di14"] = float("nan")

    # ATR
    work["atr14"] = ta.atr(
        work["high"],
        work["low"],
        work["close"],
        length=14,
    )

    work["atr_pct"] = (
        work["atr14"]
        / work["close"]
        * 100.0
    )

    # Bollinger
    bb = ta.bbands(
        work["close"],
        length=20,
        std=2.0,
    )

    if bb is not None and not bb.empty:

        lower = bb.get(
            "BBL_20_2.0",
            pd.Series(
                index=work.index,
                dtype=float,
            ),
        )

        middle = bb.get(
            "BBM_20_2.0",
            pd.Series(
                index=work.index,
                dtype=float,
            ),
        )

        upper = bb.get(
            "BBU_20_2.0",
            pd.Series(
                index=work.index,
                dtype=float,
            ),
        )

        work["bb_width"] = (
            (upper - lower)
            / middle
        )

    else:

        work["bb_width"] = float("nan")

    # MACD
    macd = ta.macd(
        work["close"],
        fast=12,
        slow=26,
        signal=9,
    )

    if macd is not None and not macd.empty:

        work["macd_hist"] = macd.get(
            "MACDh_12_26_9",
            pd.Series(
                index=work.index,
                dtype=float,
            ),
        )

    else:

        work["macd_hist"] = float("nan")

    work["macd_hist_pct"] = (
        work["macd_hist"]
        / work["close"]
        * 100.0
    )

    # Percentiles
    atr_pctile = _previous_history_percentile(
        work["atr_pct"]
    )

    bb_width_pctile = _previous_history_percentile(
        work["bb_width"]
    )

    last = work.iloc[-1]

    # EMA structure
    ema20 = _safe_float(
        last["ema20"]
    )

    ema50 = _safe_float(
        last["ema50"]
    )

    ema200 = _safe_float(
        last["ema200"]
    )

    if None in (
        ema20,
        ema50,
        ema200,
    ):

        ema_direction = "neutral"

    elif ema20 > ema50 > ema200:

        ema_direction = "bullish"

    elif ema20 < ema50 < ema200:

        ema_direction = "bearish"

    else:

        ema_direction = "mixed"

    rsi = _safe_float(
        last["rsi14"]
    )

    adx_value = _safe_float(
        last["adx14"]
    )

    plus_di = _safe_float(
        last["plus_di14"]
    )

    minus_di = _safe_float(
        last["minus_di14"]
    )

    macd_hist = _safe_float(
        last["macd_hist"]
    )

    macd_hist_pct = _safe_float(
        last["macd_hist_pct"]
    )

    if macd_hist is None:

        macd_sign = "unknown"
        macd_slope = "unknown"

    else:

        if macd_hist > 0:
            macd_sign = "positive"

        elif macd_hist < 0:
            macd_sign = "negative"

        else:
            macd_sign = "zero"

        if len(work) >= 2:

            prev_hist = _safe_float(
                work["macd_hist"].iloc[-2]
            )

            if prev_hist is None:
                macd_slope = "unknown"

            elif macd_hist > prev_hist:
                macd_slope = "up"

            elif macd_hist < prev_hist:
                macd_slope = "down"

            else:
                macd_slope = "flat"

        else:

            macd_slope = "unknown"

    return {
        "ema_direction": ema_direction,
        "rsi14": rsi,
        "adx14": adx_value,
        "plus_di14": plus_di,
        "minus_di14": minus_di,
        "atr_pct": _safe_float(
            last["atr_pct"]
        ),
        "atr_pctile": atr_pctile,
        "bb_width_pctile": bb_width_pctile,
        "macd_hist": macd_hist,
        "macd_hist_pct": macd_hist_pct,
        "macd_sign": macd_sign,
        "macd_slope": macd_slope,
    }


# ============================================================
# TF DIRECTION
# ============================================================

def _directional_components(
    f: dict,
) -> dict:

    # EMA
    if f["ema_direction"] == "bullish":
        ema = 1

    elif f["ema_direction"] == "bearish":
        ema = -1

    else:
        ema = 0

    # DI
    plus = f.get("plus_di14")
    minus = f.get("minus_di14")

    if plus is None or minus is None:
        di = 0

    elif plus > minus:
        di = 1

    elif plus < minus:
        di = -1

    else:
        di = 0

    # MACD sign
    if f["macd_sign"] == "positive":
        macd = 1

    elif f["macd_sign"] == "negative":
        macd = -1

    else:
        macd = 0

    return {
        "ema": ema,
        "di": di,
        "macd": macd,
        "raw": ema + di + macd,
    }


def _classify_tf_direction(
    f: dict,
    raw_score: int,
) -> tuple[str, str]:

    ema = f["ema_direction"]

    plus = f.get("plus_di14")
    minus = f.get("minus_di14")

    if plus is None or minus is None:
        di_direction = "neutral"

    elif plus > minus:
        di_direction = "bullish"

    elif plus < minus:
        di_direction = "bearish"

    else:
        di_direction = "neutral"

    macd_direction = (
        "bullish"
        if f["macd_sign"] == "positive"
        else "bearish"
        if f["macd_sign"] == "negative"
        else "neutral"
    )

    bullish = sum(
        x == "bullish"
        for x in (
            ema,
            di_direction,
            macd_direction,
        )
    )

    bearish = sum(
        x == "bearish"
        for x in (
            ema,
            di_direction,
            macd_direction,
        )
    )

    # Completely aligned bullish.
    if bullish == 3:
        return "🟢", "BULLISH"

    # Completely aligned bearish.
    if bearish == 3:
        return "🔴", "BEARISH"

    # Bearish EMA + bullish momentum = recovery.
    if (
        ema == "bearish"
        and bullish > bearish
        and (
            di_direction == "bullish"
            or macd_direction == "bullish"
        )
    ):
        return "🟡", "MIXED / RECOVERY"

    # Bullish EMA + bearish momentum = weakening.
    if (
        ema == "bullish"
        and bearish > bullish
        and (
            di_direction == "bearish"
            or macd_direction == "bearish"
        )
    ):
        return "🟡", "MIXED / WEAKENING"

    if bullish > bearish:
        return "🟡", "MIXED / BULLISH"

    if bearish > bullish:
        return "🟡", "MIXED / BEARISH"

    return "🟡", "MIXED"


# ============================================================
# ENTRY TIMING
# ============================================================

def _build_entry_timing(
    features: dict,
) -> tuple[str, str, list[str]]:

    warnings = []

    for tf in ("1h", "4h"):

        f = features.get(tf)

        if not f:
            continue

        rsi = f.get("rsi14")

        if rsi is not None and rsi >= 80:

            warnings.append(
                f"{tf.upper()} RSI extreme"
            )

        elif rsi is not None and rsi >= 70:

            warnings.append(
                f"{tf.upper()} RSI elevated"
            )

    # ATR + BBW = one volatility cluster.
    volatility = []

    for tf in ("1h", "4h"):

        f = features.get(tf)

        if not f:
            continue

        values = [
            x
            for x in (
                f.get("atr_pctile"),
                f.get("bb_width_pctile"),
            )
            if x is not None
        ]

        if values:
            volatility.append(
                max(values)
            )

    if volatility:

        maximum = max(volatility)

        if maximum >= 95:

            warnings.append(
                "1H/4H extreme volatility"
            )

        elif maximum >= 75:

            warnings.append(
                "1H/4H high volatility"
            )

    # 1H MACD positive but weakening.
    f1 = features.get("1h")

    if f1:

        if (
            f1.get("macd_sign") == "positive"
            and f1.get("macd_slope") == "down"
        ):

            warnings.append(
                "1H bullish momentum weakening"
            )

    if not warnings:

        return "🟢", "GOOD", []

    if len(warnings) == 1:

        return "🟡", "CAUTION", warnings

    return "🟠", "STRETCHED", warnings


# ============================================================
# PUBLIC FUNCTION
# ============================================================

def get_ta_context(
    symbol: str,
) -> dict | None:
    """
    Основная функция для monitor.py.

    Возвращает только Telegram summary.

    При любой ошибке возвращает None,
    чтобы TA никогда не ломала существующий сигнал.
    """

    symbol = normalize_symbol(
        symbol
    )

    # Per-run cache.
    if symbol in _TA_CACHE:

        return _TA_CACHE[symbol]

    try:

        features = {}

        for interval in TIMEFRAMES:

            df = _fetch_klines(
                symbol,
                interval,
            )

            features[interval] = (
                _calculate_features(df)
            )

        long_evidence = 0
        short_evidence = 0
        weighted_net = 0
        tf_results = {}

        max_possible = (
            sum(TIMEFRAME_WEIGHTS.values())
            * 3
        )

        for tf in TIMEFRAMES:

            f = features[tf]

            components = (
                _directional_components(f)
            )

            weight = TIMEFRAME_WEIGHTS[tf]

            raw = components["raw"]

            weighted = raw * weight

            weighted_net += weighted

            if components["ema"] > 0:
                long_evidence += weight

            if components["di"] > 0:
                long_evidence += weight

            if components["macd"] > 0:
                long_evidence += weight

            if components["ema"] < 0:
                short_evidence += weight

            if components["di"] < 0:
                short_evidence += weight

            if components["macd"] < 0:
                short_evidence += weight

            icon, label = (
                _classify_tf_direction(
                    f,
                    raw,
                )
            )

            tf_results[tf] = {
                "score": weighted,
                "icon": icon,
                "label": label,
            }

        # -----------------------------------------------
        # Overall direction
        # -----------------------------------------------

        if weighted_net > 0:

            bias_icon = "🟢"
            bias_label = "TA LONG"

        elif weighted_net < 0:

            bias_icon = "🔴"
            bias_label = "TA SHORT"

        else:

            bias_icon = "🟡"
            bias_label = "TA MIXED"

        # -----------------------------------------------
        # Timing
        # -----------------------------------------------

        timing_icon, timing_label, timing_reasons = (
            _build_entry_timing(
                features
            )
        )

        result = {
            "bias_icon": bias_icon,
            "bias_label": bias_label,
            "net_score": weighted_net,
            "max_score": max_possible,
            "long_evidence": long_evidence,
            "short_evidence": short_evidence,
            "timeframes": tf_results,
            "entry_timing": {
                "icon": timing_icon,
                "label": timing_label,
                "reasons": timing_reasons,
            },
        }

        _TA_CACHE[symbol] = result

        return result

    except Exception as exc:

        # НИКОГДА не ломаем основной сигнал из-за TA.
        print(
            f"[TA] {symbol}: "
            f"technical context unavailable: {exc}"
        )

        return None


# ============================================================
# TELEGRAM FORMAT
# ============================================================

def format_ta_telegram(
    ta_context: dict | None,
) -> str:

    if not ta_context:

        return ""

    line = "━━━━━━━━━━━━━━━━━━"

    out = [
        line,
        "🎯 TA DIRECTION",
        "",
        (
            f"{ta_context['bias_icon']} "
            f"{ta_context['bias_label']}"
        ),
        (
            f"Strength: "
            f"{ta_context['net_score']:+d}/"
            f"{ta_context['max_score']}"
        ),
        "",
        (
            f"LONG evidence: "
            f"{ta_context['long_evidence']}/"
            f"{ta_context['max_score']}"
        ),
        (
            f"SHORT evidence: "
            f"{ta_context['short_evidence']}/"
            f"{ta_context['max_score']}"
        ),
        "",
    ]

    for tf in TIMEFRAMES:

        item = ta_context["timeframes"].get(tf)

        if not item:
            continue

        out.append(
            f"{tf.upper()} "
            f"{item['score']:+d} "
            f"{item['icon']} "
            f"{item['label']}"
        )

    timing = ta_context["entry_timing"]

    out.extend(
        [
            "",
            (
                f"Entry Timing: "
                f"{timing['icon']} "
                f"{timing['label']}"
            ),
        ]
    )

    if timing["reasons"]:

        out.extend(
            [
                "",
                "Причины:",
            ]
        )

        for reason in timing["reasons"][:5]:

            out.append(
                f"• {reason}"
            )

    return "\n".join(out)
