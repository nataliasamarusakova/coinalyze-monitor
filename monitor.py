"""
coinalyze_monitor_v2.py

Playwright -> Coinalyze parser ->
Lifecycle Engine ->
Snapshot history ->
Candidate ranking ->
Local LLM analysis ->
Telegram alerts

Версия v2:
- жизненный цикл монеты
- автоматическое снятие с наблюдения
- защита от зависания LLM
- ускоренный инференс
- сохранение состояния
"""

import os
import re
import sys
import time
import json
import html
import multiprocessing
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


try:
    from playwright_stealth import stealth_sync
except ImportError:

    def stealth_sync(page):
        pass


# ============================================================
# ENV
# ============================================================

COINALYZE_P_SID = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")


# ============================================================
# LLM
# ============================================================

LLM_MODEL_PATH = os.environ.get("LLM_MODEL_PATH", "models/Qwen3.5-4B-Q4_K_M.gguf")

LLM_N_CTX = int(os.environ.get("LLM_N_CTX", "4096"))

LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "180"))


# ============================================================
# FILES
# ============================================================

SNAPSHOT_FILE = "snapshots.jsonl"
HEARTBEAT_FILE = "heartbeat.jsonl"

STATE_FILE = "lifecycle_state.json"
LLM_STATE_FILE = "llm_state.json"

PROMPT_FILE = "analyst_prompt_condensed.md"


# ============================================================
# ENGINE SETTINGS
# ============================================================

MIN_SCORE = 3

MIN_SNAPSHOTS = 3

ANALYSIS_WINDOW_MIN = 30

LLM_COOLDOWN_MIN = 45

MAX_LLM_CALLS = 2


# сколько дней хранить историю

SNAPSHOT_RETENTION = 14
HEARTBEAT_RETENTION = 14


# ============================================================
# LIFECYCLE
# ============================================================

LIFECYCLE = {
    "NEW": 0,
    "ACCUMULATION": 1,
    "TREND": 2,
    "ACCELERATION": 3,
    "EXHAUSTION": 4,
    "DISTRIBUTION": 5,
    "INVALID": 6,
    "EXIT": 7,
}


# ============================================================
# REGIME COLORS
# ============================================================

REGIME_BUCKET = {
    "Healthy Trend": "bullish",
    "Short Squeeze Setup": "bullish",
    "Accumulation": "bullish",
    "Exhaustion": "warning",
    "Distribution": "warning",
    "Invalid": "danger",
    "Neutral": "neutral",
}


def bucket_of(regime):

    return REGIME_BUCKET.get(regime, "neutral")


# ============================================================
# ENV CHECK
# ============================================================


def check_env():

    required = ["COINALYZE_P_SID", "COINALYZE_CHAT_SID", "TG_BOT_TOKEN", "TG_CHAT_ID"]

    missing = [x for x in required if not os.environ.get(x)]

    if missing:

        print("Нет переменных:", missing)

        sys.exit(1)

    if not os.path.exists(PROMPT_FILE):

        print("Нет промпта:", PROMPT_FILE)

        sys.exit(1)


# ============================================================
# NUM PARSER
# ============================================================


def parse_number(value):

    if value is None:

        return None

    s = str(value)

    s = s.replace("$", "").replace("%", "").replace(",", "").replace("+", "").strip()

    if s in ("", "-", "—", "n/a"):

        return None

    mult = 1

    if s[-1:].lower() in ("k", "m", "b", "t"):

        mult = {"k": 1000, "m": 1000000, "b": 1000000000, "t": 1000000000000}[
            s[-1].lower()
        ]

        s = s[:-1]

    try:

        return float(s) * mult

    except:

        return None


# ============================================================
# COINALYZE PARSER
# ============================================================


URL = (
    "https://coinalyze.net/"
    "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
    "&filter=ZV9ndF8yLjUmYl9ndF8xJmNfZ3RfMTAwMDAwMCZjbTYxNjVfZ3RfMzUmY202MTY0X2x0XzQ1"
    "&order_by=oi_current"
    "&order_dir=desc"
)


