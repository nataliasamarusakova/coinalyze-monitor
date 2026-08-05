"""
monitor.py — event-driven research engine для оценки качества сигналов.
LIFECYCLE_ENGINE_VERSION = 2 — FREEZE. Любые изменения только через новый эксперимент.
"""
import os, sys, time, json, shutil, hashlib
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
except ImportError:
    def stealth_sync(page): pass

from conditions import check_confirmed_path_a, check_confirmed_path_b

BASE = Path(__file__).resolve().parent
MARKET_HISTORY_FILE  = BASE / "market_history.jsonl"
SNAPSHOTS_FILE       = BASE / "snapshots.jsonl"
HEARTBEAT_FILE       = BASE / "heartbeat.jsonl"
WATCHLIST_FILE       = BASE / "watchlist.json"
CALIBRATION_FILE     = BASE / "calibration.jsonl"
TRADES_FILE          = BASE / "trades.jsonl"
PENDING_FILE         = BASE / "pending_trades.jsonl"
LIFECYCLE_STATE_FILE = BASE / "lifecycle_state.json"
DEBUG_HTML_FILE      = BASE / "debug_page.html"

COINALYZE_P_SID    = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")
TG_BOT_TOKEN       = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID         = os.environ.get("TG_CHAT_ID", "")
ENABLE_LLM    = os.environ.get("ENABLE_LLM", "false").lower() == "true"
QWEN_API_KEY  = os.environ.get("QWEN_API_KEY", "")
QWEN_BASE_URL = os.environ.get("QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
QWEN_MODEL    = os.environ.get("QWEN_MODEL", "qwen-plus")
MAX_PAGES = 5  # Максимум страниц пагинации (защита от аномалий)
COINALYZE_URL = (
    "https://coinalyze.net/"
    "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
    "&filter=Y19ndF8yMDAwMDAwJmRfZ3RfMTAwMDAwMCZlX2d0XzAmc19ndF8wJmNtNjE2NV9ndF80NSZjbTYxNjRfbHRfNjA"
    "&order_by=volume_24hour&order_dir=desc"
)
ENABLE_BINGX = os.environ.get("ENABLE_BINGX", "false").lower() == "true"
MARKET_TTL_DAYS=2; SNAPSHOTS_TTL_DAYS=7; HEARTBEAT_TTL_DAYS=3
LIFECYCLE_WINDOW_MIN=90; MIN_SNAPS_LIFECYCLE=5
MISS_EXIT_RUNS=2; MISS_REMOVE_RUNS=4; NEUTRAL_HYSTERESIS=2

TRADE_SCHEMA_VERSION=2; SIGNAL_LOGIC_VERSION=1; LIFECYCLE_ENGINE_VERSION=2
ENGINE_VERSIONS={"schema":TRADE_SCHEMA_VERSION,"signal":SIGNAL_LOGIC_VERSION,"lifecycle":LIFECYCLE_ENGINE_VERSION,"created":"2026-08-04"}
HASH_VERSION="sha256_v1"
TRADE_TIMEOUT_MIN=240; FEE_PCT=0.0
STOP_MODE="fixed"; STOP_LOSS_PCT=5.0
SIGNAL_DECAY_MIN=90
IDEA_REGISTRY_TTL_DAYS=30
TRADE_HORIZONS=[30,60,120,240]; TRADE_WIN_PCT=1.0
PENDING_GRACE_MIN=10; PENDING_WAIT_MAX_MIN=60; HORIZON_MAX_LAG_MIN=15
PENDING=[]
COOLDOWN_BY_EXIT_REASON={"STOP_LOSS":120,"INVALIDATED":60,"DISTRIBUTION":60,"EXHAUSTION":45,"SIGNAL_DECAY":30,"TIMEOUT":0,"MISSED":0,"NEUTRAL":0}
PROTECTION_REASONS={"STOP_LOSS"}
EXIT_PRIORITY=["INVALIDATED","EXHAUSTION","DISTRIBUTION","STOP_LOSS","SIGNAL_DECAY","TIMEOUT","MISSED","NEUTRAL"]
EXIT_CLASS={"EXHAUSTION":"SIGNAL","INVALIDATED":"SIGNAL","DISTRIBUTION":"SIGNAL","STOP_LOSS":"PROTECTION","TIMEOUT":"LIFETIME","SIGNAL_DECAY":"LIFETIME","MISSED":"DATA","NEUTRAL":"LIFETIME"}
STATE_RANK={"ACCUMULATION":1,"EARLY_MOVE":2,"CONFIRMED_TREND":3,"ACCELERATION":4,"EXHAUSTION":5,"DISTRIBUTION":6}
EQUITY_SYMBOLS={"META","AMZN","NVDA","CRWV","AXTI","PLTR","AVGO","AAPL","TSLA","GOOGL","MSTR","COIN","BZ"}
EQUITY_HINTS=("Inc","Corp","Technologies","Platforms")
COMMODITY_SYMBOLS={"CL"}; COMMODITY_HINTS=("Crude","Oil","Gold","Silver")
logging.basicConfig(level=logging.INFO,format="%(asctime)s [%(levelname)s] %(message)s",datefmt="%Y-%m-%d %H:%M:%S")
log=logging.getLogger("monitor")

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
    sym=r.get("symbol",""); name=r.get("name","")
    if sym in COMMODITY_SYMBOLS or any(h in name for h in COMMODITY_HINTS): return "commodity"
    if sym in EQUITY_SYMBOLS or any(h in name for h in EQUITY_HINTS): return "equity"
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

def _setup_browser_context(p):
    """Создаёт browser context с cookies. Возвращает (browser, page)."""
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
    """Загружает одну страницу с полной подгрузкой динамического контента."""
    # Ждём networkidle — все AJAX-запросы завершены
    page.goto(url, wait_until="networkidle", timeout=60_000)
    page.wait_for_timeout(3000)

    if "Attention Required" in page.content():
        log.warning("Cloudflare, waiting...")
        page.wait_for_timeout(10_000)
        page.wait_for_load_state("networkidle", timeout=30_000)

    # Ждём таблицу
    page.wait_for_selector("tbody tr", timeout=25_000)
    initial_count = len(page.query_selector_all("tbody tr"))
    log.info(f"Строк после загрузки: {initial_count}")

    # Скроллим вниз и ждём стабилизации количества строк
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

    # После скролла — ждём networkidle (подгрузка могла триггернуть запросы)
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass

    # Скроллим обратно наверх
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)

    final_count = len(page.query_selector_all("tbody tr"))
    has_pagination = page.query_selector(".pagination") is not None
    log.info(f"Итого: строк={final_count}, пагинация={'есть' if has_pagination else 'нет'}")

    html_text = page.content()

    # Сохраняем HTML для отладки
    DEBUG_HTML_FILE.write_text(html_text, encoding="utf-8")
    log.info(f"HTML сохранён в {DEBUG_HTML_FILE.name} ({len(html_text)} символов)")

    return html_text

