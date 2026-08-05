"""
coinalyze_loader.py — загрузчик/диагностика данных с Coinalyze.

Логика:
 1. Реальные цифры таблицы (price/volume/OI/funding/liquidations и т.д.)
    в DOM не подгружаются отдельным XHR и не идут через WebSocket.
 2. assets-info/ — это лишь справочник монет/бирж/символов (метаданные),
    не содержит текущих цифр.
 3. Инлайн-скрипты главного HTML тоже не содержат данных таблицы (проверено).
 4. Следующий шаг: искать реальный скроллящийся контейнер таблицы (не window/body)
    и проверять, растёт ли tbody при его скролле, а также скачать
    mainTop,coinsPage.js напрямую и поискать в нём API-эндпоинт/логику виртуализации.
"""
from __future__ import annotations

import os
import json
import logging
from pathlib import Path
from typing import Optional, Any

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Browser, Page, Response

try:
    from playwright_stealth import stealth_sync
except ImportError:
    def stealth_sync(page):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("coinalyze_loader")

BASE = Path(__file__).resolve().parent

COINALYZE_P_SID = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"

ASSETS_INFO_MATCH = os.environ.get("ASSETS_INFO_MATCH", "assets-info")
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "40000"))
POST_LOAD_WAIT_MS = int(os.environ.get("POST_LOAD_WAIT_MS", "3000"))

LOG_CHUNK_SIZE = int(os.environ.get("LOG_CHUNK_SIZE", "4000"))
MAX_LOG_CHARS = int(os.environ.get("MAX_LOG_CHARS", "0"))  # 0 = без ограничений

BIG_SCRIPT_THRESHOLD = int(os.environ.get("BIG_SCRIPT_THRESHOLD", "5000"))

KEYWORDS = [
    "volume_24hour", "volume24", "open_interest", "openInterest", "funding_rate",
    "fundingRate", "price_change", "priceChange", "oi_change", "liquidation",
    "market_cap", "marketCap", "mktcap", "oi_vol", "cvd", "long_short",
    "longShort", "btc_corr",
]

COINALYZE_URL = os.environ.get(
    "COINALYZE_URL",
    "https://coinalyze.net/"
    "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
    "&filter=Y19ndF8yMDAwMDAwJmRfZ3RfMTAwMDAwMCZlX2d0XzAmc19ndF8wJmNtNjE2NV9ndF80NSZjbTYxNjRfbHRfNjA"
    "&order_by=volume_24hour&order_dir=desc",
)


# ─────────────────────────── утилиты логирования ───────────────────────────
def log_full_body(label: str, body: str, chunk_size: int = LOG_CHUNK_SIZE, max_chars: int = MAX_LOG_CHARS):
    text = body
    total_len = len(text)
    if max_chars and total_len > max_chars:
        text = text[:max_chars]
        log.warning(
            f"⚠️ [{label}] Тело обрезано для лога до {max_chars} из {total_len} символов "
            f"(поставь MAX_LOG_CHARS=0, чтобы логировать всё)"
        )

    n_chunks = (len(text) + chunk_size - 1) // chunk_size or 1
    log.info(f"📦 [{label}] Начинаю вывод: всего_символов={total_len}, кусков={n_chunks}, chunk_size={chunk_size}")
    for i in range(0, len(text), chunk_size):
        idx = i // chunk_size + 1
        chunk = text[i:i + chunk_size]
        log.info(f"📦 [{label}] chunk {idx}/{n_chunks}:\n{chunk}")
    log.info(f"📦 [{label}] === КОНЕЦ ===")


