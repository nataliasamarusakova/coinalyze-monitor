"""
coinalyze_monitor.py
Playwright -> парсинг таблицы -> точный скоринг/режим по коду (раздел 4-5) ->
JSONL лог снимков -> сборка мини-истории по кандидатам ->
LLM-анализ (по промпту целиком, раздел 0-13) -> Telegram.
"""

import os
import sys
import time
import json
import html
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

USE_SAMPLE = os.environ.get("USE_SAMPLE_HTML", "false").lower() == "true"
COINALYZE_P_SID = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

URL = ("https://coinalyze.net/"
       "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
       "&filter=ZV9ndF8yLjUmYl9ndF8xJmNfZ3RfMTAwMDAwMCZjbTYxNjVfZ3RfMzUmY202MTY0X2x0XzQ1"
       "&order_by=oi_current&order_dir=desc")

LOG_FILE = "snapshots.jsonl"
LLM_STATE_FILE = "llm_state.json"
PROMPT_FILE = "analyst_prompt.md"

MIN_SCORE_TO_WATCH = 3          # раздел 11 промпта: анализируем всех с баллом >=3
MIN_SNAPSHOTS_FOR_ANALYSIS = 3   # минимум точек, прежде чем звать LLM
ANALYSIS_WINDOW_MINUTES = 20     # окно, в котором ищем снимки для анализа
REANALYSIS_COOLDOWN_MINUTES = 30 # не дёргаем LLM по той же монете чаще этого
MAX_LLM_CALLS_PER_RUN = 5        # защита от исчерпания бесплатной квоты

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
    if USE_SAMPLE:
        print("Режим теста — проверка переменных окружения пропущена.")
        return
    required = ["COINALYZE_P_SID", "COINALYZE_CHAT_SID", "TG_BOT_TOKEN",
                "TG_CHAT_ID", "GROQ_API_KEY"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"ОШИБКА: не заданы переменные окружения: {missing}")
        sys.exit(1)
    if not os.path.exists(PROMPT_FILE):
        print(f"ОШИБКА: не найден файл {PROMPT_FILE} с текстом промпта.")
        sys.exit(1)
    print("Все переменные окружения и файл промпта на месте.")


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
    if USE_SAMPLE:
        with open("sample.html", "r", encoding="utf-8") as f:
            return fetch_rows_from_html(f.read())

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


# ============ ЛОГ СНАПШОТОВ ============

def load_history():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_history(rec):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


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


# ============ СБОРКА ДАННЫХ ДЛЯ LLM ============

def build_snapshot_log_table(snaps):
    lines = ["| № | Время (UTC) | Режим | Балл | OI24h% | OI4h% | CVD24 | LLS24 | FR_OIW% | Price% |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for i, s in enumerate(snaps, 1):
        t = time.strftime("%H:%M:%S", time.gmtime(s["ts"]))
        lines.append(
            f"| {i} | {t} | {s.get('regime')} | {s.get('score')} | "
            f"{s.get('oi_chg24_pct')} | {s.get('oi_chg4h_pct')} | "
            f"{s.get('cvd24')} | {s.get('lls24')} | {s.get('fr_oiw')} | "
            f"{s.get('price_chg24')} |"
        )
    return "\n".join(lines)


def format_full_metrics(r):
    return (
        f"Coin: {r['name']} ({r['symbol']})\n"
        f"Price: {r['price']} | Price Change % 24H: {r['price_chg24']}%\n"
        f"Market Capitalisation: {r['mktcap']}\n"
        f"Volume 24H: {r['volume24']}\n"
        f"Open Interest: {r['oi']} | OI Change % 24H: {r['oi_chg24_pct']}% "
        f"| OI Change % 4H: {r['oi_chg4h_pct']}%\n"
        f"Open Interest / Volume 24H: {r['oi_vol_ratio']}\n"
        f"Open Interest / Market Capitalization: {r['oi_mktcap_ratio']}\n"
        f"Funding Rate Average: {r['fr_avg']}% | Predicted: {r['pfr_avg']}%\n"
        f"Funding Rate Average, OI Weighted: {r['fr_oiw']}% | Predicted OI-W: {r['pfr_oiw']}%\n"
        f"Short Liquidations 24H: {r['liq_short24']}\n"
        f"Long Liquidations 24H: {r['liq_long24']}\n"
        f"Long/Short Accounts Ratio (1D): {r['ls_accounts']}\n"
        f"BTC Correlation 7D: {r['btc_corr7d']}\n"
        f"CVD24: {r['cvd24']}\n"
        f"LLS24: {r['lls24']}%\n"
    )


# ============ LLM (Groq, бесплатный API) ============

def load_system_prompt():
    with open(PROMPT_FILE, "r", encoding="utf-8") as f:
        return f.read()


def call_llm(system_prompt, user_message):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.2,
        "max_tokens": 2000,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        if resp.status_code != 200:
            print(f"ОШИБКА Groq API: {resp.status_code} — {resp.text[:500]}")
            return None
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"Исключение при вызове Groq API: {e}")
        return None


