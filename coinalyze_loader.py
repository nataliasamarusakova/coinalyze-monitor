"""
coinalyze_loader.py — загрузчик с Coinalyze.
Данные приходят одним запросом assets-info/ (JSON/JS), перехватываем его через Playwright,
а не парсим HTML-таблицу со скроллом.
"""
from __future__ import annotations

import os
import re
import json
import time
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

# паттерн, по которому ловим нужный сетевой ответ
ASSETS_INFO_MATCH = os.environ.get("ASSETS_INFO_MATCH", "assets-info")

# сколько ждать ответ assets-info (с учётом возможного Cloudflare-челленджа)
ASSETS_INFO_TIMEOUT_MS = int(os.environ.get("ASSETS_INFO_TIMEOUT_MS", "45000"))

# размер куска при логировании тела ответа
LOG_CHUNK_SIZE = int(os.environ.get("LOG_CHUNK_SIZE", "4000"))

# ограничение на общий объём логируемого текста (0 = без ограничений, логируем всё)
MAX_LOG_CHARS = int(os.environ.get("MAX_LOG_CHARS", "0"))

COINALYZE_URL = os.environ.get(
    "COINALYZE_URL",
    "https://coinalyze.net/"
    "?columns=YSZiJm4mYyZkJmUmZiZzJnQmaCZyJmkmaiZwJnEmbCZtJjYmdiZjbTYxNjUmY202MTY0"
    "&filter=Y19ndF8yMDAwMDAwJmRfZ3RfMTAwMDAwMCZlX2d0XzAmc19ndF8wJmNtNjE2NV9ndF80NSZjbTYxNjRfbHRfNjA"
    "&order_by=volume_24hour&order_dir=desc",
)


# ─────────────────────────── логирование сырых данных ───────────────────────────
def log_full_body(label: str, body: str, chunk_size: int = LOG_CHUNK_SIZE, max_chars: int = MAX_LOG_CHARS):
    """Льёт содержимое ответа прямо в лог кусками, чтобы сразу видеть его в консоли."""
    text = body
    total_len = len(text)
    if max_chars and total_len > max_chars:
        text = text[:max_chars]
        log.warning(f"⚠️ [{label}] Тело обрезано для лога до {max_chars} из {total_len} символов "
                    f"(измени MAX_LOG_CHARS=0, чтобы логировать всё)")

    n_chunks = (len(text) + chunk_size - 1) // chunk_size or 1
    log.info(f"📦 [{label}] Начинаю вывод тела ответа: всего_символов={total_len}, кусков={n_chunks}, "
              f"chunk_size={chunk_size}")
    for i in range(0, len(text), chunk_size):
        idx = i // chunk_size + 1
        chunk = text[i:i + chunk_size]
        log.info(f"📦 [{label}] chunk {idx}/{n_chunks}:\n{chunk}")
    log.info(f"📦 [{label}] === КОНЕЦ ТЕЛА ОТВЕТА ===")


def try_parse_payload(body: str) -> Optional[Any]:
    """Пробуем распарсить тело как чистый JSON, иначе вытащить JSON-подстроку из JS-обёртки."""
    body_stripped = body.strip()

    # 1) чистый JSON
    try:
        data = json.loads(body_stripped)
        log.info(f"✅ [PARSE] Тело — валидный JSON. Тип: {type(data).__name__}")
        return data
    except json.JSONDecodeError as e:
        log.info(f"ℹ️ [PARSE] Прямой json.loads не сработал ({e}). Пробуем вытащить JSON из обёртки...")

    # 2) JS-обёртка вида "var x = {...}" / "callback({...})" — ищем самый длинный {..} или [..]
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


def describe_payload(data: Any):
    """Логируем структуру распарсенных данных, чтобы понять форму (список? словарь? ключи?)."""
    if isinstance(data, list):
        log.info(f"🔎 [STRUCT] Это list длиной {len(data)}")
        if data:
            first = data[0]
            log.info(f"🔎 [STRUCT] Тип первого элемента: {type(first).__name__}")
            if isinstance(first, dict):
                log.info(f"🔎 [STRUCT] Ключи первого элемента: {list(first.keys())}")
                log.info(f"🔎 [STRUCT] Пример первого элемента: {json.dumps(first, ensure_ascii=False)[:1000]}")
    elif isinstance(data, dict):
        log.info(f"🔎 [STRUCT] Это dict с ключами верхнего уровня: {list(data.keys())}")
        for k, v in data.items():
            if isinstance(v, list):
                log.info(f"🔎 [STRUCT]   ключ '{k}' -> list длиной {len(v)}")
                if v and isinstance(v[0], dict):
                    log.info(f"🔎 [STRUCT]     пример элемента: {json.dumps(v[0], ensure_ascii=False)[:1000]}")
            else:
                log.info(f"🔎 [STRUCT]   ключ '{k}' -> {type(v).__name__}")
    else:
        log.info(f"🔎 [STRUCT] Неожиданный тип верхнего уровня: {type(data).__name__}")


