#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import hashlib
import hmac
import math
import os
import sys
import time
from urllib.parse import urlencode

import pandas as pd
import requests

try:
    import pandas_ta_classic as ta
except ImportError:
    print("ERROR: pandas-ta-classic не установлен")
    print("Установите:")
    print("    pip install requests pandas pandas-ta-classic")
    sys.exit(1)


# ============================================================
# ENV
# ============================================================

API_KEY = os.environ.get("BINGX_API_KEY", "").strip()
SECRET_KEY = os.environ.get("BINGX_SECRET_KEY", "").strip()
BASE_URL = os.environ.get("BINGX_BASE_URL", "").strip().rstrip("/")

if not API_KEY:
    raise RuntimeError("BINGX_API_KEY не задан")

if not SECRET_KEY:
    raise RuntimeError("BINGX_SECRET_KEY не задан")

if not BASE_URL:
    raise RuntimeError("BINGX_BASE_URL не задан")


# ============================================================
# CONFIG
# ============================================================

KLINE_PATH = "/openApi/swap/v3/quote/klines"

TIMEFRAMES = ("1h", "4h", "1d")

# Вес timeframe только для Telegram summary.
# НЕ торговый параметр.
TIMEFRAME_WEIGHTS = {
    "1h": 1,
    "4h": 2,
    "1d": 2,
}

KLINE_LIMIT = 250

# Reference history для percentile.
PERCENTILE_WINDOW = 200

REQUEST_TIMEOUT = 15

SOURCE_KEY = "BX-AI-SKILL"

TIMEFRAME_MS = {
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


# ============================================================
# SYMBOL / API
# ============================================================

def normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()

    if not s:
        raise ValueError("Пустой symbol")

    s = s.replace("/", "-")

    if s.endswith("-USDT"):
        return s

    if s.endswith("USDT"):
        return s[:-4] + "-USDT"

    return s + "-USDT"


def sign_params(params: dict) -> str:
    query_string = urlencode(params)

    return hmac.new(
        SECRET_KEY.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def request_signed(
    method: str,
    path: str,
    params: dict | None = None,
) -> dict:

    params = dict(params or {})

    params["timestamp"] = str(int(time.time() * 1000))
    params["signature"] = sign_params(params)

    headers = {
        "X-BX-APIKEY": API_KEY,
        "X-SOURCE-KEY": SOURCE_KEY,
    }

    url = BASE_URL + path

    print(
        f"    URL: {url}",
        flush=True,
    )

    response = requests.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        timeout=REQUEST_TIMEOUT,
    )

    print(
        f"    HTTP: {response.status_code}",
        flush=True,
    )

    response.raise_for_status()

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "BingX вернул не-JSON:\n"
            + response.text[:1000]
        ) from exc

    print(
        f"    API code: {data.get('code')}",
        flush=True,
    )

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

