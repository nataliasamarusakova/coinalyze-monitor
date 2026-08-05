"""
coinalyze_loader.py — загрузчик с Coinalyze.
Использует огромный виртуальный экран (5000px) для моментального получения всех 90+ монет.
"""
from __future__ import annotations

import os
import re
import json
import time
import logging
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Browser, Page

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
DEBUG_HTML_FILE = BASE / "debug_page.html"
DEBUG_TBODY_FILE = BASE / "debug_tbody.html"

COINALYZE_P_SID = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "10"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
DEBUG = os.environ.get("DEBUG", "true").lower() == "true"

COINALYZE_URL = (
    "https://coinalyze.net/"
    "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
    "&filter=Y19ndF8yMDAwMDAwJmRfZ3RfMTAwMDAwMCZlX2d0XzAmc19ndF8w"
    "&order_by=volume_24hour&order_dir=desc"
)

# ─────────────────────────── парсеры ───────────────────────────

def parse_number(text: str) -> Optional[float]:
    """Парсинг чисел с поддержкой заглавных и строчных букв ($13.4b, $55.7m, $983.0k)."""
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


def extract_symbol_and_name(tr, tds, row_idx: int) -> tuple[str, str]:
    """Извлечение символа и названия монеты."""
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


def parse_table(html_text: str, ts: Optional[float] = None) -> list[dict]:
    ts = ts or time.time()
    soup = BeautifulSoup(html_text, "html.parser")
    tbody = soup.select_one("tbody")

    if tbody:
        tbody_html = str(tbody)
        DEBUG_TBODY_FILE.write_text(tbody_html, encoding="utf-8")
        tr_elements = tbody.find_all("tr")
        log.info(f"🔥 [TBODY DUMP] Всего строк <tr> внутри <tbody>: {len(tr_elements)}")

    rows = soup.select("tbody tr") if tbody else soup.select("tr")

    out = []
    for idx, tr in enumerate(rows, start=1):
        tds = tr.find_all(["td", "th"])

        if len(tds) < 2:
            continue

        symbol, name = extract_symbol_and_name(tr, tds, idx)

        rec = {
            "ts": ts,
            "symbol": symbol,
            "name": name,
            "price": get_td(tds, 2),
            "price_chg24": get_td(tds, 3),
            "mktcap": get_td(tds, 4),
            "volume24": get_td(tds, 5),
            "oi": get_td(tds, 6),
            "oi_chg24_pct": get_td(tds, 7),
            "oi_chg4h_pct": get_td(tds, 9),
            "oi_vol_ratio": get_td(tds, 11),
            "oi_mktcap_ratio": get_td(tds, 12),
            "fr_avg": get_td(tds, 13),
            "pfr_avg": get_td(tds, 14),
            "fr_oiw": get_td(tds, 15),
            "pfr_oiw": get_td(tds, 16),
            "liq_short24": get_td(tds, 17),
            "liq_long24": get_td(tds, 18),
            "ls_accounts": get_td(tds, 19),
            "btc_corr7d": get_td(tds, 20),
            "cvd24": get_td(tds, 21),
            "lls24": get_td(tds, 22),
        }
        out.append(rec)

    log.info(f"✅ [PARSER] Успешно распарсено монет: {len(out)}/{len(rows)}")
    return out


def get_td(tds: list, idx: int) -> Optional[float]:
    if idx < len(tds):
        return parse_number(tds[idx].get_text(strip=True))
    return None


def extract_pagination_urls(html_text: str) -> list[str]:
    """Извлечение ссылок пагинации из href и onclick."""
    soup = BeautifulSoup(html_text, "html.parser")
    found_urls = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "p=" in href or "page=" in href:
            full = f"https://coinalyze.net{href}" if href.startswith("/") else href
            if full not in found_urls:
                found_urls.append(full)

    for el in soup.find_all(attrs={"onclick": True}):
        onclick_str = el["onclick"]
        matches = re.findall(r"['\"](/?[^'\"]*p=\d+[^'\"]*)['\"]", onclick_str)
        for m in matches:
            full = f"https://coinalyze.net{m}" if m.startswith("/") else m
            if full not in found_urls:
                found_urls.append(full)

    return found_urls


# ─────────────────────────── скрапер ───────────────────────────

