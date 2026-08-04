"""
browser.py — модуль браузера (Playwright) для scraping coinalyze.net.
Вынесен из monitor.py для отдельного тестирования.
Запуск: python browser.py
Запуск с видимым окном локально: HEADLESS=false python browser.py
"""
import os
import logging
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

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
log = logging.getLogger("browser")

BASE = Path(__file__).resolve().parent
DEBUG_HTML_FILE = BASE / "debug_page.html"
DEBUG_PAGINATION_FILE = BASE / "debug_pagination.html"

COINALYZE_P_SID = os.environ.get("COINALYZE_P_SID", "")
COINALYZE_CHAT_SID = os.environ.get("COINALYZE_CHAT_SID", "")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"

COINALYZE_URL = (
    "https://coinalyze.net/"
    "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
    "&filter=Y19ndF8yMDAwMDAwJmRfZ3RfMTAwMDAwMCZlX2d0XzAmc19ndF8wJmNtNjE2NV9ndF80NSZjbTYxNjRfbHRfNjA"
    "&order_by=volume_24hour&order_dir=desc"
)


def setup_browser_context(p, headless=True):
    """Создаёт browser context с cookies. Возвращает (browser, page)."""
    browser = p.chromium.launch(headless=headless)
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
    )
    if COINALYZE_P_SID or COINALYZE_CHAT_SID:
        cookies = []
        if COINALYZE_P_SID:
            cookies.append({
                "name": "p_sid", "value": COINALYZE_P_SID,
                "domain": "coinalyze.net", "path": "/", "secure": True,
            })
        if COINALYZE_CHAT_SID:
            cookies.append({
                "name": "chat_sid", "value": COINALYZE_CHAT_SID,
                "domain": "coinalyze.net", "path": "/", "secure": True,
            })
        cookies.append({
            "name": "cookies_accepted", "value": "1",
            "domain": "coinalyze.net", "path": "/", "secure": True,
        })
        ctx.add_cookies(cookies)
    page = ctx.new_page()
    stealth_sync(page)
    return browser, page


def load_page(page, url):
    """Загружает одну страницу и возвращает полный HTML.
    Ждёт таблицу, пагинацию, скроллит для подгрузки всех строк."""
    page.goto(url, wait_until="domcontentloaded", timeout=50_000)
    page.wait_for_timeout(4000)

    if "Attention Required" in page.content():
        log.warning("Cloudflare, waiting...")
        page.wait_for_timeout(10_000)

    page.wait_for_selector("tbody tr", timeout=25_000)

    try:
        page.wait_for_selector(".pagination", timeout=10_000)
    except Exception:
        log.warning("Блок .pagination не найден за 10с — работаем с одной страницей")

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

    row_count = len(page.query_selector_all("tbody tr"))
    has_pagination = page.query_selector(".pagination") is not None
    log.info(f"Строк в таблице: {row_count} · пагинация: {'есть' if has_pagination else 'нет'}")

    return page.content()


def dump_pagination_debug(html_text):
    """Сохраняет содержимое .pagination отдельно + считает <a>/<button>,
    чтобы понять реальную структуру виджета пагинации."""
    soup = BeautifulSoup(html_text, "lxml")
    pag = soup.select_one(".pagination")
    if pag:
        DEBUG_PAGINATION_FILE.write_text(pag.prettify(), encoding="utf-8")
        links = pag.select("a[href]")
        buttons = pag.select("button")
        lis = pag.select("li")
        log.info(
            f"Блок .pagination сохранён в {DEBUG_PAGINATION_FILE.name} "
            f"({len(str(pag))} симв.) | a[href]={len(links)} button={len(buttons)} li={len(lis)}"
        )
    else:
        log.warning("Блок .pagination не найден в HTML вообще")


def get_page_urls(html_text):
    """Парсит блок .pagination и возвращает список URL всех страниц.
    Если пагинации нет (или это JS-виджет без <a href>) — вернёт только базовый URL."""
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


def click_next_page(page, current_page_num):
    """
    Фолбэк на случай, если пагинация — JS-виджет без прямых ссылок:
    кликаем по нужному номеру / кнопке 'Next' внутри .pagination
    и ждём, пока изменится первая строка таблицы (data-coin).
    """
    pag = page.query_selector(".pagination")
    if not pag:
        return False

    first_row = page.query_selector("tbody tr")
    before = first_row.get_attribute("data-coin") if first_row else None

    target = None
    for el in pag.query_selector_all("a, button, li"):
        txt = (el.inner_text() or "").strip()
        if txt == str(current_page_num + 1):
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


def fetch_all_pages_via_click(page, max_pages=MAX_PAGES):
    """Собирает HTML всех страниц через клики, без предположения о <a href>."""
    htmls = [page.content()]
    for i in range(1, max_pages):
        ok = click_next_page(page, i)
        if not ok:
            log.info(f"Клик на страницу {i + 1} не удался/не нужен — останавливаемся")
            break
        page.wait_for_selector("tbody tr", timeout=15_000)
        htmls.append(page.content())
        log.info(f"Страница {i + 1} получена через клик")
    return htmls


def main():
    with sync_playwright() as p:
        browser, page = setup_browser_context(p, headless=HEADLESS)
        try:
            html_text = load_page(page, COINALYZE_URL)
            DEBUG_HTML_FILE.write_text(html_text, encoding="utf-8")
            dump_pagination_debug(html_text)

            urls = get_page_urls(html_text)
            log.info(f"get_page_urls() (парсинг href) нашёл {len(urls)} URL: {urls}")

            log.info("Собираем страницы через клики по .pagination...")
            htmls = fetch_all_pages_via_click(page, MAX_PAGES)
            log.info(f"Через клики получено {len(htmls)} страниц(ы)")

            total_rows = 0
            seen_symbols = set()
            for idx, h in enumerate(htmls, start=1):
                soup = BeautifulSoup(h, "lxml")
                rows = soup.select("tbody tr")
                page_symbols = {tr.get("data-coin") for tr in rows if tr.get("data-coin")}
                new_symbols = page_symbols - seen_symbols
                seen_symbols |= page_symbols
                total_rows += len(new_symbols)
                log.info(f"  Страница {idx}: {len(rows)} строк, новых уникальных монет: {len(new_symbols)}")

            log.info(f"ИТОГО уникальных монет собрано через клики: {total_rows}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
