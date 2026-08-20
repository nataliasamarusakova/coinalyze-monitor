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

# Только для Telegram summary.
# На торговое решение НЕ влияет.
TIMEFRAME_WEIGHTS = {
    "1h": 1,
    "4h": 2,
    "1d": 2,
}

# BingX API limit.
KLINE_LIMIT = 250

# Reference history для ATR / BBW percentile.
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
# ============================================================
#
# Ключ = уже разрешённый BingX API symbol.
#
# Например:
#   LIGHTER-USDT
#
# TA не занимается mapping.
# Mapping делает bingx_client.to_bx_symbol()
# в monitor.py.
#
# ============================================================

_TA_CACHE: dict[str, dict] = {}


# ============================================================
# HELPERS
# ============================================================

def _normalize_symbol(symbol: str) -> str:
    """
    Здесь ТОЛЬКО нормализация формата.

    ВАЖНО:
    функция НЕ делает displayName -> API symbol mapping.

    Ожидается уже реальный BingX symbol:
        QTUM-USDT
        BTC-USDT
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
    """
    Binance/BingX-style HMAC-SHA256 signature.
    """

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
    Выполняет BingX request.

    TA использует только market-data endpoint,
    но оставляем текущий рабочий способ запроса,
    который уже подтвердился в test_ta_context.py.
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
    """
    Поддерживает фактический BingX VST dict format:

    {
        "open": "...",
        "close": "...",
        "high": "...",
        "low": "...",
        "volume": "...",
        "time": 1787234400000
    }

    time = OPEN TIME.

    close_time рассчитывается самостоятельно.
    """

    duration = TIMEFRAME_MS.get(
        interval
    )

    if duration is None:
        return None

    # --------------------------------------------------------
    # Dict format
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

    if isinstance(
        row,
        (list, tuple),
    ):

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
    Получает только CLOSED candles.

    Важно:
    bingx_symbol уже должен быть разрешён monitor.py.

    Например:
        LIGHTER-USDT
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

    raw = response.get(
        "data"
    )

    if not isinstance(
        raw,
        list,
    ):

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

        # ----------------------------------------------------
        # CLOSED ONLY
        # ----------------------------------------------------

        if item["close_time"] > now_ms:
            continue

        # ----------------------------------------------------
        # Sanity
        # ----------------------------------------------------

        if (
            item["open"] <= 0
            or item["high"] <= 0
            or item["low"] <= 0
            or item["close"] <= 0
            or item["volume"] < 0
        ):
            continue

        parsed.append(
            item
        )

    if not parsed:

        raise RuntimeError(
            f"{bingx_symbol} {interval}: "
            "no closed candles"
        )

    df = pd.DataFrame(
        parsed
    )

    df = (
        df
        .sort_values(
            "close_time"
        )
        .drop_duplicates(
            subset=[
                "close_time"
            ],
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )

    if len(df) < 100:

        raise RuntimeError(
            f"{bingx_symbol} {interval}: "
            f"too few closed candles: "
            f"{len(df)}"
        )

    return df


# ============================================================
# HISTORICAL PERCENTILE
# ============================================================

def _previous_history_percentile(
    series: pd.Series,
    window: int = PERCENTILE_WINDOW,
) -> float | None:
    """
    Percentile последней точки относительно
    только ПРЕДЫДУЩИХ значений.

    Текущая точка в reference history НЕ входит.
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
# INDICATORS
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

        "atr_pct": atr_pct,

        "atr_pctile": atr_pctile,

        "bb_width_pctile": bb_width_pctile,

        "macd_hist": macd_hist,

        "macd_hist_pct": macd_hist_pct,

        "macd_sign": macd_sign,

        "macd_slope": macd_slope,
    }


# ============================================================
# DIRECTIONAL COMPONENTS
# ============================================================

def _directional_components(
    features: dict,
) -> dict:
    """
    Три directional components:

        EMA  = -1 / 0 / +1
        DI   = -1 / 0 / +1
        MACD = -1 / 0 / +1

    Это только Telegram summary.
    """

    # --------------------------------------------------------
    # EMA
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # DI
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # MACD histogram sign
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Full bullish agreement
    # --------------------------------------------------------

    if bullish_count == 3:

        return (
            "🟢",
            "BULLISH",
        )

    # --------------------------------------------------------
    # Full bearish agreement
    # --------------------------------------------------------

    if bearish_count == 3:

        return (
            "🔴",
            "BEARISH",
        )

    # --------------------------------------------------------
    # Bearish structure + bullish momentum
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Bullish structure + bearish momentum
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Majority bullish
    # --------------------------------------------------------

    if bullish_count > bearish_count:

        return (
            "🟡",
            "MIXED / BULLISH",
        )

    # --------------------------------------------------------
    # Majority bearish
    # --------------------------------------------------------

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
# ENTRY TIMING
# ============================================================

