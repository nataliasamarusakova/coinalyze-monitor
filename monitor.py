"""
coinalyze_monitor.py
Playwright -> парсинг таблицы -> точный скоринг/режим по коду (раздел 4-5) ->
JSONL лог снимков + heartbeat -> сборка мини-истории и макро-контекста ->
Локальный LLM-анализ (llama-cpp-python, Qwen2.5, ChatML) -> Telegram.

Запускается по внешнему триггеру (cron-job.org -> GitHub repository_dispatch).
"""

import os
import re
import sys
import time
import json
import html
import multiprocessing
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import requests

try:
    from playwright_stealth import stealth_sync
except ImportError:
    print("ПРЕДУПРЕЖДЕНИЕ: playwright_stealth недоступен, продолжаю без него.")
    def stealth_sync(page):
        pass

# ============ НАСТРОЙКИ ============

COINALYZE_P_SID = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

LLM_MODEL_PATH = os.environ.get("LLM_MODEL_PATH", "models/Qwen3.5-4B-Q4_K_M.gguf")
LLM_N_CTX = int(os.environ.get("LLM_N_CTX", "4096"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "700"))

URL = ("https://coinalyze.net/"
       "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
       "&filter=ZV9ndF8yLjUmYl9ndF8xJmNfZ3RfMTAwMDAwMCZjbTYxNjVfZ3RfMzUmY202MTY0X2x0XzQ1"
       "&order_by=oi_current&order_dir=desc")

LOG_FILE = "snapshots.jsonl"
HEARTBEAT_FILE = "heartbeat.jsonl"
LLM_STATE_FILE = "llm_state.json"
PROMPT_FILE = "analyst_prompt_condensed.md"

MIN_SCORE_TO_WATCH = 3
MIN_SNAPSHOTS_FOR_ANALYSIS = 3
ANALYSIS_WINDOW_MINUTES = 20
REANALYSIS_COOLDOWN_MINUTES = 30
MAX_LLM_CALLS_PER_RUN = 3
SLEEP_BETWEEN_LLM_CALLS = 1  # локально нет rate limit, пауза почти не нужна

RETENTION_DAYS_SNAPSHOTS = 14
RETENTION_DAYS_HEARTBEAT = 14
MACRO_CONTEXT_DAYS = 7

BUCKET_MAP = {
    "Healthy Trend": "bullish", "Short Squeeze Setup": "bullish",
    "Mixed": "bullish", "Weak Trend": "bullish", "Capitulation": "bullish",
    "Distribution": "warning", "Exhaustion": "warning",
    "Exhaustion (умеренная)": "warning", "Extreme Exhaustion": "warning",
    "Neutral": "neutral",
}


def bucket_of(regime):
    return BUCKET_MAP.get(regime, "neutral")


def check_env():
    required = ["COINALYZE_P_SID", "COINALYZE_CHAT_SID", "TG_BOT_TOKEN", "TG_CHAT_ID"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"ОШИБКА: не заданы переменные окружения: {missing}")
        sys.exit(1)
    if not os.path.exists(PROMPT_FILE):
        print(f"ОШИБКА: не найден файл {PROMPT_FILE} с текстом промпта.")
        sys.exit(1)
    if not os.path.exists(LLM_MODEL_PATH):
        print(f"ОШИБКА: не найден файл модели {LLM_MODEL_PATH}.")
        sys.exit(1)
    print("Все переменные окружения, промпт и модель на месте.")


# ============ ПАРСИНГ ЧИСЕЛ ============