def click_next_page(page, current_page_num):
    """Фолбэк: кликаем по номеру страницы в .pagination, если href не найдены."""
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
    """Парсит блок .pagination и возвращает список URL всех страниц."""
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
    """Загружает все страницы пагинации, возвращает список всех монет.
    Один браузер на все страницы. Дедупликация по symbol."""
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

def append_jsonl(path,rec):
    with open(path,"a",encoding="utf-8") as f: f.write(json.dumps(rec,ensure_ascii=False)+"\n")
def load_jsonl(path):
    if not path.exists(): return []
    out=[]
    with open(path,"r",encoding="utf-8") as f:
        for line in f:
            line=line.strip()
            if line:
                try: out.append(json.loads(line))
                except json.JSONDecodeError: continue
    return out
def cleanup_jsonl(path,ttl_days):
    if not path.exists(): return
    cutoff=now_ts()-ttl_days*86400; recs=load_jsonl(path)
    fresh=[r for r in recs if r.get("ts",0)>cutoff]; removed=len(recs)-len(fresh)
    if removed:
        with open(path,"w",encoding="utf-8") as f:
            for r in fresh: f.write(json.dumps(r,ensure_ascii=False)+"\n")
        log.info(f"Cleanup {path.name}: -{removed}")
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
def save_watchlist(wl): WATCHLIST_FILE.write_text(json.dumps(wl,ensure_ascii=False,indent=2),encoding="utf-8")
def load_lifecycle_state():
    if not LIFECYCLE_STATE_FILE.exists(): return {}
    try: data=json.loads(LIFECYCLE_STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError: return {}
    now=time.time()
    return {sym:v for sym,v in data.items() if now-v.get("idea_first_seen_ts",0)<IDEA_REGISTRY_TTL_DAYS*86400}
def save_lifecycle_state(state): LIFECYCLE_STATE_FILE.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
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
    if bad: log.warning(f"pending_trades.jsonl: {bad} битых строк пропущено")
    seen,out=set(),[]
    for item in data:
        tid=item.get("rec",{}).get("trade_id")
        if tid and tid in seen: continue
        if tid: seen.add(tid)
        out.append(item)
    if len(out)!=len(data): log.warning(f"pending dedup: {len(data)}→{len(out)}")
    return out
def save_pending(pending):
    with open(PENDING_FILE,"w",encoding="utf-8") as f:
        for item in pending: f.write(json.dumps(item,ensure_ascii=False)+"\n")

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
    btc=next((r for r in rows if r["symbol"]=="BTCUSDT"),None)
    if btc: btc_pc=btc.get("price_chg24") or 0
    else:
        btc_pc=fetch_btc_price_chg()
        if btc_pc is None: return {"phase":"unknown","note":"","modifier":0}
    up=sum(1 for r in rows if (r.get("price_chg24") or 0)>0); ratio=up/max(len(rows),1)
    if btc_pc>2 and ratio>0.6: return {"phase":"risk-on","note":"BTC↑ рынок широкий","modifier":+1}
    if btc_pc>2 and ratio<0.4: return {"phase":"btc-dominance","note":"BTC↑ альты нет","modifier":0}
    if btc_pc<-2 and ratio<0.3: return {"phase":"risk-off","note":"BTC↓ рынок слабый","modifier":-1}
    if btc_pc<-2 and ratio>0.5: return {"phase":"rotation","note":"BTC↓ альты держатся","modifier":0}
    return {"phase":"neutral","note":"","modifier":0}

ACTIONS={"NEUTRAL":"IGNORE","ACCUMULATION":"WATCH","EARLY_MOVE":"WATCH_LONG","CONFIRMED_TREND":"POSSIBLE_ENTRY","ACCELERATION":"LONG_SETUP","EXHAUSTION":"NO_NEW_ENTRY","DISTRIBUTION":"EXIT_AVOID","INVALIDATED":"REMOVE"}
STATE_EMOJI={"NEUTRAL":"⚪","ACCUMULATION":"🔍","EARLY_MOVE":"🌱","CONFIRMED_TREND":"🟢","ACCELERATION":"🚀","EXHAUSTION":"🟠","DISTRIBUTION":"🔴","INVALIDATED":"❌"}
ALLOWED_FROM={"ACCUMULATION":{"NEUTRAL","ACCUMULATION"},"EARLY_MOVE":{"NEUTRAL","ACCUMULATION","EARLY_MOVE"},"CONFIRMED_TREND":{"ACCUMULATION","EARLY_MOVE","CONFIRMED_TREND"},"ACCELERATION":{"CONFIRMED_TREND","ACCELERATION"},"EXHAUSTION":{"CONFIRMED_TREND","ACCELERATION","EXHAUSTION"},"DISTRIBUTION":{"CONFIRMED_TREND","ACCELERATION","EXHAUSTION","DISTRIBUTION"},"INVALIDATED":{"NEUTRAL","ACCUMULATION","EARLY_MOVE","CONFIRMED_TREND","ACCELERATION","EXHAUSTION","DISTRIBUTION","INVALIDATED"}}

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
    return msg

def format_trade_close(rec):
    pnl=rec.get("strategy_pnl_pct")
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
    r60=rec.get("return_60m")
    if r60 is not None: msg+=f"Signal@60m: {r60:+.1f}%\n"
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

def open_trade_record(r,ts,state,path,score,momentum,conf,early_val,early_label,pattern,derived,market,idea_first_seen_ts,snapshots,price):
    if state=="CONFIRMED_TREND": trigger=f"confirmed_trend_{path}_path"
    elif state=="ACCELERATION": trigger="acceleration_confirmation"
    else: trigger=state.lower()
    entry_snapshot={
        "timestamp":ts,"price":price,"symbol":r.get("symbol"),
        "features":{
            "price_chg24":safe(r.get("price_chg24")),"oi_chg24":safe(r.get("oi_chg24_pct")),
            "oi_chg4h":safe(r.get("oi_chg4h_pct")),"funding":safe(r.get("fr_avg")),
            "funding_oiw":safe(r.get("fr_oiw")),"cvd24":safe(r.get("cvd24")),
            "lls24":safe(r.get("lls24")),"oi_vol_ratio":safe(r.get("oi_vol_ratio")),
            "oi_mcap_ratio":safe(r.get("oi_mktcap_ratio")),"ls_accounts":r.get("ls_accounts"),
            "volume24":r.get("volume24"),"liq_short24":safe(r.get("liq_short24")),
            "liq_long24":safe(r.get("liq_long24")),"btc_corr7d":r.get("btc_corr7d"),
            "cvd_momentum":round(derived["cvd_momentum"],2),"oi_accel":round(derived["oi_accel"],3),
            "price_accel":round(derived["price_accel"],3),"funding_pressure":round(derived["funding_pressure"],5),
            "oi_trend":derived["oi_trend"],"cvd_trend":derived["cvd_trend"],
            "price_trend":derived["price_trend"],"oi4h_trend":derived["oi4h_trend"],
            "divergence":derived["divergence"],
        },
        "decision":{"state":state,"score":score,"momentum":momentum,"path":path,
                    "pattern":pattern,"market_phase":market.get("phase","unknown"),
                    "entry_reason":{"state":state,"path":path,"trigger":trigger}},
    }
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
        "entry_price_chg24":safe(r.get("price_chg24")),"entry_oi_chg24":safe(r.get("oi_chg24_pct")),
        "entry_oi_chg4h":safe(r.get("oi_chg4h_pct")),"entry_cvd24":safe(r.get("cvd24")),
        "entry_lls24":safe(r.get("lls24")),"entry_fr_oiw":safe(r.get("fr_oiw")),
        "entry_oi_vol_ratio":safe(r.get("oi_vol_ratio")),"entry_oi_mktcap_ratio":safe(r.get("oi_mktcap_ratio")),
        "entry_liq_short24":safe(r.get("liq_short24")),"entry_liq_long24":safe(r.get("liq_long24")),
        "entry_ls_accounts":r.get("ls_accounts"),"entry_btc_corr7d":r.get("btc_corr7d"),
        "entry_market_phase":market.get("phase","unknown"),
        "data_quality":{"entry_price_source":"live","exit_price_source":None,"missing_snapshots":0,"max_price_age_min":0,"price_unknown":False},
    }
    for h in TRADE_HORIZONS:
        ot[f"return_{h}m"]=None
        ot[f"return_{h}m_available"]=False
    return ot

