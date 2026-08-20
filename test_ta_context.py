#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Standalone BingX TA Context tester.

Ничего не меняет в monitor.py.
Ничего не открывает/закрывает на BingX.
Только:
    1) получает OHLCV с BingX,
    2) берёт только ЗАКРЫТЫЕ свечи,
    3) считает TA для 1h / 4h / 1d,
    4) печатает готовый Telegram-блок.

Использование:

    python test_ta_context.py --symbol QTUM

или:

    python test_ta_context.py --symbol QTUM-USDT

Зависимости:

    pip install requests pandas pandas-ta-classic

Переменные окружения:

    BINGX_API_KEY
    BINGX_SECRET_KEY

Для текущей архитектуры можно использовать тот же BASE_URL,
который уже используется в bingx_client.py:

    BINGX_BASE_URL=https://open-api-vst.bingx.com

Для live:

    BINGX_BASE_URL=https://open-api.bingx.com
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import math
import os
import sys
import time
from decimal import Decimal
from urllib.parse import urlencode

import pandas as pd
import requests

try:
    import pandas_ta_classic as ta
except ImportError:
    print(
        "ERROR: не установлен pandas-ta-classic.\n"
        "Установите:\n"
        "    pip install requests pandas pandas-ta-classic"
    )
    sys.exit(1)


# ============================================================
# CONFIG
# ============================================================

API_KEY = os.environ.get("BINGX_API_KEY", "").strip()
SECRET_KEY = os.environ.get("BINGX_SECRET_KEY", "").strip()
BASE_URL = os.environ.get("BINGX_BASE_URL", "").strip().rstrip("/")

if not API_KEY:
    raise RuntimeError(
        "Environment variable BINGX_API_KEY is not set"
    )

if not SECRET_KEY:
    raise RuntimeError(
        "Environment variable BINGX_SECRET_KEY is not set"
    )

if not BASE_URL:
    raise RuntimeError(
        "Environment variable BINGX_BASE_URL is not set"
    )

KLINE_PATH = "/openApi/swap/v3/quote/klines"

TIMEFRAMES = ("1h", "4h", "1d")

# Берём больше, чем нужно для индикаторов.
KLINE_LIMIT = 250

# Сколько последних значений использовать для percentile.
PERCENTILE_WINDOW = 200

REQUEST_TIMEOUT = 15


# ============================================================
# HELPERS
# ============================================================

def normalize_symbol(symbol: str) -> str:
    """
    QTUM -> QTUM-USDT
    QTUMUSDT -> QTUM-USDT
    QTUM-USDT -> QTUM-USDT
    """
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
    """
    BingX HMAC-SHA256 signature.
    """
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
    """
    Выполняет signed request к BingX.

    Для этого теста подписываем запрос так же,
    как текущий bingx_client.py.
    """
    if not API_KEY or not SECRET_KEY:
        raise RuntimeError(
            "Не заданы BINGX_API_KEY / BINGX_SECRET_KEY"
        )

    params = dict(params or {})

    params["timestamp"] = str(int(time.time() * 1000))

    params["signature"] = sign_params(params)

    headers = {
        "X-BX-APIKEY": API_KEY,
    }

    url = BASE_URL + path

    response = requests.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"BingX вернул не-JSON ответ: {response.text[:500]}"
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

def fetch_klines(
    symbol: str,
    interval: str,
    limit: int = KLINE_LIMIT,
) -> pd.DataFrame:
    """
    Получает K-lines и оставляет только закрытые свечи.
    """

    data = request_signed(
        "GET",
        KLINE_PATH,
        {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        },
    )

    rows = data.get("data")

    if not isinstance(rows, list):
        raise RuntimeError(
            f"{symbol} {interval}: BingX data не является list"
        )

    parsed = []

    now_ms = int(time.time() * 1000)

    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue

        if len(row) < 7:
            continue

        try:
            open_time = int(row[0])
            close_time = int(row[6])

            # Пропускаем ещё незакрытую свечу.
            if close_time >= now_ms:
                continue

            parsed.append(
                {
                    "open_time": open_time,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "close_time": close_time,
                }
            )

        except (TypeError, ValueError):
            continue

    if len(parsed) < 100:
        raise RuntimeError(
            f"{symbol} {interval}: "
            f"слишком мало закрытых свечей: {len(parsed)}"
        )

    df = pd.DataFrame(parsed)

    df = df.sort_values("close_time").drop_duplicates(
        subset=["close_time"],
        keep="last",
    )

    df.reset_index(drop=True, inplace=True)

    return df


# ============================================================
# INDICATORS
# ============================================================

