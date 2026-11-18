# Проверка оркестратора

Исполнитель: `python3 agent_worker.py`. Он занимает одну первую задачу `OPEN`; ID задачи в аргументах не принимает. HTTP-запуск: `POST /api/agent/run` (202 с PID, 409 при активном запуске, 503 при ошибке создания процесса). Завершение HTTP-запроса не означает завершения агента.

Настройки читаются через `Config.get`: `remote_server.username`, `remote_server.host`, `remote_server.password`, `remote_server.ssh_key`, `tasks.base_dir`, `agent.shell`, `agent.agent_timeout`. В `agent.shell` обязателен отдельный не заключённый в кавычки placeholder `${PROMPT}`, например `qwen -p ${PROMPT} --acp --yolo`. Неизвестный SSH host key необходимо заранее проверить и добавить в системное хранилище известных ключей. Исполнитель не принимает ключ автоматически.

История хранится в SQLite, таблица `agent_runs`. Активные состояния: `PREPARING`, `RUNNING`; конечные: `SUCCEEDED`, `FAILED`. Восстановление выполняется при следующем запуске. Linux использует `/proc`, boot ID, start ticks и pidfd. Windows использует WinAPI: время создания процесса из `GetProcessTimes` и завершение через проверенный handle. Существующий каталог задачи не перезаписывается, в том числе после неудачной попытки: перед повтором оператор должен отдельно решить, что делать с диагностическими файлами.

### Разработка на Windows 10

Из PowerShell/CMD: `.\.venv\Scripts\python.exe start.py` (Python 3.12+). Веб-сервер и консьюмер запускаются вместе. Для штатной остановки используйте Ctrl+C: родитель отправляет дочерним группам Ctrl+Break. Без доступной консоли возможна принудительная остановка без выполнения `finally`; запись запуска восстанавливается при следующем запуске исполнителя. Принудительное закрытие терминала или завершение через диспетчер задач не гарантирует остановку всех дочерних процессов. Это базовый режим разработки, не Windows-служба.

`remote_server.ssh_key` — абсолютный Windows-путь к локальному ключу (если используется ключ), а каталог задачи — POSIX-путь удалённого SSH-сервера. Base directory (`tasks.base_dir`) задаёт абсолютный родительский каталог для создания папок задач; Init и агент запускаются внутри папки конкретной задачи. Удалённому агенту нужен POSIX shell; Windows-поддержка относится к локальному приложению.

Автоматические тесты (установлены зависимости из `requirements.txt`):

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m pytest -q -p no:cacheprovider tests
```

Тесты используют временные БД/файлы и эмуляцию SSH; реальные реквизиты не требуются. Тесты транспорта проверяют ACP/SFTP, тесты исполнителя — БД и конкуренцию отдельных процессов.

Браузерный smoke-тест требует Playwright и отдельного тестового сервера с пустой БД. **Не запускать против рабочей очереди:** тест вызывает API запуска.

```sh
AGENT_TEST_URL=http://127.0.0.1:8769 node tests/agent_browser_smoke.js
```

Он проверяет загрузку Board без ошибок JS и получение PID из API. Реальный удалённый агент и визуальное соответствие UI этим тестом не проверяются.

### Real-time Board

`tests/test_board_events.py` проверяет SSE-снимки, захват/завершение задачи, heartbeat, переподключение и завершение потока.

```sh
HOME=/root PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright \
NODE_PATH=/root/.npm/_npx/e41f203b7505f1fb/node_modules \
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. node tests/board_live.js
```

Тест запускает отдельный локальный FastAPI с временной SQLite и использует отдельный процесс для изменений исполнителя. Рабочая очередь и реальный SSH не используются; сервер и временные данные очищаются после теста. Требуются Python-зависимости из `requirements.txt` и Playwright; пути окружения выше соответствуют окружению разработки проекта.



### Живой ACP-прогон с провайдером

Явный запуск `tests/live_acp_matrix.py` выполняет ACP-01…26 и INT-01…07
для Codex и Claude на настроенном SSH-host. Используются платные модельные
вызовы, отдельные удалённые cwd и временная локальная БД. Обычный pytest
эти сценарии не запускает. Установленные адаптеры должны поддерживать
orchestratorBackgroundTasks v1; для Codex доступен `--codex-binary`.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. dev-venv/bin/python tests/live_acp_matrix.py \
  --key-file /private/routerai.key --endpoint https://routerai.ru/api/v1 \
  --report /tmp/routerai-live.json
```

