Понял проблему — вы копируете весь мой ответ целиком, включая текст после блока кода. Уберите последнюю строку с пояснениями из файла — в файле должен быть **только** код между тройными кавычками ``` , и ничего после `if __name__ == "__main__": main()`.

Вот тот же код без единого слова текста после него — просто остановите копирование на последней строке `main()`:
```python
"""
coinalyze_loader.py — загрузчик/диагностика данных с Coinalyze.

Статус диагностики:
 - assets-info/ — справочник монет/бирж (не содержит текущих цифр).
 - WebSocket не используется (0 соединений за 8с ожидания).
 - Реальные данные ЕСТЬ в живом tbody после рендера (цены/объёмы верные),
   но с текущим фильтром там только 42 строки, хотя в браузере видно 90+.
 - Этот шаг: (1) ищем счётчик "показано X из Y" в тексте страницы,
   (2) пробуем настоящие wheel-события именно над <table> (не над body/window),
   (3) ищем признаки виртуализации/лимита (pageSize, limit, slice() и т.п.) в coinsPage.js,
   (4) логируем АБСОЛЮТНО все сетевые ответы за сессию (любой resourceType).
"""
from __future__ import annotations

import os
import re
import json
import logging
from pathlib import Path
from typing import Optional, Any

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Browser, Page, Response, WebSocket

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
POST_LOAD_WAIT_MS = int(os.environ.get("POST_LOAD_WAIT_MS", "4000"))

LOG_CHUNK_SIZE = int(os.environ.get("LOG_CHUNK_SIZE", "4000"))
MAX_LOG_CHARS = int(os.environ.get("MAX_LOG_CHARS", "0"))

COINALYZE_URL = os.environ.get(
    "COINALYZE_URL",
    "https://coinalyze.net/"
    "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
    "&filter=Y19ndF8yMDAwMDAwJmRfZ3RfMTAwMDAwMCZlX2d0XzAmc19ndF8wJmNtNjE2NV9ndF80NSZjbTYxNjRfbHRfNjA"
    "&order_by=volume_24hour&order_dir=desc",
)


def log_full_body(label: str, body: str, chunk_size: int = LOG_CHUNK_SIZE, max_chars: int = MAX_LOG_CHARS):
    text = body
    total_len = len(text)
    if max_chars and total_len > max_chars:
        text = text[:max_chars]
        log.warning(f"⚠️ [{label}] Тело обрезано для лога до {max_chars} из {total_len} символов")

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
        log.info(f"ℹ️ [PARSE] Прямой json.loads не сработал ({e}).")
    return None


def describe_payload(data: Any, prefix: str = "STRUCT"):
    if isinstance(data, list):
        log.info(f"🔎 [{prefix}] list длиной {len(data)}")
        if data and isinstance(data[0], dict):
            log.info(f"🔎 [{prefix}] Ключи первого элемента: {list(data[0].keys())}")
            log.info(f"🔎 [{prefix}] Пример: {json.dumps(data[0], ensure_ascii=False)[:1500]}")
    elif isinstance(data, dict):
        log.info(f"🔎 [{prefix}] dict, ключи: {list(data.keys())}")
    else:
        log.info(f"🔎 [{prefix}] Тип: {type(data).__name__}")


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


