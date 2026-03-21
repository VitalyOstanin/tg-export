# Правила проекта tg-export

## Инструменты

- Использовать `uv` вместо `pip` для всех операций с пакетами (`uv pip install`, `uv venv` и т.д.)
- Запускать тесты через `uv run python -m pytest` (не напрямую через venv)
- Запускать CLI через `uv run tg-export` (не напрямую через venv)
- Проверять логику Telethon по его исходникам: `$HOME/devel/Telethon`
- Использовать Python LSP для навигации по коду (findReferences, goToDefinition и т.д.)
