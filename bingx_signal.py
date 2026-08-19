#!/usr/bin/env python3
"""
bingx_signal_lab.py
===================

Standalone READ-ONLY research collector for coinalyze-monitor.

Purpose
-------
Collect BingX context alongside the existing Coinalyze snapshots so that
additional features can be evaluated later without touching production logic.

This module deliberately does NOT:
- import monitor.py
- import conditions.py
- modify production scores
- send Telegram alerts
- open/cancel/close orders
- generate LONG/SHORT decisions
- calculate a trading score
- veto production signals

It only:
1. Reads the latest Coinalyze snapshots from market_history.jsonl
2. Fetches CLOSED BingX 15m / 1h candles
3. Fetches current BingX Open Interest
4. Persists BingX OI observations locally
5. Calculates research features:
   - ATR(14) 1h
   - confirmed 1h swing structure
   - nearest confirmed resistance
   - distance to resistance %
   - distance to resistance in ATR
   - 1h volume ratio vs previous SMA(20)
   - 4h relative strength vs BTC
   - BingX OI z-score once enough history exists
   - 30d ATR compression once enough history exists
6. Writes shadow_bingx.jsonl

Leakage rules
-------------
- Candle usable only when close_time < snapshot_ts
- Swing high/low usable only after confirmation candles have CLOSED
- OI usable only when oi_timestamp <= snapshot_ts
- Future values are never substituted for missing values
- Current forming candle is never used

Important
---------
The module is a collector, not an evaluator.

MFE/MAE and forward-return labeling should be performed later by a separate
shadow_evaluator.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import requests


# ============================================================================
# Configuration
# ============================================================================

BASE_DIR = Path(__file__).resolve().parent

MARKET_HISTORY_FILE = Path(
    os.environ.get(
        "MARKET_HISTORY_FILE",
        str(BASE_DIR / "market_history.jsonl"),
    )
)

SHADOW_FILE = Path(
    os.environ.get(
        "BINGX_SHADOW_FILE",
        str(BASE_DIR / "shadow_bingx.jsonl"),
    )
)

OI_HISTORY_FILE = Path(
    os.environ.get(
        "BINGX_OI_HISTORY_FILE",
        str(BASE_DIR / "bingx_oi_history.jsonl"),
    )
)

STATE_FILE = Path(
    os.environ.get(
        "BINGX_LAB_STATE_FILE",
        str(BASE_DIR / "bingx_signal_lab_state.json"),
    )
)

BINGX_BASE_URL = os.environ.get(
    "BINGX_BASE_URL",
    "https://open-api.bingx.com",
)

KLINES_ENDPOINT = "/openApi/swap/v3/quote/klines"
OI_ENDPOINT = "/openApi/swap/v2/quote/openInterest"

REQUEST_TIMEOUT_SEC = float(
    os.environ.get(
        "BINGX_LAB_TIMEOUT_SEC",
        "15",
    )
)

RECV_WINDOW_MS = int(
    os.environ.get(
        "BINGX_LAB_RECV_WINDOW_MS",
        "5000",
    )
)

# Keep modest because this script is a research collector and should not
# generate unnecessary API load.
KLINE_LIMIT = int(
    os.environ.get(
        "BINGX_LAB_KLINE_LIMIT",
        "750",
    )
)

ATR_PERIOD = int(
    os.environ.get(
        "BINGX_LAB_ATR_PERIOD",
        "14",
    )
)

SWING_ORDER = int(
    os.environ.get(
        "BINGX_LAB_SWING_ORDER",
        "5",
    )
)

VOLUME_SMA_PERIOD = int(
    os.environ.get(
        "BINGX_LAB_VOLUME_SMA_PERIOD",
        "20",
    )
)

# 720 hourly observations ~= 30 days.
ATR_COMPRESSION_WINDOW = int(
    os.environ.get(
        "BINGX_LAB_ATR_COMPRESSION_WINDOW",
        "720",
    )
)

OI_HISTORY_DAYS = int(
    os.environ.get(
        "BINGX_LAB_OI_HISTORY_DAYS",
        "14",
    )
)

# Need enough observations before producing a z-score.
OI_MIN_OBS = int(
    os.environ.get(
        "BINGX_LAB_OI_MIN_OBS",
        "72",
    )
)

RELATIVE_STRENGTH_HOURS = int(
    os.environ.get(
        "BINGX_LAB_RELATIVE_STRENGTH_HOURS",
        "4",
    )
)

KLINE_STALE_SEC_1H = int(
    os.environ.get(
        "BINGX_LAB_KLINE_STALE_SEC_1H",
        str(75 * 60),
    )
)

KLINE_STALE_SEC_15M = int(
    os.environ.get(
        "BINGX_LAB_KLINE_STALE_SEC_15M",
        str(30 * 60),
    )
)

OI_STALE_SEC = int(
    os.environ.get(
        "BINGX_LAB_OI_STALE_SEC",
        str(10 * 60),
    )
)

# BingX market endpoints are rate-limited.
REQUEST_MIN_INTERVAL_SEC = float(
    os.environ.get(
        "BINGX_LAB_REQUEST_MIN_INTERVAL_SEC",
        "1.05",
    )
)

LOOP_SEC = int(
    os.environ.get(
        "BINGX_LAB_LOOP_SEC",
        "300",
    )
)

LOG_LEVEL = os.environ.get(
    "BINGX_LAB_LOG_LEVEL",
    "INFO",
).upper()


logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("bingx_signal_lab")


# ============================================================================
# Data types
# ============================================================================

@dataclass(frozen=True)
class Kline:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int
    quote_volume: Optional[float] = None
    trades: Optional[int] = None
    taker_buy_base: Optional[float] = None
    taker_buy_quote: Optional[float] = None

    def raw_array(self) -> list[Any]:
        return [
            self.open_time_ms,
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.close_time_ms,
            self.quote_volume,
            self.trades,
            self.taker_buy_base,
            self.taker_buy_quote,
        ]


class BingXLabError(RuntimeError):
    pass


# ============================================================================
# Generic utilities
# ============================================================================

def parse_timestamp(value: Any) -> Optional[int]:
    """
    Normalize a timestamp into UNIX seconds.

    Supports both:
    - seconds
    - milliseconds
    """
    try:
        x = int(float(value))
    except (TypeError, ValueError):
        return None

    if x <= 0:
        return None

    if x > 10_000_000_000:
        return x // 1000

    return x


def finite_float(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(x):
        return None

    return x


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []

    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()

                if not line:
                    continue

                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    log.warning(
                        "%s: invalid JSON on line %d",
                        path.name,
                        line_no,
                    )
                    continue

                if isinstance(item, dict):
                    records.append(item)

    except OSError as exc:
        log.warning(
            "Unable to read %s: %s",
            path,
            exc,
        )

    return records


def append_jsonl(
    path: Path,
    record: dict[str, Any],
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with path.open(
        "a",
        encoding="utf-8",
    ) as f:
        f.write(
            json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        f.flush()


def atomic_write_json(
    path: Path,
    value: Any,
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp_path = path.with_name(
        path.name + ".tmp"
    )

    tmp_path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    tmp_path.replace(path)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}

    try:
        value = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )
    except (
        OSError,
        json.JSONDecodeError,
    ):
        log.warning(
            "Cannot read state file %s; using empty state",
            STATE_FILE.name,
        )
        return {}

    return value if isinstance(value, dict) else {}


def get_row_value(
    row: dict[str, Any],
    *names: str,
) -> Any:
    """
    Supports direct fields and a few common nested layouts without assuming
    a specific production schema.
    """
    for name in names:
        if name in row:
            return row.get(name)

    for container_name in (
        "market",
        "metrics",
        "data",
        "snapshot",
    ):
        container = row.get(container_name)

        if not isinstance(container, dict):
            continue

        for name in names:
            if name in container:
                return container.get(name)

    return None


def extract_symbol(
    row: dict[str, Any],
) -> Optional[str]:
    raw = (
        row.get("symbol")
        or row.get("ticker")
        or row.get("contract")
    )

    if raw is None:
        return None

    symbol = str(raw).strip().upper()

    return symbol or None


def extract_snapshot_ts(
    row: dict[str, Any],
) -> Optional[int]:
    return parse_timestamp(
        get_row_value(
            row,
            "ts",
            "timestamp",
            "snapshot_ts",
        )
    )


# ============================================================================
# BingX API client
# ============================================================================

class BingXMarketClient:
    """
    Read-only BingX market API client.

    No signed trading endpoints are used.
    """

    def __init__(self) -> None:
        self.session = requests.Session()

        self.last_request_monotonic: dict[
            str,
            float,
        ] = {}

    @staticmethod
    def to_bingx_symbol(
        symbol: str,
    ) -> str:
        symbol = symbol.strip().upper()

        if not symbol:
            raise ValueError(
                "Empty symbol"
            )

        if "-" in symbol:
            return symbol

        if symbol.endswith("USDT"):
            return (
                f"{symbol[:-4]}-USDT"
            )

        if symbol.endswith("USD"):
            return (
                f"{symbol[:-3]}-USD"
            )

        return symbol

    def _rate_limit(
        self,
        key: str,
    ) -> None:
        previous = self.last_request_monotonic.get(
            key
        )

        if previous is not None:
            elapsed = (
                time.monotonic()
                - previous
            )

            remaining = (
                REQUEST_MIN_INTERVAL_SEC
                - elapsed
            )

            if remaining > 0:
                time.sleep(remaining)

        self.last_request_monotonic[key] = (
            time.monotonic()
        )

    def _get(
        self,
        endpoint: str,
        params: dict[str, Any],
        rate_key: str,
    ) -> Any:
        self._rate_limit(rate_key)

        query = dict(params)

        # Explicit timestamp on every request.
        query["timestamp"] = int(
            time.time() * 1000
        )

        query["recvWindow"] = (
            RECV_WINDOW_MS
        )

        url = (
            f"{BINGX_BASE_URL}"
            f"{endpoint}"
        )

        response = self.session.get(
            url,
            params=query,
            timeout=REQUEST_TIMEOUT_SEC,
        )

        response.raise_for_status()

        payload = response.json()

        if not isinstance(
            payload,
            dict,
        ):
            raise BingXLabError(
                f"Unexpected response from {endpoint}"
            )

        code = payload.get("code")

        if code not in (
            None,
            0,
            "0",
        ):
            raise BingXLabError(
                f"{endpoint}: "
                f"code={code}, "
                f"msg={payload.get('msg')}"
            )

        return payload.get("data")

    def fetch_klines(
        self,
        symbol: str,
        interval: str,
        snapshot_ts: int,
    ) -> list[Kline]:
        """
        Fetch candles and KEEP ONLY:
            candle.close_time < snapshot_ts

        This is the final local leakage guard.
        """
        bingx_symbol = (
            self.to_bingx_symbol(symbol)
        )

        end_time_ms = (
            snapshot_ts * 1000
        ) - 1

        data = self._get(
            KLINES_ENDPOINT,
            {
                "symbol": bingx_symbol,
                "interval": interval,
                "endTime": end_time_ms,
                "limit": KLINE_LIMIT,
            },
            f"klines:{interval}",
        )

        if not isinstance(
            data,
            list,
        ):
            raise BingXLabError(
                f"Unexpected kline payload "
                f"for {bingx_symbol} {interval}"
            )

        candles: list[Kline] = []

        for item in data:
            if not isinstance(
                item,
                (list, tuple),
            ):
                continue

            if len(item) < 7:
                continue

            try:
                candle = Kline(
                    open_time_ms=int(
                        item[0]
                    ),
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume=float(item[5]),
                    close_time_ms=int(
                        item[6]
                    ),
                    quote_volume=(
                        float(item[7])
                        if len(item) > 7
                        and item[7] is not None
                        else None
                    ),
                    trades=(
                        int(item[8])
                        if len(item) > 8
                        and item[8] is not None
                        else None
                    ),
                    taker_buy_base=(
                        float(item[9])
                        if len(item) > 9
                        and item[9] is not None
                        else None
                    ),
                    taker_buy_quote=(
                        float(item[10])
                        if len(item) > 10
                        and item[10] is not None
                        else None
                    ),
                )
            except (
                TypeError,
                ValueError,
                OverflowError,
            ):
                continue

            if (
                candle.close_time_ms
                >= snapshot_ts * 1000
            ):
                continue

            if (
                candle.close_time_ms
                <= candle.open_time_ms
            ):
                continue

            if candle.low < 0:
                continue

            if candle.high < candle.low:
                continue

            if candle.high < max(
                candle.open,
                candle.close,
            ):
                continue

            if candle.low > min(
                candle.open,
                candle.close,
            ):
                continue

            if candle.volume < 0:
                continue

            candles.append(candle)

        candles.sort(
            key=lambda item: (
                item.open_time_ms
            )
        )

        return candles

    def fetch_open_interest(
        self,
        symbol: str,
    ) -> dict[str, Any]:
        bingx_symbol = (
            self.to_bingx_symbol(symbol)
        )

        data = self._get(
            OI_ENDPOINT,
            {
                "symbol": bingx_symbol,
            },
            "openInterest",
        )

        if not isinstance(
            data,
            dict,
        ):
            raise BingXLabError(
                f"Unexpected OI payload "
                f"for {bingx_symbol}"
            )

        return {
            "bingx_symbol": data.get(
                "symbol",
                bingx_symbol,
            ),
            "open_interest": finite_float(
                data.get(
                    "openInterest"
                )
            ),
            "timestamp_sec": parse_timestamp(
                data.get("time")
            ),
        }


# ============================================================================
# Technical/research functions
# ============================================================================

def true_range(
    previous_close: float,
    high: float,
    low: float,
) -> float:
    return max(
        high - low,
        abs(high - previous_close),
        abs(low - previous_close),
    )


def atr_wilder(
    candles: list[Kline],
    period: int = ATR_PERIOD,
) -> Optional[float]:
    """
    Wilder ATR.

    Since candles passed here are already closed, there is no candle leakage.
    """
    if len(candles) < period + 1:
        return None

    tr_values: list[float] = []

    for i in range(
        1,
        len(candles),
    ):
        previous = candles[i - 1]
        current = candles[i]

        tr_values.append(
            true_range(
                previous_close=previous.close,
                high=current.high,
                low=current.low,
            )
        )

    if len(tr_values) < period:
        return None

    atr = (
        sum(
            tr_values[:period]
        )
        / period
    )

    for tr in tr_values[period:]:
        atr = (
            (
                (period - 1)
                * atr
            )
            + tr
        ) / period

    return atr


def rolling_atr_series(
    candles: list[Kline],
    period: int = ATR_PERIOD,
) -> list[Optional[float]]:
    """
    ATR value aligned to each candle index.

    ATR[i] uses information through candle i only.
    """
    result: list[
        Optional[float]
    ] = [None] * len(candles)

    if len(candles) < period + 1:
        return result

    tr: list[
        Optional[float]
    ] = [None] * len(candles)

    for i in range(
        1,
        len(candles),
    ):
        tr[i] = true_range(
            previous_close=(
                candles[i - 1].close
            ),
            high=candles[i].high,
            low=candles[i].low,
        )

    first_values = [
        x
        for x in tr[1 : period + 1]
        if x is not None
    ]

    if len(first_values) < period:
        return result

    atr = (
        sum(first_values)
        / period
    )

    result[period] = atr

    for i in range(
        period + 1,
        len(candles),
    ):
        tr_value = tr[i]

        if tr_value is None:
            continue

        atr = (
            (
                (period - 1)
                * atr
            )
            + tr_value
        ) / period

        result[i] = atr

    return result


def confirmed_swing_points(
    candles: list[Kline],
    order: int = SWING_ORDER,
) -> tuple[
    list[int],
    list[int],
]:
    """
    Fractal-style swing detection with explicit right-side confirmation.

    For a swing high at index i:

        high[i] > highs of left `order` candles
        high[i] >= highs of right `order` candles

    Because all candles in the supplied series are already CLOSED before T,
    the right-side confirmation candles are known at T.

    This avoids the common repaint/look-ahead problem.
    """
    swing_highs: list[int] = []
    swing_lows: list[int] = []

    if len(candles) < (
        2 * order + 1
    ):
        return (
            swing_highs,
            swing_lows,
        )

    for i in range(
        order,
        len(candles) - order,
    ):
        center = candles[i]

        left = candles[
            i - order : i
        ]

        right = candles[
            i + 1 : i + order + 1
        ]

        left_highs = [
            candle.high
            for candle in left
        ]

        right_highs = [
            candle.high
            for candle in right
        ]

        left_lows = [
            candle.low
            for candle in left
        ]

        right_lows = [
            candle.low
            for candle in right
        ]

        if (
            center.high
            > max(left_highs)
            and center.high
            >= max(right_highs)
        ):
            swing_highs.append(i)

        if (
            center.low
            < min(left_lows)
            and center.low
            <= min(right_lows)
        ):
            swing_lows.append(i)

    return (
        swing_highs,
        swing_lows,
    )


def calculate_structure(
    candles: list[Kline],
) -> dict[str, Any]:
    swing_highs, swing_lows = (
        confirmed_swing_points(
            candles
        )
    )

    result: dict[str, Any] = {
        "label": "INSUFFICIENT_DATA",
        "last_swing_high": None,
        "previous_swing_high": None,
        "last_swing_low": None,
        "previous_swing_low": None,
        "confirmed_swing_high_count": len(
            swing_highs
        ),
        "confirmed_swing_low_count": len(
            swing_lows
        ),
    }

    if len(swing_highs) >= 2:
        previous_index = swing_highs[-2]
        latest_index = swing_highs[-1]

        result[
            "previous_swing_high"
        ] = {
            "price": candles[
                previous_index
            ].high,
            "close_time_ms": candles[
                previous_index
            ].close_time_ms,
        }

        result[
            "last_swing_high"
        ] = {
            "price": candles[
                latest_index
            ].high,
            "close_time_ms": candles[
                latest_index
            ].close_time_ms,
        }

    if len(swing_lows) >= 2:
        previous_index = swing_lows[-2]
        latest_index = swing_lows[-1]

        result[
            "previous_swing_low"
        ] = {
            "price": candles[
                previous_index
            ].low,
            "close_time_ms": candles[
                previous_index
            ].close_time_ms,
        }

        result[
            "last_swing_low"
        ] = {
            "price": candles[
                latest_index
            ].low,
            "close_time_ms": candles[
                latest_index
            ].close_time_ms,
        }

    if (
        len(swing_highs) >= 2
        and len(swing_lows) >= 2
    ):
        latest_high = candles[
            swing_highs[-1]
        ].high

        previous_high = candles[
            swing_highs[-2]
        ].high

        latest_low = candles[
            swing_lows[-1]
        ].low

        previous_low = candles[
            swing_lows[-2]
        ].low

        if (
            latest_high > previous_high
            and latest_low > previous_low
        ):
            label = "HH_HL"

        elif (
            latest_high < previous_high
            and latest_low < previous_low
        ):
            label = "LH_LL"

        elif latest_high > previous_high:
            label = "HH"

        elif latest_high < previous_high:
            label = "LH"

        elif latest_low > previous_low:
            label = "HL"

        elif latest_low < previous_low:
            label = "LL"

        else:
            label = "MIXED"

        result["label"] = label

    return result


def nearest_confirmed_resistance(
    candles: list[Kline],
    price: float,
) -> Optional[dict[str, Any]]:
    if price <= 0:
        return None

    swing_highs, _ = (
        confirmed_swing_points(
            candles
        )
    )

    candidates: list[
        dict[str, Any]
    ] = []

    for index in swing_highs:
        swing_price = candles[
            index
        ].high

        if swing_price <= price:
            continue

        candidates.append(
            {
                "price": swing_price,
                "close_time_ms": candles[
                    index
                ].close_time_ms,
                "bars_ago": (
                    len(candles)
                    - 1
                    - index
                ),
                "confirmation_bars": SWING_ORDER,
            }
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (
            item["price"]
        )
    )

    return candidates[0]


def previous_volume_ratio(
    candles: list[Kline],
    period: int = VOLUME_SMA_PERIOD,
) -> Optional[float]:
    """
    Current closed 1h volume divided by the average volume of the PREVIOUS
    `period` closed candles.

    This avoids using the current candle in its own benchmark.
    """
    if len(candles) < period + 1:
        return None

    current = candles[-1]

    baseline = [
        candle.volume
        for candle in candles[
            -period - 1 : -1
        ]
        if math.isfinite(
            candle.volume
        )
    ]

    if len(baseline) < period:
        return None

    mean_volume = (
        statistics.fmean(
            baseline
        )
    )

    if mean_volume <= 0:
        return None

    return (
        current.volume
        / mean_volume
    )


def relative_strength_vs_btc(
    alt_candles: list[Kline],
    btc_candles: list[Kline],
    hours: int = RELATIVE_STRENGTH_HOURS,
) -> Optional[float]:
    """
    Return:

        ALT return over N hourly candles
        -
        BTC return over same N hourly candles

    Both series must already be cut off at the same snapshot timestamp.
    """
    if (
        len(alt_candles)
        < hours + 1
        or len(btc_candles)
        < hours + 1
    ):
        return None

    alt_old = alt_candles[
        -1 - hours
    ].close

    alt_new = alt_candles[
        -1
    ].close

    btc_old = btc_candles[
        -1 - hours
    ].close

    btc_new = btc_candles[
        -1
    ].close

    if (
        alt_old <= 0
        or alt_new <= 0
        or btc_old <= 0
        or btc_new <= 0
    ):
        return None

    alt_return = (
        alt_new / alt_old
    ) - 1.0

    btc_return = (
        btc_new / btc_old
    ) - 1.0

    return (
        alt_return
        - btc_return
    )


def calculate_oi_zscore(
    current_oi: float,
    history_values: Iterable[float],
) -> Optional[float]:
    values = [
        value
        for value in history_values
        if math.isfinite(value)
        and value > 0
    ]

    if len(values) < OI_MIN_OBS:
        return None

    mean = statistics.fmean(
        values
    )

    std = statistics.pstdev(
        values
    )

    if std <= 0:
        return None

    return (
        current_oi - mean
    ) / std


def calculate_oi_zscore_from_file(
    symbol: str,
    current_oi: float,
    reference_ts: int,
) -> Optional[float]:
    cutoff_ts = (
        reference_ts
        - OI_HISTORY_DAYS * 86400
    )

    values: list[float] = []

    for record in load_jsonl(
        OI_HISTORY_FILE
    ):
        record_symbol = str(
            record.get(
                "symbol"
            ) or ""
        ).upper()

        if record_symbol != symbol.upper():
            continue

        record_ts = parse_timestamp(
            record.get(
                "oi_timestamp_sec"
            )
        )

        if record_ts is None:
            record_ts = parse_timestamp(
                record.get(
                    "observed_ts"
                )
            )

        value = finite_float(
            record.get(
                "open_interest"
            )
        )

        if (
            record_ts is None
            or value is None
        ):
            continue

        # Important:
        # Current/future observations relative to the feature timestamp are
        # excluded from the reference distribution.
        if (
            record_ts >= reference_ts
        ):
            continue

        if record_ts < cutoff_ts:
            continue

        values.append(value)

    # Bound memory / runtime if the JSONL grows very large.
    values = values[-5000:]

    return calculate_oi_zscore(
        current_oi=current_oi,
        history_values=values,
    )


def calculate_atr_compression(
    candles: list[Kline],
    current_atr: Optional[float],
    window: int = ATR_COMPRESSION_WINDOW,
) -> Optional[float]:
    """
    Current ATR / median of PREVIOUS ATR values.

    The current ATR itself is not included in the baseline.
    """
    if current_atr is None:
        return None

    atr_series = rolling_atr_series(
        candles
    )

    current_index = (
        len(candles) - 1
    )

    if current_index <= 0:
        return None

    start = max(
        0,
        current_index - window,
    )

    previous = [
        atr
        for atr in atr_series[
            start:current_index
        ]
        if atr is not None
        and atr > 0
    ]

    # 100 prior values is a conservative minimum for a meaningful regime
    # statistic. This is not a trading threshold, only a data sufficiency
    # guard.
    if len(previous) < 100:
        return None

    median_atr = statistics.median(
        previous
    )

    if median_atr <= 0:
        return None

    return (
        current_atr
        / median_atr
    )


# ============================================================================
# Coinalyze context
# ============================================================================

def latest_market_rows() -> dict[
    str,
    dict[str, Any],
]:
    """
    Return latest Coinalyze snapshot for each symbol.

    Historical rows remain untouched. We only need the newest row per symbol
    for a live collection run.
    """
    rows = load_jsonl(
        MARKET_HISTORY_FILE
    )

    latest: dict[
        str,
        dict[str, Any],
    ] = {}

    for row in rows:
        symbol = extract_symbol(
            row
        )

        snapshot_ts = extract_snapshot_ts(
            row
        )

        if (
            symbol is None
            or snapshot_ts is None
        ):
            continue

        previous = latest.get(
            symbol
        )

        if previous is None:
            latest[symbol] = row
            continue

        previous_ts = (
            extract_snapshot_ts(
                previous
            )
        )

        if (
            previous_ts is None
            or snapshot_ts > previous_ts
        ):
            latest[symbol] = row

    return latest


def align_open_interest(
    oi_payload: Optional[
        dict[str, Any]
    ],
    snapshot_ts: int,
) -> dict[str, Any]:
    if not oi_payload:
        return {
            "raw_value": None,
            "timestamp": None,
            "age_sec": None,
            "alignment": "missing",
        }

    oi_value = finite_float(
        oi_payload.get(
            "open_interest"
        )
    )

    oi_timestamp = parse_timestamp(
        oi_payload.get(
            "timestamp_sec"
        )
    )

    if (
        oi_value is None
        or oi_timestamp is None
    ):
        return {
            "raw_value": oi_value,
            "timestamp": oi_timestamp,
            "age_sec": None,
            "alignment": "missing",
        }

    # Future data is NEVER accepted.
    if (
        oi_timestamp > snapshot_ts
    ):
        return {
            "raw_value": None,
            "timestamp": oi_timestamp,
            "age_sec": None,
            "alignment": "future_not_allowed",
        }

    age = (
        snapshot_ts
        - oi_timestamp
    )

    if age > OI_STALE_SEC:
        return {
            "raw_value": oi_value,
            "timestamp": oi_timestamp,
            "age_sec": age,
            "alignment": "stale",
        }

    return {
        "raw_value": oi_value,
        "timestamp": oi_timestamp,
        "age_sec": age,
        "alignment": "ok",
    }


# ============================================================================
# OI history
# ============================================================================

def append_oi_observation(
    symbol: str,
    oi_payload: dict[str, Any],
    observed_ts: int,
) -> None:
    append_jsonl(
        OI_HISTORY_FILE,
        {
            "schema_version": (
                "bingx_oi_history_v0.2"
            ),
            "observed_ts": observed_ts,
            "symbol": symbol.upper(),
            "bingx_symbol": oi_payload.get(
                "bingx_symbol"
            ),
            "open_interest": oi_payload.get(
                "open_interest"
            ),
            "oi_timestamp_sec": oi_payload.get(
                "timestamp_sec"
            ),
        },
    )


# ============================================================================
# Record builder
# ============================================================================

def build_shadow_record(
    client: BingXMarketClient,
    symbol: str,
    coinalyze_row: dict[str, Any],
    capture_ts: int,
    btc_1h_candles: Optional[
        list[Kline]
    ],
    oi_payload: Optional[
        dict[str, Any]
    ],
) -> dict[str, Any]:
    snapshot_ts = (
        extract_snapshot_ts(
            coinalyze_row
        )
    )

    if snapshot_ts is None:
        raise ValueError(
            f"{symbol}: missing snapshot timestamp"
        )

    price = finite_float(
        get_row_value(
            coinalyze_row,
            "price",
            "last_price",
            "lastPrice",
        )
    )

    candles_15m = (
        client.fetch_klines(
            symbol=symbol,
            interval="15m",
            snapshot_ts=snapshot_ts,
        )
    )

    candles_1h = (
        client.fetch_klines(
            symbol=symbol,
            interval="1h",
            snapshot_ts=snapshot_ts,
        )
    )

    last_15m = (
        candles_15m[-1]
        if candles_15m
        else None
    )

    last_1h = (
        candles_1h[-1]
        if candles_1h
        else None
    )

    # If production snapshot does not expose price, using the latest CLOSED
    # BingX close is acceptable as a descriptive fallback. It is not used to
    # alter production logic.
    if (
        price is None
        and last_1h is not None
    ):
        price = last_1h.close

    if (
        price is None
        or price <= 0
    ):
        raise ValueError(
            f"{symbol}: invalid price"
        )

    atr_1h = atr_wilder(
        candles_1h,
        ATR_PERIOD,
    )

    structure_1h = (
        calculate_structure(
            candles_1h
        )
    )

    resistance = (
        nearest_confirmed_resistance(
            candles_1h,
            price,
        )
    )

    distance_res_pct = None
    distance_res_atr = None

    if resistance is not None:
        resistance_price = (
            resistance["price"]
        )

        distance_res_pct = (
            (
                resistance_price
                - price
            )
            / price
            * 100.0
        )

        if (
            atr_1h is not None
            and atr_1h > 0
        ):
            distance_res_atr = (
                (
                    resistance_price
                    - price
                )
                / atr_1h
            )

    volume_ratio = (
        previous_volume_ratio(
            candles_1h,
            VOLUME_SMA_PERIOD,
        )
    )

    compression = (
        calculate_atr_compression(
            candles_1h,
            atr_1h,
            ATR_COMPRESSION_WINDOW,
        )
    )

    relative_strength = None

    if (
        btc_1h_candles is not None
        and not symbol.startswith("BTC")
    ):
        relative_strength = (
            relative_strength_vs_btc(
                candles_1h,
                btc_1h_candles,
                RELATIVE_STRENGTH_HOURS,
            )
        )

    oi_alignment = (
        align_open_interest(
            oi_payload,
            snapshot_ts,
        )
    )

    oi_z = None

    if (
        oi_alignment[
            "alignment"
        ] == "ok"
        and oi_alignment[
            "raw_value"
        ] is not None
        and oi_alignment[
            "timestamp"
        ] is not None
    ):
        oi_z = (
            calculate_oi_zscore_from_file(
                symbol=symbol,
                current_oi=oi_alignment[
                    "raw_value"
                ],
                reference_ts=oi_alignment[
                    "timestamp"
                ],
            )
        )

    age_1h = None

    if last_1h is not None:
        age_1h = (
            snapshot_ts
            - int(
                last_1h.close_time_ms
                / 1000
            )
        )

    age_15m = None

    if last_15m is not None:
        age_15m = (
            snapshot_ts
            - int(
                last_15m.close_time_ms
                / 1000
            )
        )

    kline_1h_stale = (
        age_1h is None
        or age_1h < 0
        or age_1h
        > KLINE_STALE_SEC_1H
    )

    kline_15m_stale = (
        age_15m is None
        or age_15m < 0
        or age_15m
        > KLINE_STALE_SEC_15M
    )

    # Keep all production fields as context only.
    coinalyze_context = {
        "state": get_row_value(
            coinalyze_row,
            "state",
            "coinalyze_state",
        ),
        "price": get_row_value(
            coinalyze_row,
            "price",
            "last_price",
            "lastPrice",
        ),
        "price_chg24": get_row_value(
            coinalyze_row,
            "price_chg24",
            "price_change_24h",
        ),
        "volume24": get_row_value(
            coinalyze_row,
            "volume24",
            "volume_24h",
        ),
        "open_interest": get_row_value(
            coinalyze_row,
            "oi",
            "open_interest",
        ),
        "oi_chg4h_pct": get_row_value(
            coinalyze_row,
            "oi_chg4h_pct",
        ),
        "oi_chg24_pct": get_row_value(
            coinalyze_row,
            "oi_chg24_pct",
        ),
        "cvd24": get_row_value(
            coinalyze_row,
            "cvd24",
        ),
        "lls24": get_row_value(
            coinalyze_row,
            "lls24",
        ),
        "funding_oi_weighted": get_row_value(
            coinalyze_row,
            "fr_oiw",
            "funding_oi_weighted",
        ),
        "btc_corr7d": get_row_value(
            coinalyze_row,
            "btc_corr7d",
        ),
    }

    return {
        "schema_version": (
            "bingx_signal_lab_v0.2"
        ),

        "capture_ts": capture_ts,

        "snapshot_ts": snapshot_ts,

        "symbol": symbol,

        "coinalyze_context": (
            coinalyze_context
        ),

        "bingx_kline_15m": {
            "open_time": (
                int(
                    last_15m.open_time_ms
                    / 1000
                )
                if last_15m
                else None
            ),
            "close_time": (
                int(
                    last_15m.close_time_ms
                    / 1000
                )
                if last_15m
                else None
            ),
            "feature_age_sec": age_15m,
            "raw_array": (
                last_15m.raw_array()
                if last_15m
                else None
            ),
        },

        "bingx_kline_1h": {
            "open_time": (
                int(
                    last_1h.open_time_ms
                    / 1000
                )
                if last_1h
                else None
            ),
            "close_time": (
                int(
                    last_1h.close_time_ms
                    / 1000
                )
                if last_1h
                else None
            ),
            "feature_age_sec": age_1h,
            "raw_array": (
                last_1h.raw_array()
                if last_1h
                else None
            ),
        },

        "bingx_oi": {
            "raw_value": oi_alignment[
                "raw_value"
            ],
            "timestamp": oi_alignment[
                "timestamp"
            ],
            "age_sec": oi_alignment[
                "age_sec"
            ],
            "alignment": oi_alignment[
                "alignment"
            ],
            "z_score": (
                round(
                    oi_z,
                    6,
                )
                if oi_z is not None
                else None
            ),
        },

        "derived_features": {
            "atr_1h": (
                round(
                    atr_1h,
                    12,
                )
                if atr_1h is not None
                else None
            ),

            "atr_1h_pct": (
                round(
                    (
                        atr_1h
                        / price
                        * 100.0
                    ),
                    6,
                )
                if atr_1h is not None
                else None
            ),

            "atr_compression_30d": (
                round(
                    compression,
                    6,
                )
                if compression is not None
                else None
            ),

            "structure_1h": (
                structure_1h["label"]
            ),

            "structure_1h_detail": (
                structure_1h
            ),

            "nearest_resistance_price": (
                resistance["price"]
                if resistance is not None
                else None
            ),

            "nearest_resistance_close_time": (
                int(
                    resistance[
                        "close_time_ms"
                    ]
                    / 1000
                )
                if resistance is not None
                else None
            ),

            "nearest_resistance_bars_ago": (
                resistance["bars_ago"]
                if resistance is not None
                else None
            ),

            "resistance_confirmation_bars": (
                resistance[
                    "confirmation_bars"
                ]
                if resistance is not None
                else None
            ),

            "distance_resistance_pct": (
                round(
                    distance_res_pct,
                    6,
                )
                if distance_res_pct
                is not None
                else None
            ),

            "distance_resistance_atr": (
                round(
                    distance_res_atr,
                    6,
                )
                if distance_res_atr
                is not None
                else None
            ),

            "volume_ratio_1h": (
                round(
                    volume_ratio,
                    6,
                )
                if volume_ratio
                is not None
                else None
            ),

            "relative_strength_vs_btc_4h": (
                round(
                    relative_strength,
                    8,
                )
                if relative_strength
                is not None
                else None
            ),
        },

        "data_quality": {
            "kline_1h_stale": (
                kline_1h_stale
            ),

            "kline_15m_stale": (
                kline_15m_stale
            ),

            "kline_1h_age_sec": age_1h,

            "kline_15m_age_sec": (
                age_15m
            ),

            "oi_stale": (
                oi_alignment[
                    "alignment"
                ]
                == "stale"
            ),

            "oi_future_not_allowed": (
                oi_alignment[
                    "alignment"
                ]
                == "future_not_allowed"
            ),

            "oi_missing": (
                oi_alignment[
                    "alignment"
                ]
                == "missing"
            ),

            "all_required_candles_fresh": (
                not kline_1h_stale
                and not kline_15m_stale
            ),

            "capture_minus_snapshot_sec": max(
                0,
                capture_ts
                - snapshot_ts,
            ),
        },

        # Explicitly research-only.
        "decision": None,
        "score": None,
        "veto": None,
    }


# ============================================================================
# Live collection
# ============================================================================

def process_once() -> int:
    latest_rows = (
        latest_market_rows()
    )

    if not latest_rows:
        log.warning(
            "No valid rows found in %s",
            MARKET_HISTORY_FILE,
        )
        return 0

    capture_ts = int(
        time.time()
    )

    client = (
        BingXMarketClient()
    )

    state = load_state()

    last_written_by_symbol = state.get(
        "last_written_by_symbol",
        {},
    )

    if not isinstance(
        last_written_by_symbol,
        dict,
    ):
        last_written_by_symbol = {}

    # ------------------------------------------------------------------------
    # Fetch BTC 1h reference once.
    # ------------------------------------------------------------------------

    btc_row = None

    if "BTCUSDT" in latest_rows:
        btc_row = latest_rows[
            "BTCUSDT"
        ]
    else:
        for candidate, row in latest_rows.items():
            if (
                candidate.startswith("BTC")
                and candidate.endswith("USDT")
            ):
                btc_row = row
                break

    btc_candles: Optional[
        list[Kline]
    ] = None

    if btc_row is not None:
        btc_snapshot_ts = (
            extract_snapshot_ts(
                btc_row
            )
        )

        if btc_snapshot_ts is not None:
            try:
                btc_candles = (
                    client.fetch_klines(
                        "BTCUSDT",
                        "1h",
                        btc_snapshot_ts,
                    )
                )
            except Exception as exc:
                log.warning(
                    "BTC 1h reference unavailable: %s",
                    exc,
                )

    # ------------------------------------------------------------------------
    # Process symbols.
    # ------------------------------------------------------------------------

    records_written = 0

    for symbol, row in sorted(
        latest_rows.items()
    ):
        snapshot_ts = (
            extract_snapshot_ts(row)
        )

        if snapshot_ts is None:
            continue

        dedup_key = (
            f"{symbol}:{snapshot_ts}"
        )

        # If a new Coinalyze snapshot is not available, still sample OI.
        # This is important because BingX OI history is built locally.
        if (
            last_written_by_symbol.get(
                symbol
            )
            == dedup_key
        ):
            try:
                oi_payload = (
                    client.fetch_open_interest(
                        symbol
                    )
                )

                if (
                    oi_payload.get(
                        "open_interest"
                    )
                    is not None
                ):
                    append_oi_observation(
                        symbol=symbol,
                        oi_payload=oi_payload,
                        observed_ts=capture_ts,
                    )

            except Exception as exc:
                log.warning(
                    "[%s] OI-only sample failed: %s",
                    symbol,
                    exc,
                )

            continue

        try:
            # ---------------------------------------------------------------
            # Current OI
            # ---------------------------------------------------------------
            oi_payload = (
                client.fetch_open_interest(
                    symbol
                )
            )

            if (
                oi_payload.get(
                    "open_interest"
                )
                is not None
            ):
                append_oi_observation(
                    symbol=symbol,
                    oi_payload=oi_payload,
                    observed_ts=capture_ts,
                )

            # ---------------------------------------------------------------
            # Build feature record
            # ---------------------------------------------------------------
            record = build_shadow_record(
                client=client,
                symbol=symbol,
                coinalyze_row=row,
                capture_ts=capture_ts,
                btc_1h_candles=btc_candles,
                oi_payload=oi_payload,
            )

            append_jsonl(
                SHADOW_FILE,
                record,
            )

            last_written_by_symbol[
                symbol
            ] = dedup_key

            records_written += 1

        except requests.RequestException as exc:
            log.warning(
                "[%s] HTTP request failed: %s",
                symbol,
                exc,
            )

        except Exception as exc:
            log.warning(
                "[%s] shadow collection failed: %s",
                symbol,
                exc,
            )

    # ------------------------------------------------------------------------
    # Persist state
    # ------------------------------------------------------------------------

    state.update(
        {
            "schema_version": (
                "bingx_signal_lab_state_v0.2"
            ),
            "last_run_ts": capture_ts,
            "last_written_by_symbol": (
                last_written_by_symbol
            ),
        }
    )

    atomic_write_json(
        STATE_FILE,
        state,
    )

    log.info(
        "Shadow run complete: %d record(s) written",
        records_written,
    )

    return records_written


# ============================================================================
# Entrypoint
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Standalone read-only BingX research "
            "collector for coinalyze-monitor"
        )
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one collection cycle",
    )

    parser.add_argument(
        "--loop",
        action="store_true",
        help=(
            "Run continuously; default interval "
            "is 300 seconds"
        ),
    )

    args = parser.parse_args()

    if not args.once and not args.loop:
        args.once = True

    if args.once:
        process_once()
        return 0

    while True:
        started = time.time()

        try:
            process_once()

        except KeyboardInterrupt:
            log.info(
                "Interrupted by user"
            )
            return 0

        except Exception:
            log.exception(
                "Unhandled shadow-loop error"
            )

        elapsed = (
            time.time() - started
        )

        sleep_for = max(
            1,
            LOOP_SEC - int(elapsed),
        )

        time.sleep(
            sleep_for
        )


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
