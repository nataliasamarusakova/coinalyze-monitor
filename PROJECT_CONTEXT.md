# 📂 SYSTEM CONTEXT: Coinalyze Monitor — Trading Signal Research Engine

## 1. 🎯 Executive Summary (Резюме)
- **Цель проекта:** Автоматизированный мониторинг криптовалютного рынка через Coinalyze.net, детекция торговых сигналов на основе многофакторного анализа (OI, CVD, Funding, Price Momentum), ведение журнала сделок и исследование эффективности сигналов.
- **Решаемая проблема:** Ручной анализ рыночных данных трудоёмок и субъективен. Проект автоматизирует сбор данных, классификацию состояний рынка, entry/exit логику и пост-анализ результатов.
- **Ключевые фичи и функционал:**
  - Парсинг Coinalyze.net с обходом Cloudflare (Playwright + stealth)
  - Lifecycle-движок состояний: ACCUMULATION → EARLY_MOVE → CONFIRMED_TREND → ACCELERATION → EXHAUSTION → DISTRIBUTION
  - Два пути входа: classic (Path A/B) и early
  - Автосохранение сделок в trades.jsonl с полным аудитом state_history
  - Трекер упущенных возможностей (unentered_tracker.py)
  - HTML-дашборд с метриками win-rate, PnL, capture rate
  - GitHub Actions CI для запуска по расписанию/диспатчу

## 2. 🛠 Tech Stack (Технический стек)
- **Языки и версии:** Python 3.11
- **Фреймворки и библиотеки:**
  - `playwright` + `playwright-stealth==1.0.6` — браузерная автоматизация
  - `beautifulsoup4` + `lxml` — HTML-парсинг
  - `requests` — HTTP-запросы
- **Базы данных:** Нет (файловые JSONL-логи: trades.jsonl, market_history.jsonl, snapshots.jsonl)
- **Инфраструктура и инструменты:**
  - GitHub Actions (cron/dispatch)
  - Telegram Bot API (уведомления)
  - [Предположение] Qwen LLM API (опционально, через ENABLE_LLM)

## 3. 🏗 Architecture & Structure (Архитектура)
- **Архитектурный паттерн:** Event-driven batch processing pipeline
- **Точки входа (Entry points):**
  - `monitor.py` — главный пайплайн (fetch → parse → classify → trade lifecycle)
  - `unentered_tracker.py` — анализ пропущенных сигналов
  - `trades_report.py` — исследовательская статистика
  - `make_dashboard.py` — генерация docs/index.html
  - `conditions.py` — предикаты условий входа/выхода
- **Описание структуры папок и файлов:**
```
/workspace/
├── monitor.py              # Главный движок: парсинг + lifecycle + trade execution
├── conditions.py           # Предикаты: check_confirmed_path_a/b, check_early_move, check_accumulation
├── unentered_tracker.py    # Детектор упущенных движений
├── trades_report.py        # Анализ trades.jsonl (win-rate, slicing by momentum/cvd)
├── make_dashboard.py       # Генератор HTML-дашборда
├── requirements.txt        # Зависимости
├── watchlist.json          # Текущие open-позиции (state, score, pattern)
├── lifecycle_state.json    # Cooldown/tracking по символам
├── trades.jsonl            # Журнал закрытых сделок (schema v2)
├── market_history.jsonl    # Исторические снимки рынка (TTL=2 дня)
├── snapshots.jsonl         # Снимки для отладки (TTL=7 дней)
├── heartbeat.jsonl         # Heartbeat-лог (TTL=3 дня)
├── calibration.jsonl       # LLM calibration logs
├── unentered_candidates.jsonl  # Кандидаты на упущенные сделки
├── unentered_analysis.jsonl    # Финализированный анализ упущенных
├── docs/index.html         # Сгенерированный дашборд
└── .github/workflows/
    ├── monitor.yml         # CI: запуск monitor + unentered + dashboard
    ├── trades_report.yml   # Manual: аналитический отчёт
    └── unentered_tracker.yml # Manual: запуск трекера
```
- **Схема потока данных (Data Flow):**
```
Coinalyze.net → Playwright (Chromium) → HTML → BeautifulSoup → market_history.jsonl
                                               ↓
                                    Lifecycle Engine (conditions.py)
                                               ↓
                          ┌────────────────────┴────────────────────┐
                          ↓                                         ↓
                   Entry Signal                              Exit Signal
                          ↓                                         ↓
                   watchlist.json (open_trade)            trades.jsonl (closed)
                          ↓                                         ↓
                   make_dashboard.py ←───────────────────── trades_report.py
                          ↓
                   docs/index.html
```

## 4. 🧠 Core Logic & Modules (Бизнес-логика)

