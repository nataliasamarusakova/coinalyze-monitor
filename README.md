# Crypto Momentum Monitor & BingX Trading Bot

Система мониторинга криптовалютного рынка с автоматическим исполнением сделок на бирже BingX (демо-счёт VST).

## 📋 Описание

Проект представляет собой торговую систему, которая:
- Мониторит криптовалюты из watchlist в реальном времени
- Анализирует рыночные данные (OI, CVD, Funding Rate, Open Interest)
- Выявляет паттерны momentum (Short Squeeze, Long Squeeze и др.)
- Генерирует торговые сигналы на основе подтверждённых трендов
- Автоматически исполняет сделки через BingX API
- Ведёт подробный лог всех событий и сделок

## 🏗️ Архитектура

### Основные компоненты

| Файл | Назначение |
|------|-----------|
| `monitor.py` | Главный процесс мониторинга: скрапинг данных, анализ условий, управление lifecycle сигналов |
| `bingx_client.py` | Клиент для работы с BingX API: исполнение ордеров, управление позициями |
| `conditions.py` | Предикаты и условия для определения торговых сигналов (Path A, Path B, Accumulation) |
| `make_dashboard.py` | Генерация HTML-дашборда для визуализации состояния системы |
| `trades_report.py` | Отчёты по торговым результатам |
| `unentered_tracker.py` | Трекер невошедших сделок для анализа |

### Структура данных

- **market_history.jsonl** — история рыночных данных
- **snapshots.jsonl** — снимки состояний инструментов
- **heartbeat.jsonl** — логи heartbeat монитора
- **watchlist.json** — текущий статус инструментов в вотчлисте
- **trades.jsonl** — история сделок
- **pending_trades.jsonl** — ожидающие исполнения сделки
- **bingx_orders.jsonl** — логи ордеров BingX
- **execution_events.jsonl** — события исполнения
- **lifecycle_state.json** — текущее состояние lifecycle движка
- **shadow_signals.jsonl** — теневые сигналы для тестирования стратегий
- **calibration.jsonl** — данные калибровки модели

## ⚙️ Установка

### Требования

- Python 3.8+
- Playwright (для браузерного скрапинга)

### Шаг 1: Установка зависимостей

```bash
pip install -r requirements.txt
playwright install
```

### Шаг 2: Настройка переменных окружения

Создайте файл `.env` или экспортируйте переменные:

```bash
# BingX API (демо-счёт VST)
export BINGX_API_KEY="your_api_key"
export BINGX_SECRET_KEY="your_secret_key"
export BINGX_BASE_URL="https://open-api-vst.bingx.com"  # демо
export BINGX_MARGIN_USDT="1"        # маржа в USDT
export BINGX_LEVERAGE="10"          # плечо
export BINGX_MAX_LEVERAGE="50"      # макс. плечо

# CoinAllyze (источник рыночных данных)
export COINALYZE_P_SID="your_session_id"
export COINALYZE_CHAT_SID="your_chat_sid"
export COINALYZE_URL="https://coinalyze.net"

# Telegram уведомления (опционально)
export TG_BOT_TOKEN="your_bot_token"
export TG_CHAT_ID="your_chat_id"

# LLM интеграция (опционально)
export ENABLE_LLM="false"
export QWEN_API_KEY="your_qwen_key"
export QWEN_BASE_URL="https://dashscope.aliyuncs.com"
export QWEN_MODEL="qwen-plus"

# Настройки системы
export ENABLE_BINGX="false"         # включить исполнение ордеров
export ALLOW_NO_STEALTH="false"     # разрешить запуск без stealth
```

## 🚀 Запуск

### Основной монитор

```bash
python monitor.py
```

Монитор работает в цикле:
1. Скрапит данные с CoinAllyze
2. Проверяет условия входа (Path A, Path B, Accumulation)
3. Управляет lifecycle сигналов
4. Отправляет сигналы в BingX (если включено)
5. Генерирует дашборд

### Дашборд

Дашборд генерируется автоматически в `docs/index.html`. Откройте файл в браузере для просмотра текущего состояния.

### Отчёты по сделкам

```bash
python trades_report.py
```

## 📊 Стратегия

### Условия входа

Система использует три основных паттерна:

1. **Path A (Classic)** — подтверждённый тренд с ростом OI и CVD
2. **Path B (Early)** — ранний вход по накоплению
3. **Accumulation** — фаза накопления перед движением

### Lifecycle сигналов

```
NEUTRAL → EARLY_MOVE → CONFIRMED_TREND → POSSIBLE_ENTRY → ENTRY
                                ↓
                        SIGNAL_DECAY / EXHAUSTION / DISTRIBUTION
```

### Управление рисками

- Адаптивный стоп-лосс на основе волатильности
- Тейк-профит в 3 уровня (tp1, tp2, tp3)
- Максимальное плечо ограничено настройкой
- Гейт плотности окна данных (минимум 5 снимков за 30 мин)

## 🔧 Конфигурация

### Параметры условий (`conditions.py`)

- `k-of-n` логика вместо `all()` для устойчивости к шуму
- Непрерывная сила сигнала для триггера Шмитта
- Трёхзначная логика: `true` / `false` / `unknown`

### Версии движков

```python
ENGINE_VERSIONS = {
    "schema": 4,
    "signal": 3,
    "lifecycle": 4,
    "protection": 2,
}
```

## 📈 Логирование

Все события пишутся в JSONL-файлы:

- **execution_events.jsonl** — события исполнения ордеров
- **reconciliation.jsonl** — сверка позиций
- **discovery_history.jsonl** — история обнаружения паттернов
- **unentered_analysis.jsonl** — анализ невошедших сделок

## 🛡️ Безопасность

⚠️ **Внимание**: Проект работает с демо-счётом BingX (VST). Для реальной торговли:

1. Внимательно проверьте код
2. Протестируйте на демо
3. Используйте минимальные суммы
4. Настройте лимиты потерь

## 📝 Лицензия

Проект предоставлен "как есть" для образовательных целей.

## 🤝 Поддержка

Для вопросов создавайте issue в репозитории.

---

**Версия**: 2026-08-17  
**Последнее обновление**: Август 2025
