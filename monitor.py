"""
monitor.py.
"""

import os, sys, time, json, shutil, hashlib, tempfile, uuid
import html as html_mod
import logging
from pathlib import Path
from typing import Optional
from bisect import bisect_right
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from statistics import median
import ta_context
import requests


try:
    from playwright_stealth import stealth_sync
    _STEALTH_AVAILABLE = True
except ImportError:
    _STEALTH_AVAILABLE = False
    def stealth_sync(page):
        pass


from conditions import (
    check_confirmed_path_a,
    check_confirmed_path_b,
    check_early_move,
    check_accumulation,
    signal_strength,
    window_quality,
    shadow_variants,
    SHADOW_VARIANTS,
    CONFIG as CONDITIONS_CONFIG,
)

BASE = Path(__file__).resolve().parent
MARKET_HISTORY_FILE = BASE / "market_history.jsonl"
TA_DIRECTION_BLOCKS_FILE = BASE / "ta_direction_blocks.jsonl"
SNAPSHOTS_FILE = BASE / "snapshots.jsonl"
HEARTBEAT_FILE = BASE / "heartbeat.jsonl"
WATCHLIST_FILE = BASE / "watchlist.json"
CALIBRATION_FILE = BASE / "calibration.jsonl"
TRADES_FILE = BASE / "trades.jsonl"
PENDING_FILE = BASE / "pending_trades.jsonl"
LIFECYCLE_STATE_FILE = BASE / "lifecycle_state.json"
SHADOW_SIGNALS_FILE = BASE / "shadow_signals.jsonl"
DISCOVERY_HISTORY_FILE = BASE / "discovery_history.jsonl"
RECONCILE_FILE = BASE / "reconciliation.jsonl"
DEBUG_HTML_FILE = BASE / "debug_page.html"
EXECUTION_EVENTS_FILE = BASE / "execution_events.jsonl"
LAST_SCRAPE_COMPLETE = True
LAST_SCRAPE_PAGE_ERRORS = []
COINALYZE_P_SID = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_IDS = [chat_id.strip() for chat_id in os.environ.get("TG_CHAT_IDS", "").split(",") if chat_id.strip()]
ENABLE_LLM = os.environ.get("ENABLE_LLM", "false").lower() == "true"
QWEN_API_KEY = os.environ.get("QWEN_API_KEY", "")
QWEN_BASE_URL = os.environ.get("QWEN_BASE_URL", "")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen-plus")
MAX_PAGES = 5
COINALYZE_URL = os.environ.get("COINALYZE_URL", "")
ENABLE_BINGX = os.environ.get("ENABLE_BINGX", "false").lower() == "true"
PROTECTION_LOGIC_VERSION = 2
STOP_MODE = "adaptive_volatility" 

ALLOW_NO_STEALTH = os.environ.get("ALLOW_NO_STEALTH", "false").lower() == "true"
MARKET_TTL_DAYS = 4
SNAPSHOTS_TTL_DAYS = 7
HEARTBEAT_TTL_DAYS = 3
LIFECYCLE_WINDOW_MIN = 90
MIN_SNAPS_LIFECYCLE = 5
MISS_EXIT_RUNS = 2
MISS_REMOVE_RUNS = 4
NEUTRAL_HYSTERESIS = 2
TRADE_SCHEMA_VERSION = 5
SIGNAL_LOGIC_VERSION = 3
LIFECYCLE_ENGINE_VERSION = 4
POSITION_MANAGER_VERSION = 2
HARD_EXIT_REASONS = {
    "EXCHANGE_CLOSED",
    "INVALIDATED",
    "EXHAUSTION",
    "DISTRIBUTION",
    "STOP_LOSS",
    "TIMEOUT",
}
SOFT_POSITION_REASONS = {
    "SIGNAL_DECAY",
    "MISSED",
    "DATA_STALE",
    "NEUTRAL",
}
ENGINE_VERSIONS = {
    "schema": TRADE_SCHEMA_VERSION,
    "signal": SIGNAL_LOGIC_VERSION,
    "lifecycle": LIFECYCLE_ENGINE_VERSION,
    "protection": PROTECTION_LOGIC_VERSION,
    "created": "2026-08-17",
    "conditions": CONDITIONS_CONFIG,
}
HASH_VERSION = "sha256_v1"
TRADE_TIMEOUT_MIN = 480 
BINGX_SKIP_NOTIFY_COOLDOWN_SEC = 4 * 3600
FEE_PCT = 0.10
SIGNAL_DECAY_MIN = 90
IDEA_REGISTRY_TTL_DAYS = 30
TRADE_HORIZONS = [30, 60, 120, 240, 360, 480, 600]
TRADE_WIN_PCT = 1.0
PENDING_GRACE_MIN = 10
PENDING_WAIT_MAX_MIN = 60
HORIZON_MAX_LAG_MIN = 15
PENDING = []
DISCOVERY = {}
PRICE_STALE_EXIT_MIN = 15
SHADOW_SIGNALS_TTL_DAYS = 7
RECONCILE_AUTOCLOSE = (os.environ.get("BINGX_RECONCILE_AUTOCLOSE", "false").lower() == "true")
RECONCILE_QTY_TOLERANCE = 0.02
SHADOW_STOP_LEVELS = (1.5, 2.5)
USE_SCHMITT = False
SCHMITT_ENTER = 9.5
SCHMITT_EXIT = 8.0
BTC_SYMBOLS = ("BTC", "BTCUSDT", "BTCUSD", "XBTUSD")
MARKET_PHASE_MODIFIER_ENABLED = False
COOLDOWN_BY_EXIT_REASON = {
    "STOP_LOSS": 120,
    "INVALIDATED": 60,
    "DISTRIBUTION": 60,
    "EXHAUSTION": 45,
    "EXCHANGE_CLOSED": 60,
    "TIMEOUT": 30,
}
PROTECTION_REASONS = {"STOP_LOSS"}
EXIT_PRIORITY = [
    "EXCHANGE_CLOSED",
    "INVALIDATED",
    "EXHAUSTION",
    "DISTRIBUTION",
    "STOP_LOSS",
    "TIMEOUT",
]
EXIT_CLASS = {
    "EXCHANGE_CLOSED": "EXTERNAL",
    "INVALIDATED": "SIGNAL",
    "EXHAUSTION": "SIGNAL",
    "DISTRIBUTION": "SIGNAL",
    "STOP_LOSS": "PROTECTION",
    "TIMEOUT": "LIFETIME_SOFT",
    "SIGNAL_DECAY": "LIFETIME_SOFT",
    "DATA_STALE": "DATA_SOFT",
    "MISSED": "DATA_SOFT",
    "NEUTRAL": "LIFETIME_SOFT",
}
STATE_RANK = {
    "ACCUMULATION": 1,
    "EARLY_MOVE": 2,
    "CONFIRMED_TREND": 3,
    "ACCELERATION": 4,
    "EXHAUSTION": 5,
    "DISTRIBUTION": 6,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("monitor")
MIN_FR_ZSCORE_OBS = 3


def _research_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def calc_entry_short_liq_share24(liq_short, liq_long):
    ls = _research_float(liq_short)
    ll = _research_float(liq_long)
    if ls is None or ll is None:
        return None
    total = ls + ll
    return round(ls / total, 6) if total > 0 else None


def calc_entry_liq_imbalance(liq_short, liq_long):
    ls = _research_float(liq_short)
    ll = _research_float(liq_long)
    if ls is None or ll is None:
        return None
    total = ls + ll
    return round((ls - ll) / total, 6) if total > 0 else None


def calc_entry_funding_oi_pressure(fr_oiw, oi_chg4h):
    fr = _research_float(fr_oiw)
    oi4h = _research_float(oi_chg4h)
    if fr is None or oi4h is None:
        return None
    return round(-fr * oi4h, 8)


def calc_entry_liquidation_intensity(liq_short, liq_long, oi_abs):
    ls = _research_float(liq_short)
    ll = _research_float(liq_long)
    oi = _research_float(oi_abs)
    if ls is None or ll is None or oi is None:
        return None
    total = ls + ll
    return round(total / oi, 8) if oi > 0 else None


def calc_entry_fr_oiw_zscore_from_hist(hist, current_fr):
    cur = _research_float(current_fr)
    if cur is None:
        return None
    vals = [
        _research_float(r.get("fr_oiw"))
        for r in (hist or [])
        if _research_float(r.get("fr_oiw")) is not None
    ]
    if len(vals) < MIN_FR_ZSCORE_OBS:
        return None
    mean_fr = sum(vals) / len(vals)
    std_fr = (sum((x - mean_fr) ** 2 for x in vals) / len(vals)) ** 0.5
    if std_fr == 0.0:
        return None
    return round((cur - mean_fr) / std_fr, 4)


def parse_number(raw):
    if raw is None:
        return None
    s = raw.strip().replace("$", "").replace("%", "").replace(",", "").replace("+", "")
    if s in ("", "n/a", "-", "—", "N/A"):
        return None
    mult = 1.0
    if s and s[-1].lower() in ("k", "m", "b", "t"):
        mult = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}[s[-1].lower()]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def now_ts():
    return int(time.time())


def esc(val):
    return html_mod.escape(str(val), quote=False)


def fmt_pct(val):
    if val is None:
        return "—"
    return f"{'+' if val>0 else ''}{val:.1f}%"


def fmt_num(val, suffix="", dec=1):
    if val is None:
        return "—"
    return f"{val:.{dec}f}{suffix}"


def fmt_price(val):
    if val is None:
        return "—"
    n = float(val)
    dec = 4 if abs(n) >= 0.01 else 10
    return f"{n:.{dec}f}".rstrip("0").rstrip(".")


def safe(val, default=0.0):
    return val if val is not None else default


def clamp(val, lo, hi):
    return max(lo, min(hi, val))


def valid_price(p):
    return p is not None and p > 0


def compute_adaptive_tp_sl(r: dict, snaps: list) -> tuple[float, list[dict]]:
    """
    Adaptive protection for NEW LONG positions.

    Model:
      - volatility = max(24h component, robust local range);
      - SL is the single risk unit R;
      - TP levels are expressed as multiples of R;
      - stronger OI/CVD/LLS continuation widens the TP ladder;
      - one global TP3 cap preserves the ladder geometry;
      - 20% / 25% / 35% + 20% runner.

    Protection is calculated once before opening a position.
    Existing positions must keep their already stored protection.
    """
    try:
        pc24 = abs(float(r.get("price_chg24") or 0.0))
    except (TypeError, ValueError):
        pc24 = 0.0

    prices = []

    for snap in snaps or []:
        try:
            price = float(snap.get("price"))
        except (TypeError, ValueError):
            continue

        if price > 0:
            prices.append(price)

    try:
        current_price = float(r.get("price") or 0.0)
    except (TypeError, ValueError):
        current_price = 0.0

    if current_price > 0 and (not prices or prices[-1] != current_price):
        prices.append(current_price)

    if len(prices) < 3 and pc24 <= 0:
        raise ValueError(
            f"insufficient volatility data: "
            f"prices={len(prices)} price_chg24={pc24}"
        )

    volatility_pct = pc24 / 3.0

    if len(prices) >= 3:
        ordered = sorted(prices)
        n = len(ordered)

        def _interp(index_float):
            lo = int(index_float)
            hi = min(lo + 1, n - 1)
            frac = index_float - lo
            return ordered[lo] + (ordered[hi] - ordered[lo]) * frac

        p10 = _interp((n - 1) * 0.10)
        p90 = _interp((n - 1) * 0.90)
        reference_price = median(ordered)

        if reference_price > 0 and p90 > p10:
            local_range_pct = (
                (p90 - p10) / reference_price
            ) * 100.0

            volatility_pct = max(
                volatility_pct,
                local_range_pct * 1.75,
            )

    if volatility_pct <= 0:
        raise ValueError(
            f"invalid volatility estimate: {volatility_pct}"
        )

    sl_pct = 5.00

    try:
        oi24 = float(r.get("oi_chg24_pct") or 0.0)
    except (TypeError, ValueError):
        oi24 = 0.0

    try:
        oi4h = float(r.get("oi_chg4h_pct") or 0.0)
    except (TypeError, ValueError):
        oi4h = 0.0

    try:
        cvd = float(r.get("cvd24") or 0.0)
    except (TypeError, ValueError):
        cvd = 0.0

    try:
        lls = float(r.get("lls24") or 100.0)
    except (TypeError, ValueError):
        lls = 100.0

    if (oi24 >= 10.0 and oi4h >= 10.0 and cvd >= 80.0 and lls < 30.0):
        tp_r1, tp_r2, tp_r3 = 1.45, 2.65, 4.40

    elif (oi24 >= 5.0 and oi4h >= 5.0 and cvd >= 70.0 and lls < 35.0):
        tp_r1, tp_r2, tp_r3 = 1.30, 2.40, 3.90

    else:
        tp_r1, tp_r2, tp_r3 = 1.20, 2.15, 3.40

    tp1 = sl_pct * tp_r1
    tp2 = sl_pct * tp_r2
    tp3 = sl_pct * tp_r3

    min_gap_12 = max(1.00, sl_pct * 0.40)
    min_gap_23 = max(1.50, sl_pct * 0.60)

    tp2 = max(
        tp2,
        tp1 + min_gap_12,
    )

    tp3 = max(
        tp3,
        tp2 + min_gap_23,
    )

    max_tp3 = 30.0

    if tp3 > max_tp3:
        scale = max_tp3 / tp3
        tp1 *= scale
        tp2 *= scale
        tp3 = max_tp3

    tp1 = round(tp1, 2)
    tp2 = round(tp2, 2)
    tp3 = round(tp3, 2)

    if not (0 < sl_pct < tp1 < tp2 < tp3):
        raise ValueError(
            f"invalid adaptive TP/SL sequence: "
            f"SL={sl_pct}, TP={tp1}/{tp2}/{tp3}"
        )

    tp_levels = [
        {
            "leg": "tp1",
            "pnl_pct": tp1,
            "close_fraction": 0.20,
        },
        {
            "leg": "tp2",
            "pnl_pct": tp2,
            "close_fraction": 0.25,
        },
        {
            "leg": "tp3",
            "pnl_pct": tp3,
            "close_fraction": 0.35,
        },
    ]

    return sl_pct, tp_levels


def get_trade_protection(ot: dict) -> tuple[float, list[dict], str]:
    """
    Return the persisted protection of an open trade.

    Canonical protection location:

        ot["protection"]

    Protection is mandatory for every newly-created trade,
    both research/paper and live BingX.

    Protection is calculated once when the trade is opened
    and is never recalculated for an already-open position.
    """
    protection = ot.get("protection")

    if not isinstance(protection, dict):
        raise ValueError("missing trade protection for open trade")

    try:
        sl_pct = float(protection["stop_loss_pct"])
        tp_levels = protection["tp_levels"]
    except (TypeError, ValueError, KeyError) as e:
        raise ValueError(f"invalid trade protection: {e}") from e

    if (
        sl_pct <= 0
        or not isinstance(tp_levels, list)
        or len(tp_levels) != 3
    ):
        raise ValueError("invalid trade protection structure")

    return (
        sl_pct,
        [dict(x) for x in tp_levels],
        str(protection.get("source", "adaptive_volatility")),
    )


def price_at(price_full, sym, ts_target, max_lag_sec=None):
    idx = price_full.get(sym, [])
    if not idx:
        return None

    times = [t for t, _ in idx]
    i = bisect_right(times, ts_target) - 1

    if i < 0:
        return None

    found_ts, found_price = idx[i]

    if max_lag_sec is not None and (ts_target - found_ts) > max_lag_sec:
        return None

    return found_price


def resolve_exit_reason(candidates):
    all_triggered = [r for r in EXIT_PRIORITY if candidates.get(r, False)]
    resolved = all_triggered[0] if all_triggered else None
    return resolved, all_triggered


def compute_snapshot_hash(snapshot):
    snapshot_json = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    return {
        "algorithm": "sha256",
        "version": "v1",
        "value": hashlib.sha256(snapshot_json.encode()).hexdigest(),
    }


def discovery_fingerprint():
    from urllib.parse import urlparse, parse_qs
    import base64

    url = COINALYZE_URL or ""
    out = {
        "url_sha256": hashlib.sha256(url.encode()).hexdigest()[:16],
        "url_len": len(url),
        "max_pages": MAX_PAGES,
        "url_configured": bool(url),
    }
    try:
        q = parse_qs(urlparse(url).query)
        for key in ("filter", "columns", "order_by", "order_dir"):
            v = (q.get(key) or [None])[0]
            if v is None:
                continue
            if key in ("filter", "columns"):
                try:
                    out[key] = base64.urlsafe_b64decode(v + "=" * (-len(v) % 4)).decode(
                        "utf-8", "replace"
                    )
                except Exception:
                    out[key] = f"<base64 не декодируется, len={len(v)}>"
            else:
                out[key] = v
    except Exception as e:
        out["parse_error"] = str(e)[:80]
    return out


def log_discovery_change(fp, ts):
    prev = load_jsonl(DISCOVERY_HISTORY_FILE)
    last = prev[-1] if prev else None
    if last and last.get("url_sha256") == fp["url_sha256"]:
        return False
    append_jsonl(
        DISCOVERY_HISTORY_FILE,
        {
            "ts": ts,
            **fp,
            "previous_sha256": (last or {}).get("url_sha256"),
            "lifecycle_engine_version": LIFECYCLE_ENGINE_VERSION,
            "signal_logic_version": SIGNAL_LOGIC_VERSION,
        },
    )
    if last:
        log.warning(f"DISCOVERY CHANGED {last.get('url_sha256')} → {fp['url_sha256']}")
        send_tg(
            f"⚙️ <b>Discovery-фильтр изменён</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"{esc(last.get('url_sha256'))} → {esc(fp['url_sha256'])}\n"
            f"<i>Вселенная сигналов другая. Статистику до и после смешивать нельзя.</i>\n"
            f"filter: <code>{esc(str(fp.get('filter'))[:200])}</code>"
        )
    else:
        log.info(f"DISCOVERY baseline {fp['url_sha256']}")
    return True


def _atomic_write_text(path: Path, text: str):
    d = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=str(d), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def append_jsonl(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _append_execution_event_durable(event):
    try:
        with open(EXECUTION_EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception as e:
        log.error(f"Durable execution journal write failed: {e}")
        return False


TA_DIRECTION_GATE_VERSION = 2
TA_DIRECTION_MATCH = "LONG"


def ta_direction_allows_long(ta_data: dict | None) -> tuple[bool, str]:
    if not isinstance(ta_data, dict):
        return False, "unavailable"

    market_context = ta_data.get("market_context") or {}
    if isinstance(market_context, dict):
        for tf in ("1h", "4h"):
            tf_ctx = market_context.get(tf) or {}
            if isinstance(tf_ctx, dict) and tf_ctx.get("divergence_at_resistance") is True:
                return False, f"divergence_at_resistance_{tf}"

    result = str(ta_data.get("result_label") or "").strip().upper()
    if result == TA_DIRECTION_MATCH:
        return True, "long"
    if result == "SHORT":
        return False, "short"
    if result == "MIXED":
        return False, "mixed"
    return False, "unknown"


def _ta_direction_snapshot(ta_data: dict | None) -> dict | None:
    if not isinstance(ta_data, dict):
        return None
    return {
        "result_label": ta_data.get("result_label"),
        "result_icon": ta_data.get("result_icon"),
        "net_score": ta_data.get("net_score"),
        "max_score": ta_data.get("max_score"),
        "long_evidence": ta_data.get("long_evidence"),
        "short_evidence": ta_data.get("short_evidence"),
        "timeframes": {k: dict(v) for k, v in (ta_data.get("timeframes") or {}).items() if isinstance(v, dict)},
        "market_context": {k: dict(v) for k, v in (ta_data.get("market_context") or {}).items() if isinstance(v, dict)},
    }


def _record_ta_direction_block(symbol, ts, idea_ts, state, path, price, score, ta_data, reason, name=None):
    key = f"{symbol}:{int(idea_ts or ts)}"
    try:
        if any(str(x.get("block_key")) == key for x in load_jsonl(TA_DIRECTION_BLOCKS_FILE)):
            return
    except Exception as e:
        log.warning(f"[{symbol}] TA block dedup read failed: {e}")
    append_jsonl(TA_DIRECTION_BLOCKS_FILE, {
        "block_key": key, "block_ts": int(ts), "idea_first_seen_ts": int(idea_ts or ts),
        "symbol": symbol, "name": name or symbol, "state": state, "path": path, "price": price,
        "score": score, "reason": reason, "gate_version": TA_DIRECTION_GATE_VERSION,
        "ta_direction": _ta_direction_snapshot(ta_data),
    })


def load_jsonl(path):
    if not path.exists():
        return []
    out = []
    bad = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    bad += 1
    if bad:
        log.warning(f"{path.name}: {bad} битых строк пропущено при загрузке")
    return out


def cleanup_jsonl(path, ttl_days):
    if not path.exists():
        return
    cutoff = now_ts() - ttl_days * 86400
    recs = load_jsonl(path)
    fresh = [r for r in recs if r.get("ts", 0) > cutoff]
    removed = len(recs) - len(fresh)
    if removed:
        content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in fresh)
        _atomic_write_text(path, content)
        log.info(f"Cleanup {path.name}: -{removed}")


def log_execution_event(symbol, kind, **fields):
    event = {"ts": now_ts(), "symbol": symbol, "kind": kind, **fields}
    try:
        return _append_execution_event_durable(event)
    except Exception as e:
        log.error(f"log_execution_event({symbol},{kind}) failed: {e}")
        return False


def _quarantine_corrupt_file(path):
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.corrupt.{now_ts()}")
    try:
        shutil.copy2(path, dest)
        return dest
    except Exception as e:
        log.error(f"backup {path.name}: {e}")
        return None


def load_watchlist():
    if not WATCHLIST_FILE.exists():
        return {}
    try:
        data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        backup = _quarantine_corrupt_file(WATCHLIST_FILE)
        msg = f"⚠️ <b>Monitor CRITICAL</b>\nwatchlist.json повреждён: {e}\nПрогон остановлен."
        log.error(msg.replace("<b>", "").replace("</b>", ""))
        send_tg(msg)
        sys.exit(1)
    for sym, rec in data.items():
        if rec.get("trade_id") and not rec.get("open_trade"):
            log.error(f"CORRUPTION: {sym} trade_id без open_trade — очищаю trade_id")
            rec.pop("trade_id", None)
        if rec.get("open_trade") and not rec.get("trade_id"):
            log.error(
                f"CORRUPTION: {sym} open_trade без trade_id — восстанавливаю из open_trade"
            )
            rec["trade_id"] = rec["open_trade"].get("trade_id_full") or _new_trade_id(
                sym, rec["open_trade"].get("entry_ts", now_ts())
            )
    return data


def save_watchlist(wl):
    _atomic_write_text(WATCHLIST_FILE, json.dumps(wl, ensure_ascii=False, indent=2))


def load_lifecycle_state():
    if not LIFECYCLE_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(LIFECYCLE_STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        backup = _quarantine_corrupt_file(LIFECYCLE_STATE_FILE)
        msg = (
            f"⚠️ <b>Monitor CRITICAL</b>\nlifecycle_state.json повреждён: {esc(str(e))}\n"
            f"Файл в карантине{f' ({esc(backup.name)})' if backup else ''}.\n"
            f"<i>Прогон остановлен: автоматический reset cooldown/idea_first_seen_ts "
            f"запрещён, чтобы не создать преждевременный повторный вход.</i>"
        )
        log.error(f"lifecycle_state.json повреждён: {e}")
        send_tg(msg)
        sys.exit(1)
    now = time.time()
    ttl = IDEA_REGISTRY_TTL_DAYS * 86400
    out = {}
    for sym, v in data.items():
        if not isinstance(v, dict):
            continue
        idea_fresh = (
            (now - v.get("idea_first_seen_ts", 0)) < ttl
            if v.get("idea_first_seen_ts")
            else False
        )
        cooldown_active = v.get("cooldown_until", 0) > now
        if idea_fresh or cooldown_active:
            out[sym] = v
    return out


def save_lifecycle_state(state):
    _atomic_write_text(
        LIFECYCLE_STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2)
    )


def load_market_history():
    cutoff = now_ts() - LIFECYCLE_WINDOW_MIN * 60
    recs = load_jsonl(MARKET_HISTORY_FILE)
    grouped = {}
    seen = set()
    for r in recs:
        if r.get("ts", 0) > cutoff:
            sym = r.get("symbol", "")
            key = (r.get("ts"), sym)
            if sym and key not in seen:
                seen.add(key)
                grouped.setdefault(sym, []).append(r)
    for sym in grouped:
        grouped[sym].sort(key=lambda x: x["ts"])
    return grouped


def load_price_full():
    recs = load_jsonl(MARKET_HISTORY_FILE)
    idx = {}
    for r in recs:
        sym, ts, p = r.get("symbol"), r.get("ts"), r.get("price")
        if sym and ts and valid_price(p):
            idx.setdefault(sym, []).append((ts, p))
    for sym in idx:
        idx[sym].sort(key=lambda x: x[0])
    return idx


def load_existing_trade_ids():
    ids = set()
    for r in load_jsonl(TRADES_FILE):
        tid = r.get("trade_id")
        if tid:
            ids.add(tid)
    return ids


def load_pending():
    if not PENDING_FILE.exists():
        return []
    raw = PENDING_FILE.read_text(encoding="utf-8")
    data = []
    bad = 0
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            data.append(json.loads(ln))
        except json.JSONDecodeError:
            bad += 1
    if bad and not data:
        backup = _quarantine_corrupt_file(PENDING_FILE)
        msg = f"⚠️ <b>Monitor CRITICAL</b>\npending_trades.jsonl полностью бит ({bad} строк)\nПрогон остановлен."
        log.error(msg.replace("<b>", "").replace("</b>", ""))
        send_tg(msg)
        sys.exit(1)
    if bad:
        log.warning(f"pending_trades.jsonl: {bad} битых строк пропущено")
        send_tg(
            f"⚠️ <b>Monitor WARNING</b>\npending_trades.jsonl: {bad} повреждённых строк пропущено.\n"
            f"<i>Соответствующие сделки не попадут в trades.jsonl как WIN/LOSS.</i>"
        )
    seen, out = set(), []
    for item in data:
        tid = item.get("rec", {}).get("trade_id")
        if tid and tid in seen:
            continue
        if tid:
            seen.add(tid)
        out.append(item)
    if len(out) != len(data):
        log.warning(f"pending dedup: {len(data)}→{len(out)}")
    return out


def save_pending(pending):
    content = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in pending)
    _atomic_write_text(PENDING_FILE, content)


