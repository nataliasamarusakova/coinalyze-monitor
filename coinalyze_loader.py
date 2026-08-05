Хорошо, сделаем это программно — раскодируем текущий фильтр, убираем `cm6165_gt_45` и `cm6164_lt_60`, перекодируем обратно в base64 и делаем запрос с этим новым URL. Заодно оставим сравнение с полным фильтром и без фильтра вовсе, чтобы увидеть все три цифры сразу.

```python
"""
coinalyze_loader.py — сравнение количества строк с полным фильтром, с урезанным
фильтром (без кастомных метрик cm6165/cm6164) и вовсе без фильтра.
"""
from __future__ import annotations

import os
import base64
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

HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "40000"))
POST_LOAD_WAIT_MS = int(os.environ.get("POST_LOAD_WAIT_MS", "5000"))

COINALYZE_COOKIES_RAW = os.environ.get(
    "COINALYZE_COOKIES_RAW",
    "_ga=GA1.1.1437320651.1775048231; cookies_accepted=1; theme=dark; "
    "chat_sid=bfd66807-13b1-42e7-bd4c-047f84aacd56; "
    "p_sid=s%3ApxWmyam1Q3mxLdmX3wsqYmdZl0TOOEaY.lrXOk%2BfKqC%2BF%2F9b8qiofzHGnWfM%2FdlxAJMY%2BCP3lGnM; "
    "_ga_S5GL9D82Q3=GS2.1.s1785952092$o129$g1$t1785953007$j60$l0$h0",
)

COLUMNS_PARAM = "YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"

FULL_FILTER_B64 = "Y19ndF8yMDAwMDAwJmRfZ3RfMTAwMDAwMCZlX2d0XzAmc19ndF8wJmNtNjE2NV9ndF80NSZjbTYxNjRfbHRfNjA"

# ─── декодируем полный фильтр, убираем cm6165/cm6164, перекодируем обратно ───
full_filter_decoded = base64.b64decode(FULL_FILTER_B64).decode()
log.info(f"🔎 [FILTER] Полный фильтр раскодирован: {full_filter_decoded}")

parts = full_filter_decoded.split("&")
trimmed_parts = [p for p in parts if not p.startswith("cm6165_") and not p.startswith("cm6164_")]
trimmed_filter_decoded = "&".join(trimmed_parts)
log.info(f"🔎 [FILTER] После удаления cm6165/cm6164: {trimmed_filter_decoded}")

trimmed_filter_b64 = base64.b64encode(trimmed_filter_decoded.encode()).decode()
log.info(f"🔎 [FILTER] Перекодировано обратно в base64: {trimmed_filter_b64}")

URL_NO_FILTER = "https://coinalyze.net/"
URL_FULL_FILTER = (
    f"https://coinalyze.net/?columns={COLUMNS_PARAM}&filter={FULL_FILTER_B64}"
    f"&order_by=volume_24hour&order_dir=desc"
)
URL_TRIMMED_FILTER = (
    f"https://coinalyze.net/?columns={COLUMNS_PARAM}&filter={trimmed_filter_b64}"
    f"&order_by=volume_24hour&order_dir=desc"
)


def parse_cookie_header(raw: str) -> list[dict]:
    cookies = []
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    for part in parts:
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": "coinalyze.net",
            "path": "/",
            "secure": True,
        })
    return cookies


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


def parse_table(html_text: str) -> list[dict]:
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
    return out


class CoinalyzeScraper:
    def __init__(self, headless: bool = HEADLESS):
        self.headless = headless
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
        self._add_full_cookies(ctx)
        self._page = ctx.new_page()
        stealth_sync(self._page)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    @staticmethod
    def _add_full_cookies(ctx):
        cookies = parse_cookie_header(COINALYZE_COOKIES_RAW)
        log.info(f"🔑 [COOKIES] Загружаем {len(cookies)} куки")
        if cookies:
            ctx.add_cookies(cookies)
        else:
            log.warning("⚠️ [COOKIES] Куки не распарсились!")

    def _wait_cloudflare(self):
        page = self._page
        try:
            content = page.content()
        except Exception:
            content = ""
        if any(m in content for m in ("Attention Required", "Just a moment", "cf-browser-verification")):
            log.warning("⚠️ Обнаружен Cloudflare челлендж, ждём подольше...")
            page.wait_for_timeout(10_000)

    def load_and_count(self, url: str, label: str) -> list[dict]:
        page = self._page
        log.info(f"🌐 [{label}] Открываем: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        self._wait_cloudflare()
        page.wait_for_timeout(POST_LOAD_WAIT_MS)

        html = page.content()
        rows = parse_table(html)
        log.info(f"📊 [{label}] Строк в tbody: {len(rows)}")
        if rows:
            log.info(f"📊 [{label}] Первая строка: {rows[0]}")
            log.info(f"📊 [{label}] Последняя строка: {rows[-1]}")

        out_file = BASE / f"dump_{label.lower().replace(' ', '_')}.html"
        out_file.write_text(html, encoding="utf-8")
        log.info(f"💾 [{label}] HTML сохранён в {out_file.name}")
        return rows

    def compare_three(self):
        log.info("=" * 60)
        log.info("ШАГ 1: без фильтра вовсе")
        log.info("=" * 60)
        rows_no_filter = self.load_and_count(URL_NO_FILTER, "NO-FILTER")

        log.info("=" * 60)
        log.info("ШАГ 2: с ПОЛНЫМ фильтром (включая cm6165/cm6164)")
        log.info("=" * 60)
        rows_full = self.load_and_count(URL_FULL_FILTER, "FULL-FILTER")

        log.info("=" * 60)
        log.info("ШАГ 3: с УРЕЗАННЫМ фильтром (без cm6165/cm6164)")
        log.info("=" * 60)
        rows_trimmed = self.load_and_count(URL_TRIMMED_FILTER, "TRIMMED-FILTER")

        log.info("=" * 60)
        log.info(
            f"🏁 ИТОГ: без фильтра={len(rows_no_filter)}, "
            f"полный фильтр={len(rows_full)}, "
            f"урезанный фильтр (без cm6165/cm6164)={len(rows_trimmed)}"
        )
        log.info("=" * 60)


def main():
    log.info(f"🚀 Запуск сравнения в {'HEADLESS' if HEADLESS else 'GUI'} режиме")
    with CoinalyzeScraper() as scraper:
        scraper.compare_three()
    log.info("🏁 Завершено.")


if __name__ == "__main__":
    main()
```

Запустите и пришлите лог — особенно три строки `📊 [...] Строк в tbody: N` и финальный `🏁 ИТОГ`. Если "урезанный фильтр" (без `cm6165`/`cm6164`, но с оставшимися `c_gt_2000000&d_gt_1000000&e_gt_0&s_gt_0`) даст число близкое к 90+, значит именно эти две кастомные метрики были главной причиной сужения выборки, и дальше можно точно решить, оставлять их или ослаблять пороги.
