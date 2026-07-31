import os
import re
import sys
import time
import json
import html
import uuid
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

QWEN_RESPONSE_TIMEOUT_S = 120

URL = ("https://coinalyze.net/"
       "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
       "&filter=ZV9ndF8yLjUmYl9ndF8xJmNfZ3RfMTAwMDAwMCZjbTYxNjVfZ3RfMzUmY202MTY0X2x0XzQ1"
       "&order_by=oi_current&order_dir=desc")

LOG_FILE = "snapshots.jsonl"
LLM_STATE_FILE = "llm_state.json"
PROMPT_FILE = "analyst_prompt.md"

MIN_SCORE_TO_WATCH = 3
MIN_SNAPSHOTS_FOR_ANALYSIS = 3
ANALYSIS_WINDOW_MINUTES = 20
REANALYSIS_COOLDOWN_MINUTES = 30
MAX_LLM_CALLS_PER_RUN = 4
SLEEP_BETWEEN_LLM_CALLS = 5

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
    required = ["COINALYZE_P_SID", "COINALYZE_CHAT_SID", "TG_BOT_TOKEN", "TG_CHAT_ID"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"ОШИБКА: не заданы переменные окружения: {missing}")
        sys.exit(1)
    if not os.path.exists(PROMPT_FILE):
        print(f"ОШИБКА: не найден файл {PROMPT_FILE} с текстом промпта.")
        sys.exit(1)
    print("Все переменные окружения и файл промпта на месте. LLM: прямой API Qwen.")

# ============ ПАРСИНГ ЧИСЕЛ И ДАННЫХ COINALYZE ============

def parse_number(raw):
    if raw is None: return None
    s = raw.strip().replace("$", "").replace("%", "").replace(",", "").replace("+", "")
    if s in ("", "n/a", "-", "—"): return None
    mult = 1
    if s and s[-1].lower() in ("k", "m", "b", "t"):
        mult = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}[s[-1].lower()]
        s = s[:-1]
    try: return float(s) * mult
    except ValueError: return None

def fetch_rows_from_html(html_text):
    soup = BeautifulSoup(html_text, "lxml")
    rows_found = soup.select("tbody tr")
    print(f"Найдено строк: {len(rows_found)}")
    records = []
    for tr in rows_found:
        symbol = tr.get("data-coin")
        tds = tr.find_all("td")
        if len(tds) < 23: continue
        name_spans = tds[1].find_all("span")
        coin_name = name_spans[0].get_text(strip=True) if name_spans else symbol
        rec = {
            "ts": int(time.time()), "symbol": symbol, "name": coin_name,
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
            viewport={"width": 1920, "height": 1080}, locale="ru-RU",
        )
        if COINALYZE_P_SID or COINALYZE_CHAT_SID:
            context.add_cookies([
                {"name": "p_sid", "value": COINALYZE_P_SID, "domain": "coinalyze.net", "path": "/", "secure": True},
                {"name": "chat_sid", "value": COINALYZE_CHAT_SID, "domain": "coinalyze.net", "path": "/", "secure": True},
                {"name": "cookies_accepted", "value": "1", "domain": "coinalyze.net", "path": "/", "secure": True},
            ])
        page = context.new_page()
        stealth_sync(page)
        html_content = ""
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            if "Attention Required" in page.content(): page.wait_for_timeout(8000)
            page.wait_for_selector("tbody tr", timeout=20000)
            html_content = page.content()
        except Exception as e:
            print(f"Ошибка загрузки Coinalyze: {e}")
            try: html_content = page.content()
            except Exception: pass
        browser.close()
        return html_content

def fetch_rows():
    if USE_SAMPLE:
        with open("sample.html", "r", encoding="utf-8") as f:
            return fetch_rows_from_html(f.read())
    html_content = fetch_rows_via_browser()
    with open("debug_page.html", "w", encoding="utf-8") as f: f.write(html_content)
    rows = fetch_rows_from_html(html_content)
    if not rows:
        send_telegram_long("⚠️ Coinalyze monitor: не получены данные.")
        sys.exit(1)
    return rows

