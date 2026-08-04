"""
coinalyze_loader.py — отдельный загрузчик таблицы Coinalyze.

Умеет:
- работать с печеньками и storage_state.json;
- ждать таблицу;
- пытаться выставить максимальный page size;
- искать пагинацию по широкому списку селекторов;
- листать страницы кликом по Next, даже если пагинация сделана JS-кнопками;
- сохранять debug_page.html;
- возвращать список монет в том же формате, который ожидает monitor.py.
"""

import os
import sys
import time
import json
import logging
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

try:
    from playwright_stealth import stealth_sync
except ImportError:
    def stealth_sync(page):
        pass

try:
    import requests
except ImportError:
    requests = None


BASE = Path(__file__).resolve().parent
DEBUG_HTML_FILE = BASE / "debug_page.html"
STORAGE_STATE_FILE = BASE / "storage_state.json"

COINALYZE_P_SID = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

MAX_PAGES = int(os.environ.get("COINALYZE_MAX_PAGES", "5"))
HEADLESS = os.environ.get("COINALYZE_HEADLESS", "true").lower() != "false"

DEFAULT_COINALYZE_URL = (
    "https://coinalyze.net/"
    "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
    "&filter=Y19ndF8yMDAwMDAwJmRfZ3RfMTAwMDAwMCZlX2d0XzAmc19ndF8wJmNtNjE2NV9ndF80NSZjbTYxNjRfbHRfNjA"
    "&order_by=volume_24hour&order_dir=desc"
)

COINALYZE_URL = os.environ.get("COINALYZE_URL", DEFAULT_COINALYZE_URL)

PAGINATION_SELECTORS = [
    ".pagination",
    ".dataTables_paginate",
    ".dataTables_pagination",
    ".paginate",
    "nav[aria-label*='agination' i]",
    "[class*=pagination i]",
    "[class*=paginate i]",
    "[class*=pager i]",
    "[class*=page-nav i]",
    "[role='navigation']",
]

NEXT_SELECTORS = [
    ".pagination li.next a",
    ".pagination li.next button",
    ".pagination a.next",
    ".pagination button.next",
    ".dataTables_paginate .paginate_button.next",
    ".dataTables_paginate a.next",
    ".page-item.next a",
    ".page-item.next button",
    "a.next",
    "button.next",
    "[aria-label*='Next' i]",
    "[title*='Next' i]",
    ".el-pagination .btn-next",
    ".ant-pagination-next",
    "button:has-text('Next')",
    "a:has-text('Next')",
    "button:has-text('→')",
    "a:has-text('→')",
    "button:has-text('>')",
    "a:has-text('>')",
]

log = logging.getLogger("coinalyze_loader")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def now_ts() -> int:
    return int(time.time())


def parse_number(raw):
    if raw is None:
        return None

    s = raw.strip().replace("$", "").replace("%", "").replace(",", "").replace("+", "")

    if s in ("", "n/a", "-", "—", "N/A"):
        return None

    mult = 1.0
    if s and s[-1].lower() in ("k", "m", "b", "t"):
        mult = {
            "k": 1e3,
            "m": 1e6,
            "b": 1e9,
            "t": 1e12,
        }[s[-1].lower()]
        s = s[:-1]

    try:
        return float(s) * mult
    except ValueError:
        return None