def _ensure_stealth_available():
    if _STEALTH_AVAILABLE:
        return
    if ALLOW_NO_STEALTH:
        log.warning(
            "playwright-stealth недоступен, ALLOW_NO_STEALTH=true — продолжаем БЕЗ stealth."
        )
        return
    msg = (
        "⚠️ <b>Monitor CRITICAL</b>\nplaywright-stealth не установлен.\n"
        "Скрапинг без stealth рискует блокировкой Cloudflare без явного сигнала об этом.\n"
        "Установи playwright-stealth или явно разреши через ALLOW_NO_STEALTH=true."
    )
    log.error(
        "playwright-stealth не установлен — прогон остановлен (см. ALLOW_NO_STEALTH)."
    )
    send_tg(msg)
    sys.exit(1)


def _setup_browser_context(p):
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
    )
    if COINALYZE_P_SID or COINALYZE_CHAT_SID:
        cookies = []
        if COINALYZE_P_SID:
            cookies.append(
                {
                    "name": "p_sid",
                    "value": COINALYZE_P_SID,
                    "domain": "coinalyze.net",
                    "path": "/",
                    "secure": True,
                }
            )
        if COINALYZE_CHAT_SID:
            cookies.append(
                {
                    "name": "chat_sid",
                    "value": COINALYZE_CHAT_SID,
                    "domain": "coinalyze.net",
                    "path": "/",
                    "secure": True,
                }
            )
        cookies.append(
            {
                "name": "cookies_accepted",
                "value": "1",
                "domain": "coinalyze.net",
                "path": "/",
                "secure": True,
            }
        )
        ctx.add_cookies(cookies)
    page = ctx.new_page()
    stealth_sync(page)
    return browser, page


def _load_page(page, url):
    page.goto(url, wait_until="networkidle", timeout=60_000)
    page.wait_for_timeout(3000)
    if "Attention Required" in page.content():
        log.warning("Cloudflare, waiting...")
        page.wait_for_timeout(10_000)
        page.wait_for_load_state("networkidle", timeout=30_000)
    page.wait_for_selector("tbody tr", timeout=25_000)
    initial_count = len(page.query_selector_all("tbody tr"))
    log.info(f"Строк после загрузки: {initial_count}")
    prev_count = initial_count
    for scroll_attempt in range(15):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(700)
        cur_count = len(page.query_selector_all("tbody tr"))
        has_pagination = page.query_selector(".pagination") is not None
        if has_pagination:
            log.info(
                f"Пагинация найдена после скролла {scroll_attempt + 1}, строк: {cur_count}"
            )
            break
        if cur_count != prev_count:
            log.info(f"Скролл {scroll_attempt + 1}: строк {prev_count} → {cur_count}")
        if cur_count == prev_count and scroll_attempt >= 3:
            break
        prev_count = cur_count
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)
    final_count = len(page.query_selector_all("tbody tr"))
    has_pagination = page.query_selector(".pagination") is not None
    log.info(
        f"Итого: строк={final_count}, пагинация={'есть' if has_pagination else 'нет'}"
    )
    html_text = page.content()
    return html_text


def click_next_page(page, current_page_num):
    pag = page.query_selector(".pagination")
    if not pag:
        return False
    first_row = page.query_selector("tbody tr")
    before = first_row.get_attribute("data-coin") if first_row else None
    target = None
    for el in pag.query_selector_all("a, button, li"):
        if (el.inner_text() or "").strip() == str(current_page_num + 1):
            target = el
            break
    if target is None:
        target = pag.query_selector("[aria-label='Next'], .next, a[rel='next']")
    if target is None:
        return False
    target.click()
    page.wait_for_timeout(1500)
    for _ in range(10):
        first_row = page.query_selector("tbody tr")
        after = first_row.get_attribute("data-coin") if first_row else None
        if after and after != before:
            return True
        page.wait_for_timeout(500)
    return False


def get_page_urls(html_text):
    soup = BeautifulSoup(html_text, "lxml")
    pagination = soup.select_one(".pagination")
    if not pagination:
        return [COINALYZE_URL]
    urls = [COINALYZE_URL]
    for a in pagination.select("a[href]"):
        href = a.get("href", "")
        if not href:
            continue
        full_url = f"https://coinalyze.net{href}" if href.startswith("/") else href
        if full_url not in urls:
            urls.append(full_url)
    return urls[:MAX_PAGES]


def parse_table(html_text):
    soup = BeautifulSoup(html_text, "lxml")
    rows = soup.select("tbody tr")
    log.info(f"Строк: {len(rows)}")
    ts = now_ts()
    out = []
    range_violations = 0
    for tr in rows:
        symbol = tr.get("data-coin")
        tds = tr.find_all("td")
        if len(tds) < 23:
            continue
        spans = tds[1].find_all("span")
        name = spans[0].get_text(strip=True) if spans else (symbol or "?")
        rec = {
            "ts": ts,
            "symbol": symbol,
            "name": name,
            "price": parse_number(tds[2].get_text(strip=True)),
            "price_chg24": parse_number(tds[3].get_text(strip=True)),
            "mktcap": parse_number(tds[4].get_text(strip=True)),
            "volume24": parse_number(tds[5].get_text(strip=True)),
            "oi": parse_number(tds[6].get_text(strip=True)),
            "oi_chg24_pct": parse_number(tds[7].get_text(strip=True)),
            "oi_chg4h_pct": parse_number(tds[9].get_text(strip=True)),
            "oi_vol_ratio": parse_number(tds[11].get_text(strip=True)),
            "oi_mktcap_ratio": parse_number(tds[12].get_text(strip=True)),
            "fr_avg": parse_number(tds[13].get_text(strip=True)),
            "pfr_avg": parse_number(tds[14].get_text(strip=True)),
            "fr_oiw": parse_number(tds[15].get_text(strip=True)),
            "pfr_oiw": parse_number(tds[16].get_text(strip=True)),
            "liq_short24": parse_number(tds[17].get_text(strip=True)),
            "liq_long24": parse_number(tds[18].get_text(strip=True)),
            "ls_accounts": parse_number(tds[19].get_text(strip=True)),
            "btc_corr7d": parse_number(tds[20].get_text(strip=True)),
            "cvd24": parse_number(tds[21].get_text(strip=True)),
            "lls24": parse_number(tds[22].get_text(strip=True)),
        }
        cvd = rec.get("cvd24")
        lls = rec.get("lls24")
        if (
            (cvd is not None and not (0 <= cvd <= 100))
            or (lls is not None and not (0 <= lls <= 100))
            or (rec.get("price") is not None and rec["price"] <= 0)
        ):
            range_violations += 1
        out.append(rec)
    if range_violations:
        log.warning(f"parse_table: {range_violations} подозрительных строк")
    return out


def fetch_data() -> list[dict]:
    global LAST_SCRAPE_COMPLETE, LAST_SCRAPE_PAGE_ERRORS
    LAST_SCRAPE_COMPLETE = True
    LAST_SCRAPE_PAGE_ERRORS = []
    all_rows = []
    seen_symbols = set()
    with sync_playwright() as p:
        browser, page = _setup_browser_context(p)
        try:
            html_text = _load_page(page, COINALYZE_URL)
            DEBUG_HTML_FILE.write_text(html_text, encoding="utf-8")
            rows = parse_table(html_text)
            all_rows.extend(rows)
            for r in rows:
                seen_symbols.add(r.get("symbol"))
            log.info(f"Страница 1: {len(rows)} монет")
            page_urls = get_page_urls(html_text)
            log.info(f"Пагинация: найдено {len(page_urls)} страниц")
            if len(page_urls) > 1:
                for i, page_url in enumerate(page_urls[1:], start=2):
                    try:
                        html_text = _load_page(page, page_url)
                        rows = parse_table(html_text)
                        new_count = 0
                        for r in rows:
                            sym = r.get("symbol")
                            if sym and sym not in seen_symbols:
                                all_rows.append(r)
                                seen_symbols.add(sym)
                                new_count += 1
                        log.info(f"Страница {i}: +{new_count} новых монет")
                    except Exception as e:
                        log.error(f"Ошибка страницы {i} ({page_url}): {e}")
                        LAST_SCRAPE_COMPLETE = False
                        LAST_SCRAPE_PAGE_ERRORS.append(
                            {"page": i, "url": page_url, "error": str(e)[:200]}
                        )
                        continue
            else:
                soup = BeautifulSoup(html_text, "lxml")
                if soup.select_one(".pagination"):
                    log.info("href не найдены, пробуем клики по .pagination")
                    page_num = 1
                    while page_num < MAX_PAGES:
                        if not click_next_page(page, page_num):
                            break
                        page.wait_for_selector("tbody tr", timeout=15_000)
                        page.wait_for_timeout(500)
                        html_text = page.content()
                        rows = parse_table(html_text)
                        new_count = 0
                        for r in rows:
                            sym = r.get("symbol")
                            if sym and sym not in seen_symbols:
                                all_rows.append(r)
                                seen_symbols.add(sym)
                                new_count += 1
                        page_num += 1
                        log.info(
                            f"Страница {page_num}: +{new_count} новых монет (клик)"
                        )
        except Exception as e:
            log.error(f"Загрузка: {e}")
            try:
                page.screenshot(path=str(BASE / "debug_screenshot.png"), full_page=True)
            except Exception:
                pass
        finally:
            browser.close()
    if not all_rows:
        send_tg("⚠️ <b>Monitor</b>\nДанные не получены. Проверь debug_page.html")
        sys.exit(1)
    if not LAST_SCRAPE_COMPLETE:
        log.error(
            f"Scrape incomplete: {len(LAST_SCRAPE_PAGE_ERRORS)} page errors; "
            "disappearance/missed/remove logic will be skipped for this run"
        )
        send_tg(
            "⚠️ <b>Monitor</b>\n"
            "Coinalyze scrape неполный: часть страниц не загрузилась.\n"
            "<i>Этот прогон не будет трактовать отсутствующие монеты как исчезнувшие.</i>"
        )
    log.info(f"Всего монет после пагинации: {len(all_rows)}")
    return all_rows


def passes_filter(r):
    v = r.get("volume24")
    pc = r.get("price_chg24")
    oi = r.get("oi_chg24_pct")
    oi4 = r.get("oi_chg4h_pct")
    cvd = r.get("cvd24")
    lls = r.get("lls24")
    oiv = r.get("oi_vol_ratio")
    fr = r.get("fr_oiw")
    if v is None or v <= 500_000:
        return False
    if pc is None or pc < 0.5 or pc > 15.0:
        return False
    if oi is None or oi <= 2.0 or oi >= 50.0:
        return False
    if oi4 is None or oi4 <= 0:
        return False
    if cvd is None or cvd <= 50:
        return False
    if lls is None or lls >= 40:
        return False
    if oiv is None or oiv < 0.05 or oiv > 3.0:
        return False
    if fr is not None and fr > 0.08:
        return False
    return True


def calculate_score(r):
    score = 0
    pros, cons = [], []
    cvd = r.get("cvd24")
    if cvd is not None:
        if cvd > 70:
            score += 2
            pros.append(f"CVD={cvd:.0f}>70")
        elif cvd >= 55:
            score += 1
            pros.append(f"CVD={cvd:.0f}")
    lls = r.get("lls24")
    if lls is not None:
        if lls < 15:
            score += 2
            pros.append(f"LLS={lls:.0f}%<15")
        elif lls < 40:
            score += 1
            pros.append(f"LLS={lls:.0f}%")
        if lls > 50:
            score -= 2
            cons.append(f"LLS={lls:.0f}%>50")
    oi = r.get("oi_chg24_pct")
    if oi is not None:
        if 5 <= oi <= 35:
            score += 2
            pros.append(f"OI24={oi:.1f}%")
        elif oi > 50:
            score -= 2
            cons.append(f"OI24={oi:.1f}%>50")
    oi4 = r.get("oi_chg4h_pct")
    if oi4 is not None and oi4 > 0:
        score += 1
        pros.append(f"OI4h={oi4:.1f}%")
    pc = r.get("price_chg24")
    if pc is not None:
        if 2 <= pc <= 10:
            score += 1
            pros.append(f"P={pc:.1f}%")
        elif pc > 20:
            score -= 2
            cons.append(f"P={pc:.1f}%>20")
    fr = r.get("fr_oiw")
    if fr is not None:
        if -0.01 <= fr <= 0.03:
            score += 1
            pros.append(f"FR={fr:.4f}")
        elif fr > 0.06:
            score -= 2
            cons.append(f"FR={fr:.4f}>0.06")
    oim = r.get("oi_mktcap_ratio")
    if oim is not None:
        if oim >= 0.15:
            score += 2
            pros.append(f"OI/Mc={oim:.3f}>=0.15")
        elif oim >= 0.05:
            score += 1
            pros.append(f"OI/Mc={oim:.3f}")
    return score, pros, cons


def calc_derived(snaps):
    n = len(snaps)
    d = {
        "oi_accel": 0.0,
        "cvd_momentum": 0.0,
        "price_accel": 0.0,
        "funding_pressure": 0.0,
        "oi_trend": "flat",
        "cvd_trend": "flat",
        "price_trend": "flat",
        "oi4h_trend": "flat",
        "divergence": "none",
        "note": "",
    }
    if n < 2:
        return d

    def trend(vals):
        clean = [v for v in vals if v is not None]
        if len(clean) < 2:
            return "flat"
        diff = clean[-1] - clean[0]
        base = abs(clean[0]) if abs(clean[0]) > 1 else 10.0
        if diff > base * 0.05:
            return "up"
        if diff < -base * 0.05:
            return "down"
        return "flat"

    d["oi_trend"] = trend([s.get("oi_chg24_pct") for s in snaps])
    d["cvd_trend"] = trend([s.get("cvd24") for s in snaps])
    d["price_trend"] = trend([s.get("price_chg24") for s in snaps])
    d["oi4h_trend"] = trend([s.get("oi_chg4h_pct") for s in snaps])
    if n >= 3:
        oi = [safe(s.get("oi_chg24_pct")) for s in snaps[-3:]]
        d["oi_accel"] = (oi[2] - oi[1]) - (oi[1] - oi[0])
    if n >= 4:
        d["cvd_momentum"] = safe(snaps[-1].get("cvd24")) - safe(snaps[-4].get("cvd24"))
    elif n >= 2:
        d["cvd_momentum"] = safe(snaps[-1].get("cvd24")) - safe(snaps[0].get("cvd24"))
    if n >= 3:
        pc = [safe(s.get("price_chg24")) for s in snaps[-3:]]
        d["price_accel"] = (pc[2] - pc[1]) - (pc[1] - pc[0])
        fr = [safe(s.get("fr_oiw")) for s in snaps[-3:]]
        d["funding_pressure"] = (fr[2] - fr[1]) - (fr[1] - fr[0])
    if d["price_trend"] == "up" and d["oi_trend"] == "down":
        d["divergence"] = "price_up_oi_down"
        d["note"] = "Цена ↑ OI ↓ — рост на закрытии шортов"
    elif d["price_trend"] == "up" and d["cvd_trend"] == "down":
        d["divergence"] = "price_up_cvd_down"
        d["note"] = "Цена ↑ CVD ↓ — покупатели ослабевают"
    elif d["price_trend"] == "down" and d["oi_trend"] == "up":
        d["divergence"] = "price_down_oi_up"
        d["note"] = "Цена ↓ OI ↑ — возможное накопление"
    elif d["oi_trend"] == "down" and n >= 3:
        fr_vals = [safe(s.get("fr_oiw")) for s in snaps[-3:]]
        if fr_vals[-1] > fr_vals[0] + 0.005:
            d["divergence"] = "funding_up_oi_down"
            d["note"] = "Funding ↑ OI ↓ — выход участников"
    elif d["price_trend"] == "up" and n >= 3:
        lls_vals = [safe(s.get("lls24")) for s in snaps[-3:]]
        if lls_vals[-1] > lls_vals[0] + 10:
            d["divergence"] = "lls_up_price_up"
            d["note"] = "LLS ↑ Price ↑ — поздняя стадия"
    elif (
        d["oi_trend"] != "down"
        and d["cvd_trend"] != "down"
        and d["price_trend"] == "up"
    ):
        d["note"] = "Здоровое движение: Price↑ OI и CVD не падают"
    return d


def calc_momentum(derived):
    m = 0
    tags = []
    if derived["oi_accel"] > 0:
        m += 2
        tags.append("OI accel↑")
    elif derived["oi_accel"] < 0:
        m -= 1
        tags.append("OI accel↓")
    if derived["cvd_momentum"] > 5:
        m += 2
        tags.append("CVD mom↑")
    elif derived["cvd_momentum"] < -5:
        m -= 1
        tags.append("CVD mom↓")
    if derived["price_accel"] > 0:
        m += 1
        tags.append("Price accel↑")
    if derived["funding_pressure"] <= 0:
        m += 1
        tags.append("Funding stable")
    else:
        tags.append("Funding↑")
    if derived["oi4h_trend"] == "up":
        m += 1
        tags.append("OI4h↑")
    if derived["divergence"] == "none":
        m += 2
        tags.append("No divergence")
    else:
        m -= 2
        tags.append(f"Div: {derived['divergence']}")
    if derived["oi_trend"] == "up" and derived["cvd_trend"] == "up":
        m += 1
        tags.append("OI+CVD sync↑")
    return clamp(m, 0, 10), tags


def detect_pattern(r, derived, momentum):
    pc = safe(r.get("price_chg24"))
    oi24 = safe(r.get("oi_chg24_pct"))
    cvd = safe(r.get("cvd24"))
    lls = safe(r.get("lls24"))
    fr = safe(r.get("fr_oiw"))
    ls = r.get("ls_accounts")
    div = derived["divergence"]
    if (
        cvd > 90
        and (fr > 0.03 or derived["funding_pressure"] > 0.005)
        and derived["oi_accel"] < 0
        and pc > 15
    ):
        return "Exhaustion"
    if div == "funding_up_oi_down" or (pc > 5 and derived["oi_trend"] == "down"):
        return "Distribution"
    if oi24 < -10 and lls > 45 and derived["cvd_trend"] == "up":
        return "Capitulation"
    if pc > 10 and oi24 > 20 and derived["cvd_trend"] == "down":
        return "Late Trend"
    if pc > 0 and oi24 > 5 and lls > 35 and ls is not None and ls < 1.0:
        return "Short Squeeze"
    if (
        pc < 3
        and derived["cvd_trend"] == "up"
        and fr < 0.005
        and oi24 > 0
        and div == "none"
    ):
        return "Stealth Accumulation"
    if momentum >= 7 and derived["oi_accel"] > 2 and derived["cvd_momentum"] > 10:
        return "Momentum Expansion"
    if (
        pc > 0
        and oi24 > 5
        and cvd > 60
        and lls < 30
        and derived["oi_trend"] != "down"
        and div == "none"
    ):
        return "Healthy Trend"
    return "—"


def fetch_btc_price_chg():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
            timeout=8,
        )
        if r.status_code == 200:
            chg = r.json().get("bitcoin", {}).get("usd_24h_change")
            if chg is not None:
                return float(chg)
    except Exception as e:
        log.warning(f"CoinGecko: {e}")
    return None


def detect_market_phase(rows):
    btc = next((r for r in rows if r.get("symbol") in BTC_SYMBOLS), None)
    btc_src = "rows"
    if btc:
        btc_pc = btc.get("price_chg24") or 0
    else:
        btc_pc = fetch_btc_price_chg()
        btc_src = "coingecko"
    if btc_pc is None:
        return {
            "phase": "unknown",
            "note": "",
            "modifier": 0,
            "btc_chg24": None,
            "btc_source": "none",
        }
    up = sum(1 for r in rows if (r.get("price_chg24") or 0) > 0)
    ratio = up / max(len(rows), 1)
    if btc_pc > 2 and ratio > 0.6:
        ph, note, mod = "risk-on", "BTC↑ рынок широкий", +1
    elif btc_pc > 2 and ratio < 0.4:
        ph, note, mod = "btc-dominance", "BTC↑ альты нет", 0
    elif btc_pc < -2 and ratio < 0.3:
        ph, note, mod = "risk-off", "BTC↓ рынок слабый", -1
    elif btc_pc < -2 and ratio > 0.5:
        ph, note, mod = "rotation", "BTC↓ альты держатся", 0
    else:
        ph, note, mod = "neutral", "", 0
    return {
        "phase": ph,
        "note": note,
        "modifier": mod if MARKET_PHASE_MODIFIER_ENABLED else 0,
        "modifier_raw": mod,
        "btc_chg24": round(btc_pc, 3),
        "btc_source": btc_src,
        "breadth_ratio": round(ratio, 3),
    }


ACTIONS = {
    "NEUTRAL": "IGNORE",
    "ACCUMULATION": "WATCH",
    "EARLY_MOVE": "WATCH_LONG",
    "CONFIRMED_TREND": "POSSIBLE_ENTRY",
    "ACCELERATION": "LONG_SETUP",
    "EXHAUSTION": "NO_NEW_ENTRY",
    "DISTRIBUTION": "EXIT_AVOID",
    "INVALIDATED": "REMOVE",
}
STATE_EMOJI = {
    "NEUTRAL": "⚪",
    "ACCUMULATION": "🔍",
    "EARLY_MOVE": "🌱",
    "CONFIRMED_TREND": "🟢",
    "ACCELERATION": "🚀",
    "EXHAUSTION": "🟠",
    "DISTRIBUTION": "🔴",
    "INVALIDATED": "❌",
}
ALLOWED_FROM = {
    "ACCUMULATION": {"NEUTRAL", "ACCUMULATION"},
    "EARLY_MOVE": {"NEUTRAL", "ACCUMULATION", "EARLY_MOVE"},
    "CONFIRMED_TREND": {"NEUTRAL", "ACCUMULATION", "EARLY_MOVE", "CONFIRMED_TREND"},
    "ACCELERATION": {"CONFIRMED_TREND", "ACCELERATION"},
    "EXHAUSTION": {"CONFIRMED_TREND", "ACCELERATION", "EXHAUSTION"},
    "DISTRIBUTION": {"CONFIRMED_TREND", "ACCELERATION", "EXHAUSTION", "DISTRIBUTION"},
    "INVALIDATED": {
        "NEUTRAL",
        "ACCUMULATION",
        "EARLY_MOVE",
        "CONFIRMED_TREND",
        "ACCELERATION",
        "EXHAUSTION",
        "DISTRIBUTION",
        "INVALIDATED",
    },
}