# ============ СКОРИНГ И РЕЖИМЫ ============

def score_profile_a(r):
    flags = []
    if not (r["volume24"] and r["volume24"] > 10_000_000): return False, 0, flags
    if not (r["price_chg24"] is not None and r["price_chg24"] > 0 and r["oi_chg24_pct"] is not None and r["oi_chg24_pct"] > 0): return False, 0, flags
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
    if not (r["volume24"] and r["volume24"] > 10_000_000): return False, 0, flags
    if not (r["price_chg24"] is not None and r["price_chg24"] < 0 and r["oi_chg24_pct"] is not None and r["oi_chg24_pct"] < 0): return False, 0, flags
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
    if oi is None or pc is None: return "Neutral", []
    regime = "Neutral"
    if oi < -5: regime = "Capitulation" if pc < -3 else "Distribution"
    elif -5 <= oi <= 5: regime = "Weak Trend" if (pc > 2 and cvd is not None and cvd < 50) else "Neutral"
    elif 5 < oi <= 15:
        if cvd is not None and cvd > 70 and lls is not None and lls < 25 and 2 <= pc <= 10: regime = "Short Squeeze Setup"
        elif cvd is not None and cvd > 50 and lls is not None and lls < 35 and 2 <= pc <= 15: regime = "Healthy Trend"
        else: regime = "Mixed"
    elif 15 < oi <= 35: regime = "Exhaustion" if (pc > 15 and cvd is not None and cvd > 70) else "Exhaustion (умеренная)"
    else: regime = "Extreme Exhaustion"
    tags = []
    fr = r["fr_oiw"]
    if regime in ("Healthy Trend", "Short Squeeze Setup") and fr is not None and 0 <= fr <= 0.015: tags.append("Stealth Accumulation")
    if regime in ("Healthy Trend", "Short Squeeze Setup", "Mixed") and fr is not None and fr < 0: tags.append("Funding-дивергенция")
    if regime in ("Exhaustion", "Extreme Exhaustion") and fr is not None and fr > 0.05: tags.append("Euphoria")
    if regime == "Capitulation" and cvd is not None and cvd > 50: tags.append("Скрытое накопление на дне")
    ls = r["ls_accounts"]
    if ls is not None and ls > 1.5 and regime in ("Healthy Trend", "Short Squeeze Setup", "Weak Trend"): tags.append("Ритейл FOMO")
    return regime, tags

# ============ ЛОГ И ИСТОРИЯ ============

def load_history():
    if not os.path.exists(LOG_FILE): return []
    with open(LOG_FILE, "r", encoding="utf-8") as f: return [json.loads(line) for line in f if line.strip()]

def append_history(rec):
    with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def load_llm_state():
    if not os.path.exists(LLM_STATE_FILE): return {}
    with open(LLM_STATE_FILE, "r", encoding="utf-8") as f: return json.load(f)

def save_llm_state(state):
    with open(LLM_STATE_FILE, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=2)

def recent_snapshots(history, symbol, window_minutes):
    cutoff = int(time.time()) - window_minutes * 60
    return sorted([h for h in history if h["symbol"] == symbol and h["ts"] > cutoff], key=lambda h: h["ts"])

# ============ ФОРМАТИРОВАНИЕ ДЛЯ LLM ============

def build_snapshot_log_table(snaps):
    lines = ["| № | Время (UTC) | Режим | Балл | OI24h% | OI4h% | CVD24 | LLS24 | FR_OIW% | Price% |", "|---|---|---|---|---|---|---|---|---|---|"]
    for i, s in enumerate(snaps, 1):
        t = time.strftime("%H:%M:%S", time.gmtime(s["ts"]))
        lines.append(f"| {i} | {t} | {s.get('regime')} | {s.get('score')} | {s.get('oi_chg24_pct')} | {s.get('oi_chg4h_pct')} | {s.get('cvd24')} | {s.get('lls24')} | {s.get('fr_oiw')} | {s.get('price_chg24')} |")
    return "\n".join(lines)