# ─────────────────────────── парсер чисел (оставлен для fallback HTML-парсинга) ───────────────────────────
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


# ─────────────────────────── скрапер ───────────────────────────
class CoinalyzeScraper:
    def __init__(self, headless: bool = HEADLESS):
        self.headless = headless
        self._pw = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._captured: list[dict] = []  # сюда складываем все пойманные assets-info ответы

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

        # регистрируем слушатель ДО любых переходов, чтобы не пропустить ранний запрос
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
        """Ловим все ответы, но интересует только assets-info."""
        if ASSETS_INFO_MATCH not in response.url:
            return
        try:
            status = response.status
            headers = response.headers
            content_type = headers.get("content-type", "?")
            body = response.text()
        except Exception as e:
            log.warning(f"⚠️ [CAPTURE] Не удалось прочитать тело ответа {response.url}: {e}")
            return

        log.info("==================================================")
        log.info(f"🎯 [CAPTURE] Поймали assets-info ответ!")
        log.info(f"🎯 [CAPTURE] URL: {response.url}")
        log.info(f"🎯 [CAPTURE] Status: {status}")
        log.info(f"🎯 [CAPTURE] Content-Type: {content_type}")
        log.info(f"🎯 [CAPTURE] Длина тела: {len(body)} символов")
        log.info("==================================================")

        self._captured.append({
            "url": response.url,
            "status": status,
            "content_type": content_type,
            "body": body,
        })

    def _wait_cloudflare(self):
        page = self._page
        try:
            content = page.content()
        except Exception:
            content = ""
        if any(marker in content for marker in ("Attention Required", "Just a moment", "cf-browser-verification")):
            log.warning("⚠️ Обнаружен Cloudflare челлендж, ждём подольше (реальный браузер должен пройти сам)...")
            page.wait_for_timeout(10_000)

    def fetch_assets_info(self, url: str = COINALYZE_URL) -> list[dict]:
        """Главный метод: открываем страницу и ждём именно ответ assets-info/."""
        page = self._page
        self._captured.clear()

        log.info(f"🌐 [PLAYWRIGHT] Открываем URL: {url}")
        log.info(f"⏳ [PLAYWRIGHT] Ждём ответ, содержащий '{ASSETS_INFO_MATCH}', таймаут={ASSETS_INFO_TIMEOUT_MS}ms")

        try:
            with page.expect_response(
                lambda r: ASSETS_INFO_MATCH in r.url,
                timeout=ASSETS_INFO_TIMEOUT_MS,
            ):
                page.goto(url, wait_until="domcontentloaded", timeout=40_000)
                self._wait_cloudflare()
                # даём странице время дозагрузить скрипты/сделать сам fetch, если ещё не сделала
                page.wait_for_timeout(3_000)
        except Exception as e:
            log.warning(f"⚠️ [PLAYWRIGHT] Не дождались assets-info через expect_response ({e}). "
                        f"Проверяем, может он всё же был пойман listener'ом асинхронно...")

        if not self._captured:
            log.error("🛑 [CAPTURE] Ни одного ответа assets-info поймать не удалось. "
                      "Возможные причины: изменился URL эндпоинта, Cloudflare заблокировал сессию, "
                      "нужны доп. куки (например cf_clearance).")
            return []

        results = []
        for i, item in enumerate(self._captured, start=1):
            label = f"ASSETS-INFO #{i}"
            log_full_body(label, item["body"])
            data = try_parse_payload(item["body"])
            if data is not None:
                describe_payload(data)
                results.append({"meta": item, "data": data})
            else:
                log.warning(f"⚠️ [{label}] Данные остались нераспарсенными, только сырой текст в логе выше.")

        return results


def main():
    log.info(f"🚀 Запуск скрипта в {'HEADLESS' if HEADLESS else 'GUI'} режиме")
    with CoinalyzeScraper() as scraper:
        results = scraper.fetch_assets_info()

    log.info(f"🎉 Поймано и обработано ответов assets-info: {len(results)}")

    if results:
        out_file = BASE / "assets_info_raw.json"
        out_file.write_text(
            json.dumps([r["data"] for r in results], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info(f"💾 Распарсенные данные (для дальнейшего анализа структуры) сохранены в {out_file.name}")
    else:
        log.error("❌ Данных не получено, смотри лог выше — там весь сырой ответ или причина отказа.")


if __name__ == "__main__":
    main()
