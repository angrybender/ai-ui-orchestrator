## Git proxy service

Необходим программный proxy до git.

## Требования

### Конфигурация

- Proxy встроен в существующее FastAPI-приложение по префиксу `/git` на том же порте и слушает только `127.0.0.1`.
- `remote_host` обязателен; проверяется только заполненность поля, синтаксис значения не валидируется.
- Если задан только `token`, выбирается HTTPS; если задан только `ssh_key`, выбирается SSH. Одновременное задание обоих способов доступа невалидно.
- Для HTTPS используются `https_username` (по умолчанию `x-access-token`) и token. Для SSH — `ssh_username` (по умолчанию `git`) и путь к приватному ключу.
- Парольная аутентификация и SSH agent не поддерживаются. Ключ должен быть доступен без интерактивного ввода passphrase.
- Token хранится в `settings/user` в password-поле, не возвращается в UI, логи или ошибки. Пустое поле при редактировании сохраняет прежнее значение.
- SSH host keys проверяются через `known_hosts`; неизвестные ключи и auto-add отклоняются.
- Settings не сохраняет невалидную конфигурацию.

### URL и безопасность

- Локальный URL имеет формат `/git/<repository-path>`, включая вложенные namespace.
- Из запроса используется только относительный repository path; upstream host всегда берётся из конфигурации.
- Запрещены `..`, абсолютные пути, encoded traversal, query-параметры и fragment.
- Upstream repository автоматически не создаётся.

### Git protocol

- Для HTTPS потоково проксируются `info/refs`, `git-upload-pack` и `git-receive-pack` как HTTP requests/responses. Basic Auth добавляется только серверно.
- Для SSH используется настроенный ключ и потоковая передача Git protocol.
- Разрешены только `info/refs` с `service=git-upload-pack` или `service=git-receive-pack`, `git-upload-pack` и `git-receive-pack`; остальные endpoints отклоняются.
- Поддерживаются clone, fetch, fetch --all, pull, push и push --force через Git Smart HTTP.
- Force-push разрешён. Публикация proxy за пределы localhost не входит в стандартную конфигурацию.
- Git-совместимые status, headers и protocol stream передаются без JSON-преобразования. Credentials и внутренние пути не раскрываются.

### Выполнение

- Постоянный bare-repository кэш и локальное состояние не используются.
- Для одного repository операции выполняются последовательно через asyncio mutex.
- Приложение запускается строго с одним worker.
- Timeout задаётся в Settings, значение по умолчанию — 10 секунд; зависшие subprocess и upstream-соединения завершаются.
- Git payload передаётся streaming-образом без накопления целиком в памяти.
- При невалидной конфигурации приложение запускается, но `/git/...` возвращает безопасную ошибку конфигурации.

### Логирование и тестирование

- В аудит записываются время, repository path, операция, HTTP status, длительность и размеры потоков.
- Credentials, URL с credentials и Git payload не логируются.
- Обязательны unit-тесты path validation, auth redaction, routing и конфигурации.
- Интеграционные тесты настоящего Git protocol, Playwright-тесты Settings и отдельная совместимость с nginx/reverse proxy не требуются.