def parse_kline_row(
    row: object,
    interval: str,
) -> dict | None:

    if interval not in TIMEFRAME_MS:
        return None

    # Фактический VST format:
    #
    # {
    #   "open": "...",
    #   "close": "...",
    #   "high": "...",
    #   "low": "...",
    #   "volume": "...",
    #   "time": 1787234400000
    # }
    #
    # time = open time.
    # close_time рассчитывается по timeframe.

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

        close_time = (
            open_time
            + TIMEFRAME_MS[interval]
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
                    + TIMEFRAME_MS[interval]
                )

        else:

            close_time = (
                open_time
                + TIMEFRAME_MS[interval]
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


def fetch_klines(
    symbol: str,
    interval: str,
    limit: int = KLINE_LIMIT,
) -> pd.DataFrame:

    response = request_signed(
        "GET",
        KLINE_PATH,
        {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        },
    )

    raw = response.get("data")

    print(
        f"    data type: {type(raw).__name__}",
        flush=True,
    )

    if raw is None:
        raise RuntimeError(
            f"{symbol} {interval}: "
            "data отсутствует в API response"
        )

    if not isinstance(raw, list):

        raise RuntimeError(
            f"{symbol} {interval}: "
            f"data имеет тип {type(raw).__name__}: "
            f"{str(raw)[:1000]}"
        )

    print(
        f"    raw candles: {len(raw)}",
        flush=True,
    )

    if raw:

        print(
            f"    first raw candle: "
            f"{str(raw[0])[:1000]}",
            flush=True,
        )

    now_ms = int(time.time() * 1000)

    parsed = []

    skipped_open = 0
    skipped_invalid = 0

    for row in raw:

        item = parse_kline_row(
            row,
            interval,
        )

        if item is None:

            skipped_invalid += 1

            continue

        # Только закрытые свечи.
        if item["close_time"] > now_ms:

            skipped_open += 1

            continue

        # Basic sanity.
        if (
            item["open"] <= 0
            or item["high"] <= 0
            or item["low"] <= 0
            or item["close"] <= 0
            or item["volume"] < 0
        ):

            skipped_invalid += 1

            continue

        parsed.append(item)

    print(
        f"    parsed closed candles: "
        f"{len(parsed)}",
        flush=True,
    )

    print(
        f"    skipped current/open candle: "
        f"{skipped_open}",
        flush=True,
    )

    print(
        f"    skipped invalid: "
        f"{skipped_invalid}",
        flush=True,
    )

    if not parsed:

        last_raw = raw[-1] if raw else None

        raise RuntimeError(
            f"{symbol} {interval}: "
            "после parsing/filter осталось 0 свечей.\n"
            f"now_ms={now_ms}\n"
            f"last_raw={last_raw}"
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

    last = df.iloc[-1]

    print(
        f"    last CLOSED candle: "
        f"open={int(last['open_time'])} "
        f"close={int(last['close_time'])} "
        f"close_price={last['close']}",
        flush=True,
    )

    return df


# ============================================================
# NUMERIC
# ============================================================

def safe_float(value) -> float | None:

    if value is None:
        return None

    try:

        value = float(value)

        if not math.isfinite(value):
            return None

        return value

    except (TypeError, ValueError):

        return None


def previous_history_percentile(
    series: pd.Series,
    window: int = PERCENTILE_WINDOW,
) -> float | None:
    """
    Percentile текущего значения относительно
    только ПРЕДЫДУЩИХ значений.

    Текущая точка НЕ входит в reference history.
    """

    s = pd.to_numeric(
        series,
        errors="coerce",
    ).dropna()

    if len(s) < 2:
        return None

    current = float(s.iloc[-1])

    history = (
        s.iloc[:-1]
        .tail(window)
    )

    if len(history) < 10:
        return None

    rank = (
        history <= current
    ).sum()

    percentile = (
        rank
        / len(history)
        * 100.0
    )

    return round(
        float(percentile),
        1,
    )


# ============================================================
# TA CALCULATION
# ============================================================

def calculate_features(
    df: pd.DataFrame,
) -> dict:

    work = df.copy()

    # --------------------------------------------------------
    # EMA
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    work["rsi14"] = ta.rsi(
        work["close"],
        length=14,
    )

    # --------------------------------------------------------
    # ADX / DI
    # --------------------------------------------------------

    adx = ta.adx(
        work["high"],
        work["low"],
        work["close"],
        length=14,
    )

    if adx is not None and not adx.empty:

        work["adx14"] = (
            adx["ADX_14"]
            if "ADX_14" in adx.columns
            else float("nan")
        )

        work["plus_di14"] = (
            adx["DMP_14"]
            if "DMP_14" in adx.columns
            else float("nan")
        )

        work["minus_di14"] = (
            adx["DMN_14"]
            if "DMN_14" in adx.columns
            else float("nan")
        )

    else:

        work["adx14"] = float("nan")
        work["plus_di14"] = float("nan")
        work["minus_di14"] = float("nan")

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Bollinger
    # --------------------------------------------------------

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

        work["bb_lower"] = lower
        work["bb_middle"] = middle
        work["bb_upper"] = upper

        work["bb_width"] = (
            (upper - lower)
            / middle
        )

        denominator = (
            upper - lower
        )

        work["bb_pctb"] = (
            (work["close"] - lower)
            / denominator.replace(
                0,
                float("nan"),
            )
        )

    else:

        work["bb_width"] = float("nan")
        work["bb_pctb"] = float("nan")

    # --------------------------------------------------------
    # MACD
    # --------------------------------------------------------

    macd = ta.macd(
        work["close"],
        fast=12,
        slow=26,
        signal=9,
    )

    if macd is not None and not macd.empty:

        work["macd_hist"] = (
            macd["MACDh_12_26_9"]
            if "MACDh_12_26_9" in macd.columns
            else float("nan")
        )

    else:

        work["macd_hist"] = float("nan")

    work["macd_hist_pct"] = (
        work["macd_hist"]
        / work["close"]
        * 100.0
    )

    # --------------------------------------------------------
    # Percentiles
    # --------------------------------------------------------

    atr_pctile = previous_history_percentile(
        work["atr_pct"]
    )

    bb_width_pctile = previous_history_percentile(
        work["bb_width"]
    )

    # --------------------------------------------------------
    # LAST CLOSED CANDLE
    # --------------------------------------------------------

    last = work.iloc[-1]

    # --------------------------------------------------------
    # EMA structure
    # --------------------------------------------------------

    ema20 = safe_float(last["ema20"])
    ema50 = safe_float(last["ema50"])
    ema200 = safe_float(last["ema200"])

    if None in (
        ema20,
        ema50,
        ema200,
    ):

        ema_structure = "N/A"
        ema_direction = "neutral"

    elif ema20 > ema50 > ema200:

        ema_structure = "20>50>200"
        ema_direction = "bullish"

    elif ema20 < ema50 < ema200:

        ema_structure = "20<50<200"
        ema_direction = "bearish"

    else:

        ema_structure = "mixed"
        ema_direction = "mixed"

    # --------------------------------------------------------
    # Core values
    # --------------------------------------------------------

    rsi = safe_float(last["rsi14"])

    adx_value = safe_float(last["adx14"])

    plus_di = safe_float(last["plus_di14"])

    minus_di = safe_float(last["minus_di14"])

    atr_pct = safe_float(last["atr_pct"])

    bb_pctb = safe_float(last["bb_pctb"])

    macd_hist = safe_float(last["macd_hist"])

    macd_hist_pct = safe_float(last["macd_hist_pct"])

    # --------------------------------------------------------
    # DI normalized ratio
    # --------------------------------------------------------

    if (
        plus_di is not None
        and minus_di is not None
        and (plus_di + minus_di) > 0
    ):

        di_ratio = (
            (plus_di - minus_di)
            / (plus_di + minus_di)
        )

    else:

        di_ratio = None

    # --------------------------------------------------------
    # MACD sign + slope
    # --------------------------------------------------------

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

            prev_macd_hist = safe_float(
                work["macd_hist"].iloc[-2]
            )

            if prev_macd_hist is None:

                macd_slope = "unknown"

            elif macd_hist > prev_macd_hist:

                macd_slope = "up"

            elif macd_hist < prev_macd_hist:

                macd_slope = "down"

            else:

                macd_slope = "flat"

        else:

            macd_slope = "unknown"

    return {
        "close": safe_float(last["close"]),

        "open_time": int(
            last["open_time"]
        ),

        "close_time": int(
            last["close_time"]
        ),

        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,

        "ema_structure": ema_structure,
        "ema_direction": ema_direction,

        "rsi14": rsi,

        "adx14": adx_value,
        "plus_di14": plus_di,
        "minus_di14": minus_di,

        "di_ratio": di_ratio,

        "atr_pct": atr_pct,
        "atr_pctile": atr_pctile,

        "bb_width_pctile": bb_width_pctile,
        "bb_pctb": bb_pctb,

        "macd_hist": macd_hist,
        "macd_hist_pct": macd_hist_pct,

        "macd_sign": macd_sign,
        "macd_slope": macd_slope,

        "bars": len(work),
    }


# ============================================================
# HUMAN INTERPRETATION
# ============================================================

def classify_ema(
    direction: str,
) -> tuple[str, str]:

    if direction == "bullish":
        return "🟢", "BULLISH"

    if direction == "bearish":
        return "🔴", "BEARISH"

    if direction == "mixed":
        return "🟡", "MIXED"

    return "⚪", "NEUTRAL"


def classify_rsi(
    value: float | None,
) -> tuple[str, str]:

    if value is None:
        return "⚪", "N/A"

    if value >= 80:
        return "🟠", "EXTREME"

    if value >= 70:
        return "🟠", "HIGH / EXTENDED"

    if value >= 60:
        return "🟢", "STRONG"

    if value >= 50:
        return "🟢", "BULLISH"

    if value >= 40:
        return "🟡", "NEUTRAL"

    if value >= 30:
        return "🟡", "WEAK"

    return "🔴", "VERY WEAK"


def classify_adx(
    value: float | None,
) -> tuple[str, str]:

    if value is None:
        return "⚪", "N/A"

    if value >= 40:
        return "🟢", "VERY STRONG"

    if value >= 25:
        return "🟢", "STRONG"

    if value >= 20:
        return "🟡", "DEVELOPING"

    if value >= 15:
        return "🟡", "WEAK"

    return "⚪", "NO TREND"


def classify_di(
    plus: float | None,
    minus: float | None,
) -> tuple[str, str, float | None]:

    if plus is None or minus is None:

        return "⚪", "N/A", None

    spread = plus - minus

    if spread > 5:

        return "🟢", "BULLISH", spread

    if spread > 0:

        return "🟢", "SLIGHT BULLISH", spread

    if spread < -5:

        return "🔴", "BEARISH", spread

    if spread < 0:

        return "🔴", "SLIGHT BEARISH", spread

    return "🟡", "NEUTRAL", spread


def classify_atr_percentile(
    percentile: float | None,
) -> tuple[str, str]:

    if percentile is None:
        return "⚪", "N/A"

    if percentile >= 95:
        return "🔴", "EXTREME VOL"

    if percentile >= 75:
        return "🟠", "HIGH VOL"

    if percentile >= 25:
        return "⚪", "NORMAL VOL"

    if percentile >= 10:
        return "🔵", "LOW VOL"

    return "🔵", "VERY LOW VOL"


def classify_bb_width(
    percentile: float | None,
) -> tuple[str, str]:

    if percentile is None:
        return "⚪", "N/A"

    if percentile >= 95:
        return "🔴", "EXTREME EXPANSION"

    if percentile >= 75:
        return "🟠", "HIGH EXPANSION"

    if percentile >= 25:
        return "⚪", "NORMAL"

    if percentile >= 10:
        return "🔵", "LOW WIDTH"

    return "🔵", "VERY LOW WIDTH"


def classify_macd(
    sign: str,
    slope: str,
) -> tuple[str, str]:

    if sign == "positive" and slope == "up":
        return "🟢", "BULLISH MOMENTUM"

    if sign == "positive" and slope == "down":
        return "🟡", "MOMENTUM WEAKENING"

    if sign == "negative" and slope == "up":
        return "🟡", "RECOVERY"

    if sign == "negative" and slope == "down":
        return "🔴", "BEARISH MOMENTUM"

    if sign == "positive":
        return "🟢", "BULLISH"

    if sign == "negative":
        return "🔴", "BEARISH"

    return "⚪", "NEUTRAL"


# ============================================================
# TIMEFRAME DIRECTION LABEL
# ============================================================

def classify_tf_direction(
    f: dict,
    raw_score: int,
) -> tuple[str, str]:
    """
    Важное отличие от общего Net Score.

    Если конкретный TF имеет:
        EMA bullish
        DI bullish
        MACD bullish
    → BULLISH

    Если конфликт:
        EMA bearish
        DI bullish
        MACD bullish
    → MIXED / RECOVERY

    То есть нельзя назвать такой timeframe
    просто BULLISH только из-за положительного score.
    """

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

    macd_sign = f["macd_sign"]

    macd_direction = (
        "bullish"
        if macd_sign == "positive"
        else "bearish"
        if macd_sign == "negative"
        else "neutral"
    )

    # --------------------------------------------------------
    # Pure bullish
    # --------------------------------------------------------

    if (
        ema == "bullish"
        and di_direction == "bullish"
        and macd_direction == "bullish"
    ):
        return "🟢", "BULLISH"

    # --------------------------------------------------------
    # Pure bearish
    # --------------------------------------------------------

    if (
        ema == "bearish"
        and di_direction == "bearish"
        and macd_direction == "bearish"
    ):
        return "🔴", "BEARISH"

    # --------------------------------------------------------
    # Clear bullish majority but mixed structure
    # --------------------------------------------------------

    bullish_components = sum(
        x == "bullish"
        for x in (
            ema,
            di_direction,
            macd_direction,
        )
    )

    bearish_components = sum(
        x == "bearish"
        for x in (
            ema,
            di_direction,
            macd_direction,
        )
    )

    # --------------------------------------------------------
    # EMA bearish, momentum bullish:
    # special recovery state.
    # --------------------------------------------------------

    if (
        ema == "bearish"
        and
        (
            di_direction == "bullish"
            or macd_direction == "bullish"
        )
        and
        bullish_components > bearish_components
    ):
        return "🟡", "MIXED / RECOVERY"

    # --------------------------------------------------------
    # EMA bullish, momentum bearish:
    # weakening bullish structure.
    # --------------------------------------------------------

    if (
        ema == "bullish"
        and
        (
            di_direction == "bearish"
            or macd_direction == "bearish"
        )
        and
        bearish_components > bullish_components
    ):
        return "🟡", "MIXED / WEAKENING"

    # --------------------------------------------------------
    # Simple majority.
    # --------------------------------------------------------

    if bullish_components > bearish_components:
        return "🟡", "MIXED / BULLISH"

    if bearish_components > bullish_components:
        return "🟡", "MIXED / BEARISH"

    return "🟡", "MIXED"


# ============================================================
# DIRECTIONAL SCORE
# ============================================================

def directional_components(
    f: dict,
) -> dict:

    # EMA
    if f["ema_direction"] == "bullish":
        ema_score = 1
    elif f["ema_direction"] == "bearish":
        ema_score = -1
    else:
        ema_score = 0

    # DI
    plus = f.get("plus_di14")
    minus = f.get("minus_di14")

    if plus is None or minus is None:
        di_score = 0
    elif plus > minus:
        di_score = 1
    elif plus < minus:
        di_score = -1
    else:
        di_score = 0

    # MACD sign
    if f["macd_sign"] == "positive":
        macd_score = 1
    elif f["macd_sign"] == "negative":
        macd_score = -1
    else:
        macd_score = 0

    raw = (
        ema_score
        + di_score
        + macd_score
    )

    return {
        "ema": ema_score,
        "di": di_score,
        "macd": macd_score,
        "raw": raw,
    }


def build_ta_direction(
    features: dict,
) -> dict:
    """
    Telegram-only directional summary.

    НЕ торговый сигнал.
    """

    tf_results = {}

    long_evidence = 0
    short_evidence = 0

    weighted_net = 0

    max_possible = (
        sum(TIMEFRAME_WEIGHTS.values())
        * 3
    )

    for tf in TIMEFRAMES:

        f = features.get(tf)

        if not f:
            continue

        components = directional_components(
            f
        )

        weight = TIMEFRAME_WEIGHTS[tf]

        raw = components["raw"]

        weighted = raw * weight

        weighted_net += weighted

        # LONG evidence.
        if components["ema"] > 0:
            long_evidence += weight

        if components["di"] > 0:
            long_evidence += weight

        if components["macd"] > 0:
            long_evidence += weight

        # SHORT evidence.
        if components["ema"] < 0:
            short_evidence += weight

        if components["di"] < 0:
            short_evidence += weight

        if components["macd"] < 0:
            short_evidence += weight

        tf_icon, tf_label = classify_tf_direction(
            f,
            raw,
        )

        tf_results[tf] = {
            "weight": weight,
            "ema": components["ema"],
            "di": components["di"],
            "macd": components["macd"],
            "raw": raw,
            "weighted": weighted,
            "icon": tf_icon,
            "label": tf_label,
        }

    # --------------------------------------------------------
    # Overall Telegram label.
    #
    # Не используем STRONG LONG / STRONG SHORT,
    # чтобы не создавать впечатление торгового сигнала.
    # --------------------------------------------------------

    if weighted_net > 0:

        bias_icon = "🟢"
        bias_label = "TA LONG"

    elif weighted_net < 0:

        bias_icon = "🔴"
        bias_label = "TA SHORT"

    else:

        bias_icon = "🟡"
        bias_label = "TA MIXED"

    return {
        "tf_results": tf_results,
        "long_evidence": long_evidence,
        "short_evidence": short_evidence,
        "weighted_net": weighted_net,
        "max_possible": max_possible,
        "bias_icon": bias_icon,
        "bias_label": bias_label,
    }


# ============================================================
# ENTRY TIMING
# ============================================================

def build_entry_timing(
    features: dict,
) -> dict:
    """
    Только Telegram context.

    Не торговый фильтр.
    """

    warnings = []

    # --------------------------------------------------------
    # RSI extension
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Volatility cluster
    #
    # ATR + BBW не считаются двумя независимыми warning.
    # Берём максимальную относительную экстремальность.
    # --------------------------------------------------------

    volatility_levels = []

    for tf in ("1h", "4h"):

        f = features.get(tf)

        if not f:
            continue

        atr_pctile = f.get("atr_pctile")
        bb_pctile = f.get("bb_width_pctile")

        values = [
            x
            for x in (
                atr_pctile,
                bb_pctile,
            )
            if x is not None
        ]

        if values:

            volatility_levels.append(
                max(values)
            )

    max_volatility = (
        max(volatility_levels)
        if volatility_levels
        else None
    )

    if (
        max_volatility is not None
        and max_volatility >= 95
    ):

        warnings.append(
            "1H/4H extreme volatility"
        )

    elif (
        max_volatility is not None
        and max_volatility >= 75
    ):

        warnings.append(
            "1H/4H high volatility"
        )

    # --------------------------------------------------------
    # 1H MACD weakening
    # --------------------------------------------------------

    f1 = features.get("1h")

    if f1:

        if (
            f1.get("macd_sign") == "positive"
            and f1.get("macd_slope") == "down"
        ):

            warnings.append(
                "1H bullish momentum weakening"
            )

    # --------------------------------------------------------
    # Timing label
    # --------------------------------------------------------

    count = len(warnings)

    if count == 0:

        icon = "🟢"
        label = "GOOD"

    elif count == 1:

        icon = "🟡"
        label = "CAUTION"

    else:

        icon = "🟠"
        label = "STRETCHED"

    return {
        "icon": icon,
        "label": label,
        "warnings": warnings,
        "warning_count": count,
    }


# ============================================================
# SUMMARY
# ============================================================

def format_direction_summary(
    features: dict,
) -> str:

    direction = build_ta_direction(
        features
    )

    timing = build_entry_timing(
        features
    )

    out = []

    out.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    out.append(
        "🎯 TA DIRECTION"
    )

    out.append("")

    out.append(
        f"{direction['bias_icon']} "
        f"{direction['bias_label']}"
    )

    out.append(
        f"Net Score: "
        f"{direction['weighted_net']:+d}"
        f"/{direction['max_possible']}"
    )

    out.append(
        f"LONG evidence: "
        f"{direction['long_evidence']}"
        f"/{direction['max_possible']}"
    )

    out.append(
        f"SHORT evidence: "
        f"{direction['short_evidence']}"
        f"/{direction['max_possible']}"
    )

    out.append("")

    for tf in TIMEFRAMES:

        item = direction["tf_results"].get(tf)

        if not item:
            continue

        out.append(
            f"{tf.upper()} "
            f"{item['weighted']:+d} "
            f"{item['icon']} "
            f"{item['label']}"
        )

    out.append("")

    out.append(
        f"Entry Timing: "
        f"{timing['icon']} "
        f"{timing['label']}"
    )

    if timing["warnings"]:

        out.append("")
        out.append("Причины:")

        for warning in timing["warnings"][:5]:

            out.append(
                f"• {warning}"
            )

    return "\n".join(out)


# ============================================================
# DETAILED TA
# ============================================================

def fnum(
    value,
    decimals: int = 1,
) -> str:

    if value is None:
        return "—"

    return f"{value:.{decimals}f}"


def format_ta_details(
    features: dict,
) -> str:

    out = []

    out.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    out.append(
        "📊 Technical Context"
    )

    out.append("")

    for tf in TIMEFRAMES:

        f = features.get(tf)

        out.append(
            tf.upper()
        )

        if not f:

            out.append(
                "⚠️ TA data unavailable"
            )

            out.append("")

            continue

        # EMA
        ema_icon, ema_label = classify_ema(
            f["ema_direction"]
        )

        out.append(
            f"EMA {f['ema_structure']} "
            f"{ema_icon} {ema_label}"
        )

        # RSI
        rsi_icon, rsi_label = classify_rsi(
            f["rsi14"]
        )

        out.append(
            f"RSI {fnum(f['rsi14'], 1)} "
            f"{rsi_icon} {rsi_label}"
        )

        # ADX
        adx_icon, adx_label = classify_adx(
            f["adx14"]
        )

        out.append(
            f"ADX {fnum(f['adx14'], 1)} "
            f"{adx_icon} {adx_label}"
        )

        # DI
        di_icon, di_label, di_spread = classify_di(
            f["plus_di14"],
            f["minus_di14"],
        )

        plus = f["plus_di14"]
        minus = f["minus_di14"]

        if (
            plus is not None
            and minus is not None
            and di_spread is not None
        ):

            if plus > minus:
                relation = ">"

            elif plus < minus:
                relation = "<"

            else:
                relation = "="

            out.append(
                f"+DI {fnum(plus, 1)} "
                f"{relation} "
                f"-DI {fnum(minus, 1)} "
                f"{di_icon} {di_label} "
                f"(spread {di_spread:+.1f})"
            )

        else:

            out.append(
                "+DI — | -DI — ⚪ N/A"
            )

        # ATR
        atr_icon, atr_label = (
            classify_atr_percentile(
                f["atr_pctile"]
            )
        )

        out.append(
            f"ATR {fnum(f['atr_pct'], 2)}% | "
            f"{fnum(f['atr_pctile'], 0)}%ile "
            f"{atr_icon} {atr_label}"
        )

        # BB Width
        bb_icon, bb_label = (
            classify_bb_width(
                f["bb_width_pctile"]
            )
        )

        out.append(
            f"BBW "
            f"{fnum(f['bb_width_pctile'], 0)}%ile "
            f"{bb_icon} {bb_label}"
        )

        # MACD
        macd_icon, macd_label = classify_macd(
            f["macd_sign"],
            f["macd_slope"],
        )

        if f["macd_hist_pct"] is None:

            out.append(
                f"MACD "
                f"{macd_icon} "
                f"{macd_label}"
            )

        else:

            if f["macd_hist_pct"] >= 0:

                value = (
                    f"+{abs(f['macd_hist_pct']):.3f}%"
                )

            else:

                value = (
                    f"-{abs(f['macd_hist_pct']):.3f}%"
                )

            if f["macd_slope"] == "up":
                arrow = "↑"

            elif f["macd_slope"] == "down":
                arrow = "↓"

            elif f["macd_slope"] == "flat":
                arrow = "→"

            else:
                arrow = "—"

            out.append(
                f"MACD {arrow} "
                f"{value} "
                f"{macd_icon} {macd_label}"
            )

        out.append("")

    return "\n".join(out)


# ============================================================
# FINAL OUTPUT
# ============================================================

def format_output(
    symbol: str,
    features: dict,
) -> str:

    out = []

    out.append(
        f"📊 {symbol} · TA CONTEXT"
    )

    out.append("")

    out.append(
        format_direction_summary(
            features
        )
    )

    out.append("")

    out.append(
        format_ta_details(
            features
        )
    )

    out.append("")

    out.append(
        "ℹ️ TA — только Telegram-контекст. "
        "На торговое решение не влияет."
    )

    return "\n".join(out)


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Standalone BingX TA Context tester"
        )
    )

    parser.add_argument(
        "--symbol",
        default="QTUM",
        help="Например QTUM или QTUM-USDT",
    )

    args = parser.parse_args()

    symbol = normalize_symbol(
        args.symbol
    )

    print()

    print("=" * 70)

    print(
        f"BingX TA Context TEST: {symbol}"
    )

    print("=" * 70)

    print()

    all_features = {}

    for interval in TIMEFRAMES:

        print(
            f"[{interval}] "
            "Получение закрытых свечей...",
            flush=True,
        )

        try:

            df = fetch_klines(
                symbol,
                interval,
                KLINE_LIMIT,
            )

            result = calculate_features(
                df
            )

            all_features[interval] = result

            close_time = pd.to_datetime(
                result["close_time"],
                unit="ms",
                utc=True,
            )

            print(
                f"[{interval}] OK: "
                f"{result['bars']} "
                f"закрытых свечей | "
                f"close={result['close']} | "
                f"closed={close_time}",
                flush=True,
            )

        except Exception as exc:

            print(
                f"[{interval}] ERROR: {exc}",
                file=sys.stderr,
            )

    if not all_features:

        raise SystemExit(
            "\nНе удалось получить TA "
            "ни для одного timeframe."
        )

    print()

    print(
        format_output(
            symbol,
            all_features,
        )
    )

    print()


if __name__ == "__main__":
    main()
