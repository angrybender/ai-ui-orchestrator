## Ход разработки: импрувы и фиксы

### Ревью спецификаций по реализации — 2026-09-17

В рамках этого ревью реализация принята источником истины. Проверены все девять Markdown-файлов `specs/`, пять PNG-прототипов, соответствующие контроллеры, хранилища, настройки, SSH/ACP/Init, прокси, шаблоны и клиентские скрипты. PNG сохранены как структурные схемы; отличия фактического UI описаны текстом. Код приложения и тестов не изменялся.

После обновления `dev-venv` и установки `requirements.txt` полный Python-набор с `-W error`: **399 passed, 15 failed, 1 skipped**. Все 15 падений — в `tests/test_task_remote.py`: тестовая подмена `Config.get` не задаёт `tasks.logs_max_age`, поэтому lifespan падает на `time.time() - None` до запросов удаления. Это не подтверждает исправность этих 15 сценариев. Пропущена проверка реального WinAPI на Linux. Приведённые выше численные результаты прошлых реализаций — исторические, не результаты этого ревью.

Playwright успешно выполнил `tests/task_cycle_ui.js`, `tests/http_proxy_ui.js`, `tests/settings_required.js`. Дополнительная проверка на изолированном FastAPI подтвердила меню и создание задачи, пагинацию Archive (21 задача), режим просмотра архивной задачи, возможность редактирования ARCHIVE через общий API, возврат тестового token в форме/Save, отклонение пустого token без ключа, сохранение относительного base_dir, Markdown/HTML и кнопку копирования в чате, отсутствие переполнения формы на ширинах 320/375/700px. Визуально просмотрены формы, Settings, карточка Init, чат и HTTP proxy в сравнении с исходными PNG.

Браузерные проверки использовали изолированную БД/настройки и вымышленные данные; рабочие SSH/Git/upstream-серверы не изменялись. Реальная совместимость Git-команд и Windows в этом ревью не проверялась. Временные скрипты, БД и снимки после проверки удалены; тестовые raw-логи очищает session fixture, рабочие логи сохраняются.

### Исправление тестовой конфигурации после ревью — 2026-09-17

В `tests/test_task_remote.py` три подмены общего метода `Config.get` заменены подменой ссылки `board_store.Config`. Все 15 ранее упавших сценариев теперь доходят до проверки удаления; lifespan использует обычный сервис настроек и default срока хранения логов. Код приложения и проверки ожидаемых результатов не изменены.

После обновления `dev-venv` и установки `requirements.txt` полный Python-набор с `-W error`: **414 passed, 1 skipped** (реальный WinAPI требует Windows). Playwright `tests/board_delete_button.js` прошёл: видимость Delete, подтверждение, обработка 502, сохранение формы, Spinner, защита от повторного удаления и переходы после 204. Проверки использовали изолированные данные/маршруты; временные тестовые данные удалены, новые тестовые логи очищены session fixture.

### Синхронизация требований при повторном запуске — 2026-09-23