def detect_lifecycle(symbol, snaps, score, derived, prev_state="NEUTRAL"):
    n = len(snaps)
    reasons, warnings = [], []
    if n < 2:
        return "NEUTRAL", ["недостаточно данных"], [], None
    last = snaps[-1]
    if last.get("cvd24") is None or last.get("oi_chg24_pct") is None:
        return "NEUTRAL", ["нет данных CVD/OI"], [], None
    pc = safe(last.get("price_chg24"))
    oi24 = safe(last.get("oi_chg24_pct"))
    oi4h = safe(last.get("oi_chg4h_pct"))
    cvd = safe(last.get("cvd24"))
    fr = safe(last.get("fr_oiw"))
    oi_accel = derived["oi_accel"]
    cvd_mom = derived["cvd_momentum"]
    price_accel = derived["price_accel"]
    fund_press = derived["funding_pressure"]

    def allowed(target):
        return prev_state in ALLOWED_FROM.get(target, set())

    if oi24 < -5 and cvd < 40:
        reasons.append(f"OI24={oi24:.1f}%<-5, CVD={cvd:.0f}<40")
        if pc < -3:
            reasons.append(f"Price={pc:.1f}% — структура сломана")
        return "INVALIDATED", reasons, warnings, None
    if allowed("DISTRIBUTION") and n >= 2:
        oi4h_prev = safe(snaps[-2].get("oi_chg4h_pct"))
        if oi4h < 0 and oi4h_prev < 0 and cvd_mom < -10:
            reasons.append(f"OI4h<0 2 снимка, CVD mom={cvd_mom:.0f}<-10")
            if pc > 5:
                reasons.append(f"Price={pc:.1f}% — ещё высокая")
            if fr > 0.02:
                reasons.append(f"FR={fr:.4f} — остаётся высоким")
            return "DISTRIBUTION", reasons, warnings, None
    if allowed("EXHAUSTION"):
        if cvd > 90 and (fr > 0.03 or fund_press > 0.005) and oi_accel < 0 and pc > 15:
            reasons.append(
                f"CVD={cvd:.0f}>90, FR={fr:.4f}, fund_press={fund_press:.4f}"
            )
            reasons.append(f"OI accel={oi_accel:.1f}<0 — замедление")
            warnings.append(f"Price={pc:.1f}% — вертикальный рост")
            return "EXHAUSTION", reasons, warnings, None
    if allowed("ACCELERATION"):
        if (
            n >= 3
            and oi_accel > 2
            and cvd_mom > 10
            and price_accel >= 1
            and derived["oi_trend"] != "down"
            and derived["cvd_trend"] != "down"
        ):
            reasons.append(f"OI accel={oi_accel:.1f}>2")
            reasons.append(f"CVD mom={cvd_mom:.0f}>10")
            reasons.append(f"Price accel={price_accel:.1f}>=1")
            if cvd > 85:
                warnings.append(f"CVD={cvd:.0f} — близко к максимуму")
            if pc > 10:
                warnings.append(f"Price={pc:.1f}% — уже вырос")
            return "ACCELERATION", reasons, warnings, None
    if allowed("CONFIRMED_TREND") and n >= MIN_SNAPS_LIFECYCLE:
        recent = snaps[-MIN_SNAPS_LIFECYCLE:]
        result_a = check_confirmed_path_a(recent)
        result_b = check_confirmed_path_b(recent, cvd_mom)
        trends_ok = derived["cvd_trend"] != "down" and derived["oi_trend"] != "down"
        if (result_a["passed"] or result_b["passed"]) and trends_ok:
            is_early = result_b["passed"] and not result_a["passed"]
            if is_early:
                reasons.append(
                    "Раннее подтверждение: OI>2 CVD>50 + ускорение + momentum"
                )
            else:
                reasons.append(
                    f"{MIN_SNAPS_LIFECYCLE} снимков: OI>5 CVD>55 LLS<40 OI4h>0 P↑ FR<0.05"
                )
            reasons.append("OI и CVD не снижаются (шагово и по тренду)")
            if oi_accel <= 0:
                warnings.append("OI не ускоряется")
            return (
                "CONFIRMED_TREND",
                reasons,
                warnings,
                ("early" if is_early else "classic"),
            )
    if allowed("EARLY_MOVE") and n >= 3:
        res_em = check_early_move(snaps)
        if res_em.get("passed"):
            reasons.append("3 снимка: Price↑ OI↑ CVD↑ Vol↑")
            return "EARLY_MOVE", reasons, warnings, None
    if allowed("ACCUMULATION") and n >= 3:
        res_acc = check_accumulation(snaps)
        if res_acc.get("passed"):
            last3 = snaps[-3:]
            cvd_vals = [s.get("cvd24") for s in last3 if s.get("cvd24") is not None]
            cvd_avg = sum(cvd_vals) / len(cvd_vals) if cvd_vals else 0.0
            reasons.append(f"OI4h>0 3 снимка, CVD avg={cvd_avg:.0f}>50, FR<0.03")
            reasons.append(f"Price={pc:.1f}%<5 — ещё не ушёл")
            return "ACCUMULATION", reasons, warnings, None
    return "NEUTRAL", ["нет подтверждённого движения"], warnings, None


def calc_confidence(state, snaps, score, derived, market_mod):
    base = {
        "NEUTRAL": 50,
        "ACCUMULATION": 40,
        "EARLY_MOVE": 55,
        "CONFIRMED_TREND": 70,
        "ACCELERATION": 80,
        "EXHAUSTION": 75,
        "DISTRIBUTION": 70,
        "INVALIDATED": 90,
    }.get(state, 50)
    snap_bonus = min(len(snaps), 10) * 2
    penalty = 0
    if derived["divergence"] != "none":
        penalty += 15
    if derived["oi_accel"] < 0 and state in ("ACCELERATION", "CONFIRMED_TREND"):
        penalty += 10
    if len(snaps) > 0 and safe(snaps[-1].get("cvd24")) > 95 and state == "ACCELERATION":
        penalty += 5
    return clamp(base + snap_bonus + score + market_mod * 3 - penalty, 0, 100)


def entry_earliness(r):
    pc_pos = min(max(safe(r.get("price_chg24")) / 15.0, 0.0), 1.0)
    oi_pos = min(max(safe(r.get("oi_chg24_pct")) / 50.0, 0.0), 1.0)
    fr_pos = min(max(safe(r.get("fr_oiw")) / 0.05, 0.0), 1.0)
    avg = (pc_pos + oi_pos + fr_pos) / 3
    return avg, ("ранняя" if avg < 0.35 else "средняя" if avg < 0.65 else "поздняя")


def send_tg(text):
    if not TG_BOT_TOKEN:
        log.warning("TG not configured: TG_BOT_TOKEN is empty")
        return False

    if not TG_CHAT_IDS:
        log.warning("TG not configured: TG_CHAT_IDS is empty")
        return False

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    chunks = []
    remaining = str(text)

    while len(remaining) > 3800:
        split_at = remaining.rfind("\n", 0, 3800)

        if split_at <= 0:
            split_at = 3800

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]

    chunks.append(remaining)

    all_ok = True

    for chat_id in TG_CHAT_IDS:
        for chunk_index, chunk in enumerate(chunks, start=1):
            delivered = False

            for attempt in range(3):
                try:
                    response = requests.post(
                        url,
                        data={
                            "chat_id": chat_id,
                            "text": chunk,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                        },
                        timeout=15,
                    )

                    if response.status_code == 200:
                        try:
                            payload = response.json()
                        except ValueError:
                            payload = {}

                        if payload.get("ok") is True:
                            delivered = True
                            break

                        log.warning(
                            "Telegram rejected message: "
                            f"chat_id={chat_id} "
                            f"chunk={chunk_index}/{len(chunks)} "
                            f"attempt={attempt + 1}/3 "
                            f"response={str(payload)[:300]}"
                        )
                    else:
                        log.warning(
                            "Telegram HTTP error: "
                            f"chat_id={chat_id} "
                            f"chunk={chunk_index}/{len(chunks)} "
                            f"attempt={attempt + 1}/3 "
                            f"status={response.status_code} "
                            f"body={response.text[:200]}"
                        )

                except requests.RequestException as exc:
                    log.warning(
                        "Telegram request error: "
                        f"chat_id={chat_id} "
                        f"chunk={chunk_index}/{len(chunks)} "
                        f"attempt={attempt + 1}/3: {exc}"
                    )

                except Exception as exc:
                    log.exception(
                        "Telegram unexpected error: "
                        f"chat_id={chat_id} "
                        f"chunk={chunk_index}/{len(chunks)} "
                        f"attempt={attempt + 1}/3: {exc}"
                    )

                if attempt < 2:
                    time.sleep(1.0 + attempt)

            if not delivered:
                try:
                    response = requests.post(
                        url,
                        data={
                            "chat_id": chat_id,
                            "text": chunk,
                            "disable_web_page_preview": True,
                        },
                        timeout=15,
                    )

                    if response.status_code == 200:
                        try:
                            payload = response.json()
                        except ValueError:
                            payload = {}

                        delivered = payload.get("ok") is True

                    if not delivered:
                        log.error(
                            "Telegram fallback failed: "
                            f"chat_id={chat_id} "
                            f"chunk={chunk_index}/{len(chunks)} "
                            f"status={response.status_code} "
                            f"body={response.text[:300]}"
                        )

                except requests.RequestException as exc:
                    log.error(
                        "Telegram fallback request error: "
                        f"chat_id={chat_id} "
                        f"chunk={chunk_index}/{len(chunks)}: {exc}"
                    )

                except Exception as exc:
                    log.exception(
                        "Telegram fallback unexpected error: "
                        f"chat_id={chat_id} "
                        f"chunk={chunk_index}/{len(chunks)}: {exc}"
                    )

            if not delivered:
                all_ok = False
                log.error(
                    "Telegram delivery failed: "
                    f"chat_id={chat_id} "
                    f"chunk={chunk_index}/{len(chunks)}"
                )
                break

    return all_ok


def should_notify_bingx_skip(existing: dict, reason: str, ts: int) -> bool:
    last_reason = existing.get("bingx_skip_notify_reason")
    last_ts = int(existing.get("bingx_skip_notify_ts") or 0)

    if reason != last_reason:
        return True

    return (ts - last_ts) >= BINGX_SKIP_NOTIFY_COOLDOWN_SEC


def mark_bingx_skip_notified(existing: dict, reason: str, ts: int):
    existing["bingx_skip_notify_reason"] = reason
    existing["bingx_skip_notify_ts"] = ts


def _retry_pending_tp_notifications():
    try:
        events = load_jsonl(EXECUTION_EVENTS_FILE)
    except Exception as e:
        log.error(f"TP Telegram recovery: не удалось прочитать journal: {e}")
        return False
    if not events:
        return False
    sent_ids = set()
    for event in events:
        if event.get("kind") != "tp_telegram_sent":
            continue
        event_id = event.get("event_id")
        if event_id:
            sent_ids.add(str(event_id))
    pending = []
    for event in events:
        kind = str(event.get("kind", ""))
        if not kind.startswith("tp_"):
            continue
        if kind == "tp_telegram_sent":
            continue
        event_id = event.get("event_id")
        telegram_text = event.get("telegram_text")
        if not event_id:
            continue
        if not telegram_text:
            log.warning(f"TP event {event_id} has no telegram_text")
            continue
        if str(event_id) in sent_ids:
            continue
        pending.append(event)
    if not pending:
        return False
    pending.sort(key=lambda e: int(e.get("fill_time_ms", 0) or e.get("ts", 0) or 0))
    changed = False
    for event in pending:
        event_id = str(event["event_id"])
        ok = send_tg(event["telegram_text"])
        if not ok:
            log.warning(
                f"[{event.get('symbol')}] TP Telegram recovery failed event_id={event_id}"
            )
            continue
        append_jsonl(
            EXECUTION_EVENTS_FILE,
            {
                "ts": now_ts(),
                "kind": "tp_telegram_sent",
                "event_id": event_id,
                "symbol": event.get("symbol"),
                "trade_id": event.get("trade_id"),
                "order_id": event.get("order_id"),
                "leg": event.get("leg"),
                "fill_time_ms": event.get("fill_time_ms"),
                "recovered": True,
            },
        )
        log.info(f"[{event.get('symbol')}] TP Telegram recovered event_id={event_id}")
        changed = True
    return changed


def format_signal(symbol, wl, cur, snaps, reasons, warnings, market, ta_context_data=None):
    state = wl["state"]
    emoji = STATE_EMOJI.get(state, "⚪")
    action = ACTIONS.get(state, "")
    conf = wl.get("confidence", 0)
    score = cur.get("score", 0)
    mom = cur.get("momentum", 0)
    pattern = cur.get("pattern", "—")
    derived = cur.get("derived", {})
    prev = wl.get("previous_state", "")
    s = snaps[-1] if snaps else {}
    ar = {"up": "↑", "down": "↓", "flat": "→"}
    oi_t = ar.get(derived.get("oi_trend"), "→")
    cvd_t = ar.get(derived.get("cvd_trend"), "→")
    prc_t = ar.get(derived.get("price_trend"), "→")
    _, early_label = entry_earliness(s)
    line = "━━━━━━━━━━━━━━━━━━"
    reas = " · ".join(reasons[:3]) if reasons else ""
    warns = " · ".join(warnings[:3]) if warnings else ""
    prev_line = f" ({esc(prev)} →)" if prev and prev != state else ""
    msg = (
        f"{emoji} <b>{esc(cur.get('name',symbol))} ({esc(symbol)})</b>\n{line}\n"
        f"{esc(state)}{prev_line} → {esc(action)}\n"
        f"Score {score}/10 · Momentum {mom}/10 · Conf {conf}%\n"
        f"Вход: {early_label} · Паттерн: {esc(pattern)}\n"
        f"OI {oi_t} CVD {cvd_t} Price {prc_t}"
    )
    if market.get("note"):
        msg += f" · {esc(market['note'])}"
    msg += (
        f"\n{line}\nP {fmt_pct(s.get('price_chg24'))} | OI {fmt_pct(s.get('oi_chg24_pct'))} | "
        f"4h {fmt_pct(s.get('oi_chg4h_pct'))} | CVD {fmt_num(s.get('cvd24'),dec=0)} | LLS {fmt_num(s.get('lls24'),'%',0)}\n"
    )
    if derived.get("note"):
        msg += f"<i>{esc(derived['note'])}</i>\n"
    if reas:
        msg += f"{line}\n✅ {esc(reas)}\n"
    if warns:
        msg += f"⚠️ {esc(warns)}\n"
    liq_share = cur.get("entry_short_liq_share24")
    liq_imb = cur.get("entry_liq_imbalance")
    fund_press = cur.get("entry_funding_oi_pressure")
    liq_int = cur.get("entry_liquidation_intensity")
    fr_z = cur.get("entry_fr_oiw_zscore")
    research_parts = [
        *([ f"FR·OI: {fund_press:.5f}"] if fund_press is not None else []),
        *([ f"LiqImb: {liq_imb:+.3f}"] if liq_imb is not None else []),
        *([ f"LiqInt: {liq_int:.5f}"] if liq_int is not None else []),
        *([ f"ShortLiq%: {liq_share:.2%}"] if liq_share is not None else []),
        *([ f"FR z-score: {fr_z:+.2f}"] if fr_z is not None else []),
    ]
    msg += f"{line}\n" f"📊 Research\n" + " · ".join(research_parts)
    if ta_context_data:
        ta_block = ta_context.format_ta_telegram(ta_context_data)
        if ta_block:
            msg += f"\n{ta_block}"    
        market_block = (ta_context.format_market_context_telegram(ta_context_data))
        if market_block:
            msg += f"\n{market_block}\n"
    return msg


def format_trade_close(rec):
    pnl = rec.get("strategy_pnl_pct")
    outcome_unknown = rec.get("outcome_unknown", False)
    pnl_s = "—" if pnl is None else f"{pnl:+.1f}%"
    emoji = (
        "💚"
        if (pnl is not None and pnl > 0)
        else "💔" if (pnl is not None and pnl < 0) else "➖"
    )
    peak = rec.get("max_pnl_pct")
    dd = rec.get("drawdown_from_peak_pct")
    line = "━━━━━━━━━━━━━━━━━━"
    stale_min = rec.get("exit_price_stale_min")
    stale_note = (
        f" (цена устарела на {fmt_num(stale_min,' мин',0)})" if stale_min else ""
    )
    msg = (
        f"{emoji} <b>{esc(rec.get('name',rec['symbol']))} ({esc(rec['symbol'])})</b> — сделка закрыта\n{line}\n"
        f"Вход {fmt_price(rec.get('entry_price'))} → Выход {fmt_price(rec.get('exit_price'))}{esc(stale_note)}   <b>{pnl_s}</b>\n"
        f"Держали {rec.get('hold_min')} мин · пик {fmt_pct(peak)} · просадка {fmt_pct(-dd if dd else None)}\n"
        f"Выход по: {esc(rec.get('exit_reason'))}\n"
        f"Вход был: {esc(rec.get('entry_path'))} · mom {rec.get('entry_momentum')} · "
        f"cvd_m {fmt_num(rec.get('entry_cvd_momentum'),dec=0)} · {esc(rec.get('entry_pattern'))} · "
        f"{esc(rec.get('entry_earliness_label'))}\n"
    )
    if outcome_unknown:
        msg += "⚠️ <i>Цена закрытия неизвестна — исход сделки помечен outcome_unknown, PnL не считается.</i>\n"
    r60 = rec.get("return_60m")
    if r60 is not None:
        msg += f"Signal@60m: {r60:+.1f}%\n"
    liq_share = rec.get("entry_short_liq_share24")
    liq_imb = rec.get("entry_liq_imbalance")
    fund_press = rec.get("entry_funding_oi_pressure")
    liq_int = rec.get("entry_liquidation_intensity")
    fr_z = rec.get("entry_fr_oiw_zscore")
    research_parts = [
        *([ f"FR·OI: {fund_press:.5f}"] if fund_press is not None else []),
        *([ f"LiqImb: {liq_imb:+.3f}"] if liq_imb is not None else []),
        *([ f"LiqInt: {liq_int:.5f}"] if liq_int is not None else []),
        *([ f"ShortLiq%: {liq_share:.2%}"] if liq_share is not None else []),
        *([ f"FR z-score: {fr_z:+.2f}"] if fr_z is not None else []),
    ]
    msg += f"{line}\n" f"📊 Research\n" + " · ".join(research_parts) + "\n"
    return msg


def llm_verify(symbol, wl, cur, snaps):
    if not ENABLE_LLM or not QWEN_API_KEY:
        return None
    state = wl["state"]
    conf = wl.get("confidence", 0)
    action = ACTIONS.get(state, "")
    derived = cur.get("derived", {})
    recent = snaps[-5:]
    snap_txt = "\n".join(
        f"  ts={s['ts']} P={s.get('price_chg24')} OI24={s.get('oi_chg24_pct')} OI4h={s.get('oi_chg4h_pct')} CVD={s.get('cvd24')} LLS={s.get('lls24')} FR={s.get('fr_oiw')}"
        for s in recent
    )
    user_msg = (
        f"Монета: {symbol}\nState: {state}\nConfidence: {conf}%\nAction: {action}\n"
        f"Momentum: {cur.get('momentum',0)}/10\nPattern: {cur.get('pattern','—')}\n"
        f"Derived: OI_accel={derived.get('oi_accel',0):.1f} CVD_mom={derived.get('cvd_momentum',0):.0f} "
        f"Price_accel={derived.get('price_accel',0):.1f} Funding_press={derived.get('funding_pressure',0):.4f}\n"
        f"Divergence: {derived.get('divergence','none')}\nСнимки:\n{snap_txt}\n"
        f'Верни JSON: {{"agree": true/false, "reason": "одно предложение", "risk": "low/medium/high"}}'
    )
    try:
        resp = requests.post(
            f"{QWEN_BASE_URL.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {QWEN_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": QWEN_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": "Верификатор крипто-сигналов. Только JSON.",
                    },
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.1,
                "max_tokens": 150,
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        if resp.status_code == 200:
            return json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception as e:
        log.warning(f"LLM: {e}")
    return None


def _new_trade_id(symbol, ts):
    return f"{symbol}_{ts}_{uuid.uuid4().hex[:8]}"


def _fill_horizons(ot, sym, as_of_ts, price_full):
    ep = ot.get("entry_price")
    if not ep:
        return
    max_lag = HORIZON_MAX_LAG_MIN * 60
    for h in TRADE_HORIZONS:
        key = f"return_{h}m"
        if ot.get(key) is None and as_of_ts >= ot["entry_ts"] + h * 60:
            ph = price_at(price_full, sym, ot["entry_ts"] + h * 60, max_lag_sec=max_lag)
            if ph:
                ot[key] = round((ph - ep) / ep * 100, 3)
                ot[f"{key}_available"] = True