def format_full_metrics(r):
    return (
        f"Coin: {r['name']} ({r['symbol']})\nPrice: {r['price']} | Price Change % 24H: {r['price_chg24']}%\n"
        f"Market Capitalisation: {r['mktcap']}\nVolume 24H: {r['volume24']}\n"
        f"Open Interest: {r['oi']} | OI Change % 24H: {r['oi_chg24_pct']}% | OI Change % 4H: {r['oi_chg4h_pct']}%\n"
        f"Open Interest / Volume 24H: {r['oi_vol_ratio']}\nOpen Interest / Market Capitalization: {r['oi_mktcap_ratio']}\n"
        f"Funding Rate Average: {r['fr_avg']}% | Predicted: {r['pfr_avg']}%\n"
        f"Funding Rate Average, OI Weighted: {r['fr_oiw']}% | Predicted OI-W: {r['pfr_oiw']}%\n"
        f"Short Liquidations 24H: {r['liq_short24']}\nLong Liquidations 24H: {r['liq_long24']}\n"
        f"Long/Short Accounts Ratio (1D): {r['ls_accounts']}\nBTC Correlation 7D: {r['btc_corr7d']}\n"
        f"CVD24: {r['cvd24']}\nLLS24: {r['lls24']}%\n"
    )

def load_system_prompt():
    with open(PROMPT_FILE, "r", encoding="utf-8") as f: return f.read()

VERDICT_JSON_SCHEMA_HINT = """
Верни ТОЛЬКО валидный JSON (без markdown, без ```, без пояснений вне JSON), строго со следующими ключами:
{
  "signal": "Бычий" | "Медвежий" | "Нейтральный" | "Смешанный",
  "regime": "точное название режима",
  "tag": "название тега или 'нет'",
  "score_comment": "короткое предложение про балл",
  "confidence": "Низкая" | "Средняя" | "Высокая",
  "persistence_snapshots": число,
  "persistence_comment": "короткое предложение",
  "pros": ["метрика ЗА 1", "метрика ЗА 2"],
  "cons": ["метрика ПРОТИВ 1", "метрика ПРОТИВ 2"],
  "pattern": "название паттерна или 'нет'",
  "dynamics": "1-2 предложения о динамике",
  "risks": "1-2 предложения о рисках",
  "heatmap": "строка, обычно 'нет данных'",
  "next_check": "что подождать дальше",
  "verdict": "НЕ ВХОДИТЬ" | "НАБЛЮДАТЬ" | "РАССМАТРИВАТЬ",
  "verdict_reason": "короткое предложение"
}
"""

# ============ QWEN API (ПРЯМОЙ ЗАПРОС ЧЕРЕЗ REQUESTS) ============

