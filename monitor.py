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


# ================== НАСТРОЙКИ ==================

USE_SAMPLE = os.environ.get("USE_SAMPLE_HTML", "false").lower() == "true"

COINALYZE_P_SID = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# Более мягкий фильтр, как вы указали (без прокси, прокси убрали)
URL = ("https://coinalyze.net/"
       "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
       "&filter=ZV9ndF8yLjUmYl9ndF8zJmNfZ3RfMTAwMDAwJmNtNjE2NV9ndF80MCZjbTYxNjRfbHRfNjU"
       "&order_by=price_24hour_pchange&order_dir=desc")

LOG_FILE = "snapshots.jsonl"
STATE_FILE = "alerted_state.json"

STREAK_FOR_VERDICT = 4   # 3 успешных анализа накопились -> на 4-м даём вердикт


# ================== БАЗОВЫЕ УТИЛИТЫ ==================

def check_env():
    if USE_SAMPLE:
        print("Режим теста — проверка переменных окружения пропущена.")
        return
    required = ["COINALYZE_P_SID", "COINALYZE_CHAT_SID", "TG_BOT_TOKEN", "TG_CHAT_ID"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"ОШИБКА: не заданы переменные окружения: {missing}")
        sys.exit(1)
    print("Все переменные окружения на месте.")


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


def esc(value):
    return html.escape(str(value), quote=False)


def unique_list(items):
    return list(dict.fromkeys([x for x in items if x]))


# ================== TELEGRAM ==================

def send_telegram(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Telegram не настроен, пропускаю отправку:", text)
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
        print(f"Telegram ответ: статус={resp.status_code}, тело={resp.text[:300]}")
        if resp.status_code != 200:
            print(f"ОШИБКА Telegram API: {resp.status_code} — {resp.text}")
            return False
        return True
    except Exception as e:
        print(f"Исключение при отправке в Telegram: {e}")
        return False


# ================== ПОЛУЧЕНИЕ ДАННЫХ (БЕЗ ПРОКСИ) ==================

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
        browser = p.chromium.launch(headless=True)  # без прокси
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
        )

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
            page.wait_for_timeout(4000)
            content_check = page.content()
            if "Attention Required" in content_check:
                print("Обнаружен Cloudflare challenge, ждём подольше...")
                page.wait_for_timeout(9000)
            page.wait_for_selector("tbody tr", timeout=20000)
            html_content = page.content()
        except Exception as e:
            print(f"Ошибка загрузки страницы: {e}")
            try:
                html_content = page.content()
            except Exception:
                html_content = ""
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
        send_telegram("⚠️ Coinalyze monitor: данные не получены. "
                       "Проверь куки (могли истечь) или доступ (без прокси мог вернуться Cloudflare-блок).")
        sys.exit(1)
    return rows


# ================== СКОРИНГ — РАЗДЕЛ 4 ПРОМПТА, БЕЗ ИЗМЕНЕНИЙ ==================

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
    oi = r["oi_chg24_pct"]
    pc = r["price_chg24"]
    cvd = r["cvd24"]
    lls = r["lls24"]

    if oi is None or pc is None:
        return "Neutral", []

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


# ================== ИСТОРИЯ ==================

def load_history():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_history(rec):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ================== ВЕРДИКТ ПОСЛЕ 4 АНАЛИЗОВ ПОДРЯД ==================

