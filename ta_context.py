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
import pandas_ta_classic as ta
import requests


# ============================================================
# ENV
# ============================================================

API_KEY = os.environ.get(
    "BINGX_API_KEY",
    "",
).strip()

SECRET_KEY = os.environ.get(
    "BINGX_SECRET_KEY",
    "",
).strip()

BASE_URL = os.environ.get(
    "BINGX_BASE_URL",
    "https://open-api-vst.bingx.com",
).strip().rstrip("/")


# ============================================================
# CONFIG
# ============================================================

KLINE_PATH = "/openApi/swap/v3/quote/klines"

TIMEFRAMES = (
    "1h",
    "4h",
    "1d",
)

# Только для Telegram TA Direction.
# На существующий trading engine НЕ влияет.
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
# MARKET CONTEXT CONFIG
# ============================================================

# Resistance ищем только для 1H / 4H.
RESISTANCE_LOOKBACK = {
    "1h": 60,
    "4h": 30,
}

SWING_LEFT = 2
SWING_RIGHT = 2

RELATIVE_VOLUME_WINDOW = 20

BREAKOUT_VOLUME_GOOD = 1.20
BREAKOUT_VOLUME_STRONG = 1.50

BREAKOUT_CLOSE_GOOD = 0.60
BREAKOUT_CLOSE_STRONG = 0.70


# ============================================================
# CACHE
# ============================================================

# TA cache на один monitor process/run.
_TA_CACHE: dict[str, dict] = {}

# BTC candle cache на один monitor process/run.
# Не делаем повторный запрос BTC для каждой монеты.
_BTC_KLINE_CACHE: dict[str, pd.DataFrame] = {}


# ============================================================
# HELPERS
# ============================================================

