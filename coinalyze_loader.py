"""
coinalyze_loader.py — загрузчик с Coinalyze.
Диагностика: полный HTML пагинации, колонки, сырой HTML строк.
"""
from __future__ import annotations

import os
import json
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

# ─────────────────────────── логирование ───────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("coinalyze_loader")

# ─────────────────────────── конфигурация ───────────────────────────

BASE = Path(__file__).resolve().parent
DEBUG_HTML_FILE = BASE / "debug_page.html"
DEBUG_PAGINATION_FILE = BASE / "debug_pagination.html"

COINALYZE_P_SID = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

COINALYZE_URL = (
    "https://coinalyze.net/"
    "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
    "&filter=Y19ndF8yMDAwMDAwJmRfZ3RfMTAwMDAwMCZlX2d0XzAmc19ndF8wJmNtNjE2NV9ndF80NSZjbTYxNjRfbHRfNjA"
    "&order_by=volume_24hour&order_dir=desc"
)


# ─────────────────────────── парсеры ───────────────────────────

def parse_number(text: str) -> Optional[float]:
    if not text:
        return None
    t = text.strip().replace(",", "").replace("$", "").replace("%", "")
    if t in ("", "-", "—", "N/A", "n/a"):
        return None
    mult = 1.0
    if t and t[-1] in "KMB":
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}[t[-1]]
        t = t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def parse_table(html_text: str, ts: Optional[float] = None) -> list[dict]:
    import time
    ts = ts or time.time()
    soup = BeautifulSoup(html_text, "lxml")
    rows = soup.select("tbody tr")
    out = []
    for tr in rows:
        symbol = tr.get("data-coin")
        tds = tr.find_all("td")
        if len(tds) < 23:
            continue
        spans = tds[1].find_all("span")
        name = spans[0].get_text(strip=True) if spans else (symbol or "?")
        rec = {
            "ts": ts, "symbol": symbol, "name": name,
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
        out.append(rec)
    return out


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
        self._browser = self._pw.chromium.launch(headless=self.headless)
        ctx = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
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
        log.info(f"🔑 Env check: COINALYZE_P_SID={'SET (' + str(len(COINALYZE_P_SID)) + ' chars)' if COINALYZE_P_SID else 'EMPTY'}")
        log.info(f"🔑 Env check: COINALYZE_CHAT_SID={'SET (' + str(len(COINALYZE_CHAT_SID)) + ' chars)' if COINALYZE_CHAT_SID else 'EMPTY'}")
        if not (COINALYZE_P_SID or COINALYZE_CHAT_SID):
            log.warning("⚠️ Куки НЕ установлены — сайт отдаст дефолтную таблицу")
            return
        cookies = []
        if COINALYZE_P_SID:
            cookies.append({"name": "p_sid", "value": COINALYZE_P_SID,
                             "domain": "coinalyze.net", "path": "/", "secure": True})
        if COINALYZE_CHAT_SID:
            cookies.append({"name": "chat_sid", "value": COINALYZE_CHAT_SID,
                             "domain": "coinalyze.net", "path": "/", "secure": True})
        cookies.append({"name": "cookies_accepted", "value": "1",
                         "domain": "coinalyze.net", "path": "/", "secure": True})
        ctx.add_cookies(cookies)
        log.info(f"✅ Куки установлены: {len(cookies)} шт.")

    # ───────── загрузка страницы ─────────

    def _load(self, url: str) -> str:
        page = self._page
        page.goto(url, wait_until="domcontentloaded", timeout=50_000)
        page.wait_for_timeout(4000)

        if "Attention Required" in page.content():
            log.warning("Cloudflare detected, waiting 10s...")
            page.wait_for_timeout(10_000)

        # Ждём таблицу
        page.wait_for_selector("tbody tr", timeout=25_000)

        # ═══════════════════════════════════════════════════════════
        # ДИАГНОСТИКА СЕЛЕКТОРА .pagination — ПОЛНЫЙ ВЫВОД В ЛОГ
        # ═══════════════════════════════════════════════════════════
        log.info("🔍 Пытаемся найти селектор: .pagination")
        try:
            page.wait_for_selector(".pagination", timeout=10_000)
            log.info("✅ СЕЛЕКТОР .pagination НАЙДЕН в DOM")
            
            pag_el = page.query_selector(".pagination")
            if pag_el:
                # ПОЛНЫЙ HTML блока пагинации
                pag_html = pag_el.evaluate("el => el.outerHTML")
                log.info("=" * 80)
                log.info("📄 ПОЛНЫЙ HTML БЛОКА .pagination (outerHTML):")
                log.info("=" * 80)
                # Разбиваем на строки для читаемости в логе GitHub Actions
                for line in pag_html.split("\n"):
                    if line.strip():
                        log.info(f"   {line.strip()}")
                log.info("=" * 80)
                
                # Сохраняем в файл
                DEBUG_PAGINATION_FILE.write_text(pag_html, encoding="utf-8")
                log.info(f"💾 Сохранено в {DEBUG_PAGINATION_FILE.name} ({len(pag_html)} симв.)")
                
                # Детальный разбор всех элементов внутри
                links = pag_el.query_selector_all("a")
                log.info(f"🔗 Элементов <a> внутри .pagination: {len(links)}")
                for i, a in enumerate(links):
                    href = a.get_attribute("href") or "(no href)"
                    text = (a.inner_text() or "").strip()
                    log.info(f"   a[{i}]: text='{text}' href='{href}'")
                    
                buttons = pag_el.query_selector_all("button")
                log.info(f"🔘 Элементов <button> внутри .pagination: {len(buttons)}")
                for i, b in enumerate(buttons):
                    text = (b.inner_text() or "").strip()
                    log.info(f"   button[{i}]: text='{text}'")
                    
                spans = pag_el.query_selector_all("span")
                log.info(f"📍 Элементов <span> внутри .pagination: {len(spans)}")
                for i, s in enumerate(spans):
                    text = (s.inner_text() or "").strip()
                    cls = s.get_attribute("class") or ""
                    log.info(f"   span[{i}]: text='{text}' class='{cls}'")
            else:
                log.warning("⚠️ query_selector('.pagination') вернул None, хотя wait_for_selector сработал!")
        except Exception as e:
            log.warning(f"❌ СЕЛЕКТОР .pagination НЕ НАЙДЕН за 10с: {e}")
        # ═══════════════════════════════════════════════════════════

        # Скроллим для подгрузки
        prev_count = 0
        for _ in range(5):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(800)
            cur_count = len(page.query_selector_all("tbody tr"))
            if cur_count == prev_count:
                break
            prev_count = cur_count
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)

        rows = page.query_selector_all("tbody tr[data-coin]") or page.query_selector_all("tbody tr")
        row_count = len(rows)

        # Диагностика колонок
        log.info(f"🔗 URL: {url}")
        if row_count > 0:
            first_row = rows[0]
            tds = first_row.query_selector_all("td")
            log.info(f"🔍 Первая строка: КОЛОНОК = {len(tds)} (требуется >= 23)")
            
            if len(tds) < 23:
                log.warning(f"⚠️ ТАБЛИЦА ИМЕЕТ ТОЛЬКО {len(tds)} КОЛОНОК — парсер отбросит все {row_count} строк!")
                row_html = first_row.evaluate("el => el.outerHTML")
                log.info("📄 HTML первой строки таблицы:")
                for line in row_html.split("\n")[:20]:
                    if line.strip():
                        log.info(f"   {line.strip()}")
                Path(BASE / "debug_first_row.html").write_text(row_html, encoding="utf-8")
                log.info(f"💾 Первая строка сохранена в debug_first_row.html")
        else:
            log.warning("❌ В таблице вообще нет строк")

        has_pagination = page.query_selector(".pagination") is not None
        log.info(f"📊 Итог: строк={row_count}, пагинация={'ЕСТЬ' if has_pagination else 'НЕТ'}")

        return page.inner_html("body")

    # ───────── пагинация ─────────

    @staticmethod
    def _get_page_urls(html_text: str) -> list[str]:
        soup = BeautifulSoup(html_text, "lxml")
        pagination = soup.select_one(".pagination")
        if not pagination:
            return [COINALYZE_URL]
        urls = [COINALYZE_URL]
        for a in pagination.select("a[href]"):
            href = a.get("href", "")
            if not href:
                continue
            full_url = f"https://coinalyze.net{href}" if href.startswith("/") else href
            if full_url not in urls:
                urls.append(full_url)
        return urls[:MAX_PAGES]

    def _click_next_page(self, current_page_num: int) -> bool:
        page = self._page
        pag = page.query_selector(".pagination")
        if not pag:
            return False
        first_row = page.query_selector("tbody tr")
        before = first_row.get_attribute("data-coin") if first_row else None
        target = None
        for el in pag.query_selector_all("a, button, li"):
            if (el.inner_text() or "").strip() == str(current_page_num + 1):
                target = el
                break
        if target is None:
            target = pag.query_selector("[aria-label='Next'], .next, a[rel='next']")
        if target is None:
            return False
        target.click()
        page.wait_for_timeout(1500)
        for _ in range(10):
            first_row = page.query_selector("tbody tr")
            after = first_row.get_attribute("data-coin") if first_row else None
            if after and after != before:
                return True
            page.wait_for_timeout(500)
        return False

    def _dump_debug(self, html_text: str):
        if not self.debug:
            return
        DEBUG_HTML_FILE.write_text(html_text, encoding="utf-8")
        log.info(f"💾 Полный HTML страницы сохранён в {DEBUG_HTML_FILE.name}")

    # ───────── публичный метод ─────────

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

        html_text = self._load(COINALYZE_URL)
        self._dump_debug(html_text)
        add_rows(parse_table(html_text))
        log.info(f"Страница 1: {len(all_rows)} монет (из {len(seen)} уникальных)")

        urls = self._get_page_urls(html_text)
        log.info(f"📑 URL-ов для загрузки: {len(urls)}")

        if len(urls) > 1:
            for i, url in enumerate(urls[1:], start=2):
                try:
                    log.info(f"➡️ Загружаю страницу {i}: {url[:120]}...")
                    html_text = self._load(url)
                    new_count = add_rows(parse_table(html_text))
                    log.info(f"Страница {i}: +{new_count} новых монет")
                except Exception as e:
                    log.error(f"Ошибка страницы {i}: {e}")
        else:
            soup = BeautifulSoup(html_text, "lxml")
            if soup.select_one(".pagination"):
                log.info("href не найдены, пробуем клики по .pagination")
                page_num = 1
                while page_num < self.max_pages:
                    if not self._click_next_page(page_num):
                        break
                    self._page.wait_for_selector("tbody tr", timeout=15_000)
                    self._page.wait_for_timeout(500)
                    html_text = self._page.content()
                    new_count = add_rows(parse_table(html_text))
                    page_num += 1
                    log.info(f"Страница {page_num}: +{new_count} новых монет (клик)")

        log.info(f"ИТОГО уникальных монет: {len(all_rows)}")
        return all_rows


def main():
    log.info(f"🚀 Запуск в {'HEADLESS' if HEADLESS else 'GUI'} режиме")
    with CoinalyzeScraper() as scraper:
        coins = scraper.fetch_all()

    out_file = BASE / "coins_debug.json"
    out_file.write_text(json.dumps(coins, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"Сохранено {len(coins)} монет в {out_file.name}")


if __name__ == "__main__":
    main()