def try_parse_payload(body: str) -> Optional[Any]:
    body_stripped = body.strip()

    try:
        data = json.loads(body_stripped)
        log.info(f"✅ [PARSE] Прямой json.loads сработал. Тип: {type(data).__name__}")
        return data
    except json.JSONDecodeError as e:
        log.info(f"ℹ️ [PARSE] Прямой json.loads не сработал ({e}). Пробуем вытащить JSON из обёртки...")

    candidates = []
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = body_stripped.find(open_ch)
        end = body_stripped.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            candidates.append(body_stripped[start:end + 1])

    for cand in sorted(candidates, key=len, reverse=True):
        try:
            data = json.loads(cand)
            log.info(f"✅ [PARSE] Извлекли JSON из обёртки. Тип: {type(data).__name__}, длина_подстроки={len(cand)}")
            return data
        except json.JSONDecodeError:
            continue

    log.warning("⚠️ [PARSE] Не удалось распознать JSON ни напрямую, ни через извлечение подстроки.")
    return None


def describe_payload(data: Any, prefix: str = "STRUCT"):
    if isinstance(data, list):
        log.info(f"🔎 [{prefix}] Это list длиной {len(data)}")
        if data:
            first = data[0]
            log.info(f"🔎 [{prefix}] Тип первого элемента: {type(first).__name__}")
            if isinstance(first, dict):
                log.info(f"🔎 [{prefix}] Ключи первого элемента: {list(first.keys())}")
                log.info(f"🔎 [{prefix}] Пример: {json.dumps(first, ensure_ascii=False)[:1500]}")
    elif isinstance(data, dict):
        log.info(f"🔎 [{prefix}] Это dict, ключи верхнего уровня: {list(data.keys())}")
        for k, v in data.items():
            if isinstance(v, list):
                log.info(f"🔎 [{prefix}]   '{k}' -> list длиной {len(v)}")
                if v and isinstance(v[0], dict):
                    log.info(f"🔎 [{prefix}]     пример: {json.dumps(v[0], ensure_ascii=False)[:1000]}")
            elif isinstance(v, dict):
                log.info(f"🔎 [{prefix}]   '{k}' -> dict, ключей={len(v)}, первые_ключи={list(v.keys())[:10]}")
            else:
                log.info(f"🔎 [{prefix}]   '{k}' -> {type(v).__name__} = {str(v)[:200]}")
    else:
        log.info(f"🔎 [{prefix}] Неожиданный тип верхнего уровня: {type(data).__name__}")


# ─────────────────────────── парсер чисел (fallback для HTML-таблицы) ───────────────────────────
def parse_number(text: str) -> Optional[float]:
    if not text:
        return None
    t = text.strip().replace(",", "").replace("$", "").replace("%", "")
    if t in ("", "-", "—", "N/A", "n/a"):
        return None
    mult = 1.0
    last_char = t[-1].upper() if t else ""
    if last_char in ("K", "M", "B"):
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}[last_char]
        t = t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def get_td(tds: list, idx: int) -> Optional[float]:
    if idx < len(tds):
        return parse_number(tds[idx].get_text(strip=True))
    return None


def extract_symbol_and_name(tr, tds, row_idx: int) -> tuple[str, str]:
    symbol = tr.get("data-coin") or tr.get("data-symbol") or tr.get("data-id")
    if not symbol:
        a_tag = tr.find("a", href=True)
        if a_tag:
            href = a_tag["href"].strip("/")
            parts = href.split("/")
            if parts:
                symbol = parts[-1].upper()
    name = "?"
    if len(tds) > 1:
        text_cell = tds[1].get_text(strip=True)
        spans = tds[1].find_all("span")
        if spans:
            name = spans[0].get_text(strip=True)
        elif text_cell:
            name = text_cell.split()[0]
        if not symbol and text_cell:
            symbol = text_cell.replace("\n", " ").split()[0].upper()
    elif len(tds) > 0 and not symbol:
        symbol = tds[0].get_text(strip=True).upper()
    if not symbol or symbol in ("-", "N/A", "?"):
        symbol = f"COIN_{row_idx}"
    if not name or name == "?":
        name = symbol
    return symbol, name


