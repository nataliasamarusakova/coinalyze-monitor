"""
coinalyze_monitor.py
Playwright + прокси -> Cloudflare bypass -> парсинг -> скоринг/режим ->
устойчивый к шуму Persistence Score -> дедуп алертов -> Telegram.
"""

import os
import sys
import re
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
PROXY_URL = os.environ.get("PROXY_URL", "")

URL = ("https://coinalyze.net/"
       "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
       "&filter=ZV9ndF8yLjUmYl9ndF8xJmNfZ3RfMTAwMDAwMCZjbTYxNjVfZ3RfMzUmY202MTY0X2x0XzQ1"
       "&order_by=oi_current&order_dir=desc")

LOG_FILE = "snapshots.jsonl"
STATE_FILE = "alerted_state.json"

PERSISTENCE_WINDOW_HOURS = 3       # окно, в котором ищем историю для Persistence
GRACE_SECONDS = 20 * 60            # 20 минут молчания = считать "реакселерацией", не шумом

BUCKET_MAP = {
    "Healthy Trend": "bullish",
    "Short Squeeze Setup": "bullish",
    "Mixed": "bullish",
    "Weak Trend": "bullish",
    "Capitulation": "bullish",
    "Distribution": "warning",
    "Exhaustion": "warning",
    "Exhaustion (умеренная)": "warning",
    "Extreme Exhaustion": "warning",
    "Neutral": "neutral",
}

STAGE_LABELS = {
    "early": "🟡 РАННИЙ СИГНАЛ (1 снимок, не подтверждён)",
    "confirmed_3": "🟢 ПОДТВЕРЖДЕНО (устойчиво 3+ снимка)",
    "confirmed_6": "🟢🟢 ВЫСОКАЯ УВЕРЕННОСТЬ (устойчиво 6+ снимков)",
}


def bucket_of(regime):
    return BUCKET_MAP.get(regime, "neutral")


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
    proxy_config = None
    if PROXY_URL:
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
            content_check = page.content()
            if "Attention Required" in content_check:
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
        send_telegram("⚠️ Coinalyze monitor: не получены данные (куки истекли "
                       "или изменилась разметка). Проверь debug_page.html.")
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


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def persistence_robust(history, symbol, current_bucket, window_hours=PERSISTENCE_WINDOW_HOURS):
    """
    Считает снимки того же 'направления' (bucket), допуская пропуски
    (когда монета временно не прошла ворота — шум), но обрывая счёт,
    если встретился снимок ПРОТИВОПОЛОЖНОГО направления (реальный разворот).
    """
    cutoff = int(time.time()) - window_hours * 3600
    relevant = [h for h in reversed(history)
                if h["symbol"] == symbol and h["ts"] > cutoff]
    count = 0
    for h in relevant:
        h_bucket = bucket_of(h.get("regime"))
        if h_bucket == current_bucket:
            count += 1
        elif h_bucket == "neutral":
            continue  # нейтральный снимок — не шум и не разворот, просто пропускаем
        else:
            break  # встретили противоположный бакет — настоящий разворот, стоп
    return count


# ============ TELEGRAM ============

def send_telegram(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Telegram не настроен, пропускаю отправку:", text)
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={
            "chat_id": TG_CHAT_ID, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }, timeout=15)
        print(f"Telegram ответ: статус={resp.status_code}, тело={resp.text[:500]}")
        if resp.status_code != 200:
            print(f"ОШИБКА Telegram API: {resp.status_code} — {resp.text}")
            return False
        data = resp.json()
        if not data.get("ok"):
            print(f"Telegram API вернул ok=false: {data}")
            return False
        print("Сообщение в Telegram успешно отправлено.")
        return True
    except Exception as e:
        print(f"Исключение при отправке в Telegram: {e}")
        return False


def esc(value):
    return html.escape(str(value), quote=False)


