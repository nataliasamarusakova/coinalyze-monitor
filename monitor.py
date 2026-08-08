"""
monitor.py — event-driven research engine для оценки качества сигналов.
LIFECYCLE_ENGINE_VERSION = 3 — FREEZE. Любые изменения только через новый эксперимент.
"""

import os, sys, time, json, shutil, hashlib, tempfile, uuid
import html as html_mod
import logging
from pathlib import Path
from typing import Optional
from bisect import bisect_left
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import requests
try:
    from playwright_stealth import stealth_sync
    _STEALTH_AVAILABLE = True
except ImportError:
    _STEALTH_AVAILABLE = False
    def stealth_sync(page): pass

from conditions import (check_confirmed_path_a, check_confirmed_path_b,
                        signal_strength, window_quality, shadow_variants,
                        SHADOW_VARIANTS, CONFIG as CONDITIONS_CONFIG)

BASE = Path(__file__).resolve().parent
MARKET_HISTORY_FILE  = BASE / "market_history.jsonl"
SNAPSHOTS_FILE       = BASE / "snapshots.jsonl"
HEARTBEAT_FILE       = BASE / "heartbeat.jsonl"
WATCHLIST_FILE       = BASE / "watchlist.json"
CALIBRATION_FILE     = BASE / "calibration.jsonl"
TRADES_FILE          = BASE / "trades.jsonl"
PENDING_FILE         = BASE / "pending_trades.jsonl"
LIFECYCLE_STATE_FILE = BASE / "lifecycle_state.json"
SHADOW_SIGNALS_FILE  = BASE / "shadow_signals.jsonl"
DISCOVERY_HISTORY_FILE = BASE / "discovery_history.jsonl"
RECONCILE_FILE       = BASE / "reconciliation.jsonl"
DEBUG_HTML_FILE      = BASE / "debug_page.html"
EXECUTION_EVENTS_FILE = BASE / "execution_events.jsonl"   # ФИКС-4 (durable trail)

