"""
coinalyze_monitor.py
====================
Автоматический внутридневной поиск качественных LONG-кандидатов.

Запуск через cron каждые 5 минут:
  */5 * * * * cd /opt/monitor && /usr/bin/python3 coinalyze_monitor.py

Все файлы состояния лежат в корне рядом со скриптом.
"""

import os
import sys
import time
import json
import html as html_mod
import logging
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import requests

try:
    from playwright_stealth import stealth_sync
except ImportError:
    def stealth_sync(page):
        pass

# ═══════════════════════════════════════════════════════════
# ПУТИ — всё в корне рядом со скриптом
# ═══════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent

SNAPSHOTS_FILE  = BASE_DIR / "snapshots.jsonl"
HEARTBEAT_FILE  = BASE_DIR / "heartbeat.jsonl"
WATCHLIST_FILE  = BASE_DIR / "watchlist.json"
DEBUG_HTML_FILE = BASE_DIR / "debug_page.html"

# ═══════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════

COINALYZE_P_SID    = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")
TG_BOT_TOKEN       = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID         = os.environ.get("TG_CHAT_ID", "")

ENABLE_LLM    = os.environ.get("ENABLE_LLM", "false").lower() == "true"
QWEN_API_KEY  = os.environ.get("QWEN_API_KEY", "")
QWEN_BASE_URL = os.environ.get("QWEN_BASE_URL",
                               "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
QWEN_MODEL    = os.environ.get("QWEN_MODEL", "qwen-plus")

COINALYZE_URL = (
    "https://coinalyze.net/"
    "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
    "&filter=ZV9ndF8yLjUmYl9ndF8xJmNfZ3RfMTAwMDAwMCZjbTYxNjVfZ3RfMzUmY202MTY0X2x0XzQ1"
    "&order_by=oi_current&order_dir=desc"
)

# Сроки хранения
SNAPSHOTS_TTL_DAYS = 7
HEARTBEAT_TTL_DAYS = 3

# Lifecycle
CONFIRM_SNAPSHOTS  = 3
CONFIRM_WINDOW_MIN = 30
RUNNING_SNAPSHOTS  = 4
EXIT_NO_RECOVERY   = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("coinalyze")


# ═══════════════════════════════════════════════════════════
# УТИЛИТЫ
# ═══════════════════════════════════════════════════════════

def parse_number(raw: Optional[str]) -> Optional[float]:
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


def now_ts() -> int:
    return int(time.time())


# ═══════════════════════════════════════════════════════════
# 1. ИСТОЧНИК ДАННЫХ
# ═══════════════════════════════════════════════════════════

def fetch_html() -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
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
                cookies.append({"name": "p_sid", "value": COINALYZE_P_SID,
                                "domain": "coinalyze.net", "path": "/", "secure": True})
            if COINALYZE_CHAT_SID:
                cookies.append({"name": "chat_sid", "value": COINALYZE_CHAT_SID,
                                "domain": "coinalyze.net", "path": "/", "secure": True})
            cookies.append({"name": "cookies_accepted", "value": "1",
                            "domain": "coinalyze.net", "path": "/", "secure": True})
            context.add_cookies(cookies)

        page = context.new_page()
        stealth_sync(page)

        try:
            page.goto(COINALYZE_URL, wait_until="domcontentloaded", timeout=50_000)
            page.wait_for_timeout(4000)
            if "Attention Required" in page.content():
                log.warning("Cloudflare challenge, waiting...")
                page.wait_for_timeout(10_000)
            page.wait_for_selector("tbody tr", timeout=25_000)
            html_content = page.content()
        except Exception as e:
            log.error(f"Ошибка загрузки: {e}")
            try:
                html_content = page.content()
            except Exception:
                html_content = ""
            try:
                page.screenshot(path=str(BASE_DIR / "debug_screenshot.png"), full_page=True)
            except Exception:
                pass
        finally:
            browser.close()

    return html_content


def parse_table(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "lxml")
    rows = soup.select("tbody tr")
    log.info(f"Строк в таблице: {len(rows)}")

    records = []
    ts = now_ts()
    for tr in rows:
        symbol = tr.get("data-coin")
        tds = tr.find_all("td")
        if len(tds) < 23:
            continue
        name_spans = tds[1].find_all("span")
        coin_name = name_spans[0].get_text(strip=True) if name_spans else (symbol or "?")

        records.append({
            "ts":              ts,
            "symbol":          symbol,
            "name":            coin_name,
            "price":           parse_number(tds[2].get_text(strip=True)),
            "price_chg24":     parse_number(tds[3].get_text(strip=True)),
            "mktcap":          parse_number(tds[4].get_text(strip=True)),
            "volume24":        parse_number(tds[5].get_text(strip=True)),
            "oi":              parse_number(tds[6].get_text(strip=True)),
            "oi_chg24_pct":    parse_number(tds[7].get_text(strip=True)),
            "oi_chg4h_pct":    parse_number(tds[9].get_text(strip=True)),
            "oi_vol_ratio":    parse_number(tds[11].get_text(strip=True)),
            "oi_mktcap_ratio": parse_number(tds[12].get_text(strip=True)),
            "fr_avg":          parse_number(tds[13].get_text(strip=True)),
            "pfr_avg":         parse_number(tds[14].get_text(strip=True)),
            "fr_oiw":          parse_number(tds[15].get_text(strip=True)),
            "pfr_oiw":         parse_number(tds[16].get_text(strip=True)),
            "liq_short24":     parse_number(tds[17].get_text(strip=True)),
            "liq_long24":      parse_number(tds[18].get_text(strip=True)),
            "ls_accounts":     parse_number(tds[19].get_text(strip=True)),
            "btc_corr7d":      parse_number(tds[20].get_text(strip=True)),
            "cvd24":           parse_number(tds[21].get_text(strip=True)),
            "lls24":           parse_number(tds[22].get_text(strip=True)),
        })
    return records


def fetch_data() -> list[dict]:
    html_text = fetch_html()
    DEBUG_HTML_FILE.write_text(html_text, encoding="utf-8")
    rows = parse_table(html_text)
    if not rows:
        send_telegram(
            "⚠️ <b>Coinalyze Monitor</b>\n"
            "Данные не получены.\n"
            "• Cookies истекли\n"
            "• Разметка изменилась\n"
            "• Cloudflare\n\n"
            "Проверь debug_page.html"
        )
        sys.exit(1)
    return rows


# ═══════════════════════════════════════════════════════════
# 2. ХРАНЕНИЕ
# ═══════════════════════════════════════════════════════════

def append_jsonl(path: Path, record: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def cleanup_jsonl(path: Path, ttl_days: int):
    if not path.exists():
        return
    cutoff = now_ts() - ttl_days * 86400
    records = load_jsonl(path)
    fresh = [r for r in records if r.get("ts", 0) > cutoff]
    removed = len(records) - len(fresh)
    if removed > 0:
        with open(path, "w", encoding="utf-8") as f:
            for r in fresh:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        log.info(f"Cleanup {path.name}: -{removed} записей")


def load_watchlist() -> dict:
    if not WATCHLIST_FILE.exists():
        return {}
    with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_watchlist(wl: dict):
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(wl, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════
# 3. ПЕРВИЧНЫЙ ФИЛЬТР
# ═══════════════════════════════════════════════════════════

def passes_primary_filter(r: dict) -> bool:
    vol = r.get("volume24")
    if vol is None or vol <= 1_000_000:
        return False

    pc = r.get("price_chg24")
    if pc is None or pc < 1.0 or pc > 15.0:
        return False

    oi24 = r.get("oi_chg24_pct")
    if oi24 is None or oi24 <= 5.0 or oi24 >= 50.0:
        return False

    oi4h = r.get("oi_chg4h_pct")
    if oi4h is None or oi4h <= 0:
        return False

    cvd = r.get("cvd24")
    if cvd is None or cvd <= 55:
        return False

    lls = r.get("lls24")
    if lls is None or lls >= 40:
        return False

    oi_mc = r.get("oi_mktcap_ratio")
    if oi_mc is None or oi_mc >= 0.15:
        return False

    oi_v = r.get("oi_vol_ratio")
    if oi_v is None or oi_v < 0.1 or oi_v > 2.5:
        return False

    fr = r.get("fr_oiw")
    if fr is not None and fr > 0.05:
        return False

    return True


# ═══════════════════════════════════════════════════════════
# 4. SCORING (макс 10)
# ═══════════════════════════════════════════════════════════

def calculate_score(r: dict) -> tuple[int, list[str], list[str]]:
    score = 0
    pros: list[str] = []
    cons: list[str] = []

    # CVD
    cvd = r.get("cvd24")
    if cvd is not None:
        if cvd > 70:
            score += 2; pros.append(f"CVD={cvd:.0f} >70")
        elif cvd >= 55:
            score += 1; pros.append(f"CVD={cvd:.0f} 55-70")

    # LLS
    lls = r.get("lls24")
    if lls is not None:
        if lls < 15:
            score += 2; pros.append(f"LLS={lls:.0f}% <15")
        elif lls < 40:
            score += 1; pros.append(f"LLS={lls:.0f}% 15-40")
        if lls > 50:
            score -= 2; cons.append(f"LLS={lls:.0f}% >50")

    # OI 24h
    oi24 = r.get("oi_chg24_pct")
    if oi24 is not None:
        if 5 <= oi24 <= 35:
            score += 2; pros.append(f"OI24={oi24:.1f}% 5-35")
        elif oi24 > 50:
            score -= 2; cons.append(f"OI24={oi24:.1f}% >50 перегрев")

    # OI 4h
    oi4h = r.get("oi_chg4h_pct")
    if oi4h is not None and oi4h > 0:
        score += 1; pros.append(f"OI4h={oi4h:.1f}% >0")

    # Price
    pc = r.get("price_chg24")
    if pc is not None:
        if 2 <= pc <= 10:
            score += 1; pros.append(f"Price={pc:.1f}% 2-10")
        elif pc > 20:
            score -= 2; cons.append(f"Price={pc:.1f}% >20 вертикаль")

    # Funding
    fr = r.get("fr_oiw")
    if fr is not None:
        if -0.01 <= fr <= 0.03:
            score += 1; pros.append(f"Funding={fr:.4f} норма")
        elif fr > 0.05:
            score -= 2; cons.append(f"Funding={fr:.4f} перегрет")

    # OI/Mcap
    oi_mc = r.get("oi_mktcap_ratio")
    if oi_mc is not None and oi_mc < 0.10:
        score += 1; pros.append(f"OI/Mcap={oi_mc:.3f} <0.10")

    return score, pros, cons


# ═══════════════════════════════════════════════════════════
# 7. ДИНАМИКА
# ═══════════════════════════════════════════════════════════

def compute_trend(values: list) -> str:
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return "flat"
    diffs = [clean[i] - clean[i - 1] for i in range(1, len(clean))]
    avg = sum(diffs) / len(diffs)
    if avg > 0.5:
        return "up"
    if avg < -0.5:
        return "down"
    return "flat"


def analyze_dynamics(snaps: list[dict]) -> dict:
    if len(snaps) < 2:
        return {"oi_trend": "flat", "cvd_trend": "flat",
                "price_trend": "flat", "oi4h_trend": "flat",
                "divergence": "none", "note": ""}

    recent = snaps[-6:]
    oi_trend    = compute_trend([s.get("oi_chg24_pct") for s in recent])
    cvd_trend   = compute_trend([s.get("cvd24") for s in recent])
    price_trend = compute_trend([s.get("price_chg24") for s in recent])
    oi4h_trend  = compute_trend([s.get("oi_chg4h_pct") for s in recent])

    divergence = "none"
    note = ""
    if price_trend == "up" and oi_trend == "down":
        divergence = "price_up_oi_down"
        note = "Цена ↑ OI ↓ — рост на закрытии шортов, не на новом спросе"
    elif price_trend == "up" and cvd_trend == "down":
        divergence = "price_up_cvd_down"
        note = "Цена ↑ CVD ↓ — покупатели ослабевают"
    elif oi_trend == "up" and cvd_trend == "up" and price_trend == "up":
        note = "Здоровое движение: Price+OI+CVD растут синхронно"

    return {
        "oi_trend": oi_trend, "cvd_trend": cvd_trend,
        "price_trend": price_trend, "oi4h_trend": oi4h_trend,
        "divergence": divergence, "note": note,
    }


# ═══════════════════════════════════════════════════════════
# 8. ПАТТЕРНЫ
# ═══════════════════════════════════════════════════════════

def detect_pattern(r: dict, dyn: dict) -> str:
    pc   = r.get("price_chg24") or 0
    oi24 = r.get("oi_chg24_pct") or 0
    cvd  = r.get("cvd24") or 50
    lls  = r.get("lls24") or 30
    fr   = r.get("fr_oiw") or 0
    ls   = r.get("ls_accounts") or 1.0

    if pc > 0 and oi24 > 5 and cvd > 60 and lls < 30 and dyn["oi_trend"] == "up":
        return "Healthy Trend"
    if pc > 0 and oi24 > 5 and lls > 35 and ls < 1.0:
        return "Short Squeeze Setup"
    if pc < 3 and dyn["cvd_trend"] == "up" and fr < 0.005:
        return "Stealth Accumulation"
    if pc > 10 and oi24 > 20 and dyn["cvd_trend"] == "down":
        return "Late Trend"
    if pc < 0 and dyn["oi_trend"] == "down":
        return "Distribution"
    if oi24 < -10 and lls > 45 and dyn["cvd_trend"] == "up":
        return "Capitulation"
    return "Neutral"


# ═══════════════════════════════════════════════════════════
# 5-6. LIFECYCLE
# ═══════════════════════════════════════════════════════════

def get_symbol_snapshots(symbol: str) -> list[dict]:
    all_snaps = load_jsonl(SNAPSHOTS_FILE)
    return sorted([s for s in all_snaps if s.get("symbol") == symbol],
                  key=lambda s: s["ts"])


def lifecycle_transition(symbol: str, entry: dict, current: dict,
                         snaps: list[dict]) -> tuple[str, list[str]]:
    stage = entry.get("stage", "NEW")
    reasons: list[str] = []
    score = current.get("score", 0)
    dyn = current.get("dynamics", {})

    # NEW → WAIT_CONFIRMATION
    if stage == "NEW":
        if score >= 6:
            reasons.append(f"Score={score} ≥6")
            return "WAIT_CONFIRMATION", reasons
        return stage, reasons

    # WAIT_CONFIRMATION → CONFIRMED_LONG
    if stage == "WAIT_CONFIRMATION":
        cutoff = now_ts() - CONFIRM_WINDOW_MIN * 60
        recent = [s for s in snaps if s["ts"] > cutoff]
        if len(recent) >= CONFIRM_SNAPSHOTS:
            oi_ok = all(
                (recent[i].get("oi_chg24_pct") or 0) >=
                (recent[i - 1].get("oi_chg24_pct") or 0) - 1
                for i in range(1, len(recent))
            )
            oi4h_ok = all((s.get("oi_chg4h_pct") or 0) >= -0.5 for s in recent)
            cvd_ok = all(
                (recent[i].get("cvd24") or 50) >=
                (recent[i - 1].get("cvd24") or 50) - 5
                for i in range(1, len(recent))
            )
            lls_ok = all((s.get("lls24") or 30) < 50 for s in recent)

            if oi_ok and oi4h_ok and cvd_ok and lls_ok:
                reasons.append(f"{len(recent)} снимков за {CONFIRM_WINDOW_MIN} мин")
                reasons.append("OI↑ OI4h≥0 CVD стабилен LLS<50")
                return "CONFIRMED_LONG", reasons
            if not oi_ok:   reasons.append("OI24 не растёт")
            if not oi4h_ok: reasons.append("OI4h падает")
            if not cvd_ok:  reasons.append("CVD падает")
            if not lls_ok:  reasons.append("LLS>50")
        return stage, reasons

    # CONFIRMED_LONG → RUNNING
    if stage == "CONFIRMED_LONG":
        if len(snaps) >= RUNNING_SNAPSHOTS and score >= 7:
            reasons.append(f"{len(snaps)} снимков, score={score}≥7")
            return "RUNNING", reasons
        return stage, reasons

    # RUNNING → EXIT_WARNING
    if stage == "RUNNING":
        triggered = False

        if len(snaps) >= 3:
            oi3 = [s.get("oi_chg24_pct") or 0 for s in snaps[-3:]]
            if oi3[-1] < oi3[-2] < oi3[-3]:
                triggered = True
                reasons.append("OI падает 2 снимка подряд")

        if len(snaps) >= 2:
            cvd_prev = snaps[-2].get("cvd24") or 50
            cvd_curr = snaps[-1].get("cvd24") or 50
            if cvd_prev - cvd_curr > 15:
                triggered = True
                reasons.append(f"CVD -{cvd_prev - cvd_curr:.0f} пунктов")

        if (current.get("price_chg24") or 0) > 0 and dyn.get("oi_trend") == "down":
            triggered = True
            reasons.append("Дивергенция Price↑ OI↓")

        if (current.get("lls24") or 0) > 50:
            triggered = True
            reasons.append(f"LLS={current['lls24']:.0f}%>50")

        if triggered:
            return "EXIT_WARNING", reasons
        return stage, reasons

    # EXIT_WARNING → REMOVED
    if stage == "EXIT_WARNING":
        since = entry.get("exit_warning_since", 0)
        after = [s for s in snaps if s["ts"] > since]
        if len(after) >= EXIT_NO_RECOVERY:
            recovered = any(
                (s.get("oi_chg4h_pct") or 0) > 0 and (s.get("cvd24") or 0) > 55
                for s in after[-EXIT_NO_RECOVERY:]
            )
            if not recovered:
                reasons.append(f"Нет восстановления {EXIT_NO_RECOVERY} снимка")
                return "REMOVED", reasons
        return stage, reasons

    return stage, reasons


# ═══════════════════════════════════════════════════════════
# 9. TELEGRAM
# ═══════════════════════════════════════════════════════════

def esc(val) -> str:
    return html_mod.escape(str(val), quote=False)


def send_telegram(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram не настроен")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    chunks, rem = [], text
    while len(rem) > 3800:
        sp = rem.rfind("\n", 0, 3800)
        if sp == -1:
            sp = 3800
        chunks.append(rem[:sp])
        rem = rem[sp:]
    chunks.append(rem)

    for ch in chunks:
        try:
            r = requests.post(url, data={
                "chat_id": TG_CHAT_ID, "text": ch,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }, timeout=15)
            if r.status_code != 200:
                requests.post(url, data={
                    "chat_id": TG_CHAT_ID, "text": ch,
                    "disable_web_page_preview": True,
                }, timeout=15)
            time.sleep(0.4)
        except Exception as e:
            log.error(f"TG error: {e}")


def format_signal(symbol: str, entry: dict, cur: dict,
                  snaps: list[dict], reasons: list[str]) -> str:
    stage = entry["stage"]
    dyn = cur.get("dynamics", {})
    s = snaps[-1] if snaps else {}

    cfg = {
        "CONFIRMED_LONG": ("🟢", "ЛОНГ ПОДТВЕРЖДЁН"),
        "RUNNING":        ("🔵", "ТРЕНД АКТИВЕН"),
        "EXIT_WARNING":   ("🟠", "ПРИЗНАКИ ВЫХОДА"),
    }
    emoji, label = cfg.get(stage, ("⚪", stage))

    ar = {"up": "↑", "down": "↓", "flat": "→"}
    oi_t  = ar.get(dyn.get("oi_trend"), "→")
    cvd_t = ar.get(dyn.get("cvd_trend"), "→")
    prc_t = ar.get(dyn.get("price_trend"), "→")

    pattern = cur.get("pattern", "Neutral")
    pattern = "—" if pattern == "Neutral" else pattern

    score = cur.get("score", 0)
    note = dyn.get("note", "")
    reas = " · ".join(reasons) if reasons else ""

    line = "━━━━━━━━━━━━━━━━━━"

    msg = (
        f"{emoji} <b>{esc(cur.get('name', symbol))} ({esc(symbol)})</b>\n"
        f"{line}\n"
        f"Стадия: {label}\n"
        f"Score: {score}/10\n"
        f"Паттерн: {esc(pattern)}\n"
        f"Тренды: OI {oi_t} | CVD {cvd_t} | Price {prc_t}\n"
        f"{line}\n"
        f"P {fmt_pct(s.get('price_chg24'))} | "
        f"OI {fmt_pct(s.get('oi_chg24_pct'))} | "
        f"4h {fmt_pct(s.get('oi_chg4h_pct'))} | "
        f"CVD {fmt_num(s.get('cvd24'), decimals=0)} | "
        f"LLS {fmt_num(s.get('lls24'), '%', 0)}\n"
    )

    if note:
        msg += f"{line}\n<i>{esc(note)}</i>\n"

    if reas:
        msg += f"{line}\n<i>{esc(reas)}</i>\n"

    return msg


# ═══════════════════════════════════════════════════════════
# 10. LLM (опционально, только объяснение)
# ═══════════════════════════════════════════════════════════

def llm_explain(symbol: str, entry: dict, cur: dict,
                snaps: list[dict]) -> Optional[str]:
    if not ENABLE_LLM or not QWEN_API_KEY:
        return None

    recent = snaps[-5:]
    snap_txt = "\n".join(
        f"  ts={s['ts']} p={s.get('price_chg24')} oi24={s.get('oi_chg24_pct')} "
        f"oi4h={s.get('oi_chg4h_pct')} cvd={s.get('cvd24')} lls={s.get('lls24')}"
        for s in recent
    )
    user_msg = (
        f"Монета: {symbol}\nСтадия: {entry.get('stage')}\n"
        f"Score: {cur.get('score')}\nПаттерн: {cur.get('pattern')}\n"
        f"Динамика: {json.dumps(cur.get('dynamics',{}), ensure_ascii=False)}\n\n"
        f"Снимки:\n{snap_txt}\n\n"
        f"Объясни ситуацию в 2-3 предложениях. Без рекомендаций."
    )
    try:
        resp = requests.post(
            f"{QWEN_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {QWEN_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": QWEN_MODEL,
                  "messages": [
                      {"role": "system",
                       "content": "Ты аналитик крипто-деривативов. Кратко и нейтрально."},
                      {"role": "user", "content": user_msg}],
                  "temperature": 0.2, "max_tokens": 250},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning(f"LLM: {e}")
    return None


# ═══════════════════════════════════════════════════════════
# 12. ОСНОВНОЙ ЦИКЛ (один прогон)
# ═══════════════════════════════════════════════════════════

def run():
    log.info("═══ Прогон ═══")

    # 1. Данные
    rows = fetch_data()
    log.info(f"Монет: {len(rows)}")

    # 2. Heartbeat
    ts = now_ts()
    for r in rows:
        append_jsonl(HEARTBEAT_FILE, {"ts": ts, "symbol": r["symbol"],
                                      "price": r.get("price")})

    # 3. Фильтр
    candidates = [r for r in rows if passes_primary_filter(r)]
    log.info(f"Кандидатов: {len(candidates)}")

    # 4-6. Snapshots + watchlist + lifecycle
    watchlist = load_watchlist()

    for r in candidates:
        sym = r["symbol"]
        score, pros, cons = calculate_score(r)

        sym_snaps = get_symbol_snapshots(sym)
        cur_snap = {**r, "score": score, "pros": pros, "cons": cons}
        dyn = analyze_dynamics(sym_snaps + [cur_snap])
        pattern = detect_pattern(r, dyn)

        full = {**r, "score": score, "pros": pros, "cons": cons,
                "dynamics": dyn, "pattern": pattern}
        append_jsonl(SNAPSHOTS_FILE, full)

        # Watchlist
        if sym not in watchlist:
            watchlist[sym] = {"stage": "NEW", "first_seen": ts,
                              "last_seen": ts, "snapshots": 1,
                              "score": score, "warnings": []}
        else:
            watchlist[sym]["last_seen"] = ts
            watchlist[sym]["snapshots"] += 1
            watchlist[sym]["score"] = score

        # Lifecycle
        entry = watchlist[sym]
        all_snaps = get_symbol_snapshots(sym)
        new_stage, reasons = lifecycle_transition(sym, entry, full, all_snaps)

        old_stage = entry["stage"]
        if new_stage != old_stage:
            log.info(f"[{sym}] {old_stage} → {new_stage} | {reasons}")
            watchlist[sym]["stage"] = new_stage
            if new_stage == "EXIT_WARNING":
                watchlist[sym]["exit_warning_since"] = ts

            # Telegram только для CONFIRMED_LONG / RUNNING / EXIT_WARNING
            if new_stage in ("CONFIRMED_LONG", "RUNNING", "EXIT_WARNING"):
                llm_txt = llm_explain(sym, watchlist[sym], full, all_snaps)
                msg = format_signal(sym, watchlist[sym], full, all_snaps, reasons)
                if llm_txt:
                    msg += f"\n\n🤖 {esc(llm_txt)}"
                send_telegram(msg)

        if new_stage == "REMOVED":
            log.info(f"[{sym}] REMOVED")
            del watchlist[sym]

    # 7-9. Сохранение + cleanup
    save_watchlist(watchlist)
    cleanup_jsonl(SNAPSHOTS_FILE, SNAPSHOTS_TTL_DAYS)
    cleanup_jsonl(HEARTBEAT_FILE, HEARTBEAT_TTL_DAYS)

    log.info(f"═══ Готово. Watchlist: {len(watchlist)} ═══")


# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log.exception(f"Фатальная ошибка: {e}")
        send_telegram(f"⚠️ <b>Coinalyze Monitor</b>\nОшибка: {esc(str(e)[:500])}")
        sys.exit(1)