def open_trade_record(
    r,
    ts,
    state,
    path,
    score,
    momentum,
    conf,
    early_val,
    early_label,
    pattern,
    derived,
    market,
    idea_first_seen_ts,
    snapshots,
    price,
    history_len=None,
    window=None,
    strength=None,
    shadow=None,
    hist=None,
    ta_direction=None,
    market_context=None,
):
    if state == "CONFIRMED_TREND":
        trigger = f"confirmed_trend_{path}_path"
    elif state == "ACCELERATION":
        trigger = "acceleration_confirmation"
    else:
        trigger = state.lower()
    entry_snapshot = {
        "timestamp": ts,
        "price": price,
        "symbol": r.get("symbol"),
        "ta_direction": dict(ta_direction) if isinstance(ta_direction, dict) else None,
        "market_context": dict(market_context) if isinstance(market_context, dict) else None,
        "features": {
            "price_chg24": r.get("price_chg24"),
            "oi_chg24": r.get("oi_chg24_pct"),
            "oi_chg4h": r.get("oi_chg4h_pct"),
            "funding": r.get("fr_avg"),
            "funding_oiw": r.get("fr_oiw"),
            "cvd24": r.get("cvd24"),
            "lls24": r.get("lls24"),
            "oi_vol_ratio": r.get("oi_vol_ratio"),
            "oi_mcap_ratio": r.get("oi_mktcap_ratio"),
            "ls_accounts": r.get("ls_accounts"),
            "volume24": r.get("volume24"),
            "oi_abs": r.get("oi"),
            "mktcap": r.get("mktcap"),
            "liq_short24": r.get("liq_short24"),
            "liq_long24": r.get("liq_long24"),
            "btc_corr7d": r.get("btc_corr7d"),
            "cvd_momentum": round(derived["cvd_momentum"], 2),
            "oi_accel": round(derived["oi_accel"], 3),
            "price_accel": round(derived["price_accel"], 3),
            "funding_pressure": round(derived["funding_pressure"], 5),
            "oi_trend": derived["oi_trend"],
            "cvd_trend": derived["cvd_trend"],
            "price_trend": derived["price_trend"],
            "oi4h_trend": derived["oi4h_trend"],
            "divergence": derived["divergence"],
        },
        "decision": {
            "state": state,
            "score": score,
            "momentum": momentum,
            "path": path,
            "pattern": pattern,
            "market_phase": market.get("phase", "unknown"),
            "entry_reason": {"state": state, "path": path, "trigger": trigger},
        },
        "discovery": dict(DISCOVERY),
    }
    research_oi_chg4h = r.get("oi_chg4h_pct")
    research_price_chg24 = r.get("price_chg24")
    research_liq_short24 = r.get("liq_short24")
    research_liq_long24 = r.get("liq_long24")
    research_oi_abs = r.get("oi")
    research_fr_oiw = r.get("fr_oiw")
    entry_short_liq_share24 = calc_entry_short_liq_share24(
        research_liq_short24, research_liq_long24
    )
    entry_liq_imbalance = calc_entry_liq_imbalance(
        research_liq_short24, research_liq_long24
    )
    entry_funding_oi_pressure = calc_entry_funding_oi_pressure(
        research_fr_oiw, research_oi_chg4h
    )
    entry_liquidation_intensity = calc_entry_liquidation_intensity(
        research_liq_short24, research_liq_long24, research_oi_abs
    )
    entry_fr_oiw_zscore = calc_entry_fr_oiw_zscore_from_hist(hist[:-1], research_fr_oiw)
    ot = {
        "entry_ts": ts,
        "entry_price": price,
        "entry_state": state,
        "entry_path": path,
        "last_price": price,
        "last_price_ts": ts,
        "max_pnl_pct": 0.0,
        "min_pnl_pct": 0.0,
        "peak_ts": ts,
        "max_state": state,
        "state_history": [
            {"ts": ts, "state": state, "score": score, "reason": "entry"}
        ],
        "current_state": state,
        "current_state_start_ts": ts,
        "time_in_states": {},
        "idea_first_seen_ts": idea_first_seen_ts,
        "idea_age_minutes": (
            round((ts - idea_first_seen_ts) / 60, 1) if idea_first_seen_ts else None
        ),
        "signal_age_min": (
            round((ts - idea_first_seen_ts) / 60, 1) if idea_first_seen_ts else None
        ),
        "snapshot_count_before_entry": snapshots,
        "watchlist_tenure_runs": snapshots,
        "history_len_before_entry": history_len,
        "entry_window": window,
        "entry_signal_strength": strength,
        "entry_shadow_predicates": (
            {k: shadow[k] for k, _ in SHADOW_VARIANTS} if shadow else None
        ),
        "asset_class": "unknown",
        "name": r.get("name", r.get("symbol", "")),
        "engine_versions": dict(ENGINE_VERSIONS),
        "entry_snapshot": entry_snapshot,
        "entry_snapshot_hash": compute_snapshot_hash(entry_snapshot),
        "entry_reason": {"state": state, "path": path, "trigger": trigger},
        "entry_score": score,
        "entry_momentum": momentum,
        "entry_cvd_momentum": round(derived["cvd_momentum"], 2),
        "entry_oi_accel": round(derived["oi_accel"], 3),
        "entry_price_accel": round(derived["price_accel"], 3),
        "entry_confidence": conf,
        "entry_earliness": round(early_val, 2),
        "entry_earliness_label": early_label,
        "entry_pattern": pattern,
        "entry_oi_trend": derived["oi_trend"],
        "entry_cvd_trend": derived["cvd_trend"],
        "entry_price_trend": derived["price_trend"],
        "entry_divergence": derived["divergence"],
        "entry_price_chg24": r.get("price_chg24"),
        "entry_oi_chg24": r.get("oi_chg24_pct"),
        "entry_oi_chg4h": r.get("oi_chg4h_pct"),
        "entry_cvd24": r.get("cvd24"),
        "entry_lls24": r.get("lls24"),
        "entry_fr_oiw": r.get("fr_oiw"),
        "entry_oi_vol_ratio": r.get("oi_vol_ratio"),
        "entry_oi_mktcap_ratio": r.get("oi_mktcap_ratio"),
        "entry_liq_short24": r.get("liq_short24"),
        "entry_liq_long24": r.get("liq_long24"),
        "entry_ls_accounts": r.get("ls_accounts"),
        "entry_btc_corr7d": r.get("btc_corr7d"),
        "entry_volume24": r.get("volume24"),
        "entry_oi_abs": r.get("oi"),
        "entry_mktcap": r.get("mktcap"),
        "entry_market_phase": market.get("phase", "unknown"),
        "entry_market_breadth": market.get("breadth_ratio"),
        "entry_btc_chg24": market.get("btc_chg24"),
        "entry_short_liq_share24": entry_short_liq_share24,
        "entry_liq_imbalance": entry_liq_imbalance,
        "entry_funding_oi_pressure": entry_funding_oi_pressure,
        "entry_liquidation_intensity": entry_liquidation_intensity,
        "entry_fr_oiw_zscore": entry_fr_oiw_zscore,
        "data_quality": {
            "entry_price_source": "live",
            "exit_price_source": None,
            "missing_snapshots": 0,
            "max_price_age_min": 0,
            "price_unknown": False,
            "exit_price_age_min": 0.0,
        },
        "shadow_stops": {str(lvl): None for lvl in SHADOW_STOP_LEVELS},
        "shadow_schmitt_on": True,
        "shadow_schmitt_exit_ts": None,
    }
    for h in TRADE_HORIZONS:
        ot[f"return_{h}m"] = None
        ot[f"return_{h}m_available"] = False
    return ot


def _attempt_exchange_close(ot, symbol):
    if not ENABLE_BINGX:
        return "not_applicable"
    bx = ot.get("bingx") or {}
    if bx.get("status") not in ("opened", "already_open"):
        return "not_applicable"
    # Before any exchange close, refresh the authoritative position size.
    # A failed global reconciliation must never cause us to trust stale local
    # qty_remaining and submit a close for an unknown/incorrect quantity.
    try:
        import bingx_client
        position_check = bingx_client.get_position(symbol)
    except Exception as e:
        log.error(f"[{symbol}] EXCHANGE_CLOSE position check exception: {e}")
        return "unconfirmed"

    position_status = position_check.get("status")
    if position_status == "error":
        err = str(position_check.get("error", "position check failed"))[:500]
        bx["qty_resync_required"] = True
        bx["qty_resync_error"] = err
        ot["bingx"] = bx
        log.error(
            f"[{symbol}] EXCHANGE_CLOSE blocked: authoritative position check failed: {err}"
        )
        return "unconfirmed"

    if position_status == "not_found":
        bx["qty_remaining"] = 0.0
        bx["qty_exchange_confirmed"] = True
        bx.pop("qty_resync_required", None)
        bx.pop("qty_resync_error", None)
        ot["bingx"] = bx
        return "confirmed"

    if position_status != "found":
        bx["qty_resync_required"] = True
        bx["qty_resync_error"] = f"unexpected position status={position_status}"
        ot["bingx"] = bx
        log.error(
            f"[{symbol}] EXCHANGE_CLOSE blocked: unexpected position status={position_status}"
        )
        return "unconfirmed"

    try:
        exchange_qty = float(position_check.get("positionAmt", 0) or 0)
    except (TypeError, ValueError):
        bx["qty_resync_required"] = True
        bx["qty_resync_error"] = f"invalid exchange positionAmt={position_check.get('positionAmt')!r}"
        ot["bingx"] = bx
        log.error(f"[{symbol}] EXCHANGE_CLOSE blocked: invalid exchange positionAmt")
        return "unconfirmed"

    if exchange_qty <= 0:
        bx["qty_remaining"] = 0.0
        bx["qty_exchange_confirmed"] = True
        bx.pop("qty_resync_required", None)
        bx.pop("qty_resync_error", None)
        ot["bingx"] = bx
        return "confirmed"

    local_qty = safe(
        bx.get("qty_remaining"),
        safe(bx.get("qty"), 0.0),
    )
    qty_to_close = exchange_qty
    if local_qty and abs(local_qty - exchange_qty) > 1e-12:
        log.warning(
            f"[{symbol}] EXCHANGE_CLOSE qty resync {local_qty} → {exchange_qty}"
        )
    bx["qty_remaining"] = exchange_qty
    bx["qty_exchange_confirmed"] = True
    bx.pop("qty_resync_required", None)
    bx.pop("qty_resync_error", None)
    ot["bingx"] = bx

    try:
        trade_id = ot.get("trade_id_full")
        try:
            close_attempt = max(0, int(ot.get("close_attempt", 0) or 0))
        except (TypeError, ValueError):
            close_attempt = 0
        res = bingx_client.close_long(
            symbol,
            qty_to_close,
            cancel_tp=True,
            trade_id=trade_id,
            close_attempt=close_attempt,
        )
    except Exception as e:
        log.error(f"[{symbol}] EXCHANGE_CLOSE exception: {e}")
        ot.setdefault("close_attempts", []).append(
            {
                "ts": now_ts(),
                "error": str(e)[:200],
            }
        )
        return "unconfirmed"
    ot["bingx_close"] = res
    status = res.get("status")

    if status == "closed":
        bx["qty_remaining"] = 0.0
        bx["execution_status"] = "CLOSED_MANUAL"
        ot["bingx"] = bx

        log.info(
            f"[{symbol}] BingX CLOSE ok "
            f"orderId={res.get('order_id')} "
            f"qty={qty_to_close}"
        )

        log_execution_event(
            symbol,
            "close_confirmed",
            qty=qty_to_close,
            order_id=res.get("order_id"),
            trade_id=ot.get("trade_id_full"),
        )
        return "confirmed"

    if status == "already_closed":
        bx["qty_remaining"] = 0.0
        bx["execution_status"] = "CLOSED_EXTERNAL"
        ot["bingx"] = bx

        log.info(
            f"[{symbol}] BingX CLOSE: LONG позиции уже нет "
            f"на бирже, SELL не отправлялся"
        )

        log_execution_event(
            symbol,
            "close_confirmed_external",
            qty=qty_to_close,
            order_id=None,
            trade_id=ot.get("trade_id_full"),
        )
        return "confirmed"

    if status == "close_retryable":
        try:
            next_attempt = int(res.get("next_close_attempt"))
            if next_attempt <= int(ot.get("close_attempt", 0) or 0):
                next_attempt = int(ot.get("close_attempt", 0) or 0) + 1
        except (TypeError, ValueError):
            next_attempt = int(ot.get("close_attempt", 0) or 0) + 1
        ot["close_attempt"] = next_attempt
        log.warning(
            f"[{symbol}] previous close order is terminal ({res.get('previous_order_status')}); "
            f"next close attempt will use attempt={next_attempt}"
        )
        return "unconfirmed"

    if (
        status == "close_pending"
        and res.get("recovery_check") == "position_still_open_after_filled_close"
        and float(res.get("remaining_qty", 0.0) or 0.0) > 0
    ):
        try:
            ot["close_attempt"] = max(0, int(ot.get("close_attempt", 0) or 0)) + 1
        except (TypeError, ValueError):
            ot["close_attempt"] = 1
        log.warning(
            f"[{symbol}] previous close order already FILLED but position remains; "
            f"next close attempt will use a new deterministic clientOrderId "
            f"attempt={ot['close_attempt']}"
        )

    log.error(
        f"[{symbol}] BingX CLOSE НЕ подтверждён: "
        f"status={status} error={res.get('error')}"
    )
    ot.setdefault("close_attempts", []).append(
        {
            "ts": now_ts(),
            "status": status,
            "error": str(res.get("error"))[:200],
        }
    )
    send_tg(
        f"🚨 <b>{esc(ot.get('name', symbol))} "
        f"({esc(symbol)})</b> — закрытие НЕ подтверждено биржей\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"status={esc(status)} "
        f"error={esc(str(res.get('error'))[:200])}\n"
        f"<i>Позиция считается открытой. "
        f"Повторная попытка на следующем прогоне.</i>"
    )
    return "unconfirmed"


def _exit_meta(source, exit_ts, last_price_ts):
    if source == "live" or last_price_ts is None:
        return source, 0.0
    return source, round(max(0, exit_ts - last_price_ts) / 60, 1)


def close_trade(
    ot,
    symbol,
    exit_ts,
    exit_price,
    exit_reason,
    exit_state,
    price_full,
    exit_price_source="live",
    exit_candidates=None,
    lifecycle_complete=True,
    exchange_close_status="not_applicable",
):
    ep = ot.get("entry_price")
    if exit_price is None or exit_price_source == "unknown":
        bx_close_avg = ot.get("bingx_close", {}).get("avg_price")
        if bx_close_avg and bx_close_avg > 0:
            exit_price = bx_close_avg
            exit_price_source = "exchange_fill"
        elif ot.get("last_price") and ot.get("last_price") > 0:
            exit_price = ot.get("last_price")
            exit_price_source = "last_seen_snapshot"

    _fill_horizons(ot, symbol, exit_ts, price_full)
    outcome_unknown = (exit_price is None) or (exit_price_source == "unknown")

    bx = ot.get("bingx") or {}
    tpf = bx.get("tp_fills") or {}
    tp_fill_meta = bx.get("tp_fill_meta") or {}
    slf = bx.get("sl_fills") or {}
    sl_fill_meta = bx.get("sl_fill_meta") or {}
    qty_initial = safe(bx.get("qty_initial"), safe(bx.get("qty"), 0.0))
    tp_orders = {str(o.get("order_id")): o for o in bx.get("tp_orders", [])}

    # These are immutable entry-time research values captured by
    # open_trade_record(). close_trade() must read them from the trade state
    # rather than relying on local variables that only exist in the entry
    # path. This also guarantees the same values survive restart/recovery.
    entry_short_liq_share24 = ot.get("entry_short_liq_share24")
    entry_liq_imbalance = ot.get("entry_liq_imbalance")
    entry_funding_oi_pressure = ot.get("entry_funding_oi_pressure")
    entry_liquidation_intensity = ot.get("entry_liquidation_intensity")
    entry_fr_oiw_zscore = ot.get("entry_fr_oiw_zscore")

    if (tpf or slf) and qty_initial and qty_initial > 0 and ep and ep > 0:
        realized_weighted_pnl = 0.0
        total_closed_qty = 0.0

        # Durable execution journal is the canonical source for realized
        # partial fills.  One order can receive multiple partial fills, so
        # summing cumulative executed_qty/avg_price from the final order
        # snapshot would double-count earlier fills.  Each durable event
        # stores the delta quantity and execution-time price separately.
        journal_events = load_jsonl(EXECUTION_EVENTS_FILE)
        fill_events = []
        trade_id = ot.get("trade_id_full")
        for event in journal_events:
            if event.get("symbol") != symbol:
                continue
            if not trade_id or event.get("trade_id") != trade_id:
                continue
            kind = str(event.get("kind", ""))
            if not (kind.startswith("tp_") and kind.endswith("_filled") or kind in {"sl_filled", "sl_filled_partial"}):
                continue
            try:
                q = float(event.get("executed_qty", 0) or 0)
                px = event.get("execution_avg_price", event.get("avg_price"))
                px = float(px) if px is not None else None
            except (TypeError, ValueError):
                continue
            if q <= 0 or px is None or px <= 0:
                continue
            fill_events.append((event, q, px))

        if fill_events:
            for event, filled_qty, fill_price in fill_events:
                leg_pnl = (fill_price - ep) / ep * 100.0
                realized_weighted_pnl += (filled_qty / qty_initial) * leg_pnl
                total_closed_qty += filled_qty
        else:
            # Backward-compatible fallback for records created before the
            # durable execution-price fields existed. For a clean-start
            # project this path should not be needed, but keeping it prevents
            # a malformed/missing journal from inventing a zero PnL.
            for oid, filled_qty in tpf.items():
                oid = str(oid)
                meta = tp_fill_meta.get(oid) or {}
                o = tp_orders.get(oid)
                try:
                    filled_qty = float(filled_qty or 0.0)
                except (TypeError, ValueError):
                    continue
                if filled_qty <= 0:
                    continue
                tp_price = meta.get("delta_avg_price") or meta.get("avg_price")
                if tp_price is None and o:
                    tp_price = o.get("price")
                try:
                    tp_price = float(tp_price) if tp_price is not None else None
                except (TypeError, ValueError):
                    tp_price = None
                if tp_price and tp_price > 0:
                    leg_pnl = (tp_price - ep) / ep * 100.0
                    realized_weighted_pnl += (filled_qty / qty_initial) * leg_pnl
                    total_closed_qty += filled_qty

            for oid, filled_qty in slf.items():
                oid = str(oid)
                meta = sl_fill_meta.get(oid) or {}
                try:
                    filled_qty = float(filled_qty or 0.0)
                except (TypeError, ValueError):
                    continue
                if filled_qty <= 0:
                    continue
                sl_price = meta.get("delta_avg_price") or meta.get("avg_price")
                try:
                    sl_price = float(sl_price) if sl_price is not None else None
                except (TypeError, ValueError):
                    sl_price = None
                if sl_price and sl_price > 0:
                    leg_pnl = (sl_price - ep) / ep * 100.0
                    realized_weighted_pnl += (filled_qty / qty_initial) * leg_pnl
                    total_closed_qty += filled_qty

        rem_fraction = max(0.0, 1.0 - (total_closed_qty / qty_initial))
        if exit_price and ep and not outcome_unknown:
            rem_pnl = (exit_price - ep) / ep * 100.0
            realized_weighted_pnl += rem_fraction * rem_pnl
            gross = round(realized_weighted_pnl, 3)
        elif total_closed_qty > 0:
            gross = round(realized_weighted_pnl, 3)
        else:
            gross = None
    else:
        gross = (
            round((exit_price - ep) / ep * 100, 3)
            if (exit_price and ep and not outcome_unknown)
            else None
        )

    max_pnl = ot.get("max_pnl_pct", 0.0)
    min_pnl = ot.get("min_pnl_pct", 0.0)
    peak_ts = ot.get("peak_ts", ot["entry_ts"])
    if gross is not None:
        if gross > max_pnl:
            max_pnl = gross
            peak_ts = exit_ts
        if gross < min_pnl:
            min_pnl = gross
    strategy_pnl = round(gross - FEE_PCT, 3) if gross is not None else None
    hold_min = round((exit_ts - ot["entry_ts"]) / 60, 1)
    drawdown = round(max_pnl - gross, 3) if gross is not None else None
    time_to_peak = round((peak_ts - ot["entry_ts"]) / 60, 1)
    _, stale_min = _exit_meta(exit_price_source, exit_ts, ot.get("last_price_ts"))
    if ot.get("data_quality"):
        ot["data_quality"]["exit_price_source"] = exit_price_source
        ot["data_quality"]["price_unknown"] = stale_min > 30 or outcome_unknown
    duration_min = round((exit_ts - ot.get("current_state_start_ts", exit_ts)) / 60, 1)
    last_state = ot.get("current_state", "UNKNOWN")
    time_in_states = dict(ot.get("time_in_states", {}))
    time_in_states[last_state] = time_in_states.get(last_state, 0) + duration_min
    protections_triggered = [
        r for r in (exit_candidates or []) if r in PROTECTION_REASONS
    ]

    def winflag(h):
        v = ot.get(f"return_{h}m")
        if v is None:
            return None
        return 1 if v >= TRADE_WIN_PCT else 0

    rec = {
        "schema_version": TRADE_SCHEMA_VERSION,
        "trade_id": (
            f"{symbol}_{ot['entry_ts']}"
            if not ot.get("trade_id_full")
            else ot["trade_id_full"]
        ),
        "symbol": symbol,
        "name": ot.get("name", symbol),
        "asset_class": ot.get("asset_class", "unknown"),
        "entry_ts": ot["entry_ts"],
        "entry_price": ep,
        "exit_ts": exit_ts,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "exit_state": exit_state,
        "exit_class": EXIT_CLASS.get(exit_reason, "UNKNOWN"),
        "exit_candidates": exit_candidates or [exit_reason],
        "exit_count": len(exit_candidates or [exit_reason]),
        "protections_triggered": protections_triggered,
        "close_type": "natural" if lifecycle_complete else "technical",
        "trade_lifecycle_complete": lifecycle_complete,
        "exit_price_source": exit_price_source,
        "exit_price_stale_min": stale_min,
        "outcome_unknown": outcome_unknown,
        "exchange_close_status": exchange_close_status,
        "closed_before_60m": hold_min < 60,
        "hold_min": hold_min,
        "entry_state": ot.get("entry_state"),
        "entry_path": ot.get("entry_path"),
        "entry_reason": ot.get("entry_reason", {}),
        "entry_snapshot": ot.get("entry_snapshot", {}),
        "entry_snapshot_hash": ot.get("entry_snapshot_hash", {}),
        "idea_first_seen_ts": ot.get("idea_first_seen_ts"),
        "idea_age_minutes": ot.get("idea_age_minutes"),
        "signal_age_min": ot.get("signal_age_min"),
        "snapshot_count_before_entry": ot.get("snapshot_count_before_entry"),
        "watchlist_tenure_runs": ot.get("watchlist_tenure_runs"),
        "history_len_before_entry": ot.get("history_len_before_entry"),
        "entry_window": ot.get("entry_window"),
        "entry_signal_strength": ot.get("entry_signal_strength"),
        "entry_shadow_predicates": ot.get("entry_shadow_predicates"),
        "last_signal_strength": ot.get("last_signal_strength"),
        "shadow_stops": ot.get("shadow_stops"),
        "shadow_schmitt_exit_ts": ot.get("shadow_schmitt_exit_ts"),
        "shadow_schmitt_exit_min": ot.get("shadow_schmitt_exit_min"),
        "entry_volume24": ot.get("entry_volume24"),
        "entry_oi_abs": ot.get("entry_oi_abs"),
        "entry_mktcap": ot.get("entry_mktcap"),
        "entry_market_breadth": ot.get("entry_market_breadth"),
        "entry_btc_chg24": ot.get("entry_btc_chg24"),
        "exit_price_age_min": ot.get("data_quality", {}).get("exit_price_age_min"),
        "stale_reason": ot.get("data_quality", {}).get("stale_reason"),
        "engine_versions": ot.get("engine_versions", dict(ENGINE_VERSIONS)),
        "discovery": ot.get("entry_snapshot", {}).get("discovery"),
        "discovery_sha256": (ot.get("entry_snapshot", {}).get("discovery") or {}).get(
            "url_sha256"
        ),
        "signal_logic_version": ot.get("engine_versions", {}).get(
            "signal", SIGNAL_LOGIC_VERSION
        ),
        "lifecycle_engine_version": ot.get("engine_versions", {}).get(
            "lifecycle", LIFECYCLE_ENGINE_VERSION
        ),
        "max_state": ot.get("max_state"),
        "state_history": ot.get("state_history", []),
        "time_in_states": time_in_states,
        "data_quality": ot.get("data_quality", {}),
        "entry_score": ot.get("entry_score"),
        "entry_momentum": ot.get("entry_momentum"),
        "entry_cvd_momentum": ot.get("entry_cvd_momentum"),
        "entry_oi_accel": ot.get("entry_oi_accel"),
        "entry_price_accel": ot.get("entry_price_accel"),
        "entry_confidence": ot.get("entry_confidence"),
        "entry_earliness": ot.get("entry_earliness"),
        "entry_earliness_label": ot.get("entry_earliness_label"),
        "entry_pattern": ot.get("entry_pattern"),
        "entry_oi_trend": ot.get("entry_oi_trend"),
        "entry_cvd_trend": ot.get("entry_cvd_trend"),
        "entry_price_trend": ot.get("entry_price_trend"),
        "entry_divergence": ot.get("entry_divergence"),
        "entry_price_chg24": ot.get("entry_price_chg24"),
        "entry_oi_chg24": ot.get("entry_oi_chg24"),
        "entry_oi_chg4h": ot.get("entry_oi_chg4h"),
        "entry_cvd24": ot.get("entry_cvd24"),
        "entry_lls24": ot.get("entry_lls24"),
        "entry_fr_oiw": ot.get("entry_fr_oiw"),
        "entry_oi_vol_ratio": ot.get("entry_oi_vol_ratio"),
        "entry_oi_mktcap_ratio": ot.get("entry_oi_mktcap_ratio"),
        "entry_liq_short24": ot.get("entry_liq_short24"),
        "entry_liq_long24": ot.get("entry_liq_long24"),
        "entry_ls_accounts": ot.get("entry_ls_accounts"),
        "entry_btc_corr7d": ot.get("entry_btc_corr7d"),
        "entry_market_phase": ot.get("entry_market_phase"), 
        "entry_short_liq_share24": entry_short_liq_share24,
        "entry_liq_imbalance": entry_liq_imbalance,
        "entry_funding_oi_pressure": entry_funding_oi_pressure,
        "entry_liquidation_intensity": entry_liquidation_intensity,
        "entry_fr_oiw_zscore": entry_fr_oiw_zscore,
        "fee_pct": FEE_PCT,
        "gross_pnl_pct": gross,
        "strategy_pnl_pct": strategy_pnl,
        "max_pnl_pct": max_pnl,
        "min_pnl_pct": min_pnl,
        "mfe_pct": max_pnl,
        "mae_pct": min_pnl,
        "drawdown_from_peak_pct": drawdown,
        "time_to_peak_min": time_to_peak,
        "bingx_final_state": ot.get("bingx"),
    }
    for h in TRADE_HORIZONS:
        rec[f"return_{h}m"] = ot.get(f"return_{h}m")
        rec[f"return_{h}m_available"] = bool(ot.get(f"return_{h}m_available"))
    rec["win_60m"] = winflag(60)
    rec["win_120m"] = winflag(120)
    PENDING.append(
        {"rec": rec, "entry_ts": ot["entry_ts"], "symbol": symbol, "entry_price": ep}
    )
    log.info(
        f"[{symbol}] TRADE → PENDING {exit_reason} strat={strategy_pnl} hold={hold_min}m outcome_unknown={outcome_unknown}"
    )
    send_tg(format_trade_close(rec))