def parse_table(html_text: str) -> list[dict]:
    """
    Парсит таблицу Coinalyze из HTML.
    Формат строк совместим с monitor.py.
    """
    soup = BeautifulSoup(html_text, "lxml")
    rows = soup.select("tbody tr")

    log.info(f"Строк: {len(rows)}")

    ts = now_ts()
    out = []
    range_violations = 0

    for tr in rows:
        symbol = tr.get("data-coin")
        tds = tr.find_all("td")

        if len(tds) < 23:
            continue

        if not symbol and tds:
            symbol = tds[0].get_text(strip=True)

        spans = tds[1].find_all("span")
        name = spans[0].get_text(strip=True) if spans else (symbol or "?")

        rec = {
            "ts": ts,
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

        cvd = rec.get("cvd24")
        lls = rec.get("lls24")
        price = rec.get("price")

        if (
            (cvd is not None and not (0 <= cvd <= 100))
            or (lls is not None and not (0 <= lls <= 100))
            or (price is not None and price <= 0)
        ):
            range_violations += 1

        out.append(rec)

    if range_violations:
        log.warning(f"parse_table: {range_violations} подозрительных строк")

    return out


def send_tg(text: str):
    """
    Необязательная отправка в Telegram.
    Если requests/TG не настроены — просто пишем warning.
    """
    if requests is None or not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning("TG не настроен")
        return

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    chunks = []
    rem = text

    while len(rem) > 3800:
        sp = rem.rfind("\n", 0, 3800)
        if sp == -1:
            sp = 3800
        chunks.append(rem[:sp])
        rem = rem[sp:]

    chunks.append(rem)

    for ch in chunks:
        try:
            r = requests.post(
                url,
                data={
                    "chat_id": TG_CHAT_ID,
                    "text": ch,
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if r.status_code != 200:
                log.warning(f"TG status={r.status_code}: {r.text[:200]}")
            time.sleep(0.4)
        except Exception as e:
            log.error(f"TG: {e}")


def _find_visible(page, selectors):
    """
    Возвращает первый видимый селектор из списка.
    """
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return sel
        except Exception:
            pass
    return None


def _wait_pagination(page, timeout_ms: int = 8000):
    """
    Ждёт появление блока пагинации.
    """
    deadline = time.time() + timeout_ms / 1000.0

    while time.time() < deadline:
        sel = _find_visible(page, PAGINATION_SELECTORS)
        if sel:
            return sel
        page.wait_for_timeout(250)

    return None


def _wait_table(page):
    """
    Ждёт появление строк таблицы.
    """
    page.wait_for_selector("tbody tr", timeout=25_000)

    try:
        page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:
        pass


def _scroll_to_bottom(page):
    """
    Скроллит страницу вниз, чтобы триггернуть возможную ленивую подгрузку строк.
    """
    prev = 0

    for _ in range(5):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)

        cur = page.evaluate("document.querySelectorAll('tbody tr').length")
        if cur == prev:
            break

        prev = cur

    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(400)


def _try_set_max_rows(page) -> bool:
    """
    Если на странице есть селектор количества строк, пробуем поставить максимум.
    Это полезно, если сайт хранит настройку page size в UI/localStorage.
    """
    selectors = [
        ".dataTables_length select",
        "select[name*='length' i]",
        "select[name*='per_page' i]",
        "select[name*='page_size' i]",
        "select[aria-label*='rows' i]",
        "[class*='rows' i] select",
    ]

    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if not el or not el.is_visible():
                continue

            values = page.evaluate(
                "(el) => Array.from(el.options).map(o => o.value).concat(Array.from(el.options).map(o => o.text))",
                el,
            )

            for val in ["100", "200", "500", "1000", "all", "All", "ALL", "-1"]:
                if val in values:
                    try:
                        page.select_option(sel, val)
                    except Exception:
                        page.evaluate(
                            """
                            ([sel, val]) => {
                                const el = document.querySelector(sel);
                                if (!el) return;

                                const opt = Array.from(el.options).find(o => o.value === val || o.text === val);
                                if (!opt) return;

                                el.value = opt.value;
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                            }
                            """,
                            [sel, val],
                        )

                    page.wait_for_timeout(1500)
                    _wait_table(page)
                    log.info(f"Установлен размер таблицы: {val}")
                    return True

        except Exception:
            continue

    return False


def _find_next_selector(page):
    """
    Ищет активную кнопку/ссылку на следующую страницу.
    """
    for sel in NEXT_SELECTORS:
        try:
            el = page.query_selector(sel)
            if not el or not el.is_visible():
                continue

            if el.evaluate("el => el.disabled === true"):
                continue

            cls = (el.get_attribute("class") or "").lower()
            aria = (el.get_attribute("aria-disabled") or "").lower()

            if "disabled" in cls or aria == "true":
                continue

            parent_cls = el.evaluate("el => el.closest('li')?.className || el.parentElement?.className || ''")
            if "disabled" in parent_cls.lower():
                continue

            return sel

        except Exception:
            continue

    return None


def _page_signature(page) -> str:
    """
    Сигнатура текущей страницы.
    Используется, чтобы понять, что клик по Next реально переключил страницу.
    """
    return page.evaluate(
        """
        () => {
            const rows = Array.from(document.querySelectorAll('tbody tr')).slice(0, 10).map(tr => tr.innerText);
            const active = document.querySelector('.active.page-item, .paginate_button.current, [aria-current="page"], .pagination .active, .current');

            return JSON.stringify({
                href: location.href,
                rowCount: document.querySelectorAll('tbody tr').length,
                active: active ? active.innerText : null,
                rows: rows
            });
        }
        """
    )


def _setup_browser_context(p):
    """
    Создаёт browser context.
    Если есть storage_state.json, использует его.
    """
    browser = p.chromium.launch(headless=HEADLESS)

    ctx_kwargs = dict(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
    )

    if STORAGE_STATE_FILE.exists():
        ctx_kwargs["storage_state"] = str(STORAGE_STATE_FILE)
        log.info("Использую storage_state.json")

    ctx = browser.new_context(**ctx_kwargs)

    if COINALYZE_P_SID or COINALYZE_CHAT_SID:
        cookies = []

        if COINALYZE_P_SID:
            cookies.append(
                {
                    "name": "p_sid",
                    "value": COINALYZE_P_SID,
                    "domain": "coinalyze.net",
                    "path": "/",
                    "secure": True,
                }
            )

        if COINALYZE_CHAT_SID:
            cookies.append(
                {
                    "name": "chat_sid",
                    "value": COINALYZE_CHAT_SID,
                    "domain": "coinalyze.net",
                    "path": "/",
                    "secure": True,
                }
            )

        cookies.append(
            {
                "name": "cookies_accepted",
                "value": "1",
                "domain": "coinalyze.net",
                "path": "/",
                "secure": True,
            }
        )

        ctx.add_cookies(cookies)

    page = ctx.new_page()
    stealth_sync(page)

    return browser, page


def _load_first_page(page) -> str:
    """
    Загружает первую страницу и возвращает HTML.
    """
    page.goto(COINALYZE_URL, wait_until="domcontentloaded", timeout=50_000)
    page.wait_for_timeout(4000)

    if "Attention Required" in page.content():
        log.warning("Cloudflare, waiting...")
        page.wait_for_timeout(10_000)

    _wait_table(page)
    _try_set_max_rows(page)

    pag_sel = _wait_pagination(page, 8000)
    _scroll_to_bottom(page)

    row_count = page.evaluate("document.querySelectorAll('tbody tr').length")
    pag_txt = f"есть ({pag_sel})" if pag_sel else "нет"

    log.info(f"Строк в таблице: {row_count} · пагинация: {pag_txt}")

    return page.content()


def _goto_next_page(page) -> bool:
    """
    Кликает Next и ждёт, что страница реально изменилась.
    """
    next_sel = _find_next_selector(page)
    if not next_sel:
        return False

    old_sig = _page_signature(page)

    try:
        page.click(next_sel, timeout=15_000)
    except Exception as e:
        log.warning(f"page.click({next_sel}) не сработал: {e}, пробую JS")
        try:
            page.evaluate("(sel) => document.querySelector(sel)?.click()", next_sel)
        except Exception as e2:
            log.error(f"JS click failed: {e2}")
            return False

    try:
        page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass

    changed = False

    for _ in range(30):
        try:
            if _page_signature(page) != old_sig:
                changed = True
                break
        except Exception:
            pass

        page.wait_for_timeout(500)

    if not changed:
        log.warning("Клик Next выполнен, но страница не изменилась")
        return False

    _wait_table(page)
    _scroll_to_bottom(page)

    return True


def fetch_data() -> list[dict]:
    """
    Главная функция для monitor.py.
    Возвращает список всех монет со страниц пагинации.
    """
    all_rows = []
    seen_symbols = set()

    with sync_playwright() as p:
        browser, page = _setup_browser_context(p)

        try:
            html_text = _load_first_page(page)
            DEBUG_HTML_FILE.write_text(html_text, encoding="utf-8")

            rows = parse_table(html_text)
            all_rows.extend(rows)

            for r in rows:
                sym = r.get("symbol")
                if sym:
                    seen_symbols.add(sym)

            log.info(f"Страница 1: {len(rows)} монет")

            for page_no in range(2, MAX_PAGES + 1):
                if not _goto_next_page(page):
                    pag_sel = _find_visible(page, PAGINATION_SELECTORS)

                    if pag_sel:
                        outer = page.evaluate(
                            "(sel) => document.querySelector(sel)?.outerHTML || ''",
                            pag_sel,
                        )
                        log.warning(
                            f"Пагинация найдена ({pag_sel}), но Next не найден/не активен. "
                            f"HTML: {outer[:1000]}"
                        )
                    else:
                        log.info("Next не найден — пагинация закончилась")

                    break

                html_text = page.content()
                rows = parse_table(html_text)

                new_count = 0

                for r in rows:
                    sym = r.get("symbol")
                    if sym and sym not in seen_symbols:
                        all_rows.append(r)
                        seen_symbols.add(sym)
                        new_count += 1

                log.info(f"Страница {page_no}: +{new_count} новых монет")

                if new_count == 0:
                    log.warning("Страница загружена, но новых монет нет — останавливаемся")
                    break

        except Exception as e:
            log.error(f"Загрузка: {e}")

            try:
                page.screenshot(path=str(BASE / "debug_screenshot.png"), full_page=True)
            except Exception:
                pass

        finally:
            browser.close()

    if not all_rows:
        send_tg("⚠️ Monitor: данные не получены. Проверь debug_page.html")
        sys.exit(1)

    log.info(f"Всего монет после пагинации: {len(all_rows)}")
    return all_rows


def make_storage_state():
    """
    Однократная утилита для создания storage_state.json.

    Запуск:
        python -c "from coinalyze_loader import make_storage_state; make_storage_state()"

    Откроется браузер. Нужно залогиниться на coinalyze.net,
    дождаться таблицу с пагинацией, затем нажать Enter в терминале.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        ctx = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )

        page = ctx.new_page()
        page.goto("https://coinalyze.net/")

        input("Залогинься, дождись таблицу с пагинацией, затем нажми Enter здесь...")

        ctx.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

        log.info(f"storage_state сохранён в {STORAGE_STATE_FILE}")


if __name__ == "__main__":
    rows = fetch_data()
    print(f"Loaded rows: {len(rows)}")