def avg(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def make_verdict(streak_snapshots, current):
    """
    streak_snapshots — последние N (>=4) снимков подряд, где монета прошла
    жёсткие ворота (список словарей rec, от старого к новому).
    Возвращает (verdict, reasons).
    """
    scores = [x["score"] for x in streak_snapshots]
    cvds = [x["cvd24"] for x in streak_snapshots]
    llss = [x["lls24"] for x in streak_snapshots]
    regimes = [x["regime"] for x in streak_snapshots]

    first_score, last_score = scores[0], scores[-1]
    cvd_avg = avg(cvds)
    lls_avg = avg(llss)

    warning_regimes = {"Distribution", "Exhaustion", "Extreme Exhaustion"}
    warning_count = sum(1 for reg in regimes if reg in warning_regimes)

    reasons = []
    profile = current["profile"]
    regime = current["regime"]

    # --- Явное ухудшение за окно ---
    degrade_reasons = []
    if last_score < first_score - 1:
        degrade_reasons.append(f"балл упал за окно: {first_score} -> {last_score}")
    if cvd_avg is not None and cvd_avg < 45:
        degrade_reasons.append(f"средний CVD24 низкий: {cvd_avg:.1f}")
    if lls_avg is not None and lls_avg > 45:
        degrade_reasons.append(f"средний LLS24 высокий: {lls_avg:.1f}%")
    if warning_count >= 2:
        degrade_reasons.append(f"{warning_count}/{len(regimes)} снимков в предупреждающем режиме")
    if regime in warning_regimes:
        degrade_reasons.append(f"текущий режим: {regime}")

    if degrade_reasons:
        return "NOT_SUITABLE", degrade_reasons

    # --- Явное перегрев (не входить по рынку, но не "отмена", а предупреждение) ---
    if current.get("price_chg24") is not None and current["price_chg24"] > 20:
        reasons.append("цена перегрета (>20% за 24ч), риск входа по рынку высокий")
    if current.get("oi_chg24_pct") is not None and current["oi_chg24_pct"] > 35:
        reasons.append("OI вырос экстремально (>35% за 24ч) — риск позднего входа")

    if reasons:
        return "OVERHEATED", reasons

    # --- Хорошая устойчивая структура ---
    good_reasons = []
    if last_score >= 6:
        good_reasons.append(f"балл держится высоким: {last_score}")
    if last_score >= first_score:
        good_reasons.append(f"балл не падает за окно: {first_score} -> {last_score}")
    if cvd_avg is not None and cvd_avg >= 55:
        good_reasons.append(f"средний CVD24 хороший: {cvd_avg:.1f}")
    if lls_avg is not None and lls_avg <= 35:
        good_reasons.append(f"средний LLS24 в норме: {lls_avg:.1f}%")
    if warning_count == 0:
        good_reasons.append("ни одного предупреждающего режима за окно")

    if last_score >= 6 and warning_count == 0 and (cvd_avg is None or cvd_avg >= 50):
        return "GOOD_FOR_LONG", good_reasons

    return "MIXED", ["сигналы неоднозначны, часть условий не выполнена"] + good_reasons


VERDICT_LABELS = {
    "GOOD_FOR_LONG": "✅ МОЖНО РАССМАТРИВАТЬ В ЛОНГ",
    "NOT_SUITABLE": "❌ НЕ ПОДХОДИТ (структура ослабла)",
    "OVERHEATED": "⚠️ ПЕРЕГРЕТО — не входить по рынку",
    "MIXED": "⏳ СМЕШАННО / ЖДАТЬ ЕЩЁ ПОДТВЕРЖДЕНИЯ",
}


def format_window_table(streak_snapshots):
    lines = ["время | балл | режим | price24 | oi24 | oi4h | CVD | LLS"]
    for x in streak_snapshots:
        t = time.strftime("%H:%M", time.localtime(x["ts"]))
        lines.append(
            f"{t} | {x['score']} | {x['regime']} | {x['price_chg24']} | "
            f"{x['oi_chg24_pct']} | {x['oi_chg4h_pct']} | {x['cvd24']} | {x['lls24']}"
        )
    return "\n".join(lines)


def format_verdict_message(current, streak_snapshots, verdict, reasons):
    reasons_text = "\n".join(f"• {esc(r)}" for r in reasons)
    tags = current.get("tags") or []
    tags_text = ", ".join(tags) if tags else "нет"

    return (
        f"{VERDICT_LABELS.get(verdict, verdict)}\n"
        f"<b>{esc(current['name'])} ({esc(current['symbol'])})</b>\n\n"
        f"Профиль: {esc(current['profile'])} | Балл сейчас: {current['score']}\n"
        f"Режим сейчас: <b>{esc(current['regime'])}</b>\n"
        f"Тег: {esc(tags_text)}\n\n"
        f"<b>Причины вердикта</b>\n{reasons_text}\n\n"
        f"<b>Снимки, на которых основан вердикт ({len(streak_snapshots)} шт., "
        f"~{len(streak_snapshots) * 5} мин)</b>\n"
        f"<pre>{esc(format_window_table(streak_snapshots))}</pre>"
    )


# ================== ГЛАВНЫЙ ЦИКЛ ==================

def run_once():
    check_env()
    history = load_history()
    state = load_state()
    rows = fetch_rows()
    print(f"Получено монет: {len(rows)}")

    for r in rows:
        symbol = r["symbol"]

        passed_a, score_a, flags_a = score_profile_a(r)
        passed_b, score_b, flags_b = score_profile_b(r)

        profile, score, flags = None, 0, []
        if passed_a and score_a >= score_b:
            profile, score, flags = "A", score_a, flags_a
        elif passed_b:
            profile, score, flags = "B", score_b, flags_b

        regime, tags = classify_regime(r)
        all_tags = unique_list(tags + flags)

        rec = {
            **r,
            "profile": profile,
            "score": score,
            "regime": regime,
            "tags": all_tags,
        }
        append_history(rec)
        history.append(rec)

       raw_prev = state.get(symbol, {})
prev = {
    "streak": raw_prev.get("streak", 0),
    "streak_snapshots": raw_prev.get("streak_snapshots", []),
    "last_verdict": raw_prev.get("last_verdict"),
}
        # Жёсткое условие для входа в счётчик: ворота пройдены + минимальное
        # качество (score>=3), иначе это не "успешный анализ", а мусор.
        if profile is not None and score >= 3:
            streak = prev["streak"] + 1
            streak_snaps = prev["streak_snapshots"] + [rec]
            streak_snaps = streak_snaps[-8:]  # держим не более 8 последних для окна
        else:
            streak = 0
            streak_snaps = []

        print(f"[{symbol}] streak={streak} score={score} regime={regime} profile={profile}")

        verdict = None
        reasons = []

        if streak >= STREAK_FOR_VERDICT:
            verdict, reasons = make_verdict(streak_snaps[-STREAK_FOR_VERDICT:], rec)
            print(f"  -> Вердикт для {symbol}: {verdict} ({reasons})")

            if verdict != prev.get("last_verdict"):
                send_telegram(format_verdict_message(rec, streak_snaps[-STREAK_FOR_VERDICT:], verdict, reasons))

        state[symbol] = {
            "streak": streak,
            "streak_snapshots": streak_snaps,
            "last_verdict": verdict if verdict else prev.get("last_verdict"),
        }

    save_state(state)
    print("Готово.")


if __name__ == "__main__":
    run_once()
