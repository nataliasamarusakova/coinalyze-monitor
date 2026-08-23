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

STALE_TIMEFRAME_MULTIPLIER = 2.0


# ============================================================
# MARKET CONTEXT CONFIG
# ============================================================

# Resistance / Divergence ищем только для 1H / 4H.
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

# Squeeze
SQUEEZE_LOOKBACK_MAX = 50

# Divergence
DIV_MIN_BARS = 5
DIV_MAX_BARS = 40
DIV_MIN_PRICE_DIFF_PCT = 0.20  # >= 0.20% Higher High
DIV_NEAR_RESISTANCE_ATR = 1.0


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
    if not API_KEY:
        raise RuntimeError("BINGX_API_KEY not configured")

    if not SECRET_KEY:
        raise RuntimeError("BINGX_SECRET_KEY not configured")

    if not BASE_URL:
        raise RuntimeError("BINGX_BASE_URL not configured")

    request_params = dict(params or {})
    request_params["timestamp"] = str(int(time.time() * 1000))
    request_params["signature"] = _sign_params(request_params)

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
        raise RuntimeError("BingX returned non-JSON response") from exc

    if payload.get("code") != 0:
        raise RuntimeError(
            f"BingX API error: code={payload.get('code')} msg={payload.get('msg')}"
        )

    return payload


# ============================================================
# KLINE PARSING
# ============================================================

def _parse_kline_row(
    row: object,
    interval: str,
) -> dict | None:
    duration = TIMEFRAME_MS.get(interval)

    if duration is None:
        return None

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
                close_time = open_time + duration
        else:
            close_time = open_time + duration

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
        raise RuntimeError(f"{bingx_symbol} {interval}: invalid kline data")

    now_ms = int(time.time() * 1000)
    parsed = []

    for row in raw:
        item = _parse_kline_row(row, interval)
        if item is None:
            continue

        # Только полностью закрытые свечи
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
        raise RuntimeError(f"{bingx_symbol} {interval}: no closed candles")

    df = pd.DataFrame(parsed)

    df = (
        df.sort_values("close_time")
        .drop_duplicates(subset=["close_time"], keep="last")
        .reset_index(drop=True)
    )

    if len(df) < 100:
        raise RuntimeError(
            f"{bingx_symbol} {interval}: too few closed candles: {len(df)}"
        )

    return df


# ============================================================
# PERCENTILE
# ============================================================