def _normalize_symbol(symbol: str) -> str:
    """
    Только нормализация.

    ВАЖНО:
    displayName -> real BingX API symbol mapping
    выполняется в monitor.py:

        bingx_client.to_bx_symbol(sym)

    В этот модуль должен приходить уже реальный
    BingX API symbol, например:

        BTC-USDT
        QTUM-USDT
        LIGHTER-USDT
    """

    s = (symbol or "").strip().upper()

    if not s:
        raise ValueError("empty BingX symbol")

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
    """
    Запрос к BingX.

    TA использует только market-data endpoint.
    """

    if not API_KEY:
        raise RuntimeError(
            "BINGX_API_KEY not configured"
        )

    if not SECRET_KEY:
        raise RuntimeError(
            "BINGX_SECRET_KEY not configured"
        )

    if not BASE_URL:
        raise RuntimeError(
            "BINGX_BASE_URL not configured"
        )

    request_params = dict(
        params or {}
    )

    request_params["timestamp"] = str(
        int(time.time() * 1000)
    )

    request_params["signature"] = _sign_params(
        request_params
    )

    response = requests.request(
        method=method,
        url=BASE_URL + path,
        headers={
            "X-BX-APIKEY": API_KEY,
            "X-SOURCE-KEY": SOURCE_KEY,
        },
        params=request_params,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    try:
        payload = response.json()

    except ValueError as exc:
        raise RuntimeError(
            "BingX returned non-JSON response"
        ) from exc

    if payload.get("code") != 0:
        raise RuntimeError(
            "BingX API error: "
            f"code={payload.get('code')} "
            f"msg={payload.get('msg')}"
        )

    return payload


# ============================================================
# KLINE PARSING
# ============================================================

def _parse_kline_row(
    row: object,
    interval: str,
) -> dict | None:

    duration = TIMEFRAME_MS.get(
        interval
    )

    if duration is None:
        return None

    # --------------------------------------------------------
    # BingX VST dict format
    # --------------------------------------------------------

    if isinstance(row, dict):

        try:
            open_time = int(
                row["time"]
            )

            open_price = float(
                row["open"]
            )

            high = float(
                row["high"]
            )

            low = float(
                row["low"]
            )

            close = float(
                row["close"]
            )

            volume = float(
                row["volume"]
            )

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            return None

        return {
            "open_time": open_time,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "close_time": (
                open_time + duration
            ),
        }

    # --------------------------------------------------------
    # Array fallback
    # --------------------------------------------------------

    if isinstance(row, (list, tuple)):

        if len(row) < 6:
            return None

        try:
            open_time = int(
                row[0]
            )

            open_price = float(
                row[1]
            )

            high = float(
                row[2]
            )

            low = float(
                row[3]
            )

            close = float(
                row[4]
            )

            volume = float(
                row[5]
            )

        except (
            TypeError,
            ValueError,
        ):
            return None

        if len(row) >= 7:

            try:
                close_time = int(
                    row[6]
                )

            except (
                TypeError,
                ValueError,
            ):

                close_time = (
                    open_time + duration
                )

        else:

            close_time = (
                open_time + duration
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


# ============================================================
# FETCH CLOSED KLINES
# ============================================================

def _fetch_klines(
    bingx_symbol: str,
    interval: str,
) -> pd.DataFrame:
    """
    Только закрытые свечи.
    """

    response = _request_signed(
        "GET",
        KLINE_PATH,
        {
            "symbol": bingx_symbol,
            "interval": interval,
            "limit": KLINE_LIMIT,
        },
    )

    raw = response.get("data")

    if not isinstance(raw, list):
        raise RuntimeError(
            f"{bingx_symbol} {interval}: "
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

        # Только полностью закрытые свечи.
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
            f"{bingx_symbol} {interval}: "
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
            f"{bingx_symbol} {interval}: "
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
    """
    Percentile текущего значения только относительно
    предыдущей истории.

    Текущая точка не входит в reference set.
    """

    values = pd.to_numeric(
        series,
        errors="coerce",
    ).dropna()

    if len(values) < 2:
        return None

    current = float(
        values.iloc[-1]
    )

    history = (
        values.iloc[:-1]
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
# TA FEATURES
# ============================================================

def _calculate_features(
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

    if (
        adx is not None
        and not adx.empty
    ):

        work["adx14"] = adx.get(
            "ADX_14",
            pd.Series(
                index=work.index,
                dtype=float,
            ),
        )

        work["plus_di14"] = adx.get(
            "DMP_14",
            pd.Series(
                index=work.index,
                dtype=float,
            ),
        )

        work["minus_di14"] = adx.get(
            "DMN_14",
            pd.Series(
                index=work.index,
                dtype=float,
            ),
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
    # Bollinger Width
    # --------------------------------------------------------

    bb = ta.bbands(
        work["close"],
        length=20,
        std=2.0,
    )

    if (
        bb is not None
        and not bb.empty
    ):

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

    # --------------------------------------------------------
    # MACD
    # --------------------------------------------------------

    macd = ta.macd(
        work["close"],
        fast=12,
        slow=26,
        signal=9,
    )

    if (
        macd is not None
        and not macd.empty
    ):

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

    # --------------------------------------------------------
    # Percentiles
    # --------------------------------------------------------

    atr_pctile = (
        _previous_history_percentile(
            work["atr_pct"]
        )
    )

    bb_width_pctile = (
        _previous_history_percentile(
            work["bb_width"]
        )
    )

    # --------------------------------------------------------
    # Last closed candle
    # --------------------------------------------------------

    last = work.iloc[-1]

    # --------------------------------------------------------
    # EMA structure
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Core values
    # --------------------------------------------------------

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

    atr_value = _safe_float(
        last["atr14"]
    )

    atr_pct = _safe_float(
        last["atr_pct"]
    )

    macd_hist = _safe_float(
        last["macd_hist"]
    )

    macd_hist_pct = _safe_float(
        last["macd_hist_pct"]
    )

    # --------------------------------------------------------
    # Normalized DI ratio
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

            previous_hist = _safe_float(
                work[
                    "macd_hist"
                ].iloc[-2]
            )

            if previous_hist is None:

                macd_slope = "unknown"

            elif macd_hist > previous_hist:

                macd_slope = "up"

            elif macd_hist < previous_hist:

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
        "di_ratio": di_ratio,
        "atr14": atr_value,
        "atr_pct": atr_pct,
        "atr_pctile": atr_pctile,
        "bb_width_pctile": bb_width_pctile,
        "macd_hist": macd_hist,
        "macd_hist_pct": macd_hist_pct,
        "macd_sign": macd_sign,
        "macd_slope": macd_slope,
    }


# ============================================================
# RESISTANCE
# ============================================================

def _find_nearest_resistance(
    df: pd.DataFrame,
    atr: float | None,
    lookback: int,
) -> dict:
    """
    Ищет ближайший подтверждённый swing-high
    выше текущей закрытой цены.

    Pivot подтверждается двумя сторонами:

        high[i] >= left highs
        high[i] >= right highs

    Последние SWING_RIGHT свечей не используются
    как подтверждённые pivots.
    """

    result = {
        "resistance_price": None,
        "distance_pct": None,
        "distance_atr": None,
    }

    if len(df) < (
        lookback
        + SWING_LEFT
        + SWING_RIGHT
        + 5
    ):
        return result

    current_close = float(
        df["close"].iloc[-1]
    )

    # Последние right candles нельзя использовать
    # для pivot confirmation, поэтому исключаем их.
    work = df.iloc[:-SWING_RIGHT].copy()

    work = work.tail(
        lookback
    )

    if len(work) < (
        SWING_LEFT + SWING_RIGHT + 1
    ):
        return result

    highs = (
        work["high"]
        .astype(float)
        .tolist()
    )

    candidates = []

    for i in range(
        SWING_LEFT,
        len(highs) - SWING_RIGHT,
    ):

        pivot = highs[i]

        left = highs[
            i - SWING_LEFT:i
        ]

        right = highs[
            i + 1:i + 1 + SWING_RIGHT
        ]

        if (
            pivot >= max(left)
            and pivot >= max(right)
            and pivot > current_close
        ):

            candidates.append(
                pivot
            )

    if not candidates:
        return result

    resistance = min(
        candidates
    )

    distance_pct = (
        (
            resistance
            - current_close
        )
        / current_close
        * 100.0
    )

    distance_atr = None

    if atr is not None and atr > 0:

        distance_atr = (
            resistance
            - current_close
        ) / atr

    result[
        "resistance_price"
    ] = resistance

    result[
        "distance_pct"
    ] = distance_pct

    result[
        "distance_atr"
    ] = distance_atr

    return result


# ============================================================
# BREAKOUT QUALITY
# ============================================================

def _calculate_breakout_quality(
    df: pd.DataFrame,
    resistance_price: float | None,
) -> dict:
    """
    Descriptive breakout quality.

    НЕ влияет на TA Direction.
    НЕ влияет на trading decision.

    Использует:
        relative volume
        close location
        breakout above resistance
    """

    result = {
        "status": "NONE",
        "icon": "⚪",
        "relative_volume": None,
        "close_location": None,
    }

    if len(df) < (
        RELATIVE_VOLUME_WINDOW + 2
    ):
        return result

    last = df.iloc[-1]

    close = float(
        last["close"]
    )

    high = float(
        last["high"]
    )

    low = float(
        last["low"]
    )

    volume = float(
        last["volume"]
    )

    previous_volumes = (
        pd.to_numeric(
            df["volume"].iloc[:-1],
            errors="coerce",
        )
        .dropna()
        .tail(
            RELATIVE_VOLUME_WINDOW
        )
    )

    if previous_volumes.empty:
        return result

    median_volume = float(
        previous_volumes.median()
    )

    if median_volume <= 0:
        return result

    relative_volume = (
        volume
        / median_volume
    )

    candle_range = high - low

    if candle_range > 0:

        close_location = (
            close - low
        ) / candle_range

    else:

        close_location = None

    result[
        "relative_volume"
    ] = relative_volume

    result[
        "close_location"
    ] = close_location

    if resistance_price is None:
        return result

    # --------------------------------------------------------
    # No breakout.
    # --------------------------------------------------------

    if close <= resistance_price:

        # Если цена уже очень близко к сопротивлению,
        # помечаем как NEAR, чтобы это было видно
        # в Telegram.
        distance_pct = (
            (
                resistance_price
                - close
            )
            / close
            * 100.0
        )

        if distance_pct <= 0.75:

            result["status"] = "NEAR"
            result["icon"] = "🟡"

        return result

    # --------------------------------------------------------
    # Strong breakout.
    # --------------------------------------------------------

    if (
        relative_volume
        >= BREAKOUT_VOLUME_STRONG
        and close_location is not None
        and close_location
        >= BREAKOUT_CLOSE_STRONG
    ):

        result["status"] = "STRONG"
        result["icon"] = "🟢"

        return result

    # --------------------------------------------------------
    # Good breakout.
    # --------------------------------------------------------

    if (
        relative_volume
        >= BREAKOUT_VOLUME_GOOD
        and close_location is not None
        and close_location
        >= BREAKOUT_CLOSE_GOOD
    ):

        result["status"] = "GOOD"
        result["icon"] = "🟢"

        return result

    # --------------------------------------------------------
    # Breakout without strong confirmation.
    # --------------------------------------------------------

    result["status"] = "WEAK"
    result["icon"] = "🟡"

    return result


# ============================================================
# BTC RELATIVE STRENGTH
# ============================================================

def _calculate_relative_strength(
    coin_df: pd.DataFrame,
    btc_df: pd.DataFrame,
) -> dict:
    """
    Relative Strength:

        coin return - BTC return

    Только descriptive context.
    """

    if (
        coin_df.empty
        or btc_df.empty
    ):
        return {
            "rs_pct": None
        }

    if len(coin_df) < 2:
        return {
            "rs_pct": None
        }

    if len(btc_df) < 2:
        return {
            "rs_pct": None
        }

    coin_close = float(
        coin_df.iloc[-1]["close"]
    )

    coin_previous = float(
        coin_df.iloc[-2]["close"]
    )

    btc_close = float(
        btc_df.iloc[-1]["close"]
    )

    btc_previous = float(
        btc_df.iloc[-2]["close"]
    )

    if (
        coin_previous <= 0
        or btc_previous <= 0
    ):
        return {
            "rs_pct": None
        }

    coin_return = (
        coin_close
        / coin_previous
        - 1.0
    ) * 100.0

    btc_return = (
        btc_close
        / btc_previous
        - 1.0
    ) * 100.0

    return {
        "rs_pct": (
            coin_return
            - btc_return
        )
    }


# ============================================================
# DIRECTIONAL COMPONENTS
# ============================================================

def _directional_components(
    features: dict,
) -> dict:
    """
    TA Direction состоит только из:

        EMA
        DI
        MACD histogram sign

    RSI / ADX / ATR / BBW
    НЕ дают directional points.
    """

    # EMA
    if (
        features["ema_direction"]
        == "bullish"
    ):

        ema_score = 1

    elif (
        features["ema_direction"]
        == "bearish"
    ):

        ema_score = -1

    else:

        ema_score = 0

    # DI
    plus = features.get(
        "plus_di14"
    )

    minus = features.get(
        "minus_di14"
    )

    if (
        plus is None
        or minus is None
    ):

        di_score = 0

    elif plus > minus:

        di_score = 1

    elif plus < minus:

        di_score = -1

    else:

        di_score = 0

    # MACD histogram sign
    if (
        features["macd_sign"]
        == "positive"
    ):

        macd_score = 1

    elif (
        features["macd_sign"]
        == "negative"
    ):

        macd_score = -1

    else:

        macd_score = 0

    return {
        "ema": ema_score,
        "di": di_score,
        "macd": macd_score,
        "raw": (
            ema_score
            + di_score
            + macd_score
        ),
    }


# ============================================================
# TIMEFRAME LABEL
# ============================================================

def _classify_tf_direction(
    features: dict,
) -> tuple[str, str]:
    """
    Визуальная классификация timeframe.

    Важный момент:
    positive score сам по себе не означает
    полностью bullish structure.

    Например:

        EMA bearish
        DI bullish
        MACD bullish

    -> MIXED / RECOVERY
    """

    ema = features[
        "ema_direction"
    ]

    plus = features.get(
        "plus_di14"
    )

    minus = features.get(
        "minus_di14"
    )

    if (
        plus is None
        or minus is None
    ):

        di_direction = "neutral"

    elif plus > minus:

        di_direction = "bullish"

    elif plus < minus:

        di_direction = "bearish"

    else:

        di_direction = "neutral"

    if (
        features["macd_sign"]
        == "positive"
    ):

        macd_direction = "bullish"

    elif (
        features["macd_sign"]
        == "negative"
    ):

        macd_direction = "bearish"

    else:

        macd_direction = "neutral"

    directions = (
        ema,
        di_direction,
        macd_direction,
    )

    bullish_count = sum(
        x == "bullish"
        for x in directions
    )

    bearish_count = sum(
        x == "bearish"
        for x in directions
    )

    # Full bullish.
    if bullish_count == 3:

        return (
            "🟢",
            "BULLISH",
        )

    # Full bearish.
    if bearish_count == 3:

        return (
            "🔴",
            "BEARISH",
        )

    # Bearish structure + bullish recovery.
    if (
        ema == "bearish"
        and bullish_count > bearish_count
        and (
            di_direction == "bullish"
            or macd_direction == "bullish"
        )
    ):

        return (
            "🟡",
            "MIXED / RECOVERY",
        )

    # Bullish structure + bearish weakening.
    if (
        ema == "bullish"
        and bearish_count > bullish_count
        and (
            di_direction == "bearish"
            or macd_direction == "bearish"
        )
    ):

        return (
            "🟡",
            "MIXED / WEAKENING",
        )

    # Majority bullish.
    if bullish_count > bearish_count:

        return (
            "🟡",
            "MIXED / BULLISH",
        )

    # Majority bearish.
    if bearish_count > bullish_count:

        return (
            "🟡",
            "MIXED / BEARISH",
        )

    return (
        "🟡",
        "MIXED",
    )


# ============================================================
# PUBLIC API
# ============================================================

def get_ta_context(
    bingx_symbol: str,
) -> dict | None:
    """
    Главная функция.

    Получает УЖЕ РАЗРЕШЁННЫЙ BingX API symbol.

    Пример:

        bingx_client.to_bx_symbol("LIT-USDT")
            -> LIGHTER-USDT

        get_ta_context("LIGHTER-USDT")

    TA:
        - не запрашивает contracts;
        - не занимается mapping;
        - не меняет trading logic.
    """

    symbol = _normalize_symbol(
        bingx_symbol
    )

    cached = _TA_CACHE.get(
        symbol
    )

    if cached is not None:
        return cached

    try:

        # ----------------------------------------------------
        # MAIN FEATURE STORAGE
        # ----------------------------------------------------

        features_by_tf = {}

        # ----------------------------------------------------
        # MARKET CONTEXT
        # ----------------------------------------------------

        market_context = {}

        # ----------------------------------------------------
        # Fetch all requested TFs.
        # ----------------------------------------------------

        for timeframe in TIMEFRAMES:

            df = _fetch_klines(
                symbol,
                timeframe,
            )

            features = (
                _calculate_features(
                    df
                )
            )

            features_by_tf[
                timeframe
            ] = features

            # ------------------------------------------------
            # Resistance / Breakout / BTC RS
            #
            # Только для 1H / 4H.
            # ------------------------------------------------

            if timeframe in (
                "1h",
                "4h",
            ):

                resistance = (
                    _find_nearest_resistance(
                        df=df,
                        atr=features.get(
                            "atr14"
                        ),
                        lookback=RESISTANCE_LOOKBACK[
                            timeframe
                        ],
                    )
                )

                breakout = (
                    _calculate_breakout_quality(
                        df=df,
                        resistance_price=resistance[
                            "resistance_price"
                        ],
                    )
                )

                # BTC cache.
                btc_df = (
                    _BTC_KLINE_CACHE.get(
                        timeframe
                    )
                )

                if btc_df is None:

                    btc_df = _fetch_klines(
                        "BTC-USDT",
                        timeframe,
                    )

                    _BTC_KLINE_CACHE[
                        timeframe
                    ] = btc_df

                relative_strength = (
                    _calculate_relative_strength(
                        df,
                        btc_df,
                    )
                )

                market_context[
                    timeframe
                ] = {
                    **resistance,
                    **breakout,
                    "btc_rs_pct": (
                        relative_strength[
                            "rs_pct"
                        ]
                    ),
                }

        # ----------------------------------------------------
        # TA DIRECTION SCORE
        # ----------------------------------------------------

        max_score = (
            sum(
                TIMEFRAME_WEIGHTS.values()
            )
            * 3
        )

        net_score = 0

        long_evidence = 0

        short_evidence = 0

        timeframe_results = {}

        for timeframe in TIMEFRAMES:

            features = (
                features_by_tf[
                    timeframe
                ]
            )

            components = (
                _directional_components(
                    features
                )
            )

            weight = (
                TIMEFRAME_WEIGHTS[
                    timeframe
                ]
            )

            raw_score = components[
                "raw"
            ]

            weighted_score = (
                raw_score
                * weight
            )

            net_score += (
                weighted_score
            )

            # ------------------------------------------------
            # LONG evidence
            # ------------------------------------------------

            if components["ema"] > 0:
                long_evidence += weight

            if components["di"] > 0:
                long_evidence += weight

            if components["macd"] > 0:
                long_evidence += weight

            # ------------------------------------------------
            # SHORT evidence
            # ------------------------------------------------

            if components["ema"] < 0:
                short_evidence += weight

            if components["di"] < 0:
                short_evidence += weight

            if components["macd"] < 0:
                short_evidence += weight

            icon, label = (
                _classify_tf_direction(
                    features
                )
            )

            timeframe_results[
                timeframe
            ] = {
                "score": weighted_score,
                "icon": icon,
                "label": label,
            }

        # ----------------------------------------------------
        # TA RESULT
        # ----------------------------------------------------

        if net_score > 0:

            result_icon = "🟢"
            result_label = "LONG"

        elif net_score < 0:

            result_icon = "🔴"
            result_label = "SHORT"

        else:

            result_icon = "🟡"
            result_label = "MIXED"

        result = {
            "result_icon": result_icon,
            "result_label": result_label,

            "net_score": net_score,
            "max_score": max_score,

            "long_evidence": (
                long_evidence
            ),

            "short_evidence": (
                short_evidence
            ),

            "timeframes": (
                timeframe_results
            ),

            "market_context": (
                market_context
            ),
        }

        _TA_CACHE[
            symbol
        ] = result

        return result

    except Exception as exc:

        # TA никогда не ломает основной сигнал.
        print(
            f"[TA] {symbol}: "
            f"technical context unavailable: "
            f"{exc}"
        )

        return None


# ============================================================
# TELEGRAM: TA DIRECTION
# ============================================================

def format_ta_telegram(
    ta_context: dict | None,
) -> str:
    """
    Финальный компактный блок TA.

    Ровно такой формат:

    ━━━━━━━━━━━━━━━━━━
    🎯 TA DIRECTION
    Strength: +5/15 · LONG: 9/15 · SHORT: 4/15
    1H +3 🟢 BULLISH
    4H +4 🟡 MIXED / BULLISH
    1D -2 🟡 MIXED / BEARISH
    TA RESULT: 🟢 LONG
    """

    if not ta_context:
        return ""

    line = (
        "━━━━━━━━━━━━━━━━━━"
    )

    out = [
        line,
        "🎯 TA DIRECTION",
        (
            f"Strength: "
            f"{ta_context['net_score']:+d}/"
            f"{ta_context['max_score']} · "
            f"LONG: "
            f"{ta_context['long_evidence']}/"
            f"{ta_context['max_score']} · "
            f"SHORT: "
            f"{ta_context['short_evidence']}/"
            f"{ta_context['max_score']}"
        ),
    ]

    for timeframe in TIMEFRAMES:

        item = (
            ta_context[
                "timeframes"
            ].get(timeframe)
        )

        if not item:
            continue

        out.append(
            f"{timeframe.upper()} "
            f"{item['score']:+d} "
            f"{item['icon']} "
            f"{item['label']}"
        )

    out.append(
        "TA RESULT: "
        f"{ta_context['result_icon']} "
        f"{ta_context['result_label']}"
    )

    return "\n".join(
        out
    )


# ============================================================
# TELEGRAM: MARKET CONTEXT
# ============================================================

def format_market_context_telegram(
    ta_context: dict | None,
) -> str:
    """
    Отдельный descriptive context:

    Resistance
    Breakout quality
    Relative Strength vs BTC

    НЕ влияет на TA score.
    """

    if not ta_context:
        return ""

    market = (
        ta_context.get(
            "market_context"
        )
        or {}
    )

    if not market:
        return ""

    line = (
        "━━━━━━━━━━━━━━━━━━"
    )

    out = [
        line,
        "📍 MARKET CONTEXT",
    ]

    for timeframe in (
        "1h",
        "4h",
    ):

        item = market.get(
            timeframe
        )

        if not item:
            continue

        # ----------------------------------------------------
        # Resistance
        # ----------------------------------------------------

        resistance = item.get(
            "resistance_price"
        )

        distance_atr = item.get(
            "distance_atr"
        )

        if (
            resistance is not None
            and distance_atr is not None
        ):

            resistance_text = (
                f"{distance_atr:.1f} ATR ↑"
            )

        elif resistance is not None:

            resistance_text = (
                f"{resistance:.6g} ↑"
            )

        else:

            resistance_text = "—"

        out.append(
            f"{timeframe.upper()} "
            f"Resistance: "
            f"{resistance_text}"
        )

        # ----------------------------------------------------
        # Breakout
        # ----------------------------------------------------

        breakout_status = item.get(
            "status",
            "NONE",
        )

        breakout_icon = item.get(
            "icon",
            "⚪",
        )

        relative_volume = item.get(
            "relative_volume"
        )

        if relative_volume is not None:

            volume_text = (
                f" · Vol "
                f"{relative_volume:.1f}×"
            )

        else:

            volume_text = ""

        out.append(
            f"{timeframe.upper()} "
            f"Breakout: "
            f"{breakout_icon} "
            f"{breakout_status}"
            f"{volume_text}"
        )

        # ----------------------------------------------------
        # Relative Strength vs BTC
        # ----------------------------------------------------

        rs = item.get(
            "btc_rs_pct"
        )

        if rs is not None:

            out.append(
                f"BTC RS "
                f"{timeframe.upper()}: "
                f"{rs:+.2f}%"
            )

    return "\n".join(
        out
    )


# ============================================================
# OPTIONAL
# ============================================================

def clear_cache() -> None:
    """
    Для standalone tests.
    """

    _TA_CACHE.clear()
    _BTC_KLINE_CACHE.clear()


__all__ = [
    "get_ta_context",
    "format_ta_telegram",
    "format_market_context_telegram",
    "clear_cache",
]