def _build_entry_timing(
    features_by_tf: dict,
) -> tuple[
    str,
    str,
    list[str],
]:
    """
    Только Telegram context.

    Не меняет trading decision.
    """

    warnings = []

    # --------------------------------------------------------
    # RSI extension
    # --------------------------------------------------------

    for timeframe in (
        "1h",
        "4h",
    ):

        features = features_by_tf.get(
            timeframe
        )

        if not features:
            continue

        rsi = features.get(
            "rsi14"
        )

        if (
            rsi is not None
            and rsi >= 80
        ):

            warnings.append(
                f"{timeframe.upper()} "
                "RSI extreme"
            )

        elif (
            rsi is not None
            and rsi >= 70
        ):

            warnings.append(
                f"{timeframe.upper()} "
                "RSI elevated"
            )

    # --------------------------------------------------------
    # Volatility cluster
    #
    # ATR и BBW НЕ считаются двумя отдельными warning.
    # --------------------------------------------------------

    volatility_percentiles = []

    for timeframe in (
        "1h",
        "4h",
    ):

        features = features_by_tf.get(
            timeframe
        )

        if not features:
            continue

        values = [
            value
            for value in (
                features.get(
                    "atr_pctile"
                ),
                features.get(
                    "bb_width_pctile"
                ),
            )
            if value is not None
        ]

        if values:

            volatility_percentiles.append(
                max(values)
            )

    if volatility_percentiles:

        highest_volatility = max(
            volatility_percentiles
        )

        if highest_volatility >= 95:

            warnings.append(
                "1H/4H extreme volatility"
            )

        elif highest_volatility >= 75:

            warnings.append(
                "1H/4H high volatility"
            )

    # --------------------------------------------------------
    # 1H MACD weakening
    # --------------------------------------------------------

    one_hour = features_by_tf.get(
        "1h"
    )

    if one_hour:

        if (
            one_hour.get(
                "macd_sign"
            ) == "positive"
            and
            one_hour.get(
                "macd_slope"
            ) == "down"
        ):

            warnings.append(
                "1H bullish momentum weakening"
            )

    # --------------------------------------------------------
    # Final label
    # --------------------------------------------------------

    warning_count = len(
        warnings
    )

    if warning_count == 0:

        return (
            "🟢",
            "GOOD",
            [],
        )

    if warning_count == 1:

        return (
            "🟡",
            "CAUTION",
            warnings,
        )

    return (
        "🟠",
        "STRETCHED",
        warnings,
    )


# ============================================================
# PUBLIC API
# ============================================================

def get_ta_context(
    bingx_symbol: str,
) -> dict | None:
    """
    Главная функция, которую вызывает monitor.py.

    ВАЖНО:
        bingx_symbol должен быть уже настоящим
        BingX API symbol.

    Пример:

        bingx_client.to_bx_symbol("LIT-USDT")
            -> "LIGHTER-USDT"

        get_ta_context("LIGHTER-USDT")

    TA НЕ делает:
        displayName mapping
        contracts endpoint
        manual symbol map

    При любой ошибке возвращается None,
    чтобы TA никогда не ломала основной сигнал.
    """

    symbol = _normalize_symbol(
        bingx_symbol
    )

    # --------------------------------------------------------
    # Per-run cache
    # --------------------------------------------------------

    cached = _TA_CACHE.get(
        symbol
    )

    if cached is not None:

        return cached

    try:

        features_by_tf = {}

        # ----------------------------------------------------
        # Получаем 1H / 4H / 1D
        # ----------------------------------------------------

        for timeframe in TIMEFRAMES:

            df = _fetch_klines(
                symbol,
                timeframe,
            )

            features_by_tf[
                timeframe
            ] = _calculate_features(
                df
            )

        # ----------------------------------------------------
        # Directional score
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
        # Overall TA direction
        # ----------------------------------------------------

        if net_score > 0:

            bias_icon = "🟢"
            bias_label = "TA LONG"

        elif net_score < 0:

            bias_icon = "🔴"
            bias_label = "TA SHORT"

        else:

            bias_icon = "🟡"
            bias_label = "TA MIXED"

        # ----------------------------------------------------
        # Entry timing
        # ----------------------------------------------------

        (
            timing_icon,
            timing_label,
            timing_reasons,
        ) = _build_entry_timing(
            features_by_tf
        )

        result = {
            "bias_icon": bias_icon,
            "bias_label": bias_label,

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

            "entry_timing": {
                "icon": timing_icon,
                "label": timing_label,
                "reasons": timing_reasons,
            },
        }

        _TA_CACHE[
            symbol
        ] = result

        return result

    except Exception as exc:

        # ----------------------------------------------------
        # TA никогда не ломает основной monitor signal.
        # ----------------------------------------------------

        print(
            f"[TA] {symbol}: "
            f"technical context unavailable: "
            f"{exc}"
        )

        return None


# ============================================================
# TELEGRAM FORMAT
# ============================================================

def format_ta_telegram(
    ta_context: dict | None,
) -> str:
    """
    Возвращает именно тот компактный блок,
    который мы согласовали для Telegram.
    """

    if not ta_context:

        return ""

    line = (
        "━━━━━━━━━━━━━━━━━━"
    )

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

    timing = (
        ta_context[
            "entry_timing"
        ]
    )

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

        for reason in timing[
            "reasons"
        ][:5]:

            out.append(
                f"• {reason}"
            )

    return "\n".join(
        out
    )


# ============================================================
# OPTIONAL DEBUG HELPER
# ============================================================

def clear_cache() -> None:
    """
    Только для standalone tests.
    """

    _TA_CACHE.clear()


__all__ = [
    "get_ta_context",
    "format_ta_telegram",
    "clear_cache",
]
