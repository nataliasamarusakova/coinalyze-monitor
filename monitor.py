from curl_cffi import requests as cffi_requests  # вместо обычного requests для этого запроса

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "ru,en;q=0.9",
    "cache-control": "max-age=0",
    "priority": "u=0, i",
    "referer": URL,
    "sec-ch-ua": '"Chromium";v="148", "YaBrowser";v="26.6", "Not/A)Brand";v="99", "Yowser";v="2.5"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/148.0.0.0 YaBrowser/26.6.0.0 Safari/537.36"),
}

def fetch_rows():
    resp = cffi_requests.get(
        URL, headers=HEADERS, cookies=COOKIES,
        impersonate="chrome124",   # имитирует настоящий TLS-отпечаток Chrome
        timeout=20,
    )
    print(f"HTTP статус: {resp.status_code}")
    print(f"Длина ответа: {len(resp.text)} символов")

    if resp.status_code != 200:
        print("Первые 500 символов ответа:", resp.text[:500])
        send_telegram(f"⚠️ Coinalyze monitor: статус {resp.status_code}, доступ заблокирован.")
        raise SystemExit(1)

    soup = BeautifulSoup(resp.text, "lxml")
    rows_found = soup.select("tbody tr")
    if not rows_found:
        print("Строк не найдено, первые 1000 символов:", resp.text[:1000])
        send_telegram("⚠️ Coinalyze monitor: таблица пустая — куки истекли или изменилась разметка.")
        raise SystemExit(1)

    # ... дальше разбор tds как раньше
