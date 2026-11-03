# Исправление Codex ACP для RouterAI

Патч содержит commit `1b3232d` отдельного форка
`https://github.com/angrybender/acp-wrapper`, поверх `9524663`.

Он исправляет поиск настоящего PID для команд с кавычками после shell exec,
например `python3 -c '…'`. Сопоставляются только процессы с маркером этой сессии
и точным argv. Подстановки и pipeline не исполняются; неоднозначный PID
по-прежнему блокирует остановку. Включены регрессии с настоящими процессами.

Также исправлен тестовый клиент `run-codex`: актуальный метод авторизации,
типы событий и освобождение соединения при ошибке.

Применение в checkout отдельного форка:

```bash
git checkout 9524663
git am /path/to/0001-quoted-native-processes.patch
cd codex
npm ci --ignore-scripts
npm run typecheck
npm test
npm run build
```

Установку выполнять существующим `install.py --agent codex` из корня форка
в отдельные `--prefix` и `--bin`. Ключ провайдера не включён в патч.

Проверено: typecheck, 1519 Vitest-тестов (36 skipped), реальные ACP-20 и ACP-23
на VM и INT-02 с выбранным `agent-full-access`. Полный результат,
отрицательные сценарии и ограничения: [отчёт](../../specs/routerai-live-results.json).