### monitor.py — Lifecycle Engine (v2)
- **Назначение:** Основной пайплайн сбора данных и управления сделками
- **Ключевые функции:**
  - `fetch_data()` — загрузка всех страниц пагинации Coinalyze с debounce
  - `parse_table()` — извлечение 23+ метрик на монету (price, OI, CVD, funding, liq)
  - `classify_asset_class()` — классификация: crypto/equity/commodity
  - `_run_lifecycle_engine()` — переходы между состояниями, entry/exit логика
  - `_check_exit_conditions()` — приоритизация выходов по EXIT_PRIORITY
  - `send_tg()` — уведомления в Telegram
- **Вход:** COINALYZE_P_SID, COINALYZE_CHAT_SID cookies
- **Выход:** watchlist.json, trades.jsonl, lifecycle_state.json

### conditions.py — Signal Predicates
- **Назначение:** Детерминированные предикаты для проверки условий входа
- **Ключевые предикаты:**
  - `check_confirmed_path_a(snaps)` — строгий путь: 5 снимков, OI>5%, CVD>55, LLS<40, FR<0.05
  - `check_confirmed_path_b(snaps, cvd_momentum)` — альтернативный путь с momentum
  - `check_early_move(snaps)` — ранний вход: 3 снимка роста price/OI/CVD/vol
  - `check_accumulation(snaps)` — фаза накопления: OI4h>0, CVD_avg>50, price_chg<5%
  - `closest_miss_for_confirmed()` — диагностика ближайшего невыполненного условия
- **Константы:** CONFIRMED_A_SNAPS=5, TRADE_WIN_PCT=1.0, SIGNAL_DECAY_MIN=90

### unentered_tracker.py — Missed Opportunity Detector
- **Назначение:** Выявление символов с ростом >5% за 24ч, которые не были взяты в сделку
- **Логика:**
  1. Фильтрация активных символов (watchlist + trades за 24ч)
  2. Детекция кандидатов: price_chg24 > MISSED_THRESHOLD_PCT (5.0)
  3. Финализация через FORWARD_HORIZONS=[60,120] минут
  4. Классификация качества: good/noise/late/undetermined
  5. Определение fail_point (stage:condition:deficit)
- **Выход:** unentered_analysis.jsonl для trades_report.py

### trades_report.py — Research Analytics
- **Назначение:** Статистический анализ trades.jsonl
- **Метрики:**
  - Win-rate @60m/@120m по уровням [0.0, 0.5, 1.0, 2.0]%
  - Slicing по entry_momentum, cvd_momentum, price_chg24, path, pattern
  - Coverage analysis (горизонты, причина финализации pending)
  - Stale exit detection (exit_price_source=last_seen)
  - False negative analysis по упущенным сделкам
- **LOW_SAMPLE_WARNING=20** — эвристика для малых выборок

### make_dashboard.py — Dashboard Generator
- **Назначение:** Генерация single-page HTML-дашборда
- **Компоненты:**
  - Live positions grid (из watchlist.json)
  - KPI bento (total trades, win-rate, cumulative PnL)
  - Daily stats grid + table
  - Capture rate section (caught vs missed good longs)
  - Charts: momentum vs win-rate, cvd_momentum scatter, equity curve
  - Top signals tables (best/worst signal & strategy, divergence)
  - Sortable trades archive table
- **Стили:** Dark theme, Space Grotesk/IBM Plex шрифты, Chart.js

## 5. 🗄 Data Models & State (Модели данных)

### Основные сущности и связи
```
Symbol → Lifecycle State → Trade → Exit Reason → Return@Horizon
   ↓           ↓               ↓          ↓              ↓
watchlist  lifecycle_state  trades.jsonl  EXIT_CLASS  return_60m/120m/240m
```

### Структуры данных / Схемы БД
**trades.jsonl (schema_version=2):**
```json
{
  "schema_version": 2,
  "trade_id": "SYMBOL_ENTRYTS",
  "symbol": "...",
  "asset_class": "crypto|equity|commodity",
  "entry_ts": 1234567890,
  "entry_price": 100.0,
  "exit_ts": 1234590000,
  "exit_price": 102.5,
  "exit_reason": "SIGNAL_DECAY|EXHAUSTION|STOP_LOSS|TIMEOUT|MISSED|NEUTRAL|INVALIDATED|DISTRIBUTION",
  "exit_class": "SIGNAL|PROTECTION|LIFETIME|DATA",
  "hold_min": 180.1,
  "entry_state": "CONFIRMED_TREND",
  "entry_path": "classic|early",
  "strategy_pnl_pct": 2.5,
  "return_60m": 1.2,
  "return_120m": 2.1,
  "return_240m": 3.5,
  "state_history": [{"ts": ..., "state": "...", "score": N, "reason": "..."}],
  "engine_versions": {"schema": 2, "signal": 1, "lifecycle": 2, "created": "2026-08-04"}
}
```

**watchlist.json:**
```json
{
  "SYMBOL": {
    "state": "CONFIRMED_TREND",
    "score": 9,
    "momentum": 7,
    "pattern": "Healthy Trend",
    "open_trade": {
      "entry_ts": 1234567890,
      "entry_price": 100.0,
      "last_price": 102.0,
      "max_pnl_pct": 2.0,
      "state_history": [...],
      "return_60m_available": false
    }
  }
}
```