def _process_filled_tps(wl_all, ts, price_full, exch):
    try:
        import bingx_client
    except Exception as e:
        log.error(f"TP reconcile: bingx_client недоступен: {e}")
        return False
    changed = False
    for symbol, entry in wl_all.items():
        ot = entry.get("open_trade")
        if not ot:
            continue
        bx = ot.get("bingx") or {}
        if bx.get("status") not in ("opened", "already_open"):
            continue
        trade_id = ot.get("trade_id_full")
        trade_id = str(trade_id) if trade_id else None
        opened_ts = ot.get("opened_ts", ot.get("entry_ts", 0))
        processed_fills = bx.setdefault("tp_fills", {})
        filled_legs_state = {
            str(x).lower()
            for x in (bx.get("tp_filled_legs") or [])
            if x
        }
        qty_initial = safe(bx.get("qty_initial"), safe(bx.get("qty"), 0.0))
        qty_remaining = safe(bx.get("qty_remaining"), qty_initial)
        result = bingx_client.compute_new_tp_fills(
            symbol,
            trade_id,
            opened_ts,
            processed_fills,
            processed_fill_meta=bx.get("tp_fill_meta") or {},
        )
        if result.get("status") != "ok":
            continue
        for filled in result.get("fills", []):
            order_id = filled["order_id"]
            leg = filled["leg"]
            executed_qty = filled["executed_qty_total"]
            delta_qty = filled["executed_qty_delta"]
            avg_price = filled["avg_price"]
            delta_avg_price = filled.get("delta_avg_price") or avg_price
            fill_time_ms = filled["fill_time_ms"]
            status = filled["status"]
            new_qty_remaining = max(0.0, qty_remaining - delta_qty)
            exchange_after_fill = None
            position_state_unknown = False
            try:
                exchange_after_fill = bingx_client.get_position(symbol)
            except Exception as e:
                position_state_unknown = True
                log.warning(
                    f"[{symbol}] TP {leg}: post-fill position check failed: {e}"
                )

            if exchange_after_fill and exchange_after_fill.get("status") == "found":
                try:
                    new_qty_remaining = float(
                        exchange_after_fill.get("positionAmt", new_qty_remaining)
                    )
                except (TypeError, ValueError):
                    position_state_unknown = True
            elif exchange_after_fill and exchange_after_fill.get("status") == "not_found":
                new_qty_remaining = 0.0
            elif exchange_after_fill and exchange_after_fill.get("status") == "error":
                position_state_unknown = True

            # A TP fill is independently confirmed by allOrders, but after a
            # fill we must not invent the remaining position size when the
            # authoritative position query failed. Commit the fill journal
            # and metadata, preserve the last known qty, and force protection
            # reconciliation to resync the real exchange quantity on the next
            # pass.
            if position_state_unknown:
                new_qty_remaining = qty_remaining
            if fill_time_ms > 0:
                fill_ts = fill_time_ms / 1000.0
                fill_time_text = time.strftime(
                    "%Y-%m-%d %H:%M:%S UTC", time.gmtime(fill_ts)
                )
            else:
                fill_ts = None
                fill_time_text = "unknown"
            entry_price = ot.get("entry_price")
            pnl_pct = (
                (delta_avg_price - entry_price) / entry_price * 100
                if delta_avg_price and entry_price
                else None
            )
            remaining_pct = (
                new_qty_remaining / qty_initial * 100 if qty_initial else None
            )
            pnl_text = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "—"
            price_text = fmt_price(avg_price) if avg_price is not None else "—"
            remaining_text = f"{new_qty_remaining:.8f}"
            remaining_pct_text = (
                f"{remaining_pct:.1f}%" if remaining_pct is not None else "—"
            )
            telegram_text = (
                f"💰 <b>{esc(ot.get('name', symbol))} "
                f"({esc(symbol)})</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Leg: {esc(leg)}\n"
                f"PnL TP: {pnl_text}\n"
                f"Цена исполнения: {price_text}\n"
                f"Закрыто: {delta_qty:.8f}\n"
                f"Осталось: {remaining_text} "
                f"({remaining_pct_text})\n"
            )
            event_id = f"tp:{trade_id or 'unknown'}:{order_id}:{executed_qty:.12f}"
            existing_events = load_jsonl(EXECUTION_EVENTS_FILE)
            event_already_exists = any(
                e.get("event_id") == event_id
                for e in existing_events
                if e.get("symbol") == symbol
            )
            event_created = False
            if event_already_exists:
                log.info(
                    f"[{symbol}] TP {leg} event_id={event_id} already in journal "
                    f"(crash recovery) — restoring state without duplicate write"
                )
                durable_ok = True
            else:
                durable_ok = log_execution_event(
                    symbol,
                    f"tp_{leg}_filled",
                    event_id=event_id,
                    trade_id=trade_id,
                    order_id=order_id,
                    client_order_id=filled.get("client_order_id"),
                    leg=leg,
                    status=status,
                    executed_qty=delta_qty,
                    executed_qty_total=executed_qty,
                    qty_before=qty_remaining,
                    qty_remaining=new_qty_remaining,
                    avg_price=delta_avg_price,
                    cumulative_avg_price=avg_price,
                    execution_avg_price=delta_avg_price,
                    pnl_pct=(round(pnl_pct, 3) if pnl_pct is not None else None),
                    fill_ts=fill_ts,
                    fill_time_ms=fill_time_ms,
                    source="bingx_order_history",
                    telegram_text=telegram_text,
                    telegram_status="pending",
                )
                event_created = durable_ok
            if not durable_ok:
                log.error(
                    f"[{symbol}] CRITICAL: TP {leg} журнал write FAILED — "
                    f"fill NOT committed, will retry next run"
                )
                continue
            processed_fills[order_id] = executed_qty
            if position_state_unknown:
                bx["execution_status"] = "QTY_UNKNOWN_AFTER_TP_FILL"
                bx["qty_resync_required"] = True
            fill_meta = bx.setdefault("tp_fill_meta", {})
            fill_meta[str(order_id)] = {
                "leg": str(leg).lower(),
                "avg_price": avg_price,
                "delta_avg_price": delta_avg_price,
                "executed_qty_total": executed_qty,
                "fill_time_ms": fill_time_ms,
                "client_order_id": filled.get("client_order_id"),
            }
            if filled.get("is_fully_filled"):
                filled_legs_state.add(str(leg).lower())
            bx["tp_filled_legs"] = sorted(filled_legs_state)
            qty_remaining = new_qty_remaining
            bx["qty_remaining"] = qty_remaining
            bx["execution_status"] = "PARTIAL_TP"
            if event_created:
                telegram_ok = send_tg(telegram_text)
                if telegram_ok:
                    append_jsonl(
                        EXECUTION_EVENTS_FILE,
                        {
                            "ts": now_ts(),
                            "kind": "tp_telegram_sent",
                            "event_id": event_id,
                            "symbol": symbol,
                            "trade_id": trade_id,
                            "order_id": order_id,
                            "leg": leg,
                            "fill_time_ms": fill_time_ms,
                        },
                    )
                else:
                    log.warning(f"[{symbol}] TP telegram pending event_id={event_id}")
            log.info(
                f"[{symbol}] TP {leg} FILLED "
                f"delta={delta_qty:.8f} total={executed_qty:.8f} "
                f"remaining={qty_remaining:.8f} time={fill_time_text}"
            )
            changed = True

            if qty_remaining > 0 and entry_price and entry_price > 0:
                try:
                    _, tp_lvls, _ = get_trade_protection(ot)
                except Exception:
                    tp_lvls = []

                if len(tp_lvls) != 3:
                    log.error(
                        f"[{symbol}] TP trailing skipped: invalid protection "
                        f"tp_levels={tp_lvls}"
                    )
                    continue

                tp_dict = {
                    lvl.get("leg"): float(lvl.get("pnl_pct"))
                    for lvl in tp_lvls
                    if (
                        isinstance(lvl, dict)
                        and lvl.get("leg")
                        and lvl.get("pnl_pct") is not None
                    )
                }

                all_filled_legs = set(filled_legs_state)
                if filled.get("is_fully_filled"):
                    all_filled_legs.add(str(leg).lower())
                for fid in processed_fills:
                    for o in bx.get("tp_orders", []):
                        if str(o.get("order_id")) == str(fid) and o.get("leg"):
                            all_filled_legs.add(str(o.get("leg")).lower())

                target_sl_price = None
                sl_label = ""
                if "tp3" in all_filled_legs:
                    target_pct = tp_dict.get("tp2", 6.0)
                    target_sl_price = entry_price * (1.0 + target_pct / 100.0)
                    sl_label = f"Locked TP2 (+{target_pct:.1f}%)"
                elif "tp2" in all_filled_legs:
                    target_pct = tp_dict.get("tp1", 3.0)
                    target_sl_price = entry_price * (1.0 + target_pct / 100.0)
                    sl_label = f"Locked TP1 (+{target_pct:.1f}%)"
                elif "tp1" in all_filled_legs:
                    target_sl_price = entry_price * 1.001
                    sl_label = "Breakeven (+0.1%)"

                cur_sl_order = bx.get("sl_order") or {}
                cur_sl_price = float(cur_sl_order.get("stop_price") or 0.0)

                if target_sl_price and target_sl_price > cur_sl_price:
                    trail_res = bingx_client.update_stop_loss_order(
                        symbol, target_sl_price, qty_remaining, trade_id=trade_id
                    )
                    if trail_res.get("status") == "created":
                        bx["sl_order"] = trail_res
                        if isinstance(bx.get("protection"), dict):
                            bx["protection"]["current_stop_price"] = target_sl_price
                        log.info(
                            f"[{symbol}] Trailing Stop обновлен: {target_sl_price:.6f} ({sl_label})"
                        ) 
        bx["tp_fills"] = processed_fills
        bx["tp_fill_meta"] = bx.get("tp_fill_meta", {})
        bx["tp_filled_legs"] = sorted(filled_legs_state)
        ot["bingx"] = bx
        entry["open_trade"] = ot
    return changed



def _process_filled_sls(wl_all, ts):
    """Persist cumulative/partial SL fills without treating partial fill as full exit."""
    try:
        import bingx_client
    except Exception as e:
        log.error(f"SL reconcile: bingx_client недоступен: {e}")
        return False

    changed = False
    for symbol, entry in wl_all.items():
        ot = entry.get("open_trade")
        if not ot:
            continue
        bx = ot.get("bingx") or {}
        if bx.get("status") not in ("opened", "already_open"):
            continue
        trade_id = str(ot.get("trade_id_full")) if ot.get("trade_id_full") else None
        opened_ts = ot.get("opened_ts", ot.get("entry_ts", 0))
        processed = bx.setdefault("sl_fills", {})
        meta_store = bx.setdefault("sl_fill_meta", {})

        result = bingx_client.get_filled_sl_orders(
            symbol, opened_ts=opened_ts, trade_id=trade_id
        )
        if result.get("status") != "ok":
            continue

        for filled in result.get("orders", []):
            order_id = str(filled.get("order_id") or "")
            if not order_id:
                continue
            executed_total = float(filled.get("executed_qty", 0.0) or 0.0)
            previous_total = float(processed.get(order_id, 0.0) or 0.0)
            delta_qty = executed_total - previous_total
            if delta_qty <= 1e-12:
                continue

            avg_price = float(filled.get("avg_price", 0.0) or 0.0)
            previous_meta = meta_store.get(order_id) or {}
            previous_avg = float(previous_meta.get("avg_price", 0.0) or 0.0)
            previous_qty = float(previous_meta.get("executed_qty_total", previous_total) or previous_total)
            delta_avg_price = avg_price
            if (
                avg_price > 0
                and previous_qty > 0
                and previous_avg > 0
                and executed_total > previous_qty
            ):
                delta_avg_price = (
                    avg_price * executed_total - previous_avg * previous_qty
                ) / delta_qty
                if delta_avg_price <= 0:
                    delta_avg_price = avg_price

            # A confirmed SL fill changes the real position quantity. Do not
            # infer the remaining size from local state: ask the exchange.
            # On an unknown/error response, preserve the last known quantity
            # and force reconciliation on the next pass.
            position_state_unknown = False
            try:
                exchange_after_fill = bingx_client.get_position(symbol)
            except Exception as e:
                exchange_after_fill = {"status": "error", "error": str(e)}

            if exchange_after_fill.get("status") == "found":
                try:
                    exchange_qty = float(exchange_after_fill.get("positionAmt", 0) or 0)
                    if exchange_qty < 0:
                        position_state_unknown = True
                    else:
                        bx["qty_remaining"] = exchange_qty
                        bx["qty_exchange_confirmed"] = True
                        bx.pop("qty_resync_required", None)
                        bx.pop("qty_resync_error", None)
                        if exchange_after_fill.get("avgPrice") is not None:
                            bx["bingx_avg_price"] = exchange_after_fill.get("avgPrice")
                except (TypeError, ValueError):
                    position_state_unknown = True
            elif exchange_after_fill.get("status") == "not_found":
                bx["qty_remaining"] = 0.0
                bx["qty_exchange_confirmed"] = True
                bx.pop("qty_resync_required", None)
                bx.pop("qty_resync_error", None)
            else:
                position_state_unknown = True
                bx["qty_resync_required"] = True
                bx["qty_resync_error"] = str(
                    exchange_after_fill.get("error", "position state unknown")
                )[:300]

            fill_time_ms = int(filled.get("time", 0) or 0)
            event_id = f"sl:{trade_id or 'unknown'}:{order_id}:{executed_total:.12f}"
            existing_events = load_jsonl(EXECUTION_EVENTS_FILE)
            already = any(
                e.get("event_id") == event_id and e.get("symbol") == symbol
                for e in existing_events
            )
            if not already:
                ok = log_execution_event(
                    symbol,
                    "sl_filled_partial" if not filled.get("is_fully_filled") else "sl_filled",
                    event_id=event_id,
                    trade_id=trade_id,
                    order_id=order_id,
                    client_order_id=filled.get("client_order_id"),
                    status=filled.get("status"),
                    executed_qty=delta_qty,
                    executed_qty_total=executed_total,
                    avg_price=delta_avg_price,
                    cumulative_avg_price=avg_price,
                    execution_avg_price=delta_avg_price,
                    fill_ts=(fill_time_ms / 1000.0 if fill_time_ms else None),
                    fill_time_ms=fill_time_ms,
                    source="bingx_order_history",
                )
                if not ok:
                    log.error(
                        f"[{symbol}] CRITICAL: SL fill journal write FAILED "
                        f"order_id={order_id}; will retry next run"
                    )
                    continue

            processed[order_id] = executed_total
            meta_store[order_id] = {
                "avg_price": avg_price,
                "delta_avg_price": delta_avg_price,
                "executed_qty_total": executed_total,
                "fill_time_ms": fill_time_ms,
                "client_order_id": filled.get("client_order_id"),
                "is_fully_filled": bool(filled.get("is_fully_filled")),
            }
            bx["sl_fills"] = processed
            bx["sl_fill_meta"] = meta_store
            if filled.get("is_fully_filled"):
                bx["sl_filled_order_ids"] = sorted(set(bx.get("sl_filled_order_ids", [])) | {order_id})
                bx["execution_status"] = "FULL_SL_FILL_DETECTED"
            else:
                bx["execution_status"] = "PARTIAL_SL"
            if position_state_unknown:
                bx["execution_status"] = "QTY_UNKNOWN_AFTER_SL_FILL"
            changed = True

        bx["sl_fills"] = processed
        bx["sl_fill_meta"] = meta_store
        ot["bingx"] = bx
        entry["open_trade"] = ot

    return changed


def _detect_sl_exit(ot, sym):
    try:
        import bingx_client
    except Exception as e:
        log.error(f"[{sym}] _detect_sl_exit: bingx_client недоступен: {e}")
        return None
    trade_id = ot.get("trade_id_full")
    opened_ts = ot.get("opened_ts", ot.get("entry_ts", 0))
    res = bingx_client.get_last_filled_sl(sym, opened_ts=opened_ts, trade_id=trade_id)
    if res.get("status") != "ok":
        return None
    order = res.get("order")
    if not order or not order.get("avg_price"):
        return None
    log.info(
        f"[{sym}] SL FILL найден: order_id={order.get('order_id')} "
        f"avg_price={order.get('avg_price')} time={order.get('time')}"
    )
    return "STOP_LOSS", order.get("avg_price"), "exchange_fill"