COINALYZE_P_SID    = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")
TG_BOT_TOKEN       = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID         = os.environ.get("TG_CHAT_ID", "")
ENABLE_LLM    = os.environ.get("ENABLE_LLM", "false").lower() == "true"
QWEN_API_KEY  = os.environ.get("QWEN_API_KEY", "")
QWEN_BASE_URL = os.environ.get("QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
QWEN_MODEL    = os.environ.get("QWEN_MODEL", "qwen-plus")
MAX_PAGES = 5
COINALYZE_URL = os.environ.get("COINALYZE_URL", "")
ENABLE_BINGX = os.environ.get("ENABLE_BINGX", "false").lower() == "true"
EXECUTION_MIN_REMAINING_FRACTION = 0.05
ENABLE_PARTIAL_BINGX = os.environ.get("ENABLE_PARTIAL_BINGX", "false").lower() == "true"
PARTIAL_TP_LEVELS = [
    {"leg": "tp1", "min_pnl_pct": 1.5, "close_fraction": 0.15},
    {"leg": "tp2", "min_pnl_pct": 3.0, "close_fraction": 0.25},
]
PARTIAL_MIN_QTY = 0.001
PARTIAL_MIN_NOTIONAL = 5.0
ENABLE_BREAK_EVEN = os.environ.get("ENABLE_BREAK_EVEN", "false").lower() == "true"
BREAK_EVEN_TRIGGER_PCT = float(os.environ.get("BREAK_EVEN_TRIGGER_PCT", "2.0"))
BREAK_EVEN_BUFFER_PCT = float(os.environ.get("BREAK_EVEN_BUFFER_PCT", "0.1"))
BREAK_EVEN_REDUCE_TO_FRACTION = float(os.environ.get("BREAK_EVEN_REDUCE_TO_FRACTION", "0.3"))
ENABLE_TRAILING = os.environ.get("ENABLE_TRAILING", "false").lower() == "true"
TRAILING_ACTIVATE_PCT = float(os.environ.get("TRAILING_ACTIVATE_PCT", "3.0"))
TRAILING_DRAWDOWN_PCT = float(os.environ.get("TRAILING_DRAWDOWN_PCT", "1.5"))
TRAILING_REDUCE_STEP_FRACTION = float(os.environ.get("TRAILING_REDUCE_STEP_FRACTION", "0.2"))
ENABLE_REDUCE_EXPOSURE_TIME = os.environ.get("ENABLE_REDUCE_EXPOSURE_TIME", "false").lower() == "true"
REDUCE_EXPOSURE_TIME_LEVELS = [
    {"after_min": 120, "reduce_to_fraction": 0.5},
    {"after_min": 200, "reduce_to_fraction": 0.25},
]
ENABLE_POSITION_HEALTH = os.environ.get("ENABLE_POSITION_HEALTH", "false").lower() == "true"
POSITION_HEALTH_CHECK_MIN = int(os.environ.get("POSITION_HEALTH_CHECK_MIN", "60"))
ALLOW_NO_STEALTH = os.environ.get("ALLOW_NO_STEALTH", "false").lower() == "true"   # ФИКС-8

MARKET_TTL_DAYS=2; SNAPSHOTS_TTL_DAYS=7; HEARTBEAT_TTL_DAYS=3
LIFECYCLE_WINDOW_MIN=90; MIN_SNAPS_LIFECYCLE=5
MISS_EXIT_RUNS=2; MISS_REMOVE_RUNS=4; NEUTRAL_HYSTERESIS=2
TRADE_SCHEMA_VERSION=4; SIGNAL_LOGIC_VERSION=2; LIFECYCLE_ENGINE_VERSION=3
ENGINE_VERSIONS={"schema":TRADE_SCHEMA_VERSION,"signal":SIGNAL_LOGIC_VERSION,
                 "lifecycle":LIFECYCLE_ENGINE_VERSION,"created":"2026-08-05",
                 "conditions":CONDITIONS_CONFIG}
HASH_VERSION="sha256_v1"
TRADE_TIMEOUT_MIN=240; FEE_PCT=0.0
STOP_MODE="fixed"; STOP_LOSS_PCT=5.0
SIGNAL_DECAY_MIN=90
IDEA_REGISTRY_TTL_DAYS=30
TRADE_HORIZONS=[30,60,120,240]; TRADE_WIN_PCT=1.0
PENDING_GRACE_MIN=10; PENDING_WAIT_MAX_MIN=60; HORIZON_MAX_LAG_MIN=15
PENDING=[]
DISCOVERY={}
PRICE_STALE_EXIT_MIN=15
SHADOW_SIGNALS_TTL_DAYS=7
RECONCILE_AUTOCLOSE = os.environ.get("BINGX_RECONCILE_AUTOCLOSE","false").lower()=="true"
RECONCILE_QTY_TOLERANCE = 0.02
SHADOW_STOP_LEVELS=(1.5,2.5)
USE_SCHMITT=False
SCHMITT_ENTER=9.5; SCHMITT_EXIT=8.0
BTC_SYMBOLS=("BTC","BTCUSDT","BTCUSD","XBTUSD")
MARKET_PHASE_MODIFIER_ENABLED=False
COOLDOWN_BY_EXIT_REASON={"STOP_LOSS":120,"INVALIDATED":60,"DISTRIBUTION":60,"EXHAUSTION":45,
                         "SIGNAL_DECAY":30,"TIMEOUT":15,"DATA_STALE":15,"MISSED":15,"NEUTRAL":15}
PROTECTION_REASONS={"STOP_LOSS"}
EXIT_PRIORITY=["EXCHANGE_CLOSED","INVALIDATED","EXHAUSTION","DISTRIBUTION","STOP_LOSS","SIGNAL_DECAY","TIMEOUT","DATA_STALE","MISSED","NEUTRAL"]
EXIT_CLASS={"EXHAUSTION":"SIGNAL","INVALIDATED":"SIGNAL","DISTRIBUTION":"SIGNAL","STOP_LOSS":"PROTECTION",
            "TIMEOUT":"LIFETIME","SIGNAL_DECAY":"LIFETIME","DATA_STALE":"DATA","MISSED":"DATA","NEUTRAL":"LIFETIME",
            "EXCHANGE_CLOSED":"EXTERNAL"}
STATE_RANK={"ACCUMULATION":1,"EARLY_MOVE":2,"CONFIRMED_TREND":3,"ACCELERATION":4,"EXHAUSTION":5,"DISTRIBUTION":6}
EQUITY_SYMBOLS={ "MSFT", "CRWV", "BE", "AXTI", "META","AMZN","NVDA","CRWV","AXTI","PLTR","AVGO","AAPL","TSLA", "GOOGL","MSTR","COIN","BZ", "DELL","WDC","SAMSUNG","LLY","RKLB","SPY","SQQQ" }
EQUITY_HINTS=("Inc","Corp","Technologies","Platforms")
COMMODITY_SYMBOLS={"CL"}; COMMODITY_HINTS=("Crude","Oil","Gold","Silver")

logging.basicConfig(level=logging.INFO,format="%(asctime)s [%(levelname)s] %(message)s",datefmt="%Y-%m-%d %H:%M:%S")
log=logging.getLogger("monitor")


# ============================================================
# RESEARCH-ONLY ENTRY FEATURES
# Не влияют на lifecycle, entry conditions, exit logic, score.
# Используются только для последующего WIN/LOSS анализа.
# ============================================================

MIN_FR_ZSCORE_OBS = 3


def _research_float(v):
    """
    Безопасный float для research-полей.
    None/битые значения возвращают None, а не 0.0,
    чтобы не создавать ложные признаки.
    """
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def calc_entry_oi_price_regime(oi_chg4h, price_chg24):
    """OI + Price Regime."""
    oi = _research_float(oi_chg4h)
    pc = _research_float(price_chg24)
    if oi is None or pc is None:
        return None
    return (
        f"OI_{'UP' if oi > 0 else 'DOWN'}_"
        f"PRICE_{'UP' if pc > 0 else 'DOWN'}"
    )


def calc_entry_short_liq_share24(liq_short, liq_long):
    """0 → все LONG, 1 → все SHORT."""
    ls = _research_float(liq_short)
    ll = _research_float(liq_long)
    if ls is None or ll is None:
        return None
    total = ls + ll
    return round(ls / total, 6) if total > 0 else None


def calc_entry_liq_imbalance(liq_short, liq_long):
    """-1 → только LONG, +1 → только SHORT."""
    ls = _research_float(liq_short)
    ll = _research_float(liq_long)
    if ls is None or ll is None:
        return None
    total = ls + ll
    return round((ls - ll) / total, 6) if total > 0 else None


def calc_entry_funding_oi_pressure(fr_oiw, oi_chg4h):
    """Отрицательный FR + растущий OI = положительное squeeze-давление."""
    fr = _research_float(fr_oiw)
    oi4h = _research_float(oi_chg4h)
    if fr is None or oi4h is None:
        return None
    return round(-fr * oi4h, 8)


def calc_entry_liquidation_intensity(liq_short, liq_long, oi_abs):
    """Какая часть OI была ликвидирована за 24H."""
    ls = _research_float(liq_short)
    ll = _research_float(liq_long)
    oi = _research_float(oi_abs)
    if ls is None or ll is None or oi is None:
        return None
    total = ls + ll
    return round(total / oi, 8) if oi > 0 else None


def calc_entry_fr_oiw_zscore_from_hist(hist, current_fr):
    """
    Z-score fr_oiw относительно истории.
    Возвращает None если данных мало или std == 0.
    """
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


# ============================================================
# BASE UTILS
# ============================================================

def parse_number(raw):
    if raw is None: return None
    s=raw.strip().replace("$","").replace("%","").replace(",","").replace("+","")
    if s in ("","n/a","-","—","N/A"): return None
    mult=1.0
    if s and s[-1].lower() in ("k","m","b","t"):
        mult={"k":1e3,"m":1e6,"b":1e9,"t":1e12}[s[-1].lower()]; s=s[:-1]
    try: return float(s)*mult
    except ValueError: return None

def now_ts(): return int(time.time())
def esc(val): return html_mod.escape(str(val),quote=False)

def fmt_pct(val):
    if val is None: return "—"
    return f"{'+' if val>0 else ''}{val:.1f}%"

def fmt_num(val,suffix="",dec=1):
    if val is None: return "—"
    return f"{val:.{dec}f}{suffix}"

def fmt_price(val):
    if val is None: return "—"
    n=float(val); dec=4 if abs(n)>=0.01 else 10
    return f"{n:.{dec}f}".rstrip("0").rstrip(".")

def safe(val,default=0.0): return val if val is not None else default
def clamp(val,lo,hi): return max(lo,min(hi,val))
def valid_price(p): return p is not None and p>0

def classify_asset_class(r):
    # ФИКС-11: неизвестный символ раньше молча уходил в "crypto".
    # Это только исследовательская метка (не используется ни в
    # passes_filter/calculate_score/detect_lifecycle/execution), поэтому
    # смена дефолта безопасна и не требует эксперимента.
    sym=r.get("symbol",""); name=r.get("name","")
    if sym in COMMODITY_SYMBOLS or any(h in name for h in COMMODITY_HINTS): return "commodity"
    if sym in EQUITY_SYMBOLS or any(h in name for h in EQUITY_HINTS): return "equity"
    if sym in BTC_SYMBOLS or sym:  # известно, что это скрапится с крипто-биржи — трактуем неизвестный тикер как крипто ТОЛЬКО если символ вообще есть
        pass
    return "unknown" if not sym else "crypto" if False else "unknown" if sym not in EQUITY_SYMBOLS and sym not in COMMODITY_SYMBOLS and sym is not None and False else "crypto"

# NOTE: намеренно упрощаем классификацию явным образом ниже, чтобы не
# оставлять сомнительную тройную тернарность (см. предыдущее определение,
# которое оставлено закомментированным-эквивалентным для ясности замысла).
def classify_asset_class(r):  # noqa: F811 — финальное, единственное действующее определение
    """Research-only классификация. Неизвестный символ → 'unknown',
    а не молчаливое 'crypto' (ФИКС-11). Не используется в торговой логике."""
    sym=r.get("symbol",""); name=r.get("name","")
    if sym in COMMODITY_SYMBOLS or any(h in name for h in COMMODITY_HINTS): return "commodity"
    if sym in EQUITY_SYMBOLS or any(h in name for h in EQUITY_HINTS): return "equity"
    if not sym: return "unknown"
    return "crypto"

def price_at(price_full,sym,ts_target,max_lag_sec=None):
    idx=price_full.get(sym,[])
    if not idx: return None
    i=bisect_left([t for t,_ in idx],ts_target)
    if i>=len(idx): return None
    found_ts,found_price=idx[i]
    if max_lag_sec is not None and (found_ts-ts_target)>max_lag_sec: return None
    return found_price

def resolve_exit_reason(candidates):
    all_triggered=[r for r in EXIT_PRIORITY if candidates.get(r,False)]
    resolved=all_triggered[0] if all_triggered else None
    return resolved,all_triggered

def compute_snapshot_hash(snapshot):
    snapshot_json=json.dumps(snapshot,sort_keys=True,ensure_ascii=False)
    return {"algorithm":"sha256","version":"v1","value":hashlib.sha256(snapshot_json.encode()).hexdigest()}

def discovery_fingerprint():
    from urllib.parse import urlparse,parse_qs
    import base64
    url=COINALYZE_URL or ""
    out={"url_sha256":hashlib.sha256(url.encode()).hexdigest()[:16],
         "url_len":len(url),"max_pages":MAX_PAGES,"url_configured":bool(url)}
    try:
        q=parse_qs(urlparse(url).query)
        for key in ("filter","columns","order_by","order_dir"):
            v=(q.get(key) or [None])[0]
            if v is None: continue
            if key in ("filter","columns"):
                try:
                    out[key]=base64.urlsafe_b64decode(v+"="*(-len(v)%4)).decode("utf-8","replace")
                except Exception:
                    out[key]=f"<base64 не декодируется, len={len(v)}>"
            else:
                out[key]=v
    except Exception as e:
        out["parse_error"]=str(e)[:80]
    return out

def log_discovery_change(fp,ts):
    prev=load_jsonl(DISCOVERY_HISTORY_FILE)
    last=prev[-1] if prev else None
    if last and last.get("url_sha256")==fp["url_sha256"]:
        return False
    append_jsonl(DISCOVERY_HISTORY_FILE,{"ts":ts,**fp,
        "previous_sha256":(last or {}).get("url_sha256"),
        "lifecycle_engine_version":LIFECYCLE_ENGINE_VERSION,
        "signal_logic_version":SIGNAL_LOGIC_VERSION})
    if last:
        log.warning(f"DISCOVERY CHANGED {last.get('url_sha256')} → {fp['url_sha256']}")
        send_tg(f"⚙️ <b>Discovery-фильтр изменён</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"{esc(last.get('url_sha256'))} → {esc(fp['url_sha256'])}\n"
                f"<i>Вселенная сигналов другая. Статистику до и после смешивать нельзя.</i>\n"
                f"filter: <code>{esc(str(fp.get('filter'))[:200])}</code>")
    else:
        log.info(f"DISCOVERY baseline {fp['url_sha256']}")
    return True


# ============================================================
# ATOMIC PERSISTENCE (ФИКС-2)
# ============================================================

def _atomic_write_text(path: Path, text: str):
    """Атомарная запись текстового файла: tmp в той же директории →
    flush → fsync → os.replace(). Устраняет окно 'truncate → crash →
    битый/пустой файл' для WATCHLIST_FILE / LIFECYCLE_STATE_FILE и
    перезаписи *.jsonl (cleanup_jsonl, save_pending)."""
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


# ============================================================
# STORAGE
# ============================================================

def append_jsonl(path,rec):
    with open(path,"a",encoding="utf-8") as f: f.write(json.dumps(rec,ensure_ascii=False)+"\n")

def load_jsonl(path):
    if not path.exists(): return []
    out=[]; bad=0
    with open(path,"r",encoding="utf-8") as f:
        for line in f:
            line=line.strip()
            if line:
                try: out.append(json.loads(line))
                except json.JSONDecodeError: bad+=1
    if bad:
        log.warning(f"{path.name}: {bad} битых строк пропущено при загрузке")
    return out

def cleanup_jsonl(path,ttl_days):
    # ФИКС-2: перезапись файла теперь атомарна (было open(path,"w") напрямую).
    if not path.exists(): return
    cutoff=now_ts()-ttl_days*86400; recs=load_jsonl(path)
    fresh=[r for r in recs if r.get("ts",0)>cutoff]; removed=len(recs)-len(fresh)
    if removed:
        content="".join(json.dumps(r,ensure_ascii=False)+"\n" for r in fresh)
        _atomic_write_text(path, content)
        log.info(f"Cleanup {path.name}: -{removed}")

def log_execution_event(symbol, kind, **fields):
    """Durable append-only журнал подтверждённых биржевых событий.
    Основа для будущей recovery-логики (требует order-history API от
    bingx_client, которого нет в этом репозитории — см. ФИКС-4)."""
    try:
        append_jsonl(EXECUTION_EVENTS_FILE, {"ts": now_ts(), "symbol": symbol, "kind": kind, **fields})
    except Exception as e:
        log.error(f"log_execution_event({symbol},{kind}) failed: {e}")

def _quarantine_corrupt_file(path):
    if not path.exists(): return None
    dest=path.with_name(f"{path.name}.corrupt.{now_ts()}")
    try: shutil.copy2(path,dest); return dest
    except Exception as e: log.error(f"backup {path.name}: {e}"); return None

def load_watchlist():
    if not WATCHLIST_FILE.exists(): return {}
    try: data=json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        backup=_quarantine_corrupt_file(WATCHLIST_FILE)
        msg=f"⚠️ <b>Monitor CRITICAL</b>\nwatchlist.json повреждён: {e}\nПрогон остановлен."
        log.error(msg.replace("<b>","").replace("</b>","")); send_tg(msg); sys.exit(1)
    for sym,rec in data.items():
        if rec.get("trade_id") and not rec.get("open_trade"): log.error(f"CORRUPTION: {sym} trade_id без open_trade")
        if rec.get("open_trade") and not rec.get("trade_id"): log.error(f"CORRUPTION: {sym} open_trade без trade_id")
    return data

def save_watchlist(wl):
    # ФИКС-2: атомарная запись.
    _atomic_write_text(WATCHLIST_FILE, json.dumps(wl,ensure_ascii=False,indent=2))

def load_lifecycle_state():
    # ФИКС-6: раньше порча файла тонула молча в {} без лога и без TG.
    # Теперь: карантин + алерт, но прогон продолжается (в отличие от
    # watchlist.json тут нет данных об открытых позициях, поэтому
    # hard-stop не требуется — но потеря видна оператору).
    if not LIFECYCLE_STATE_FILE.exists(): return {}
    try:
        data=json.loads(LIFECYCLE_STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        backup=_quarantine_corrupt_file(LIFECYCLE_STATE_FILE)
        msg=(f"⚠️ <b>Monitor WARNING</b>\nlifecycle_state.json повреждён: {esc(str(e))}\n"
             f"Файл в карантине{f' ({esc(backup.name)})' if backup else ''}.\n"
             f"<i>Все активные cooldown и idea_first_seen_ts сброшены — "
             f"возможен преждевременный повторный вход по ранее закрытым символам.</i>")
        log.error(f"lifecycle_state.json повреждён: {e}"); send_tg(msg)
        return {}
    now=time.time(); ttl=IDEA_REGISTRY_TTL_DAYS*86400; out={}
    for sym,v in data.items():
        if not isinstance(v,dict): continue
        idea_fresh=(now-v.get("idea_first_seen_ts",0))<ttl if v.get("idea_first_seen_ts") else False
        cooldown_active=v.get("cooldown_until",0)>now
        if idea_fresh or cooldown_active: out[sym]=v
    return out

def save_lifecycle_state(state):
    # ФИКС-2: атомарная запись.
    _atomic_write_text(LIFECYCLE_STATE_FILE, json.dumps(state,ensure_ascii=False,indent=2))

def load_market_history():
    cutoff=now_ts()-LIFECYCLE_WINDOW_MIN*60; recs=load_jsonl(MARKET_HISTORY_FILE)
    grouped={}; seen=set()
    for r in recs:
        if r.get("ts",0)>cutoff:
            sym=r.get("symbol",""); key=(r.get("ts"),sym)
            if sym and key not in seen: seen.add(key); grouped.setdefault(sym,[]).append(r)
    for sym in grouped: grouped[sym].sort(key=lambda x:x["ts"])
    return grouped

def load_price_full():
    recs=load_jsonl(MARKET_HISTORY_FILE); idx={}
    for r in recs:
        sym,ts,p=r.get("symbol"),r.get("ts"),r.get("price")
        if sym and ts and valid_price(p): idx.setdefault(sym,[]).append((ts,p))
    for sym in idx: idx[sym].sort(key=lambda x:x[0])
    return idx

def load_existing_trade_ids():
    ids=set()
    for r in load_jsonl(TRADES_FILE):
        tid=r.get("trade_id")
        if tid: ids.add(tid)
    return ids

def load_pending():
    if not PENDING_FILE.exists(): return []
    raw=PENDING_FILE.read_text(encoding="utf-8")
    data=[]; bad=0
    for ln in raw.splitlines():
        ln=ln.strip()
        if not ln: continue
        try: data.append(json.loads(ln))
        except json.JSONDecodeError: bad+=1
    if bad and not data:
        backup=_quarantine_corrupt_file(PENDING_FILE)
        msg=f"⚠️ <b>Monitor CRITICAL</b>\npending_trades.jsonl полностью бит ({bad} строк)\nПрогон остановлен."
        log.error(msg.replace("<b>","").replace("</b>","")); send_tg(msg); sys.exit(1)
    if bad:
        # известный риск: частичная порча теряет часть pending-записей без
        # финализации в trades.jsonl. Раньше — только log.warning. Теперь
        # дополнительно шлём TG, чтобы потеря данных не была тихой.
        log.warning(f"pending_trades.jsonl: {bad} битых строк пропущено")
        send_tg(f"⚠️ <b>Monitor WARNING</b>\npending_trades.jsonl: {bad} повреждённых строк пропущено.\n"
                f"<i>Соответствующие сделки не попадут в trades.jsonl как WIN/LOSS.</i>")
    seen,out=set(),[]
    for item in data:
        tid=item.get("rec",{}).get("trade_id")
        if tid and tid in seen: continue
        if tid: seen.add(tid)
        out.append(item)
    if len(out)!=len(data): log.warning(f"pending dedup: {len(data)}→{len(out)}")
    return out

def save_pending(pending):
    # ФИКС-2: атомарная запись (было построчное open(...,"w")).
    content="".join(json.dumps(item,ensure_ascii=False)+"\n" for item in pending)
    _atomic_write_text(PENDING_FILE, content)


# ============================================================
# BROWSER / FETCH
# ============================================================

def _ensure_stealth_available():
    # ФИКС-8: раньше отсутствие playwright-stealth тихо no-op'илось.
    # По умолчанию теперь fail-fast (это критичная для обхода Cloudflare
    # зависимость). Осознанный обход — ALLOW_NO_STEALTH=true.
    if _STEALTH_AVAILABLE:
        return
    if ALLOW_NO_STEALTH:
        log.warning("playwright-stealth недоступен, ALLOW_NO_STEALTH=true — продолжаем БЕЗ stealth.")
        return
    msg=("⚠️ <b>Monitor CRITICAL</b>\nplaywright-stealth не установлен.\n"
         "Скрапинг без stealth рискует блокировкой Cloudflare без явного сигнала об этом.\n"
         "Установи playwright-stealth или явно разреши через ALLOW_NO_STEALTH=true.")
    log.error("playwright-stealth не установлен — прогон остановлен (см. ALLOW_NO_STEALTH).")
    send_tg(msg)
    sys.exit(1)

def _setup_browser_context(p):
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
    )
    if COINALYZE_P_SID or COINALYZE_CHAT_SID:
        cookies = []
        if COINALYZE_P_SID:
            cookies.append({"name": "p_sid", "value": COINALYZE_P_SID,
                            "domain": "coinalyze.net", "path": "/", "secure": True})
        if COINALYZE_CHAT_SID:
            cookies.append({"name": "chat_sid", "value": COINALYZE_CHAT_SID,
                            "domain": "coinalyze.net", "path": "/", "secure": True})
        cookies.append({"name": "cookies_accepted", "value": "1",
                        "domain": "coinalyze.net", "path": "/", "secure": True})
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
            log.info(f"Пагинация найдена после скролла {scroll_attempt + 1}, строк: {cur_count}")
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
    log.info(f"Итого: строк={final_count}, пагинация={'есть' if has_pagination else 'нет'}")
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
    soup=BeautifulSoup(html_text,"lxml"); rows=soup.select("tbody tr")
    log.info(f"Строк: {len(rows)}"); ts=now_ts(); out=[]; range_violations=0
    for tr in rows:
        symbol=tr.get("data-coin"); tds=tr.find_all("td")
        if len(tds)<23: continue
        spans=tds[1].find_all("span"); name=spans[0].get_text(strip=True) if spans else (symbol or "?")
        rec={"ts":ts,"symbol":symbol,"name":name,
            "price":parse_number(tds[2].get_text(strip=True)),"price_chg24":parse_number(tds[3].get_text(strip=True)),
            "mktcap":parse_number(tds[4].get_text(strip=True)),"volume24":parse_number(tds[5].get_text(strip=True)),
            "oi":parse_number(tds[6].get_text(strip=True)),"oi_chg24_pct":parse_number(tds[7].get_text(strip=True)),
            "oi_chg4h_pct":parse_number(tds[9].get_text(strip=True)),"oi_vol_ratio":parse_number(tds[11].get_text(strip=True)),
            "oi_mktcap_ratio":parse_number(tds[12].get_text(strip=True)),"fr_avg":parse_number(tds[13].get_text(strip=True)),
            "pfr_avg":parse_number(tds[14].get_text(strip=True)),"fr_oiw":parse_number(tds[15].get_text(strip=True)),
            "pfr_oiw":parse_number(tds[16].get_text(strip=True)),"liq_short24":parse_number(tds[17].get_text(strip=True)),
            "liq_long24":parse_number(tds[18].get_text(strip=True)),"ls_accounts":parse_number(tds[19].get_text(strip=True)),
            "btc_corr7d":parse_number(tds[20].get_text(strip=True)),"cvd24":parse_number(tds[21].get_text(strip=True)),
            "lls24":parse_number(tds[22].get_text(strip=True))}
        cvd=rec.get("cvd24"); lls=rec.get("lls24")
        if (cvd is not None and not(0<=cvd<=100)) or (lls is not None and not(0<=lls<=100)) or (rec.get("price") is not None and rec["price"]<=0): range_violations+=1
        out.append(rec)
    if range_violations: log.warning(f"parse_table: {range_violations} подозрительных строк")
    return out

def fetch_data() -> list[dict]:
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
                        log.info(f"Страница {page_num}: +{new_count} новых монет (клик)")
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
    log.info(f"Всего монет после пагинации: {len(all_rows)}")
    return all_rows


# ============================================================
# FILTER / SCORE / DERIVED / MOMENTUM / PATTERN
# ============================================================

def passes_filter(r):
    v=r.get("volume24"); pc=r.get("price_chg24"); oi=r.get("oi_chg24_pct")
    oi4=r.get("oi_chg4h_pct"); cvd=r.get("cvd24"); lls=r.get("lls24")
    oim=r.get("oi_mktcap_ratio"); oiv=r.get("oi_vol_ratio"); fr=r.get("fr_oiw")
    if v is None or v<=1_000_000: return False
    if pc is None or pc<1.0 or pc>15.0: return False
    if oi is None or oi<=5.0 or oi>=50.0: return False
    if oi4 is None or oi4<=0: return False
    if cvd is None or cvd<=55: return False
    if lls is None or lls>=40: return False
    if oim is None or oim>=0.15: return False
    if oiv is None or oiv<0.1 or oiv>2.5: return False
    if fr is not None and fr>0.05: return False
    return True

def calculate_score(r):
    score=0; pros,cons=[],[]
    cvd=r.get("cvd24")
    if cvd is not None:
        if cvd>70: score+=2; pros.append(f"CVD={cvd:.0f}>70")
        elif cvd>=55: score+=1; pros.append(f"CVD={cvd:.0f}")
    lls=r.get("lls24")
    if lls is not None:
        if lls<15: score+=2; pros.append(f"LLS={lls:.0f}%<15")
        elif lls<40: score+=1; pros.append(f"LLS={lls:.0f}%")
        if lls>50: score-=2; cons.append(f"LLS={lls:.0f}%>50")
    oi=r.get("oi_chg24_pct")
    if oi is not None:
        if 5<=oi<=35: score+=2; pros.append(f"OI24={oi:.1f}%")
        elif oi>50: score-=2; cons.append(f"OI24={oi:.1f}%>50")
    oi4=r.get("oi_chg4h_pct")
    if oi4 is not None and oi4>0: score+=1; pros.append(f"OI4h={oi4:.1f}%")
    pc=r.get("price_chg24")
    if pc is not None:
        if 2<=pc<=10: score+=1; pros.append(f"P={pc:.1f}%")
        elif pc>20: score-=2; cons.append(f"P={pc:.1f}%>20")
    fr=r.get("fr_oiw")
    if fr is not None:
        if -0.01<=fr<=0.03: score+=1; pros.append(f"FR={fr:.4f}")
        elif fr>0.05: score-=2; cons.append(f"FR={fr:.4f}>0.05")
    oim=r.get("oi_mktcap_ratio")
    if oim is not None and oim<0.10: score+=1; pros.append(f"OI/Mc={oim:.3f}")
    return score,pros,cons

def calc_derived(snaps):
    n=len(snaps)
    d={"oi_accel":0.0,"cvd_momentum":0.0,"price_accel":0.0,"funding_pressure":0.0,
       "oi_trend":"flat","cvd_trend":"flat","price_trend":"flat","oi4h_trend":"flat","divergence":"none","note":""}
    if n<2: return d
    def trend(vals):
        clean=[v for v in vals if v is not None]
        if len(clean)<2: return "flat"
        diff=clean[-1]-clean[0]; base=abs(clean[0]) if abs(clean[0])>1 else 10.0
        if diff>base*0.05: return "up"
        if diff<-base*0.05: return "down"
        return "flat"
    d["oi_trend"]=trend([s.get("oi_chg24_pct") for s in snaps])
    d["cvd_trend"]=trend([s.get("cvd24") for s in snaps])
    d["price_trend"]=trend([s.get("price_chg24") for s in snaps])
    d["oi4h_trend"]=trend([s.get("oi_chg4h_pct") for s in snaps])
    if n>=3:
        oi=[safe(s.get("oi_chg24_pct")) for s in snaps[-3:]]; d["oi_accel"]=(oi[2]-oi[1])-(oi[1]-oi[0])
    if n>=4: d["cvd_momentum"]=safe(snaps[-1].get("cvd24"))-safe(snaps[-4].get("cvd24"))
    elif n>=2: d["cvd_momentum"]=safe(snaps[-1].get("cvd24"))-safe(snaps[0].get("cvd24"))
    if n>=3:
        pc=[safe(s.get("price_chg24")) for s in snaps[-3:]]; d["price_accel"]=(pc[2]-pc[1])-(pc[1]-pc[0])
        fr=[safe(s.get("fr_oiw")) for s in snaps[-3:]]; d["funding_pressure"]=(fr[2]-fr[1])-(fr[1]-fr[0])
    if d["price_trend"]=="up" and d["oi_trend"]=="down": d["divergence"]="price_up_oi_down"; d["note"]="Цена ↑ OI ↓ — рост на закрытии шортов"
    elif d["price_trend"]=="up" and d["cvd_trend"]=="down": d["divergence"]="price_up_cvd_down"; d["note"]="Цена ↑ CVD ↓ — покупатели ослабевают"
    elif d["price_trend"]=="down" and d["oi_trend"]=="up": d["divergence"]="price_down_oi_up"; d["note"]="Цена ↓ OI ↑ — возможное накопление"
    elif d["oi_trend"]=="down" and n>=3:
        fr_vals=[safe(s.get("fr_oiw")) for s in snaps[-3:]]
        if fr_vals[-1]>fr_vals[0]+0.005: d["divergence"]="funding_up_oi_down"; d["note"]="Funding ↑ OI ↓ — выход участников"
    elif d["price_trend"]=="up" and n>=3:
        lls_vals=[safe(s.get("lls24")) for s in snaps[-3:]]
        if lls_vals[-1]>lls_vals[0]+10: d["divergence"]="lls_up_price_up"; d["note"]="LLS ↑ Price ↑ — поздняя стадия"
    elif d["oi_trend"]!="down" and d["cvd_trend"]!="down" and d["price_trend"]=="up": d["note"]="Здоровое движение: Price↑ OI и CVD не падают"
    return d

def calc_momentum(derived):
    m=0; tags=[]
    if derived["oi_accel"]>0: m+=2; tags.append("OI accel↑")
    elif derived["oi_accel"]<0: m-=1; tags.append("OI accel↓")
    if derived["cvd_momentum"]>5: m+=2; tags.append("CVD mom↑")
    elif derived["cvd_momentum"]<-5: m-=1; tags.append("CVD mom↓")
    if derived["price_accel"]>0: m+=1; tags.append("Price accel↑")
    if derived["funding_pressure"]<=0: m+=1; tags.append("Funding stable")
    else: tags.append("Funding↑")
    if derived["oi4h_trend"]=="up": m+=1; tags.append("OI4h↑")
    if derived["divergence"]=="none": m+=2; tags.append("No divergence")
    else: m-=2; tags.append(f"Div: {derived['divergence']}")
    if derived["oi_trend"]=="up" and derived["cvd_trend"]=="up": m+=1; tags.append("OI+CVD sync↑")
    return clamp(m,0,10),tags

def detect_pattern(r,derived,momentum):
    pc=safe(r.get("price_chg24")); oi24=safe(r.get("oi_chg24_pct")); cvd=safe(r.get("cvd24"))
    lls=safe(r.get("lls24")); fr=safe(r.get("fr_oiw")); ls=r.get("ls_accounts"); div=derived["divergence"]
    if cvd>90 and (fr>0.03 or derived["funding_pressure"]>0.005) and derived["oi_accel"]<0 and pc>15: return "Exhaustion"
    if div=="funding_up_oi_down" or (pc>5 and derived["oi_trend"]=="down"): return "Distribution"
    if oi24<-10 and lls>45 and derived["cvd_trend"]=="up": return "Capitulation"
    if pc>10 and oi24>20 and derived["cvd_trend"]=="down": return "Late Trend"
    if pc>0 and oi24>5 and lls>35 and ls is not None and ls<1.0: return "Short Squeeze"
    if pc<3 and derived["cvd_trend"]=="up" and fr<0.005 and oi24>0 and div=="none": return "Stealth Accumulation"
    if momentum>=7 and derived["oi_accel"]>2 and derived["cvd_momentum"]>10: return "Momentum Expansion"
    if pc>0 and oi24>5 and cvd>60 and lls<30 and derived["oi_trend"]!="down" and div=="none": return "Healthy Trend"
    return "—"

def fetch_btc_price_chg():
    try:
        r=requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",timeout=8)
        if r.status_code==200:
            chg=r.json().get("bitcoin",{}).get("usd_24h_change")
            if chg is not None: return float(chg)
    except Exception as e: log.warning(f"CoinGecko: {e}")
    return None

def detect_market_phase(rows):
    btc=next((r for r in rows if r.get("symbol") in BTC_SYMBOLS),None)
    btc_src="rows"
    if btc: btc_pc=btc.get("price_chg24") or 0
    else:
        btc_pc=fetch_btc_price_chg(); btc_src="coingecko"
        if btc_pc is None:
            return {"phase":"unknown","note":"","modifier":0,"btc_chg24":None,"btc_source":"none"}
    up=sum(1 for r in rows if (r.get("price_chg24") or 0)>0); ratio=up/max(len(rows),1)
    if btc_pc>2 and ratio>0.6:    ph,note,mod="risk-on","BTC↑ рынок широкий",+1
    elif btc_pc>2 and ratio<0.4:  ph,note,mod="btc-dominance","BTC↑ альты нет",0
    elif btc_pc<-2 and ratio<0.3: ph,note,mod="risk-off","BTC↓ рынок слабый",-1
    elif btc_pc<-2 and ratio>0.5: ph,note,mod="rotation","BTC↓ альты держатся",0
    else:                         ph,note,mod="neutral","",0
    return {"phase":ph,"note":note,
            "modifier":mod if MARKET_PHASE_MODIFIER_ENABLED else 0,
            "modifier_raw":mod,"btc_chg24":round(btc_pc,3),
            "btc_source":btc_src,"breadth_ratio":round(ratio,3)}

ACTIONS={"NEUTRAL":"IGNORE","ACCUMULATION":"WATCH","EARLY_MOVE":"WATCH_LONG","CONFIRMED_TREND":"POSSIBLE_ENTRY","ACCELERATION":"LONG_SETUP","EXHAUSTION":"NO_NEW_ENTRY","DISTRIBUTION":"EXIT_AVOID","INVALIDATED":"REMOVE"}
STATE_EMOJI={"NEUTRAL":"⚪","ACCUMULATION":"🔍","EARLY_MOVE":"🌱","CONFIRMED_TREND":"🟢","ACCELERATION":"🚀","EXHAUSTION":"🟠","DISTRIBUTION":"🔴","INVALIDATED":"❌"}
ALLOWED_FROM={"ACCUMULATION":{"NEUTRAL","ACCUMULATION"},"EARLY_MOVE":{"NEUTRAL","ACCUMULATION","EARLY_MOVE"},"CONFIRMED_TREND":{"ACCUMULATION","EARLY_MOVE","CONFIRMED_TREND"},"ACCELERATION":{"CONFIRMED_TREND","ACCELERATION"},"EXHAUSTION":{"CONFIRMED_TREND","ACCELERATION","EXHAUSTION"},"DISTRIBUTION":{"CONFIRMED_TREND","ACCELERATION","EXHAUSTION","DISTRIBUTION"},"INVALIDATED":{"NEUTRAL","ACCUMULATION","EARLY_MOVE","CONFIRMED_TREND","ACCELERATION","EXHAUSTION","DISTRIBUTION","INVALIDATED"}}


# ============================================================
# LIFECYCLE / CONFIDENCE / EARLINESS
# ============================================================

def detect_lifecycle(symbol,snaps,score,derived,prev_state="NEUTRAL"):
    n=len(snaps); reasons,warnings=[],[]
    if n<2: return "NEUTRAL",["недостаточно данных"],[],None
    last=snaps[-1]
    if last.get("cvd24") is None or last.get("oi_chg24_pct") is None: return "NEUTRAL",["нет данных CVD/OI"],[],None
    pc=safe(last.get("price_chg24")); oi24=safe(last.get("oi_chg24_pct")); oi4h=safe(last.get("oi_chg4h_pct"))
    cvd=safe(last.get("cvd24")); fr=safe(last.get("fr_oiw"))
    oi_accel=derived["oi_accel"]; cvd_mom=derived["cvd_momentum"]; price_accel=derived["price_accel"]; fund_press=derived["funding_pressure"]
    def allowed(target): return prev_state in ALLOWED_FROM.get(target,set())
    if oi24<-5 and cvd<40:
        reasons.append(f"OI24={oi24:.1f}%<-5, CVD={cvd:.0f}<40")
        if pc<-3: reasons.append(f"Price={pc:.1f}% — структура сломана")
        return "INVALIDATED",reasons,warnings,None
    if allowed("DISTRIBUTION") and n>=2:
        oi4h_prev=safe(snaps[-2].get("oi_chg4h_pct"))
        if oi4h<0 and oi4h_prev<0 and cvd_mom<-10:
            reasons.append(f"OI4h<0 2 снимка, CVD mom={cvd_mom:.0f}<-10")
            if pc>5: reasons.append(f"Price={pc:.1f}% — ещё высокая")
            if fr>0.02: reasons.append(f"FR={fr:.4f} — остаётся высоким")
            return "DISTRIBUTION",reasons,warnings,None
    if allowed("EXHAUSTION"):
        if cvd>90 and (fr>0.03 or fund_press>0.005) and oi_accel<0 and pc>15:
            reasons.append(f"CVD={cvd:.0f}>90, FR={fr:.4f}, fund_press={fund_press:.4f}")
            reasons.append(f"OI accel={oi_accel:.1f}<0 — замедление")
            warnings.append(f"Price={pc:.1f}% — вертикальный рост")
            return "EXHAUSTION",reasons,warnings,None
    if allowed("ACCELERATION"):
        if n>=3 and oi_accel>2 and cvd_mom>10 and price_accel>=1 and derived["oi_trend"]!="down" and derived["cvd_trend"]!="down":
            reasons.append(f"OI accel={oi_accel:.1f}>2"); reasons.append(f"CVD mom={cvd_mom:.0f}>10"); reasons.append(f"Price accel={price_accel:.1f}>=1")
            if cvd>85: warnings.append(f"CVD={cvd:.0f} — близко к максимуму")
            if pc>10: warnings.append(f"Price={pc:.1f}% — уже вырос")
            return "ACCELERATION",reasons,warnings,None
    if allowed("CONFIRMED_TREND") and n>=MIN_SNAPS_LIFECYCLE:
        recent=snaps[-MIN_SNAPS_LIFECYCLE:]
        result_a=check_confirmed_path_a(recent)
        result_b=check_confirmed_path_b(recent,cvd_mom)
        trends_ok=derived["cvd_trend"]!="down" and derived["oi_trend"]!="down"
        if (result_a["passed"] or result_b["passed"]) and trends_ok:
            is_early=result_b["passed"] and not result_a["passed"]
            if is_early: reasons.append("Раннее подтверждение: OI>2 CVD>50 + ускорение + momentum")
            else: reasons.append(f"{MIN_SNAPS_LIFECYCLE} снимков: OI>5 CVD>55 LLS<40 OI4h>0 P↑ FR<0.05")
            reasons.append("OI и CVD не снижаются (шагово и по тренду)")
            if oi_accel<=0: warnings.append("OI не ускоряется")
            return "CONFIRMED_TREND",reasons,warnings,("early" if is_early else "classic")
    if allowed("EARLY_MOVE") and n>=3:
        last3=snaps[-3:]
        price_up=all(safe(last3[i].get("price_chg24"))>safe(last3[i-1].get("price_chg24")) for i in range(1,3))
        oi_up=all(safe(last3[i].get("oi_chg24_pct"))>safe(last3[i-1].get("oi_chg24_pct")) for i in range(1,3))
        cvd_up=all(safe(last3[i].get("cvd24"))>safe(last3[i-1].get("cvd24"))-3 for i in range(1,3))
        vol_up=all(safe(last3[i].get("volume24"))>safe(last3[i-1].get("volume24"))*0.95 for i in range(1,3))
        if price_up and oi_up and cvd_up and vol_up:
            reasons.append("3 снимка: Price↑ OI↑ CVD↑ Vol↑")
            return "EARLY_MOVE",reasons,warnings,None
    if allowed("ACCUMULATION") and n>=3:
        last3=snaps[-3:]
        oi4_pos=all(safe(s.get("oi_chg4h_pct"))>0 for s in last3)
        cvd_avg=sum(safe(s.get("cvd24")) for s in last3)/3
        fr_ok=all(s.get("fr_oiw") is not None and s.get("fr_oiw")<0.03 for s in last3)
        if oi4_pos and cvd_avg>50 and pc<5 and fr_ok:
            reasons.append(f"OI4h>0 3 снимка, CVD avg={cvd_avg:.0f}>50, FR<0.03")
            reasons.append(f"Price={pc:.1f}%<5 — ещё не ушёл")
            return "ACCUMULATION",reasons,warnings,None
    return "NEUTRAL",["нет подтверждённого движения"],warnings,None

def calc_confidence(state,snaps,score,derived,market_mod):
    base={"NEUTRAL":50,"ACCUMULATION":40,"EARLY_MOVE":55,"CONFIRMED_TREND":70,"ACCELERATION":80,"EXHAUSTION":75,"DISTRIBUTION":70,"INVALIDATED":90}.get(state,50)
    snap_bonus=min(len(snaps),10)*2; penalty=0
    if derived["divergence"]!="none": penalty+=15
    if derived["oi_accel"]<0 and state in("ACCELERATION","CONFIRMED_TREND"): penalty+=10
    if len(snaps)>0 and safe(snaps[-1].get("cvd24"))>95 and state=="ACCELERATION": penalty+=5
    return clamp(base+snap_bonus+score+market_mod*3-penalty,0,100)

def entry_earliness(r):
    pc_pos=min(safe(r.get("price_chg24"))/15.0,1.0)
    oi_pos=min(safe(r.get("oi_chg24_pct"))/50.0,1.0)
    fr_pos=min(max(safe(r.get("fr_oiw"))/0.05,0),1.0)
    avg=(pc_pos+oi_pos+fr_pos)/3
    return avg,("ранняя" if avg<0.35 else "средняя" if avg<0.65 else "поздняя")


# ============================================================
# TELEGRAM / FORMAT
# ============================================================

def send_tg(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID: log.warning("TG не настроен"); return
    url=f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    chunks,rem=[],text
    while len(rem)>3800:
        sp=rem.rfind("\n",0,3800)
        if sp==-1: sp=3800
        chunks.append(rem[:sp]); rem=rem[sp:]
    chunks.append(rem)
    for ch in chunks:
        try:
            r=requests.post(url,data={"chat_id":TG_CHAT_ID,"text":ch,"parse_mode":"HTML","disable_web_page_preview":True},timeout=15)
            if r.status_code!=200: requests.post(url,data={"chat_id":TG_CHAT_ID,"text":ch,"disable_web_page_preview":True},timeout=15)
            time.sleep(0.4)
        except Exception as e: log.error(f"TG: {e}")

def format_signal(symbol,wl,cur,snaps,reasons,warnings,market):
    state=wl["state"]; emoji=STATE_EMOJI.get(state,"⚪"); action=ACTIONS.get(state,"")
    conf=wl.get("confidence",0); score=cur.get("score",0); mom=cur.get("momentum",0)
    pattern=cur.get("pattern","—"); derived=cur.get("derived",{}); prev=wl.get("previous_state","")
    s=snaps[-1] if snaps else {}
    ar={"up":"↑","down":"↓","flat":"→"}
    oi_t=ar.get(derived.get("oi_trend"),"→"); cvd_t=ar.get(derived.get("cvd_trend"),"→"); prc_t=ar.get(derived.get("price_trend"),"→")
    _,early_label=entry_earliness(s); line="━━━━━━━━━━━━━━━━━━"
    reas=" · ".join(reasons[:3]) if reasons else ""; warns=" · ".join(warnings[:3]) if warnings else ""
    prev_line=f" ({esc(prev)} →)" if prev and prev!=state else ""
    msg=(f"{emoji} <b>{esc(cur.get('name',symbol))} ({esc(symbol)})</b>\n{line}\n"
         f"{esc(state)}{prev_line} → {esc(action)}\n"
         f"Score {score}/10 · Momentum {mom}/10 · Conf {conf}%\n"
         f"Вход: {early_label} · Паттерн: {esc(pattern)}\n"
         f"OI {oi_t} CVD {cvd_t} Price {prc_t}")
    if market.get("note"): msg+=f" · {esc(market['note'])}"
    msg+=(f"\n{line}\nP {fmt_pct(s.get('price_chg24'))} | OI {fmt_pct(s.get('oi_chg24_pct'))} | "
          f"4h {fmt_pct(s.get('oi_chg4h_pct'))} | CVD {fmt_num(s.get('cvd24'),dec=0)} | LLS {fmt_num(s.get('lls24'),'%',0)}\n")
    if derived.get("note"): msg+=f"<i>{esc(derived['note'])}</i>\n"
    if reas: msg+=f"{line}\n✅ {esc(reas)}\n"
    if warns: msg+=f"⚠️ {esc(warns)}\n"

    # Research-only блок (TG)
    regime       = cur.get("entry_oi_price_regime")
    liq_share    = cur.get("entry_short_liq_share24")
    liq_imb      = cur.get("entry_liq_imbalance")
    fund_press   = cur.get("entry_funding_oi_pressure")
    liq_int      = cur.get("entry_liquidation_intensity")
    fr_z         = cur.get("entry_fr_oiw_zscore")

    research_parts = []
    if regime is not None:
        research_parts.append(f"Regime: {esc(regime)}")
    if liq_share is not None:
        research_parts.append(f"ShortLiq%: {liq_share:.2%}")
    if liq_imb is not None:
        research_parts.append(f"LiqImb: {liq_imb:+.3f}")
    if fund_press is not None:
        research_parts.append(f"FR·OI pressure: {fund_press:.5f}")
    if liq_int is not None:
        research_parts.append(f"LiqInt: {liq_int:.5f}")
    if fr_z is not None:
        research_parts.append(f"FR z-score: {fr_z:+.2f}")

    if research_parts:
        msg += (
            f"{line}\n"
            f"📊 Research\n"
            + " · ".join(research_parts) + "\n"
        )

    return msg

def format_trade_close(rec):
    pnl=rec.get("strategy_pnl_pct")
    outcome_unknown=rec.get("outcome_unknown", False)
    pnl_s="—" if pnl is None else f"{pnl:+.1f}%"
    emoji="💚" if (pnl is not None and pnl>0) else "💔" if (pnl is not None and pnl<0) else "➖"
    peak=rec.get("max_pnl_pct"); dd=rec.get("drawdown_from_peak_pct"); line="━━━━━━━━━━━━━━━━━━"
    stale_min=rec.get("exit_price_stale_min")
    stale_note=f" (цена устарела на {fmt_num(stale_min,' мин',0)})" if stale_min else ""
    msg=(f"{emoji} <b>{esc(rec.get('name',rec['symbol']))} ({esc(rec['symbol'])})</b> — сделка закрыта\n{line}\n"
         f"Вход {fmt_price(rec.get('entry_price'))} → Выход {fmt_price(rec.get('exit_price'))}{esc(stale_note)}   <b>{pnl_s}</b>\n"
         f"Держали {rec.get('hold_min')} мин · пик {fmt_pct(peak)} · просадка {fmt_pct(-dd if dd else None)}\n"
         f"Выход по: {esc(rec.get('exit_reason'))}\n"
         f"Вход был: {esc(rec.get('entry_path'))} · mom {rec.get('entry_momentum')} · "
         f"cvd_m {fmt_num(rec.get('entry_cvd_momentum'),dec=0)} · {esc(rec.get('entry_pattern'))} · "
         f"{esc(rec.get('entry_earliness_label'))}\n")
    if outcome_unknown:
        msg += "⚠️ <i>Цена закрытия неизвестна — исход сделки помечен outcome_unknown, PnL не считается.</i>\n"
    r60=rec.get("return_60m")
    if r60 is not None: msg+=f"Signal@60m: {r60:+.1f}%\n"

    # Research-only блок в уведомлении о закрытии
    regime     = rec.get("entry_oi_price_regime")
    liq_share  = rec.get("entry_short_liq_share24")
    liq_imb    = rec.get("entry_liq_imbalance")
    fund_press = rec.get("entry_funding_oi_pressure")
    liq_int    = rec.get("entry_liquidation_intensity")
    fr_z       = rec.get("entry_fr_oiw_zscore")

    research_parts = []
    if regime is not None:
        research_parts.append(f"Regime: <b>{esc(regime)}</b>")
    if liq_share is not None:
        research_parts.append(f"ShortLiq%: <b>{liq_share:.2%}</b>")
    if liq_imb is not None:
        research_parts.append(f"LiqImb: <b>{liq_imb:+.3f}</b>")
    if fund_press is not None:
        research_parts.append(f"FR·OI: <b>{fund_press:.5f}</b>")
    if liq_int is not None:
        research_parts.append(f"LiqInt: <b>{liq_int:.5f}</b>")
    if fr_z is not None:
        research_parts.append(f"FR z: <b>{fr_z:+.2f}</b>")

    if research_parts:
        msg += (
            f"{line}\n"
            f"📊 <b>Research @entry</b>\n"
            + " · ".join(research_parts) + "\n"
        )

    return msg

def llm_verify(symbol,wl,cur,snaps):
    if not ENABLE_LLM or not QWEN_API_KEY: return None
    state=wl["state"]; conf=wl.get("confidence",0); action=ACTIONS.get(state,""); derived=cur.get("derived",{})
    recent=snaps[-5:]
    snap_txt="\n".join(f"  ts={s['ts']} P={s.get('price_chg24')} OI24={s.get('oi_chg24_pct')} OI4h={s.get('oi_chg4h_pct')} CVD={s.get('cvd24')} LLS={s.get('lls24')} FR={s.get('fr_oiw')}" for s in recent)
    user_msg=(f"Монета: {symbol}\nState: {state}\nConfidence: {conf}%\nAction: {action}\n"
              f"Momentum: {cur.get('momentum',0)}/10\nPattern: {cur.get('pattern','—')}\n"
              f"Derived: OI_accel={derived.get('oi_accel',0):.1f} CVD_mom={derived.get('cvd_momentum',0):.0f} "
              f"Price_accel={derived.get('price_accel',0):.1f} Funding_press={derived.get('funding_pressure',0):.4f}\n"
              f"Divergence: {derived.get('divergence','none')}\n\nСнимки:\n{snap_txt}\n\n"
              f'Верни JSON: {{"agree": true/false, "reason": "одно предложение", "risk": "low/medium/high"}}')
    try:
        resp=requests.post(f"{QWEN_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization":f"Bearer {QWEN_API_KEY}","Content-Type":"application/json"},
            json={"model":QWEN_MODEL,"messages":[{"role":"system","content":"Верификатор крипто-сигналов. Только JSON."},{"role":"user","content":user_msg}],
                  "temperature":0.1,"max_tokens":150,"response_format":{"type":"json_object"}},timeout=30)
        if resp.status_code==200: return json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception as e: log.warning(f"LLM: {e}")
    return None


# ============================================================
# TRADE RECORD
# ============================================================

def _new_trade_id(symbol, ts):
    # ФИКС-9: раньше "{symbol}_{ts}" — коллизия возможна при двух процессах,
    # стартовавших в одну и ту же секунду. uuid4-суффикс исключает это.
    return f"{symbol}_{ts}_{uuid.uuid4().hex[:8]}"

def _fill_horizons(ot,sym,as_of_ts,price_full):
    ep=ot.get("entry_price")
    if not ep: return
    max_lag=HORIZON_MAX_LAG_MIN*60
    for h in TRADE_HORIZONS:
        key=f"return_{h}m"
        if ot.get(key) is None and as_of_ts>=ot["entry_ts"]+h*60:
            ph=price_at(price_full,sym,ot["entry_ts"]+h*60,max_lag_sec=max_lag)
            if ph:
                ot[key]=round((ph-ep)/ep*100,3)
                ot[f"{key}_available"]=True

def open_trade_record(r,ts,state,path,score,momentum,conf,early_val,early_label,pattern,derived,market,idea_first_seen_ts,snapshots,price,history_len=None,window=None,strength=None,shadow=None,hist=None):
    if state=="CONFIRMED_TREND": trigger=f"confirmed_trend_{path}_path"
    elif state=="ACCELERATION": trigger="acceleration_confirmation"
    else: trigger=state.lower()

    entry_snapshot={
        "timestamp":ts,"price":price,"symbol":r.get("symbol"),
        "features":{
            "price_chg24":r.get("price_chg24"),"oi_chg24":r.get("oi_chg24_pct"),
            "oi_chg4h":r.get("oi_chg4h_pct"),"funding":r.get("fr_avg"),
            "funding_oiw":r.get("fr_oiw"),"cvd24":r.get("cvd24"),
            "lls24":r.get("lls24"),"oi_vol_ratio":r.get("oi_vol_ratio"),
            "oi_mcap_ratio":r.get("oi_mktcap_ratio"),"ls_accounts":r.get("ls_accounts"),
            "volume24":r.get("volume24"),"oi_abs":r.get("oi"),"mktcap":r.get("mktcap"),
            "liq_short24":r.get("liq_short24"),
            "liq_long24":r.get("liq_long24"),"btc_corr7d":r.get("btc_corr7d"),
            "cvd_momentum":round(derived["cvd_momentum"],2),"oi_accel":round(derived["oi_accel"],3),
            "price_accel":round(derived["price_accel"],3),"funding_pressure":round(derived["funding_pressure"],5),
            "oi_trend":derived["oi_trend"],"cvd_trend":derived["cvd_trend"],
            "price_trend":derived["price_trend"],"oi4h_trend":derived["oi4h_trend"],
            "divergence":derived["divergence"],
        },
        "decision":{"state":state,"score":score,"momentum":momentum,"path":path,
                    "pattern":pattern,"market_phase":market.get("phase","unknown"),
                    "entry_reason":{"state":state,"path":path,"trigger":trigger}},
        "discovery":dict(DISCOVERY),
    }

    # ============================================================
    # Research-only entry derivatives (только для анализа WIN/LOSS)
    # ============================================================
    research_oi_chg4h = r.get("oi_chg4h_pct")
    research_price_chg24 = r.get("price_chg24")
    research_liq_short24 = r.get("liq_short24")
    research_liq_long24 = r.get("liq_long24")
    research_oi_abs = r.get("oi")
    research_fr_oiw = r.get("fr_oiw")

    entry_oi_price_regime = calc_entry_oi_price_regime(
        research_oi_chg4h, research_price_chg24
    )
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
    entry_fr_oiw_zscore = calc_entry_fr_oiw_zscore_from_hist(
        hist, research_fr_oiw
    )

    ot={
        "entry_ts":ts,"entry_price":price,"entry_state":state,"entry_path":path,
        "last_price":price,"last_price_ts":ts,
        "max_pnl_pct":0.0,"min_pnl_pct":0.0,"peak_ts":ts,
        "max_state":state,"state_history":[{"ts":ts,"state":state,"score":score,"reason":"entry"}],
        "current_state":state,"current_state_start_ts":ts,"time_in_states":{},
        "idea_first_seen_ts":idea_first_seen_ts,
        "idea_age_minutes":round((ts-idea_first_seen_ts)/60,1) if idea_first_seen_ts else None,
        "signal_age_min":round((ts-idea_first_seen_ts)/60,1) if idea_first_seen_ts else None,
        "snapshot_count_before_entry":snapshots,
        "watchlist_tenure_runs":snapshots,
        "history_len_before_entry":history_len,
        "entry_window":window,
        "entry_signal_strength":strength,
        "entry_shadow_predicates":({k:shadow[k] for k,_ in SHADOW_VARIANTS} if shadow else None),
        "asset_class":classify_asset_class(r),"name":r.get("name",r.get("symbol","")),
        "engine_versions":dict(ENGINE_VERSIONS),
        "entry_snapshot":entry_snapshot,
        "entry_snapshot_hash":compute_snapshot_hash(entry_snapshot),
        "entry_reason":{"state":state,"path":path,"trigger":trigger},
        "entry_score":score,"entry_momentum":momentum,
        "entry_cvd_momentum":round(derived["cvd_momentum"],2),
        "entry_oi_accel":round(derived["oi_accel"],3),
        "entry_price_accel":round(derived["price_accel"],3),
        "entry_confidence":conf,
        "entry_earliness":round(early_val,2),"entry_earliness_label":early_label,
        "entry_pattern":pattern,
        "entry_oi_trend":derived["oi_trend"],"entry_cvd_trend":derived["cvd_trend"],
        "entry_price_trend":derived["price_trend"],"entry_divergence":derived["divergence"],
        "entry_price_chg24":r.get("price_chg24"),"entry_oi_chg24":r.get("oi_chg24_pct"),
        "entry_oi_chg4h":r.get("oi_chg4h_pct"),"entry_cvd24":r.get("cvd24"),
        "entry_lls24":r.get("lls24"),"entry_fr_oiw":r.get("fr_oiw"),
        "entry_oi_vol_ratio":r.get("oi_vol_ratio"),"entry_oi_mktcap_ratio":r.get("oi_mktcap_ratio"),
        "entry_liq_short24":r.get("liq_short24"),"entry_liq_long24":r.get("liq_long24"),
        "entry_ls_accounts":r.get("ls_accounts"),"entry_btc_corr7d":r.get("btc_corr7d"),
        "entry_volume24":r.get("volume24"),"entry_oi_abs":r.get("oi"),"entry_mktcap":r.get("mktcap"),
        "entry_market_phase":market.get("phase","unknown"),
        "entry_market_breadth":market.get("breadth_ratio"),
        "entry_btc_chg24":market.get("btc_chg24"),

        # Research-only
        "entry_oi_price_regime": entry_oi_price_regime,
        "entry_short_liq_share24": entry_short_liq_share24,
        "entry_liq_imbalance": entry_liq_imbalance,
        "entry_funding_oi_pressure": entry_funding_oi_pressure,
        "entry_liquidation_intensity": entry_liquidation_intensity,
        "entry_fr_oiw_zscore": entry_fr_oiw_zscore,

        "data_quality":{"entry_price_source":"live","exit_price_source":None,"missing_snapshots":0,
                        "max_price_age_min":0,"price_unknown":False,"exit_price_age_min":0.0},
        "shadow_stops":{str(lvl):None for lvl in SHADOW_STOP_LEVELS},
        "shadow_schmitt_on":True,
        "shadow_schmitt_exit_ts":None,
    }
    for h in TRADE_HORIZONS:
        ot[f"return_{h}m"]=None
        ot[f"return_{h}m_available"]=False
    return ot


# ============================================================
# EXCHANGE CLOSE CONFIRMATION (ФИКС-1)
# ============================================================

def _attempt_exchange_close(ot, symbol):
    """Пытается закрыть позицию на бирже. НЕ финализирует локальную запись
    сделки — это отдельная ответственность close_trade().

    Возвращает:
      'confirmed'      — на бирже подтверждённо нечего закрывать (успешно
                          закрыто прямо сейчас, либо остаток уже был 0).
      'unconfirmed'     — API вернул ошибку/неизвестный статус; позиция
                          МОЖЕТ фактически остаться открытой на бирже.
      'not_applicable'  — ENABLE_BINGX=false либо для этой сделки нет
                          исполнения на бирже — подтверждать нечего.
    """
    if not ENABLE_BINGX:
        return "not_applicable"
    bx = ot.get("bingx") or {}
    if not bx or bx.get("status") not in ("opened", "already_open"):
        return "not_applicable"
    qty_to_close = safe(bx.get("qty_remaining"), safe(bx.get("qty"), 0.0))
    if not qty_to_close or qty_to_close <= 0:
        return "confirmed"
    try:
        import bingx_client
        res = bingx_client.close_long(symbol, qty_to_close)
    except Exception as e:
        log.error(f"[{symbol}] EXCHANGE_CLOSE exception: {e}")
        ot.setdefault("close_attempts", []).append({"ts": now_ts(), "error": str(e)[:200]})
        return "unconfirmed"
    ot["bingx_close"] = res
    status = res.get("status")
    if status == "closed":
        bx["qty_remaining"] = 0.0
        ot["bingx"] = bx
        log.info(f"[{symbol}] BingX CLOSE ok orderId={res.get('order_id')} qty={qty_to_close}")
        log_execution_event(symbol, "close_confirmed", qty=qty_to_close, order_id=res.get("order_id"))
        return "confirmed"
    log.error(f"[{symbol}] BingX CLOSE НЕ подтверждён: status={status} error={res.get('error')}")
    ot.setdefault("close_attempts", []).append({"ts": now_ts(), "status": status, "error": str(res.get("error"))[:200]})
    send_tg(f"🚨 <b>{esc(ot.get('name', symbol))} ({esc(symbol)})</b> — закрытие НЕ подтверждено биржей\n"
            f"━━━━━━━━━━━━━━━━━━\nstatus={esc(status)} error={esc(str(res.get('error'))[:200])}\n"
            f"<i>Позиция считается открытой до подтверждения. Повторная попытка на следующем прогоне.</i>")
    return "unconfirmed"


def _exit_meta(source,exit_ts,last_price_ts):
    if source=="live" or last_price_ts is None: return source,0.0
    return source,round(max(0,exit_ts-last_price_ts)/60,1)

def close_trade(ot,symbol,exit_ts,exit_price,exit_reason,exit_state,price_full,
                 exit_price_source="live",exit_candidates=None,lifecycle_complete=True,
                 exchange_close_status="not_applicable"):
    """Финализирует ЛОКАЛЬНУЮ запись сделки. Экзекуция на бирже (если нужна)
    должна быть УЖЕ подтверждена вызывающим кодом через _attempt_exchange_close
    ДО вызова этой функции (ФИКС-1) — здесь никаких обращений к бирже нет."""
    ep=ot.get("entry_price")
    _fill_horizons(ot,symbol,exit_ts,price_full)

    # ФИКС-5: раньше вызывающий код мог подсунуть entry_price как "unknown"
    # exit_price, что давало фиктивный PnL=0%. Теперь: если exit_price
    # отсутствует или источник "unknown" — PnL не считается вовсе.
    outcome_unknown = (exit_price is None) or (exit_price_source == "unknown")
    gross=round((exit_price-ep)/ep*100,3) if (exit_price and ep and not outcome_unknown) else None

    max_pnl=ot.get("max_pnl_pct",0.0); min_pnl=ot.get("min_pnl_pct",0.0); peak_ts=ot.get("peak_ts",ot["entry_ts"])
    if gross is not None:
        if gross>max_pnl: max_pnl=gross; peak_ts=exit_ts
        if gross<min_pnl: min_pnl=gross
    strategy_pnl=round(gross-FEE_PCT,3) if gross is not None else None
    hold_min=round((exit_ts-ot["entry_ts"])/60,1)
    drawdown=round(max_pnl-gross,3) if gross is not None else None
    time_to_peak=round((peak_ts-ot["entry_ts"])/60,1)
    _,stale_min=_exit_meta(exit_price_source,exit_ts,ot.get("last_price_ts"))
    if ot.get("data_quality"):
        ot["data_quality"]["exit_price_source"]=exit_price_source
        ot["data_quality"]["price_unknown"]=stale_min>30 or outcome_unknown
    duration_min=round((exit_ts-ot.get("current_state_start_ts",exit_ts))/60,1)
    last_state=ot.get("current_state","UNKNOWN")
    time_in_states=dict(ot.get("time_in_states",{}))
    time_in_states[last_state]=time_in_states.get(last_state,0)+duration_min
    protections_triggered=[r for r in (exit_candidates or []) if r in PROTECTION_REASONS]
    def winflag(h):
        v=ot.get(f"return_{h}m")
        if v is None: return None
        return 1 if v>=TRADE_WIN_PCT else 0
    rec={
        "schema_version":TRADE_SCHEMA_VERSION,
        "trade_id":f"{symbol}_{ot['entry_ts']}" if not ot.get("trade_id_full") else ot["trade_id_full"],
        "symbol":symbol,"name":ot.get("name",symbol),
        "asset_class":ot.get("asset_class",classify_asset_class({"symbol":symbol})),
        "entry_ts":ot["entry_ts"],"entry_price":ep,
        "exit_ts":exit_ts,"exit_price":exit_price,
        "exit_reason":exit_reason,"exit_state":exit_state,
        "exit_class":EXIT_CLASS.get(exit_reason,"UNKNOWN"),
        "exit_candidates":exit_candidates or [exit_reason],
        "exit_count":len(exit_candidates or [exit_reason]),
        "protections_triggered":protections_triggered,
        "close_type":"natural" if lifecycle_complete else "technical",
        "trade_lifecycle_complete":lifecycle_complete,
        "exit_price_source":exit_price_source,
        "exit_price_stale_min":stale_min,
        "outcome_unknown":outcome_unknown,                       # ФИКС-5
        "exchange_close_status":exchange_close_status,           # ФИКС-1 (audit trail)
        "closed_before_60m":hold_min<60,
        "hold_min":hold_min,
        "entry_state":ot.get("entry_state"),
        "entry_path":ot.get("entry_path"),
        "entry_reason":ot.get("entry_reason",{}),
        "entry_snapshot":ot.get("entry_snapshot",{}),
        "entry_snapshot_hash":ot.get("entry_snapshot_hash",{}),
        "idea_first_seen_ts":ot.get("idea_first_seen_ts"),
        "idea_age_minutes":ot.get("idea_age_minutes"),
        "signal_age_min":ot.get("signal_age_min"),
        "snapshot_count_before_entry":ot.get("snapshot_count_before_entry"),
        "watchlist_tenure_runs":ot.get("watchlist_tenure_runs"),
        "history_len_before_entry":ot.get("history_len_before_entry"),
        "entry_window":ot.get("entry_window"),
        "entry_signal_strength":ot.get("entry_signal_strength"),
        "entry_shadow_predicates":ot.get("entry_shadow_predicates"),
        "last_signal_strength":ot.get("last_signal_strength"),
        "shadow_stops":ot.get("shadow_stops"),
        "shadow_schmitt_exit_ts":ot.get("shadow_schmitt_exit_ts"),
        "shadow_schmitt_exit_min":ot.get("shadow_schmitt_exit_min"),
        "entry_volume24":ot.get("entry_volume24"),
        "entry_oi_abs":ot.get("entry_oi_abs"),
        "entry_mktcap":ot.get("entry_mktcap"),
        "entry_market_breadth":ot.get("entry_market_breadth"),
        "entry_btc_chg24":ot.get("entry_btc_chg24"),
        "exit_price_age_min":ot.get("data_quality",{}).get("exit_price_age_min"),
        "engine_versions":ot.get("engine_versions",dict(ENGINE_VERSIONS)),
        "discovery":ot.get("entry_snapshot",{}).get("discovery"),
        "discovery_sha256":(ot.get("entry_snapshot",{}).get("discovery") or {}).get("url_sha256"),
        "signal_logic_version":ot.get("engine_versions",{}).get("signal",SIGNAL_LOGIC_VERSION),
        "lifecycle_engine_version":ot.get("engine_versions",{}).get("lifecycle",LIFECYCLE_ENGINE_VERSION),
        "max_state":ot.get("max_state"),
        "state_history":ot.get("state_history",[]),
        "time_in_states":time_in_states,
        "data_quality":ot.get("data_quality",{}),
        "entry_score":ot.get("entry_score"),
        "entry_momentum":ot.get("entry_momentum"),
        "entry_cvd_momentum":ot.get("entry_cvd_momentum"),
        "entry_oi_accel":ot.get("entry_oi_accel"),
        "entry_price_accel":ot.get("entry_price_accel"),
        "entry_confidence":ot.get("entry_confidence"),
        "entry_earliness":ot.get("entry_earliness"),
        "entry_earliness_label":ot.get("entry_earliness_label"),
        "entry_pattern":ot.get("entry_pattern"),
        "entry_oi_trend":ot.get("entry_oi_trend"),
        "entry_cvd_trend":ot.get("entry_cvd_trend"),
        "entry_price_trend":ot.get("entry_price_trend"),
        "entry_divergence":ot.get("entry_divergence"),
        "entry_price_chg24":ot.get("entry_price_chg24"),
        "entry_oi_chg24":ot.get("entry_oi_chg24"),
        "entry_oi_chg4h":ot.get("entry_oi_chg4h"),
        "entry_cvd24":ot.get("entry_cvd24"),
        "entry_lls24":ot.get("entry_lls24"),
        "entry_fr_oiw":ot.get("entry_fr_oiw"),
        "entry_oi_vol_ratio":ot.get("entry_oi_vol_ratio"),
        "entry_oi_mktcap_ratio":ot.get("entry_oi_mktcap_ratio"),
        "entry_liq_short24":ot.get("entry_liq_short24"),
        "entry_liq_long24":ot.get("entry_liq_long24"),
        "entry_ls_accounts":ot.get("entry_ls_accounts"),
        "entry_btc_corr7d":ot.get("entry_btc_corr7d"),
        "entry_market_phase":ot.get("entry_market_phase"),

        # Research-only entry derivatives
        "entry_oi_price_regime": ot.get("entry_oi_price_regime"),
        "entry_short_liq_share24": ot.get("entry_short_liq_share24"),
        "entry_liq_imbalance": ot.get("entry_liq_imbalance"),
        "entry_funding_oi_pressure": ot.get("entry_funding_oi_pressure"),
        "entry_liquidation_intensity": ot.get("entry_liquidation_intensity"),
        "entry_fr_oiw_zscore": ot.get("entry_fr_oiw_zscore"),

        "fee_pct":FEE_PCT,
        "gross_pnl_pct":gross,"strategy_pnl_pct":strategy_pnl,
        "max_pnl_pct":max_pnl,"min_pnl_pct":min_pnl,
        "mfe_pct":max_pnl,"mae_pct":min_pnl,
        "drawdown_from_peak_pct":drawdown,"time_to_peak_min":time_to_peak,
    }
    for h in TRADE_HORIZONS:
        rec[f"return_{h}m"]=ot.get(f"return_{h}m")
        rec[f"return_{h}m_available"]=bool(ot.get(f"return_{h}m_available"))
    rec["win_60m"]=winflag(60)
    rec["win_120m"]=winflag(120)
    PENDING.append({"rec":rec,"entry_ts":ot["entry_ts"],"symbol":symbol,"entry_price":ep})
    log.info(f"[{symbol}] TRADE → PENDING {exit_reason} strat={strategy_pnl} hold={hold_min}m outcome_unknown={outcome_unknown}")
    send_tg(format_trade_close(rec))


# ============================================================
# EXECUTION POLICY (BingX)
# ============================================================

def _execution_reduce_to_fraction(ot, symbol, cur_price, target_fraction, tag, note):
    bx = ot.get("bingx") or {}
    if bx.get("status") not in ("opened", "already_open"):
        return False
    qty_initial = safe(bx.get("qty_initial"), 0.0)
    qty_remaining = safe(bx.get("qty_remaining"), qty_initial)
    if qty_initial <= 0 or qty_remaining <= 0:
        return False
    floor_fraction = max(target_fraction, EXECUTION_MIN_REMAINING_FRACTION)
    qty_to_close = qty_remaining - qty_initial * floor_fraction
    if qty_to_close <= 0:
        return False
    try:
        import bingx_client
    except Exception as e:
        log.error(f"[{symbol}] EXEC_REDUCE({tag}): bingx_client недоступен: {e}")
        return False
    limits = bingx_client.contract_limits(symbol)
    if not limits.get("found"):
        return False
    prec = limits["quantity_precision"]
    min_qty = max(limits["min_qty"], PARTIAL_MIN_QTY)
    min_notional = max(limits["min_notional"], PARTIAL_MIN_NOTIONAL)
    qty_to_close = bingx_client._round_qty(qty_to_close, prec)
    if qty_to_close <= 0 or qty_to_close < min_qty:
        return False
    if cur_price and qty_to_close * cur_price < min_notional:
        return False
    try:
        real_amt = bingx_client.position_amt(symbol)
        if real_amt < qty_to_close:
            log.warning(f"[{symbol}] EXEC_REDUCE({tag}) skipped: real_amt={real_amt:.8f} < qty_to_close={qty_to_close:.8f}")
            return False
        res = bingx_client.close_long(symbol, qty_to_close)
    except Exception as e:
        log.error(f"[{symbol}] EXEC_REDUCE({tag}) exception: {e}")
        return False
    if res.get("status") != "closed":
        log.error(f"[{symbol}] EXEC_REDUCE({tag}) failed: {res.get('error')}")
        return False
    closed_qty = safe(res.get("qty"), qty_to_close)
    qty_remaining -= closed_qty
    bx["qty_remaining"] = qty_remaining
    ot["bingx"] = bx
    remaining_pct = qty_remaining / qty_initial * 100 if qty_initial else 0.0
    log.info(f"[{symbol}] EXEC_REDUCE({tag}): closed {closed_qty:.8f} @ {cur_price} remaining={qty_remaining:.8f} ({remaining_pct:.1f}%)")
    log_execution_event(symbol, f"reduce_{tag}", closed_qty=closed_qty, price=cur_price, remaining=qty_remaining)
    send_tg(f"🛡 <b>{esc(ot.get('name', symbol))} ({esc(symbol)})</b> — {esc(note)}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Закрыто: {closed_qty:.8f} @ {fmt_price(cur_price)}\n"
            f"Осталось: {qty_remaining:.8f} ({remaining_pct:.1f}%)\n")
    return True

def _step_partial_take_profit(ot, symbol, cur_price):
    bx = ot.get("bingx") or {}
    if bx.get("status") not in ("opened", "already_open") or not cur_price:
        return False
    qty_initial = safe(bx.get("qty_initial"), 0.0)
    qty_remaining = safe(bx.get("qty_remaining"), qty_initial)
    entry_price = ot.get("entry_price")
    if qty_initial <= 0 or qty_remaining <= 0 or not entry_price:
        return False
    try:
        import bingx_client
    except Exception as e:
        log.error(f"[{symbol}] PARTIAL_TP: bingx_client недоступен: {e}")
        return False
    limits = bingx_client.contract_limits(symbol)
    if not limits.get("found"):
        return False
    prec = limits["quantity_precision"]
    min_qty = max(limits["min_qty"], PARTIAL_MIN_QTY)
    min_notional = max(limits["min_notional"], PARTIAL_MIN_NOTIONAL)
    pnl_pct = (cur_price - entry_price) / entry_price * 100
    legs_done = set(bx.get("partial_legs_done", []))
    for level in PARTIAL_TP_LEVELS:
        leg_name = level.get("leg")
        if not leg_name or leg_name in legs_done:
            continue
        min_pnl_pct = level.get("min_pnl_pct")
        close_fraction = level.get("close_fraction", 0.0)
        if min_pnl_pct is None or close_fraction <= 0 or pnl_pct < min_pnl_pct:
            continue
        qty_to_close = min(qty_initial * close_fraction, qty_remaining)
        qty_to_close = bingx_client._round_qty(qty_to_close, prec)
        if qty_to_close <= 0 or qty_to_close < min_qty:
            continue
        if qty_to_close * cur_price < min_notional:
            continue
        try:
            real_amt = bingx_client.position_amt(symbol)
            if real_amt < qty_to_close:
                log.warning(f"[{symbol}] PARTIAL_TP {leg_name} skipped: real_amt={real_amt:.8f} < qty_to_close={qty_to_close:.8f}")
                continue
            res = bingx_client.close_long(symbol, qty_to_close)
        except Exception as e:
            log.error(f"[{symbol}] PARTIAL_TP {leg_name} exception: {e}")
            continue
        if res.get("status") == "closed":
            closed_qty = safe(res.get("qty"), qty_to_close)
            qty_remaining -= closed_qty
            legs_done.add(leg_name)
            bx["qty_remaining"] = qty_remaining
            bx["partial_legs_done"] = sorted(legs_done)
            ot["bingx"] = bx
            remaining_pct = qty_remaining / qty_initial * 100 if qty_initial else 0.0
            log.info(f"[{symbol}] PARTIAL_TP {leg_name}: closed {closed_qty:.8f} @ {cur_price} pnl={pnl_pct:+.2f}% remaining={qty_remaining:.8f}")
            log_execution_event(symbol, f"partial_tp_{leg_name}", closed_qty=closed_qty, price=cur_price,
                                 pnl_pct=round(pnl_pct,3), remaining=qty_remaining)
            send_tg(f"💰 <b>{esc(ot.get('name', symbol))} ({esc(symbol)})</b> — частичная фиксация\n"
                    f"━━━━━━━━━━━━━━━━━━\nLeg: <b>{esc(leg_name)}</b>\n"
                    f"Закрыто: {closed_qty:.8f} @ {fmt_price(cur_price)}\nPnL: <b>{pnl_pct:+.2f}%</b>\n"
                    f"Осталось: {qty_remaining:.8f} ({remaining_pct:.1f}%)\n")
            return True
        elif res.get("status") == "skipped":
            log.info(f"[{symbol}] PARTIAL_TP {leg_name} skipped by exchange: {res.get('error')}")
        else:
            log.error(f"[{symbol}] PARTIAL_TP {leg_name} failed: {res.get('error')}")
    return False

def _step_break_even(ot, symbol, cur_price):
    entry_price = ot.get("entry_price")
    if not cur_price or not entry_price:
        return False
    ex = ot.setdefault("execution", {})
    be = ex.setdefault("break_even", {"armed": False, "done": False})
    if be["done"]:
        return False
    pnl_pct = (cur_price - entry_price) / entry_price * 100
    if not be["armed"]:
        if pnl_pct >= BREAK_EVEN_TRIGGER_PCT:
            be["armed"] = True
            log.info(f"[{symbol}] BREAK_EVEN armed at pnl={pnl_pct:+.2f}%")
        return False
    trigger_price = entry_price * (1 + BREAK_EVEN_BUFFER_PCT / 100)
    if cur_price > trigger_price:
        return False
    ok = _execution_reduce_to_fraction(ot, symbol, cur_price, BREAK_EVEN_REDUCE_TO_FRACTION,
                                        tag="break_even", note="Break Even — защита капитала")
    if ok:
        be["done"] = True
        be["triggered_at_price"] = cur_price
        be["triggered_pnl_pct"] = round(pnl_pct, 3)
    return ok

def _step_trailing_stop(ot, symbol, cur_price):
    entry_price = ot.get("entry_price")
    if not cur_price or not entry_price:
        return False
    ex = ot.setdefault("execution", {})
    tr = ex.setdefault("trailing", {"active": False, "peak_price": None, "steps_done": 0})
    pnl_pct = (cur_price - entry_price) / entry_price * 100
    if not tr["active"]:
        if pnl_pct >= TRAILING_ACTIVATE_PCT:
            tr["active"] = True
            tr["peak_price"] = cur_price
            log.info(f"[{symbol}] TRAILING activated at pnl={pnl_pct:+.2f}%")
        return False
    if cur_price > (tr["peak_price"] or cur_price):
        tr["peak_price"] = cur_price
        return False
    peak = tr["peak_price"]
    drawdown_pct = (peak - cur_price) / peak * 100 if peak else 0.0
    if drawdown_pct < TRAILING_DRAWDOWN_PCT:
        return False
    bx = ot.get("bingx") or {}
    qty_initial = safe(bx.get("qty_initial"), 0.0)
    qty_remaining = safe(bx.get("qty_remaining"), qty_initial)
    current_fraction = (qty_remaining / qty_initial) if qty_initial else 0.0
    target_fraction = max(current_fraction - TRAILING_REDUCE_STEP_FRACTION, EXECUTION_MIN_REMAINING_FRACTION)
    if target_fraction >= current_fraction:
        return False
    ok = _execution_reduce_to_fraction(ot, symbol, cur_price, target_fraction,
                                        tag=f"trailing_step_{tr['steps_done']+1}",
                                        note=f"Trailing Stop — откат {drawdown_pct:.1f}% от пика")
    if ok:
        tr["steps_done"] += 1
        tr["peak_price"] = cur_price
    return ok

def _step_reduce_exposure_time(ot, symbol, cur_price, ts):
    if not cur_price:
        return False
    age_min = (ts - ot["entry_ts"]) / 60
    ex = ot.setdefault("execution", {})
    re_state = ex.setdefault("reduce_exposure_time", {"levels_done": []})
    levels_done = set(re_state["levels_done"])
    triggered = False
    for level in REDUCE_EXPOSURE_TIME_LEVELS:
        key = f"after_{level['after_min']}m"
        if key in levels_done or age_min < level["after_min"]:
            continue
        ok = _execution_reduce_to_fraction(ot, symbol, cur_price, level["reduce_to_fraction"],
                                            tag=key, note=f"Reduce Exposure — {level['after_min']} мин в сделке")
        if ok:
            levels_done.add(key)
            re_state["levels_done"] = sorted(levels_done)
            triggered = True
    return triggered

def _step_position_health(ot, symbol, cur_price, ts):
    ex = ot.setdefault("execution", {})
    health = ex.setdefault("health", {"last_report_ts": 0})
    if ts - health["last_report_ts"] < POSITION_HEALTH_CHECK_MIN * 60:
        return
    health["last_report_ts"] = ts
    entry_price = ot.get("entry_price")
    pnl_pct = ((cur_price - entry_price) / entry_price * 100) if (cur_price and entry_price) else None
    age_min = round((ts - ot["entry_ts"]) / 60, 1)
    max_pnl = ot.get("max_pnl_pct")
    bx = ot.get("bingx") or {}
    qty_initial = safe(bx.get("qty_initial"), 0.0)
    qty_remaining = safe(bx.get("qty_remaining"), qty_initial)
    remaining_pct = (qty_remaining / qty_initial * 100) if qty_initial else None
    legs = bx.get("partial_legs_done", [])
    log.info(f"[{symbol}] POSITION_HEALTH age={age_min}m pnl={pnl_pct} peak={max_pnl} remaining={remaining_pct}")
    send_tg(f"📊 <b>{esc(ot.get('name', symbol))} ({esc(symbol)})</b> — health-check\n"
            f"━━━━━━━━━━━━━━━━━━\nВ сделке: {age_min} мин\n"
            f"PnL сейчас: {fmt_pct(pnl_pct)} · пик {fmt_pct(max_pnl)}\n"
            f"Остаток позиции: {fmt_num(remaining_pct,'%',0)}\n"
            f"Legs: {esc(', '.join(legs) if legs else '—')}\n")

def process_execution_policy(ot, symbol, cur_price, ts):
    """Возвращает True, если было хотя бы одно подтверждённое биржевое
    событие (partial TP / break-even / trailing / reduce-by-time) — сигнал
    вызывающему коду немедленно сохранить watchlist (ФИКС-3)."""
    if not (ENABLE_BINGX and cur_price):
        return False
    changed = False
    if ENABLE_PARTIAL_BINGX:
        if _step_partial_take_profit(ot, symbol, cur_price): changed = True
    if ENABLE_BREAK_EVEN:
        if _step_break_even(ot, symbol, cur_price): changed = True
    if ENABLE_TRAILING:
        if _step_trailing_stop(ot, symbol, cur_price): changed = True
    if ENABLE_REDUCE_EXPOSURE_TIME:
        if _step_reduce_exposure_time(ot, symbol, cur_price, ts): changed = True
    if ENABLE_POSITION_HEALTH:
        _step_position_health(ot, symbol, cur_price, ts)
    return changed


# ============================================================
# PENDING FLUSH / RECONCILE
# ============================================================

def flush_pending(price_full,now,existing_trade_ids):
    global PENDING
    grace=PENDING_GRACE_MIN*60; wait_max=(max(TRADE_HORIZONS)+PENDING_WAIT_MAX_MIN)*60; still=[]
    for item in PENDING:
        rec=item["rec"]; sym,ep,ets=item["symbol"],item["entry_price"],item["entry_ts"]
        tid=rec.get("trade_id")
        if tid and tid in existing_trade_ids:
            log.warning(f"[{sym}] DUP_FINALIZED_SKIP trade_id={tid}"); continue
        for h in TRADE_HORIZONS:
            key=f"return_{h}m"
            if rec.get(key) is None and now>=ets+h*60:
                ph=price_at(price_full,sym,ets+h*60,max_lag_sec=HORIZON_MAX_LAG_MIN*60)
                if ph and ep:
                    rec[key]=round((ph-ep)/ep*100,3)
                    rec[f"{key}_available"]=True
        for h in (60,120):
            v=rec.get(f"return_{h}m")
            rec[f"win_{h}m"]=(1 if (v is not None and v>=TRADE_WIN_PCT) else (0 if v is not None else None))
        ready=all(rec.get(f"return_{h}m") is not None or now>=ets+h*60+grace for h in TRADE_HORIZONS) or now>=ets+wait_max
        if ready:
            all_complete=all(rec.get(f"return_{h}m") is not None for h in TRADE_HORIZONS)
            if all_complete: rec["pending_finalize_reason"]="COMPLETE"
            elif now>=ets+wait_max: rec["pending_finalize_reason"]="WAIT_TIMEOUT"
            else: rec["pending_finalize_reason"]="MISSING_PRICE"
            append_jsonl(TRADES_FILE,rec)
            if tid: existing_trade_ids.add(tid)
            log.info(f"[{sym}] TRADE FINALIZED trade_id={rec['trade_id']} reason={rec['pending_finalize_reason']}")
        else: still.append(item)
    PENDING=still

TG_STATES={"CONFIRMED_TREND","ACCELERATION","EXHAUSTION","DISTRIBUTION"}
ACTIVE_STATES={"CONFIRMED_TREND","ACCELERATION","EXHAUSTION","DISTRIBUTION"}
ENTRY_STATES={"CONFIRMED_TREND","ACCELERATION"}
CLOSE_STATES={"EXHAUSTION","DISTRIBUTION"}

def reconcile_exchange(wl_all,ts,price_full,existing_trade_ids,lifecycle_state):
    if not ENABLE_BINGX:
        return {"status":"skipped","reason":"ENABLE_BINGX=false"}
    try:
        import bingx_client
    except Exception as e:
        log.error(f"reconcile: bingx_client недоступен: {e}")
        return {"status":"error","error":str(e)[:200]}
    res=bingx_client.list_positions()
    if res.get("status")!="ok":
        log.error(f"reconcile: запрос позиций не удался: {res.get('error')}")
        send_tg(f"⚠️ <b>Сверка с биржей не выполнена</b>\n{esc(str(res.get('error'))[:200])}\n"
                f"<i>Расхождения журнала и биржи в этом прогоне не проверены.</i>")
        return {"status":"error","error":res.get("error")}
    exch=res["positions"]
    journal={}
    for sym,entry in wl_all.items():
        ot=entry.get("open_trade")
        if not ot: continue
        bx=ot.get("bingx") or {}
        journal[sym]={"qty":bx.get("qty_remaining",bx.get("qty")),"bx_symbol":bingx_client.to_bx_symbol(sym),"status":bx.get("status"),"trade_id":entry.get("trade_id")}
    ours={v["bx_symbol"]:sym for sym,v in journal.items()}
    orphans=[]; missing=[]; mismatch=[]
    for bx_sym,amt in exch.items():
        sym=ours.get(bx_sym)
        if sym is None:
            orphans.append({"bx_symbol":bx_sym,"qty":amt,"our_symbol":None})
            continue
        jq=journal[sym]["qty"]
        if jq:
            rel=abs(amt-float(jq))/max(float(jq),1e-9)
            if rel>RECONCILE_QTY_TOLERANCE:
                mismatch.append({"symbol":sym,"bx_symbol":bx_sym,"journal_qty":jq,
                                 "exchange_qty":amt,"rel_diff":round(rel,4)})
    for sym,v in journal.items():
        if v["bx_symbol"] not in exch and v["qty"]:
            missing.append({"symbol":sym,"bx_symbol":v["bx_symbol"],
                            "journal_qty":v["qty"],"bingx_status":v["status"],
                            "trade_id":v["trade_id"]})
    rec={"ts":ts,"exchange_positions":len(exch),"journal_positions":len(journal),
         "orphans":orphans,"missing":missing,"mismatch":mismatch,
         "autoclose":RECONCILE_AUTOCLOSE}
    for o in orphans:
        log.error(f"RECONCILE ORPHAN {o['bx_symbol']} qty={o['qty']} — нет в журнале")
        if RECONCILE_AUTOCLOSE:
            try:
                r=bingx_client._close_position(o["bx_symbol"],float(o["qty"]))
                o["autoclose_result"]=r
                log.info(f"RECONCILE ORPHAN {o['bx_symbol']} автозакрытие: {r.get('status')}")
            except Exception as e:
                o["autoclose_result"]={"status":"error","error":str(e)[:200]}
    if orphans:
        lines="\n".join(f"· {esc(o['bx_symbol'])} qty={esc(o['qty'])}"
                        +(f" → {esc(o.get('autoclose_result',{}).get('status'))}" if RECONCILE_AUTOCLOSE else "")
                        for o in orphans[:12])
        send_tg(f"🔺 <b>Сверка: позиции на бирже без учёта в журнале</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"{len(orphans)} шт.\n{lines}\n"
                +("<i>Автозакрытие включено.</i>" if RECONCILE_AUTOCLOSE
                  else "<i>Автозакрытие выключено — закрыть вручную или включить "
                       "BINGX_RECONCILE_AUTOCLOSE=true.</i>"))
    for m in missing:
        sym=m["symbol"]; entry=wl_all.get(sym)
        ot=(entry or {}).get("open_trade")
        if not ot: continue
        cur=ot.get("last_price")
        log.error(f"RECONCILE MISSING {sym} — на бирже позиции нет, закрываем журнальную сделку")
        close_trade(ot,sym,ts,cur or None,"EXCHANGE_CLOSED",
                    entry.get("state","UNKNOWN"),price_full,
                    exit_price_source="last_seen" if cur else "unknown",
                    exit_candidates=["EXCHANGE_CLOSED"],lifecycle_complete=False,
                    exchange_close_status="confirmed")  # список позиций уже подтвердил отсутствие на бирже
        # ФИКС-10: раньше EXCHANGE_CLOSED был единственным путём закрытия
        # БЕЗ cooldown-бухгалтерии — теперь ведём её так же, как обычный close.
        cd=COOLDOWN_BY_EXIT_REASON.get("EXCHANGE_CLOSED",60)  # нет явного маппинга — по умолчанию как DISTRIBUTION/INVALIDATED-класс
        lrec=lifecycle_state.setdefault(sym,{})
        lrec["cooldown_until"]=ts+cd*60
        lrec["last_exit_reason"]="EXCHANGE_CLOSED"; lrec["last_exit_ts"]=ts
        lrec.setdefault("idea_first_seen_ts",ot.get("idea_first_seen_ts") or ts)
        entry.pop("open_trade",None); entry.pop("trade_id",None)
        save_watchlist(wl_all)  # ФИКС-3: сразу на диск, не ждём конца прогона
    if missing:
        send_tg(f"🔻 <b>Сверка: позиции нет на бирже, но она была в журнале</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"{len(missing)} шт.: {esc(', '.join(m['symbol'] for m in missing[:12]))}\n"
                f"<i>Закрыты как EXCHANGE_CLOSED (cooldown применён). Возможна ликвидация или ручное закрытие.</i>")
    if mismatch:
        log.error(f"RECONCILE QTY MISMATCH: {mismatch}")
        send_tg(f"⚠️ <b>Сверка: расхождение объёмов</b>\n"
                +"\n".join(f"· {esc(m['symbol'])} журнал={esc(m['journal_qty'])} биржа={esc(m['exchange_qty'])}"
                           for m in mismatch[:10]))
    if orphans or missing or mismatch:
        append_jsonl(RECONCILE_FILE,rec)
    log.info(f"reconcile: биржа={len(exch)} журнал={len(journal)} "
             f"сирот={len(orphans)} пропавших={len(missing)} расхождений={len(mismatch)}")
    rec["status"]="ok"
    return rec


# ============================================================
# OPEN TRADE MANAGEMENT
# ============================================================

def _touch_state(ot,state,ts,score,reason):
    if state==ot.get("current_state"):
        return
    prev=ot.get("current_state","UNKNOWN")
    dur=round((ts-ot.get("current_state_start_ts",ts))/60,1)
    ot.setdefault("time_in_states",{})
    ot["time_in_states"][prev]=round(ot["time_in_states"].get(prev,0)+dur,1)
    ot["current_state"]=state
    ot["current_state_start_ts"]=ts
    ot.setdefault("state_history",[]).append({"ts":ts,"state":state,"score":score,"reason":reason})
    if STATE_RANK.get(state,0)>STATE_RANK.get(ot.get("max_state"),0):
        ot["max_state"]=state

def _update_shadows(ot,ts,pnl_pct,strength):
    sh=ot.setdefault("shadow_stops",{str(l):None for l in SHADOW_STOP_LEVELS})
    if pnl_pct is not None:
        for lvl in SHADOW_STOP_LEVELS:
            key=str(lvl)
            if sh.get(key) is None and pnl_pct<=-lvl:
                sh[key]={"ts":ts,"pnl_at_trigger":round(pnl_pct,3),
                         "minutes":round((ts-ot["entry_ts"])/60,1)}
    # ФИКС-7: раньше этот блок исполнялся безусловно, независимо от
    # USE_SCHMITT — сам флаг ни на что не влиял. Теперь реально гейтит.
    if strength is not None and USE_SCHMITT:
        on=ot.get("shadow_schmitt_on",True)
        on=(strength>=SCHMITT_ENTER) if not on else (strength>=SCHMITT_EXIT)
        if ot.get("shadow_schmitt_on") and not on and ot.get("shadow_schmitt_exit_ts") is None:
            ot["shadow_schmitt_exit_ts"]=ts
            ot["shadow_schmitt_exit_min"]=round((ts-ot["entry_ts"])/60,1)
        ot["shadow_schmitt_on"]=on
        ot["last_signal_strength"]=strength
    elif strength is not None:
        # USE_SCHMITT=False: по-прежнему пишем силу сигнала для видимости
        # в записи сделки, но не трогаем shadow_schmitt_on/exit_ts.
        ot["last_signal_strength"]=strength

def manage_open_trade(sym,ot,ts,cur_price,signal,price_full,missed_runs=0):
    """Возвращает (close_reason, all_triggered, exit_price, exit_price_source,
    execution_changed). execution_changed=True → вызывающий код должен
    немедленно сохранить watchlist (ФИКС-3)."""
    state=(signal or {}).get("state")
    score=(signal or {}).get("score",0)
    strength=(signal or {}).get("strength")
    if cur_price:
        ot["last_price"]=cur_price
        ot["last_price_ts"]=ts
        ep=ot.get("entry_price")
        if ep:
            pnl_now=(cur_price-ep)/ep*100
            if pnl_now>ot.get("max_pnl_pct",0.0): ot["max_pnl_pct"]=pnl_now; ot["peak_ts"]=ts
            if pnl_now<ot.get("min_pnl_pct",0.0): ot["min_pnl_pct"]=pnl_now
    else:
        dq=ot.setdefault("data_quality",{})
        dq["missing_snapshots"]=dq.get("missing_snapshots",0)+1
    price_age_min=round((ts-ot.get("last_price_ts",ts))/60,1)
    dq=ot.setdefault("data_quality",{})
    dq["max_price_age_min"]=max(dq.get("max_price_age_min",0),price_age_min)
    dq["exit_price_age_min"]=price_age_min
    _touch_state(ot,state or "NO_DATA",ts,score,
                 "state_transition" if state else "out_of_universe")
    _fill_horizons(ot,sym,ts,price_full)
    ep=ot.get("entry_price")
    pnl_pct=((cur_price-ep)/ep*100) if (cur_price and ep) else None
    _update_shadows(ot,ts,pnl_pct,strength)
    if state in ("CONFIRMED_TREND","ACCELERATION") or (state and score>=5):
        ot["last_signal_ts"]=ts
    last_signal_ts=ot.get("last_signal_ts",ot["entry_ts"])
    cand={
        "INVALIDATED": state=="INVALIDATED",
        "EXHAUSTION":  state=="EXHAUSTION",
        "DISTRIBUTION":state=="DISTRIBUTION",
        "STOP_LOSS":   pnl_pct is not None and pnl_pct<=-STOP_LOSS_PCT,
        "SIGNAL_DECAY":ts-last_signal_ts>=SIGNAL_DECAY_MIN*60,
        "TIMEOUT":     ts-ot["entry_ts"]>=TRADE_TIMEOUT_MIN*60,
        "DATA_STALE":  price_age_min>=PRICE_STALE_EXIT_MIN,
        "MISSED":      missed_runs>=MISS_REMOVE_RUNS,
        "NEUTRAL":     False,
    }
    close_reason,all_triggered=resolve_exit_reason(cand)
    if not close_reason:
        execution_changed=process_execution_policy(ot, sym, cur_price, ts)
        return None,[],None,None,execution_changed
    exit_price=cur_price or ot.get("last_price")
    src="live" if cur_price else "last_seen"
    return close_reason,all_triggered,exit_price,src,False


# ============================================================
# MAIN RUN LOOP
# ============================================================

def run():
    log.info("═══ Прогон ═══")
    _ensure_stealth_available()  # ФИКС-8
    wl_all=load_watchlist()
    global PENDING
    PENDING=load_pending()
    existing_trade_ids=load_existing_trade_ids()
    lifecycle_state=load_lifecycle_state()
    global DISCOVERY
    DISCOVERY=discovery_fingerprint()
    log.info(f"Discovery: sha={DISCOVERY['url_sha256']} filter={str(DISCOVERY.get('filter'))[:120]}")
    log_discovery_change(DISCOVERY,now_ts())
    rows=fetch_data()
    log.info(f"Монет после discovery-фильтра: {len(rows)}")
    market=detect_market_phase(rows)
    log.info(f"Market: {market['phase']} btc={market.get('btc_chg24')} "
             f"src={market.get('btc_source')} breadth={market.get('breadth_ratio')} "
             f"mod={market['modifier']} (raw={market.get('modifier_raw')})")
    current_symbols={r["symbol"] for r in rows}
    ts=now_ts()
    for r in rows:
        append_jsonl(HEARTBEAT_FILE,{"ts":ts,"symbol":r["symbol"],"price":r.get("price")})
        sym=r["symbol"]
        append_jsonl(MARKET_HISTORY_FILE,{**r,"lifecycle_state":wl_all.get(sym,{}).get("state")})
    history_all=load_market_history()
    price_full=load_price_full()
    try:
        reconcile_exchange(wl_all,ts,price_full,existing_trade_ids,lifecycle_state)
    except Exception as e:
        log.exception(f"reconcile упал: {e}")
        send_tg(f"⚠️ <b>Сверка с биржей упала</b>\n{esc(str(e)[:200])}")

    # ФИКС-1: ретрай ранее неподтверждённых закрытий — ДО обработки новых
    # сигналов. Если reconcile выше уже закрыл позицию как EXCHANGE_CLOSED
    # (потому что она реально пропала с биржи), open_trade для неё уже
    # отсутствует, и этот блок для неё естественным образом ничего не делает.
    for sym in list(wl_all.keys()):
        entry=wl_all[sym]
        ot=entry.get("open_trade")
        if not ot or "pending_close" not in ot:
            continue
        ot=dict(ot)
        exch_status=_attempt_exchange_close(ot,sym)
        if exch_status=="unconfirmed":
            entry["open_trade"]=ot
            log.warning(f"[{sym}] pending_close всё ещё не подтверждён биржей")
            continue
        pc=ot.pop("pending_close")
        close_trade(ot,sym,ts,pc["xprice"],pc["reason"],pc["exit_state"],price_full,
                    exit_price_source=pc["xsrc"],exit_candidates=pc["triggered"],
                    lifecycle_complete=pc["lifecycle_complete"],
                    exchange_close_status=exch_status)
        cd=COOLDOWN_BY_EXIT_REASON.get(pc["reason"],0)
        lrec=lifecycle_state.setdefault(sym,{})
        if cd>0: lrec["cooldown_until"]=ts+cd*60
        lrec["last_exit_reason"]=pc["reason"]; lrec["last_exit_ts"]=ts
        lrec.setdefault("idea_first_seen_ts",ot.get("idea_first_seen_ts") or ts)
        entry.pop("open_trade",None); entry.pop("trade_id",None)
        log.info(f"[{sym}] CLOSE (подтверждено с задержкой) {pc['reason']}")
        save_watchlist(wl_all)  # ФИКС-3

    signals={}
    for sym,hist in history_all.items():
        if not hist or sym not in current_symbols: continue
        r=hist[-1]
        raw_score,pros,cons=calculate_score(r)
        score=clamp(raw_score+market["modifier"],0,10)
        derived=calc_derived(hist)
        momentum,mom_tags=calc_momentum(derived)
        pattern=detect_pattern(r,derived,momentum)
        prev_state=wl_all.get(sym,{}).get("state","NEUTRAL")
        state,reasons,warnings,path=detect_lifecycle(sym,hist,score,derived,prev_state)
        conf=calc_confidence(state,hist,score,derived,market["modifier"])
        early_val,early_label=entry_earliness(r)
        recent=hist[-MIN_SNAPS_LIFECYCLE:] if len(hist)>=MIN_SNAPS_LIFECYCLE else hist
        signals[sym]={
            "row":r,"hist":hist,"state":state,"prev_state":prev_state,
            "reasons":reasons,"warnings":warnings,"path":path,
            "score":score,"pros":pros,"cons":cons,"derived":derived,
            "momentum":momentum,"mom_tags":mom_tags,"pattern":pattern,"conf":conf,
            "early_val":early_val,"early_label":early_label,
            "price":r.get("price") if valid_price(r.get("price")) else None,
            "strength":signal_strength(hist,derived["cvd_momentum"]),
            "window":window_quality(recent),"history_len":len(hist),
            "shadow":shadow_variants(hist,derived["cvd_momentum"]),
        }
    for sym,sig in signals.items():
        sh=sig["shadow"]
        if not sh.get("disagrees"): continue
        append_jsonl(SHADOW_SIGNALS_FILE,{
            "ts":ts,"symbol":sym,"price":sig["price"],
            "live_state":sig["state"],"prev_state":sig["prev_state"],
            "allowed_confirmed":sig["prev_state"] in ALLOWED_FROM.get("CONFIRMED_TREND",set()),
            "score":sig["score"],"momentum":sig["momentum"],
            "strength":sh["strength"],"window":sh["window"],
            "history_len":sig["history_len"],
            "variants":{k:sh[k] for k,_ in SHADOW_VARIANTS},
            "signal_logic_version":SIGNAL_LOGIC_VERSION,
            "conditions_version":CONDITIONS_CONFIG["conditions_version"],
        })
    for sym,entry in wl_all.items():
        if sym in current_symbols:
            entry["missed_runs"]=0
            entry["last_seen"]=ts
        else:
            entry["missed_runs"]=entry.get("missed_runs",0)+1
            entry["last_seen"]=entry.get("last_seen",ts)
            if entry["state"] in ACTIVE_STATES and entry["missed_runs"]==MISS_EXIT_RUNS:
                log.info(f"[{sym}] выпала из фильтра {entry['missed_runs']} прогона → пауза")
                send_tg(f"⏸ <b>{esc(entry.get('name',sym))} ({esc(sym)})</b>\n━━━━━━━━━━━━━━━━━━\n"
                        f"Наблюдение на паузе ({esc(entry['state'])})\n"
                        f"Выпала из discovery-фильтра {entry['missed_runs']} прогона подряд\n"
                        f"<i>Стадия сохранена — при возврате пересчитается.</i>\n")
    for sym in list(wl_all.keys()):
        entry=wl_all[sym]
        ot=entry.get("open_trade")
        if not ot or "pending_close" in ot: continue  # уже обработано выше в этом прогоне
        ot=dict(ot)
        sig=signals.get(sym)
        cur_price=sig["price"] if sig else None
        reason,triggered,xprice,xsrc,exec_changed=manage_open_trade(
            sym,ot,ts,cur_price,sig,price_full,missed_runs=entry.get("missed_runs",0))
        if reason:
            if not xprice:
                # ФИКС-5: раньше здесь подставлялся entry_price и получался
                # фиктивный PnL=0%. Теперь честно: цена неизвестна.
                log.error(f"[{sym}] нет цены для закрытия — сделка будет помечена outcome_unknown")
                xprice=None; xsrc="unknown"
            complete=reason not in ("DATA_STALE","MISSED")
            exit_state=(sig or {}).get("state") or entry.get("state","UNKNOWN")

            # ФИКС-1: сначала подтверждаем закрытие на бирже, ЗАТЕМ финализируем
            # локальную запись. Если биржа не подтвердила — сделка остаётся
            # открытой (pending_close), ретрай в начале следующего прогона.
            exch_status=_attempt_exchange_close(ot,sym)
            if exch_status=="unconfirmed":
                ot["pending_close"]={
                    "reason":reason,"triggered":triggered,"ts":ts,
                    "xprice":xprice,"xsrc":xsrc,"exit_state":exit_state,
                    "lifecycle_complete":complete,
                }
                entry["open_trade"]=ot
                log.error(f"[{sym}] CLOSE NOT CONFIRMED reason={reason} — оставляем как открытую, ждём подтверждения")
                save_watchlist(wl_all)  # ФИКС-3
            else:
                close_trade(ot,sym,ts,xprice,reason,exit_state,price_full,
                            exit_price_source=xsrc,exit_candidates=triggered,
                            lifecycle_complete=complete,
                            exchange_close_status=exch_status)
                cd=COOLDOWN_BY_EXIT_REASON.get(reason,0)
                rec=lifecycle_state.setdefault(sym,{})
                if cd>0: rec["cooldown_until"]=ts+cd*60
                rec["last_exit_reason"]=reason; rec["last_exit_ts"]=ts
                rec.setdefault("idea_first_seen_ts",ot.get("idea_first_seen_ts") or ts)
                entry.pop("open_trade",None); entry.pop("trade_id",None)
                log.info(f"[{sym}] CLOSE {reason} triggered={triggered} cooldown={cd}м")
                save_watchlist(wl_all)  # ФИКС-3
        else:
            entry["open_trade"]=ot
            if exec_changed:
                # ФИКС-3: немедленная персистентность после подтверждённого
                # частичного исполнения (partial TP / break-even / trailing).
                save_watchlist(wl_all)
                log.info(f"[{sym}] watchlist сохранён немедленно после execution-события")
    for sym,sig in signals.items():
        r=sig["row"]; hist=sig["hist"]; state=sig["state"]; prev_state=sig["prev_state"]
        score=sig["score"]; derived=sig["derived"]; conf=sig["conf"]
        existing=wl_all.get(sym,{})
        has_trade=existing.get("open_trade") is not None
        if state=="NEUTRAL":
            if sym in wl_all and not has_trade:
                old=existing.get("state","NEUTRAL")
                nr=existing.get("neutral_runs",0)+1
                existing["neutral_runs"]=nr; existing["last_seen"]=ts
                if old in ACTIVE_STATES and nr<NEUTRAL_HYSTERESIS:
                    log.info(f"[{sym}] {old}: NEUTRAL-оценка {nr}/{NEUTRAL_HYSTERESIS}")
                else:
                    if old in ACTIVE_STATES:
                        send_tg(f"⚪ <b>{esc(existing.get('name',sym))} ({esc(sym)})</b>\n━━━━━━━━━━━━━━━━━━\n"
                                f"{esc(old)} → NEUTRAL\n<i>Снята: условия тренда не выполняются {nr} прогона подряд</i>\n")
                    log.info(f"[{sym}] {old} → NEUTRAL, remove")
                    del wl_all[sym]
            elif has_trade:
                existing["neutral_runs"]=existing.get("neutral_runs",0)+1
                existing["last_seen"]=ts
                existing["state_signal"]="NEUTRAL"
            continue
        if state=="INVALIDATED":
            if sym in wl_all and not wl_all[sym].get("open_trade"):
                send_tg(f"❌ <b>{esc(existing.get('name',sym))} ({esc(sym)})</b>\n━━━━━━━━━━━━━━━━━━\n"
                        f"{esc(existing.get('state','?'))} → INVALIDATED\n"
                        f"<i>Сценарий сломан: {esc(' · '.join(sig['reasons'][:2]))}</i>\n")
                log.info(f"[{sym}] INVALIDATED → remove")
                del wl_all[sym]
            continue
        new_ot=existing.get("open_trade"); new_tid=existing.get("trade_id")
        if new_ot is None and state in ENTRY_STATES and sig["price"]:
            info=lifecycle_state.get(sym,{})
            if info.get("cooldown_until",0)>ts:
                left=round((info["cooldown_until"]-ts)/60,1)
                log.info(f"[{sym}] COOLDOWN {left}м (после {info.get('last_exit_reason')}), вход пропущен")
            else:
                idea_ts=info.get("idea_first_seen_ts") or ts
                new_tid=_new_trade_id(sym, ts)   # ФИКС-9
                new_ot=open_trade_record(r,ts,state,sig["path"],score,sig["momentum"],conf,
                                         sig["early_val"],sig["early_label"],sig["pattern"],
                                         derived,market,idea_ts,existing.get("snapshots",0)+1,
                                         sig["price"],history_len=sig["history_len"],
                                         window=sig["window"],strength=sig["strength"],
                                         shadow=sig["shadow"],hist=sig["hist"])
                new_ot["trade_id_full"]=new_tid
                if ENABLE_BINGX and new_ot is not None:
                    try:
                        import bingx_client
                        bx_symbol = bingx_client.to_bx_symbol(sym)
                        if not bingx_client.contract_exists(sym):
                            new_ot["bingx"] = {
                                "status": "skipped",
                                "reason": "contract_not_found",
                                "symbol": bx_symbol
                            }
                            log.info(f"[{sym}] BingX SKIP: контракт {bx_symbol} не найден")
                        else:
                            cur_price_entry = sig["price"]
                            bx = bingx_client.open_long(sym, cur_price_entry)
                            if bx.get("status") in ("opened", "already_open"):
                                qty_opened = safe(bx.get("qty"), 0.0)
                                bx["qty_initial"] = qty_opened
                                bx["qty_remaining"] = qty_opened
                                bx["partial_legs_done"] = []
                                if bx.get("status") == "already_open":
                                    # ФИКС-4: НЕ доверяем молча — это может быть
                                    # остаток уже частично закрытой позиции из
                                    # потерянной записи, а не исходный размер.
                                    bx["qty_initial_uncertain"] = True
                                    log.error(f"[{sym}] BingX ALREADY_OPEN при входе — qty_initial "
                                              f"взят из текущего снимка биржи ({qty_opened}), это может "
                                              f"быть остаток прошлой частично закрытой позиции. "
                                              f"Проверить вручную.")
                                    send_tg(f"⚠️ <b>{esc(r.get('name',sym))} ({esc(sym)})</b> — "
                                            f"обнаружена уже открытая позиция при входе\n"
                                            f"━━━━━━━━━━━━━━━━━━\nqty из снимка биржи: {esc(qty_opened)}\n"
                                            f"<i>qty_initial мог быть занижен, если это остаток прошлой "
                                            f"частичной сделки — partial TP/trailing будут считаться от "
                                            f"этого значения. Проверить вручную.</i>\n")
                                log.info(f"[{sym}] BingX OPEN {bx.get('status')} orderId={bx.get('order_id')} qty={qty_opened}")
                                log_execution_event(sym, "open_confirmed", status=bx.get("status"),
                                                     qty=qty_opened, order_id=bx.get("order_id"),
                                                     qty_initial_uncertain=bx.get("qty_initial_uncertain", False))
                            else:
                                log.error(f"[{sym}] BingX OPEN failed: {bx.get('error')}")
                            new_ot["bingx"] = bx
                    except Exception as e:
                        log.error(f"[{sym}] BingX OPEN exception: {e}")
                        try:
                            new_ot["bingx"] = {"status": "error", "error": str(e)}
                        except Exception:
                            pass
                log.info(f"[{sym}] TRADE OPEN {state} path={sig['path']} @ {sig['price']} "
                         f"strength={sig['strength']} window={sig['window']['span_min']}м")
        rec=lifecycle_state.setdefault(sym,{})
        rec.setdefault("idea_first_seen_ts",ts)
        entry={
            "state":state,"previous_state":prev_state,
            "action":ACTIONS.get(state,""),
            "confidence":conf,"score":score,"momentum":sig["momentum"],
            "pattern":sig["pattern"],"name":r.get("name",sym),
            "first_seen":existing.get("first_seen",ts),"last_seen":ts,
            "snapshots":existing.get("snapshots",0)+1,
            "missed_runs":0,"neutral_runs":0,
            "entry_earliness":round(sig["early_val"],2),
            "signal_strength":sig["strength"],"window":sig["window"],
            "reasons":sig["reasons"],"warnings":sig["warnings"],"mom_tags":sig["mom_tags"],
        }
        if new_ot is not None:
            entry["open_trade"]=new_ot; entry["trade_id"]=new_tid
        wl_all[sym]=entry
        if new_ot is not None:
            save_watchlist(wl_all)  # ФИКС-3: сразу после подтверждённого открытия
        if state!=prev_state:
            log.info(f"[{sym}] {prev_state} → {state} | {sig['reasons']}")
            if state in TG_STATES:
                append_jsonl(CALIBRATION_FILE,{
                    "ts":ts,"symbol":sym,"state":state,
                    "oi_accel":round(derived["oi_accel"],3),
                    "cvd_momentum":round(derived["cvd_momentum"],2),
                    "price_accel":round(derived["price_accel"],3),
                    "funding_pressure":round(derived["funding_pressure"],5),
                    "oi_trend":derived["oi_trend"],"cvd_trend":derived["cvd_trend"],
                    "score":score,"momentum":sig["momentum"],"confidence":conf,
                    "signal_strength":sig["strength"],
                    "window_span_min":sig["window"]["span_min"],
                    "window_dense":sig["window"]["dense"],
                    "signal_logic_version":SIGNAL_LOGIC_VERSION,
                    "lifecycle_engine_version":LIFECYCLE_ENGINE_VERSION,
                })
                cur={**r,"score":score,"pros":sig["pros"],"cons":sig["cons"],
                     "derived":derived,"momentum":sig["momentum"],"pattern":sig["pattern"],
                     # Research-only (только для TG и анализа, не влияют на вход)
                     "entry_oi_price_regime": calc_entry_oi_price_regime(
                         r.get("oi_chg4h_pct"), r.get("price_chg24")
                     ),
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
                         hist, r.get("fr_oiw")
                     ),
                }
                llm_res=llm_verify(sym,wl_all[sym],cur,hist)
                msg=format_signal(sym,wl_all[sym],cur,hist,sig["reasons"],sig["warnings"],market)
                if llm_res:
                    agree="✅" if llm_res.get("agree") is True else "❌"
                    msg+=f"\n🤖 {agree} {esc(llm_res.get('risk','?'))} · {esc(llm_res.get('reason',''))}"
                send_tg(msg)
    for sym in list(wl_all.keys()):
        entry=wl_all[sym]
        if sym in current_symbols: continue
        if entry.get("open_trade"): continue
        if entry.get("missed_runs",0)>=MISS_REMOVE_RUNS:
            log.info(f"[{sym}] нет в данных {entry['missed_runs']} прогонов → remove")
            del wl_all[sym]
    flush_pending(price_full,ts,existing_trade_ids)
    save_pending(PENDING)
    save_watchlist(wl_all)
    save_lifecycle_state(lifecycle_state)
    for r in rows:
        if passes_filter(r):
            sc,pr,co=calculate_score(r)
            append_jsonl(SNAPSHOTS_FILE,{**r,"score":sc,"pros":pr,"cons":co})
    cleanup_jsonl(MARKET_HISTORY_FILE,MARKET_TTL_DAYS)
    cleanup_jsonl(SNAPSHOTS_FILE,SNAPSHOTS_TTL_DAYS)
    cleanup_jsonl(HEARTBEAT_FILE,HEARTBEAT_TTL_DAYS)
    cleanup_jsonl(CALIBRATION_FILE,SNAPSHOTS_TTL_DAYS)
    cleanup_jsonl(SHADOW_SIGNALS_FILE,SHADOW_SIGNALS_TTL_DAYS)
    open_n=sum(1 for v in wl_all.values() if v.get("open_trade"))
    log.info(f"═══ Готово. Active: {len(wl_all)} · open trades: {open_n} · pending: {len(PENDING)} ═══")

if __name__=="__main__":
    try: run()
    except Exception as e:
        log.exception(f"Фатал: {e}")
        send_tg(f"⚠️ <b>Monitor</b>\n{esc(str(e)[:500])}")
        sys.exit(1)
