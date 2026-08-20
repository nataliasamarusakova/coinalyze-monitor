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
    print("Установите: pip install requests pandas pandas-ta-classic")
    sys.exit(1)


# ============================================================
# ENV — только из environment
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

KLINE_LIMIT = 250

PERCENTILE_WINDOW = 200

REQUEST_TIMEOUT = 15

SOURCE_KEY = "BX-AI-SKILL"


# BingX candle interval -> milliseconds.
TIMEFRAME_MS = {
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


# ============================================================
# HELPERS
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

    print(f"    URL: {url}", flush=True)

    response = requests.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        timeout=REQUEST_TIMEOUT,
    )

    print(f"    HTTP: {response.status_code}", flush=True)

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
# KLINE PARSING
# ============================================================

def parse_kline_row(
    row: object,
    interval: str,
) -> dict | None:
    """
    Реальный VST response, который мы увидели:

    {
        "open": "0.7633",
        "close": "0.7647",
        "high": "0.7666",
        "low": "0.7633",
        "volume": "6776.3",
        "time": 1787234400000
    }

    Здесь `time` = open time свечи.

    close_time вычисляем как:
        open_time + timeframe duration
    """

    if interval not in TIMEFRAME_MS:
        return None

    # --------------------------------------------------------
    # Dict — фактический BingX VST формат
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Array — fallback
    # --------------------------------------------------------

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

        # Если endpoint дал closeTime — используем его.
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

    raw_response = request_signed(
        "GET",
        KLINE_PATH,
        {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        },
    )

    raw = raw_response.get("data")

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
            f"    first raw candle: {str(raw[0])[:1000]}",
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

        # ----------------------------------------------------
        # ВАЖНО:
        #
        # `close_time` рассчитан из `time` + timeframe.
        #
        # Не используем просто `time < now`, потому что
        # текущая свеча уже имеет прошлое open_time,
        # но ещё может быть незакрыта.
        # ----------------------------------------------------

        if item["close_time"] > now_ms:
            skipped_open += 1
            continue

        # ----------------------------------------------------
        # Basic OHLC sanity
        # ----------------------------------------------------

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
        f"    parsed closed candles: {len(parsed)}",
        flush=True,
    )

    print(
        f"    skipped current/open candle: {skipped_open}",
        flush=True,
    )

    print(
        f"    skipped invalid: {skipped_invalid}",
        flush=True,
    )

    if not parsed:
        last_item = raw[-1] if raw else None

        raise RuntimeError(
            f"{symbol} {interval}: "
            "после parsing/filter осталось 0 свечей.\n"
            f"now_ms={now_ms}\n"
            f"last_raw={last_item}"
        )

    df = pd.DataFrame(parsed)

    df = (
        df.sort_values("close_time")
        .drop_duplicates(
            subset=["close_time"],
            keep="last",
        )
        .reset_index(drop=True)
    )

    # --------------------------------------------------------
    # Печатаем последнюю реально использованную свечу.
    # --------------------------------------------------------

    last = df.iloc[-1]

    print(
        f"    last CLOSED candle: "
        f"open={last['open_time']} "
        f"close={last['close_time']} "
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


def percentile_of_last(
    series: pd.Series,
    window: int = PERCENTILE_WINDOW,
) -> float | None:

    s = pd.to_numeric(
        series,
        errors="coerce",
    ).dropna()

    if len(s) < 30:
        return None

    s = s.tail(window)

    if len(s) < 10:
        return None

    current = float(s.iloc[-1])

    rank = (s <= current).sum()

    return round(
        rank / len(s) * 100.0,
        1,
    )


# ============================================================
# TA
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
    # ADX / +DI / -DI
    # --------------------------------------------------------

    adx = ta.adx(
        work["high"],
        work["low"],
        work["close"],
        length=14,
    )

    if adx is not None and not adx.empty:

        if "ADX_14" in adx.columns:
            work["adx14"] = adx["ADX_14"]
        else:
            work["adx14"] = float("nan")

        if "DMP_14" in adx.columns:
            work["plus_di14"] = adx["DMP_14"]
        else:
            work["plus_di14"] = float("nan")

        if "DMN_14" in adx.columns:
            work["minus_di14"] = adx["DMN_14"]
        else:
            work["minus_di14"] = float("nan")

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

        if "MACDh_12_26_9" in macd.columns:
            work["macd_hist"] = (
                macd["MACDh_12_26_9"]
            )
        else:
            work["macd_hist"] = float("nan")

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

    atr_pctile = percentile_of_last(
        work["atr_pct"]
    )

    bb_width_pctile = percentile_of_last(
        work["bb_width"]
    )

    # --------------------------------------------------------
    # LAST CLOSED CANDLE
    # --------------------------------------------------------

    last = work.iloc[-1]

    # --------------------------------------------------------
    # EMA STRUCTURE
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
    # ADX / DI
    # --------------------------------------------------------

    adx_value = safe_float(
        last["adx14"]
    )

    plus_di = safe_float(
        last["plus_di14"]
    )

    minus_di = safe_float(
        last["minus_di14"]
    )

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    rsi = safe_float(
        last["rsi14"]
    )

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    atr_pct = safe_float(
        last["atr_pct"]
    )

    # --------------------------------------------------------
    # BB
    # --------------------------------------------------------

    bb_width_pctile = bb_width_pctile

    bb_pctb = safe_float(
        last["bb_pctb"]
    )

    # --------------------------------------------------------
    # MACD
    # --------------------------------------------------------

    macd_hist = safe_float(
        last["macd_hist"]
    )

    macd_hist_pct = safe_float(
        last["macd_hist_pct"]
    )

    if macd_hist is None:
        macd_direction = "—"
    elif macd_hist > 0:
        macd_direction = "↑"
    elif macd_hist < 0:
        macd_direction = "↓"
    else:
        macd_direction = "→"

    return {
        "close": safe_float(last["close"]),
        "close_time": int(last["close_time"]),
        "open_time": int(last["open_time"]),

        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,

        "ema_structure": ema_structure,
        "ema_direction": ema_direction,

        "rsi14": rsi,

        "adx14": adx_value,
        "plus_di14": plus_di,
        "minus_di14": minus_di,

        "atr_pct": atr_pct,
        "atr_pctile": atr_pctile,

        "bb_width_pctile": bb_width_pctile,
        "bb_pctb": bb_pctb,

        "macd_hist": macd_hist,
        "macd_hist_pct": macd_hist_pct,
        "macd_direction": macd_direction,

        "bars": len(work),
    }


# ============================================================
# FORMAT
# ============================================================

def fnum(
    value,
    decimals: int = 1,
) -> str:

    if value is None:
        return "—"

    return f"{value:.{decimals}f}"


def format_ta(
    features: dict,
) -> str:

    out = []

    out.append("━━━━━━━━━━━━━━━━━━")
    out.append("📊 Technical Context")
    out.append("")

    for tf in TIMEFRAMES:

        f = features.get(tf)

        out.append(tf.upper())

        if not f:
            out.append("ERROR: нет данных")
            out.append("")
            continue

        # ----------------------------------------------------
        # EMA
        # ----------------------------------------------------

        if f["ema_direction"] == "bullish":
            ema_icon = "🟢"
        elif f["ema_direction"] == "bearish":
            ema_icon = "🔴"
        elif f["ema_direction"] == "mixed":
            ema_icon = "🟡"
        else:
            ema_icon = "⚪"

        out.append(
            f"EMA {f['ema_structure']} {ema_icon}"
        )

        # ----------------------------------------------------
        # RSI
        # ----------------------------------------------------

        out.append(
            f"RSI {fnum(f['rsi14'], 1)}"
        )

        # ----------------------------------------------------
        # ADX / DI
        # ----------------------------------------------------

        plus = f["plus_di14"]
        minus = f["minus_di14"]

        if (
            plus is not None
            and minus is not None
        ):
            if plus > minus:
                di = (
                    f"+DI {fnum(plus)} > "
                    f"-DI {fnum(minus)}"
                )
            elif plus < minus:
                di = (
                    f"+DI {fnum(plus)} < "
                    f"-DI {fnum(minus)}"
                )
            else:
                di = (
                    f"+DI {fnum(plus)} = "
                    f"-DI {fnum(minus)}"
                )
        else:
            di = "+DI — | -DI —"

        out.append(
            f"ADX {fnum(f['adx14'])} | {di}"
        )

        # ----------------------------------------------------
        # ATR
        # ----------------------------------------------------

        out.append(
            f"ATR {fnum(f['atr_pct'], 2)}% | "
            f"{fnum(f['atr_pctile'], 0)}%ile"
        )

        # ----------------------------------------------------
        # Bollinger
        # ----------------------------------------------------

        out.append(
            f"BB Width "
            f"{fnum(f['bb_width_pctile'], 0)}%ile"
        )

        # ----------------------------------------------------
        # MACD
        # ----------------------------------------------------

        macd_value = f["macd_hist_pct"]

        if macd_value is None:
            out.append(
                f"MACD Hist {f['macd_direction']}"
            )
        else:
            out.append(
                f"MACD Hist "
                f"{f['macd_direction']} "
                f"({macd_value:+.3f}%)"
            )

        out.append("")

    out.append(
        "ℹ️ TA — только контекст, "
        "на торговое решение не влияет."
    )

    return "\n".join(out)


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description="Standalone BingX TA Context tester"
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
            f"[{interval}] Получение закрытых свечей...",
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
                f"{result['bars']} закрытых свечей | "
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
        format_ta(all_features)
    )
    print()


if __name__ == "__main__":
    main()
