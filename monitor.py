"""
coinalyze_monitor.py
====================
Автоматический внутридневной поиск качественных LONG-кандидатов на крипторынке.

Жизненный цикл: обнаружение → наблюдение → подтверждение → сигнал → угасание → удаление.

Запуск: каждые 5 минут через cron / systemd timer / while-loop.

Переменные окружения:
  COINALYZE_P_SID, COINALYZE_CHAT_SID  — cookies Coinalyze
  TG_BOT_TOKEN, TG_CHAT_ID             — Telegram
  QWEN_API_KEY, QWEN_BASE_URL, QWEN_MODEL — LLM (опционально)
  ENABLE_LLM=true|false                — включать ли LLM-объяснения
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
# КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════

COINALYZE_P_SID   = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")
TG_BOT_TOKEN      = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID        = os.environ.get("TG_CHAT_ID", "")

ENABLE_LLM        = os.environ.get("ENABLE_LLM", "false").lower() == "true"
QWEN_API_KEY      = os.environ.get("QWEN_API_KEY", "")
QWEN_BASE_URL     = os.environ.get("QWEN_BASE_URL",
                                   "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
QWEN_MODEL        = os.environ.get("QWEN_MODEL", "qwen-plus")

COINALYZE_URL = (
    "https://coinalyze.net/"
    "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
    "&filter=ZV9ndF8yLjUmYl9ndF8xJmNfZ3RfMTAwMDAwMCZjbTYxNjVfZ3RfMzUmY202MTY0X2x0XzQ1"
    "&order_by=oi_current&order_dir=desc"
)

# Файлы состояния
DATA_DIR          = Path("data")
SNAPSHOTS_FILE    = DATA_DIR / "snapshots.jsonl"
HEARTBEAT_FILE    = DATA_DIR / "heartbeat.jsonl"
WATCHLIST_FILE    = DATA_DIR / "watchlist.json"
DEBUG_HTML_FILE   = DATA_DIR / "debug_page.html"

# Сроки хранения
SNAPSHOTS_TTL_DAYS  = 7
HEARTBEAT_TTL_DAYS  = 3

# Lifecycle параметры
CONFIRM_SNAPSHOTS   = 3       # мин. снимков для CONFIRMED_LONG
CONFIRM_WINDOW_MIN  = 30      # окно для подтверждения (мин)
RUNNING_SNAPSHOTS   = 4       # мин. снимков для RUNNING
EXIT_NO_RECOVERY    = 3       # снимков без восстановления → REMOVED

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("coinalyze")

# ═══════════════════════════════════════════════════════════
# УТИЛИТЫ
# ═══════════════════════════════════════════════════════════

def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def parse_number(raw: Optional[str]) -> Optional[float]:
    """Парсит числа вида '$1.2M', '+3.5%', 'n/a' и т.д."""
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
# 1. ИСТОЧНИК ДАННЫХ — PLAYWRIGHT
# ═══════════════════════════════════════════════════════════

def fetch_html() -> str:
    """Открывает Coinalyze через Playwright, возвращает HTML."""
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
                cookies.append({
                    "name": "p_sid", "value": COINALYZE_P_SID,
                    "domain": "coinalyze.net", "path": "/", "secure": True,
                })
            if COINALYZE_CHAT_SID:
                cookies.append({
                    "name": "chat_sid", "value": COINALYZE_CHAT_SID,
                    "domain": "coinalyze.net", "path": "/", "secure": True,
                })
            cookies.append({
                "name": "cookies_accepted", "value": "1",
                "domain": "coinalyze.net", "path": "/", "secure": True,
            })
            context.add_cookies(cookies)

        page = context.new_page()
        stealth_sync(page)

        try:
            page.goto(COINALYZE_URL, wait_until="domcontentloaded", timeout=50_000)
            page.wait_for_timeout(4000)
            # Cloudflare challenge
            if "Attention Required" in page.content():
                log.warning("Cloudflare challenge detected, waiting...")
                page.wait_for_timeout(10_000)
            page.wait_for_selector("tbody tr", timeout=25_000)
            html_content = page.content()
        except Exception as e:
            log.error(f"Ошибка загрузки страницы: {e}")
            try:
                html_content = page.content()
            except Exception:
                html_content = ""
            try:
                page.screenshot(path=str(DEBUG_HTML_FILE).replace(".html", ".png"),
                                full_page=True)
            except Exception:
                pass
        finally:
            browser.close()

    return html_content


def parse_table(html_text: str) -> list[dict]:
    """Парсит HTML-таблицу Coinalyze в список словарей."""
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

        rec = {
            "ts":             ts,
            "symbol":         symbol,
            "name":           coin_name,
            "price":          parse_number(tds[2].get_text(strip=True)),
            "price_chg24":    parse_number(tds[3].get_text(strip=True)),
            "mktcap":         parse_number(tds[4].get_text(strip=True)),
            "volume24":       parse_number(tds[5].get_text(strip=True)),
            "oi":             parse_number(tds[6].get_text(strip=True)),
            "oi_chg24_pct":   parse_number(tds[7].get_text(strip=True)),
            "oi_chg4h_pct":   parse_number(tds[9].get_text(strip=True)),
            "oi_vol_ratio":   parse_number(tds[11].get_text(strip=True)),
            "oi_mktcap_ratio":parse_number(tds[12].get_text(strip=True)),
            "fr_avg":         parse_number(tds[13].get_text(strip=True)),
            "pfr_avg":        parse_number(tds[14].get_text(strip=True)),
            "fr_oiw":         parse_number(tds[15].get_text(strip=True)),
            "pfr_oiw":        parse_number(tds[16].get_text(strip=True)),
            "liq_short24":    parse_number(tds[17].get_text(strip=True)),
            "liq_long24":     parse_number(tds[18].get_text(strip=True)),
            "ls_accounts":    parse_number(tds[19].get_text(strip=True)),
            "btc_corr7d":     parse_number(tds[20].get_text(strip=True)),
            "cvd24":          parse_number(tds[21].get_text(strip=True)),
            "lls24":          parse_number(tds[22].get_text(strip=True)),
        }
        records.append(rec)

    return records


def fetch_data() -> list[dict]:
    """Полный цикл: браузер → HTML → парсинг → записи."""
    html_text = fetch_html()

    # Сохраняем debug
    ensure_data_dir()
    DEBUG_HTML_FILE.write_text(html_text, encoding="utf-8")

    rows = parse_table(html_text)
    if not rows:
        send_telegram(
            "⚠️ <b>Coinalyze Monitor</b>\n"
            "Не получены данные. Возможные причины:\n"
            "• Cookies истекли\n"
            "• Изменилась HTML-разметка\n"
            "• Cloudflare блокировка\n\n"
            "Проверь data/debug_page.html"
        )
        sys.exit(1)

    return rows


# ═══════════════════════════════════════════════════════════
# 2. ХРАНЕНИЕ ИСТОРИИ
# ═══════════════════════════════════════════════════════════

def append_jsonl(path: Path, record: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def cleanup_jsonl(path: Path, ttl_days: int):
    """Удаляет записи старше ttl_days."""
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
        log.info(f"Cleanup {path.name}: удалено {removed} записей")


def load_watchlist() -> dict:
    if not WATCHLIST_FILE.exists():
        return {}
    with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_watchlist(wl: dict):
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(wl, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════
# 3. ПЕРВИЧНЫЙ ФИЛЬТР LONG
# ═══════════════════════════════════════════════════════════

def passes_primary_filter(r: dict) -> bool:
    """
    Раздел 3 спецификации. Все условия обязательны.
    """
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

    # Funding не экстремально положительный
    fr = r.get("fr_oiw")
    if fr is not None and fr > 0.05:
        return False

    return True


# ═══════════════════════════════════════════════════════════
# 4. SCORING ENGINE (макс 10)
# ═══════════════════════════════════════════════════════════

def calculate_score(r: dict) -> tuple[int, list[str], list[str]]:
    """
    Раздел 4. Возвращает (score, pros, cons).
    """
    score = 0
    pros = []
    cons = []

    cvd = r.get("cvd24")
    if cvd is not None:
        if cvd > 70:
            score += 2; pros.append(f"CVD24={cvd:.0f} >70 — сильный спрос")
        elif cvd >= 55:
            score += 1; pros.append(f"CVD24={cvd:.0f} 55-70 — умеренный спрос")

    lls = r.get("lls24")
    if lls is not None:
        if lls < 15:
            score += 2; pros.append(f"LLS24={lls:.0f}% <15 — шортов мало")
        elif lls < 40:
            score += 1; pros.append(f"LLS24={lls:.0f}% 15-40 — нормально")

    oi24 = r.get("oi_chg24_pct")
    if oi24 is not None:
        if 5 <= oi24 <= 35:
            score += 2; pros.append(f"OI24={oi24:.1f}% 5-35 — здоровый рост")
        elif oi24 > 50:
            score -= 2; cons.append(f"OI24={oi24:.1f}% >50 — перегрев")

    oi4h = r.get("oi_chg4h_pct")
    if oi4h is not None and oi4h > 0:
        score += 1; pros.append(f"OI4h={oi4h:.1f}% >0 — приток продолжается")

    pc = r.get("price_chg24")
    if pc is not None:
        if 2 <= pc <= 10:
            score += 1; pros.append(f"Price24={pc:.1f}% 2-10 — умеренный рост")
        elif pc > 20:
            score -= 2; cons.append(f"Price24={pc:.1f}% >20 — вертикальный рост")

    fr = r.get("fr_oiw")
    if fr is not None:
        if -0.01 <= fr <= 0.03:
            score += 1; pros.append(f"Funding={fr:.4f} — нормальный")
        elif fr > 0.05:
            score -= 2; cons.append(f"Funding={fr:.4f} — перегрет")

    oi_mc = r.get("oi_mktcap_ratio")
    if oi_mc is not None and oi_mc < 0.10:
        score += 1; pros.append(f"OI/Mcap={oi_mc:.3f} <0.10 — безопасно")

    # Доп. штраф
    if lls is not None and lls > 50:
        score -= 2; cons.append(f"LLS24={lls:.0f}% >50 — массовый выход")

    return score, pros, cons


# ═══════════════════════════════════════════════════════════
# 7. АНАЛИЗ ДИНАМИКИ
# ═══════════════════════════════════════════════════════════

def compute_trend(values: list[Optional[float]]) -> str:
    """Определяет тренд по последним значениям: up / down / flat."""
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return "flat"
    diffs = [clean[i] - clean[i - 1] for i in range(1, len(clean))]
    avg_diff = sum(diffs) / len(diffs)
    threshold = 0.5
    if avg_diff > threshold:
        return "up"
    elif avg_diff < -threshold:
        return "down"
    return "flat"


def analyze_dynamics(snaps: list[dict]) -> dict:
    """
    Раздел 7. Анализирует последние 3-6 снимков.
    """
    if len(snaps) < 2:
        return {"oi_trend": "flat", "cvd_trend": "flat",
                "divergence": "none", "note": "недостаточно данных"}

    recent = snaps[-6:]  # максимум 6

    oi_vals   = [s.get("oi_chg24_pct") for s in recent]
    cvd_vals  = [s.get("cvd24") for s in recent]
    price_vals = [s.get("price_chg24") for s in recent]
    oi4h_vals = [s.get("oi_chg4h_pct") for s in recent]

    oi_trend  = compute_trend(oi_vals)
    cvd_trend = compute_trend(cvd_vals)
    price_trend = compute_trend(price_vals)

    # Дивергенции
    divergence = "none"
    if price_trend == "up" and oi_trend == "down":
        divergence = "price_up_oi_down"  # плохо
    elif price_trend == "up" and cvd_trend == "down":
        divergence = "price_up_cvd_down"  # плохо

    note = ""
    if divergence == "price_up_oi_down":
        note = "Цена растёт, но OI падает — движение на закрытии шортов, не на новом спросе"
    elif divergence == "price_up_cvd_down":
        note = "Цена растёт, но CVD падает — покупатели ослабевают"
    elif oi_trend == "up" and cvd_trend == "up" and price_trend == "up":
        note = "Здоровое движение: цена, OI и CVD растут синхронно"

    return {
        "oi_trend": oi_trend,
        "cvd_trend": cvd_trend,
        "price_trend": price_trend,
        "oi4h_trend": compute_trend(oi4h_vals),
        "divergence": divergence,
        "note": note,
    }


# ═══════════════════════════════════════════════════════════
# 8. ПАТТЕРНЫ
# ═══════════════════════════════════════════════════════════

def detect_pattern(r: dict, dynamics: dict) -> str:
    """
    Раздел 8. Определяет паттерн по текущему снимку + динамике.
    """
    pc   = r.get("price_chg24") or 0
    oi24 = r.get("oi_chg24_pct") or 0
    cvd  = r.get("cvd24") or 50
    lls  = r.get("lls24") or 30
    fr   = r.get("fr_oiw") or 0
    ls   = r.get("ls_accounts") or 1.0

    oi_trend  = dynamics.get("oi_trend", "flat")
    cvd_trend = dynamics.get("cvd_trend", "flat")

    # Healthy Trend
    if pc > 0 and oi24 > 5 and cvd > 60 and lls < 30 and oi_trend == "up":
        return "Healthy Trend"

    # Short Squeeze Setup
    if pc > 0 and oi24 > 5 and lls > 35 and ls < 1.0:
        return "Short Squeeze Setup"

    # Stealth Accumulation
    if pc < 3 and cvd_trend == "up" and fr < 0.005:
        return "Stealth Accumulation"

    # Late Trend
    if pc > 10 and oi24 > 20 and cvd_trend == "down":
        return "Late Trend"

    # Distribution
    if pc < 0 and oi_trend == "down":
        return "Distribution"

    # Capitulation
    if oi24 < -10 and lls > 45 and cvd_trend == "up":
        return "Capitulation"

    return "Neutral"


# ═══════════════════════════════════════════════════════════
# 5-6. LIFECYCLE ENGINE
# ═══════════════════════════════════════════════════════════

STAGES = ["NEW", "WAIT_CONFIRMATION", "CONFIRMED_LONG", "RUNNING",
          "EXIT_WARNING", "REMOVED"]


def get_symbol_snapshots(symbol: str) -> list[dict]:
    """Загружает все снимки монеты из snapshots.jsonl."""
    all_snaps = load_jsonl(SNAPSHOTS_FILE)
    return sorted(
        [s for s in all_snaps if s.get("symbol") == symbol],
        key=lambda s: s["ts"]
    )


def lifecycle_transition(symbol: str, entry: dict, current: dict,
                         snaps: list[dict]) -> tuple[str, list[str]]:
    """
    Разделы 5-6. Определяет переход состояния.
    Возвращает (new_stage, reasons).
    """
    stage = entry.get("stage", "NEW")
    reasons = []
    score = current.get("score", 0)
    dynamics = current.get("dynamics", {})

    # ── NEW → WAIT_CONFIRMATION ──
    if stage == "NEW":
        if score >= 6:
            reasons.append(f"Score={score} ≥6 — первый хороший снимок")
            return "WAIT_CONFIRMATION", reasons
        return stage, reasons

    # ── WAIT_CONFIRMATION → CONFIRMED_LONG ──
    if stage == "WAIT_CONFIRMATION":
        # Нужно минимум 3 снимка за последние 30 минут
        cutoff = now_ts() - CONFIRM_WINDOW_MIN * 60
        recent = [s for s in snaps if s["ts"] > cutoff]
        if len(recent) >= CONFIRM_SNAPSHOTS:
            # Проверяем условия подтверждения
            oi_ok = all(
                (recent[i].get("oi_chg24_pct") or 0) >=
                (recent[i-1].get("oi_chg24_pct") or 0) - 1
                for i in range(1, len(recent))
            )
            oi4h_ok = all(
                (s.get("oi_chg4h_pct") or 0) >= -0.5 for s in recent
            )
            cvd_ok = all(
                (recent[i].get("cvd24") or 50) >=
                (recent[i-1].get("cvd24") or 50) - 5
                for i in range(1, len(recent))
            )
            lls_ok = all((s.get("lls24") or 30) < 50 for s in recent)

            if oi_ok and oi4h_ok and cvd_ok and lls_ok:
                reasons.append(f"{len(recent)} снимков за {CONFIRM_WINDOW_MIN} мин")
                reasons.append("OI24 растёт, OI4h не падает, CVD стабилен, LLS <50")
                return "CONFIRMED_LONG", reasons
            else:
                if not oi_ok:
                    reasons.append("OI24 не растёт последовательно")
                if not oi4h_ok:
                    reasons.append("OI4h падает")
                if not cvd_ok:
                    reasons.append("CVD падает")
                if not lls_ok:
                    reasons.append("LLS >50")
        return stage, reasons

    # ── CONFIRMED_LONG → RUNNING ──
    if stage == "CONFIRMED_LONG":
        if len(snaps) >= RUNNING_SNAPSHOTS and score >= 7:
            reasons.append(f"{len(snaps)} снимков, score={score} ≥7")
            return "RUNNING", reasons
        return stage, reasons

    # ── RUNNING → EXIT_WARNING ──
    if stage == "RUNNING":
        exit_triggered = False

        # Условие 1: OI падает 2 снимка подряд
        if len(snaps) >= 3:
            oi_last3 = [s.get("oi_chg24_pct") or 0 for s in snaps[-3:]]
            if oi_last3[-1] < oi_last3[-2] < oi_last3[-3]:
                exit_triggered = True
                reasons.append("OI падает 2 снимка подряд")

        # Условие 2: CVD падает >15 пунктов
        if len(snaps) >= 2:
            cvd_prev = snaps[-2].get("cvd24") or 50
            cvd_curr = snaps[-1].get("cvd24") or 50
            if cvd_prev - cvd_curr > 15:
                exit_triggered = True
                reasons.append(f"CVD упал на {cvd_prev - cvd_curr:.0f} пунктов")

        # Условие 3: цена растёт, OI уменьшается
        pc = current.get("price_chg24") or 0
        oi_trend = dynamics.get("oi_trend", "flat")
        if pc > 0 and oi_trend == "down":
            exit_triggered = True
            reasons.append("Дивергенция: цена ↑, OI ↓")

        # Условие 4: LLS > 50
        lls = current.get("lls24") or 0
        if lls > 50:
            exit_triggered = True
            reasons.append(f"LLS={lls:.0f}% >50")

        if exit_triggered:
            return "EXIT_WARNING", reasons
        return stage, reasons

    # ── EXIT_WARNING → REMOVED ──
    if stage == "EXIT_WARNING":
        # Нет восстановления 3 снимка подряд
        entry_ts = entry.get("exit_warning_since", 0)
        snaps_since = [s for s in snaps if s["ts"] > entry_ts]
        if len(snaps_since) >= EXIT_NO_RECOVERY:
            # Проверяем: есть ли восстановление?
            recovered = False
            for s in snaps_since[-EXIT_NO_RECOVERY:]:
                if (s.get("oi_chg4h_pct") or 0) > 0 and (s.get("cvd24") or 0) > 55:
                    recovered = True
                    break
            if not recovered:
                reasons.append(f"Нет восстановления {EXIT_NO_RECOVERY} снимка подряд")
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
        log.warning("Telegram не настроен, пропускаю отправку")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    # Разбиваем на чанки
    chunks = []
    remaining = text
    while len(remaining) > 3800:
        split_at = remaining.rfind("\n", 0, 3800)
        if split_at == -1:
            split_at = 3800
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    chunks.append(remaining)

    for chunk in chunks:
        try:
            resp = requests.post(url, data={
                "chat_id": TG_CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=15)
            if resp.status_code != 200:
                # Fallback без HTML
                requests.post(url, data={
                    "chat_id": TG_CHAT_ID,
                    "text": chunk,
                    "disable_web_page_preview": True,
                }, timeout=15)
            time.sleep(0.5)
        except Exception as e:
            log.error(f"Telegram error: {e}")


def format_lifecycle_message(symbol: str, entry: dict, current: dict,
                             snaps: list[dict], reasons: list[str]) -> str:
    """
    Раздел 9. Форматирует сообщение для Telegram.
    """
    stage = entry.get("stage", "?")
    score = current.get("score", 0)
    pattern = current.get("pattern", "Neutral")
    dynamics = current.get("dynamics", {})

    stage_emoji = {
        "CONFIRMED_LONG": "🟢",
        "RUNNING": "🔵",
        "EXIT_WARNING": "🟠",
    }.get(stage, "⚪")

    # Последние 3-5 снимков
    recent = snaps[-5:]
    snap_lines = []
    for s in recent:
        t = time.strftime("%H:%M", time.gmtime(s["ts"]))
        snap_lines.append(
            f"  {t} | Price {s.get('price_chg24', '?')}% | "
            f"OI24 {s.get('oi_chg24_pct', '?')}% | "
            f"OI4h {s.get('oi_chg4h_pct', '?')}% | "
            f"CVD {s.get('cvd24', '?')} | "
            f"LLS {s.get('lls24', '?')}%"
        )
    snap_block = "\n".join(snap_lines) or "  нет данных"

    pros = current.get("pros", [])
    cons = current.get("cons", [])
    pros_block = "\n".join(f"  ✅ {p}" for p in pros) or "  —"
    cons_block = "\n".join(f"  ⚠️ {c}" for c in cons) or "  —"

    # Следующее условие
    next_cond = ""
    if stage == "CONFIRMED_LONG":
        next_cond = "Ждём 4+ снимков и score ≥7 для перехода в RUNNING"
    elif stage == "RUNNING":
        next_cond = "Следим за OI/CVD/LLS — при ухудшении будет EXIT_WARNING"
    elif stage == "EXIT_WARNING":
        next_cond = f"Если {EXIT_NO_RECOVERY} снимка без восстановления → REMOVED"

    msg = (
        f"{stage_emoji} <b>{esc(current.get('name', symbol))} ({esc(symbol)})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Состояние:</b> {esc(stage)}\n"
        f"<b>Score:</b> {score}/10\n"
        f"<b>Паттерн:</b> {esc(pattern)}\n"
        f"<b>Динамика:</b> OI {dynamics.get('oi_trend','?')} | "
        f"CVD {dynamics.get('cvd_trend','?')} | "
        f"Price {dynamics.get('price_trend','?')}\n"
    )
    if dynamics.get("note"):
        msg += f"<i>{esc(dynamics['note'])}</i>\n"

    msg += (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Последние снимки:</b>\n{snap_block}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>ЗА:</b>\n{pros_block}\n"
        f"<b>ПРОТИВ:</b>\n{cons_block}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Причины перехода:</b>\n"
    )
    for r in reasons:
        msg += f"  → {esc(r)}\n"

    if next_cond:
        msg += f"\n<b>Далее:</b> {esc(next_cond)}\n"

    msg += f"\n<i>Информационный сигнал, не финансовая рекомендация.</i>"
    return msg


# ═══════════════════════════════════════════════════════════
# 10. LLM (ОПЦИОНАЛЬНО)
# ═══════════════════════════════════════════════════════════

def llm_explain(symbol: str, entry: dict, current: dict,
                snaps: list[dict]) -> Optional[str]:
    """
    Раздел 10. LLM только объясняет, НЕ принимает решений.
    """
    if not ENABLE_LLM or not QWEN_API_KEY:
        return None

    recent = snaps[-5:]
    snap_summary = "\n".join(
        f"  ts={s['ts']} price_chg={s.get('price_chg24')} "
        f"oi24={s.get('oi_chg24_pct')} oi4h={s.get('oi_chg4h_pct')} "
        f"cvd={s.get('cvd24')} lls={s.get('lls24')} "
        f"score={s.get('score')} pattern={s.get('pattern')}"
        for s in recent
    )

    user_msg = (
        f"Монета: {symbol}\n"
        f"Lifecycle: {entry.get('stage')}\n"
        f"Score: {current.get('score')}\n"
        f"Pattern: {current.get('pattern')}\n"
        f"Dynamics: {json.dumps(current.get('dynamics', {}), ensure_ascii=False)}\n\n"
        f"Последние {len(recent)} снимков:\n{snap_summary}\n\n"
        f"Дай краткое объяснение ситуации (2-3 предложения). "
        f"НЕ давай торговых рекомендаций. Только описание того, что происходит."
    )

    url = f"{QWEN_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {QWEN_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": QWEN_MODEL,
        "messages": [
            {"role": "system",
             "content": "Ты аналитик крипто-деривативов. Объясняй ситуацию кратко и нейтрально."},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.2,
        "max_tokens": 300,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning(f"LLM call failed: {e}")
    return None


# ═══════════════════════════════════════════════════════════
# 12. ОСНОВНОЙ ЦИКЛ
# ═══════════════════════════════════════════════════════════

def run_once():
    """Один полный цикл мониторинга."""
    ensure_data_dir()
    log.info("═══ Запуск цикла мониторинга ═══")

    # ── Шаг 1: Получить данные ──
    rows = fetch_data()
    log.info(f"Получено монет: {len(rows)}")

    # ── Шаг 2: Записать heartbeat (все монеты) ──
    ts = now_ts()
    for r in rows:
        append_jsonl(HEARTBEAT_FILE, {
            "ts": ts,
            "symbol": r["symbol"],
            "price": r.get("price"),
        })

    # ── Шаг 3: Отфильтровать кандидатов ──
    candidates = [r for r in rows if passes_primary_filter(r)]
    log.info(f"Прошли первичный фильтр: {len(candidates)}")

    # ── Шаг 4: Обновить snapshots ──
    watchlist = load_watchlist()

    for r in candidates:
        symbol = r["symbol"]

        # Scoring
        score, pros, cons = calculate_score(r)

        # Dynamics (нужна история)
        sym_snaps = get_symbol_snapshots(symbol)
        # Добавляем текущий снимок временно для анализа
        current_snap = {**r, "score": score, "pros": pros, "cons": cons}
        sym_snaps_with_current = sym_snaps + [current_snap]
        dynamics = analyze_dynamics(sym_snaps_with_current)

        # Pattern
        pattern = detect_pattern(r, dynamics)

        # Формируем полную запись
        full_rec = {
            **r,
            "score": score,
            "pros": pros,
            "cons": cons,
            "dynamics": dynamics,
            "pattern": pattern,
        }

        # Записываем в snapshots.jsonl
        append_jsonl(SNAPSHOTS_FILE, full_rec)

        # ── Шаг 5: Обновить watchlist ──
        if symbol not in watchlist:
            watchlist[symbol] = {
                "stage": "NEW",
                "first_seen": ts,
                "last_seen": ts,
                "snapshots": 1,
                "score": score,
                "warnings": [],
            }
        else:
            watchlist[symbol]["last_seen"] = ts
            watchlist[symbol]["snapshots"] = watchlist[symbol].get("snapshots", 0) + 1
            watchlist[symbol]["score"] = score

        # ── Шаг 6: Lifecycle transition ──
        entry = watchlist[symbol]
        all_snaps = get_symbol_snapshots(symbol)
        new_stage, reasons = lifecycle_transition(
            symbol, entry, full_rec, all_snaps
        )

        old_stage = entry.get("stage", "NEW")
        if new_stage != old_stage:
            log.info(f"[{symbol}] {old_stage} → {new_stage} | {reasons}")
            watchlist[symbol]["stage"] = new_stage

            if new_stage == "EXIT_WARNING":
                watchlist[symbol]["exit_warning_since"] = ts

            # ── Шаг 7-8: Сигналы и Telegram ──
            # Отправляем только для CONFIRMED_LONG, RUNNING, EXIT_WARNING
            if new_stage in ("CONFIRMED_LONG", "RUNNING", "EXIT_WARNING"):
                # LLM объяснение (опционально)
                llm_text = llm_explain(symbol, watchlist[symbol],
                                       full_rec, all_snaps)
                msg = format_lifecycle_message(
                    symbol, watchlist[symbol], full_rec, all_snaps, reasons
                )
                if llm_text:
                    msg += f"\n\n🤖 <b>LLM-комментарий:</b>\n{esc(llm_text)}"

                send_telegram(msg)

        # Удаляем REMOVED из watchlist
        if new_stage == "REMOVED":
            log.info(f"[{symbol}] REMOVED — удаляю из watchlist")
            del watchlist[symbol]

    # ── Шаг 9: Сохранить watchlist, удалить старые данные ──
    save_watchlist(watchlist)
    cleanup_jsonl(SNAPSHOTS_FILE, SNAPSHOTS_TTL_DAYS)
    cleanup_jsonl(HEARTBEAT_FILE, HEARTBEAT_TTL_DAYS)

    log.info(f"═══ Цикл завершён. В watchlist: {len(watchlist)} монет ═══")


# ═══════════════════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════════════════

def main():
    """
    Основной entrypoint.
    Для непрерывной работы: запускает цикл каждые 5 минут.
    Для одноразового запуска (cron): передайте --once.
    """
    once = "--once" in sys.argv

    if once:
        run_once()
    else:
        log.info("Запуск в непрерывном режиме (интервал 5 мин). Ctrl+C для остановки.")
        while True:
            try:
                run_once()
            except KeyboardInterrupt:
                log.info("Остановка по Ctrl+C")
                break
            except Exception as e:
                log.exception(f"Необработанная ошибка в цикле: {e}")
                send_telegram(f"⚠️ <b>Coinalyze Monitor</b>\nОшибка: {esc(str(e)[:500])}")
            log.info("Следующий запуск через 5 минут...")
            time.sleep(300)


if __name__ == "__main__":
    main()