class CoinalyzeScraper:
    def __init__(self, headless: bool = HEADLESS, max_pages: int = MAX_PAGES, debug: bool = DEBUG):
        self.headless = headless
        self.max_pages = max_pages
        self.debug = debug
        self._pw = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None

    def __enter__(self) -> "CoinalyzeScraper":
        self._pw = sync_playwright().start()

        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--window-size=1920,5000",
            ],
        )

        # ═══════════════════════════════════════════════════════════
        # ВЫСОТА ЭКРАНА 5000px ДЛЯ МНОГОКРАТНОЙ ВМЕСТИМОСТИ МОНЕТ
        # ═══════════════════════════════════════════════════════════
        ctx = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 5000},
            locale="en-US",
        )

        # Вшиваем настройки таблицы (100 монет на страницу)
        ctx.add_init_script("""() => {
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            try {
                localStorage.setItem('table_length', '100');
                localStorage.setItem('rows_per_page', '100');
            } catch(e){}
        }""")

        self._add_cookies(ctx)
        self._page = ctx.new_page()
        stealth_sync(self._page)
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

    def _load(self, url: str) -> Optional[str]:
        page = self._page
        log.info(f"🌐 [PLAYWRIGHT] Загружаем URL: {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=40_000)
            
            # Ждем 6 секунд прогрузки таблицы
            page.wait_for_timeout(6000)

            if "Attention Required" in page.content():
                log.warning("⚠️ Обнаружен Cloudflare, ждем 10 сек...")
                page.wait_for_timeout(10_000)

            page.wait_for_selector("tbody tr", timeout=12_000)
        except Exception as e:
            log.warning(f"⚠️ Таблица `tbody tr` не загружена ({e})")
            return None

        # Прокрутка вниз по огромному экрану
        log.info("📜 [SCROLL] Прокрутка огромного экрана (5000px)...")
        prev_count = 0
        no_change = 0

        for step in range(1, 20):
            page.keyboard.press("PageDown")
            page.mouse.wheel(0, 3000)

            page.evaluate("""() => {
                const wrapper = document.querySelector('.table-wrapper') || document.querySelector('div[class*="wrapper"]');
                if (wrapper) wrapper.scrollTop += 3000;
                window.scrollBy(0, 3000);
            }""")

            page.wait_for_timeout(1200)

            cur_count = len(page.query_selector_all("tbody tr"))
            log.info(f"   [Скролл #{step:02d}] Строк в DOM: {cur_count}")

            if cur_count == prev_count:
                no_change += 1
                if no_change >= 3:
                    log.info(f"✅ [SCROLL] Итого строк в DOM: {cur_count}")
                    break
            else:
                no_change = 0
                prev_count = cur_count

        return page.content()

    def fetch_all(self) -> list[dict]:
        all_rows: list[dict] = []
        seen: set[str] = set()

        def add_rows(rows: list[dict]):
            new = 0
            for r in rows:
                sym = r.get("symbol")
                if sym and sym not in seen:
                    all_rows.append(r)
                    seen.add(sym)
                    new += 1
            return new

        queue_urls = [COINALYZE_URL]
        processed_urls = set()

        page_num = 1
        while queue_urls and page_num <= self.max_pages:
            current_url = queue_urls.pop(0)
            if current_url in processed_urls:
                continue

            processed_urls.add(current_url)
            log.info(f"\n==================================================")
            log.info(f"📑 === ЗАГРУЗКА И ОБРАБОТКА СТРАНИЦЫ #{page_num} ===")
            log.info(f"🔗 URL: {current_url}")
            log.info(f"==================================================")

            html_text = self._load(current_url)
            if not html_text:
                log.info(f"🛑 [FETCH] Страница #{page_num} не содержит таблицы.")
                break

            if self.debug and page_num == 1:
                DEBUG_HTML_FILE.write_text(html_text, encoding="utf-8")

            parsed_page = parse_table(html_text)
            new_added = add_rows(parsed_page)
            log.info(f"🏁 [SUMMARY] Страница #{page_num}: распарсено={len(parsed_page)}, добавлено_новых={new_added}, всего_уникальных={len(all_rows)}")

            found_urls = extract_pagination_urls(html_text)
            for page_url in found_urls:
                if page_url not in processed_urls and page_url not in queue_urls:
                    queue_urls.append(page_url)

            if new_added == 0 and page_num > 1:
                break

            page_num += 1

        log.info(f"\n==================================================")
        log.info(f"🎉 ИТОГО УНИКАЛЬНЫХ МОНЕТ СОБРАНО: {len(all_rows)}")
        log.info(f"==================================================")

        if all_rows:
            first_coin = all_rows[0]
            last_coin = all_rows[-1]
            log.info(
                f"🥇 ПЕРВАЯ МОНЕТА (#1): symbol={first_coin.get('symbol')}, name={first_coin.get('name')}, "
                f"price=${first_coin.get('price')}, volume24=${first_coin.get('volume24')}, oi=${first_coin.get('oi')}"
            )
            log.info(
                f"🏁 ПОСЛЕДНЯЯ МОНЕТА (#{len(all_rows)}): symbol={last_coin.get('symbol')}, name={last_coin.get('name')}, "
                f"price=${last_coin.get('price')}, volume24=${last_coin.get('volume24')}, oi=${last_coin.get('oi')}"
            )

        return all_rows


def main():
    log.info(f"🚀 Запуск скрипта в {'HEADLESS' if HEADLESS else 'GUI'} режиме")
    with CoinalyzeScraper() as scraper:
        coins = scraper.fetch_all()

    out_file = BASE / "coins_debug.json"
    out_file.write_text(json.dumps(coins, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"💾 Успешно сохранено {len(coins)} монет в файл {out_file.name}")


if __name__ == "__main__":
    main()
