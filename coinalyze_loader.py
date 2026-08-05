"""
coinalyze_loader.py — загрузчик/диагностика данных с Coinalyze.

Обновление: в coinsPage.js обнаружен pub.ReconnectingWebSocket — данные таблицы
(price/volume/OI/funding и т.д.) скорее всего приходят по WebSocket и рендерятся
клиентским JS в DOM, поэтому их нет ни в assets-info, ни в инлайн-скриптах HTML.
Этот скрипт перехватывает WS-фреймы и дампит реальный tbody после загрузки.
"""
from __future__ import annotations

import os
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
POST_LOAD_WAIT_MS = int(os.environ.get("POST_LOAD_WAIT_MS", "8000"))  # увеличено, дать WS время прислать данные

LOG_CHUNK_SIZE = int(os.environ.get("LOG_CHUNK_SIZE", "4000"))
MAX_LOG_CHARS = int(os.environ.get("MAX_LOG_CHARS", "0"))

WS_FRAME_LOG_LIMIT = int(os.environ.get("WS_FRAME_LOG_LIMIT", "3000"))  # символов на фрейм в логе

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


# ─────────────────────────── парсер таблицы ───────────────────────────
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
            log.info(f"📤 [WS SENT] {ws.url}\n{text[:WS_FRAME_LOG_LIMIT]}")
            self._ws_frames.append({"dir": "sent", "url": ws.url, "payload": text})

        def on_framereceived(payload):
            text = payload if isinstance(payload, str) else repr(payload)
            log.info(f"📥 [WS RECV] {ws.url} длина={len(text)}\n{text[:WS_FRAME_LOG_LIMIT]}")
            self._ws_frames.append({"dir": "recv", "url": ws.url, "payload": text})

        def on_close():
            log.info(f"🔌 [WS] Закрыт: {ws.url}")

        ws.on("framesent", on_framesent)
        ws.on("framereceived", on_framereceived)
        ws.on("close", lambda: on_close())

    def _wait_cloudflare(self):
        page = self._page
        try:
            content = page.content()
        except Exception:
            content = ""
        if any(m in content for m in ("Attention Required", "Just a moment", "cf-browser-verification")):
            log.warning("⚠️ Обнаружен Cloudflare челлендж, ждём подольше...")
            page.wait_for_timeout(10_000)

    def run_full_diagnosis(self, url: str = COINALYZE_URL):
        """Открываем страницу, ловим WS-фреймы и assets-info, дампим реальный tbody."""
        page = self._page
        self._captured_responses.clear()
        self._doc_html.clear()
        self._ws_frames.clear()
        self._ws_connections.clear()

        log.info(f"🌐 [PLAYWRIGHT] Открываем URL: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        self._wait_cloudflare()

        log.info(f"⏳ Ждём {POST_LOAD_WAIT_MS}ms, чтобы WS успел прислать данные...")
        page.wait_for_timeout(POST_LOAD_WAIT_MS)

        # ---- сводка по WebSocket ----
        log.info(f"🔌 [WS SUMMARY] Всего открытых WS-соединений: {len(self._ws_connections)}")
        for u in self._ws_connections:
            log.info(f"🔌 [WS SUMMARY]   - {u}")
        log.info(f"🔌 [WS SUMMARY] Всего пойманных фреймов: {len(self._ws_frames)}")

        recv_frames = [f for f in self._ws_frames if f["dir"] == "recv"]
        log.info(f"🔌 [WS SUMMARY] Из них полученных (recv): {len(recv_frames)}")
        for i, f in enumerate(recv_frames, start=1):
            log.info(f"📥 [WS RECV #{i}] от {f['url']}")
            data = try_parse_payload(f["payload"])
            if data is not None:
                describe_payload(data, prefix=f"WS RECV #{i} STRUCT")
            else:
                log_full_body(f"WS RECV #{i} RAW", f["payload"])

        if not self._ws_connections:
            log.warning("⚠️ [WS] Ни одного WebSocket-соединения не было открыто за время ожидания. "
                        "Возможно WS открывается по требованию (например, только при видимости вкладки) "
                        "или использует другой транспорт (long-polling).")

        # ---- сводка по assets-info ----
        if self._captured_responses:
            for i, item in enumerate(self._captured_responses, start=1):
                log.info(f"📦 [ASSETS-INFO #{i}] status={item['status']} длина={len(item['body'])}")

        # ---- дамп реального tbody после ожидания ----
        html_live = page.content()
        rows = parse_table_fallback(html_live)
        log.info(f"📊 [LIVE-TBODY] Строк в tbody после {POST_LOAD_WAIT_MS}ms ожидания: {len(rows)}")
        for i, r in enumerate(rows[:10], start=1):
            log.info(f"📊 [LIVE-TBODY] Строка #{i}: {r}")
        if len(rows) > 10:
            log.info(f"📊 [LIVE-TBODY] ... и ещё {len(rows) - 10} строк (не показаны, обрежь вывод при желании)")

        # сохраняем полный live HTML для ручного анализа, если нужно будет свериться
        out_file = BASE / "live_page_dump.html"
        out_file.write_text(html_live, encoding="utf-8")
        log.info(f"💾 [LIVE] Полный HTML после ожидания сохранён в {out_file.name} (длина={len(html_live)})")


def main():
    log.info(f"🚀 Запуск диагностики в {'HEADLESS' if HEADLESS else 'GUI'} режиме")
    with CoinalyzeScraper() as scraper:
        scraper.run_full_diagnosis()
    log.info("🏁 Диагностика завершена. Смотри: 🔌 [WS SUMMARY]/[WS RECV] — есть ли реальный WS-трафик с данными, "
             "📊 [LIVE-TBODY] — сколько строк и что внутри них реально после ожидания.")


if __name__ == "__main__":
    main()