def _previous_history_percentile(
    series: pd.Series,
    window: int = PERCENTILE_WINDOW,
) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()

    if len(values) < 2:
        return None

    current = float(values.iloc[-1])
    history = values.iloc[:-1].tail(window)

    if len(history) < 10:
        return None

    rank = (history <= current).sum()
    percentile = rank / len(history) * 100.0

    return round(float(percentile), 1)


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
    work["ema20"] = ta.ema(work["close"], length=20)
    work["ema50"] = ta.ema(work["close"], length=50)
    work["ema200"] = ta.ema(work["close"], length=200)

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------
    work["rsi14"] = ta.rsi(work["close"], length=14)

    # --------------------------------------------------------
    # ADX / DI
    # --------------------------------------------------------
    adx = ta.adx(work["high"], work["low"], work["close"], length=14)

    if adx is not None and not adx.empty:
        work["adx14"] = adx.get("ADX_14", pd.Series(index=work.index, dtype=float))
        work["plus_di14"] = adx.get("DMP_14", pd.Series(index=work.index, dtype=float))
        work["minus_di14"] = adx.get("DMN_14", pd.Series(index=work.index, dtype=float))
    else:
        work["adx14"] = float("nan")
        work["plus_di14"] = float("nan")
        work["minus_di14"] = float("nan")

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------
    work["atr14"] = ta.atr(work["high"], work["low"], work["close"], length=14)
    work["atr_pct"] = work["atr14"] / work["close"] * 100.0

    # --------------------------------------------------------
    # Bollinger Bands
    # --------------------------------------------------------
    bb = ta.bbands(work["close"], length=20, std=2.0)

    if bb is not None and not bb.empty:
        bbl = bb.get("BBL_20_2.0", pd.Series(index=work.index, dtype=float))
        bbm = bb.get("BBM_20_2.0", pd.Series(index=work.index, dtype=float))
        bbu = bb.get("BBU_20_2.0", pd.Series(index=work.index, dtype=float))

        work["bb_lower"] = bbl
        work["bb_middle"] = bbm
        work["bb_upper"] = bbu
        work["bb_width"] = (bbu - bbl) / bbm
    else:
        work["bb_lower"] = float("nan")
        work["bb_middle"] = float("nan")
        work["bb_upper"] = float("nan")
        work["bb_width"] = float("nan")

    # --------------------------------------------------------
    # Keltner Channels (20, 1.5) for Squeeze
    # --------------------------------------------------------
    kc = ta.kc(work["high"], work["low"], work["close"], length=20, scalar=1.5)

    if kc is not None and not kc.empty:
        kcl_col = [c for c in kc.columns if c.startswith("KCL")]
        kcu_col = [c for c in kc.columns if c.startswith("KCU")]

        work["kc_lower"] = kc[kcl_col[0]] if kcl_col else float("nan")
        work["kc_upper"] = kc[kcu_col[0]] if kcu_col else float("nan")
    else:
        work["kc_lower"] = float("nan")
        work["kc_upper"] = float("nan")

    # --------------------------------------------------------
    # MACD
    # --------------------------------------------------------
    macd = ta.macd(work["close"], fast=12, slow=26, signal=9)

    if macd is not None and not macd.empty:
        work["macd_hist"] = macd.get("MACDh_12_26_9", pd.Series(index=work.index, dtype=float))
    else:
        work["macd_hist"] = float("nan")

    work["macd_hist_pct"] = work["macd_hist"] / work["close"] * 100.0

    # --------------------------------------------------------
    # Choppiness Index (14)
    # --------------------------------------------------------
    chop_series = ta.chop(work["high"], work["low"], work["close"], length=14)
    if chop_series is not None and not chop_series.empty:
        work["chop14"] = chop_series
    else:
        work["chop14"] = float("nan")

    # --------------------------------------------------------
    # Percentiles (Strictly previous history)
    # --------------------------------------------------------
    atr_pctile = _previous_history_percentile(work["atr_pct"])
    bb_width_pctile = _previous_history_percentile(work["bb_width"])
    chop_pctile = _previous_history_percentile(work["chop14"])

    # --------------------------------------------------------
    # Squeeze Lifecycle State Machine
    # --------------------------------------------------------
    squeeze_series = (work["bb_lower"] >= work["kc_lower"]) & (work["bb_upper"] <= work["kc_upper"])

    current_squeeze_on = bool(squeeze_series.iloc[-1]) if not squeeze_series.empty else False

    squeeze_duration = 0
    if current_squeeze_on:
        for val in reversed(squeeze_series.tolist()):
            if val:
                squeeze_duration += 1
            else:
                break

    if len(squeeze_series) >= 2:
        squeeze_fired = bool(squeeze_series.iloc[-2]) and not current_squeeze_on
    else:
        squeeze_fired = False

    bars_since_fire = None
    if squeeze_fired:
        bars_since_fire = 0
    elif not current_squeeze_on and len(squeeze_series) >= 2:
        lookback_sq = squeeze_series.tail(SQUEEZE_LOOKBACK_MAX).tolist()
        for idx in range(len(lookback_sq) - 1, 0, -1):
            if lookback_sq[idx - 1] and not lookback_sq[idx]:
                bars_since_fire = len(lookback_sq) - 1 - idx
                break

    # Consensus Release Direction (Strictly on release candle)
    if squeeze_fired and len(work) >= 2:
        close_curr = float(work["close"].iloc[-1])
        close_prev = float(work["close"].iloc[-2])
        m_hist_curr = _safe_float(work["macd_hist"].iloc[-1])

        delta_close = close_curr - close_prev
        if m_hist_curr is not None:
            if delta_close > 0 and m_hist_curr > 0:
                squeeze_release_dir = "LONG"
            elif delta_close < 0 and m_hist_curr < 0:
                squeeze_release_dir = "SHORT"
            else:
                squeeze_release_dir = "NEUTRAL"
        else:
            squeeze_release_dir = "NEUTRAL"
    else:
        squeeze_release_dir = "NONE"

    # Continuous BB/KC ratio (bb_width / kc_width with guard)
    last_bbu = _safe_float(work["bb_upper"].iloc[-1])
    last_bbl = _safe_float(work["bb_lower"].iloc[-1])
    last_kcu = _safe_float(work["kc_upper"].iloc[-1])
    last_kcl = _safe_float(work["kc_lower"].iloc[-1])

    if (
        last_bbu is not None
        and last_bbl is not None
        and last_kcu is not None
        and last_kcl is not None
    ):
        kc_width = last_kcu - last_kcl
        bb_width_abs = last_bbu - last_bbl
        if kc_width > 0:
            bb_kc_ratio = round(bb_width_abs / kc_width, 4)
        else:
            bb_kc_ratio = None
    else:
        bb_kc_ratio = None

    # CHOP metrics
    chop_val = _safe_float(work["chop14"].iloc[-1])
    if chop_val is not None:
        if chop_val < 38.2:
            chop_regime = "TRENDING"
        elif chop_val >= 61.8:
            chop_regime = "CHOPPY"
        else:
            chop_regime = "NEUTRAL"
    else:
        chop_regime = "UNKNOWN"

    # --------------------------------------------------------
    # Last closed candle core values
    # --------------------------------------------------------
    last = work.iloc[-1]

    ema20 = _safe_float(last["ema20"])
    ema50 = _safe_float(last["ema50"])
    ema200 = _safe_float(last["ema200"])

    if None in (ema20, ema50, ema200):
        ema_direction = "neutral"
    elif ema20 > ema50 > ema200:
        ema_direction = "bullish"
    elif ema20 < ema50 < ema200:
        ema_direction = "bearish"
    else:
        ema_direction = "mixed"

    rsi = _safe_float(last["rsi14"])
    adx_value = _safe_float(last["adx14"])
    plus_di = _safe_float(last["plus_di14"])
    minus_di = _safe_float(last["minus_di14"])
    atr_value = _safe_float(last["atr14"])
    atr_pct = _safe_float(last["atr_pct"])
    macd_hist = _safe_float(last["macd_hist"])
    macd_hist_pct = _safe_float(last["macd_hist_pct"])

    if plus_di is not None and minus_di is not None and (plus_di + minus_di) > 0:
        di_ratio = (plus_di - minus_di) / (plus_di + minus_di)
    else:
        di_ratio = None

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
            previous_hist = _safe_float(work["macd_hist"].iloc[-2])
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
        "_df": work,
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
        "chop_14": chop_val,
        "chop_percentile": chop_pctile,
        "chop_regime": chop_regime,
        "squeeze_on": current_squeeze_on,
        "squeeze_duration": squeeze_duration,
        "squeeze_fired": squeeze_fired,
        "bars_since_fire": bars_since_fire,
        "squeeze_release_dir": squeeze_release_dir,
        "bb_kc_ratio": bb_kc_ratio,
    }