def fetch_rows_from_html(html_text):

    soup = BeautifulSoup(html_text, "lxml")

    rows = soup.select("tbody tr")

    print("Найдено строк:", len(rows))

    result = []

    for tr in rows:

        symbol = tr.get("data-coin")

        if not symbol:
            continue

        tds = tr.find_all("td")

        if len(tds) < 23:

            continue

        name_spans = tds[1].find_all("span")

        name = name_spans[0].get_text(strip=True) if name_spans else symbol

        try:

            rec = {
                "ts": int(time.time()),
                "symbol": symbol,
                "name": name,
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

            result.append(rec)

        except Exception as e:

            print("Ошибка парсинга строки:", e)

    return result


# ============================================================
# PLAYWRIGHT FETCH
# ============================================================


def fetch_rows_via_browser():

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 " "(Windows NT 10.0; Win64; x64) " "Chrome/124 Safari/537"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
        )

        if COINALYZE_P_SID:

            context.add_cookies(
                [
                    {
                        "name": "p_sid",
                        "value": COINALYZE_P_SID,
                        "domain": "coinalyze.net",
                        "path": "/",
                    },
                    {
                        "name": "chat_sid",
                        "value": COINALYZE_CHAT_SID,
                        "domain": "coinalyze.net",
                        "path": "/",
                    },
                    {
                        "name": "cookies_accepted",
                        "value": "1",
                        "domain": "coinalyze.net",
                        "path": "/",
                    },
                ]
            )

        page = context.new_page()

        stealth_sync(page)

        html_content = ""

        try:

            page.goto(URL, wait_until="domcontentloaded", timeout=45000)

            page.wait_for_timeout(3000)

            page.wait_for_selector("tbody tr", timeout=20000)

            html_content = page.content()

        except Exception as e:

            print("Ошибка загрузки Coinalyze:", e)

            try:

                html_content = page.content()

            except:

                pass

        browser.close()

        return html_content


def fetch_rows():

    html_content = fetch_rows_via_browser()

    with open("debug_page.html", "w", encoding="utf-8") as f:

        f.write(html_content)

    rows = fetch_rows_from_html(html_content)

    if not rows:

        send_telegram_long("⚠️ Coinalyze v2: данные не получены")

        sys.exit(1)

    return rows


# ============================================================
# COINALYZE PARSER
# ============================================================


URL = (
    "https://coinalyze.net/"
    "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
    "&filter=ZV9ndF8yLjUmYl9ndF8xJmNfZ3RfMTAwMDAwMCZjbTYxNjVfZ3RfMzUmY202MTY0X2x0XzQ1"
    "&order_by=oi_current"
    "&order_dir=desc"
)


def fetch_rows_from_html(html_text):

    soup = BeautifulSoup(html_text, "lxml")

    rows = soup.select("tbody tr")

    print("Найдено строк:", len(rows))

    result = []

    for tr in rows:

        symbol = tr.get("data-coin")

        if not symbol:
            continue

        tds = tr.find_all("td")

        if len(tds) < 23:

            continue

        name_spans = tds[1].find_all("span")

        name = name_spans[0].get_text(strip=True) if name_spans else symbol

        try:

            rec = {
                "ts": int(time.time()),
                "symbol": symbol,
                "name": name,
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

            result.append(rec)

        except Exception as e:

            print("Ошибка парсинга строки:", e)

    return result


# ============================================================
# PLAYWRIGHT FETCH
# ============================================================


def fetch_rows_via_browser():

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 " "(Windows NT 10.0; Win64; x64) " "Chrome/124 Safari/537"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
        )

        if COINALYZE_P_SID:

            context.add_cookies(
                [
                    {
                        "name": "p_sid",
                        "value": COINALYZE_P_SID,
                        "domain": "coinalyze.net",
                        "path": "/",
                    },
                    {
                        "name": "chat_sid",
                        "value": COINALYZE_CHAT_SID,
                        "domain": "coinalyze.net",
                        "path": "/",
                    },
                    {
                        "name": "cookies_accepted",
                        "value": "1",
                        "domain": "coinalyze.net",
                        "path": "/",
                    },
                ]
            )

        page = context.new_page()

        stealth_sync(page)

        html_content = ""

        try:

            page.goto(URL, wait_until="domcontentloaded", timeout=45000)

            page.wait_for_timeout(3000)

            page.wait_for_selector("tbody tr", timeout=20000)

            html_content = page.content()

        except Exception as e:

            print("Ошибка загрузки Coinalyze:", e)

            try:

                html_content = page.content()

            except:

                pass

        browser.close()

        return html_content