class QwenAPIClient:
    def __init__(self):
        self.session = requests.Session()
        
        # Твои куки
        self.session.cookies.set("x-ap", "eu-central-1", domain="chat.qwen.ai")
        self.session.cookies.set("_bl_uid", "L5mtgn9qe3plIbkk3j0Rfatrh0X4", domain="chat.qwen.ai")
        self.session.cookies.set("sca", "0861eb56", domain=".qwen.ai")
        self.session.cookies.set("cna", "6KxSIoDs1ykCASXWRtXdA0XL", domain=".qwen.ai")
        self.session.cookies.set("xlly_s", "1", domain=".qwen.ai")
        self.session.cookies.set("acw_tc", "0a06abd917854894453041498e3a1c5f6b9c38f4b7f5e20958ee98a205138f", domain="chat.qwen.ai")
        self.session.cookies.set("qwen-theme", "light", domain="chat.qwen.ai")
        self.session.cookies.set("qwen-locale", "ru-RU", domain="chat.qwen.ai")
        self.session.cookies.set("atpsida", "bb80167bcc6276730aa84095_1785489588_15", domain=".qwen.ai")
        self.session.cookies.set("tfstk", "ginZLlTnShKwdvdoUYq2T6jjwEZT4oRW0mNbnxD0C5VM6mZ4nWkV1VMjno-4tjFi1R9Ti-qtAVsb1CEqnbZ2NQtWVAHTkoAWNymaGKZT3Rb0isZ3t8E4IBqhL4kTDoAB6qvSvAhZtjsuSovUx-ycIojGSpr3H-EcjrjG-6VLnoq0o5fnK-2lj1V0ipk39-q0ij4MLkVLnoVmiocjKzaA2-UMBM3zxUsqzPPoIWSrwmyM7WK86ijmYRkUZAbAmimUQPoeHEFatPgmeznsCnSLfYu3xRlDqCqnE2k77cRPfSUqYfuq1K_YLqkm2lEPnhDUb5zobPBGbbDEn4aEALx8blVillHfUCMEbfMtYx6c-lrs8zoaq3CgG2Mr0-ovMHlrI4cmugu5MJj19m3NiZzgpJPWLp5f8GBWXWL88ZQY51yUNdvfkZUgpJPWLp7AkPHzL7998", domain=".qwen.ai")
        self.session.cookies.set("isg", "BMjIrwGSNofWcVmkjIBDspwwmTbacSx7Hy_TNoJ8l8M3XW7HJoVwC9gb1S0t7eRT", domain=".qwen.ai")
        self.session.cookies.set("ssxmod_itna", "1-Yqfx0D9Dy70QDtD8Dhx_xmqGj7qWFPiQ_IYDXDULqe7UQGcD8OD0pIgfvjR3p5_A5HYAY4x4Y3PC5D/fiAeDgDW5QDbxAfb00O7y4qeCFEgDPHfjk0PqdaQDruhrF9BRGpjOHrZ7yN_TnbbCHzMW4DHxi8DBFqqBaoDeeDtx0rD0eDPxDYDGbmDneDexDdkKfAkFceOnxDX67vDiPADmRIa4ceDD5DApYDw6_vKDDzKGjxaG0qaCLAo4a5DqD1=Y8PajoD964DsrGyKjgUs5UxI3ScOHMbaSfLDCKDjc2IDmn_DNwvAFkoq7txje1ODNAeeAo=D5HY4tDxIAxdD_NA5KBr=iOYBDNioAEdsjeDDf1Aj1GIZW_YjjbltgaNlEXOmeDRIMlvMGIKBKeDKIOIbnG1nwsee4n2cle4ChdDryiG5a0dW_Gz5bmGYAGzlDBGIveREUP4bixD", domain=".qwen.ai")
        self.session.cookies.set("ssxmod_itna2", "1-Yqfx0D9Dy70QDtD8Dhx_xmqGj7qWFPiQ_IYDXDULqe7UQGcD8OD0pIgfvjR3p5_A5HYAY4x4Y3PjeDA3PxxRxrKD7PbeAbBbpxDBuDa0YUnPFp0Wwi9QaOEkl6ZOcpNuU2SuGkAAzMf=7GsUcDCrAGCNicq=BpkvhGGRWcDDSBD=iapFarOqG7hFogPWarI=noFv_iHa7D7RmgFNWHaUPhxenP_0G3v64de_9PEIxfUjr7qjKhCFrdFaA8v0GcvV8K7qi8Gph7PibK7u23kf4yzFxHwA1h4LukevcZh61n0qA7X=yZD9gSX_5aWGUGaaKrOYczAKI/u0mr2D7ymxFGxymWP8KQBvw8K7K_sKHwSRf05yBvGl5aU7H0pCiOddF/KKw8qzK9FmwfenPnBleWKVK=Zg0UohBOEKAHtqG_iHfRzGq4b8Vb5fzf_GOPI35kEIKKH6_HRWaCpGRnzSwzjbsiLa7pk5gV3pxl_6IvwFdN3HF2I3gKqkKHz7xAMqZPdLKF9OOWwhvBGvHvGcCCSW/qpYwe8wlK9ZScWMHPaOyBaKa5okqyl_KaWhSRWLKfunKHeOAmaCSljbx5YfoekwuOYDcQRkFo4h2qtBNLu=x49mIfbQHYcQhCqyIA9hnzDyuRZian0qoPgoNp40xoNwEtN1c69CgaKYw8PTXll5TgcWqtHAr072WZvSQaHDCDiGwFAo7DBre9HEY=F5qzxmx4Ua4KoW3DYi2zH7EmhNGx013GV7sVGo93PGUGqqD5Ua6Rq03Gu9n3fh8prdapWhT2tQDpGY3_=0tnqViiiDNiimdVBb_eIGxYDD", domain=".qwen.ai")
        
        # Заголовки (включая Accept-Encoding, который requests распакует автоматически)
        self.session.headers.update({
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Cache-Control": "no-cache", "Origin": "https://chat.qwen.ai", "Pragma": "no-cache",
            "Referer": "https://chat.qwen.ai/c/guest", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Timezone": "Fri Jul 31 2026 12:19:48 GMT+0300",
            "Version": "0.2.81", "X-Accel-Buffering": "no", "bx-v": "2.5.37", "source": "web",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"'
        })
        
        # ИСПРАВЛЕНИЕ: Используем конкретный chat_id из твоего рабочего PowerShell-скрипта
        self.chat_id = "5bed1f3b-f915-4e58-87d1-86c8c0c0f808"
        self.url = f"https://chat.qwen.ai/api/v2/chat/completions?chat_id={self.chat_id}"

    def start(self):
        print("QwenAPIClient: сессия requests инициализирована.")

    def stop(self):
        self.session.close()
        print("QwenAPIClient: сессия закрыта.")

    def ask(self, full_prompt, tag="query"):
        # Генерируем новые ID только для конкретного сообщения, но chat_id оставляем фиксированным
        fid = str(uuid.uuid4())
        children_id = str(uuid.uuid4())
        current_ts = int(time.time())
        
        # Payload точно как в твоем рабочем PowerShell скрипте
        payload = {
            "stream": True,
            "version": "2.1",
            "incremental_output": True,
            "chatId": self.chat_id,
            "parentId": "",
            "chat_id": self.chat_id,
            "chat_mode": "guest",
            "model": "qwen3.7-plus",
            "parent_id": None,
            "messages": [{
                "id": None,
                "fid": fid,
                "parentId": None,
                "childrenIds": [children_id],
                "role": "user",
                "content": full_prompt,
                "user_action": "chat",
                "files": [],
                "timestamp": current_ts,
                "models": ["qwen3.7-plus"],
                "model": "",
                "chat_type": "t2t",
                "feature_config": {
                    "thinking_enabled": True,
                    "output_schema": "phase",
                    "research_mode": "normal",
                    "auto_thinking": True,
                    "thinking_mode": "Auto",
                    "thinking_format": "summary",
                    "auto_search": True
                },
                "extra": {
                    "meta": {
                        "subChatType": "t2t"
                    }
                },
                "sub_chat_type": "t2t",
                "parent_id": None
            }],
            "timestamp": current_ts
        }
        
        headers = {
            "Content-Type": "application/json",
            "X-Request-Id": str(uuid.uuid4()),
            "bx-ua": "231!5k/3K4mU+Uz+jm84K1UYZk8FEl2ZWfPwIPF92lBLek2KxVW/XJ2EwruCiDOX5b/I+qgf53/a7+p5Du93W+Q285toB9wbOgRQ4wYEFTV5DnVyEay11Qpk036jS7iTS5zUMSx2nDgB49mJtUTvY/F2cIT7vh49C03zGRPF3xHu2f4rH7lo9mgSDjD+uw8e+Zd++6WF1cCXuggpHJBh+++j+ygU3+jOKs0IRCiTFkk3+ROkk0mkkM1V6AO6B2qI6onY80ojLtD4RxJqpm2tFtWivVUkPo/J/t1cuHLm1d8JoniRtWMaV/4qKrQ1iwVg7/PDFVIbG9UQXRcznRq37HkLYTurOe0RDne2R+eSHQipd+D0Ts6BXp/2V0knfi1JHFeAOVM0Kg+IEGsFUgl2Q4keQw9TkvmbD5f/KeR8KIN3BuvPDDtEoHaIPoTnICb2FGvq54ZyL1t3COmIJJ54lC6wG4ZhIHr6/5+2EfJIuJ7T3MeZp7LwgViJrdQlsbLXA41aCEFUMVbrANyQD4OgOq7bTL95DqWv8jALQM73bruZ7hT0ZN/2IuPcgiHUADPirY50JsvwNeMf8eVInIzkqrRjNpXC9bfR5McTqPAnpeGT0VGT5CvFie/1EoBTs/OITbk6vhPQPcttlCHcjTrq7I21oqZUZft+T1+Zgcdr1YpW4kzGvnPo9bGx6EytZwUoLWoSnPKTYid5oNg7BehMoND/dXGnKuTiDJWaANWboqlQj4BrvChTX/x1HOBbKkYsfxPNXrh5Q5oaqeCYeJEyQvRI/ANFHsTTH40EBgPGQHYr4tbxRTK5lgx5WmIkOdg7/vt1tjPLW//XXQ7DphX7db/g2tYFsTTzrSiUgsA7DxwnV821Ii5fS07iTv5TChWhSXwcRFcYjXVwK8tZBsDrCxEeH01CHXFfDq3CsIsP2nzXtBclsU1ppmsbYBz/XBQZV0kIjHJhSZFgrBrrP1Lropf9NrHWagbzeWCFR/5PEDwhKfRYlLesXsFdB3PYrejFkFZfGKYUmzRMBJ5ax2IJClhVP7RNvB+dx4alaL6EQ4k/8coD1wffz2uWn/fOtrUmC0Em9dOSk+Vyld/P6BUcx9IRTx6bpvfsHmf39mPr5h4wYi/Y4MzVRX+TGXoV8RRQuuLhlzxfgJkHOshSpaJeqRkxArkt3cuzZJLsvIFKLbY2s5SE2qNxe6YIKYPhKQFgjiYerWAh8MxNQjEhwDo06fVNftkYiB5Z+rWO8y90wqFk9I4IllnhNzzLuxqClaw4cYo1dHNJi/WA/sc3twj5y8HmMM46pzfsOvvGYIB7TImesp12k5vNug53Eq8VUJd9eAKhO7gDrLLvA8KI1dqE3Ce40igWHD+vTFPfFOdE7uZTBL7Za5ZT7E/CwhZ3H2H5dAcHhl61w7LKlrJg66x8NhD6DBv/xlXG/rf0fX+9jqKPmBVKlJHsKmlRuL0fDGbkPACb0RFq3wVba1fOdpPrKSlbJokgftLk6GFf3aRLzEkN2wZ7xGJ8XiQAihgISsHVswVF0XijYWS=",
            "bx-umidtoken": "T2gA0YplAt4OSXWtLJ5t9X4uGRCWxJeFTuKIQolJakatLEF9mxE_pvyaVFmfyl-Xd4E="
        }

        debug_file = f"debug_qwen_{tag}_{current_ts}.log"

        try:
            response = self.session.post(self.url, json=payload, headers=headers, stream=True, timeout=QWEN_RESPONSE_TIMEOUT_S)
            
            print(f"[{tag}] HTTP Status: {response.status_code}")
            if response.status_code != 200:
                print(f"[{tag}] Ошибка сервера. Тело ответа: {response.text[:500]}")
                return None
                
            response.raise_for_status()
            
            full_text = ""
            raw_lines = []
            
            with open(debug_file, "w", encoding="utf-8") as f:
                for line in response.iter_lines(decode_unicode=True):
                    if not line: 
                        continue
                    
                    f.write(line + "\n")
                    raw_lines.append(line)
                    
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str == "[DONE]": 
                            break
                        try:
                            chunk = json.loads(data_str)
                            if "choices" in chunk and len(chunk["choices"]) > 0:
                                delta = chunk["choices"][0].get("delta", {})
                                phase = delta.get("phase", "no_phase")
                                content = delta.get("content", "")
                                
                                if content:
                                    print(f"  [{tag}] Phase: {phase} | Content: {content[:40]}...")
                                
                                # Собираем ТОЛЬКО фазу answer
                                if phase == "answer" and content:
                                    full_text += content
                                    
                        except json.JSONDecodeError:
                            pass
                            
            if not full_text:
                print(f"[{tag}] Итоговый текст пуст. Сырой ответ сохранен в {debug_file}")
                print(f"[{tag}] Первые 3 строки сырого ответа:")
                for i, chunk in enumerate(raw_lines[:3]):
                    print(f"  -> {chunk[:250]}")
                    
            return full_text
            
        except Exception as e:
            print(f"[{tag}] Критическая ошибка API requests: {e}")
            return None

