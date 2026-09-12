#!/usr/bin/env python3
"""
Запуск ACP-агента на удаленной машине через SSH.

Требования:
    Python 3.12+
    pip install "paramiko>=3.4"

Примеры:
    python ssh_acp.py "Изучи проект и исправь ошибки"

    cat prompt.txt | python ssh_acp.py
"""

from __future__ import annotations

import argparse
import codecs
import json
import shlex
import sys
import threading
from typing import Any

import paramiko


# ============================================================================
# Параметры подключения — заполните при интеграции
# ============================================================================

SSH_HOST = "192.168.56.101"
SSH_PORT = 2222
SSH_USER = "user"
SSH_PASSWORD = "123"

# Абсолютный путь к рабочему каталогу на УДАЛЕННОЙ машине.
REMOTE_CWD = "/home/user/app/"

# При необходимости укажите абсолютный путь к исполняемому файлу.
AGENT_BIN = "qwen"

CONNECT_TIMEOUT = 20
ACP_PROTOCOL_VERSION = 1


# ============================================================================
# Исключения
# ============================================================================


class ACPError(RuntimeError):
    """Ошибка взаимодействия с ACP."""


# ============================================================================
# Отображение сессии
# ============================================================================


class SessionRenderer:
    """
    Человекочитаемый вывод событий ACP.

    Для tool_call* отображаются только title формата:

        Название: конкретное описание

    Например:

        Edit: tests/test_json_to_yml.py

    Заголовки без конкретного описания скрываются:

        Shell
        Edit
        Read
        Shell:
        пустая строка
    """

    def __init__(self) -> None:
        self.active_section: str | None = None
        self.line_open = False
        self.lock = threading.RLock()

    def finish_line(self) -> None:
        with self.lock:
            if self.line_open:
                print(flush=True)
                self.line_open = False

    def section(self, name: str) -> None:
        with self.lock:
            if self.active_section == name:
                return

            self.finish_line()

            print()
            print("═" * 72)
            print(f" {name}")
            print("═" * 72)

            self.active_section = name

    def stream_text(self, section_name: str, text: str) -> None:
        if not text:
            return

        with self.lock:
            self.section(section_name)

            print(text, end="", flush=True)
            self.line_open = not text.endswith("\n")

    def show(self, section_name: str, value: Any) -> None:
        with self.lock:
            self.section(section_name)
            self.finish_line()
            self._print_value(value)

    @staticmethod
    def _scalar(value: Any) -> str:
        if value is None:
            return "null"

        if value is True:
            return "true"

        if value is False:
            return "false"

        return str(value)

    def _print_value(self, value: Any, indent: int = 0) -> None:
        """
        Печатает dict/list/строки без JSON-экранирования.

        Важно: json.loads() уже преобразует настоящие JSON escape-
        последовательности \\n в переводы строк.
        """
        prefix = " " * indent

        if isinstance(value, dict):
            if not value:
                print(f"{prefix}{{}}", flush=True)
                return

            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    print(f"{prefix}{key}:", flush=True)
                    self._print_value(item, indent + 2)

                elif isinstance(item, str) and "\n" in item:
                    print(f"{prefix}{key}:", flush=True)
                    self._print_value(item, indent + 2)

                else:
                    print(
                        f"{prefix}{key}: {self._scalar(item)}",
                        flush=True,
                    )

            return

        if isinstance(value, list):
            if not value:
                print(f"{prefix}[]", flush=True)
                return

            for index, item in enumerate(value, start=1):
                print(f"{prefix}[{index}]:", flush=True)
                self._print_value(item, indent + 2)

            return

        for line in self._scalar(value).split("\n"):
            print(f"{prefix}{line}", flush=True)

    def render_tool_call(self, update: dict[str, Any]) -> None:
        """
        Выводит только tool_call-события, у которых title содержит
        конкретное описание.

        Разрешённый формат:

            Name: description

        Примеры:

            Edit: tests/test_json_to_yml.py
            Shell: pytest -q
            Read: src/config.py

        Скрываются:

            Shell
            Edit
            Read
            Shell:
            : description
            пустой или отсутствующий title
        """
        title = update.get("title")

        if not isinstance(title, str):
            return

        title = title.strip()

        if not title:
            return

        # Должна присутствовать двоеточие.
        name, separator, description = title.partition(":")

        # Слева и справа от двоеточия должны быть непустые значения.
        if (
            not separator
            or not name.strip()
            or not description.strip()
        ):
            return

        self.section("ИНСТРУМЕНТЫ")
        self.finish_line()

        print(title, flush=True)

    def render_event(self, update: dict[str, Any]) -> None:
        """
        Универсальный метод-враппер для отображения ACP-событий.

        Обрабатывает:
        - ответы агента;
        - рассуждения агента;
        - пользовательские сообщения;
        - план;
        - вызовы инструментов;
        - смену режима;
        - неизвестные ACP-события.
        """
        with self.lock:
            event = update.get("sessionUpdate")

            # Для любых событий tool_call* выводим только конкретный title.
            if isinstance(event, str) and event.startswith("tool_call"):
                self.render_tool_call(update)
                return

            text_sections = {
                "agent_message_chunk": "АГЕНТ",
                "agent_thought_chunk": "РАССУЖДЕНИЯ АГЕНТА",
                "user_message_chunk": "ПОЛЬЗОВАТЕЛЬ",
            }

            section_name = (
                text_sections.get(event)
                if isinstance(event, str)
                else None
            )

            if section_name is not None:
                content = update.get("content")

                if (
                    isinstance(content, dict)
                    and content.get("type") == "text"
                    and isinstance(content.get("text"), str)
                ):
                    self.stream_text(
                        section_name,
                        content["text"],
                    )
                else:
                    self.show(section_name, content)

                return

            if event == "plan":
                self.render_plan(update)
                return

            if event == "current_mode_update":
                self.show(
                    "РЕЖИМ АГЕНТА",
                    update.get("currentModeId", "unknown"),
                )
                return

            # Неизвестные события не пропускаем.
            self.show(
                f"ACP-СОБЫТИЕ: {event or 'unknown'}",
                {
                    key: value
                    for key, value in update.items()
                    if key != "sessionUpdate"
                },
            )

    def render_plan(self, update: dict[str, Any]) -> None:
        self.section("ПЛАН")
        self.finish_line()

        entries = update.get("entries")

        if not isinstance(entries, list):
            self._print_value(update)
            return

        markers = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
            "failed": "[!]",
        }

        for entry in entries:
            if not isinstance(entry, dict):
                self._print_value(entry)
                continue

            status = entry.get("status", "unknown")
            marker = markers.get(status, "[?]")
            content = str(entry.get("content", ""))

            lines = content.split("\n")

            print(f"{marker} {lines[0]}", flush=True)

            for line in lines[1:]:
                print(f"    {line}", flush=True)

        print(flush=True)


