# Docker: dev и тесты

## Быстрые команды

| Задача | Команда |
|--------|---------|
| Линт + все тесты | `docker compose -f docker-compose.dev.yml --profile test run --rm test` |
| Только pytest (без сборки образа, если уже есть) | `docker compose -f docker-compose.dev.yml --profile test run --rm test` |
| Интерактивная сессия | `docker compose -f docker-compose.dev.yml --profile dev run --rm dev bash` |
| Запуск бота в dev-контейнере | `docker compose -f docker-compose.dev.yml --profile dev --env-file .env run --rm dev python main.py` |
| Проверка POL на EOA для газа (redeem) | `docker compose -f docker-compose.dev.yml --profile dev --env-file .env run --rm dev python scripts/check_eoa_polygon_gas.py` |
| Проверка EOA и proxy (Safe) адресов | `python scripts/check_wallet_addresses.py` |
| На каком кошельке токены для redeem | `python scripts/check_token_holder.py` |
| Реальные позиции кошелька (Data API) | `python scripts/check_positions.py` |

**Отладка (Run & Debug):**

1. Пересобери образ: `docker compose -f docker-compose.dev.yml build dev-debug`
2. Выбери «Docker: Launch Bot (Debug)» и нажми F5.
3. Если ECONNREFUSED — проверь: `docker ps` (контейнер polymarket-bot-dev-debug), `docker compose -f docker-compose.dev.yml --profile debug logs dev-debug`

Первый запрос соберёт образ `polymarket-trader-bot:dev` из `Dockerfile.dev` (Python 3.12, зависимости из `requirements.txt` + `requirements-dev.txt`).

## Переменные окружения для **тестов**

Юнит-тесты в `tests/` **не требуют** реальных ключей Polymarket или Telegram: используются фикстуры (`tests/conftest.py`) и временные файлы конфигурации.

Сервис `test` в `docker-compose.dev.yml` задаёт пустые строки для секретов только чтобы `load_dotenv` не подхватил случайный `.env` с хоста при монтировании каталога (при необходимости можно убрать `env_file` — его нет у сервиса `test`).

Достаточно:

```bash
docker compose -f docker-compose.dev.yml --profile test run --rm test
```

Дополнительные переменные **не нужны** для прохождения `pytest` и `ruff`.

## Переменные для **запуска бота** (`python main.py`)

Минимально обязательно (см. `.env.example`):

| Переменная | Назначение |
|------------|------------|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота (**обязательно** для старта `main.py`) |
| `PRIVATE_KEY` | EOA для подписи CLOB и Safe |
| `POLYMARKET_API_KEY` | API key CLOB |
| `POLYMARKET_API_SECRET` | API secret |
| `POLYMARKET_API_PASSPHRASE` | API passphrase |

Часто нужны для торговли и redeem:

| Переменная | Назначение |
|------------|------------|
| `POLYMARKET_PROXY_ADDRESS` | Адрес proxy-кошелька (Safe); без него redeemer не создаётся |
| `POLYGON_RPC_URL` | RPC Polygon; дефолт в коде — PublicNode (`polygon-bor.publicnode.com`). `polygon-rpc.com` из Docker часто даёт 401 — при необходимости Alchemy/Infura. |
| `POLYMARKET_SIG_TYPE` | Тип подписи CLOB (по умолчанию `2`) |

Скопируйте `.env.example` в `.env` и заполните значения.

## Продакшен-образ

Как раньше: `docker compose build` / `docker compose up` используют корневой `Dockerfile` и `docker-compose.yml` без dev-зависимостей.