# ============================================================
# RESISTANCE & DIVERGENCE (CAUSAL)
# ============================================================

def _find_nearest_resistance(
    df: pd.DataFrame,
    atr: float | None,
    lookback: int,
) -> dict:
    result = {
        "resistance_price": None,
        "distance_pct": None,
        "distance_atr": None,
    }

    if len(df) < (lookback + SWING_LEFT + SWING_RIGHT + 5):
        return result

    current_close = float(df["close"].iloc[-1])

    work = df.iloc[:-SWING_RIGHT].copy().tail(lookback)

    if len(work) < (SWING_LEFT + SWING_RIGHT + 1):
        return result

    highs = work["high"].astype(float).tolist()
    candidates = []

    for i in range(SWING_LEFT, len(highs) - SWING_RIGHT):
        pivot = highs[i]
        left = highs[i - SWING_LEFT:i]
        right = highs[i + 1:i + 1 + SWING_RIGHT]

        if pivot >= max(left) and pivot >= max(right) and pivot > current_close:
            candidates.append(pivot)

    if not candidates:
        return result

    resistance = min(candidates)
    distance_pct = (resistance - current_close) / current_close * 100.0
    distance_atr = None

    if atr is not None and atr > 0:
        distance_atr = (resistance - current_close) / atr

    result["resistance_price"] = resistance
    result["distance_pct"] = distance_pct
    result["distance_atr"] = distance_atr

    return result