def reconcile_exchange(wl_all, ts, price_full, existing_trade_ids, lifecycle_state):
    if not ENABLE_BINGX:
        return {"status": "skipped", "reason": "ENABLE_BINGX=false"}
    try:
        import bingx_client
    except Exception as e:
        log.error(f"reconcile: bingx_client недоступен: {e}")
        return {"status": "error", "error": str(e)[:200]}
    res = bingx_client.list_positions()
    if res.get("status") != "ok":
        log.error(f"reconcile: запрос позиций не удался: {res.get('error')}")
        send_tg(
            f"⚠️ <b>Сверка с биржей не выполнена</b>\n{esc(str(res.get('error'))[:200])}\n"
            f"<i>Расхождения журнала и биржи в этом прогоне не проверены.</i>"
        )
        return {"status": "error", "error": res.get("error")}
    exch = res["positions"]
    sl_changed = _process_filled_sls(wl_all, ts)
    tp_changed = _process_filled_tps(wl_all, ts, price_full, exch)
    if sl_changed or tp_changed:
        save_watchlist(wl_all)
    journal = {}
    for sym, entry in wl_all.items():
        ot = entry.get("open_trade")
        if not ot:
            continue
        bx = ot.get("bingx") or {}
        journal[sym] = {
            "qty": bx.get("qty_remaining", bx.get("qty")),
            "bx_symbol": bingx_client.to_bx_symbol(sym),
            "status": bx.get("status"),
            "trade_id": entry.get("trade_id"),
        }
    # Local zero quantity is only a candidate state. Confirm it per symbol
    # before removing the trade from persistent watch state.
    zero_qty_confirmed = []
    zero_qty_unresolved = []
    for sym, record in list(journal.items()):
        try:
            local_qty = float(record.get("qty") or 0.0)
        except (TypeError, ValueError):
            local_qty = 0.0
        if local_qty > 0:
            continue
        entry = wl_all.get(sym)
        ot = (entry or {}).get("open_trade")
        if not ot:
            continue
        try:
            pos_check = bingx_client.get_position(sym)
        except Exception as e:
            pos_check = {"status": "error", "error": str(e)}
        status = pos_check.get("status")
        if status == "error":
            bx = ot.get("bingx") or {}
            bx["qty_resync_required"] = True
            bx["qty_resync_error"] = str(pos_check.get("error", "position check failed"))[:300]
            ot["bingx"] = bx
            entry["open_trade"] = ot
            zero_qty_unresolved.append(sym)
            continue
        if status == "found" and float(pos_check.get("positionAmt", 0) or 0) > 0:
            bx = ot.get("bingx") or {}
            bx["status"] = "opened"
            bx["qty_remaining"] = float(pos_check.get("positionAmt") or 0)
            bx["qty_exchange_confirmed"] = True
            bx.pop("qty_resync_required", None)
            bx.pop("qty_resync_error", None)
            if pos_check.get("avgPrice") is not None:
                bx["bingx_avg_price"] = pos_check.get("avgPrice")
            ot["bingx"] = bx
            entry["open_trade"] = ot
            zero_qty_unresolved.append(sym)
            continue
        if status == "not_found":
            sl_exit = _detect_sl_exit(ot, sym)
            if sl_exit:
                exit_reason, cur, exit_price_source = sl_exit
                lifecycle_complete = True
            else:
                exit_reason = "EXCHANGE_CLOSED"
                lifecycle_complete = False
                cur = ot.get("last_price")
                exit_price_source = "last_seen" if cur else "unknown"
            close_trade(
                ot, sym, ts, cur or None, exit_reason, entry.get("state", "UNKNOWN"),
                price_full, exit_price_source=exit_price_source,
                exit_candidates=[exit_reason], lifecycle_complete=lifecycle_complete,
                exchange_close_status="confirmed",
            )
            cd = COOLDOWN_BY_EXIT_REASON.get(exit_reason, 60)
            lrec = lifecycle_state.setdefault(sym, {})
            lrec["cooldown_until"] = ts + cd * 60
            lrec["last_exit_reason"] = exit_reason
            lrec["last_exit_ts"] = ts
            lrec["last_exit_price"] = cur or ot.get("last_price")
            lrec.setdefault("idea_first_seen_ts", ot.get("idea_first_seen_ts") or ts)
            entry.pop("open_trade", None)
            entry.pop("trade_id", None)
            zero_qty_confirmed.append(sym)
            continue
        zero_qty_unresolved.append(sym)

    if zero_qty_confirmed:
        save_watchlist(wl_all)

    ours = {v["bx_symbol"]: sym for sym, v in journal.items()}
    orphans = []
    missing = []
    mismatch = []
    for bx_sym, amt in exch.items():
        sym = ours.get(bx_sym)
        if sym is None:
            orphans.append({"bx_symbol": bx_sym, "qty": amt, "our_symbol": None})
            continue
        jq = journal[sym]["qty"]
        if jq is not None:
            try:
                jq_float = float(jq)
                if jq_float > 0:
                    rel = abs(amt - jq_float) / jq_float
                    if rel > RECONCILE_QTY_TOLERANCE:
                        mismatch.append(
                            {
                                "symbol": sym,
                                "bx_symbol": bx_sym,
                                "journal_qty": jq,
                                "exchange_qty": amt,
                                "rel_diff": round(rel, 4),
                            }
                        )
            except (ValueError, TypeError):
                log.warning(f"[{sym}] Invalid journal qty during reconciliation: {jq!r}")

    for sym, v in journal.items():
        if v["bx_symbol"] not in exch:
            qty_val = v.get("qty")
            if qty_val is not None:
                try:
                    if float(qty_val) > 0:
                        missing.append(
                            {
                                "symbol": sym,
                                "bx_symbol": v["bx_symbol"],
                                "journal_qty": qty_val,
                                "bingx_status": v["status"],
                                "trade_id": v["trade_id"],
                            }
                        )
                except (ValueError, TypeError):
                    log.warning(
                        f"[{sym}] Invalid journal qty during reconciliation: {qty_val!r}"
                    )

    rec = {
        "ts": ts,
        "exchange_positions": len(exch),
        "journal_positions": len(journal),
        "orphans": orphans,
        "missing": missing,
        "mismatch": mismatch,
        "autoclose": RECONCILE_AUTOCLOSE,
    }
    for o in orphans:
        log.error(f"RECONCILE ORPHAN {o['bx_symbol']} qty={o['qty']} — нет в журнале")
        if RECONCILE_AUTOCLOSE:
            try:
                # Use the canonical close path exactly once. close_long() owns
                # TP/SL cancellation, verifies it, then performs the idempotent
                # MARKET close. Avoid a separate pre-cancel here: it caused
                # duplicate DELETE calls and an unnecessary state-change window.
                r = bingx_client.close_long(
                    o["bx_symbol"],
                    float(o["qty"]),
                    cancel_tp=True,
                    trade_id=None,
                )
                o["autoclose_result"] = r
                log.info(
                    f"RECONCILE ORPHAN {o['bx_symbol']} автозакрытие: {r.get('status')}"
                )
            except Exception as e:
                o["autoclose_result"] = {"status": "error", "error": str(e)[:200]}
    if orphans:
        lines = "\n".join(
            f"· {esc(o['bx_symbol'])} qty={esc(o['qty'])}"
            + (
                f" → {esc(o.get('autoclose_result',{}).get('status'))}"
                if RECONCILE_AUTOCLOSE
                else ""
            )
            for o in orphans[:12]
        )
        send_tg(
            f"🔺 <b>Сверка: позиции на бирже без учёта в журнале</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"{len(orphans)} шт.\n{lines}\n"
            + (
                "<i>Автозакрытие включено.</i>"
                if RECONCILE_AUTOCLOSE
                else "<i>Автозакрытие выключено — закрыть вручную или включить "
                "BINGX_RECONCILE_AUTOCLOSE=true.</i>"
            )
        )
    missing_confirmed_closed = []
    missing_unresolved = []
    for m in missing:
        sym = m["symbol"]
        entry = wl_all.get(sym)
        ot = (entry or {}).get("open_trade")
        if not ot:
            continue

        # A successful list_positions() response is not enough to finalize a
        # single missing position. Confirm absence with the authoritative
        # per-symbol endpoint before mutating local trade state. This prevents
        # a transient/incomplete position-list response from closing the local
        # trade record while the exchange position still exists.
        try:
            pos_check = bingx_client.get_position(sym)
        except Exception as e:
            pos_check = {"status": "error", "error": str(e)}

        pos_status = pos_check.get("status")
        if pos_status == "error":
            m["resolution"] = "unresolved_position_check_error"
            m["position_check_error"] = str(pos_check.get("error", "unknown"))[:300]
            missing_unresolved.append(m)
            log.error(
                f"RECONCILE MISSING {sym} — per-symbol position check failed; "
                f"local trade remains OPEN: {m['position_check_error']}"
            )
            continue

        if pos_status == "found" and float(pos_check.get("positionAmt", 0) or 0) > 0:
            # list_positions() omitted a position that get_position() can see.
            # Treat this as a reconciliation inconsistency, not as a close.
            m["resolution"] = "position_still_open"
            m["exchange_qty"] = float(pos_check.get("positionAmt") or 0)
            missing_unresolved.append(m)
            bx = ot.get("bingx") or {}
            bx["status"] = "opened"
            bx["qty_remaining"] = m["exchange_qty"]
            bx["qty_exchange_confirmed"] = True
            if pos_check.get("avgPrice") is not None:
                bx["bingx_avg_price"] = pos_check.get("avgPrice")
            ot["bingx"] = bx
            entry["open_trade"] = ot
            log.error(
                f"RECONCILE MISSING {sym} — list_positions omitted live position; "
                f"get_position confirms qty={m['exchange_qty']}. Trade remains OPEN."
            )
            save_watchlist(wl_all)
            continue

        if pos_status != "not_found":
            m["resolution"] = f"unexpected_position_status:{pos_status}"
            missing_unresolved.append(m)
            log.error(
                f"RECONCILE MISSING {sym} — unexpected per-symbol status={pos_status}; "
                f"trade remains OPEN"
            )
            continue

        m["resolution"] = "confirmed_not_found"
        missing_confirmed_closed.append(m)
        sl_exit = _detect_sl_exit(ot, sym)
        if sl_exit:
            exit_reason, cur, exit_price_source = sl_exit
            lifecycle_complete = True
            m["sl_explained"] = True
            log.error(
                f"RECONCILE MISSING {sym} — на бирже позиции нет, "
                f"обнаружен исполненный SL, закрываем как STOP_LOSS"
            )
        else:
            exit_reason = "EXCHANGE_CLOSED"
            lifecycle_complete = False
            cur = ot.get("last_price")
            exit_price_source = "last_seen" if cur else "unknown"
            log.error(
                f"RECONCILE MISSING {sym} — на бирже позиции нет, "
                f"SL fill не найден, закрываем как EXCHANGE_CLOSED"
            )
        close_trade(
            ot,
            sym,
            ts,
            cur or None,
            exit_reason,
            entry.get("state", "UNKNOWN"),
            price_full,
            exit_price_source=exit_price_source,
            exit_candidates=[exit_reason],
            lifecycle_complete=lifecycle_complete,
            exchange_close_status="confirmed",
        )
        cd = COOLDOWN_BY_EXIT_REASON.get(exit_reason, 60)
        lrec = lifecycle_state.setdefault(sym, {})
        lrec["cooldown_until"] = ts + cd * 60
        lrec["last_exit_reason"] = exit_reason
        lrec["last_exit_ts"] = ts
        lrec["last_exit_price"] = cur or ot.get("last_price")
        lrec.setdefault("idea_first_seen_ts", ot.get("idea_first_seen_ts") or ts)
        entry.pop("open_trade", None)
        entry.pop("trade_id", None)
        save_watchlist(wl_all)
    unexplained = [m for m in missing_confirmed_closed if not m.get("sl_explained")]
    if missing_unresolved:
        send_tg(
            f"⚠️ <b>Сверка: позиция не подтверждена как закрытая</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"{len(missing_unresolved)} шт.: {esc(', '.join(m['symbol'] for m in missing_unresolved[:12]))}\n"
            f"<i>Локальные сделки НЕ закрыты; требуется повторная сверка.</i>"
        )
    if unexplained:
        send_tg(
            f"🔻 <b>Сверка: позиции нет на бирже, но она была в журнале</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"{len(unexplained)} шт.: {esc(', '.join(m['symbol'] for m in unexplained[:12]))}\n"
            f"<i>Закрыты как STOP_LOSS/EXCHANGE_CLOSED (cooldown применён). Возможна ликвидация или ручное закрытие.</i>"
        )
    if mismatch:
        healed = []
        still_mismatch = []
        for m in mismatch:
            sym = m["symbol"]
            entry = wl_all.get(sym)
            ot = (entry or {}).get("open_trade")
            exch_qty = m["exchange_qty"]
            journal_qty = m["journal_qty"]
            if ot and exch_qty < journal_qty:
                log.error(
                    f"[{sym}] RECONCILE SELF-HEAL qty_remaining {journal_qty} → {exch_qty}"
                )
                bx = ot.get("bingx") or {}
                bx["qty_remaining"] = exch_qty
                bx["execution_status"] = "QTY_RESYNCED_FROM_EXCHANGE"
                ot["bingx"] = bx
                entry["open_trade"] = ot
                healed.append(m)
            else:
                still_mismatch.append(m)
        if healed:
            save_watchlist(wl_all)
            lines = "\n".join(
                f"· {esc(m['symbol'])} журнал={esc(m['journal_qty'])} → биржа={esc(m['exchange_qty'])}"
                for m in healed[:10]
            )
            send_tg(
                f"🔧 <b>Сверка: авто-синхронизация объёма</b>\n━━━━━━━━━━━━━━━━━━\n{lines}\n"
                f"<i>qty_remaining подтянут напрямую с биржи.</i>"
            )
        if still_mismatch:
            log.error(f"RECONCILE QTY MISMATCH: {still_mismatch}")
            send_tg(
                f"⚠️ <b>Сверка: расхождение объёмов</b>\n"
                + "\n".join(
                    f"· {esc(m['symbol'])} журнал={esc(m['journal_qty'])} биржа={esc(m['exchange_qty'])}"
                    for m in still_mismatch[:10]
                )
            )
    if orphans or missing or mismatch:
        append_jsonl(RECONCILE_FILE, rec)
    log.info(
        f"reconcile: биржа={len(exch)} журнал={len(journal)} "
        f"сирот={len(orphans)} пропавших={len(missing)} расхождений={len(mismatch)}"
    )
    rec["status"] = "ok"
    return rec


def _touch_state(ot, state, ts, score, reason):
    if state == ot.get("current_state"):
        return
    prev = ot.get("current_state", "UNKNOWN")
    dur = round((ts - ot.get("current_state_start_ts", ts)) / 60, 1)
    ot.setdefault("time_in_states", {})
    ot["time_in_states"][prev] = round(ot["time_in_states"].get(prev, 0) + dur, 1)
    ot["current_state"] = state
    ot["current_state_start_ts"] = ts
    ot.setdefault("state_history", []).append(
        {"ts": ts, "state": state, "score": score, "reason": reason}
    )
    if STATE_RANK.get(state, 0) > STATE_RANK.get(ot.get("max_state"), 0):
        ot["max_state"] = state


def _update_shadows(ot, ts, pnl_pct, strength):
    sh = ot.setdefault("shadow_stops", {str(l): None for l in SHADOW_STOP_LEVELS})
    if pnl_pct is not None:
        for lvl in SHADOW_STOP_LEVELS:
            key = str(lvl)
            if sh.get(key) is None and pnl_pct <= -lvl:
                sh[key] = {
                    "ts": ts,
                    "pnl_at_trigger": round(pnl_pct, 3),
                    "minutes": round((ts - ot["entry_ts"]) / 60, 1),
                }
    if strength is not None and USE_SCHMITT:
        on = ot.get("shadow_schmitt_on", True)
        on = (strength >= SCHMITT_ENTER) if not on else (strength >= SCHMITT_EXIT)
        if (
            ot.get("shadow_schmitt_on")
            and not on
            and ot.get("shadow_schmitt_exit_ts") is None
        ):
            ot["shadow_schmitt_exit_ts"] = ts
            ot["shadow_schmitt_exit_min"] = round((ts - ot["entry_ts"]) / 60, 1)
        ot["shadow_schmitt_on"] = on
        ot["last_signal_strength"] = strength
    elif strength is not None:
        ot["last_signal_strength"] = strength


def _exchange_position_snapshot(symbol):
    """Return exchange-confirmed LONG position data for recovery."""
    try:
        import bingx_client
        pos = bingx_client.get_position(symbol)
    except Exception as e:
        log.error(f"[{symbol}] protection recovery: get_position exception: {e}")
        return None
    if pos.get("status") != "found":
        return None
    try:
        qty = float(pos.get("positionAmt", 0) or 0)
        avg_price = float(pos.get("avgPrice", 0) or pos.get("entryPrice", 0) or 0)
    except (TypeError, ValueError):
        return None
    if qty <= 0 or avg_price <= 0:
        return None
    return {"qty": qty, "avg_price": avg_price, "position": pos}


def _sync_exchange_position_qty(ot, symbol):
    """Synchronize local BingX quantity/price from the exchange before protection recovery."""
    bx = ot.setdefault("bingx", {})
    snap = _exchange_position_snapshot(symbol)
    if not snap:
        return None
    qty = snap["qty"]
    avg_price = snap["avg_price"]
    previous_qty = safe(bx.get("qty_remaining"), safe(bx.get("qty_initial"), safe(bx.get("qty"), 0.0)))
    previous_avg = bx.get("bingx_avg_price")
    was_initial_uncertain = bool(bx.get("qty_initial_uncertain"))
    bx["status"] = "opened"
    bx["bingx_avg_price"] = avg_price

    # If OPEN was previously persisted with an explicitly uncertain requested
    # quantity, the first authoritative exchange snapshot must replace that
    # placeholder.  Otherwise a stale requested qty would remain as
    # allocation_base_qty and could distort TP sizing and realized PnL after
    # a restart/recovery.  Once an initial size is exchange-confirmed, keep it
    # immutable: later partial/external closes must affect only qty_remaining.
    if was_initial_uncertain or not bx.get("qty_initial"):
        bx["qty_initial"] = qty
        bx["qty_initial_uncertain"] = False
    else:
        try:
            bx["qty_initial"] = float(bx["qty_initial"])
        except (TypeError, ValueError):
            bx["qty_initial"] = qty
            bx["qty_initial_uncertain"] = False

    # Exchange is authoritative for the currently open quantity.
    bx["qty_remaining"] = qty
    bx["qty_exchange_confirmed"] = True
    if previous_qty and abs(previous_qty - qty) > 1e-12:
        log.warning(f"[{symbol}] PROTECTION RECOVERY qty sync: {previous_qty} -> {qty}")
    if previous_avg and abs(float(previous_avg) - avg_price) > 1e-12:
        log.warning(f"[{symbol}] PROTECTION RECOVERY avg_price sync: {previous_avg} -> {avg_price}")
    ot["bingx"] = bx
    return snap


def _retry_exchange_protection(ot, symbol):
    """Reconcile TP and SL from exchange state; never rely on a prior attempt flag."""
    if not ENABLE_BINGX:
        return False
    bx = ot.get("bingx") or {}
    if bx.get("status") not in ("opened", "already_open"):
        return False

    snap = _sync_exchange_position_qty(ot, symbol)
    if not snap:
        log.warning(f"[{symbol}] PROTECTION_RECOVERY skipped: live exchange position not confirmed")
        return False

    qty = snap["qty"]
    avg_price = snap["avg_price"]
    trade_id = ot.get("trade_id_full")
    try:
        sl_pct, tp_levels, protection_source = get_trade_protection(ot)
    except Exception as e:
        log.error(f"[{symbol}] PROTECTION_RECOVERY invalid persisted protection: {e}")
        return False

    changed = False
    try:
        import bingx_client

        expected = {str(tp.get("leg")).lower() for tp in tp_levels if tp.get("leg")}

        # --- TP reconciliation ---
        tp_existing = bingx_client.get_existing_tp_legs(symbol, tp_levels, trade_id=trade_id)
        filled_legs = set()
        filled_qty_by_leg = {}
        if tp_existing.get("status") == "ok":
            opened_ts = ot.get("opened_ts", ot.get("entry_ts", 0))
            filled_res = bingx_client.get_filled_tp_orders(
                symbol, opened_ts=opened_ts, trade_id=trade_id
            )
            if filled_res.get("status") == "ok":
                for filled_order in filled_res.get("orders", []):
                    leg_name = str(filled_order.get("leg") or "").lower()
                    if not leg_name:
                        continue
                    try:
                        filled_qty_by_leg[leg_name] = (
                            filled_qty_by_leg.get(leg_name, 0.0)
                            + float(filled_order.get("executed_qty", 0.0) or 0.0)
                        )
                    except (TypeError, ValueError):
                        continue
                    if filled_order.get("is_fully_filled"):
                        filled_legs.add(leg_name)

        if tp_existing.get("status") == "error":
            bx["tp_status"] = "TP_UNKNOWN"
            log.error(f"[{symbol}] TP_RECOVERY exchange check failed: {tp_existing.get('error')}")
        else:
            existing_active_qty = float(tp_existing.get("existing_qty", 0.0) or 0.0)
            duplicate_tp_legs = [str(x).lower() for x in (tp_existing.get("duplicate_legs") or [])]
            qty_tolerance = max(qty * 1e-9, 1e-12)

            # Exchange quantity is authoritative. If active TP orders cover
            # more volume than the live position, OR multiple active orders
            # exist for the same logical TP leg, the current protection set
            # must be rebuilt. Keeping duplicate legs can cause multiple
            # trigger orders to consume the same position quantity.
            if duplicate_tp_legs or existing_active_qty > qty + qty_tolerance:
                log.error(
                    f"[{symbol}] TP_RECOVERY mismatch: active TP qty="
                    f"{existing_active_qty:.12f} position qty={qty:.12f} "
                    f"duplicate_legs={duplicate_tp_legs}; rebuilding current TP set"
                )
                cancel_res = bingx_client.cancel_take_profit_orders(
                    symbol, trade_id=trade_id
                )
                if cancel_res.get("status") not in ("cancelled", "no_orders"):
                    bx["tp_status"] = "TP_UNKNOWN"
                    bx["protection_error"] = (
                        "cannot rebuild TP after quantity mismatch: "
                        f"cancel={cancel_res.get('status')} "
                        f"{cancel_res.get('error', '')}"
                    )
                    log.error(
                        f"[{symbol}] TP_RECOVERY blocked: old TP cancellation "
                        f"not confirmed ({cancel_res.get('status')})"
                    )
                    # The original tp_existing snapshot is now stale and must
                    # never be used for a second creation attempt in this run.
                    bx["tp_reconciled_ts"] = now_ts()
                    bx["execution_status"] = "PROTECTION_DEGRADED"
                    ot["bingx"] = bx
                    return changed

                tp_result = bingx_client.place_take_profit_orders(
                    symbol,
                    avg_price,
                    qty,
                    tp_levels,
                    trade_id=trade_id,
                    allocation_base_qty=safe(bx.get("qty_initial"), qty),
                    completed_legs=filled_legs,
                    filled_qty_by_leg=filled_qty_by_leg,
                )
                if tp_result.get("status") in ("created", "already_exists"):
                    verify = bingx_client.get_existing_tp_legs(
                        symbol, tp_levels, trade_id=trade_id
                    )
                    if verify.get("status") == "ok":
                        verify_filled_res = bingx_client.get_filled_tp_orders(
                            symbol, opened_ts=opened_ts, trade_id=trade_id
                        )
                        verify_filled = {
                            str(o.get("leg")).lower()
                            for o in (verify_filled_res.get("orders", []) if verify_filled_res.get("status") == "ok" else [])
                            if o.get("leg") and o.get("is_fully_filled")
                        }
                        verify_accounted = {
                            str(leg).lower()
                            for leg, present in (verify.get("legs") or {}).items()
                            if present
                        } | verify_filled
                        bx["tp_orders"] = verify.get("orders", [])
                        bx["tp_filled_legs"] = sorted(verify_filled)
                        bx["tp_status"] = (
                            "TP_PLACED"
                            if verify_accounted >= expected
                            and float(verify.get("existing_qty", 0.0) or 0.0) <= qty + qty_tolerance
                            else "TP_PARTIAL"
                        )
                        changed = True
                    else:
                        bx["tp_status"] = "TP_UNKNOWN"
                else:
                    bx["tp_status"] = "TP_FAILED"
                    bx["protection_error"] = str(tp_result.get("error", "unknown"))[:500]

                # Rebuild used a fresh exchange snapshot. Do not fall through
                # into the old `missing` branch using pre-rebuild data.
                bx["tp_reconciled_ts"] = now_ts()
                ot["bingx"] = bx
                return changed

            missing = [
                leg for leg in tp_existing.get("missing", [])
                if str(leg).lower() not in filled_legs
            ]
            accounted = {
                str(leg).lower()
                for leg, present in (tp_existing.get("legs") or {}).items()
                if present
            } | filled_legs
            if accounted >= expected:
                bx["tp_orders"] = tp_existing.get("orders", [])
                bx["tp_filled_legs"] = sorted(filled_legs)
                bx["tp_status"] = "TP_PLACED"
                changed = True
            elif missing:
                bx["tp_status"] = "TP_PARTIAL" if tp_existing.get("orders") or filled_legs else "TP_MISSING"
                log.warning(
                    f"[{symbol}] TP_RECOVERY missing={missing} filled={sorted(filled_legs)}"
                )
                tp_result = bingx_client.place_take_profit_orders(
                    symbol,
                    avg_price,
                    qty,
                    tp_levels,
                    trade_id=trade_id,
                    allocation_base_qty=safe(bx.get("qty_initial"), qty),
                    completed_legs=filled_legs,
                    filled_qty_by_leg=filled_qty_by_leg,
                )
                if tp_result.get("status") in ("created", "already_exists"):
                    verify = bingx_client.get_existing_tp_legs(symbol, tp_levels, trade_id=trade_id)
                    if verify.get("status") == "ok":
                        verify_filled_res = bingx_client.get_filled_tp_orders(
                            symbol, opened_ts=opened_ts, trade_id=trade_id
                        )
                        verify_filled = {
                            str(o.get("leg")).lower()
                            for o in (verify_filled_res.get("orders", []) if verify_filled_res.get("status") == "ok" else [])
                            if o.get("leg") and o.get("is_fully_filled")
                        }
                        verify_accounted = {
                            str(leg).lower()
                            for leg, present in (verify.get("legs") or {}).items()
                            if present
                        } | verify_filled
                        if verify_accounted >= expected:
                            bx["tp_orders"] = verify.get("orders", [])
                            bx["tp_filled_legs"] = sorted(verify_filled)
                            bx["tp_status"] = "TP_PLACED"
                            changed = True
                        else:
                            bx["tp_orders"] = verify.get("orders", [])
                            bx["tp_status"] = "TP_PARTIAL"
                    else:
                        bx["tp_status"] = "TP_PARTIAL"
                else:
                    bx["tp_status"] = "TP_FAILED"
                    bx["protection_error"] = str(tp_result.get("error", "unknown"))[:500]
            else:
                # No active TP is missing: all absent legs are already confirmed filled.
                bx["tp_orders"] = tp_existing.get("orders", [])
                bx["tp_filled_legs"] = sorted(filled_legs)
                bx["tp_status"] = "TP_PLACED"
                changed = True

        # --- SL reconciliation, independent from TP state ---
        sl_existing = bingx_client.get_open_sl_orders(symbol)
        if sl_existing.get("status") == "ok" and sl_existing.get("count", 0) > 0:
            owned = []
            for order in sl_existing.get("orders", []):
                parsed = bingx_client.parse_sl_client_order_id(str(order.get("clientOrderId", "")))
                if parsed and (not trade_id or bingx_client._sl_belongs_to_trade(parsed, trade_id)):
                    owned.append(order)
            if owned:
                # There must be exactly one SL for the current trade, and it
                # must cover the current exchange-confirmed position size.
                sl_qtys = []
                for order in owned:
                    try:
                        orig_qty = float(order.get("origQty") or order.get("quantity") or 0.0)
                        executed_qty = float(order.get("executedQty") or 0.0)
                        sl_qtys.append(max(0.0, orig_qty - executed_qty) if orig_qty > 0 else 0.0)
                    except (TypeError, ValueError):
                        sl_qtys.append(0.0)
                sl_qty_ok = (
                    len(owned) == 1
                    and sl_qtys[0] > 0
                    and abs(sl_qtys[0] - qty) <= max(qty * 1e-9, 1e-12)
                )
                if sl_qty_ok:
                    bx["sl_order"] = owned[0]
                    bx["sl_status"] = "SL_PLACED"
                    changed = True
                else:
                    log.warning(
                        f"[{symbol}] SL_RECOVERY quantity/duplicate mismatch: "
                        f"orders={len(owned)} qtys={sl_qtys} position_qty={qty}; rebuilding SL"
                    )
                    cancel_res = bingx_client.cancel_stop_loss_orders(
                        symbol, trade_id=trade_id
                    )
                    if cancel_res.get("status") not in ("cancelled", "no_orders"):
                        bx["sl_status"] = "SL_UNKNOWN"
                        bx["protection_error"] = (
                            "cannot rebuild SL after quantity/duplicate mismatch: "
                            f"cancel={cancel_res.get('status')} "
                            f"{cancel_res.get('error', '')}"
                        )
                    else:
                        existing_stops = []
                        for order in owned:
                            try:
                                candidate = float(order.get("stopPrice") or order.get("triggerPrice") or 0.0)
                            except (TypeError, ValueError):
                                candidate = 0.0
                            if candidate > 0:
                                existing_stops.append(candidate)

                        # LONG protection: preserve the most protective existing stop
                        # when rebuilding after quantity/duplicate mismatch. Rebuilding
                        # from the original sl_pct could otherwise move a previously
                        # trailed SL backwards and weaken protection.
                        existing_stop = max(existing_stops) if existing_stops else None
                        sl_result = bingx_client.place_stop_loss_order(
                            symbol,
                            avg_price,
                            qty,
                            sl_pct,
                            trade_id=trade_id,
                            stop_price_override=existing_stop,
                        )
                        if sl_result.get("status") == "created":
                            bx["sl_order"] = sl_result
                            bx["sl_status"] = "SL_PLACED"
                            changed = True
                        else:
                            bx["sl_status"] = "SL_FAILED"
                            bx["protection_error"] = str(sl_result.get("error", "unknown"))[:500]
            else:
                bx["sl_status"] = "SL_MISSING"
        elif sl_existing.get("status") == "error":
            bx["sl_status"] = "SL_UNKNOWN"
            log.error(f"[{symbol}] SL_RECOVERY exchange check failed: {sl_existing.get('error')}")
        else:
            bx["sl_status"] = "SL_MISSING"

        if bx.get("sl_status") in ("SL_MISSING", "SL_FAILED"):
            sl_result = bingx_client.place_stop_loss_order(
                symbol, avg_price, qty, sl_pct, trade_id=trade_id
            )
            if sl_result.get("status") == "created":
                bx["sl_order"] = sl_result
                bx["sl_status"] = "SL_PLACED"
                changed = True
            else:
                bx["sl_status"] = "SL_FAILED"
                bx["protection_error"] = str(sl_result.get("error", "unknown"))[:500]

        bx["execution_status"] = (
            "PROTECTED"
            if bx.get("tp_status") == "TP_PLACED" and bx.get("sl_status") == "SL_PLACED"
            else "PROTECTION_DEGRADED"
        )
        bx["protection_reconciled_ts"] = now_ts()
        ot["bingx"] = bx
        return changed
    except Exception as e:
        bx["execution_status"] = "PROTECTION_EXCEPTION"
        bx["protection_error"] = str(e)[:500]
        bx["protection_reconciled_ts"] = now_ts()
        ot["bingx"] = bx
        log.error(f"[{symbol}] PROTECTION_RECOVERY exception: {e}")
        return False


