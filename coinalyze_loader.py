"""
coinalyze_loader.py — загрузчик с Coinalyze.
Включает глубокую маскировку под реальный браузер и выбор 100+ строк в таблице.
"""
from __future__ import annotations

import os
import json
import time
import logging
from pathlib import Path
from typing import Optional, Tuple

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

COINALYZE_P_SID = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "10"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
DEBUG = os.environ.get("DEBUG", "true").lower() == "true"

COINALYZE_URL = (
    "https://coinalyze.net/"
    "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY4"
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
    rows = soup.select("tbody tr")
    
    if not rows:
        rows = soup.select("tr")

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

    return out


def get_td(tds: list, idx: int) -> Optional[float]:
    if idx < len(tds):
        return parse_number(tds[idx].get_text(strip=True))
    return None


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

        # ═══════════════════════════════════════════════════════════
        # МАСКИРОВКА ПОД РЕАЛЬНЫЙ БРАУЗЕР (ОБХОД ДЕТЕКТА ХЕДЛЕССА)
        # ═══════════════════════════════════════════════════════════
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--window-size=1920,1080",
            ],
        )

        ctx = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        # Скрываем атрибут navigator.webdriver
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

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
        log.info(f"🔑 P_SID: {'SET' if COINALYZE_P_SID else 'EMPTY'}, CHAT_SID: {'SET' if COINALYZE_CHAT_SID else 'EMPTY'}")
        if not (COINALYZE_P_SID or COINALYZE_CHAT_SID):
            log.warning("⚠️ Куки не установлены!")
            return
        cookies = [
            {"name": "cookies_accepted", "value": "1", "domain": "coinalyze.net", "path": "/", "secure": True}
        ]
        if COINALYZE_P_SID:
            cookies.append({"name": "p_sid", "value": COINALYZE_P_SID, "domain": "coinalyze.net", "path": "/", "secure": True})
        if COINALYZE_CHAT_SID:
            cookies.append({"name": "chat_sid", "value": COINALYZE_CHAT_SID, "domain": "coinalyze.net", "path": "/", "secure": True})
        ctx.add_cookies(cookies)

    def _load(self, url: str) -> Tuple[str, bool]:
        page = self._page
        log.info(f"🌐 Загружаем URL: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=50_000)
        page.wait_for_timeout(4000)

        if "Attention Required" in page.content():
            log.warning("⚠️ Обнаружен Cloudflare, ждем 10 сек...")
            page.wait_for_timeout(10_000)

        page.wait_for_selector("tbody tr", timeout=25_000)

        # ═══════════════════════════════════════════════════════════
        # ПРИНУДИТЕЛЬНО ПЕРЕКЛЮЧАЕМ ВЫПАДАЮЩИЙ СПИСОК СТРОК НА 100/ALL
        # ═══════════════════════════════════════════════════════════
        try:
            page.evaluate("""() => {
                const selects = document.querySelectorAll('select');
                selects.forEach(s => {
                    for (let opt of s.options) {
                        if (opt.value === '100' || opt.text.includes('100') || opt.value === '-1') {
                            s.value = opt.value;
                            s.dispatchEvent(new Event('change', { bubbles: true }));
                            break;
                        }
                    }
                });
            }""")
            page.wait_for_timeout(1500)
        except Exception as e:
            log.info(f"ℹ️ Выбор количества строк через select: {e}")

        # ═══════════════════════════════════════════════════════════
        # ГЛУБОКИЙ СКРОЛЛИНГ С НАЖАТИЕМ КЛАВИШ
        # ═══════════════════════════════════════════════════════════
        log.info("📜 Начинаем глубокий скроллинг страницы...")
        prev_count = 0
        no_change = 0

        for step in range(1, 30):
            page.mouse.wheel(0, 3000)
            page.keyboard.press("PageDown")
            page.keyboard.press("End")
            page.evaluate("window.scrollBy(0, 2500)")
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

            page.wait_for_timeout(2000)

            cur_count = len(page.query_selector_all("tbody tr"))
            log.info(f"   [Скролл #{step}] Загружено строк в DOM: {cur_count}")

            if cur_count == prev_count:
                no_change += 1
                if no_change >= 5:  # Ждем 5 шагов (10 секунд без изменений)
                    log.info(f"✅ Скроллинг завершен на шаге {step}. Всего строк: {cur_count}")
                    break
            else:
                no_change = 0
                prev_count = cur_count

        rows = page.query_selector_all("tbody tr")
        log.info(f"📊 [Playwright] Итого строк на текущей странице: {len(rows)}")

        full_html = page.content()

        soup = BeautifulSoup(full_html, "html.parser")
        has_pagination = bool(
            soup.select_one(".pagination") 
            or soup.select("a[href*='p=']") 
            or soup.select("a[href*='page=']")
        )

        return full_html, has_pagination

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

        base_url = COINALYZE_URL

        for page_num in range(1, self.max_pages + 1):
            if page_num == 1:
                url = base_url
            else:
                sep = "&" if "?" in base_url else "?"
                url = f"{base_url}{sep}p={page_num}"

            log.info(f"\n📑 === ЗАГРУЗКА СТРАНИЦЫ #{page_num} ({url}) ===")
            try:
                html_text, has_pagination = self._load(url)
                if self.debug and page_num == 1:
                    DEBUG_HTML_FILE.write_text(html_text, encoding="utf-8")

                parsed_page = parse_table(html_text)
                new_added = add_rows(parsed_page)
                log.info(f"🏁 Страница #{page_num}: распарсено={len(parsed_page)}, добавлено_новых={new_added}")

                if not has_pagination:
                    log.info("ℹ️ Пагинация на странице отсутствует (все монеты находятся на одной странице). Сбор завершен.")
                    break

                if new_added == 0:
                    log.info(f"✅ На странице #{page_num} нет новых монет. Завершаем сбор.")
                    break

            except Exception as e:
                log.error(f"❌ Ошибка при загрузке страницы #{page_num}: {e}")
                break

        log.info(f"\n🎉 ИТОГО УНИКАЛЬНЫХ МОНЕТ СОБРАНО: {len(all_rows)}")
        if all_rows:
            sample = all_rows[0]
            log.info(f"📋 Пример первой монеты: symbol={sample.get('symbol')}, name={sample.get('name')}, price={sample.get('price')}")
        return all_rows


def main():
    log.info(f"🚀 Запуск скрипта в {'HEADLESS' if HEADLESS else 'GUI'} режиме")
    with CoinalyzeScraper() as scraper:
        coins = scraper.fetch_all()

    out_file = BASE / "coins_debug.json"
    out_file.write_text(json.dumps(coins, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"💾 Сохранено {len(coins)} монет в {out_file.name}")


if __name__ == "__main__":
    main()