def fetch_rows():

    html_content = fetch_rows_via_browser()

    with open("debug_page.html", "w", encoding="utf-8") as f:

        f.write(html_content)

    rows = fetch_rows_from_html(html_content)

    if not rows:

        send_telegram_long("⚠️ Coinalyze v2: данные не получены")

        sys.exit(1)

    return rows


# ============================================================
# HISTORY STORAGE
# ============================================================


def load_jsonl(path):

    if not os.path.exists(path):

        return []

    result = []

    with open(path, "r", encoding="utf-8") as f:

        for line in f:

            if not line.strip():

                continue

            try:

                result.append(json.loads(line))

            except:

                continue

    return result


def append_jsonl(path, obj):

    with open(path, "a", encoding="utf-8") as f:

        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def save_json(path, data):

    with open(path, "w", encoding="utf-8") as f:

        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(path):

    if not os.path.exists(path):

        return {}

    try:

        with open(path, "r", encoding="utf-8") as f:

            return json.load(f)

    except:

        return {}


# ============================================================
# CLEAN OLD DATA
# ============================================================


def prune_jsonl(path, days):

    if not os.path.exists(path):

        return

    limit = time.time() - days * 86400

    keep = []

    with open(path, "r", encoding="utf-8") as f:

        for line in f:

            try:

                obj = json.loads(line)

                if obj.get("ts", 0) > limit:

                    keep.append(line)

            except:

                pass

    with open(path, "w", encoding="utf-8") as f:

        f.writelines(keep)


# ============================================================
# SNAPSHOT WINDOW
# ============================================================


def get_recent_snapshots(history, symbol, minutes=30):

    cutoff = time.time() - minutes * 60

    result = [
        x for x in history if (x.get("symbol") == symbol and x.get("ts", 0) > cutoff)
    ]

    return sorted(result, key=lambda x: x["ts"])


# ============================================================
# LIFECYCLE STATE
# ============================================================


def update_lifecycle_state(state, rec):

    symbol = rec["symbol"]

    old = state.get(symbol, {})

    lifecycle = rec.get("lifecycle")

    history = old.get("states", [])

    history.append(
        {
            "ts": rec["ts"],
            "state": lifecycle,
            "score": rec["score"],
            "price": rec["price"],
        }
    )

    # держим только последние 30 изменений

    history = history[-30:]

    state[symbol] = {
        "current": lifecycle,
        "score": rec["score"],
        "last_update": rec["ts"],
        "states": history,
    }

    return state


# ============================================================
# STOP WATCH LOGIC
# ============================================================


def should_remove_from_watch(lifecycle_state, symbol):

    data = lifecycle_state.get(symbol)

    if not data:

        return False

    current = data.get("current")

    states = data.get("states", [])

    # =================================================
    # EXIT сразу удаляем
    # =================================================

    if current == "EXIT":

        return True

    # =================================================
    # 3 раза подряд EXHAUSTION
    # =================================================

    if len(states) >= 3:

        last3 = [x["state"] for x in states[-3:]]

        if all(x == "EXHAUSTION" for x in last3):

            return True

    # =================================================
    # нет обновления больше суток
    # =================================================

    if time.time() - data.get("last_update", 0) > 86400:

        return True

    return False


# ============================================================
# SNAPSHOT TABLE FOR LLM
# ============================================================