def manage_open_trade(
    sym,
    ot,
    ts,
    cur_price,
    signal,
    price_full,
    missed_runs=0,
    symbol_in_current_scrape=True,
    scrape_complete=True,
):
    _retry_exchange_protection(ot, sym)
    state = (signal or {}).get("state")
    score = (signal or {}).get("score", 0)
    strength = (signal or {}).get("strength")
    dq = ot.setdefault("data_quality", {})
    health = ot.setdefault("position_health", {})
    ot["position_manager_version"] = POSITION_MANAGER_VERSION
    if cur_price:
        ot["last_price"] = cur_price
        ot["last_price_ts"] = ts
        dq["last_price_observation"] = "live"
        dq["last_price_observation_ts"] = ts
        ep = ot.get("entry_price")
        if ep:
            pnl_now = (cur_price - ep) / ep * 100
            if pnl_now > ot.get("max_pnl_pct", 0.0):
                ot["max_pnl_pct"] = pnl_now
                ot["peak_ts"] = ts
            if pnl_now < ot.get("min_pnl_pct", 0.0):
                ot["min_pnl_pct"] = pnl_now
    elif not scrape_complete:
        dq["last_price_observation"] = "scrape_incomplete"
        dq["last_price_observation_ts"] = ts
    elif not symbol_in_current_scrape:
        dq["discovery_missing_count"] = dq.get("discovery_missing_count", 0) + 1
        dq["last_price_observation"] = "discovery_missing"
        dq["last_price_observation_ts"] = ts
    else:
        dq["missing_snapshots"] = dq.get("missing_snapshots", 0) + 1
        dq["last_price_observation"] = "price_missing"
        dq["last_price_observation_ts"] = ts
    price_age_min = round((ts - ot.get("last_price_ts", ts)) / 60, 1)
    dq["max_price_age_min"] = max(dq.get("max_price_age_min", 0), price_age_min)
    dq["exit_price_age_min"] = price_age_min
    if state:
        _touch_state(
            ot,
            state,
            ts,
            score,
            "state_transition",
        )
    _fill_horizons(ot, sym, ts, price_full)
    ep = ot.get("entry_price")
    pnl_pct = ((cur_price - ep) / ep * 100) if (cur_price and ep) else None
    _update_shadows(ot, ts, pnl_pct, strength)
    if state in ("CONFIRMED_TREND", "ACCELERATION") or (state and score >= 5):
        ot["last_signal_ts"] = ts
    last_signal_ts = ot.get("last_signal_ts", ot["entry_ts"])
    age_min = round((ts - ot["entry_ts"]) / 60, 1)
    signal_age_min = round((ts - last_signal_ts) / 60, 1)
    timeout_reached = age_min >= TRADE_TIMEOUT_MIN
    signal_decay = ts - last_signal_ts >= SIGNAL_DECAY_MIN * 60
    discovery_missing = scrape_complete and not symbol_in_current_scrape
    data_stale = (
        scrape_complete
        and symbol_in_current_scrape
        and not cur_price
        and price_age_min >= PRICE_STALE_EXIT_MIN
    )
    missed = missed_runs >= MISS_REMOVE_RUNS
    protection_sl_pct, _, protection_source = get_trade_protection(ot)
    bx_sl = (ot.get("bingx") or {}).get("sl_order") or {}
    stop_p = bx_sl.get("stop_price")
    if stop_p and ep and ep > 0:
        is_sl_hit = (cur_price is not None and cur_price <= stop_p)
    else:
        is_sl_hit = (
            cur_price is not None and pnl_pct is not None and pnl_pct <= -protection_sl_pct
        )

    hard_candidates = {
        "EXCHANGE_CLOSED": False,
        "INVALIDATED": state == "INVALIDATED",
        "EXHAUSTION": state == "EXHAUSTION",
        "DISTRIBUTION": state == "DISTRIBUTION",
        "STOP_LOSS": is_sl_hit,
        "TIMEOUT": timeout_reached,
    }
    soft_conditions = {
        "SIGNAL_DECAY": signal_decay,
        "MISSED": missed,
        "DATA_STALE": data_stale,
        "DISCOVERY_MISSING": discovery_missing,
    }
    if discovery_missing or missed or data_stale:
        health_status = "DATA_DEGRADED"
    elif timeout_reached and signal_decay:
        health_status = "AGING_WEAK"
    elif timeout_reached:
        health_status = "AGING"
    elif signal_decay:
        health_status = "WEAK"
    else:
        health_status = "HEALTHY"
    health.update(
        {
            "version": POSITION_MANAGER_VERSION,
            "last_check_ts": ts,
            "status": health_status,
            "state": state,
            "score": score,
            "strength": strength,
            "age_min": age_min,
            "signal_age_min": signal_age_min,
            "price_age_min": price_age_min,
            "pnl_pct": round(pnl_pct, 3) if pnl_pct is not None else None,
            "timeout_reached": timeout_reached,
            "signal_decay": signal_decay,
            "missed": missed,
            "data_stale": data_stale,
            "discovery_missing": discovery_missing,
            "soft_conditions": soft_conditions,
            "hard_candidates": hard_candidates,
        }
    )
    try:
        append_jsonl(
            EXECUTION_EVENTS_FILE,
            {
                "ts": ts,
                "symbol": sym,
                "kind": "position_manager_decision",
                "position_manager_version": POSITION_MANAGER_VERSION,
                "trade_id": ot.get("trade_id_full"),
                "decision_state": state,
                "score": score,
                "position_age_min": age_min,
                "signal_age_min": signal_age_min,
                "pnl_pct": round(pnl_pct, 3) if pnl_pct is not None else None,
                "soft_conditions": soft_conditions,
                "hard_candidates": hard_candidates,
                "health_status": health_status,
                "decision": ("PENDING_HARD_EXIT" if any(hard_candidates.values()) else "HOLD"),
            },
        )
    except Exception as e:
        log.error(f"[{sym}] position manager audit failed: {e}")
    close_reason, all_triggered = resolve_exit_reason(hard_candidates)
    if close_reason:
        log.warning(
            f"[{sym}] POSITION EXIT reason={close_reason} state={state} "
            f"score={score} pnl={pnl_pct} age={age_min}m "
            f"signal_age={signal_age_min}m soft={soft_conditions}"
        )
        x_price = cur_price if cur_price else ot.get("last_price")
        x_src = "live" if cur_price else ("last_seen_snapshot" if ot.get("last_price") else "unknown")
        return close_reason, all_triggered, x_price, x_src, False
    active_soft = [k for k, v in soft_conditions.items() if v]
    if active_soft:
        log.info(
            f"[{sym}] POSITION HOLD soft={active_soft} health={health_status} "
            f"state={state} score={score} pnl={pnl_pct} age={age_min}m "
            f"signal_age={signal_age_min}m"
        )
    return None, [], None, None, False