def _calculate_causal_divergence(
    df: pd.DataFrame,
    distance_atr: float | None,
) -> dict:
    """
    Causal Bearish Divergence:
    - Swing High пивоты (2/2) ищутся вплоть до len(df) - 1 - SWING_RIGHT.
    - Осцилляторы берутся строго в точке пивота.
    - Composite warning активируется строго по RSI divergence возле сопротивления.
    """
    result = {
        "bearish_rsi_div": False,
        "bearish_macd_div": False,
        "div_price_diff_pct": None,
        "div_rsi_diff": None,
        "div_macd_diff": None,
        "div_bars_between": None,
        "divergence_at_resistance": False,
    }

    if len(df) < (DIV_MAX_BARS + SWING_LEFT + SWING_RIGHT + 10):
        return result

    # Последний допустимый пивот: len(df) - 1 - SWING_RIGHT
    # Верхняя невключаемая граница для range: len(df) - SWING_RIGHT
    usable_end = len(df) - SWING_RIGHT

    highs = df["high"].astype(float).tolist()
    rsi_vals = df["rsi14"].astype(float).tolist()
    macd_vals = df["macd_hist"].astype(float).tolist()

    confirmed_pivots = []

    start_idx = max(SWING_LEFT, usable_end - DIV_MAX_BARS - SWING_LEFT - 10)
    for i in range(start_idx, usable_end):
        pivot_h = highs[i]
        left = highs[i - SWING_LEFT:i]
        right = highs[i + 1:i + 1 + SWING_RIGHT]

        if pivot_h >= max(left) and pivot_h >= max(right):
            confirmed_pivots.append({
                "idx": i,
                "high": pivot_h,
                "rsi": rsi_vals[i],
                "macd": macd_vals[i],
            })

    if len(confirmed_pivots) < 2:
        return result

    p2 = confirmed_pivots[-1]
    p1 = confirmed_pivots[-2]

    bars_between = p2["idx"] - p1["idx"]
    if not (DIV_MIN_BARS <= bars_between <= DIV_MAX_BARS):
        return result

    price_diff_pct = (p2["high"] - p1["high"]) / p1["high"] * 100.0
    if price_diff_pct < DIV_MIN_PRICE_DIFF_PCT:
        return result

    result["div_price_diff_pct"] = round(price_diff_pct, 2)
    result["div_bars_between"] = bars_between

    if math.isfinite(p1["rsi"]) and math.isfinite(p2["rsi"]):
        rsi_diff = p2["rsi"] - p1["rsi"]
        result["div_rsi_diff"] = round(rsi_diff, 2)
        if rsi_diff < 0:
            result["bearish_rsi_div"] = True

    if math.isfinite(p1["macd"]) and math.isfinite(p2["macd"]):
        macd_diff = p2["macd"] - p1["macd"]
        result["div_macd_diff"] = round(macd_diff, 6)
        if macd_diff < 0:
            result["bearish_macd_div"] = True

    # Composite Warning (Строго RSI divergence + proximity to resistance)
    if (
        result["bearish_rsi_div"]
        and distance_atr is not None
        and distance_atr <= DIV_NEAR_RESISTANCE_ATR
    ):
        result["divergence_at_resistance"] = True

    return result


# ============================================================
# BREAKOUT QUALITY
# ============================================================