def parse_table_fallback(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    rows = soup.select("tbody tr")
    out = []
    for idx, tr in enumerate(rows, start=1):
        tds = tr.find_all(["td", "th"])
        if len(tds) < 2:
            continue
        symbol, name = extract_symbol_and_name(tr, tds, idx)
        rec = {
            "symbol": symbol,
            "name": name,
            "price": get_td(tds, 2),
            "price_chg24": get_td(tds, 3),
            "mktcap": get_td(tds, 4),
            "volume24": get_td(tds, 5),
            "oi": get_td(tds, 6),
            "oi_chg24_pct": get_td(tds, 7),
        }
        out.append(rec)
    log.info(f"✅ [FALLBACK-PARSER] Распарсено строк: {len(out)}/{len(rows)}")
    return out


# ─────────────────────────── скрапер ───────────────────────────
class CoinalyzeScraper:
    def __init__(self, headless: bool = HEADLESS):
        self.headless = headless
        self._pw = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._captured_responses: list[dict] = []
        self._doc_html: dict = {}

    def __enter__(self) -> "CoinalyzeScraper":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )
        ctx = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        ctx.add_init_script("""() => {
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        }""")
        self._add_cookies(ctx)
        self._page = ctx.new_page()
        stealth_sync(self._page)

        self._page.on("response", self._on_response)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    @staticmethod
    def _add_cookies(ctx):
        log.info(f"🔑 [COOKIES] COINALYZE_P_SID: {'УСТАНОВЛЕН ✅' if COINALYZE_P_SID else 'ПУСТО ⚠️'}")
        log.info(f"🔑 [COOKIES] COINALYZE_CHAT_SID: {'УСТАНОВЛЕН ✅' if COINALYZE_CHAT_SID else 'ПУСТО ⚠️'}")
        if not (COINALYZE_P_SID or COINALYZE_CHAT_SID):
            log.warning("⚠️ Куки не заданы!")
            return
        cookies = [
            {"name": "cookies_accepted", "value": "1", "domain": "coinalyze.net", "path": "/", "secure": True}
        ]
        if COINALYZE_P_SID:
            cookies.append({"name": "p_sid", "value": COINALYZE_P_SID, "domain": "coinalyze.net", "path": "/", "secure": True})
        if COINALYZE_CHAT_SID:
            cookies.append({"name": "chat_sid", "value": COINALYZE_CHAT_SID, "domain": "coinalyze.net", "path": "/", "secure": True})
        ctx.add_cookies(cookies)

    def _on_response(self, response: Response):
        try:
            rtype = response.request.resource_type
        except Exception:
            rtype = "?"

        if rtype == "document" and response.status == 200:
            try:
                self._doc_html["html"] = response.text()
                self._doc_html["url"] = response.url
                log.info(f"📄 [DOC-CAPTURE] Поймали главный HTML документ: {response.url}, "
                         f"длина={len(self._doc_html['html'])} символов")
            except Exception as e:
                log.warning(f"⚠️ Не удалось прочитать document-ответ: {e}")
            return

        if rtype in ("xhr", "fetch"):
            log.info(f"🌐 [XHR/FETCH] status={response.status} url={response.url}")

        if ASSETS_INFO_MATCH in response.url:
            try:
                body = response.text()
            except Exception as e:
                log.warning(f"⚠️ [CAPTURE] Не удалось прочитать тело ответа {response.url}: {e}")
                return
            log.info("==================================================")
            log.info(f"🎯 [CAPTURE] Поймали assets-info ответ! URL={response.url}, "
                     f"status={response.status}, длина={len(body)}")
            log.info("==================================================")
            self._captured_responses.append({"url": response.url, "status": response.status, "body": body})

    def _wait_cloudflare(self):
        page = self._page
        try:
            content = page.content()
        except Exception:
            content = ""
        if any(m in content for m in ("Attention Required", "Just a moment", "cf-browser-verification")):
            log.warning("⚠️ Обнаружен Cloudflare челлендж, ждём подольше...")
            page.wait_for_timeout(10_000)

    # ───────────────────── диагностика №1: assets-info + инлайн-скрипты ─────────────────────
    def load_and_diagnose(self, url: str = COINALYZE_URL):
        page = self._page
        self._captured_responses.clear()
        self._doc_html.clear()

        log.info(f"🌐 [PLAYWRIGHT] Открываем URL: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        self._wait_cloudflare()
        page.wait_for_timeout(POST_LOAD_WAIT_MS)

        if self._captured_responses:
            for i, item in enumerate(self._captured_responses, start=1):
                label = f"ASSETS-INFO #{i}"
                log.info(f"📦 [{label}] URL={item['url']} status={item['status']} длина={len(item['body'])}")
                data = try_parse_payload(item["body"])
                if data is not None:
                    describe_payload(data, prefix=label)
        else:
            log.warning("⚠️ assets-info ни разу не был пойман.")

        html = self._doc_html.get("html")
        if not html:
            log.warning("⚠️ Главный document-ответ не пойман через listener, беру page.content() напрямую.")
            html = page.content()

        log.info(f"📄 [DOC] Итоговая длина HTML для анализа: {len(html)} символов")

        soup = BeautifulSoup(html, "html.parser")
        scripts = soup.find_all("script")
        inline_scripts = [s for s in scripts if not s.get("src") and s.string]
        log.info(f"📄 [DOC] Всего <script> тегов: {len(scripts)}, инлайн (без src): {len(inline_scripts)}")

        any_keyword_hit = False
        for i, sc in enumerate(inline_scripts, start=1):
            content = sc.string or ""
            length = len(content)
            hits = [kw for kw in KEYWORDS if kw in content]
            is_big = length >= BIG_SCRIPT_THRESHOLD

            log.info(f"📄 [SCRIPT #{i}] длина={length} символов, keywords_hits={hits}, "
                     f"крупный(>{BIG_SCRIPT_THRESHOLD})={is_big}")

            if hits:
                any_keyword_hit = True

            if hits or is_big:
                log.info(f"🎯 [SCRIPT #{i}] Логируем содержимое целиком (совпадение={bool(hits)}, крупный={is_big}):")
                log_full_body(f"INLINE-SCRIPT #{i}", content)

                data = try_parse_payload(content)
                if data is not None:
                    describe_payload(data, prefix=f"SCRIPT #{i} STRUCT")

        if not any_keyword_hit:
            log.warning(
                "⚠️ [DOC] Ни одно ключевое слово не найдено ни в одном инлайн-скрипте."
            )

        table_rows = parse_table_fallback(html)
        if table_rows:
            log.info(f"📊 [FALLBACK] Строк найдено в DOM сразу при загрузке: {len(table_rows)}")
            log.info(f"📊 [FALLBACK] Пример первой строки: {table_rows[0]}")
        else:
            log.info("📊 [FALLBACK] В DOM нет заполненной <tbody> с данными.")

    # ───────────────────── диагностика №2: скролл-контейнер + coinsPage.js ─────────────────────
    def find_scroll_container_and_test(self, url: str = COINALYZE_URL):
        """Ищем реальный скроллящийся контейнер таблицы и проверяем, растёт ли tbody при его скролле.
        Также скачиваем mainTop,coinsPage.js напрямую и логируем поиск API-эндпоинтов внутри него."""
        page = self._page

        log.info(f"🌐 [PLAYWRIGHT] Открываем URL: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        self._wait_cloudflare()
        page.wait_for_timeout(POST_LOAD_WAIT_MS)

        initial_count = len(page.query_selector_all("tbody tr"))
        log.info(f"📊 [SCROLL-TEST] Строк в tbody сразу после загрузки: {initial_count}")

        candidates = page.evaluate("""
            () => {
                const results = [];
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    const style = window.getComputedStyle(el);
                    const canScroll = (style.overflowY === 'auto' || style.overflowY === 'scroll');
                    if (canScroll && el.scrollHeight > el.clientHeight + 10) {
                        let selector = el.tagName.toLowerCase();
                        if (el.id) selector += '#' + el.id;
                        if (el.className) selector += '.' + String(el.className).trim().replace(/\\s+/g, '.');
                        results.push({
                            selector: selector,
                            scrollHeight: el.scrollHeight,
                            clientHeight: el.clientHeight,
                            scrollTop: el.scrollTop,
                            containsTable: el.querySelector('table') !== null
                        });
                    }
                }
                return results;
            }
        """)

        log.info(f"🔎 [SCROLL-TEST] Найдено потенциальных скроллящихся контейнеров: {len(candidates)}")
        for i, c in enumerate(candidates, start=1):
            log.info(
                f"🔎 [SCROLL-TEST] #{i}: selector={c['selector']!r}, "
                f"scrollHeight={c['scrollHeight']}, clientHeight={c['clientHeight']}, "
                f"containsTable={c['containsTable']}"
            )

        table_containers = [c for c in candidates if c["containsTable"]]
        if not table_containers:
            log.warning(
                "⚠️ [SCROLL-TEST] Ни один найденный скроллящийся контейнер не содержит <table> внутри себя. "
                "Возможно таблица виртуализирована без обычного overflow-контейнера (canvas/custom render)."
            )
        else:
            for c in table_containers:
                sel = c["selector"]
                log.info(f"📜 [SCROLL-TEST] Скроллим контейнер: {sel}")
                try:
                    page.evaluate(
                        """(sel) => {
                            const el = document.querySelector(sel);
                            if (el) {
                                for (let i = 0; i < 15; i++) {
                                    el.scrollTop += 2000;
                                }
                            }
                        }""",
                        sel,
                    )
                except Exception as e:
                    log.warning(f"⚠️ [SCROLL-TEST] Не удалось скроллить {sel}: {e}")
                    continue
                page.wait_for_timeout(1500)
                new_count = len(page.query_selector_all("tbody tr"))
                log.info(f"📊 [SCROLL-TEST] После скролла {sel}: строк в tbody = {new_count} "
                         f"(было {initial_count})")

        log.info("🌐 [JS-FETCH] Ищем src внешних <script> на странице...")
        script_srcs = page.eval_on_selector_all(
            "script[src]",
            "els => els.map(e => e.src)"
        )
        log.info(f"🌐 [JS-FETCH] Всего найдено <script src>: {len(script_srcs)}")

        coins_page_js_url = None
        for src in script_srcs:
            if "coinsPage" in src or "mainTop" in src:
                coins_page_js_url = src
                break

        if not coins_page_js_url:
            log.warning("⚠️ [JS-FETCH] Не нашли src, содержащий coinsPage/mainTop, вот все найденные скрипты:")
            for src in script_srcs:
                log.info(f"   - {src}")
            return

        log.info(f"🌐 [JS-FETCH] Скачиваем: {coins_page_js_url}")
        try:
            resp = page.request.get(coins_page_js_url, timeout=20_000)
            js_body = resp.text()
            log.info(f"🌐 [JS-FETCH] Статус={resp.status}, длина={len(js_body)} символов")

            api_keywords = ["/api/", "fetch(", "XMLHttpRequest", ".ajax(", "endpoint",
                             "coins-listing", "coins_listing", "websocket", "WebSocket",
                             "setInterval", "polling"]
            for kw in api_keywords:
                idx = js_body.find(kw)
                if idx != -1:
                    snippet = js_body[max(0, idx - 150): idx + 350]
                    log.info(f"🎯 [JS-FETCH] Найдено '{kw}' на позиции {idx}, контекст:\n{snippet}")
                else:
                    log.info(f"ℹ️ [JS-FETCH] '{kw}' не найдено в файле.")
        except Exception as e:
            log.warning(f"⚠️ [JS-FETCH] Не удалось скачать/разобрать {coins_page_js_url}: {e}")


def main():
    log.info(f"🚀 Запуск диагностики в {'HEADLESS' if HEADLESS else 'GUI'} режиме")
    with CoinalyzeScraper() as scraper:
        scraper.find_scroll_container_and_test()

    log.info("🏁 Диагностика завершена.")


if __name__ == "__main__":
    main()