**market_history.jsonl (snapshot):**
```json
{
  "ts": 1234567890,
  "symbol": "...",
  "price": 100.0,
  "price_chg24": 3.5,
  "oi_chg24_pct": 15.2,
  "oi_chg4h_pct": 5.1,
  "cvd24": 75.0,
  "lls24": 25.0,
  "fr_oiw": 0.005,
  "lifecycle_state": "CONFIRMED_TREND"
}
```

### Управление состоянием
- **STATE_RANK:** ACCUMULATION(1) < EARLY_MOVE(2) < CONFIRMED_TREND(3) < ACCELERATION(4) < EXHAUSTION(5) < DISTRIBUTION(6)
- **EXIT_PRIORITY:** INVALIDATED > EXHAUSTION > DISTRIBUTION > STOP_LOSS > SIGNAL_DECAY > TIMEOUT > MISSED > NEUTRAL
- **Cooldown:** COOLDOWN_BY_EXIT_REASON (STOP_LOSS=120мин, INVALIDATED=60мин, etc.)
- **Hysteresis:** NEUTRAL_HYSTERESIS=2, MISS_EXIT_RUNS=2, MISS_REMOVE_RUNS=4

## 6. 🔌 API & Interfaces (Интерфейсы)

### Ключевые эндпоинты / методы взаимодействия
- **Coinalyze.net:** HTTPS GET с cookies (p_sid, chat_sid)
- **Telegram Bot API:** POST /bot<TOKEN>/sendMessage для уведомлений
- **[Предположение] Qwen API:** ENABLE_LLM=true → QWEN_BASE_URL/dashscope-intl.aliyuncs.com

### Аутентификация и авторизация
- **Coinalyze:** Cookies через env vars (COINALYZE_P_SID, COINALYZE_CHAT_SID)
- **Telegram:** TG_BOT_TOKEN + TG_CHAT_ID
- **GitHub Secrets:** Все токены хранятся в secrets

## 7. ⚙️ Setup & Commands (Запуск и конфигурация)

### Переменные окружения (.env)
```bash
COINALYZE_P_SID=...          # Cookie для доступа к данным
COINALYZE_CHAT_SID=...       # Cookie чата (опционально)
TG_BOT_TOKEN=...             # Telegram bot token
TG_CHAT_ID=...               # Chat ID для уведомлений
ENABLE_LLM=false             # Включить LLM-анализ
QWEN_API_KEY=...             # [Опционально] API ключ Qwen
QWEN_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
QWEN_MODEL=qwen-plus
```

### Команды
```bash
# Установка зависимостей
pip install -r requirements.txt
playwright install chromium

# Запуск мониторинга
python monitor.py

# Трекер упущенных сделок
python unentered_tracker.py

# Аналитический отчёт
python trades_report.py

# Сборка дашборда
python make_dashboard.py

# Syntax check
python -m py_compile monitor.py conditions.py unentered_tracker.py
```

## 8. ⚠️ Tech Debt & Constraints (Ограничения)

### Известные проблемы, баги или узкие места
- **Cloudflare:** Требуется ожидание networkidle + скроллинг, возможен instability
- **Stale prices:** exit_price_source=last_seen при MISSING_PRICE (лаг >15 мин)
- **Low sample warning:** <20 сделок — эвристические выводы ненадёжны
- **Pending trades:** WAIT_TIMEOUT/MISSING_PRICE могут искажать статистику
- **Asset classification:** EQUITY_SYMBOLS/COMMODITY_SYMBOLS захардкожены, требуют обновления

### Важные правила для ИИ
1. **FREEZE версий:** LIFECYCLE_ENGINE_VERSION=2, TRADE_SCHEMA_VERSION=2 — изменения только через новый эксперимент
2. **Crypto-only win-rate:** equity/commodity исключаются из win-rate расчётов
3. **Не модифицировать trades.jsonl напрямую:** Только через monitor.py lifecycle
4. **TTL очистка:** MARKET_HISTORY_TTL=2 дня, SNAPSHOTS_TTL=7 дней — не полагаться на старые данные
5. **EXIT_PRIORITY строго:** При множественных триггерах выбирать первый по списку
6. **HASH_VERSION=sha256_v1:** Для дедупликации snapshot'ов
7. **TRADE_TIMEOUT_MIN=240:** Максимальное время удержания позиции
8. **STOP_MODE=fixed, STOP_LOSS_PCT=5.0:** Жёсткий стоп-лосс
9. **PENDING_GRACE_MIN=10:** Grace period перед финализацией pending
10. **Дашборд генерируется из trades.jsonl + watchlist.json:** Не править docs/index.html вручную

---
**Версия документа:** 1.0  
**Дата актуализации:** 2026-08-04 (по engine_versions.created)  
**Статус:** PRODUCTION (CI/CD настроен, данные накапливаются)