def _calculate_breakout_quality(
    df: pd.DataFrame,
    resistance_price: float | None,
) -> dict:
    result = {
        "status": "NONE",
        "icon": "⚪",
        "relative_volume": None,
        "close_location": None,
    }

    if len(df) < (RELATIVE_VOLUME_WINDOW + 2):
        return result

    last = df.iloc[-1]
    close = float(last["close"])
    high = float(last["high"])
    low = float(last["low"])
    volume = float(last["volume"])

    previous_volumes = (
        pd.to_numeric(df["volume"].iloc[:-1], errors="coerce")
        .dropna()
        .tail(RELATIVE_VOLUME_WINDOW)
    )

    if previous_volumes.empty:
        return result

    median_volume = float(previous_volumes.median())
    if median_volume <= 0:
        return result

    relative_volume = volume / median_volume
    candle_range = high - low

    if candle_range > 0:
        close_location = (close - low) / candle_range
    else:
        close_location = None

    result["relative_volume"] = relative_volume
    result["close_location"] = close_location

    if resistance_price is None:
        return result

    if close <= resistance_price:
        distance_pct = (resistance_price - close) / close * 100.0
        if distance_pct <= 0.75:
            result["status"] = "NEAR"
            result["icon"] = "🟡"
        return result

    if (
        relative_volume >= BREAKOUT_VOLUME_STRONG
        and close_location is not None
        and close_location >= BREAKOUT_CLOSE_STRONG
    ):
        result["status"] = "STRONG"
        result["icon"] = "🟢"
        return result

    if (
        relative_volume >= BREAKOUT_VOLUME_GOOD
        and close_location is not None
        and close_location >= BREAKOUT_CLOSE_GOOD
    ):
        result["status"] = "GOOD"
        result["icon"] = "🟢"
        return result

    result["status"] = "WEAK"
    result["icon"] = "🟡"
    return result


# ============================================================
# BTC RELATIVE STRENGTH (P0 GUARDED & ALIGNED)
# ============================================================

def _calculate_relative_strength(
    coin_df: pd.DataFrame,
    btc_df: pd.DataFrame,
    timeframe: str,
) -> dict:
    result = {
        "rs_pct": None,
        "is_stale": False,
        "latest_common_close_time": None,
    }

    if coin_df.empty or btc_df.empty or len(coin_df) < 2 or len(btc_df) < 2:
        return result

    merged = (
        pd.merge(
            coin_df[["close_time", "close"]].rename(columns={"close": "coin_close"}),
            btc_df[["close_time", "close"]].rename(columns={"close": "btc_close"}),
            on="close_time",
            how="inner",
        )
        .dropna(subset=["coin_close", "btc_close"])
        .drop_duplicates(subset=["close_time"])
        .sort_values("close_time")
        .reset_index(drop=True)
    )

    if len(merged) < 2:
        return result

    latest_close_time = int(merged["close_time"].iloc[-1])
    result["latest_common_close_time"] = latest_close_time

    duration_ms = TIMEFRAME_MS.get(timeframe, 60 * 60 * 1000)
    now_ms = int(time.time() * 1000)
    if (now_ms - latest_close_time) > (duration_ms * STALE_TIMEFRAME_MULTIPLIER):
        result["is_stale"] = True

    coin_prev = float(merged["coin_close"].iloc[-2])
    coin_curr = float(merged["coin_close"].iloc[-1])
    btc_prev = float(merged["btc_close"].iloc[-2])
    btc_curr = float(merged["btc_close"].iloc[-1])

    if coin_prev <= 0 or btc_prev <= 0:
        return result

    coin_return = (coin_curr / coin_prev - 1.0) * 100.0
    btc_return = (btc_curr / btc_prev - 1.0) * 100.0

    result["rs_pct"] = coin_return - btc_return
    return result


# ============================================================
# DIRECTIONAL COMPONENTS (CORE STRENGTH)
# ============================================================

def _directional_components(
    features: dict,
) -> dict:
    ema_direction = features.get("ema_direction", "neutral")
    if ema_direction == "bullish":
        ema_vote = 1
    elif ema_direction == "bearish":
        ema_vote = -1
    else:
        ema_vote = 0

    plus = features.get("plus_di14")
    minus = features.get("minus_di14")

    if plus is None or minus is None:
        di_vote = 0
    elif plus > minus:
        di_vote = 1
    elif plus < minus:
        di_vote = -1
    else:
        di_vote = 0

    if ema_vote != 0 and ema_vote == di_vote:
        trend_score = ema_vote
    elif ema_vote == 0 and di_vote != 0:
        trend_score = di_vote
    else:
        trend_score = 0

    macd_sign = features.get("macd_sign", "unknown")
    if macd_sign == "positive":
        macd_sign_vote = 1
    elif macd_sign == "negative":
        macd_sign_vote = -1
    else:
        macd_sign_vote = 0

    macd_slope = features.get("macd_slope", "unknown")
    if macd_slope == "up":
        macd_slope_vote = 1
    elif macd_slope == "down":
        macd_slope_vote = -1
    else:
        macd_slope_vote = 0

    if macd_sign_vote != 0 and macd_sign_vote == macd_slope_vote:
        momentum_score = macd_sign_vote
    elif macd_sign_vote == 0 and macd_slope_vote != 0:
        momentum_score = macd_slope_vote
    else:
        momentum_score = 0

    rsi = features.get("rsi14")
    if rsi is None:
        rsi_score = 0
    elif rsi > 50.0:
        rsi_score = 1
    elif rsi < 50.0:
        rsi_score = -1
    else:
        rsi_score = 0

    return {
        "trend": trend_score,
        "momentum": momentum_score,
        "state": rsi_score,
        "ema": ema_vote,
        "di": di_vote,
        "macd": macd_sign_vote,
        "raw": trend_score + momentum_score + rsi_score,
    }