def build_snapshot_table(snaps):

    lines = ["№ | Время | Stage | Score | Price24 | OI24 | OI4H | CVD | LLS"]

    for i, s in enumerate(snaps, 1):

        tm = datetime.fromtimestamp(s["ts"]).strftime("%H:%M")

        lines.append(
            f"{i} | "
            f"{tm} | "
            f"{s.get('lifecycle')} | "
            f"{s.get('score')} | "
            f"{s.get('price_chg24')} | "
            f"{s.get('oi_chg24_pct')} | "
            f"{s.get('oi_chg4h_pct')} | "
            f"{s.get('cvd24')} | "
            f"{s.get('lls24')}"
        )

    return "\n".join(lines)


# ============================================================
# LLM ENGINE
# ============================================================


_llm = None


def load_prompt():

    with open(PROMPT_FILE, "r", encoding="utf-8") as f:

        return f.read()


def get_llm():

    global _llm

    if _llm is not None:

        return _llm

    from llama_cpp import Llama

    threads = max(2, multiprocessing.cpu_count())

    print("Загрузка LLM:", LLM_MODEL_PATH)

    start = time.time()

    _llm = Llama(
        model_path=LLM_MODEL_PATH,
        n_ctx=LLM_N_CTX,
        n_threads=threads,
        n_batch=512,
        chat_format="chatml",
        temperature=0,
        verbose=False,
        # быстрее для GitHub runner / CPU
        use_mmap=True,
        use_mlock=False,
    )

    print("LLM загружен за", round(time.time() - start, 1), "сек")

    # короткий прогрев

    try:

        _llm.create_chat_completion(
            messages=[{"role": "user", "content": "ping"}], max_tokens=2, temperature=0
        )

    except:

        pass

    return _llm


# ============================================================
# JSON EXTRACTION
# ============================================================


def extract_json(text):

    if not text:

        return None

    start = text.find("{")

    if start < 0:

        return None

    depth = 0

    for i in range(start, len(text)):

        if text[i] == "{":

            depth += 1

        elif text[i] == "}":

            depth -= 1

            if depth == 0:

                block = text[start : i + 1]

                try:

                    return json.loads(block)

                except:

                    return None

    return None


# ============================================================
# LLM CALL
# ============================================================


def ask_llm(system, user):

    llm = get_llm()

    message = f"""
ВАЖНО:

- Не пиши reasoning.
- Не используй <think>.
- Верни только JSON.
- Не пересчитывай score.
- Анализируй только динамику.


{user}


ФОРМАТ:

{{
"signal":"",
"confidence":"",
"pattern":"",
"pros":"",
"cons":"",
"risk":"",
"next_check":"",
"verdict":""
}}

"""

    try:

        start = time.time()

        result = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_tokens=LLM_MAX_TOKENS,
            stop=["<|im_end|>", "</think>", "```"],
        )

        elapsed = time.time() - start

        print("LLM время:", round(elapsed, 1), "сек")

        text = result["choices"][0]["message"]["content"]

        if "<think>" in text:

            text = text.split("</think>")[-1]

        return extract_json(text)

    except Exception as e:

        print("Ошибка LLM:", e)

        return None


# ============================================================
# TELEGRAM
# ============================================================


def escape(v):

    return html.escape(str(v), quote=False)