def format_alert(r, profile, score, regime, tags, pers, stage_label, reaccel=False):
    tag_str = ", ".join(tags) if tags else "нет"
    prefix = "🔄 РЕАКСЕЛЕРАЦИЯ ПОСЛЕ ПАУЗЫ\n" if reaccel else ""
    return (
        f"{prefix}{stage_label}\n"
        f"<b>{esc(r['name'])} ({esc(r['symbol'])})</b>\n"
        f"Профиль: {esc(profile)} | Балл: {score}\n"
        f"Режим: <b>{esc(regime)}</b>\n"
        f"Тег: {esc(tag_str)}\n"
        f"Persistence: {pers} снимков (окно {PERSISTENCE_WINDOW_HOURS}ч)\n"
        f"Price: {r['price']} ({r['price_chg24']}%)\n"
        f"OI 24H: {r['oi_chg24_pct']}% | OI 4H: {r['oi_chg4h_pct']}%\n"
        f"FR OI-W: {r['fr_oiw']}% | CVD24: {r['cvd24']} | LLS24: {r['lls24']}%\n"
    )


# ============ ГЛАВНЫЙ ЦИКЛ ============

def run_once():
    check_env()
    history = load_history()
    state = load_state()
    rows = fetch_rows()
    print(f"Получено монет: {len(rows)}")
    now_ts = int(time.time())

    seen_symbols = set()

    for r in rows:
        passed_a, score_a, flags_a = score_profile_a(r)
        passed_b, score_b, flags_b = score_profile_b(r)

        profile, score, flags = None, 0, []
        if passed_a and score_a >= score_b:
            profile, score, flags = "A", score_a, flags_a
        elif passed_b:
            profile, score, flags = "B", score_b, flags_b

        symbol = r["symbol"]

        if profile is None:
            # Ворота не пройдены на этом тике — НЕ трогаем state,
            # last_seen_ts остаётся старым => это и даёт возможность
            # обнаружить "реакселерацию", когда монета вернётся.
            continue

        seen_symbols.add(symbol)
        regime, tags = classify_regime(r)
        all_tags = tags + flags
        rec = {**r, "profile": profile, "score": score,
               "regime": regime, "tags": all_tags}
        append_history(rec)
        history.append(rec)

        if score < 3:
            continue  # даже самого низкого качества нет — пропускаем

        threshold = 6 if profile == "A" else 5
        bucket = bucket_of(regime)
        pers = persistence_robust(history, symbol, bucket)

        print(f"[{symbol}] профиль={profile} балл={score} порог={threshold} "
              f"режим={regime} бакет={bucket} persistence={pers} теги={all_tags}")

        prev = state.get(symbol)

        # Определяем стадию только если балл достиг "качественного" порога
        stage = None
        if score >= threshold:
            if pers >= 6:
                stage = "confirmed_6"
            elif pers >= 3:
                stage = "confirmed_3"
            else:
                stage = "early"

        if stage is None:
            # Балл 3..threshold-1 — просто наблюдаем, без алерта,
            # но обновляем last_seen_ts, чтобы не считать это "паузой"
            state[symbol] = {
                "stage": prev.get("stage") if prev else None,
                "bucket": bucket,
                "last_seen_ts": now_ts,
            }
            continue

        # Реакселерация: раньше уже был в state, но давно не появлялся
        reaccel = bool(prev) and (now_ts - prev.get("last_seen_ts", now_ts) > GRACE_SECONDS)

        should_alert = False
        if reaccel:
            should_alert = True
        elif prev is None or prev.get("stage") != stage:
            should_alert = True

        if should_alert:
            label = STAGE_LABELS.get(stage, stage)
            print(f"  -> Отправляю алерт по {symbol} (стадия={stage}, реакселерация={reaccel})")
            send_telegram(format_alert(r, profile, score, regime, all_tags, pers, label, reaccel))

        state[symbol] = {"stage": stage, "bucket": bucket, "last_seen_ts": now_ts}

    save_state(state)
    print("Готово.")


if __name__ == "__main__":
    run_once()
