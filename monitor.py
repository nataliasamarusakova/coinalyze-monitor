"""
coinalyze_monitor.py
Снимает таблицу Coinalyze -> считает скоринг/режим по промпту v2.5 ->
пишет в snapshots.jsonl -> шлёт алерты в Telegram.
Запускается через GitHub Actions по расписанию.
"""

import os
import sys
import time
import json
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup

# ============ СЕКРЕТЫ (из переменных окружения / GitHub Secrets) ============

def check_env():
    required = ["COINALYZE_P_SID", "COINALYZE_CHAT_SID", "TG_BOT_TOKEN", "TG_CHAT_ID"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"ОШИБКА: не заданы переменные окружения: {missing}")
        sys.exit(1)
    print("Все переменные окружения на месте.")


COOKIES = {
    "p_sid": os.environ.get("COINALYZE_P_SID", ""),
    "chat_sid": os.environ.get("COINALYZE_CHAT_SID", ""),
}
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# ============ URL (должен быть объявлен ДО HEADERS, т.к. HEADERS его использует) ============

URL = ("https://coinalyze.net/"
       "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
       "&filter=ZV9ndF8yLjUmYl9ndF8xJmNfZ3RfMTAwMDAwMCZjbTYxNjVfZ3RfMzUmY202MTY0X2x0XzQ1"
       "&order_by=oi_current&order_dir=desc")

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "ru,en;q=0.9",
    "cache-control": "max-age=0",
    "priority": "u=0, i",
    "referer": URL,
    "sec-ch-ua": '"Chromium";v="148", "YaBrowser";v="26.6", "Not/A)Brand";v="99", "Yowser";v="2.5"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/148.0.0.0 YaBrowser/26.6.0.0 Safari/537.36"),
}

LOG_FILE = "snapshots.jsonl"

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


# ============ ПОЛУЧЕНИЕ ТАБЛИЦЫ ============

def send_telegram(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Telegram не настроен, пропускаю отправку.")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        cffi_requests.post(url, data={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
    except Exception as e:
        print(f"Не удалось отправить Telegram-сообщение: {e}")


def fetch_rows():
    resp = cffi_requests.get(
        URL, headers=HEADERS, cookies=COOKIES,
        impersonate="chrome124",
        timeout=20,
    )
    print(f"HTTP статус: {resp.status_code}")
    print(f"Длина ответа: {len(resp.text)} символов")

    if resp.status_code != 200:
        print("Первые 500 символов ответа:")
        print(resp.text[:500])
        send_telegram(f"⚠️ Coinalyze monitor: статус {resp.status_code}, доступ заблокирован.")
        sys.exit(1)

    soup = BeautifulSoup(resp.text, "lxml")
    rows_found = soup.select("tbody tr")

    if not rows_found:
        print("Строк таблицы не найдено. Первые 1000 символов ответа:")
        print(resp.text[:1000])
        send_telegram("⚠️ Coinalyze monitor: таблица пустая — куки истекли или изменилась разметка.")
        sys.exit(1)

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


# ============ РЕЖИМ (раздел 5 промпта) ============

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


# ============ ЛОГ СНАПШОТОВ (JSONL вместо SQLite — переживает GitHub Actions) ============

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