def extract_json_from_text(text):
    if not text: return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start: return None
    json_str = cleaned[start:end + 1]
    try: return json.loads(json_str)
    except json.JSONDecodeError: return None

def call_llm_json(qwen_client, system_prompt, user_message, tag, max_retries=1):
    full_prompt = (system_prompt.strip() + "\n\n=== ДАННЫЕ ДЛЯ АНАЛИЗА ===\n" + 
                   user_message.strip() + "\n\n" + VERDICT_JSON_SCHEMA_HINT)
    for attempt in range(max_retries + 1):
        raw_text = qwen_client.ask(full_prompt, tag=tag)
        if raw_text is None:
            print(f"  [{tag}] попытка {attempt+1}: пустой ответ.")
            continue
        parsed = extract_json_from_text(raw_text)
        if parsed is not None: return parsed
        print(f"  [{tag}] попытка {attempt+1}: не удалось извлечь JSON. Начало: {raw_text[:200]}")
    return None

# ============ TELEGRAM ============

def esc(value): return html.escape(str(value), quote=False)
def safe_get(d, key, default="н/д"):
    val = d.get(key, default)
    return default if val is None or val == "" else val

def render_verdict_message(rec, v, snaps_count):
    r = rec
    pros = "\n".join(f"  • {esc(p)}" for p in (v.get("pros") or [])) or "  • нет"
    cons = "\n".join(f"  • {esc(c)}" for c in (v.get("cons") or [])) or "  • нет"
    verdict = safe_get(v, "verdict", "НАБЛЮДАТЬ")
    verdict_emoji = {"НЕ ВХОДИТЬ": "🔴", "НАБЛЮДАТЬ": "🟡", "РАССМАТРИВАТЬ": "🟢"}.get(verdict, "⚪")
    pers_n = v.get("persistence_snapshots", snaps_count)
    return (
        f"{verdict_emoji} <b>{esc(r['name'])} ({esc(r['symbol'])})</b> — <b>{esc(verdict)}</b>\n"
        f"Профиль: {esc(rec['profile'])} | Балл: {rec['score']} | Снимков: {snaps_count}\n"
        f"—————————————\n"
        f"<b>1. Сигнал:</b> {esc(safe_get(v, 'signal'))}\n"
        f"<b>2. Режим:</b> {esc(safe_get(v, 'regime', rec['regime']))}\n"
        f"<b>3. Тег:</b> {esc(safe_get(v, 'tag'))}\n"
        f"<b>4. Балл:</b> {rec['score']} — {esc(safe_get(v, 'score_comment'))}\n"
        f"<b>5. Уверенность:</b> {esc(safe_get(v, 'confidence'))} ({pers_n} снимков) — {esc(safe_get(v, 'persistence_comment'))}\n"
        f"<b>6. Метрики ЗА:</b>\n{pros}\n"
        f"<b>7. Метрики ПРОТИВ:</b>\n{cons}\n"
        f"<b>8. Паттерн:</b> {esc(safe_get(v, 'pattern'))}\n"
        f"<b>9. Динамика:</b> {esc(safe_get(v, 'dynamics'))}\n"
        f"<b>10. Риски:</b> {esc(safe_get(v, 'risks'))}\n"
        f"<b>11. Тепловая карта:</b> {esc(safe_get(v, 'heatmap', 'нет данных'))}\n"
        f"<b>12. Усилить/Опровергнуть:</b> {esc(safe_get(v, 'next_check'))}\n"
        f"—————————————\n"
        f"{verdict_emoji} <b>ВЕРДИКТ: {esc(verdict)}</b>\n"
        f"Причина: {esc(safe_get(v, 'verdict_reason'))}\n\n"
        f"<i>Анализ информационный, не финансовая рекомендация.</i>"
    )