# ============================================================
# TIMEFRAME LABEL
# ============================================================

def _classify_tf_direction(
    features: dict,
) -> tuple[str, str]:
    components = _directional_components(features)
    directions = (
        components["trend"],
        components["momentum"],
        components["state"],
    )

    bullish_count = sum(value > 0 for value in directions)
    bearish_count = sum(value < 0 for value in directions)

    if bullish_count == 3:
        return ("🟢", "BULLISH")

    if bearish_count == 3:
        return ("🔴", "BEARISH")

    if components["trend"] < 0 and bullish_count > bearish_count:
        return ("🟡", "MIXED / RECOVERY")

    if components["trend"] > 0 and bearish_count > bullish_count:
        return ("🟡", "MIXED / WEAKENING")

    if bullish_count > bearish_count:
        return ("🟡", "MIXED / BULLISH")

    if bearish_count > bullish_count:
        return ("🟡", "MIXED / BEARISH")

    return ("🟡", "MIXED")


# ============================================================
# PUBLIC API
# ============================================================

def get_ta_context(
    bingx_symbol: str,
) -> dict | None:
    symbol = _normalize_symbol(bingx_symbol)

    cached = _TA_CACHE.get(symbol)
    if cached is not None:
        return cached

    try:
        features_by_tf = {}
        market_context = {}

        for timeframe in TIMEFRAMES:
            df = _fetch_klines(symbol, timeframe)
            features = _calculate_features(df)
            features_by_tf[timeframe] = features

            if timeframe in ("1h", "4h"):
                raw_df = features["_df"]

                resistance = _find_nearest_resistance(
                    df=raw_df,
                    atr=features.get("atr14"),
                    lookback=RESISTANCE_LOOKBACK[timeframe],
                )

                divergence = _calculate_causal_divergence(
                    df=raw_df,
                    distance_atr=resistance["distance_atr"],
                )

                breakout = _calculate_breakout_quality(
                    df=raw_df,
                    resistance_price=resistance["resistance_price"],
                )

                btc_df = _BTC_KLINE_CACHE.get(timeframe)
                if btc_df is None:
                    btc_df = _fetch_klines("BTC-USDT", timeframe)
                    _BTC_KLINE_CACHE[timeframe] = btc_df

                relative_strength = _calculate_relative_strength(
                    raw_df,
                    btc_df,
                    timeframe,
                )

                market_context[timeframe] = {
                    **resistance,
                    **breakout,
                    "btc_rs_pct": relative_strength["rs_pct"],
                    "btc_rs_stale": relative_strength["is_stale"],
                    "btc_latest_common_close": relative_strength["latest_common_close_time"],
                    "chop_14": features["chop_14"],
                    "chop_percentile": features["chop_percentile"],
                    "chop_regime": features["chop_regime"],
                    "squeeze_on": features["squeeze_on"],
                    "squeeze_duration": features["squeeze_duration"],
                    "squeeze_fired": features["squeeze_fired"],
                    "bars_since_fire": features["bars_since_fire"],
                    "squeeze_release_dir": features["squeeze_release_dir"],
                    "bb_kc_ratio": features["bb_kc_ratio"],
                    **divergence,
                }

        for tf_data in features_by_tf.values():
            tf_data.pop("_df", None)

        max_score = sum(TIMEFRAME_WEIGHTS.values()) * 3
        net_score = 0
        long_evidence = 0
        short_evidence = 0
        timeframe_results = {}

        for timeframe in TIMEFRAMES:
            features = features_by_tf[timeframe]
            components = _directional_components(features)
            weight = TIMEFRAME_WEIGHTS[timeframe]

            raw_score = components["raw"]
            weighted_score = raw_score * weight
            net_score += weighted_score

            for block in ("trend", "momentum", "state"):
                value = components[block]
                if value > 0:
                    long_evidence += weight
                elif value < 0:
                    short_evidence += weight

            icon, label = _classify_tf_direction(features)
            timeframe_results[timeframe] = {
                "score": weighted_score,
                "icon": icon,
                "label": label,
            }

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
            "long_evidence": long_evidence,
            "short_evidence": short_evidence,
            "timeframes": timeframe_results,
            "market_context": market_context,
        }

        _TA_CACHE[symbol] = result
        return result

    except Exception as exc:
        print(f"[TA] {symbol}: technical context unavailable: {exc}")
        return None