def run():
    log.info("═══ Прогон ═══")
    import bingx_client

    _ensure_stealth_available()
    _retry_pending_tp_notifications()
    wl_all = load_watchlist()
    global PENDING
    PENDING = load_pending()
    existing_trade_ids = load_existing_trade_ids()
    lifecycle_state = load_lifecycle_state()
    for _sym, _entry in wl_all.items():
        _ot = _entry.get("open_trade")
        if _ot and _sym not in lifecycle_state:
            lifecycle_state[_sym] = {
                "idea_first_seen_ts": _ot.get("idea_first_seen_ts") or now_ts()
            }
    global DISCOVERY
    DISCOVERY = discovery_fingerprint()
    log.info(
        f"Discovery: sha={DISCOVERY['url_sha256']} filter={str(DISCOVERY.get('filter'))[:120]}"
    )
    log_discovery_change(DISCOVERY, now_ts())
    rows = fetch_data()
    log.info(f"Монет после discovery-фильтра: {len(rows)}")
    market = detect_market_phase(rows)
    log.info(
        f"Market: {market['phase']} btc={market.get('btc_chg24')} "
        f"src={market.get('btc_source')} breadth={market.get('breadth_ratio')} "
        f"mod={market['modifier']} (raw={market.get('modifier_raw')})"
    )
    current_symbols = {r["symbol"] for r in rows}
    ts = now_ts()
    for r in rows:
        append_jsonl(
            HEARTBEAT_FILE, {"ts": ts, "symbol": r["symbol"], "price": r.get("price")}
        )
        sym = r["symbol"]
        append_jsonl(
            MARKET_HISTORY_FILE,
            {**r, "lifecycle_state": wl_all.get(sym, {}).get("state")},
        )
    history_all = load_market_history()
    price_full = load_price_full()
    try:
        reconcile_exchange(wl_all, ts, price_full, existing_trade_ids, lifecycle_state)
    except Exception as e:
        log.exception(f"reconcile упал: {e}")
        send_tg(f"⚠️ <b>Сверка с биржей упала</b>\n{esc(str(e)[:200])}")
    for sym in list(wl_all.keys()):
        entry = wl_all[sym]
        ot = entry.get("open_trade")
        if not ot or "pending_close" not in ot:
            continue
        ot = dict(ot)
        pc = ot["pending_close"]
        exch_status = _attempt_exchange_close(ot, sym)
        if exch_status == "unconfirmed":
            entry["open_trade"] = ot
            log.warning(f"[{sym}] pending_close всё ещё не подтверждён биржей")
            continue
        ot.pop("pending_close")
        close_trade(
            ot,
            sym,
            ts,
            pc["xprice"],
            pc["reason"],
            pc["exit_state"],
            price_full,
            exit_price_source=pc["xsrc"],
            exit_candidates=pc["triggered"],
            lifecycle_complete=pc["lifecycle_complete"],
            exchange_close_status=exch_status,
        )
        cd = COOLDOWN_BY_EXIT_REASON.get(pc["reason"], 0)
        lrec = lifecycle_state.setdefault(sym, {})
        lrec["last_exit_price"] = pc.get("xprice") or ot.get("last_price")
        if cd > 0:
            lrec["cooldown_until"] = ts + cd * 60
            lrec["last_exit_reason"] = pc["reason"]
            lrec["last_exit_ts"] = ts
            lrec.setdefault("idea_first_seen_ts", ot.get("idea_first_seen_ts") or ts)
        entry.pop("open_trade", None)
        entry.pop("trade_id", None)
        log.info(f"[{sym}] CLOSE (подтверждено с задержкой) {pc['reason']}")
        save_watchlist(wl_all)
    signals = {}
    for sym, hist in history_all.items():
        if not hist or sym not in current_symbols:
            continue
        r = hist[-1]
        raw_score, pros, cons = calculate_score(r)
        score = clamp(raw_score + market["modifier"], 0, 10)
        derived = calc_derived(hist)
        momentum, mom_tags = calc_momentum(derived)
        pattern = detect_pattern(r, derived, momentum)
        prev_state = wl_all.get(sym, {}).get("state", "NEUTRAL")
        state, reasons, warnings, path = detect_lifecycle(
            sym, hist, score, derived, prev_state
        )
        conf = calc_confidence(state, hist, score, derived, market["modifier"])
        early_val, early_label = entry_earliness(r)
        recent = (
            hist[-MIN_SNAPS_LIFECYCLE:] if len(hist) >= MIN_SNAPS_LIFECYCLE else hist
        )
        signals[sym] = {
            "row": r,
            "hist": hist,
            "state": state,
            "prev_state": prev_state,
            "reasons": reasons,
            "warnings": warnings,
            "path": path,
            "score": score,
            "pros": pros,
            "cons": cons,
            "derived": derived,
            "momentum": momentum,
            "mom_tags": mom_tags,
            "pattern": pattern,
            "conf": conf,
            "early_val": early_val,
            "early_label": early_label,
            "price": r.get("price") if valid_price(r.get("price")) else None,
            "strength": signal_strength(hist, derived["cvd_momentum"]),
            "window": window_quality(recent),
            "history_len": len(hist),
            "shadow": shadow_variants(hist, derived["cvd_momentum"]),
        }
    for sym, sig in signals.items():
        sh = sig["shadow"]
        if not sh.get("disagrees"):
            continue
        append_jsonl(
            SHADOW_SIGNALS_FILE,
            {
                "ts": ts,
                "symbol": sym,
                "price": sig["price"],
                "live_state": sig["state"],
                "prev_state": sig["prev_state"],
                "allowed_confirmed": sig["prev_state"]
                in ALLOWED_FROM.get("CONFIRMED_TREND", set()),
                "score": sig["score"],
                "momentum": sig["momentum"],
                "strength": sh["strength"],
                "window": sh["window"],
                "history_len": sig["history_len"],
                "variants": {k: sh[k] for k, _ in SHADOW_VARIANTS},
                "signal_logic_version": SIGNAL_LOGIC_VERSION,
                "conditions_version": CONDITIONS_CONFIG["conditions_version"],
            },
        )
    for sym, entry in wl_all.items():
        if sym in current_symbols:
            entry["missed_runs"] = 0
            entry["last_seen"] = ts
        elif LAST_SCRAPE_COMPLETE:
            entry["missed_runs"] = entry.get("missed_runs", 0) + 1
            entry["last_seen"] = entry.get("last_seen", ts)
            if (
                entry["state"] in ACTIVE_STATES
                and entry["missed_runs"] == MISS_EXIT_RUNS
            ):
                log.info(
                    f"[{sym}] выпала из фильтра {entry['missed_runs']} прогона → пауза "
                    f"(state={entry['state']})"
                )
        else:
            log.info(f"[{sym}] отсутствует в частичном scrape — missed_runs не меняем")
    for sym in list(wl_all.keys()):
        entry = wl_all[sym]
        ot = entry.get("open_trade")
        if not ot or "pending_close" in ot:
            continue
        ot = dict(ot)
        sig = signals.get(sym)
        cur_price = sig["price"] if sig else None
        try:
            reason, triggered, xprice, xsrc, exec_changed = manage_open_trade(
                sym,
                ot,
                ts,
                cur_price,
                sig,
                price_full,
                missed_runs=entry.get("missed_runs", 0),
                symbol_in_current_scrape=(sym in current_symbols),
                scrape_complete=LAST_SCRAPE_COMPLETE,
            )
        except Exception as e:
            log.exception(f"[{sym}] Ошибка в manage_open_trade: {e}")
            if ENABLE_BINGX:
                raise
            continue

        if reason:
            if not xprice:
                log.error(
                    f"[{sym}] нет цены для закрытия — сделка будет помечена outcome_unknown"
                )
                xprice = None
                xsrc = "unknown"
            complete = reason not in ("DATA_STALE", "MISSED")
            exit_state = (sig or {}).get("state") or entry.get("state", "UNKNOWN")
            exch_status = _attempt_exchange_close(ot, sym)
            if exch_status == "unconfirmed":
                ot["pending_close"] = {
                    "reason": reason,
                    "triggered": triggered,
                    "ts": ts,
                    "xprice": xprice,
                    "xsrc": xsrc,
                    "exit_state": exit_state,
                    "lifecycle_complete": complete,
                }
                entry["open_trade"] = ot
                log.error(
                    f"[{sym}] CLOSE NOT CONFIRMED reason={reason} — оставляем как открытую, ждём подтверждения"
                )
                save_watchlist(wl_all)
            else:
                close_trade(
                    ot,
                    sym,
                    ts,
                    xprice,
                    reason,
                    exit_state,
                    price_full,
                    exit_price_source=xsrc,
                    exit_candidates=triggered,
                    lifecycle_complete=complete,
                    exchange_close_status=exch_status,
                )
                cd = COOLDOWN_BY_EXIT_REASON.get(reason, 0)
                rec = lifecycle_state.setdefault(sym, {})
                rec["last_exit_price"] = xprice or ot.get("last_price")
                if cd > 0:
                    rec["cooldown_until"] = ts + cd * 60
                    rec["last_exit_reason"] = reason
                    rec["last_exit_ts"] = ts
                    rec.setdefault(
                        "idea_first_seen_ts", ot.get("idea_first_seen_ts") or ts
                    )
                entry.pop("open_trade", None)
                entry.pop("trade_id", None)
                log.info(f"[{sym}] CLOSE {reason} triggered={triggered} cooldown={cd}м")
                save_watchlist(wl_all)
        else:
            entry["open_trade"] = ot
            if exec_changed:
                save_watchlist(wl_all)
                log.info(
                    f"[{sym}] watchlist сохранён немедленно после execution-события"
                )
    for sym, sig in signals.items():
        r = sig["row"]
        hist = sig["hist"]
        state = sig["state"]
        prev_state = sig["prev_state"]
        score = sig["score"]
        derived = sig["derived"]
        conf = sig["conf"]
        existing = wl_all.get(sym, {})
        has_trade = existing.get("open_trade") is not None
        if state == "NEUTRAL":
            if sym in wl_all and not has_trade:
                old = existing.get("state", "NEUTRAL")
                nr = existing.get("neutral_runs", 0) + 1
                existing["neutral_runs"] = nr
                existing["last_seen"] = ts
                if old in ACTIVE_STATES and nr < NEUTRAL_HYSTERESIS:
                    log.info(f"[{sym}] {old}: NEUTRAL-оценка {nr}/{NEUTRAL_HYSTERESIS}")
                else:
                    if old in ACTIVE_STATES:
                        log.info(
                            f"[{sym}] {old} → NEUTRAL "
                            f"(снята: условия тренда не выполняются {nr} прогонов подряд)"
                        )
                    log.info(f"[{sym}] {old} → NEUTRAL, remove")
                    del wl_all[sym]
            elif has_trade:
                existing["neutral_runs"] = existing.get("neutral_runs", 0) + 1
                existing["last_seen"] = ts
                existing["state_signal"] = "NEUTRAL"
            continue
        if state == "INVALIDATED":
            if sym in wl_all and not wl_all[sym].get("open_trade"):
                send_tg(
                    f"❌ <b>{esc(existing.get('name',sym))} ({esc(sym)})</b>\n━━━━━━━━━━━━━━━━━━\n"
                    f"{esc(existing.get('state','?'))} → INVALIDATED\n"
                    f"<i>Сценарий сломан: {esc(' · '.join(sig['reasons'][:2]))}</i>\n"
                )
                log.info(f"[{sym}] INVALIDATED → remove")
                del wl_all[sym]
            continue
        new_ot = existing.get("open_trade")
        new_tid = existing.get("trade_id")
        bingx_block_reason = existing.get("bingx_entry_block_reason")

        BINGX_RETRYABLE_BLOCK_REASONS = {
            "api_open_disabled",
            "contract_unavailable",
            "contract_state_unavailable",
        }

        if (existing.get("bingx_entry_blocked", False) and bingx_block_reason in BINGX_RETRYABLE_BLOCK_REASONS):
            existing["bingx_entry_blocked"] = False

        bingx_entry_blocked = bool(existing.get("bingx_entry_blocked", False))
        if not has_trade:
            wl_all[sym] = {
                "state": state,
                "previous_state": prev_state,
                "action": ACTIONS.get(state, ""),
                "confidence": conf,
                "score": score,
                "momentum": sig["momentum"],
                "pattern": sig["pattern"],
                "name": r.get("name", sym),
                "first_seen": existing.get("first_seen", ts),
                "last_seen": ts,
                "snapshots": existing.get("snapshots", 0) + 1,
                "missed_runs": existing.get("missed_runs", 0),
                "neutral_runs": 0,
                "entry_earliness": round(sig["early_val"], 2),
                "signal_strength": sig["strength"],
                "window": sig["window"],
                "reasons": sig["reasons"],
                "warnings": sig["warnings"],
                "mom_tags": sig["mom_tags"],
                "bingx_entry_blocked": existing.get("bingx_entry_blocked", False),
                "bingx_entry_block_reason": existing.get("bingx_entry_block_reason"),
                "bingx_entry_block_symbol": existing.get("bingx_entry_block_symbol"),
                "bingx_skip_notify_reason": existing.get("bingx_skip_notify_reason"),
                "bingx_skip_notify_ts": existing.get("bingx_skip_notify_ts"),
            }
            existing = wl_all[sym]

        entry_pc24 = safe(r.get("price_chg24"), 0.0)
        entry_lls = safe(r.get("lls24"), 0.0)
        entry_vol = safe(r.get("volume24"), 0.0)
        entry_quality_ok = (
            0.5 <= entry_pc24 <= 15.0
            and entry_lls < 40.0
            and (entry_vol >= 500_000 or entry_vol == 0.0)
        )
        if not entry_quality_ok and state in ENTRY_STATES and new_ot is None:
            log.info(
                f"[{sym}] ENTRY BLOCKED BY QUALITY GUARD: price_chg24={entry_pc24:.1f}% lls24={entry_lls:.1f} vol={entry_vol}"
            )

        ta_data = None

        if (new_ot is None and not bingx_entry_blocked and state in ENTRY_STATES and sig["price"] and entry_quality_ok):
            info = lifecycle_state.get(sym, {})
            last_exit_p = info.get("last_exit_price")
            last_exit_t = info.get("last_exit_ts", 0)
            if info.get("cooldown_until", 0) > ts:
                left = round((info["cooldown_until"] - ts) / 60, 1)
                log.info(
                    f"[{sym}] COOLDOWN {left}м (после {info.get('last_exit_reason')}), вход пропущен"
                )
            elif (
                last_exit_p
                and (ts - last_exit_t) < 180 * 60
                and sig.get("price")
                and sig["price"] > last_exit_p * 1.08
            ):
                log.info(
                    f"[{sym}] CHURN GUARD: текущая цена {sig['price']} на >8% выше недавнего выхода ({last_exit_p}) за 180м, перезаход пропущен"
                )
            else:
                idea_ts = info.get("idea_first_seen_ts") or ts
                new_tid = _new_trade_id(sym, ts)

                try:
                    ta_bx_symbol = bingx_client.to_bx_symbol(sym)
                    ta_data = ta_context.get_ta_context(ta_bx_symbol)
                    if ta_data:
                        log.info(
                            f"[{sym}] TA snapshot captured for entry: "
                            f"direction={ta_data.get('result_label')} "
                            f"score={ta_data.get('net_score')}/{ta_data.get('max_score')}"
                        )
                except Exception as e:
                    ta_data = None
                    log.warning(f"[{sym}] TA entry snapshot failed: {e}")

                ta_direction_snapshot = _ta_direction_snapshot(ta_data)
                market_context_snapshot = None
                if ta_data:
                    market_context_snapshot = {
                        k: dict(v) for k, v in (ta_data.get("market_context") or {}).items()
                        if isinstance(v, dict)
                    }

                ta_direction_allowed, ta_direction_block_reason = ta_direction_allows_long(ta_data)
                if not ta_direction_allowed:
                    _record_ta_direction_block(
                        sym, ts, idea_ts, state, sig["path"], sig["price"], score, ta_data,
                        ta_direction_block_reason, r.get("name", sym),
                    )
                    log.info(
                        f"[{sym}] ENTRY BLOCKED BY TA DIRECTION: "
                        f"result={ta_data.get('result_label') if ta_data else 'UNAVAILABLE'} "
                        f"reason={ta_direction_block_reason}"
                    )
                    new_ot = None
                    new_tid = None
                else:
                    new_ot = open_trade_record(
                        r, ts, state, sig["path"], score, sig["momentum"], conf,
                        sig["early_val"], sig["early_label"], sig["pattern"], derived, market, idea_ts,
                        existing.get("snapshots", 0) + 1, sig["price"],
                        history_len=sig["history_len"], window=sig["window"], strength=sig["strength"],
                        shadow=sig["shadow"], hist=sig["hist"],
                        ta_direction=ta_direction_snapshot,
                        market_context=market_context_snapshot,
                    )
                    new_ot["trade_id_full"] = new_tid
                    new_ot["opened_ts"] = ts

                    # Canonical Trade Protection calculation (MANDATORY FOR BOTH PAPER & LIVE)
                    try:
                        adaptive_sl_pct, adaptive_tp_levels = compute_adaptive_tp_sl(
                            r,
                            sig["hist"],
                        )
                        adaptive_protection = {
                            "logic_version": PROTECTION_LOGIC_VERSION,
                            "source": "adaptive_volatility",
                            "stop_loss_pct": adaptive_sl_pct,
                            "tp_levels": [dict(x) for x in adaptive_tp_levels],
                        }
                        new_ot["protection"] = adaptive_protection

                        # Fail-closed validation immediately
                        get_trade_protection(new_ot)

                        log.info(
                            f"[{sym}] Adaptive protection calculated and persisted: "
                            f"SL={adaptive_sl_pct:.2f}% "
                            f"TP={adaptive_tp_levels[0]['pnl_pct']:.2f}/"
                            f"{adaptive_tp_levels[1]['pnl_pct']:.2f}/"
                            f"{adaptive_tp_levels[2]['pnl_pct']:.2f}"
                        )
                    except Exception as e:
                        log.exception(
                            f"[{sym}] Adaptive TP/SL calculation failed; "
                            f"entry cancelled: {e}"
                        )
                        new_ot = None
                        new_tid = None

                if ENABLE_BINGX and new_ot is not None:
                    # Sync protection copy to bingx container
                    new_ot.setdefault("bingx", {})["protection"] = dict(new_ot["protection"])
                    adaptive_sl_pct = new_ot["protection"]["stop_loss_pct"]
                    adaptive_tp_levels = new_ot["protection"]["tp_levels"]

                    try:
                        import bingx_client

                        open_result = bingx_client.open_position(sym, sig["price"], trade_id=new_tid)
                        if open_result.get("asset_class"):
                            new_ot["asset_class"] = open_result["asset_class"]
                        if open_result.get("symbol"):
                            new_ot["bingx_symbol"] = open_result["symbol"]
                        open_status = open_result.get("status")
                        if open_status == "foreign_position":
                            new_ot["bingx"] = {
                                "status": "skipped",
                                "reason": "foreign_position",
                                "symbol": open_result.get("symbol"),
                                "protection": dict(new_ot["protection"]),
                            }
                            
                            log.warning(
                                f"[{sym}] BingX SKIP: existing position NOT owned by this trade. "
                                f"Refusing to adopt foreign position."
                            )
                            
                            send_tg(
                                f"⚠️ <b>{esc(r.get('name', sym))} ({esc(sym)})</b>\n"
                                f"Сигнал есть, но на бирже уже есть чужая позиция.\n"
                                f"<i>Вход пропущен для безопасности. Закрой старую позицию вручную.</i>"
                            )
                            
                            new_ot = None
                            new_tid = None

                            existing["bingx_entry_blocked"] = True
                            existing["bingx_entry_block_reason"] = "foreign_position"
                            existing["bingx_entry_block_symbol"] = open_result.get("symbol")
                            existing["bingx_entry_blocked_ts"] = ts
                            bingx_entry_blocked = True

                        elif open_status == "skipped":
                            new_ot["bingx"] = {
                                "status": "skipped",
                                "reason": open_result.get("reason"),
                                "symbol": open_result.get("symbol"),
                                "protection": dict(new_ot["protection"]),
                            }
                            log.info(
                                f"[{sym}] BingX SKIP: "
                                f"reason={open_result.get('reason')} "
                                f"symbol={open_result.get('symbol')}"
                            )
                            reason = open_result.get("reason")
                            
                            if reason == "contract_not_found":
                                existing["bingx_entry_blocked"] = True
                                existing["bingx_entry_block_reason"] = reason
                                existing["bingx_entry_block_symbol"] = open_result.get("symbol")
                                existing["bingx_entry_blocked_ts"] = ts
                                bingx_entry_blocked = True

                            if reason == "contract_not_found":
                                skip_text = (
                                    f"Сигнал есть, но контракт "
                                    f"{esc(str(open_result.get('symbol')))} не найден на BingX."
                                )
                            elif reason == "api_open_disabled":
                                skip_text = (
                                    f"Сигнал есть, но BingX API сейчас временно не разрешает "
                                    f"автоматическое открытие "
                                    f"{esc(str(open_result.get('symbol')))} "
                                    f"(apiStateOpen=false)."
                                )
                            elif reason == "contract_unavailable":
                                skip_text = (
                                    "Сигнал есть, но BingX не удалось получить свежие "
                                    "данные о состоянии контракта. Реальный ордер не отправлялся."
                                )
                            else:
                                skip_text = (
                                    f"Сигнал есть, но биржевой вход пропущен: "
                                    f"{esc(str(reason or 'unknown'))}."
                                )

                            notify_skip = should_notify_bingx_skip(existing, reason, ts)

                            if notify_skip:
                                tg_ok = send_tg(
                                    f"⚠️ <b>{esc(r.get('name', sym))} ({esc(sym)})</b>\n"
                                    f"{skip_text}\n"
                                    f"<i>Реальный ордер не отправлялся. "
                                    f"Состояние BingX будет перепроверено в следующем прогоне.</i>"
                                )

                                if tg_ok:
                                    mark_bingx_skip_notified(existing, reason, ts)
                                    log.info(
                                        f"[{sym}] BingX skip Telegram sent: "
                                        f"reason={reason}"
                                    )
                                else:
                                    log.warning(
                                        f"[{sym}] BingX skip Telegram NOT delivered: "
                                        f"reason={reason}; retry allowed on next run"
                                    )
                            else:
                                log.info(
                                    f"[{sym}] BingX skip Telegram suppressed: "
                                    f"reason={reason}; "
                                    f"last notification still within "
                                    f"{BINGX_SKIP_NOTIFY_COOLDOWN_SEC // 3600}h cooldown"
                                )

                            new_ot = None
                            new_tid = None
                        elif open_status == "error":
                            new_ot["bingx"] = {
                                "status": "error",
                                "error": open_result.get("error"),
                                "protection": dict(new_ot["protection"]),
                            }
                            log.error(
                                f"[{sym}] BingX OPEN error: {open_result.get('error')}"
                            )
                            send_tg(
                                f"🚨 <b>{esc(r.get('name', sym))} ({esc(sym)})</b>\n"
                                f"Сигнал есть, но открытие на бирже ЗАВЕРШИЛОСЬ ОШИБКОЙ:\n"
                                f"<code>{esc(str(open_result.get('error'))[:200])}</code>\n"
                                f"<i>Позиция открыта только как research-идея, без реального ордера.</i>"
                            )
                            new_ot = None
                            new_tid = None
                        elif open_status == "open_no_tp":
                            bx = dict(open_result.get("open", {}))
                            bx["qty_initial"] = open_result.get("qty_initial")
                            bx["qty_remaining"] = open_result.get("qty_remaining")
                            bx["partial_legs_done"] = []
                            bx["tp_orders"] = []
                            bx["execution_status"] = "OPEN_NO_TP"
                            bx["protection"] = dict(new_ot["protection"])
                            if open_result.get("qty_initial_uncertain"):
                                bx["qty_initial_uncertain"] = True
                            new_ot["bingx"] = bx
                            existing.pop("bingx_skip_notify_reason", None)
                            existing.pop("bingx_skip_notify_ts", None)
                        elif open_status == "found":
                            bx = dict(open_result.get("open", {}))
                            avg_price = open_result.get("avg_price")
                            position_qty = open_result.get("qty_initial")
                            bx["bingx_avg_price"] = avg_price
                            bx["qty_initial"] = position_qty
                            bx["qty_remaining"] = position_qty
                            bx["partial_legs_done"] = []
                            bx["execution_status"] = "OPEN"
                            bx["protection"] = dict(new_ot["protection"])
                            new_ot["bingx"] = bx
                            entry_temp = dict(existing)
                            entry_temp["open_trade"] = new_ot
                            entry_temp["trade_id"] = new_tid
                            entry_temp.pop("bingx_skip_notify_reason", None)
                            entry_temp.pop("bingx_skip_notify_ts", None)
                            wl_all[sym] = entry_temp
                            save_watchlist(wl_all)
                            log.info(
                                f"[{sym}] Watchlist saved after OPEN (crash-safe point 1)"
                            )
                            # Persist the open position before touching protection.
                            # Protection is reconciled against the exchange state, so a
                            # crash/restart cannot permanently suppress TP/SL recovery.
                            bx["protection_state"] = "PENDING"
                            bx["tp_status"] = "TP_UNKNOWN"
                            bx["sl_status"] = "SL_UNKNOWN"
                            bx["protection"] = dict(new_ot["protection"])
                            new_ot["bingx"] = bx
                            entry_temp["open_trade"] = new_ot
                            wl_all[sym] = entry_temp
                            save_watchlist(wl_all)
                            log.info(f"[{sym}] OPEN state saved before protection reconciliation")

                            protection = bingx_client.attach_protection(
                                sym,
                                avg_price,
                                position_qty,
                                adaptive_tp_levels,
                                adaptive_sl_pct,
                                trade_id=new_tid,
                            )
                            bx["tp_orders"] = protection.get("tp_orders", [])
                            bx["tp_status"] = (
                                "TP_PLACED"
                                if protection.get("tp_status") == "TP_PLACED"
                                else "TP_FAILED"
                            )
                            sl_result = protection.get("sl_result") or {}
                            if sl_result.get("status") == "created":
                                bx["sl_order"] = sl_result
                                bx["sl_status"] = "SL_PLACED"
                            else:
                                bx["sl_order"] = None
                                bx["sl_status"] = "SL_FAILED"
                            bx["protection_state"] = (
                                "PROTECTED"
                                if bx["tp_status"] == "TP_PLACED" and bx["sl_status"] == "SL_PLACED"
                                else "DEGRADED"
                            )
                            bx["execution_status"] = (
                                "PROTECTED"
                                if bx["protection_state"] == "PROTECTED"
                                else "PROTECTION_DEGRADED"
                            )
                            new_ot["bingx"] = bx

                            if protection["tp_status"] == "TP_PLACED":
                                log.info(
                                    f"[{sym}] BingX OPEN qty={position_qty} avgPrice={avg_price:.6f} "
                                    f"TP: {len(bx['tp_orders'])} уровней"
                                )
                            else:
                                log.error(
                                    f"[{sym}] TP не созданы: {protection['tp_result'].get('error')}"
                                )
                                send_tg(
                                    f"⚠️ <b>{esc(r.get('name', sym))} ({esc(sym)})</b>\n"
                                    f"Позиция открыта, но TP не созданы!\n"
                                    f"Error: {esc(str(protection['tp_result'].get('error'))[:200])}\n"
                                    f"<i>Позиция остается открытой. Retry в следующем прогоне.</i>"
                                )
                            if sl_result.get("status") == "created":
                                log.info(
                                    f"[{sym}] SL установлен на бирже: "
                                    f"stop={sl_result.get('stop_price'):.6f}"
                                )
                            else:
                                log.error(
                                    f"[{sym}] SL НЕ установлен: {sl_result.get('error')}"
                                )
                                send_tg(
                                    f"🚨 <b>{esc(r.get('name', sym))} ({esc(sym)})</b>\n"
                                    f"TP установлены, но STOP_LOSS на бирже НЕ создан!\n"
                                    f"Error: {esc(str(sl_result.get('error'))[:200])}\n"
                                    f"<i>Защита только программная до успешного восстановления "
                                    f"exchange SL. Риск гэпа выше уровня adaptive SL.</i>"
                                )
                            new_ot["bingx"] = bx

                    except Exception as e:
                        log.error(f"[{sym}] BingX OPEN exception: {e}")
                        bx = new_ot.setdefault("bingx", {})
                        bx["execution_status"] = "PROTECTION_EXCEPTION"
                        bx["protection_state"] = "DEGRADED"
                        bx["tp_status"] = bx.get("tp_status", "TP_UNKNOWN")
                        bx["sl_status"] = bx.get("sl_status", "SL_UNKNOWN")
                        bx["protection_error"] = str(e)[:500]
                        bx["protection"] = dict(new_ot["protection"])

                        try:
                            entry_temp = dict(existing)
                            entry_temp["open_trade"] = new_ot
                            entry_temp["trade_id"] = new_tid
                            wl_all[sym] = entry_temp
                            save_watchlist(wl_all)

                            log.info(
                                f"[{sym}] BingX exception state сохранён без потери "
                                f"данных открытой позиции"
                            )
                        except Exception as save_exc:
                            log.critical(
                                f"[{sym}] НЕ УДАЛОСЬ СОХРАНИТЬ состояние после BingX exception: "
                                f"{save_exc}"
                            )

                        send_tg(
                            f"🚨 <b>{esc(r.get('name', sym))} ({esc(sym)})</b>\n"
                            f"BingX: ошибка после открытия/при установке защиты.\n"
                            f"<code>{esc(str(e)[:300])}</code>\n"
                            f"<i>Состояние позиции НЕ сброшено. "
                            f"Требуется повторная проверка защиты.</i>"
                        )

                if new_ot is not None:
                    log.info(
                        f"[{sym}] TRADE OPEN {state} path={sig['path']} @ {sig['price']} "
                        f"strength={sig['strength']} window={sig['window']['span_min']}м"
                    )
                else:
                    log.info(
                        f"[{sym}] SIGNAL CONFIRMED {state} path={sig['path']} @ {sig['price']} "
                        f"strength={sig['strength']} window={sig['window']['span_min']}м "
                        f"execution=NOT_OPENED"
                    ) 
                rec = lifecycle_state.setdefault(sym, {})
                rec.setdefault("idea_first_seen_ts", ts)
                entry = {
                    "state": state,
                    "previous_state": prev_state,
                    "action": ACTIONS.get(state, ""),
                    "confidence": conf,
                    "score": score,
                    "momentum": sig["momentum"],
                    "pattern": sig["pattern"],
                    "name": r.get("name", sym),
                    "first_seen": existing.get("first_seen", ts),
                    "last_seen": ts,
                    "snapshots": existing.get("snapshots", 0) + 1,
                    "missed_runs": 0,
                    "neutral_runs": 0,
                    "entry_earliness": round(sig["early_val"], 2),
                    "signal_strength": sig["strength"],
                    "window": sig["window"],
                    "reasons": sig["reasons"],
                    "warnings": sig["warnings"],
                    "mom_tags": sig["mom_tags"],
                    "bingx_entry_blocked": existing.get("bingx_entry_blocked", False),
                    "bingx_entry_block_reason": existing.get("bingx_entry_block_reason"),
                    "bingx_entry_block_symbol": existing.get("bingx_entry_block_symbol"),
                    "bingx_entry_blocked_ts": existing.get("bingx_entry_blocked_ts"),
                    "bingx_skip_notify_reason": existing.get("bingx_skip_notify_reason"),
                    "bingx_skip_notify_ts": existing.get("bingx_skip_notify_ts"),
                }
                if new_ot is not None:
                    entry["open_trade"] = new_ot
                    entry["trade_id"] = new_tid
                wl_all[sym] = entry
                if new_ot is not None:
                    save_watchlist(wl_all)
        if state != prev_state:
            log.info(f"[{sym}] {prev_state} → {state} | {sig['reasons']}")
            if state in TG_STATES:
                append_jsonl(
                    CALIBRATION_FILE,
                    {
                        "ts": ts,
                        "symbol": sym,
                        "state": state,
                        "oi_accel": round(derived["oi_accel"], 3),
                        "cvd_momentum": round(derived["cvd_momentum"], 2),
                        "price_accel": round(derived["price_accel"], 3),
                        "funding_pressure": round(derived["funding_pressure"], 5),
                        "oi_trend": derived["oi_trend"],
                        "cvd_trend": derived["cvd_trend"],
                        "score": score,
                        "momentum": sig["momentum"],
                        "confidence": conf,
                        "signal_strength": sig["strength"],
                        "window_span_min": sig["window"]["span_min"],
                        "window_dense": sig["window"]["dense"],
                        "signal_logic_version": SIGNAL_LOGIC_VERSION,
                        "lifecycle_engine_version": LIFECYCLE_ENGINE_VERSION,
                    },
                )
                cur = {
                    **r,
                    "score": score,
                    "pros": sig["pros"],
                    "cons": sig["cons"],
                    "derived": derived,
                    "momentum": sig["momentum"],
                    "pattern": sig["pattern"],
                    "entry_short_liq_share24": calc_entry_short_liq_share24(
                        r.get("liq_short24"), r.get("liq_long24")
                    ),
                    "entry_liq_imbalance": calc_entry_liq_imbalance(
                        r.get("liq_short24"), r.get("liq_long24")
                    ),
                    "entry_funding_oi_pressure": calc_entry_funding_oi_pressure(
                        r.get("fr_oiw"), r.get("oi_chg4h_pct")
                    ),
                    "entry_liquidation_intensity": calc_entry_liquidation_intensity(
                        r.get("liq_short24"), r.get("liq_long24"), r.get("oi")
                    ),
                    "entry_fr_oiw_zscore": calc_entry_fr_oiw_zscore_from_hist(
                        hist[:-1], r.get("fr_oiw")
                    ),
                }
                signal_wl = wl_all.get(sym)
                if signal_wl is None:
                    signal_wl = {
                        "state": state,
                        "previous_state": prev_state,
                        "action": ACTIONS.get(state, ""),
                        "confidence": conf,
                        "score": score,
                        "momentum": sig["momentum"],
                        "pattern": sig["pattern"],
                        "name": r.get("name", sym),
                        "first_seen": ts,
                        "last_seen": ts,
                        "snapshots": sig["history_len"],
                        "missed_runs": 0,
                        "neutral_runs": 0,
                        "entry_earliness": round(sig["early_val"], 2),
                        "signal_strength": sig["strength"],
                        "window": sig["window"],
                        "reasons": sig["reasons"],
                        "warnings": sig["warnings"],
                        "mom_tags": sig["mom_tags"],
                    }

                should_send_tg = True
                if state in ENTRY_STATES:
                    if new_ot is None:
                        log.info(
                            f"[{sym}] ENTRY Telegram suppressed: "
                            f"новая сделка в текущем проходе не открыта "
                            f"(Quality Guard / Cooldown / Exchange block / Protection failure)"
                        )
                        should_send_tg = False
                    elif has_trade:
                        log.info(
                            f"[{sym}] ENTRY Telegram suppressed: "
                            f"open_trade already existed, "
                            f"trade_id={existing.get('trade_id')}"
                        )
                        should_send_tg = False

                if should_send_tg:
                    llm_res = llm_verify(sym, signal_wl, cur, hist)
                    if ta_data is None:
                        try:
                            ta_bx_symbol = bingx_client.to_bx_symbol(sym)
                            ta_data = ta_context.get_ta_context(ta_bx_symbol)
                        except Exception as e:
                            log.warning(f"[{sym}] TA context failed: {e}")
                    msg = format_signal(sym, signal_wl, cur, hist, sig["reasons"], sig["warnings"], market, ta_context_data=ta_data)
                    if llm_res:
                        agree = "✅" if llm_res.get("agree") is True else "❌"
                        msg += f"\n🤖 {agree} {esc(llm_res.get('risk','?'))} · {esc(llm_res.get('reason',''))}"
                    send_tg(msg)
                   
    if LAST_SCRAPE_COMPLETE:
        for sym in list(wl_all.keys()):
            entry = wl_all[sym]
            if sym in current_symbols:
                continue
            if entry.get("open_trade"):
                continue
            if entry.get("missed_runs", 0) >= MISS_REMOVE_RUNS:
                log.info(
                    f"[{sym}] нет в данных {entry['missed_runs']} прогонов → remove"
                )
                del wl_all[sym]
    else:
        log.warning(
            "Scrape incomplete: удаление watchlist entries по отсутствию в universe "
            "пропущено в этом прогоне"
        )
    flush_pending(price_full, ts, existing_trade_ids)
    save_pending(PENDING)
    save_watchlist(wl_all)
    save_lifecycle_state(lifecycle_state)
    for r in rows:
        if passes_filter(r):
            sc, pr, co = calculate_score(r)
            append_jsonl(SNAPSHOTS_FILE, {**r, "score": sc, "pros": pr, "cons": co})
    cleanup_jsonl(MARKET_HISTORY_FILE, MARKET_TTL_DAYS)
    cleanup_jsonl(SNAPSHOTS_FILE, SNAPSHOTS_TTL_DAYS)
    cleanup_jsonl(HEARTBEAT_FILE, HEARTBEAT_TTL_DAYS)
    cleanup_jsonl(CALIBRATION_FILE, SNAPSHOTS_TTL_DAYS)
    cleanup_jsonl(SHADOW_SIGNALS_FILE, SHADOW_SIGNALS_TTL_DAYS)
    open_n = sum(1 for v in wl_all.values() if v.get("open_trade"))
    log.info(
        f"═══ Готово. Active: {len(wl_all)} · open trades: {open_n} · pending: {len(PENDING)} ═══"
    )


def flush_pending(price_full, now, existing_trade_ids):
    global PENDING
    grace = PENDING_GRACE_MIN * 60
    wait_max = (max(TRADE_HORIZONS) + PENDING_WAIT_MAX_MIN) * 60
    still = []
    for item in PENDING:
        rec = item["rec"]
        sym, ep, ets = item["symbol"], item["entry_price"], item["entry_ts"]
        tid = rec.get("trade_id")
        if tid and tid in existing_trade_ids:
            log.warning(f"[{sym}] DUP_FINALIZED_SKIP trade_id={tid}")
            continue
        for h in TRADE_HORIZONS:
            key = f"return_{h}m"
            if rec.get(key) is None and now >= ets + h * 60:
                ph = price_at(
                    price_full, sym, ets + h * 60, max_lag_sec=HORIZON_MAX_LAG_MIN * 60
                )
                if ph and ep:
                    rec[key] = round((ph - ep) / ep * 100, 3)
                    rec[f"{key}_available"] = True
        for h in (60, 120):
            v = rec.get(f"return_{h}m")
            rec[f"win_{h}m"] = (
                1
                if (v is not None and v >= TRADE_WIN_PCT)
                else (0 if v is not None else None)
            )
        ready = (
            all(
                rec.get(f"return_{h}m") is not None or now >= ets + h * 60 + grace
                for h in TRADE_HORIZONS
            )
            or now >= ets + wait_max
        )
        if ready:
            all_complete = all(
                rec.get(f"return_{h}m") is not None for h in TRADE_HORIZONS
            )
            if all_complete:
                rec["pending_finalize_reason"] = "COMPLETE"
            elif now >= ets + wait_max:
                rec["pending_finalize_reason"] = "WAIT_TIMEOUT"
            else:
                rec["pending_finalize_reason"] = "MISSING_PRICE"
            append_jsonl(TRADES_FILE, rec)
            if tid:
                existing_trade_ids.add(tid)
            log.info(
                f"[{sym}] TRADE FINALIZED trade_id={rec['trade_id']} reason={rec['pending_finalize_reason']}"
            )
        else:
            still.append(item)
    PENDING = still


TG_STATES = {"CONFIRMED_TREND", "ACCELERATION", "EXHAUSTION", "DISTRIBUTION"}
ACTIVE_STATES = {"CONFIRMED_TREND", "ACCELERATION", "EXHAUSTION", "DISTRIBUTION"}
ENTRY_STATES = {"CONFIRMED_TREND", "ACCELERATION"}
CLOSE_STATES = {"EXHAUSTION", "DISTRIBUTION"}

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log.exception(f"Фатал: {e}")
        send_tg(f"⚠️ <b>Monitor</b>\n{esc(str(e)[:500])}")
        sys.exit(1)