class CoinalyzeScraper:
    def __init__(self, headless: bool = HEADLESS):
        self.headless = headless
        self._pw = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._captured_responses: list[dict] = []
        self._doc_html: dict = {}
        self._ws_frames: list[dict] = []
        self._ws_connections: list[str] = []

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
        self._page.on("websocket", self._on_websocket)
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
                log.info(f"📄 [DOC-CAPTURE] Главный HTML: {response.url}, длина={len(self._doc_html['html'])}")
            except Exception as e:
                log.warning(f"⚠️ Не удалось прочитать document-ответ: {e}")
            return

        if rtype in ("xhr", "fetch"):
            log.info(f"🌐 [XHR/FETCH] status={response.status} url={response.url}")

        if ASSETS_INFO_MATCH in response.url:
            try:
                body = response.text()
            except Exception as e:
                log.warning(f"⚠️ [CAPTURE] Не удалось прочитать тело {response.url}: {e}")
                return
            log.info(f"🎯 [CAPTURE] assets-info: status={response.status}, длина={len(body)}")
            self._captured_responses.append({"url": response.url, "status": response.status, "body": body})

    def _on_websocket(self, ws: WebSocket):
        log.info(f"🔌 [WS] Открыт WebSocket: {ws.url}")
        self._ws_connections.append(ws.url)

        def on_framesent(payload):
            text = payload if isinstance(payload, str) else repr(payload)
            log.info(f"📤 [WS SENT] {ws.url}\n{text[:3000]}")
            self._ws_frames.append({"dir": "sent", "url": ws.url, "payload": text})

        def on_framereceived(payload):
            text = payload if isinstance(payload, str) else repr(payload)
            log.info(f"📥 [WS RECV] {ws.url} длина={len(text)}\n{text[:3000]}")
            self._ws_frames.append({"dir": "recv", "url": ws.url, "payload": text})

        ws.on("framesent", on_framesent)
        ws.on("framereceived", on_framereceived)
        ws.on("close", lambda: log.info(f"🔌 [WS] Закрыт: {ws.url}"))

    def _wait_cloudflare(self):
        page = self._page
        try:
            content = page.content()
        except Exception:
            content = ""
        if any(m in content for m in ("Attention Required", "Just a moment", "cf-browser-verification")):
            log.warning("⚠️ Обнаружен Cloudflare челлендж, ждём подольше...")
            page.wait_for_timeout(10_000)

    def diagnose_virtualization(self, url: str = COINALYZE_URL):
        page = self._page
        self._captured_responses.clear()
        self._doc_html.clear()
        self._ws_frames.clear()
        self._ws_connections.clear()

        all_responses = []

        def on_any_response(response: Response):
            try:
                rtype = response.request.resource_type
            except Exception:
                rtype = "?"
            all_responses.append({"url": response.url, "status": response.status, "rtype": rtype})

        page.on("response", on_any_response)

        log.info(f"🌐 [PLAYWRIGHT] Открываем URL: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        self._wait_cloudflare()
        page.wait_for_timeout(POST_LOAD_WAIT_MS)

        initial_count = len(page.query_selector_all("tbody tr"))
        log.info(f"📊 [VIRT-TEST] Строк в tbody сразу после загрузки: {initial_count}")

        try:
            body_text = page.inner_text("body")
        except Exception as e:
            log.warning(f"⚠️ Не удалось получить inner_text body: {e}")
            body_text = ""

        count_patterns = [
            r"(\d+)\s*(?:of|из)\s*(\d+)",
            r"Showing\s+(\d+)",
            r"Total[:\s]+(\d+)",
            r"Results?[:\s]+(\d+)",
        ]
        found_any = False
        for pat in count_patterns:
            matches = re.findall(pat, body_text, re.IGNORECASE)
            if matches:
                found_any = True
                log.info(f"🔎 [VIRT-TEST] Паттерн {pat!r} нашёл: {matches}")
        if not found_any:
            log.info("ℹ️ [VIRT-TEST] Счётчик 'показано X из Y' в тексте страницы не найден.")

        table_box = page.evaluate("""
            () => {
                const table = document.querySelector('table');
                if (!table) return null;
                const rect = table.getBoundingClientRect();
                return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
            }
        """)
        log.info(f"🔎 [VIRT-TEST] Границы <table>: {table_box}")

        if table_box:
            cx = table_box["x"] + table_box["width"] / 2
            cy = table_box["y"] + min(table_box["height"] / 2, 400)
            log.info(f"🖱️ [VIRT-TEST] Двигаем мышь в центр таблицы: ({cx:.0f}, {cy:.0f})")
            page.mouse.move(cx, cy)
            page.wait_for_timeout(300)

            for step in range(1, 11):
                page.mouse.wheel(0, 1500)
                page.wait_for_timeout(800)
                cur_count = len(page.query_selector_all("tbody tr"))
                log.info(f"📊 [VIRT-TEST] После wheel-события #{step} над таблицей: строк = {cur_count}")
                if cur_count > initial_count:
                    log.info(f"✅ [VIRT-TEST] Рост обнаружен! {initial_count} -> {cur_count}")
                    initial_count = cur_count
        else:
            log.warning("⚠️ [VIRT-TEST] Не удалось получить границы <table>, wheel-тест пропущен.")

        final_count = len(page.query_selector_all("tbody tr"))
        log.info(f"📊 [VIRT-TEST] ИТОГО строк в tbody после всех wheel-попыток: {final_count}")

        script_srcs = page.eval_on_selector_all("script[src]", "els => els.map(e => e.src)")
        coins_page_js_url = next((s for s in script_srcs if "coinsPage" in s or "mainTop" in s), None)
        if coins_page_js_url:
            log.info(f"🌐 [VIRT-JS] Скачиваем: {coins_page_js_url}")
            try:
                resp = page.request.get(coins_page_js_url, timeout=20_000)
                js_body = resp.text()
                log.info(f"🌐 [VIRT-JS] Статус={resp.status}, длина={len(js_body)}")

                virt_keywords = ["virtual", "pageSize", "page_size", "limit", "offset",
                                  "rowsPerPage", "rows_per_page", "maxRows", "max_rows",
                                  "renderVisible", "visibleRows", "slice(", "loadMore", "load_more"]
                for kw in virt_keywords:
                    idx = js_body.find(kw)
                    if idx != -1:
                        snippet = js_body[max(0, idx - 150): idx + 350]
                        log.info(f"🎯 [VIRT-JS] Найдено '{kw}' на позиции {idx}:\n{snippet}")
                    else:
                        log.info(f"ℹ️ [VIRT-JS] '{kw}' не найдено.")
            except Exception as e:
                log.warning(f"⚠️ [VIRT-JS] Не удалось скачать/разобрать {coins_page_js_url}: {e}")
        else:
            log.warning("⚠️ [VIRT-JS] coinsPage.js не найден среди <script src>.")

        log.info(f"🌐 [ALL-RESPONSES] Всего ответов за сессию: {len(all_responses)}")
        for i, r in enumerate(all_responses, start=1):
            log.info(f"🌐 [ALL-RESPONSES #{i}] rtype={r['rtype']} status={r['status']} url={r['url']}")

        page.remove_listener("response", on_any_response)

        html_live = page.content()
        rows = parse_table_fallback(html_live)
        log.info(f"📊 [FINAL] Итоговое число распарсенных строк: {len(rows)}")
        out_file = BASE / "live_page_dump.html"
        out_file.write_text(html_live, encoding="utf-8")
        log.info(f"💾 [FINAL] Полный HTML сохранён в {out_file.name} (длина={len(html_live)})")


def main():
    log.info(f"🚀 Запуск диагностики в {'HEADLESS' if HEADLESS else 'GUI'} режиме")
    with CoinalyzeScraper() as scraper:
        scraper.diagnose_virtualization()
    log.info("🏁 Диагностика завершена.")


if __name__ == "__main__":
    main()
```
