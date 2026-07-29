"""
coinalyze_monitor.py
Playwright (реальный Chromium) + прокси -> обходим Cloudflare challenge ->
парсим таблицу -> скоринг/режим по промпту v2.5 -> JSONL лог -> Telegram.
"""
import os
import sys
import time
import json
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import requests

try:
    from playwright_stealth import stealth_sync
    STEALTH_AVAILABLE = True
except ImportError:
    print("ПРЕДУПРЕЖДЕНИЕ: playwright_stealth.stealth_sync недоступен, "
          "продолжаю без stealth-маскировки.")
    STEALTH_AVAILABLE = False
    def stealth_sync(page):
        pass  # no-op заглушка, чтобы остальной код не менять

# ============ НАСТРОЙКИ ============

USE_SAMPLE = os.environ.get("USE_SAMPLE_HTML", "false").lower() == "true"

COINALYZE_P_SID = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
PROXY_URL = os.environ.get("PROXY_URL", "")  # формат: http://user:pass@host:port

URL = ("https://coinalyze.net/"
       "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
       "&filter=ZV9ndF8yLjUmYl9ndF8xJmNfZ3RfMTAwMDAwMCZjbTYxNjVfZ3RfMzUmY202MTY0X2x0XzQ1"
       "&order_by=oi_current&order_dir=desc")

LOG_FILE = "snapshots.jsonl"


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
            "oi_chg24_abs": parse_number(tds[8].get_text(strip=True)),
            "oi_chg4h_pct": parse_number(tds[9].get_text(strip=True)),
            "oi_chg4h_abs": parse_number(tds[10].get_text(strip=True)),
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
    proxy_config = None
    if PROXY_URL:
        # Playwright ожидает proxy отдельно от логина/пароля в структуре
        # Формат PROXY_URL: http://user:pass@host:port
        import re
        m = re.match(r"https?://(?:([^:]+):([^@]+)@)?([^:/]+):(\d+)", PROXY_URL)
        if m:
            user, pwd, host, port = m.groups()
            proxy_config = {"server": f"http://{host}:{port}"}
            if user and pwd:
                proxy_config["username"] = user
                proxy_config["password"] = pwd

    with sync_playwright() as p:
        launch_kwargs = {"headless": True}
        if proxy_config:
            launch_kwargs["proxy"] = proxy_config

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
        )

        if COINALYZE_P_SID or COINALYZE_CHAT_SID:
            context.add_cookies([
                {"name": "p_sid", "value": COINALYZE_P_SID,
                 "domain": "coinalyze.net", "path": "/"},
                {"name": "chat_sid", "value": COINALYZE_CHAT_SID,
                 "domain": "coinalyze.net", "path": "/"},
            ])

        page = context.new_page()
        stealth_sync(page)

        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            # Даём время на Cloudflare challenge / дорисовку таблицы
            page.wait_for_timeout(5000)

            # Если challenge есть - подождать подольше и попробовать ещё раз
            content_check = page.content()
            if "Attention Required" in content_check or "cf-browser-verification" in content_check:
                print("Обнаружен Cloudflare challenge, ждём подольше...")
                page.wait_for_timeout(10000)

            page.wait_for_selector("tbody tr", timeout=20000)
            html = page.content()
        except Exception as e:
            print(f"Ошибка при загрузке страницы: {e}")
            html = page.content()
            page.screenshot(path="debug_screenshot.png", full_page=True)
            with open("debug_page.html", "w", encoding="utf-8") as f:
                f.write(html)
            browser.close()
            send_telegram(f"⚠️ Coinalyze monitor: ошибка загрузки страницы: {e}")
            sys.exit(1)

        browser.close()
        return html


def fetch_rows():
    if USE_SAMPLE:
        print("РЕЖИМ ТЕСТА: читаю sample.html вместо реального запроса к Coinalyze.")
        with open("sample.html", "r", encoding="utf-8") as f:
            html_text = f.read()
        return fetch_rows_from_html(html_text)

    html = fetch_rows_via_browser()
    rows = fetch_rows_from_html(html)

    if not rows:
        print("Строк не найдено после полной загрузки. Сохраняю debug-артефакты.")
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(html)
        send_telegram("⚠️ Coinalyze monitor: таблица пустая после загрузки браузером.")
        sys.exit(1)

    return rows


# ============ СКОРИНГ (раздел 4 промпта) ============

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
        elif cvd < 35: score -= 1; flags.append("CVD24<35 внутри бычьего сетапа")

    lls = r["lls24"]
    if lls is not None:
        if lls < 15: score += 2
        elif lls <= 35: score += 1
        elif lls > 50: score -= 1; flags.append("LLS24>50% давление на лонги")

    oi = r["oi_chg24_pct"]
    if oi is not None:
        if 5 <= oi <= 35: score += 1
        elif oi > 35: flags.append("экстремальный выброс OI")

    pc = r["price_chg24"]
    if pc is not None:
        if 2 <= pc <= 20: score += 1
        elif pc > 20: flags.append("перегрев цены")

    fr = r["fr_oiw"]
    if fr is not None:
        if -0.01 <= fr <= 0.03: score += 1
        else: flags.append("Funding-дивергенция")

    oim = r["oi_mktcap_ratio"]
    if oim is not None and oim < 0.15:
        score += 1

    oiv = r["oi_vol_ratio"]
    if oiv is not None and 0.1 <= oiv <= 2.5:
        score += 1

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
    if fr is not None and fr < 0:
        score += 1

    cvd = r["cvd24"]
    if cvd is not None:
        if cvd < 35: score += 1
        elif cvd > 50:
            score += 1
            flags.append("возможное скрытое накопление на дне")

    return True, score, flags


def classify_regime(r):
    oi = r["oi_chg24_pct"]
    pc = r["price_chg24"]
    cvd = r["cvd24"]
    lls = r["lls24"]

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


def persistence(history, symbol, regime, hours=48):
    cutoff = int(time.time()) - hours * 3600
    relevant = [h for h in reversed(history)
                if h["symbol"] == symbol and h["ts"] > cutoff]
    count = 0
    for h in relevant:
        if h["regime"] == regime:
            count += 1
        else:
            break
    return count


def send_telegram(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Telegram не настроен, пропускаю отправку:", text)
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
    except Exception as e:
        print(f"Не удалось отправить Telegram-сообщение: {e}")


def format_alert(r, profile, score, regime, tags, pers):
    tag_str = ", ".join(tags) if tags else "нет"
    return (
        f"<b>{r['name']} ({r['symbol']})</b>\n"
        f"Профиль: {profile} | Балл: {score}\n"
        f"Режим: <b>{regime}</b>\n"
        f"Тег: {tag_str}\n"
        f"Persistence: {pers} снимков\n"
        f"Price: {r['price']} ({r['price_chg24']}%)\n"
        f"OI 24H: {r['oi_chg24_pct']}% | OI 4H: {r['oi_chg4h_pct']}%\n"
        f"FR OI-W: {r['fr_oiw']}% | CVD24: {r['cvd24']} | LLS24: {r['lls24']}%\n"
    )


# ============ ГЛАВНЫЙ ЦИКЛ ============

def run_once():
    check_env()
    history = load_history()
    rows = fetch_rows()
    print(f"Получено монет: {len(rows)}")

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

        threshold = 6 if profile == "A" else 5
        if score >= 3:
            pers = persistence(history, r["symbol"], regime)
            if score >= threshold or pers in (3, 6):
                send_telegram(format_alert(r, profile, score, regime, all_tags, pers))

    print("Готово.")


if __name__ == "__main__":
    run_once()