# ============================================================
# TELEGRAM: TA DIRECTION (UNCHANGED)
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
        item = ta_context["timeframes"].get(timeframe)
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

    return "\n".join(out)


# ============================================================
# TELEGRAM: MARKET CONTEXT
# ============================================================

def format_market_context_telegram(
    ta_context: dict | None,
) -> str:
    if not ta_context:
        return ""

    market = ta_context.get("market_context") or {}
    if not market:
        return ""

    line = "━━━━━━━━━━━━━━━━━━"
    out = [
        line,
        "📍 MARKET CONTEXT",
    ]

    for timeframe in ("1h", "4h"):
        item = market.get(timeframe)
        if not item:
            continue

        # ----------------------------------------------------
        # Resistance & Exhaustion Warning
        # ----------------------------------------------------
        resistance = item.get("resistance_price")
        distance_atr = item.get("distance_atr")
        div_warning = item.get("divergence_at_resistance", False)

        if resistance is not None and distance_atr is not None:
            resistance_text = f"{distance_atr:.1f} ATR ↑"
        elif resistance is not None:
            resistance_text = f"{resistance:.6g} ↑"
        else:
            resistance_text = "—"

        if div_warning:
            resistance_text += " ⚠️ Bearish Div"

        out.append(f"{timeframe.upper()} Resistance: {resistance_text}")

        # ----------------------------------------------------
        # Breakout
        # ----------------------------------------------------
        breakout_status = item.get("status", "NONE")
        breakout_icon = item.get("icon", "⚪")
        relative_volume = item.get("relative_volume")

        if relative_volume is not None:
            volume_text = f" · Vol {relative_volume:.1f}×"
        else:
            volume_text = ""

        out.append(f"{timeframe.upper()} Breakout: {breakout_icon} {breakout_status}{volume_text}")

        # ----------------------------------------------------
        # Regime & Volatility
        # ----------------------------------------------------
        chop_regime = item.get("chop_regime", "UNKNOWN")
        squeeze_on = item.get("squeeze_on", False)
        squeeze_fired = item.get("squeeze_fired", False)

        if squeeze_on:
            sq_text = f" · 🟡 Squeeze ({item.get('squeeze_duration', 0)}b)"
        elif squeeze_fired:
            sq_text = f" · 🟢 Squeeze Fire ({item.get('squeeze_release_dir', 'NONE')})"
        else:
            sq_text = ""

        out.append(f"{timeframe.upper()} Regime: {chop_regime}{sq_text}")

        # ----------------------------------------------------
        # Relative Strength vs BTC
        # ----------------------------------------------------
        rs = item.get("btc_rs_pct")
        is_stale = item.get("btc_rs_stale", False)
        if rs is not None:
            stale_mark = " (stale)" if is_stale else ""
            out.append(f"BTC RS {timeframe.upper()}: {rs:+.2f}%{stale_mark}")

    return "\n".join(out)


# ============================================================
# OPTIONAL
# ============================================================

def clear_cache() -> None:
    _TA_CACHE.clear()
    _BTC_KLINE_CACHE.clear()


__all__ = [
    "get_ta_context",
    "format_ta_telegram",
    "format_market_context_telegram",
    "clear_cache",
]
