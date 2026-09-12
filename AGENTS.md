MUST
- index.html and *.js must works without server and building (dont use `import` in JS)
- Separate logic by files prefer create once monolith
- use playwright skill for testing web-app
- delete all tmp files after finish work (include screenshots)
- during agent taskl dont request any queries to user, do work after creating plan, if there are several variant implementation -- choose optimal without request
- after finish work - delete all tmp files/scripts what you have created
- upgrade specifications after all fixes

TESTING

- Перед запуском любых Python-тестов обязательно создать или обновить отдельное окружение разработки в `dev-venv`, не изменяя существующую `venv`:
  ```bash
  python3 -m venv dev-venv
  dev-venv/bin/python -m pip install -r requirements.txt
  ```
- Если `dev-venv` уже существует, повторный вызов `python3 -m venv dev-venv` безопасен и нужен для проверки окружения; зависимости обновлять через `dev-venv/bin/python -m pip install -r requirements.txt`.
- Папку `venv` не использовать и не изменять: это production-окружение.
- После каждого прогона тестов удалять только тестовые логи, созданные в `data/logs`; рабочие логи реальных запусков агентов не удалять.
- При `ModuleNotFoundError` или другой ошибке отсутствующей зависимости не прекращать работу: повторно выполнить установку из `requirements.txt` в `dev-venv`, проверить, что тесты запускаются тем же Python-интерпретатором, и только затем диагностировать оставшуюся ошибку.
- Для web-app дополнительно запускать Playwright-тесты после установки Python-зависимостей; использовать `playwright` skill.
- Если агент задал пользователю вопрос и получил ответ, после ответа проверить, содержит ли он новое долговременное правило, ограничение или соглашение проекта. Если содержит и такого пункта ещё нет, сформулировать это знание как короткую инструкцию и добавить в `AGENTS.md`; не добавлять дубликаты и не записывать разовые решения.
- Если создание `dev-venv` невозможно из-за отсутствия `ensurepip` или `venv`, установить соответствующий системный пакет (например, `apt install python3.12-venv`) и продолжить работу.

SPECIFICATION

directory: [specs](specs)

- tech stack: [tech-spec.md](specs/tech-spec.md)
- general UI requirements: [main_ui.md](specs/main_ui.md)
- Board page: [board.md](specs/board.md)
- Archive page: [archive.md](specs/archive.md)
- Settings page: [settings.md](specs/settings.md)

IGNORE DIR:
- dev
- data (database storage)
- venv (local python environment)

SPECIFICATION WORKFLOW (обязательно)
- Перед любыми изменениями сначала прочитать все относящиеся к задаче файлы из `specs/`, включая текстовые требования и указанные в них изображения.
- Для UI-задач сверять реализацию с эталонными изображениями из `specs/` визуально через Playwright; одной проверки функциональных тестов недостаточно.
- Не считать задачу выполненной, пока каждый пункт спецификации и визуальные детали эталона не проверены.
- В ответе указывать, какие файлы спецификации прочитаны и какие проверки выполнены.
- Если реализация расходится со спецификацией, приоритет имеют требования и эталонный дизайн из `specs/`, а не существующая реализация.
- Не добавлять на страницы декоративные или текстовые заголовки раздела: основной заголовок, подзаголовок и `eyebrow` запрещены, если явно не указаны в спецификации или эталонном макете конкретной страницы.