def send_telegram(text):

    if not TG_BOT_TOKEN:

        print(text)

        return

    url = "https://api.telegram.org/" f"bot{TG_BOT_TOKEN}/sendMessage"

    try:

        requests.post(
            url,
            data={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )

    except Exception as e:

        print("Telegram error", e)


def render_alert(rec, answer):

    return f"""

<b>{escape(rec['name'])} ({rec['symbol']})</b>


Stage:
<b>{rec['lifecycle']}</b>


Score:
{rec['score']}


Signal:
{escape(answer.get('signal'))}


Pattern:
{escape(answer.get('pattern'))}


За:
{escape(answer.get('pros'))}


Против:
{escape(answer.get('cons'))}


Риск:
{escape(answer.get('risk'))}


Следующий контроль:
{escape(answer.get('next_check'))}


<b>ВЕРДИКТ:
{escape(answer.get('verdict'))}</b>


<i>Информационный анализ, не финансовая рекомендация.</i>

"""


# ============================================================
# MAIN ENGINE
# ============================================================


def run_once():

    started = time.time()

    check_env()

    prompt = load_prompt()

    snapshots = load_jsonl(SNAPSHOT_FILE)

    heartbeat = load_jsonl(HEARTBEAT_FILE)

    lifecycle_state = load_json(STATE_FILE)

    llm_state = load_json(LLM_STATE_FILE)

    rows = fetch_rows()

    print("Получено монет:", len(rows))

    candidates = []

    now = int(time.time())

    for r in rows:

        # -------------------------------------------------
        # heartbeat всех монет
        # -------------------------------------------------

        append_jsonl(
            HEARTBEAT_FILE, {"ts": r["ts"], "symbol": r["symbol"], "price": r["price"]}
        )

        heartbeat.append({"ts": r["ts"], "symbol": r["symbol"], "price": r["price"]})

        # -------------------------------------------------
        # lifecycle
        # -------------------------------------------------

        stage, tags = detect_lifecycle(r, snapshots)

        score, flags = lifecycle_score(r)

        ok, score, extra = is_candidate(r, stage)

        rec = {**r, "lifecycle": stage, "score": score, "tags": tags + flags + extra}

        append_jsonl(SNAPSHOT_FILE, rec)

        snapshots.append(rec)

        lifecycle_state = update_lifecycle_state(lifecycle_state, rec)

        print(f"[{r['symbol']}]", stage, "score", score, tags)

        if ok:

            candidates.append(rec)

    # -----------------------------------------------------
    # сортировка качества
    # -----------------------------------------------------

    candidates.sort(key=lambda x: x["score"], reverse=True)

    print("Кандидатов:", len(candidates))

    llm_calls = 0

    # -----------------------------------------------------
    # LLM анализ
    # -----------------------------------------------------

    for rec in candidates:

        if llm_calls >= MAX_LLM_CALLS:

            break

        symbol = rec["symbol"]

        if should_remove_from_watch(lifecycle_state, symbol):

            print(symbol, "removed from watch")

            continue

        recent = get_recent_snapshots(snapshots, symbol, ANALYSIS_WINDOW_MIN)

        if len(recent) < MIN_SNAPSHOTS:

            print(symbol, "мало истории", len(recent))

            continue

        previous = llm_state.get(symbol)

        if previous:

            diff = (now - previous.get("ts", 0)) / 60

            if diff < LLM_COOLDOWN_MIN:

                print(symbol, "LLM cooldown")

                continue

        table = build_snapshot_table(recent)

        user_message = f"""

Монета:
{rec['name']} ({symbol})


Текущий lifecycle:
{rec['lifecycle']}


Score:
{rec['score']}


Tags:
{rec['tags']}



История:

{table}



Задача:

Определи:
- сохраняется ли импульс;
- стадия движения;
- стоит ли продолжать наблюдение;
- главный риск.


"""

        answer = ask_llm(prompt, user_message)

        llm_calls += 1

        if answer:

            msg = render_alert(rec, answer)

            send_telegram(msg)

            llm_state[symbol] = {"ts": now}

    # -----------------------------------------------------
    # SAVE
    # -----------------------------------------------------

    save_json(STATE_FILE, lifecycle_state)

    save_json(LLM_STATE_FILE, llm_state)

    prune_jsonl(SNAPSHOT_FILE, SNAPSHOT_RETENTION)

    prune_jsonl(HEARTBEAT_FILE, HEARTBEAT_RETENTION)

    print("Готово", "LLM:", llm_calls, "время:", round(time.time() - started, 1), "сек")


# ============================================================
# START
# ============================================================


if __name__ == "__main__":

    run_once()