# ============ TELEGRAM ============

def esc(value):
    return html.escape(str(value), quote=False)


def send_telegram_long(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Telegram не настроен, пропускаю отправку:", text[:200])
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    chunks = []
    while len(text) > 3500:
        split_at = text.rfind("\n", 0, 3500)
        if split_at == -1:
            split_at = 3500
        chunks.append(text[:split_at])
        text = text[split_at:]
    chunks.append(text)

    ok_all = True
    for chunk in chunks:
        try:
            resp = requests.post(url, data={
                "chat_id": TG_CHAT_ID, "text": chunk,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }, timeout=15)
            if resp.status_code != 200:
                print(f"ОШИБКА Telegram API: {resp.status_code} — {resp.text[:300]}")
                # fallback без HTML-разметки, если LLM выдал невалидный HTML
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
    check_env()
    system_prompt = load_system_prompt() if not USE_SAMPLE else ""
    history = load_history()
    llm_state = load_llm_state()
    rows = fetch_rows()
    print(f"Получено монет: {len(rows)}")
    now_ts = int(time.time())

    candidates = []

    for r in rows:
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
        append_history(rec)
        history.append(rec)

        print(f"[{r['symbol']}] профиль={profile} балл={score} режим={regime} теги={all_tags}")

        if score >= MIN_SCORE_TO_WATCH:
            candidates.append(rec)

    # Сортируем кандидатов по баллу — приоритет самым сильным сетапам
    candidates.sort(key=lambda c: c["score"], reverse=True)

    llm_calls_used = 0

    for rec in candidates:
        symbol = rec["symbol"]
        bucket = bucket_of(rec["regime"])

        snaps = recent_snapshots(history, symbol, ANALYSIS_WINDOW_MINUTES)
        if len(snaps) < MIN_SNAPSHOTS_FOR_ANALYSIS:
            print(f"  [{symbol}] недостаточно снимков для анализа "
                  f"({len(snaps)}/{MIN_SNAPSHOTS_FOR_ANALYSIS}), жду ещё")
            continue

        prev = llm_state.get(symbol)
        cooldown_ok = True
        if prev:
            elapsed_min = (now_ts - prev.get("last_analysis_ts", 0)) / 60
            same_bucket = prev.get("last_bucket") == bucket
            if same_bucket and elapsed_min < REANALYSIS_COOLDOWN_MINUTES:
                cooldown_ok = False

        if not cooldown_ok:
            print(f"  [{symbol}] кулдаун ещё не истёк, пропускаю LLM-анализ")
            continue

        if llm_calls_used >= MAX_LLM_CALLS_PER_RUN:
            print(f"  [{symbol}] достигнут лимит LLM-вызовов за прогон, "
                  f"отложено до следующего тика")
            continue

        print(f"  [{symbol}] отправляю на LLM-анализ ({len(snaps)} снимков в истории)")

        log_table = build_snapshot_log_table(snaps)
        full_metrics = format_full_metrics(rec)

        user_message = (
            "Ниже — лог последних снимков этой монеты (раздел 8.3) и полные метрики "
            "последнего снимка. Проведи анализ строго по инструкции (разделы 0-13), "
            "учитывая раздел 7 (методология) и раздел 8 (кросс-снимковый анализ) на "
            "основе приведённого лога. Учти, что скоринг-балл и режим уже посчитаны "
            "кодом точно по формулам — не пересчитывай их, используй как есть, но "
            "оцени их устойчивость и качество тренда во времени.\n\n"
            f"[Лог снимков — {symbol}]\n{log_table}\n\n"
            f"[Полные метрики последнего снимка]\n{full_metrics}"
        )

        llm_answer = call_llm(system_prompt, user_message)
        llm_calls_used += 1

        if llm_answer is None:
            print(f"  [{symbol}] LLM не ответил, пропускаю отправку в Telegram")
            continue

        header = f"🤖 <b>Анализ: {esc(rec['name'])} ({esc(symbol)})</b>\n\n"
        send_telegram_long(header + llm_answer)

        llm_state[symbol] = {"last_analysis_ts": now_ts, "last_bucket": bucket}

    save_llm_state(llm_state)
    print(f"Готово. LLM-вызовов за прогон: {llm_calls_used}")


if __name__ == "__main__":
    run_once()