def send_telegram_long(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Telegram не настроен:", text[:200])
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    chunks, remaining = [], text
    while len(remaining) > 3500:
        split_at = remaining.rfind("\n", 0, 3500)
        if split_at == -1: split_at = 3500
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    chunks.append(remaining)
    ok_all = True
    for chunk in chunks:
        try:
            resp = requests.post(url, data={"chat_id": TG_CHAT_ID, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=15)
            if resp.status_code != 200:
                requests.post(url, data={"chat_id": TG_CHAT_ID, "text": chunk, "disable_web_page_preview": True}, timeout=15)
            time.sleep(0.5)
        except Exception as e:
            print(f"Исключение Telegram: {e}")
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
        if passed_a and score_a >= score_b: profile, score, flags = "A", score_a, flags_a
        elif passed_b: profile, score, flags = "B", score_b, flags_b
        if profile is None: continue
        regime, tags = classify_regime(r)
        all_tags = tags + flags
        rec = {**r, "profile": profile, "score": score, "regime": regime, "tags": all_tags}
        append_history(rec)
        history.append(rec)
        print(f"[{r['symbol']}] профиль={profile} балл={score} режим={regime}")
        if score >= MIN_SCORE_TO_WATCH: candidates.append(rec)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    llm_calls_used = 0
    qwen_client = None

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
            if prev.get("last_bucket") == bucket and elapsed_min < REANALYSIS_COOLDOWN_MINUTES:
                cooldown_ok = False
        if not cooldown_ok:
            print(f"  [{symbol}] кулдаун, пропускаю")
            continue
        if llm_calls_used >= MAX_LLM_CALLS_PER_RUN:
            print(f"  [{symbol}] лимит LLM-вызовов")
            continue
        if qwen_client is None:
            qwen_client = QwenAPIClient()
            qwen_client.start()
        print(f"  [{symbol}] отправляю на LLM ({len(snaps)} снимков)")
        log_table = build_snapshot_log_table(snaps)
        full_metrics = format_full_metrics(rec)
        user_message = (
            "Проанализируй устойчивость и качество тренда по логу, определи паттерн и вынеси вердикт.\n\n"
            f"[Лог снимков — {symbol}]\n{log_table}\n\n"
            f"[Полные метрики последнего снимка]\n{full_metrics}\n\n"
            f"Скоринг-балл (уже посчитан кодом): {rec['score']}\n"
            f"Режим (уже посчитан кодом): {rec['regime']}\n"
        )
        verdict_json = call_llm_json(qwen_client, system_prompt, user_message, tag=symbol)
        llm_calls_used += 1
        if verdict_json is None:
            print(f"  [{symbol}] LLM не вернул JSON")
        else:
            msg = render_verdict_message(rec, verdict_json, len(snaps))
            send_telegram_long(msg)
            llm_state[symbol] = {"last_analysis_ts": now_ts, "last_bucket": bucket}
        if llm_calls_used < MAX_LLM_CALLS_PER_RUN:
            time.sleep(SLEEP_BETWEEN_LLM_CALLS)

    if qwen_client is not None: qwen_client.stop()
    save_llm_state(llm_state)
    print(f"Готово. LLM-вызовов: {llm_calls_used}")

if __name__ == "__main__":
    run_once()