def parse_number(raw):
    if raw is None:
        return None
    s = raw.strip().replace("$", "").replace("%", "").replace(",", "").replace("+", "")
    if s in ("", "n/a", "-", "—"):
        return None
    mult = 1
    if s and s[-1].lower() in ("k", "m", "b", "t"):
        mult = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}[s[-1].lower()]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def fetch_rows_from_html(html_text):
    soup = BeautifulSoup(html_text, "lxml")
    rows_found = soup.select("tbody tr")
    print(f"Найдено строк: {len(rows_found)}")

    records = []
    for tr in rows_found:
        symbol = tr.get("data-coin")
        tds = tr.find_all("td")
        if len(tds) < 23:
            continue
        name_spans = tds[1].find_all("span")
        coin_name = name_spans[0].get_text(strip=True) if name_spans else symbol

        rec = {
            "ts": int(time.time()),
            "symbol": symbol,
            "name": coin_name,
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
        records.append(rec)
    return records


def fetch_rows_via_browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
        )
        if COINALYZE_P_SID or COINALYZE_CHAT_SID:
            context.add_cookies([
                {"name": "p_sid", "value": COINALYZE_P_SID,
                 "domain": "coinalyze.net", "path": "/", "secure": True},
                {"name": "chat_sid", "value": COINALYZE_CHAT_SID,
                 "domain": "coinalyze.net", "path": "/", "secure": True},
                {"name": "cookies_accepted", "value": "1",
                 "domain": "coinalyze.net", "path": "/", "secure": True},
            ])
        page = context.new_page()
        stealth_sync(page)
        html_content = ""
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            if "Attention Required" in page.content():
                page.wait_for_timeout(8000)
            page.wait_for_selector("tbody tr", timeout=20000)
            html_content = page.content()
        except Exception as e:
            print(f"Ошибка загрузки: {e}")
            try:
                html_content = page.content()
            except Exception:
                pass
            try:
                page.screenshot(path="debug_screenshot.png", full_page=True)
            except Exception:
                pass
        browser.close()
        return html_content