`task_context.sync_context` вызывается перед Init/ACP при каждом захвате OPEN → IN PROGRESS. Проверены изменение описания, добавление/замена/удаление вложений, пустой набор, сохранение результатов вне requirements, повторы из WAIT и REVIEW, ошибка переноса/удаления и отклонение symlink/каталогов. Правила владения каталогом и частичного сбоя — [agent.md](agent.md#синхронизация-при-каждом-запуске).

Проверки: 160 Python-тестов (`test_agent_remote`, `test_task_init`, `test_agent_worker`, `test_agent_cancel`, `test_agent_error_chat`, `test_acp_details`) успешно; Playwright `tests/task_cycle_ui.js` успешно. Использованы dev-venv, изолированная БД, имитация SSH/SFTP и локальные процессы Bash/ACP. Реальный удалённый хост не изменялся. UI не менялся; браузерная проверка функциональная, без новой визуальной сверки макетов.

### Ложное отклонение sessionId — 2026-09-24

Проверка ответа `session/new` использовала потоковый фильтр `_Diagnostics.safe`, маскирующий даже один последний символ при совпадении с началом SSH-секрета или PEM-маркера. Корректные случайные идентификаторы поэтому периодически отклонялись с `Missing, invalid or unsafe sessionId`, включая замену отсутствующей сессии после `session/load`.

Для целого JSON-поля используется `safe(..., partial_suffix=False)`: полные известные секреты и PEM-блоки по-прежнему отклоняются, неполные суффиксы не маскируются. Передаваемый в callback и `session/prompt` ID сохраняется без изменения. Потоковая диагностика и чат используют прежнюю защиту по умолчанию.

До исправления восемь регрессий первого запуска/восстановления с совпадающими суффиксами падали. После исправления: 170 Python-тестов (`test_agent_remote`, `test_acp_details`, `test_agent_worker`, `test_agent_error_chat`, `test_agent_cancel`, `test_task_init`) успешно с `-W error`; Playwright `tests/task_cycle_ui.js` успешно. Окружение dev-venv обновлено из requirements.txt, данные изолированы, новые тестовые raw-логи удалены фикстурой. SSH/SFTP подменены; реальный удалённый агент не запускался. UI не менялся, браузерная проверка функциональная.

### Единое отображение ошибок HTTP API — 2026-10-03

`static/js/api-errors.js` оборачивает браузерный fetch до загрузки скриптов страниц, возвращает успешные Response без изменения и отклоняет запросы с неуспешным HTTP-статусом после показа общего Toast. Исключение содержит `status`, исходный `detail` для ошибок рядом с полями и `reported` для предотвращения повторного уведомления. Сетевая ошибка показывается без внутренних деталей; AbortError передаётся без уведомления. Бэкенд и формат его ответов не меняются. Клиентский модуль — обычный script без import и сборки. Правила отображения и подавления повторов фонового опроса — [main_ui.md](main_ui.md#общая-обработка-ошибок-api).

Регрессии: `tests/api_errors.js` проверяет HTTP 400–599, JSON detail/message/error, вложенные словари, FastAPI validation errors, plain text, HTML, пустое тело, сеть, отмену, повтор фонового опроса и восстановление, сохранение формы/Spinner и ошибки рядом с полями на Board и в чате, Settings, Archive, HTTP proxy и Recovery. Используются настоящие Jinja-шаблоны, браузер Playwright и перехваченные маршруты без рабочей БД и внешних подключений. `tests/board_errors.js` подключает общий модуль и проверяет существующие сценарии Board.

Проверка изменения: 148 Python-тестов (`test_app`, `test_board`, `test_settings_regressions`, `test_http_proxy_settings`, `test_agent_api`) успешно; Playwright `api_errors.js`, `board_errors.js`, `agent_recovery_ui.js` успешно. Визуально просмотрены Settings и форма Board с error Toast на 1280/375px; геометрия дополнительно проверена на 320px. Старый `task_chat.js` обращается к удалённому `.chat-agent span` и не проходит на текущем UI; новый `api_errors.js` проверяет отказ отправки комментария, ошибки рядом с полем и сохранение текста. Временные снимки и установочный лог удалены, рабочие данные не изменялись.

### Очистка локальной БД — 2026-10-10

controllers/database.py принимает подтверждённый POST /api/database/clear; database_store.py очищает таблицы и sqlite_sequence транзакционно с сохранением схемы. Активные запуски блокируют очистку (409). Локальные вложения data/files, включая оставшиеся без записей, удаляются через предварительное перемещение с восстановлением при ошибке до commit; удаление временного каталога завершается до ответа API. Настройки, логи и удалённые каталоги сохраняются. UI и ограничения — [board.md](board.md#очистка-локальной-бд).

В обновлённом dev-venv прошли 17 Python-тестов test_clear_database, test_agent_recovery, test_app с -W error. Playwright clear_db_ui.js проверил реальную очистку на изолированной БД, подтверждение/отказ, SSE, Spinner, повторные клики, ошибки, создание после очистки и ширину 320px. Снимки Board на 1459px/320px визуально просмотрены с учётом текстовых требований и структурного прототипа карточки. Рабочая БД не очищалась; временные снимки удалены.

Дополнительная проверка удаления файлов: 18 Python-тестов test_clear_database, test_agent_recovery, test_app прошли с -W error. Проверены удаление вложений/остатков, сохранение служебных файлов, rollback БД и восстановление файлов при ошибке перемещения. Playwright clear_db_ui.js прошёл, включая видимый Spinner Clearing… во время запроса; состояние загрузки просмотрено визуально. Временные снимки удалены.


### Опциональное ожидание ACP — реализация 2026-10-21

`settings/ui/agent.json` добавляет checkbox `ACP wait background tasks` с default=false; `agent_worker.CONFIG_KEYS` включает флаг в снимок claim. Старые пользовательские JSON изменять при обновлении не требуется. Шаблоны/CSS/клиентские скрипты приложения не менялись: используется существующий metadata-driven checkbox и обычный Save.

`agent_background.py` реализует ограниченный inbox stdout, чтение между RPC, проверку поддержки Qwen, опрос задач и conditional close. `agent_remote.py` подключает этот путь только при `agent.acp_wait_background_tasks is True`. Parser, санитизация и callbacks сохранены; replay при load скрыт от чата. Штатный путь ждёт close=true, EOF/выход ACP=0 и проверку остановки группы без сигналов (`AgentControl.verify_stopped`, `task_init.stop_group(terminate=False)`). При ошибке/отмене worker пробует закрыть Qwen session для очистки detached native shells, затем действует прежняя остановка группы. Неподтверждённый session close после prompt сохраняет agent_uncertain даже при пустом исходном PGID.

Весь период остаётся фазой Agent с прежним deadline и активной монопольной записью. Новый статус БД и миграции не требуются. Это ожидание учтённых Qwen задач; cgroup/универсальный runner из исследовательского плана остаётся отдельным расширением. Актуальные правила — [agent.md](agent.md#опциональное-ожидание-фоновой-работы-acp), UI — [settings.md](settings.md).

Новые регрессии: `tests/test_agent_background.py` — реальные локальные ACP/shell процессы и изолированная БД, включённый/выключенный/отсутствующий флаг, активная очередь, второе фоновое задание, автоматический итоговый ответ, отказ/ошибка close, ошибки команд/продолжения/снимка, разрыв, таймаут, отмена, неподдерживаемый агент, пустой список заданий, resume/replay, снимок настройки и boolean roundtrip. `tests/agent_background_ui.js` — настоящий FastAPI и изолированные настройки, сохранение true/false и перезагрузка, default off, desktop/mobile без переполнения. Тестовый LocalSSH дополнен безопасным закрытием broken stdin и SFTP-операциями resume; production venv не используется.


Проверка реализации в обновлённом dev-venv: полный Python-набор с `-W error` — **532 passed, 1 skipped** (проверка WinAPI требует Windows). После уточнения строгой валидации id/kind/status/exitCode снимков дополнительно прошли **21** проверки `test_agent_background.py`, включая штатное завершение без TERM/KILL. Playwright: `agent_background_ui.js`, `settings_required.js`, `board_live.js` — успешно. Settings Agent визуально проверены на 1357px и 375px относительно settings_proto.png и текстовых требований; отсутствие переполнения дополнительно проверено на 320px. Новые файлы реализации и постоянных регрессий добавлены в git.

Реальный smoke через изменённый `run_remote` на настроенной ВМ (Qwen Code 0.23.0, текущая модель), отдельный временный task cwd, флаг включён только в снимке теста: первый end_turn на 12,81 с; завершение фоновой команды с exitCode=0 подтверждено на 45,89 с; conditional close разрешён на 63,97 с после 18 отказов во время автоматического продолжения; содержимое probe.done проверено реальным ReadFile в этом продолжении, итоговый callback содержит BACKGROUND_VERIFIED_OK. `run_remote` вернулся успешно после штатного выхода ACP и проверки остановки группы. Оставшихся native процессов не обнаружено. Рабочие настройки/БД не менялись. Временные локальные и удалённые каталоги, скрипты, Qwen cache исследовательской сессии и снимки удалены; рабочие логи сохранены.


### Проверка Claude/Codex с RouterAI — 2026-10-21

Текущий opt-in backend подтверждён как Qwen-specific: на реальных @agentclientprotocol/codex-acp 2.2.2 и @agentclientprotocol/claude-agent-acp 0.94.0 вызов run_remote с флагом true завершается ошибкой отсутствия Qwen active-work support сразу после initialize, до создания/промпта сессии. Оба Qwen RPC также проверены напрямую и возвращают -32601. Это ограничение совместимости, не ошибка провайдера: оба агента успешно отвечали на openai/gpt-6-luna через RouterAI /api/v1, а оба wire API (Responses/Messages) дали HTTP 200.

Нативные фоновые пробы с открытым ACP подтвердили ранний end_turn у обоих. Codex прислал terminal completion и exit_code=0 после окончания команды; для итоговой проверки понадобился второй prompt. Claude вернул completed для запуска Bash ещё при живом процессе, но сохранил backgroundTaskId, затем автоматически проверил результат. Для расширения поддержки требуются отдельные обработчики lifecycle Claude/Codex и стратегия финальной проверки Codex. UI/код приложения не менялись; полноценный успешный прогон текущей опции для этих агентов не заявляется.

Агенты установлены на удалённой ВМ в /home/user/.local/share/acp-test-agents с symlinks в /home/user/.local/bin; локальная установка агентов не выполнялась. Конфигурация авторизации для проб была изолированной и временной, родные аккаунты не использовались. Подробные версии, проверки, наблюдения и источники — [agent.md](agent.md#совместимость-с-claude-и-codex-через-routerai--2026-10-21).


### Найденный путь поддержки Claude/Codex — 2026-10-21

На установленных удалённых codex-acp 2.2.2 и claude-agent-acp 0.94.0 реально согласованы AIR capabilities asyncTasks/nativeSubagentSessions/sessionFailure. Исследовательский клиент дождался нативной фоновой команды, отправил в ту же сессию финальный запрос проверки результата, получил end_turn и штатно закрыл оба ACP с exit=0. Это прототип, а не подключённый production backend. Текущий checkbox по-прежнему включает только Qwen backend.

Предлагается выделить AIR backend: реестр (sessionId, asyncTaskId), учёт subagent lifecycle, ожидание и повторная финализация после новых задач, прежний общий deadline и error/cancel cleanup. Для строгой атомарности требуется remote wrapper/патч с conditional-close/drain, поскольку обычный session/close безусловно прерывает работу. У Claude 0.94.0 v1 нет описанного в main автономного async-turn; пустой task registry недостаточен. Для всех произвольных процессов требуется дополнительный supervisor/учёт shell workloads, а не только asyncTasks или PGID. Подробности и реальные времена — [agent.md](agent.md#решение-для-claudecodex-согласованный-asynctasks-и-финальный-ход--2026-10-21).


### Конфигурация периода тишины ACP — 2026-10-21

settings/ui/agent.json добавляет required integer acp_background_quiet_timeout с default 60 непосредственно после acp_wait_background_tasks. Используются существующие number input с min=1/step=1 и серверная проверка integer; Save хранит JSON integer. Ключ agent.acp_background_quiet_timeout добавлен в agent_worker.CONFIG_KEYS и согласованный снимок запуска. Пользовательские JSON не изменяются при обновлении; отсутствующее значение читается из метаданных. AIR backend пока отсутствует: значение подготовлено для будущего ожидания тишины и не меняет текущий Qwen drain.


### AIR background backend — реализация 2026-10-21

agent_background_air.py — отдельная логика AIR lifecycle, capability negotiation requirements, дерева subagents и активных tools, финального prompt с проверкой результата, монотонного quiet timeout, обработки sessionFailure и штатного close. agent_background.py сохраняет единственный bounded reader и Qwen drain, выбирая AIR по подтверждённым возможностям, без настройки имени агента. agent_remote.py предлагает оба протокола только при checkbox=true, восстанавливает реестр во время load без callback replay, сохраняет только root reply в чате, выбирает правильный cleanup close, ждёт ACP exit=0 и проверяет PGID без сигналов. При выключенном флаге поток выполнения сохраняется. Quiet timeout=60 по умолчанию включён в общий deadline; Qwen не получает дополнительную задержку. Новый статус БД, миграции и изменения адаптеров на ВМ не требуются.

Предыдущие исследовательские записи о неподключённом AIR и неиспользуемой настройке теперь являются историческими. Практические гарантии и ограничения периода тишины определены в agent.md. Новые регрессии tests/test_agent_background_air.py используют реальные локальные ACP-процессы и изолированную БД; вместе с прежними Qwen проверяют позднюю/повторную работу, subagents, quiet reset, отказ/ошибки/EOF, cancel/timeout, replay и отключённый режим.


Проверки: полный Python-набор 557 passed, 1 skipped; после завершающих изменений drain/EOF/late-close — 57 passed в background/AIR/cancel. Playwright Agent settings и Board live прошли. Реальный run_remote успешно проверен на ВМ с Codex и Claude через RouterAI openai/gpt-6-luna, quiet=60, и с настроенным Qwen через его conditional-close. Подробные времена и границы проверок — agent.md. UI не менялся, производственные настройки/данные сохранены.


### Ограниченная AIR финализация — 2026-10-28

`agent_background_air.py` отделяет failed shell-команд от фатального sessionFailure; после end_turn разрешает обработку результата при ещё активных заданиях. Новый failed запускает сверку без ожидания всех остальных задач; отсутствие активности 30 секунд даёт модели ход для остановки ненужного сервера/watcher. Допускаются максимум три дополнительных prompt и абсолютный drain budget 300 секунд + quiet timeout внутри исходного agent_timeout. Только успешный TaskStop с task_id/shell_id в той же сессии подтверждает stopped без корректирующего completed. Неуспешный stop и неизвестный stopped не разрешают REVIEW. Реестры TaskStop ограничены вместе с остальным lifecycle.

`agent_background.py` проверяет дополнительный deadline при чтении молчащего RPC и предоставляет внешнему executor deadline_error; AIR cleanup ограничен пятью секундами. Выход ACP после успешного close также ограничен пятью секундами, чтобы зависший shutdown не возвращал многочасовое ожидание. `agent_remote.py` проверяет его также в supervisor, чтобы заблокированная запись не удерживала запуск до большого agent_timeout. Прежние отмена, PGID-проверка, replay, санитизация и agent_uncertain сохраняются. Qwen backend и выключенный checkbox используют прежний путь. UI/настройки и схема БД не изменены. Подробные правила — [agent.md](agent.md#claudecodex-air-и-период-тишины).

Проверки: полный Python-набор 570 passed, 1 skipped; после завершающей защиты shutdown — 69 passed AIR/Qwen/cancel. Playwright agent_background_ui.js прошёл. Реальный run_remote с Claude Sonnet через RouterAI успешно обработал failed + остановку сервера (41,17 с, quiet=3) и сервер без failed (127,90 с, quiet=60); тестовые processes/cwd/caches удалены.


### Форк Claude ACP с явным drain — 2026-10-29

`adapters/claude-background` содержит patch, installer и контракт для pinned
upstream 0.96.0. Сборка относится только к Node-адаптеру на agent host;
браузерный JS и запуск web-app не требуют нового build/server. Зависимости
адаптера устанавливаются по upstream package-lock в отдельный prefix.
`agent_background_claude.py` выбирается по явному подтверждению расширения,
использует существующий единственный stdout reader, callbacks, deadline,
cleanup и монополию БД. Дополнительные эвристические пределы обычного AIR
backend к нему не применяются. Схема БД, статусы, UI и настройки не меняются.
Нормативный контракт и границы гарантий — [agent.md](agent.md#claude-форк-с-явным-ожиданием-фоновой-работы).


Итоговые проверки: Python **585 passed, 1 skipped**, background/AIR/Qwen/cancel
**83 passed**, Playwright Agent settings успешно. На ВМ installer прошёл
build/check и **790** ACP/async-task тестов; проверены native finite работа,
повторные фоновые задания, failed+TaskStop, foreground и production quiet=60.
Два падения полного upstream-набора воспроизведены без патча и описаны в agent.md.
Неподтверждённая первая проверка PGID для форка повторяется один раз без сигналов;
две неудачи сохраняют ошибку и неопределённость.


### Материалы отдельного репозитория форка — 2026-10-29

`adapters/claude-background` дополнен подробной установочной документацией,
спецификацией RPC, результатами проверок, Apache LICENSE/NOTICE и Bash launcher
для RouterAI. Повторная установка из подготовленного standalone-репозитория с
`--test`: build/check и 790 тестов прошли, 10 skipped. Launcher проверен отдельно
без реального provider key. Публикация в angrybender/acp-wrapper заблокирована
HTTP 403 из-за прав PAT; commit сохранён в проверенном `acp-wrapper.bundle`.
Полный результат и ограничения доступа описаны в agent.md. Код приложения,
настройки production и установленный на ВМ runtime не изменены.


Публикация acp-wrapper завершена после выдачи прав PAT: main содержит commit
`20f455840131510a83ee071691bb46af4906d2d2`. Git ref и шесть опубликованных
source/documentation файлов проверены через git/GitHub API. Предыдущая запись
о HTTP 403 описывает устранённую блокировку. Временный checkout удалён.


Документация acp-wrapper обновлена по запросу пользователя: из README удалён
раздел «4. Настройка RouterAI», оставшиеся разделы перенумерованы, пример
Agent shell использует установленный executable напрямую. Изменение опубликовано
в main, commit `aee3f88e81e10273830776d1eb41e2054d8fa1bc`. Проверены нумерация, локальные ссылки,
git diff --check и совпадение README с GitHub API. Код не менялся; тесты
приложения не перезапускались. Локальные README и bundle синхронизированы.


### Полная кодовая база Claude ACP fork — 2026-10-29

По запросу пользователя patch-kit преобразован в полный source-форк в
https://github.com/angrybender/acp-wrapper, main commit `971c9ea7c9ff02a38ea1d42d1498ca442d83812b`.
Скопирована полная кодовая база upstream 0.96.0; все 320 исходных файлов
проверены по upstream tree. README upstream сохранён как README.upstream.md,
workflow/dependabot definitions перенесены в docs/upstream-workflows для
справки без активации release/npm automation. В package.json указан репозиторий
форка и private=true. Исходный патч применён к src/ и сохранён отдельно;
изменения исходников ограничены четырьмя файлами прежнего патча, reverse-check
подтверждает соответствие. UPSTREAM.json фиксирует base revision и SHA256.

Installer теперь копирует build inputs из текущего checkout, не скачивает
upstream и не применяет patch второй раз. Поддерживаются прямая сборка npm,
изолированная runtime-установка, provenance source_revision/source_dirty и
исполняемый symlink. README обновлён; удалённый ранее раздел настройки RouterAI
не восстановлен. Контракт ACP и код оркестратора не менялись.

Проверки: npm ci/build/check успешны. Полный source-suite: 3321 passed,
33 skipped, 2 failed — те же ранее воспроизведённые upstream failures,
описанные в docs/testing.md. Installer --test: 790 passed, 10 skipped;
prefix/bin с пробелами, запуск executable, provenance и byte equality compiled
JS с прямой сборкой проверены. Для чистого commit проверены source_revision и
source_dirty=false. Через git ls-remote подтверждён commit main, через GitHub
Trees API сопоставлены пути/mode/blob SHA всех 328 опубликованных файлов.
Локальная полная кодовая база и bundle в adapters/claude-background обновлены;
временные checkout, node_modules/dist и runtime проверки удалены. Прочитаны
относящиеся agent.md, tech-spec.md и AGENTS.md исходного проекта. UI и Python-код
приложения не менялись; новые Python/Playwright прогоны не требовались.

### 2026-11-02 — Codex backend acp-wrapper

Добавлен полный Codex ACP 2.2.2 рядом с Claude, отдельный installer/lockfile/CI,
строгий snapshot/conditional close, OS ownership и подтверждённый stop. Исправлены
гонка Bash после убийства sleep, cleanup app-server helpers и ESRCH при чтении
исчезающего процесса. Codex: 1516 passed / 36 skipped; installer 56 passed.
VM ACP-01..26 проверены; INT-01..07 — с адаптациями, INT-04/05 частично:
retry blocked подтверждён, оставшиеся процессы очищены независимо.
Python 86 passed, Claude targeted 790 passed / 10 skipped, Playwright Agent
settings прошёл. Детали — [codex-support.md](codex-support.md).


### 2026-11-03 — Продолжение OPEN после WAIT

При запуске через API исполнитель после ошибки и перехода своей задачи в WAIT
создаёт отдельный процесс для следующей доступной OPEN. Новый процесс атомарно
выбирает задачу по sort_order, id; цепочка ошибок больше не требует повторного
POST или consumer. Сохраняются диагностика WAIT, одна активная сессия, защита
при смене статуса/владельца и глобальная блокировка неподтверждённой остановки.
SIGTERM/KeyboardInterrupt не создают преемника. Ошибка запуска преемника
оставляет исходный результат задачи без изменений. Consumer сохраняет
последовательный запуск и настроенный интервал.

Прочитаны AGENTS.md, tech-spec.md, agent.md, board.md, main_ui.md, task-chat.md,
agent-integration-tests.md и относящиеся записи developer-log.md; просмотрены
board-task-block.png, task-edit-create-form.png и task-chat-status.png.
Обновлены agent.md и board.md. UI не менялся.

В обновлённом dev-venv успешно выполнены 217 различных Python-проверок:
worker/API/recovery/start/executor scope, Init/ACP/отмена/чат/ресурсы SQLite,
включая 14 проверок новой очереди (прогоны 61, 151 и 14, с повтором 9).
Playwright wait_queue_ui.js и board_live.js прошли на изолированных БД:
один POST, автоматический переход WAIT → запуск следующей OPEN,
SSE и прежние сценарии Board. Проверки браузера функциональные;
SSH заменён локальным транспортом, рабочая БД не использовалась.
Временные тестовые каталоги удалены; новые регрессии добавлены в git.


### 2026-11-03 — Ревью продолжения очереди после WAIT

Ревью bc32cf4 выявило две ошибки исполнения и опасную очистку тестов:

- P1: прерывание в обработчике ошибки после фиксации WAIT выполняло finally
  и запускало следующую задачу, несмотря на остановку исполнителя. Продолжение
  перенесено после обработки результата; исключение из сохранения ошибки
  не запускает преемника. Проверены Init/ACP, KeyboardInterrupt/SystemExit,
  ошибка SQLite и настоящий SIGTERM сразу после commit.
- P1: проверка launcher до Popen не защищала от запоздавшего старта ребёнка.
  Пока ребёнок запускался, consumer мог захватить и завершить следующую задачу;
  ребёнок затем самовольно брал ещё одну OPEN. Новый --after-wait передаёт
  исходный run_id в атомарный claim. Проверяются WAIT, владелец и отсутствие
  любого последующего захвата по порядку вставки agent_runs, независимо
  от перевода системных часов. Старое продолжение завершается без SSH.
- Очистка тестов могла послать SIGTERM самому pytest либо чужому или
  переиспользованному PID. Теперь она проверяет владельца/токен процесса,
  отключает ожидающие продолжения и останавливает собственные процессы
  до удаления временной БД. Это также применяется к fixture Playwright.

Новые регрессии воспроизвели обе ошибки на коде исходного коммита.
Прочитаны AGENTS.md, tech-spec.md, agent.md, board.md и относящиеся записи
журнала; обновлены agent.md и board.md. UI не менялся, браузерные проверки
функциональные. Рабочая БД и удалённый SSH-сервер не использовались.

Проверки после исправлений: полный Python-набор в обновлённом dev-venv
с -W error — 610 passed, 1 skipped (212,53 с), включая 25 тестов очереди.
Playwright wait_queue_ui.js и board_live.js прошли на изолированных БД.
Временные каталоги четырёх прогонов и собственные тестовые логи удалены.
