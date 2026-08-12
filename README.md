# Coinalyze Monitor & Trading System

Система автоматического мониторинга рынка криптовалютных фьючерсов и торговли на бирже **BingX** (USDT-M Perpetual Swap, демо-счёт VST) на основе данных **Coinalyze**.

## 📋 Описание

Проект представляет собой комплексную систему для:

- **Мониторинга рыночных данных** в реальном времени с Coinalyze
- **Анализа рыночных условий** с использованием предикатов lifecycle (Path A, Path B, накопление, раннее движение)
- **Генерации торговых сигналов** на основе многофакторного анализа
- **Автоматической торговли** через BingX API с управлением рисками
- **Отслеживания упущенных возможностей** (unentered trades analysis)
- **Визуализации результатов** через HTML-дашборд

## 🏗️ Архитектура

### Основные компоненты

| Файл | Назначение |
|------|------------|
| `monitor.py` | Главный модуль мониторинга: скрапинг данных, анализ сигналов, управление жизненным цикном сделок |
| `bingx_client.py` | Клиент для работы с BingX API: открытие позиций, установка TP/SL, управление ордерами |
| `conditions.py` | Предикаты условий lifecycle (Path A/B, накопление, сила сигнала, трёхзначная логика) |
| `unentered_tracker.py` | Детектор упущенных торговых возможностей |
| `trades_report.py` | Генерация отчётов по завершённым сделкам (статистика, win rate, PnL) |
| `make_dashboard.py` | Создание интерактивного HTML-дашборда в `docs/index.html` |

### Структура данных

| Файл | Описание |
|------|----------|
| `market_history.jsonl` | История рыночных снимков (price, CVD, momentum, funding) |
| `snapshots.jsonl` | Снимки состояний для анализа условий |
| `trades.jsonl` | Журнал всех совершённых сделок |
| `heartbeat.jsonl` | Логи heartbeat для мониторинга работоспособности |
| `shadow_signals.jsonl` | Теневые сигналы для тестирования гипотез |
| `discovery_history.jsonl` | История обнаружения торговых возможностей |
| `unentered_analysis.jsonl` | Анализ упущенных сделок |
| `unentered_candidates.jsonl` | Кандидаты на упущенные сделки |
| `watchlist.json` | Список отслеживаемых активов |
| `lifecycle_state.json` | Текущее состояние lifecycle системы |

## ⚙️ Установка

### Требования

- Python 3.11+
- Playwright (для браузерной автоматизации)

### Шаг 1: Установка зависимостей

```bash
pip install -r requirements.txt
playwright install
```

### Шаг 2: Настройка переменных окружения

#### Обязательные переменные

```bash
# Coinalyze authentication
export COINALYZE_P_SID="your_p_sid"
export COINALYZE_CHAT_SID="your_chat_sid"

# BingX API credentials (demo account VST)
export BINGX_API_KEY="your_api_key"
export BINGX_SECRET_KEY="your_secret_key"

# Telegram notifications (optional)
export TG_BOT_TOKEN="your_bot_token"
export TG_CHAT_ID="your_chat_id"
```

#### Опциональные переменные

```bash
# BingX настройки
export BINGX_MARGIN_USDT="1"          # Маржа в USDT
export BINGX_LEVERAGE="10"            # Плечо по умолчанию
export BINGX_MAX_LEVERAGE="50"        # Максимальное плечо

# LLM integration (optional)
export ENABLE_LLM="false"
export QWEN_API_KEY="your_qwen_key"
export QWEN_BASE_URL="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
export QWEN_MODEL="qwen-plus"

# Торговые настройки
export MAX_PAGES="5"                  # Максимум страниц для скрапинга
```

## 🚀 Запуск

### Основной мониторинг

```bash
python monitor.py
```

### Генерация отчёта по сделкам

```bash
python trades_report.py
```

### Трекер упущенных возможностей

```bash
python unentered_tracker.py
```

### Создание дашборда

```bash
python make_dashboard.py
```

Дашборд будет создан в `docs/index.html`.

## 🔄 GitHub Actions

Проект включает workflow для автоматизации:

- **monitor.yml** — запуск мониторинга по событию `repository_dispatch`
- **trades_report.yml** — генерация отчёта по сделкам (manual trigger)
- **unentered_tracker.yml** — анализ упущенных возможностей (manual trigger)

## 📊 Стратегия торговли

### Условия входа

Система использует комбинацию предикатов:

1. **Path A** — подтверждённый сценарий движения цены
2. **Path B** — альтернативный сценарий подтверждения
3. **Накопление (Accumulation)** — фаза накопления перед движением
4. **Раннее движение (Early Move)** — детекция начала импульса

### Управление рисками

- **Take Profit** — 3 уровня (tp1, tp2, tp3)
- **Stop Loss** — биржевой STOP_MARKET ордер
- **Hedge Mode** — защита позиции без reduceOnly
- **Timeout** — автоматический выход из сделки по таймауту

### Сигнальная логика

- **k-of-n пороги** вместо all() для устойчивости к дребезгу
- **Гейт плотности окна** — проверка временного диапазона снимков
- **Трёхзначная логика** — true/false/unknown для обработки пропусков
- **Непрерывная сила сигнала** — для триггера Шмитта

## 📈 Метрики и отчётность

Система собирает следующие метрики:

- **Win Rate** — процент прибыльных сделок
- **Return @ 60m/120m/240m** — доходность на разных горизонтах
- **Capture Good Return** — доля захваченного движения
- **Max/Min PnL** — экстремумы PnL во время сделки
- **Drawdown from Peak** — просадка от пика
- **Hold Time** — время удержания позиции

## 🔧 Конфигурация

Версии логики:

- **CONDITIONS_VERSION = 2** — текущая версия предикатов
- **SIGNAL_LOGIC_VERSION** — версия сигнальной логики

Изменения в версии 2:

- k-of-n вместо all() для пороговых условий
- Гейт плотности окна (проверка временного диапазона)
- Трёхзначная логика (true/false/unknown)
- Итоговое изменение за окно вместо шаговой монотонности
- Непрерывная сила сигнала для триггера Шмитта

## 📝 Лицензия

Проект предназначен для образовательных и исследовательских целей.

## ⚠️ Отказ от ответственности

Торговля криптовалютными фьючерсами сопряжена с высоким риском. Используйте систему на свой страх и риск. Авторы не несут ответственности за любые финансовые потери.