Выбор: `--agents codex` и `--cases ACP-20,INT-04`. Ключ читается из файла,
не из argv; конфигурации SDK и ключ на VM удаляются после прогона вместе
с собственными процессами и cwd. При SIGTERM запускается та же очистка.
Отчёт хранит endpoint, test-root и результаты без ключа. `partial` означает
подтверждённое ограничение, а не полную гарантию выполнения/очистки.
Сценарии реализованы в `tests/live_acp_matrix.py`, `tests/live_acp_cases.py` и `tests/live_acp_integration.py`.


### Живой Clear context с текущими настройками VM

`tests/live_task_context.py` выполняет отдельную интеграцию сброса контекста для Claude, Codex и Qwen. Используются установленный `~/.local/bin/acp-wrapper`, нативные CLI и существующие настройки/авторизация SSH-пользователя. По умолчанию драйвер не задаёт новый API key, provider, model, CODEX_HOME или CLAUDE_CONFIG_DIR; Qwen запускается с текущим `agent.shell`. Claude/Codex получают только стандартные флаги автономных разрешений. Штатные флаги ожидания фоновой работы и quiet timeout берутся из снимка настроек оркестратора.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests dev-venv/bin/python tests/live_task_context.py \
  --report /tmp/acp-context-live-results.json
```

Матрица: первая сессия запоминает случайный токен только в беседе и создаёт файл; resume воспроизводит токен; настоящий HTTP clear-context сохраняет историю/даты и идемпотентно задаёт границу; новая сессия читает прежний файл, не знает старый токен и запоминает новый; следующее resume воспроизводит новый токен. Проверяются реальные OUT NDJSON, sessionId, состояние REVIEW, запрет сброса во время IN PROGRESS и отсутствие собственных оставшихся процессов. Если агент не авторизован, отдельно проверяются сброс после WAIT и запрос session/new; новый ID проверяется, когда агент разрешает создание сессии без входа. Проверка памяти отмечается как невыполненная, результат partial. Доступны --agents и --phase-deadline (внешний watchdog отдельной фазы; не меняет настройки VM).

Рабочая БД и consumer не используются: каждый агент получает временную SQLite, а на VM создаётся отдельный cwd. Init и system append prompt отключены только в тестовом снимке. Сырые логи находятся во временном каталоге, а отчёт содержит только результаты и идентификаторы сессий. После прогона удаляются собственные процессы, каталоги и новые файлы нативных сессий; записи Codex удаляются по точным ID тестовых threads. Контрольные суммы файлов настроек сравниваются до/после. Секреты и рабочие сессии сохраняются.


Для Claude Code с текущим API Qwen есть отдельный явно выбираемый режим:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests dev-venv/bin/python tests/live_task_context.py \
  --agents claude --claude-qwen-provider \
  --report /tmp/claude-qwen-context-live-results.json
```

Ключ, endpoint и модель берутся непосредственно из текущих `.qwen/settings.json` на VM. Credential не возвращается в локальный процесс, stdout или argv; временный settings-файл создаётся с mode 0600 внутри test-root. Для Claude выставляются совместимые ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL и alias-модели. Один временный CLAUDE_CONFIG_DIR сохраняется на все четыре фазы; сохранение native session обязательно для проверки session/load. После завершения он удаляется вместе с остальными артефактами, постоянные настройки не меняются. Это клиент Claude Code с моделью Qwen через тот же провайдер.