def _exit_meta(source,exit_ts,last_price_ts):
    if source=="live" or last_price_ts is None: return source,0.0
    return source,round(max(0,exit_ts-last_price_ts)/60,1)

def close_trade(ot,symbol,exit_ts,exit_price,exit_reason,exit_state,price_full,exit_price_source="live",exit_candidates=None,lifecycle_complete=True):
    ep=ot.get("entry_price")
     # >>> BINGX: закрытие позиции на бирже перед закрытием журнала <<<
    bx = ot.get("bingx") or {}
    if ENABLE_BINGX and bx.get("status") in ("opened", "already_open") and bx.get("qty"):
        try:
            import bingx_client
            res = bingx_client.close_long(symbol, bx["qty"])
            ot["bingx_close"] = res
            if res.get("status") == "closed":
                log.info(f"[{symbol}] BingX CLOSE ok orderId={res.get('order_id')}")
            else:
                log.error(f"[{symbol}] BingX CLOSE failed: {res.get('error')}")
        except Exception as e:
            log.error(f"[{symbol}] BingX CLOSE exception: {e}")
    # <<< BINGX >>>
    _fill_horizons(ot,symbol,exit_ts,price_full)
    gross=round((exit_price-ep)/ep*100,3) if (exit_price and ep) else None
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
        ot["data_quality"]["price_unknown"]=stale_min>30
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
        "trade_id":f"{symbol}_{ot['entry_ts']}",
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
        "engine_versions":ot.get("engine_versions",dict(ENGINE_VERSIONS)),
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
    log.info(f"[{symbol}] TRADE → PENDING {exit_reason} strat={strategy_pnl} hold={hold_min}m")
    send_tg(format_trade_close(rec))

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