def fetch_rows():
    html_content = fetch_rows_via_browser()
    with open("debug_page.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    rows = fetch_rows_from_html(html_content)

    if not rows:
        send_telegram_long("⚠️ Coinalyze monitor: не получены данные (куки истекли "
                            "или изменилась разметка). Проверь debug_page.html.")
        sys.exit(1)
    return rows


# ============ СКОРИНГ (раздел 4 промпта, точный код) ============

def score_profile_a(r):
    flags = []
    if not (r["volume24"] and r["volume24"] > 10_000_000):
        return False, 0, flags
    if not (r["price_chg24"] is not None and r["price_chg24"] > 0
            and r["oi_chg24_pct"] is not None and r["oi_chg24_pct"] > 0):
        return False, 0, flags

    score = 0
    cvd = r["cvd24"]
    if cvd is not None:
        if cvd > 70: score += 2
        elif cvd >= 50: score += 1
        elif cvd < 35: score -= 1; flags.append("CVD24<35")
    lls = r["lls24"]
    if lls is not None:
        if lls < 15: score += 2
        elif lls <= 35: score += 1
        elif lls > 50: score -= 1; flags.append("LLS24>50%")
    oi = r["oi_chg24_pct"]
    if oi is not None:
        if 5 <= oi <= 35: score += 1
        elif oi > 35: flags.append("экстремальный OI")
    pc = r["price_chg24"]
    if pc is not None:
        if 2 <= pc <= 20: score += 1
        elif pc > 20: flags.append("перегрев цены")
    fr = r["fr_oiw"]
    if fr is not None:
        if -0.01 <= fr <= 0.03: score += 1
        else: flags.append("Funding-дивергенция")
    oim = r["oi_mktcap_ratio"]
    if oim is not None and oim < 0.15: score += 1
    oiv = r["oi_vol_ratio"]
    if oiv is not None and 0.1 <= oiv <= 2.5: score += 1
    ls = r["ls_accounts"]
    if ls is not None:
        if 0.8 <= ls <= 1.5: score += 1
        elif ls > 1.5: score -= 1; flags.append("Ритейл FOMO")
    return True, score, flags


def score_profile_b(r):
    flags = []
    if not (r["volume24"] and r["volume24"] > 10_000_000):
        return False, 0, flags
    if not (r["price_chg24"] is not None and r["price_chg24"] < 0
            and r["oi_chg24_pct"] is not None and r["oi_chg24_pct"] < 0):
        return False, 0, flags
    score = 0
    pc = r["price_chg24"]
    if pc is not None:
        if pc < -8: score += 2
        elif pc <= -3: score += 1
    oi = r["oi_chg24_pct"]
    if oi is not None:
        if oi < -15: score += 2
        elif oi <= -5: score += 1
    lls = r["lls24"]
    if lls is not None:
        if lls > 50: score += 2
        elif lls >= 35: score += 1
    fr = r["fr_oiw"]
    if fr is not None and fr < 0: score += 1
    cvd = r["cvd24"]
    if cvd is not None:
        if cvd < 35: score += 1
        elif cvd > 50: score += 1; flags.append("скрытое накопление на дне")
    return True, score, flags


def classify_regime(r):
    oi, pc, cvd, lls = r["oi_chg24_pct"], r["price_chg24"], r["cvd24"], r["lls24"]
    if oi is None or pc is None:
        return "Neutral", []
    regime = "Neutral"
    if oi < -5:
        regime = "Capitulation" if pc < -3 else "Distribution"
    elif -5 <= oi <= 5:
        regime = "Weak Trend" if (pc > 2 and cvd is not None and cvd < 50) else "Neutral"
    elif 5 < oi <= 15:
        if cvd is not None and cvd > 70 and lls is not None and lls < 25 and 2 <= pc <= 10:
            regime = "Short Squeeze Setup"
        elif cvd is not None and cvd > 50 and lls is not None and lls < 35 and 2 <= pc <= 15:
            regime = "Healthy Trend"
        else:
            regime = "Mixed"
    elif 15 < oi <= 35:
        regime = "Exhaustion" if (pc > 15 and cvd is not None and cvd > 70) else "Exhaustion (умеренная)"
    else:
        regime = "Extreme Exhaustion"

    tags = []
    fr = r["fr_oiw"]
    if regime in ("Healthy Trend", "Short Squeeze Setup") and fr is not None and 0 <= fr <= 0.015:
        tags.append("Stealth Accumulation")
    if regime in ("Healthy Trend", "Short Squeeze Setup", "Mixed") and fr is not None and fr < 0:
        tags.append("Funding-дивергенция")
    if regime in ("Exhaustion", "Extreme Exhaustion") and fr is not None and fr > 0.05:
        tags.append("Euphoria")
    if regime == "Capitulation" and cvd is not None and cvd > 50:
        tags.append("Скрытое накопление на дне")
    ls = r["ls_accounts"]
    if ls is not None and ls > 1.5 and regime in ("Healthy Trend", "Short Squeeze Setup", "Weak Trend"):
        tags.append("Ритейл FOMO")
    return regime, tags


# ============ ЛОГИ: СНАПШОТЫ КАНДИДАТОВ + HEARTBEAT ВСЕХ МОНЕТ ============

def load_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_jsonl(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def prune_jsonl_file(path, retention_days, ts_field="ts"):
    if not os.path.exists(path):
        return
    cutoff = int(time.time()) - retention_days * 86400
    kept_lines = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get(ts_field, 0) > cutoff:
                kept_lines.append(line)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(kept_lines)


def load_llm_state():
    if not os.path.exists(LLM_STATE_FILE):
        return {}
    with open(LLM_STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_llm_state(state):
    with open(LLM_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def recent_snapshots(history, symbol, window_minutes):
    cutoff = int(time.time()) - window_minutes * 60
    return sorted(
        [h for h in history if h["symbol"] == symbol and h["ts"] > cutoff],
        key=lambda h: h["ts"]
    )


# ============ МАКРО-КОНТЕКСТ (долгая память, дёшево по токенам) ============

def build_macro_context(heartbeat_history, symbol, days=MACRO_CONTEXT_DAYS):
    cutoff = int(time.time()) - days * 86400
    entries = sorted(
        [h for h in heartbeat_history if h["symbol"] == symbol and h["ts"] > cutoff],
        key=lambda h: h["ts"]
    )
    if not entries:
        return "Долгосрочная история отсутствует — монета впервые попала в поле зрения."

    prices = [e["price"] for e in entries if e.get("price") is not None]
    if not prices:
        return "Долгосрочная история есть, но без валидных цен."

    now_price = entries[-1]["price"]
    period_high = max(prices)
    period_low = min(prices)
    drawdown = (now_price - period_high) / period_high * 100 if period_high else None
    rise_from_low = (now_price - period_low) / period_low * 100 if period_low else None

    max_gap_hours = 0
    for i in range(1, len(entries)):
        dt_hours = (entries[i]["ts"] - entries[i - 1]["ts"]) / 3600
        if dt_hours > max_gap_hours:
            max_gap_hours = dt_hours
    gap_desc = ""
    if max_gap_hours > 2:
        gap_desc = (f" В истории есть разрыв до {max_gap_hours:.1f}ч — монета "
                    f"временно выпадала из наблюдения.")

    return (
        f"Контекст за {days} дн. ({len(entries)} точек): текущая цена {now_price}, "
        f"максимум {period_high} (сейчас {drawdown:+.1f}% от максимума), "
        f"минимум {period_low} (рост от минимума {rise_from_low:+.1f}%).{gap_desc}"
    )


# ============ СБОРКА ДАННЫХ ДЛЯ LLM ============

def build_snapshot_log_table(snaps):
    lines = ["№ | Время | Режим | Балл | OI24h% | OI4h% | CVD24 | LLS24 | FR_OIW% | Price%"]
    for i, s in enumerate(snaps, 1):
        t = time.strftime("%H:%M", time.gmtime(s["ts"]))
        lines.append(
            f"{i} | {t} | {s.get('regime')} | {s.get('score')} | "
            f"{s.get('oi_chg24_pct')} | {s.get('oi_chg4h_pct')} | "
            f"{s.get('cvd24')} | {s.get('lls24')} | {s.get('fr_oiw')} | "
            f"{s.get('price_chg24')}"
        )
    return "\n".join(lines)


def format_full_metrics(r):
    return (
        f"Coin: {r['name']} ({r['symbol']})\n"
        f"Price: {r['price']} | Price Change 24H: {r['price_chg24']}%\n"
        f"Volume 24H: {r['volume24']} | MktCap: {r['mktcap']}\n"
        f"OI: {r['oi']} | OI Chg 24H: {r['oi_chg24_pct']}% | OI Chg 4H: {r['oi_chg4h_pct']}%\n"
        f"OI/Volume: {r['oi_vol_ratio']} | OI/MktCap: {r['oi_mktcap_ratio']}\n"
        f"Funding OI-W: {r['fr_oiw']}% | Predicted OI-W: {r['pfr_oiw']}%\n"
        f"Short Liq 24H: {r['liq_short24']} | Long Liq 24H: {r['liq_long24']}\n"
        f"L/S Accounts Ratio: {r['ls_accounts']} | BTC Corr 7D: {r['btc_corr7d']}\n"
        f"CVD24: {r['cvd24']} | LLS24: {r['lls24']}%\n"
    )


def load_system_prompt():
    with open(PROMPT_FILE, "r", encoding="utf-8") as f:
        return f.read()


# ============ ЛОКАЛЬНЫЙ LLM (llama-cpp-python, Qwen2.5, ChatML) ============

_llm_instance = None

RESPONSE_SCHEMA_HINT = """
Ответь ТОЛЬКО валидным JSON, без markdown, без текста до/после. Ключи ровно такие:
{
  "signal": "Бычий/Медвежий/Нейтральный/Смешанный",
  "regime": "название режима",
  "tag": "тег или нет",
  "confidence": "Низкая/Средняя/Высокая",
  "persistence_snapshots": число,
  "pros": "краткий список метрик ЗА через точку с запятой",
  "cons": "краткий список метрик ПРОТИВ через точку с запятой",
  "pattern": "название паттерна или нет",
  "dynamics": "1 короткое предложение о динамике между снимками",
  "risks": "1 короткое предложение о рисках",
  "next_check": "какое условие ждать дальше",
  "verdict": "НЕ ВХОДИТЬ/НАБЛЮДАТЬ/РАССМАТРИВАТЬ",
  "verdict_reason": "1 короткое предложение обоснования"
}
Пиши кратко. Все текстовые значения — короткие фразы, не абзацы.
"""


def get_llm():
    global _llm_instance
    if _llm_instance is None:
        from llama_cpp import Llama
        n_threads = multiprocessing.cpu_count()
        print(f"Загружаю локальную модель из {LLM_MODEL_PATH} (n_threads={n_threads})...")
        t0 = time.time()
        _llm_instance = Llama(
            model_path=LLM_MODEL_PATH,
            n_ctx=LLM_N_CTX,
            n_threads=n_threads,
            n_batch=512,
            chat_format="chatml",   # критично для Qwen2.5 — без этого модель обрывает ответы
            use_mmap=False,         # читаем модель в RAM сразу целиком, а не лениво при инференсе
            use_mlock=False,
            verbose=False,
        )
        print(f"Модель загружена за {time.time() - t0:.1f}с (включая полное чтение с диска).")

        # Прогревочный вызов — трогает все веса/кеши один раз, не считается в бюджет анализа
        t0 = time.time()
        _llm_instance.create_chat_completion(
            messages=[{"role": "user", "content": "Привет"}],
            max_tokens=5,
            temperature=0,
        )
        print(f"Прогрев модели занял {time.time() - t0:.1f}с.")
    return _llm_instance


def extract_json_object(text):
    """Достаёт первый сбалансированный {...} блок из текста, даже если вокруг мусор."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None


def call_llm_json(system_prompt, user_message, max_retries=1):
    llm = get_llm()
    full_user_message = user_message + "\n\n" + RESPONSE_SCHEMA_HINT

    for attempt in range(max_retries + 1):
        try:
            t0 = time.time()
            result = llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": full_user_message},
                ],
                temperature=0.0,   # детерминированная генерация — меньше шансов "заблудиться"
                max_tokens=LLM_MAX_TOKENS,
            )
            elapsed = time.time() - t0
            content = result["choices"][0]["message"]["content"]
            print(f"Локальный инференс занял {elapsed:.1f}с, символов ответа: {len(content)}")

            parsed = extract_json_object(content)
            if parsed is not None:
                return parsed

            print(f"Не удалось извлечь валидный JSON (попытка {attempt+1}): {content[:300]}")

        except Exception as e:
            print(f"Исключение при локальном инференсе: {e}")

    return None


# ============ TELEGRAM ============

def esc(value):
    return html.escape(str(value), quote=False)


def safe_get(d, key, default="н/д"):
    val = d.get(key, default)
    if val is None or val == "":
        return default
    return val


def render_verdict_message(rec, v, snaps_count):
    r = rec

    verdict = safe_get(v, "verdict", "НАБЛЮДАТЬ")
    verdict_emoji = {
        "НЕ ВХОДИТЬ": "🔴", "НАБЛЮДАТЬ": "🟡", "РАССМАТРИВАТЬ": "🟢",
    }.get(verdict, "⚪")

    pers_n = v.get("persistence_snapshots", snaps_count)

    return (
        f"{verdict_emoji} <b>{esc(r['name'])} ({esc(r['symbol'])})</b> — "
        f"<b>{esc(verdict)}</b>\n"
        f"Профиль: {esc(rec['profile'])} | Балл: {rec['score']} | "
        f"Снимков в анализе: {snaps_count}\n"
        f"—————————————\n"
        f"<b>1. Сигнал:</b> {esc(safe_get(v, 'signal'))}\n"
        f"<b>2. Режим:</b> {esc(safe_get(v, 'regime', rec['regime']))}\n"
        f"<b>3. Тег:</b> {esc(safe_get(v, 'tag'))}\n"
        f"<b>4. Балл:</b> {rec['score']}\n"
        f"<b>5. Уверенность:</b> {esc(safe_get(v, 'confidence'))} "
        f"(Persistence: {pers_n} снимков)\n"
        f"<b>6. Метрики ЗА:</b> {esc(safe_get(v, 'pros'))}\n"
        f"<b>7. Метрики ПРОТИВ:</b> {esc(safe_get(v, 'cons'))}\n"
        f"<b>8. Паттерн:</b> {esc(safe_get(v, 'pattern'))}\n"
        f"<b>9. Динамика:</b> {esc(safe_get(v, 'dynamics'))}\n"
        f"<b>10. Риски:</b> {esc(safe_get(v, 'risks'))}\n"
        f"<b>11. Тепловая карта:</b> нет данных\n"
        f"<b>12. Усилить/Опровергнуть:</b> {esc(safe_get(v, 'next_check'))}\n"
        f"—————————————\n"
        f"{verdict_emoji} <b>ВЕРДИКТ: {esc(verdict)}</b>\n"
        f"Причина: {esc(safe_get(v, 'verdict_reason'))}\n\n"
        f"<i>Анализ информационный, не финансовая рекомендация.</i>"
    )


def send_telegram_long(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Telegram не настроен, пропускаю отправку:", text[:200])
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    chunks = []
    remaining = text
    while len(remaining) > 3500:
        split_at = remaining.rfind("\n", 0, 3500)
        if split_at == -1:
            split_at = 3500
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    chunks.append(remaining)

    ok_all = True
    for chunk in chunks:
        try:
            resp = requests.post(url, data={
                "chat_id": TG_CHAT_ID, "text": chunk,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }, timeout=15)
            if resp.status_code != 200:
                print(f"ОШИБКА Telegram API: {resp.status_code} — {resp.text[:300]}")
                resp2 = requests.post(url, data={
                    "chat_id": TG_CHAT_ID, "text": chunk,
                    "disable_web_page_preview": True,
                }, timeout=15)
                ok_all = ok_all and resp2.status_code == 200
            time.sleep(0.5)
        except Exception as e:
            print(f"Исключение при отправке в Telegram: {e}")
            ok_all = False
    return ok_all


# ============ ГЛАВНЫЙ ЦИКЛ ============

def run_once():
    run_t0 = time.time()
    check_env()
    system_prompt = load_system_prompt()
    history = load_jsonl(LOG_FILE)
    heartbeat_history = load_jsonl(HEARTBEAT_FILE)
    llm_state = load_llm_state()
    rows = fetch_rows()
    print(f"Получено монет: {len(rows)}")
    now_ts = int(time.time())

    candidates = []

    for r in rows:
        append_jsonl(HEARTBEAT_FILE, {
            "ts": r["ts"], "symbol": r["symbol"], "price": r["price"],
        })
        heartbeat_history.append({"ts": r["ts"], "symbol": r["symbol"], "price": r["price"]})

        passed_a, score_a, flags_a = score_profile_a(r)
        passed_b, score_b, flags_b = score_profile_b(r)

        profile, score, flags = None, 0, []
        if passed_a and score_a >= score_b:
            profile, score, flags = "A", score_a, flags_a
        elif passed_b:
            profile, score, flags = "B", score_b, flags_b

        if profile is None:
            continue

        regime, tags = classify_regime(r)
        all_tags = tags + flags
        rec = {**r, "profile": profile, "score": score,
               "regime": regime, "tags": all_tags}
        append_jsonl(LOG_FILE, rec)
        history.append(rec)

        print(f"[{r['symbol']}] профиль={profile} балл={score} режим={regime} теги={all_tags}")

        if score >= MIN_SCORE_TO_WATCH:
            candidates.append(rec)

    candidates.sort(key=lambda c: c["score"], reverse=True)

    llm_calls_used = 0

    for rec in candidates:
        symbol = rec["symbol"]
        bucket = bucket_of(rec["regime"])

        snaps = recent_snapshots(history, symbol, ANALYSIS_WINDOW_MINUTES)
        if len(snaps) < MIN_SNAPSHOTS_FOR_ANALYSIS:
            print(f"  [{symbol}] недостаточно снимков ({len(snaps)}/{MIN_SNAPSHOTS_FOR_ANALYSIS})")
            continue

        prev = llm_state.get(symbol)
        cooldown_ok = True
        if prev:
            elapsed_min = (now_ts - prev.get("last_analysis_ts", 0)) / 60
            same_bucket = prev.get("last_bucket") == bucket
            if same_bucket and elapsed_min < REANALYSIS_COOLDOWN_MINUTES:
                cooldown_ok = False

        if not cooldown_ok:
            print(f"  [{symbol}] кулдаун ещё не истёк")
            continue

        if llm_calls_used >= MAX_LLM_CALLS_PER_RUN:
            print(f"  [{symbol}] достигнут лимит LLM-вызовов за прогон")
            continue

        print(f"  [{symbol}] отправляю на локальный LLM-анализ ({len(snaps)} снимков)")

        log_table = build_snapshot_log_table(snaps)
        full_metrics = format_full_metrics(rec)
        macro_context = build_macro_context(heartbeat_history, symbol)

        user_message = (
            "Скоринг-балл и режим уже посчитаны кодом — не пересчитывай их, используй "
            "как готовые данные. Проанализируй динамику по логу снимков и макро-контексту, "
            "определи паттерн и вынеси вердикт.\n\n"
            f"[Макро-контекст]\n{macro_context}\n\n"
            f"[Лог снимков — {symbol}]\n{log_table}\n\n"
            f"[Метрики последнего снимка]\n{full_metrics}\n\n"
            f"Балл: {rec['score']} | Режим: {rec['regime']}\n"
        )

        verdict_json = call_llm_json(system_prompt, user_message)
        llm_calls_used += 1

        if verdict_json is None:
            print(f"  [{symbol}] LLM не дал валидный JSON, пропускаю Telegram")
        else:
            msg = render_verdict_message(rec, verdict_json, len(snaps))
            send_telegram_long(msg)
            llm_state[symbol] = {"last_analysis_ts": now_ts, "last_bucket": bucket}

        if llm_calls_used < MAX_LLM_CALLS_PER_RUN:
            time.sleep(SLEEP_BETWEEN_LLM_CALLS)

    save_llm_state(llm_state)
    prune_jsonl_file(LOG_FILE, RETENTION_DAYS_SNAPSHOTS)
    prune_jsonl_file(HEARTBEAT_FILE, RETENTION_DAYS_HEARTBEAT)

    print(f"Готово. LLM-вызовов за прогон: {llm_calls_used}. "
          f"Общее время прогона: {time.time() - run_t0:.1f}с")


if __name__ == "__main__":
    run_once()
