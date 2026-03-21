# tg-export

Flexible Telegram data export tool.

## Установка

```bash
uv pip install -e ".[dev]"
```

## Использование

Все команды запускаются через `uv run`:

```bash
# 1. Задать API credentials (с https://my.telegram.org)
uv run tg-export auth credentials

# 2. Добавить аккаунт
uv run tg-export auth add --name myaccount

# 3. Список аккаунтов
uv run tg-export auth list

# 4. Получить каталог чатов
uv run tg-export list --account myaccount --output catalog.yaml

# 5. Сгенерировать шаблон конфига
uv run tg-export init --account myaccount

# 6. Запустить экспорт
uv run tg-export run --account myaccount

# 7. Проверить целостность файлов
uv run tg-export verify --account myaccount
```

## Структура конфигов

```
~/.config/tg-export/
  api_credentials.yaml      # API ID и Hash (общие для всех аккаунтов)
  sessions/
    myaccount.session        # Telethon-сессия
  myaccount.yaml             # Конфиг экспорта для аккаунта
```

## Тесты

```bash
uv run python -m pytest tests/ -v
```