def percentile_of_last(
    series: pd.Series,
    window: int = PERCENTILE_WINDOW,
) -> float | None:
    """
    Percentile текущего последнего значения относительно
    предыдущих значений.

    Например:
        84.0
    означает, что текущее значение выше примерно 84%
    исторических значений выбранного окна.
    """

    s = pd.to_numeric(series, errors="coerce").dropna()

    if len(s) < 30:
        return None

    s = s.tail(window)

    if len(s) < 10:
        return None

    current = float(s.iloc[-1])

    # Rank percentile.
    rank = (s <= current).sum()

    pct = (rank / len(s)) * 100.0

    return round(float(pct), 1)


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


def calculate_features(df: pd.DataFrame) -> dict:
    """
    Рассчитывает TA.

    Ничего из этих признаков не влияет на торговое решение.
    """

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

    if adx is None or adx.empty:
        work["adx14"] = float("nan")
        work["plus_di14"] = float("nan")
        work["minus_di14"] = float("nan")
    else:
        adx_col = "ADX_14"
        dmp_col = "DMP_14"
        dmn_col = "DMN_14"

        work["adx14"] = (
            adx[adx_col]
            if adx_col in adx.columns
            else float("nan")
        )

        work["plus_di14"] = (
            adx[dmp_col]
            if dmp_col in adx.columns
            else float("nan")
        )

        work["minus_di14"] = (
            adx[dmn_col]
            if dmn_col in adx.columns
            else float("nan")
        )

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
        work["atr14"] / work["close"] * 100.0
    )

    # ATR percentile
    work["atr_pctile"] = (
        work["atr_pct"]
        .rolling(PERCENTILE_WINDOW, min_periods=20)
        .apply(
            lambda x: (
                (x <= x[-1]).sum() / len(x) * 100.0
            ),
            raw=True,
        )
    )

    # --------------------------------------------------------
    # Bollinger Bands
    # --------------------------------------------------------

    bb = ta.bbands(
        work["close"],
        length=20,
        std=2.0,
    )

    if bb is not None and not bb.empty:
        lower_col = "BBL_20_2.0"
        middle_col = "BBM_20_2.0"
        upper_col = "BBU_20_2.0"
        bandwidth_col = "BBB_20_2.0"

        if all(
            c in bb.columns
            for c in (
                lower_col,
                middle_col,
                upper_col,
            )
        ):
            work["bb_lower"] = bb[lower_col]
            work["bb_middle"] = bb[middle_col]
            work["bb_upper"] = bb[upper_col]

        if bandwidth_col in bb.columns:
            work["bb_width"] = bb[bandwidth_col]

        else:
            work["bb_width"] = (
                (
                    work["bb_upper"]
                    - work["bb_lower"]
                )
                / work["bb_middle"]
            )

    else:
        work["bb_width"] = float("nan")

    work["bb_width_pctile"] = (
        work["bb_width"]
        .rolling(PERCENTILE_WINDOW, min_periods=20)
        .apply(
            lambda x: (
                (x <= x[-1]).sum() / len(x) * 100.0
            ),
            raw=True,
        )
    )

    # %B
    try:
        work["bb_pctb"] = (
            (work["close"] - work["bb_lower"])
            / (work["bb_upper"] - work["bb_lower"])
        )
    except Exception:
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
        hist_col = "MACDh_12_26_9"

        if hist_col in macd.columns:
            work["macd_hist"] = macd[hist_col]
        else:
            work["macd_hist"] = float("nan")

    else:
        work["macd_hist"] = float("nan")

    # Нормированное значение:
    # MACD histogram / price * 100
    work["macd_hist_pct"] = (
        work["macd_hist"]
        / work["close"]
        * 100.0
    )

    # --------------------------------------------------------
    # LAST CLOSED CANDLE
    # --------------------------------------------------------

    last = work.iloc[-1]

    # --------------------------------------------------------
    # EMA STRUCTURE
    # --------------------------------------------------------

    ema20 = safe_float(last.get("ema20"))
    ema50 = safe_float(last.get("ema50"))
    ema200 = safe_float(last.get("ema200"))

    if None in (ema20, ema50, ema200):
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
    # DI
    # --------------------------------------------------------

    adx_value = safe_float(last.get("adx14"))
    plus_di = safe_float(last.get("plus_di14"))
    minus_di = safe_float(last.get("minus_di14"))

    if plus_di is None or minus_di is None:
        di_direction = "neutral"

    elif plus_di > minus_di:
        di_direction = "bullish"

    elif plus_di < minus_di:
        di_direction = "bearish"

    else:
        di_direction = "neutral"

    # --------------------------------------------------------
    # MACD direction
    # --------------------------------------------------------

    macd_hist = safe_float(last.get("macd_hist"))
    macd_hist_pct = safe_float(last.get("macd_hist_pct"))

    if macd_hist is None:
        macd_direction = "N/A"

    elif macd_hist > 0:
        macd_direction = "↑"

    elif macd_hist < 0:
        macd_direction = "↓"

    else:
        macd_direction = "→"

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    rsi = safe_float(last.get("rsi14"))

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    atr_pct = safe_float(last.get("atr_pct"))

    atr_pctile = percentile_of_last(
        work["atr_pct"],
        PERCENTILE_WINDOW,
    )

    # --------------------------------------------------------
    # BB
    # --------------------------------------------------------

    bb_width_pctile = percentile_of_last(
        work["bb_width"],
        PERCENTILE_WINDOW,
    )

    bb_pctb = safe_float(last.get("bb_pctb"))

    return {
        "close": safe_float(last.get("close")),
        "close_time": int(last["close_time"]),

        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,

        "ema_structure": ema_structure,
        "ema_direction": ema_direction,

        "rsi14": rsi,

        "adx14": adx_value,
        "plus_di14": plus_di,
        "minus_di14": minus_di,
        "di_direction": di_direction,

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

def format_number(
    value,
    decimals: int = 1,
) -> str:
    if value is None:
        return "—"

    return f"{value:.{decimals}f}"


def format_ema_direction(direction: str) -> str:
    if direction == "bullish":
        return "🟢"

    if direction == "bearish":
        return "🔴"

    if direction == "mixed":
        return "🟡"

    return "⚪"


def format_ta_block(
    symbol: str,
    features: dict[str, dict],
) -> str:

    lines = []

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("📊 Technical Context")
    lines.append("")

    for tf in TIMEFRAMES:
        f = features.get(tf)

        lines.append(tf.upper())

        if f is None:
            lines.append("ERROR: данные недоступны")
            lines.append("")
            continue

        # EMA
        ema_icon = format_ema_direction(
            f["ema_direction"]
        )

        lines.append(
            f"EMA {f['ema_structure']} {ema_icon}"
        )

        # RSI
        lines.append(
            f"RSI {format_number(f['rsi14'], 1)}"
        )

        # ADX + DI
        adx = format_number(
            f["adx14"],
            1,
        )

        plus_di = format_number(
            f["plus_di14"],
            1,
        )

        minus_di = format_number(
            f["minus_di14"],
            1,
        )

        if (
            f["plus_di14"] is not None
            and f["minus_di14"] is not None
        ):
            if f["plus_di14"] > f["minus_di14"]:
                di_text = (
                    f"+DI {plus_di} > -DI {minus_di}"
                )
            elif f["plus_di14"] < f["minus_di14"]:
                di_text = (
                    f"+DI {plus_di} < -DI {minus_di}"
                )
            else:
                di_text = (
                    f"+DI {plus_di} = -DI {minus_di}"
                )

        else:
            di_text = "+DI — · -DI —"

        lines.append(
            f"ADX {adx} | {di_text}"
        )

        # ATR
        atr = format_number(
            f["atr_pct"],
            2,
        )

        atr_pctile = format_number(
            f["atr_pctile"],
            0,
        )

        lines.append(
            f"ATR {atr}% | {atr_pctile}%ile"
        )

        # Bollinger
        bb_pctile = format_number(
            f["bb_width_pctile"],
            0,
        )

        lines.append(
            f"BB Width {bb_pctile}%ile"
        )

        # MACD
        macd_arrow = f["macd_direction"]

        macd_norm = f["macd_hist_pct"]

        if macd_norm is None:
            macd_line = f"MACD Hist {macd_arrow}"
        else:
            macd_line = (
                f"MACD Hist {macd_arrow} "
                f"({macd_norm:+.3f}%)"
            )

        lines.append(macd_line)

        lines.append("")

    return "\n".join(lines).rstrip()


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

    symbol = normalize_symbol(args.symbol)

    print()
    print("=" * 70)
    print(f"BingX TA Context TEST: {symbol}")
    print("=" * 70)
    print()

    features = {}

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

            result = calculate_features(df)

            features[interval] = result

            close_time = pd.to_datetime(
                result["close_time"],
                unit="ms",
                utc=True,
            )

            print(
                f"[{interval}] OK: "
                f"{result['bars']} свечей | "
                f"last close={result['close']} | "
                f"closed={close_time}"
            )

        except Exception as exc:
            print(
                f"[{interval}] ERROR: {exc}",
                file=sys.stderr,
            )

    if not features:
        raise SystemExit(
            "Не удалось получить TA ни для одного timeframe."
        )

    print()
    print(
        format_ta_block(
            symbol,
            features,
        )
    )
    print()

    print("━━━━━━━━━━━━━━━━━━")
    print(
        "ℹ️ TA используется только как контекст. "
        "Торговое решение не изменяется."
    )
    print()


if __name__ == "__main__":
    main()