def run():
    log.info("═══ Прогон ═══")
    wl_all=load_watchlist()
    global PENDING
    PENDING=load_pending()
    existing_trade_ids=load_existing_trade_ids()
    lifecycle_state=load_lifecycle_state()
    rows=fetch_data()
    log.info(f"Монет после discovery-фильтра: {len(rows)}")
    market=detect_market_phase(rows)
    log.info(f"Market: {market['phase']} {market['note']}")
    current_symbols={r["symbol"] for r in rows}
    ts=now_ts()
    for r in rows:
        append_jsonl(HEARTBEAT_FILE,{"ts":ts,"symbol":r["symbol"],"price":r.get("price")})
        sym=r["symbol"]
        append_jsonl(MARKET_HISTORY_FILE,{**r,"lifecycle_state":wl_all.get(sym,{}).get("state")})
    history_all=load_market_history()
    price_full=load_price_full()
    for sym,hist in history_all.items():
        if not hist or sym not in current_symbols: continue
        r=hist[-1]
        cur_price=r.get("price") if valid_price(r.get("price")) else None
        raw_score,pros,cons=calculate_score(r)
        score=clamp(raw_score+market["modifier"],0,10)
        derived=calc_derived(hist)
        momentum,mom_tags=calc_momentum(derived)
        pattern=detect_pattern(r,derived,momentum)
        prev_state=wl_all.get(sym,{}).get("state","NEUTRAL")
        state,reasons,warnings,path=detect_lifecycle(sym,hist,score,derived,prev_state)
        if state=="NEUTRAL":
            if sym in wl_all:
                old=wl_all[sym]["state"]
                ot=wl_all[sym].get("open_trade")
                if old in ACTIVE_STATES:
                    nr=wl_all[sym].get("neutral_runs",0)+1
                    wl_all[sym]["neutral_runs"]=nr
                    wl_all[sym]["last_seen"]=ts
                    if ot:
                        ot["state_history"].append({"ts":ts,"state":"NEUTRAL","score":score,"reason":"signal_pause"})
                    if nr>=NEUTRAL_HYSTERESIS:
                        if ot:
                            log.info(f"[{sym}] {old} → NEUTRAL (×{nr}), сделка открыта — держим")
                        else:
                            send_tg(f"⚪ <b>{esc(wl_all[sym].get('name',sym))} ({esc(sym)})</b>\n━━━━━━━━━━━━━━━━━━\n{esc(old)} → NEUTRAL\n<i>Снята: условия тренда не выполняются {nr} прогона подряд</i>\n")
                            log.info(f"[{sym}] {old} → NEUTRAL (×{nr}), remove")
                            if sym in lifecycle_state: del lifecycle_state[sym]
                            del wl_all[sym]
                    else:
                        log.info(f"[{sym}] {old}: NEUTRAL-оценка {nr}/{NEUTRAL_HYSTERESIS}")
                else:
                    if ot:
                        ot["state_history"].append({"ts":ts,"state":"NEUTRAL","score":score,"reason":"signal_pause"})
                    log.info(f"[{sym}] {old} → NEUTRAL, remove")
                    if sym in lifecycle_state: del lifecycle_state[sym]
                    del wl_all[sym]
            continue
        if state=="INVALIDATED":
            if sym in wl_all:
                old=wl_all[sym]["state"]
                ot=wl_all[sym].get("open_trade")
                if ot:
                    exit_price=cur_price or ot.get("last_price")
                    src="live" if cur_price else "last_seen"
                    close_trade(ot,sym,ts,exit_price,"INVALIDATED",state,price_full,exit_price_source=src,exit_candidates=["INVALIDATED"],lifecycle_complete=True)
                send_tg(f"❌ <b>{esc(wl_all[sym].get('name',sym))} ({esc(sym)})</b>\n━━━━━━━━━━━━━━━━━━\n{esc(old)} → INVALIDATED\n<i>Сценарий сломан: {esc(' · '.join(reasons[:2]))}</i>\n")
                log.info(f"[{sym}] INVALIDATED → remove")
                if sym in lifecycle_state: del lifecycle_state[sym]
                del wl_all[sym]
            continue
        conf=calc_confidence(state,hist,score,derived,market["modifier"])
        early_val,early_label=entry_earliness(r)
        existing=wl_all.get(sym,{})
        ot=existing.get("open_trade")
        tid=existing.get("trade_id")
        new_ot,new_tid=None,None
        if ot is not None:
            ot=dict(ot)
            if state!=ot.get("current_state"):
                duration_min=round((ts-ot.get("current_state_start_ts",ts))/60,1)
                old_state=ot.get("current_state","UNKNOWN")
                ot["time_in_states"][old_state]=ot["time_in_states"].get(old_state,0)+duration_min
                ot["current_state"]=state
                ot["current_state_start_ts"]=ts
                ot["state_history"].append({"ts":ts,"state":state,"score":score,"reason":"state_transition"})
                if STATE_RANK.get(state,0)>STATE_RANK.get(ot.get("max_state"),0):
                    ot["max_state"]=state
            exit_candidates={
                "INVALIDATED":state=="INVALIDATED",
                "EXHAUSTION":state=="EXHAUSTION",
                "DISTRIBUTION":state=="DISTRIBUTION",
                "STOP_LOSS":False,
                "SIGNAL_DECAY":False,
                "TIMEOUT":ts-ot["entry_ts"]>=TRADE_TIMEOUT_MIN*60,
                "MISSED":False,
                "NEUTRAL":False,
            }
            if cur_price and ot.get("entry_price"):
                pnl_pct=(cur_price-ot["entry_price"])/ot["entry_price"]*100
                if pnl_pct<=-STOP_LOSS_PCT: exit_candidates["STOP_LOSS"]=True
            last_signal_ts=ot.get("last_signal_ts",ot["entry_ts"])
            if ts-last_signal_ts>=SIGNAL_DECAY_MIN*60: exit_candidates["SIGNAL_DECAY"]=True
            if score>=5 or state in("CONFIRMED_TREND","ACCELERATION"): ot["last_signal_ts"]=ts
            close_reason,all_triggered=resolve_exit_reason(exit_candidates)
            if close_reason:
                exit_price=cur_price or ot.get("last_price")
                src="live" if cur_price else "last_seen"
                close_trade(ot,sym,ts,exit_price,close_reason,state,price_full,exit_price_source=src,exit_candidates=all_triggered,lifecycle_complete=True)
                cooldown_min=COOLDOWN_BY_EXIT_REASON.get(close_reason,0)
                if cooldown_min>0: lifecycle_state[sym]={"cooldown_until":ts+cooldown_min*60,"idea_first_seen_ts":lifecycle_state.get(sym,{}).get("idea_first_seen_ts")}
            else:
                if cur_price:
                    ot["last_price"]=cur_price
                    ot["last_price_ts"]=ts
                    ep=ot.get("entry_price")
                    if ep:
                        pnl_now=(cur_price-ep)/ep*100
                        if pnl_now>ot.get("max_pnl_pct",0.0): ot["max_pnl_pct"]=pnl_now; ot["peak_ts"]=ts
                        if pnl_now<ot.get("min_pnl_pct",0.0): ot["min_pnl_pct"]=pnl_now
                    ot["data_quality"]["max_price_age_min"]=max(ot["data_quality"]["max_price_age_min"],0)
                else:
                    ot["data_quality"]["missing_snapshots"]+=1
                    age=round((ts-ot.get("last_price_ts",ts))/60,1)
                    ot["data_quality"]["max_price_age_min"]=max(ot["data_quality"]["max_price_age_min"],age)
                _fill_horizons(ot,sym,ts,price_full)
                new_ot,new_tid=ot,tid
        else:
            if state in ENTRY_STATES and cur_price:
                cooldown_info=lifecycle_state.get(sym,{})
                if cooldown_info.get("cooldown_until",0)>ts:
                    log.info(f"[{sym}] COOLDOWN активен, пропускаем вход")
                else:
                    idea_first_seen_ts=cooldown_info.get("idea_first_seen_ts")
                    if idea_first_seen_ts is None and state in("ACCUMULATION","EARLY_MOVE","CONFIRMED_TREND","ACCELERATION"):
                        idea_first_seen_ts=ts
                    new_tid=f"{sym}_{ts}"
                    new_ot=open_trade_record(r,ts,state,path,score,momentum,conf,early_val,early_label,pattern,derived,market,idea_first_seen_ts,existing.get("snapshots",0)+1,cur_price)
                    # >>> BINGX: строго ПОСЛЕ open_trade_record, защищено от None <<<
                    if ENABLE_BINGX and new_ot is not None:
                        try:
                            import bingx_client
                            bx=bingx_client.open_long(sym,cur_price)
                            new_ot["bingx"]=bx
                            if bx.get("status") in ("opened","already_open"):
                                log.info(f"[{sym}] BingX OPEN {bx.get('status')} orderId={bx.get('order_id')} qty={bx.get('qty')}")
                            else:
                                log.error(f"[{sym}] BingX OPEN failed: {bx.get('error')}")
                        except Exception as e:
                            log.error(f"[{sym}] BingX OPEN exception: {e}")
                            try:
                                new_ot["bingx"]={"status":"error","error":str(e)}
                            except Exception:
                                pass
                    # <<< BINGX >>>
                    log.info(f"[{sym}] TRADE OPEN {state} path={path} @ {cur_price}")
        # [FIX] setdefault не перезаписывает cooldown_until
        idea_first_seen=lifecycle_state.get(sym,{}).get("idea_first_seen_ts")
        if idea_first_seen is None and state in("ACCUMULATION","EARLY_MOVE","CONFIRMED_TREND","ACCELERATION"):
            idea_first_seen=ts
            lifecycle_state.setdefault(sym,{})["idea_first_seen_ts"]=idea_first_seen
        entry={
            "state":state,"previous_state":prev_state,
            "action":ACTIONS.get(state,""),
            "confidence":conf,"score":score,"momentum":momentum,
            "pattern":pattern,"name":r.get("name",sym),
            "first_seen":existing.get("first_seen",ts),"last_seen":ts,
            "snapshots":existing.get("snapshots",0)+1,
            "missed_runs":0,"neutral_runs":0,
            "entry_earliness":round(early_val,2),
            "reasons":reasons,"warnings":warnings,"mom_tags":mom_tags,
        }
        if new_ot is not None:
            entry["open_trade"]=new_ot
            entry["trade_id"]=new_tid
        wl_all[sym]=entry
        cur={**r,"score":score,"pros":pros,"cons":cons,"derived":derived,"momentum":momentum,"pattern":pattern}
        if state!=prev_state:
            log.info(f"[{sym}] {prev_state} → {state} | {reasons}")
            if state in TG_STATES:
                append_jsonl(CALIBRATION_FILE,{
                    "ts":ts,"symbol":sym,"state":state,
                    "oi_accel":round(derived["oi_accel"],3),
                    "cvd_momentum":round(derived["cvd_momentum"],2),
                    "price_accel":round(derived["price_accel"],3),
                    "funding_pressure":round(derived["funding_pressure"],5),
                    "oi_trend":derived["oi_trend"],"cvd_trend":derived["cvd_trend"],
                    "score":score,"momentum":momentum,"confidence":conf,
                    "signal_logic_version":SIGNAL_LOGIC_VERSION,
                })
                llm_res=llm_verify(sym,wl_all[sym],cur,hist)
                msg=format_signal(sym,wl_all[sym],cur,hist,reasons,warnings,market)
                if llm_res:
                    agree="✅" if llm_res.get("agree") is True else "❌"
                    risk=llm_res.get("risk","?")
                    reason=llm_res.get("reason","")
                    msg+=f"\n🤖 {agree} {esc(risk)} · {esc(reason)}"
                send_tg(msg)
    for sym in list(wl_all.keys()):
        if sym in current_symbols: continue
        missed=wl_all[sym].get("missed_runs",0)+1
        wl_all[sym]["missed_runs"]=missed
        wl_all[sym]["last_seen"]=ts
        stage=wl_all[sym]["state"]
        if stage in ACTIVE_STATES and missed==MISS_EXIT_RUNS:
            log.info(f"[{sym}] выпала из фильтра {missed} прогона → пауза")
            send_tg(f"⏸ <b>{esc(wl_all[sym].get('name',sym))} ({esc(sym)})</b>\n━━━━━━━━━━━━━━━━━━\nНаблюдение на паузе ({esc(stage)})\nВыпала из discovery-фильтра {missed} прогона подряд\n<i>Стадия сохранена — при возврате пересчитается.</i>\n")
        if missed>=MISS_REMOVE_RUNS:
            ot=wl_all[sym].get("open_trade")
            if ot:
                close_trade(ot,sym,ts,ot.get("last_price"),"MISSED",stage,price_full,exit_price_source="last_seen",exit_candidates=["MISSED"],lifecycle_complete=False)
            log.info(f"[{sym}] нет в данных {missed} прогонов → remove")
            if sym in lifecycle_state: del lifecycle_state[sym]
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
    open_n=sum(1 for v in wl_all.values() if v.get("open_trade"))
    log.info(f"═══ Готово. Active: {len(wl_all)} · open trades: {open_n} · pending: {len(PENDING)} ═══")

if __name__=="__main__":
    try: run()
    except Exception as e:
        log.exception(f"Фатал: {e}")
        send_tg(f"⚠️ <b>Monitor</b>\n{esc(str(e)[:500])}")
        sys.exit(1)