# ============================================================================
# ACP-клиент
# ============================================================================


class ACPClient:
    """
    Синхронный JSON-RPC 2.0 клиент поверх stdio.

    Ожидается формат JSON Lines / NDJSON:
    одно JSON-RPC сообщение на одну строку.
    """

    def __init__(
        self,
        stdin: Any,
        stdout: Any,
        renderer: SessionRenderer,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.renderer = renderer
        self.next_id = 0

    def send(self, message: dict[str, Any]) -> None:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"

        self.stdin.write(payload.encode("utf-8"))
        self.stdin.flush()

    def handle_notification(self, message: dict[str, Any]) -> None:
        if message.get("method") != "session/update":
            self.renderer.show("ACP-УВЕДОМЛЕНИЕ", message)
            return

        params = message.get("params")

        if not isinstance(params, dict):
            self.renderer.show(
                "НЕКОРРЕКТНОЕ ACP-СОБЫТИЕ",
                message,
            )
            return

        update = params.get("update")

        if not isinstance(update, dict):
            self.renderer.show(
                "НЕКОРРЕКТНОЕ ACP-СОБЫТИЕ",
                message,
            )
            return

        self.renderer.render_event(update)

    def handle_agent_request(self, message: dict[str, Any]) -> None:
        """
        Специальная обработка разрешений отсутствует.

        Любой встречный запрос агента считается неподдерживаемым.
        """
        self.send({
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {
                "code": -32601,
                "message": (
                    f"Client method not supported: "
                    f"{message.get('method')}"
                ),
            },
        })

    def request(
        self,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        self.next_id += 1
        request_id = self.next_id

        self.send({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        })

        while True:
            line = self.stdout.readline()

            if not line:
                raise ACPError(
                    f"Stdout агента закрыт до ответа на {method!r}."
                )

            if not line.strip():
                continue

            try:
                message = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise ACPError(
                    "Stdout агента содержит невалидный JSON. "
                    "В ACP-режиме должны передаваться JSON-RPC "
                    f"сообщения по одному на строку. Получено: "
                    f"{line[:500]!r}"
                ) from exc

            if (
                not isinstance(message, dict)
                or message.get("jsonrpc") != "2.0"
            ):
                raise ACPError(
                    f"Некорректное JSON-RPC сообщение: {message!r}"
                )

            # Уведомление агента.
            if "method" in message and "id" not in message:
                self.handle_notification(message)
                continue

            # Встречный запрос агента.
            if "method" in message and "id" in message:
                self.handle_agent_request(message)
                continue

            # Ответ на наш запрос.
            if message.get("id") != request_id:
                raise ACPError(
                    f"Неожиданный id ответа: {message.get('id')!r}; "
                    f"ожидался {request_id!r}."
                )

            if "error" in message:
                raise ACPError(
                    f"{method}: "
                    f"{json.dumps(message['error'], ensure_ascii=False)}"
                )

            if "result" not in message:
                raise ACPError(
                    f"Ответ без result/error: {message!r}"
                )

            return message["result"]


# ============================================================================
# SSH
# ============================================================================


def build_remote_command(prompt: str) -> str:
    """
    Формирует команду:

        cd REMOTE_CWD && exec agent -p PROMPT --acp --yolo

    shlex.join безопасно экранирует промпт для POSIX shell.
    """
    agent_command = shlex.join([
        AGENT_BIN,
        "-p",
        prompt,
        "--acp",
        "--yolo",
        "--output-format", "stream-json",
        "--model", "qwen/qwen3.8-flash",
    ])

    return (
        f"cd {shlex.quote(REMOTE_CWD)} "
        f"&& exec {agent_command}"
    )


def forward_stderr(
    channel: paramiko.Channel,
    renderer: SessionRenderer,
) -> None:
    """
    Выводит stderr агента отдельной секцией.

    Это важно, поскольку ACP использует stdout для протокола.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(
        errors="replace",
    )

    try:
        while True:
            chunk = channel.recv_stderr(4096)

            if not chunk:
                break

            text = decoder.decode(chunk)

            if text:
                renderer.stream_text("STDERR АГЕНТА", text)

    except (OSError, EOFError):
        pass

    finally:
        tail = decoder.decode(b"", final=True)

        if tail:
            renderer.stream_text("STDERR АГЕНТА", tail)


def run_agent(prompt: str) -> None:
    renderer = SessionRenderer()
    ssh = paramiko.SSHClient()

    # Не принимать неизвестные ключи сервера автоматически.
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())

    channel: paramiko.Channel | None = None
    stderr_thread: threading.Thread | None = None

    try:
        renderer.show(
            "ПОДКЛЮЧЕНИЕ",
            f"{SSH_USER}@{SSH_HOST}:{SSH_PORT}",
        )

        ssh.connect(
            hostname=SSH_HOST,
            port=SSH_PORT,
            username=SSH_USER,
            password=SSH_PASSWORD,
            look_for_keys=False,
            allow_agent=False,
            timeout=CONNECT_TIMEOUT,
            auth_timeout=CONNECT_TIMEOUT,
            banner_timeout=CONNECT_TIMEOUT,
        )

        transport = ssh.get_transport()

        if transport is None:
            raise ACPError("SSH transport не создан.")

        transport.set_keepalive(30)

        stdin, stdout, _stderr = ssh.exec_command(
            command=build_remote_command(prompt),
            get_pty=False,
        )

        channel = stdout.channel

        stderr_thread = threading.Thread(
            target=forward_stderr,
            args=(channel, renderer),
            daemon=True,
        )
        stderr_thread.start()

        client = ACPClient(
            stdin=stdin,
            stdout=stdout,
            renderer=renderer,
        )

        # --------------------------------------------------------------
        # initialize
        # --------------------------------------------------------------

        initialized = client.request(
            "initialize",
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "clientCapabilities": {},
                "clientInfo": {
                    "name": "python-ssh-acp-client",
                    "version": "1.0.0",
                },
            },
        )

        if not isinstance(initialized, dict):
            raise ACPError(
                f"Некорректный ответ initialize: {initialized!r}"
            )

        version = initialized.get("protocolVersion")

        if version != ACP_PROTOCOL_VERSION:
            raise ACPError(
                f"Неподдерживаемая версия ACP: {version!r}"
            )

        # --------------------------------------------------------------
        # session/new
        # --------------------------------------------------------------

        session = client.request(
            "session/new",
            {
                "cwd": REMOTE_CWD,
                "mcpServers": [],
            },
        )

        if not isinstance(session, dict):
            raise ACPError(
                f"Некорректный ответ session/new: {session!r}"
            )

        session_id = session.get("sessionId")

        if not isinstance(session_id, str) or not session_id:
            raise ACPError(
                "Ответ session/new не содержит sessionId."
            )

        renderer.show(
            "СЕССИЯ",
            {
                "ID": session_id,
                "Хост": SSH_HOST,
                "Рабочий каталог": REMOTE_CWD,
                "Параметры агента": "--acp --yolo",
            },
        )

        renderer.stream_text("ПОЛЬЗОВАТЕЛЬ", prompt)
        renderer.finish_line()

        # --------------------------------------------------------------
        # session/prompt
        # --------------------------------------------------------------

        result = client.request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            },
        )

        if not isinstance(result, dict):
            raise ACPError(
                f"Некорректный ответ session/prompt: {result!r}"
            )

        renderer.show(
            "ЗАВЕРШЕНИЕ",
            f"Причина остановки: "
            f"{result.get('stopReason', 'unknown')}",
        )

    finally:
        # Для одноразового запуска закрываем канал после ответа.
        if channel is not None:
            channel.close()

        ssh.close()

        if stderr_thread is not None:
            stderr_thread.join(timeout=2)

        renderer.finish_line()


# ============================================================================
# CLI
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Запуск ACP-агента через SSH с потоковым "
            "человекочитаемым выводом."
        ),
    )

    parser.add_argument(
        "prompt",
        nargs="?",
        help="Промпт; если не указан, читается из stdin.",
    )

    args = parser.parse_args()

    prompt = (
        args.prompt
        if args.prompt is not None
        else sys.stdin.read()
    )

    if not prompt.strip():
        parser.error("Промпт не должен быть пустым.")

    try:
        run_agent(prompt)
        return 0

    except KeyboardInterrupt:
        print("\nПрервано пользователем.", file=sys.stderr)
        return 130

    except Exception as exc:
        print(f"\nОшибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())