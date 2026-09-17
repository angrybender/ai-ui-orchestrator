"""Isolated, bounded Bash initialization over SSH (never part of ACP)."""
from __future__ import annotations

import posixpath
import re
import shlex
import threading
import time

LIMIT = 64 * 1024


class InitError(RuntimeError):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def resolve_script_path(sftp, path):
    path = path.strip()
    if '\0' in path:
        raise ValueError('Invalid script path')
    if path.startswith('/'):
        return path
    home = sftp.normalize('.')
    return posixpath.join(home, path[2:] if path.startswith('~/') else path)


class Output:
    def __init__(self, sanitize):
        self.data = bytearray()
        self.truncated = False
        self.sanitize = sanitize

    def append(self, chunk):
        remaining = LIMIT - len(self.data)
        self.data.extend(chunk[:remaining])
        self.truncated |= len(chunk) > remaining

    def text(self):
        # Sanitize the entire retained prefix, including a partial secret at EOF.
        text = self.sanitize(bytes(self.data).decode('utf-8', 'replace'))
        encoded = text.encode('utf-8')
        marker = '\n[output truncated at 64 KiB]'
        if self.truncated or len(encoded) > LIMIT:
            budget = LIMIT - len(marker.encode())
            if len(encoded) > budget:
                text = encoded[:budget].decode('utf-8', 'ignore')
                # Do not leave a chopped redaction marker at the display boundary.
                opening = text.rfind('[REDACTED]')
                partial = text.rfind('[')
                if partial > opening and '[REDACTED]'.startswith(text[partial:]):
                    text = text[:partial]
                # Retain an explicit redaction note when truncation removed it.
                if '[REDACTED]' in encoded.decode('utf-8') and '[REDACTED]' not in text:
                    text = text.encode()[:budget - len('[REDACTED]')].decode('utf-8', 'ignore') + '[REDACTED]'
            return text + marker
        return text


def stop_group(transport, pid):
    """Use a separate control channel and confirm absence, never trust close()."""
    if not pid:
        return False
    control = None
    try:
        control = transport.open_session(timeout=5)
        control.settimeout(5)
        # pid is parsed as a positive integer from our supervisor handshake.
        command = (f"kill -TERM -- -{pid} 2>/dev/null; "
                   f"sleep 0.1; kill -KILL -- -{pid} 2>/dev/null; "
                   f"for n in 1 2 3 4 5 6 7 8 9 10; do "
                   f"kill -0 -- -{pid} 2>/dev/null || exit 0; "
                   f"states=$(ps -eo pgid=,stat=) || exit 1; "
                   f"living=$(printf '%s\\n' \"$states\" | awk '$1 == {pid} && $2 !~ /^Z/ {{ found=1 }} END {{ print found+0 }}') || exit 1; "
                   f'[ "$living" = 0 ] && exit 0; '
                   f"sleep 0.1; done; exit 1")
        acknowledged = threading.Event()
        failed = []
        def launch_control():
            try:
                control.exec_command('bash -c ' + shlex.quote(command))
            except Exception:
                failed.append(True)
            finally:
                acknowledged.set()
        launcher = threading.Thread(target=launch_control, daemon=True)
        launcher.start()
        if not acknowledged.wait(5) or failed:
            return False
        deadline = time.monotonic() + 5
        while not control.exit_status_ready():
            if time.monotonic() >= deadline or not transport.is_active():
                return False
            time.sleep(0.02)
        return control.recv_exit_status() == 0
    except Exception:
        return False
    finally:
        if control is not None:
            control.close()


def run_init(transport, sftp, cwd, config, *, sanitize, active, on_pid, on_log):
    path = config.get('tasks.init_script_path') or ''
    text = config.get('tasks.init_script_text') or ''
    if not isinstance(path, str) or not isinstance(text, str) or (path.strip() and text.strip()):
        raise InitError('Init script error: invalid script configuration')
    if not path.strip() and not text.strip():
        return
    timeout = config.get('tasks.init_script_timeout')
    if timeout is None:
        timeout = 300
    if type(timeout) is not int or timeout <= 0:
        raise InitError('Init script error: invalid timeout')
    channel = None
    pid = None
    stdout, stderr = Output(sanitize), Output(sanitize)
    sender = None
    failed = []
    complete = threading.Event()
    started = False
    try:
        script = ('bash -- ' + shlex.quote(resolve_script_path(sftp, path))) if path.strip() else 'bash -s'
        # A handshake gate prevents any user code running before its process group
        # has been recorded. setsid gives Init and ordinary descendants one group.
        supervisor = ('printf "INIT_PID:%s\\n" "$$"; IFS= read -r gate; '
                      '[ "$gate" = GO ] || exit 125; exec ' + script)
        # SSH may start this command as a process-group leader, causing setsid
        # to fork. Wait for that child and propagate its actual exit status.
        command = f'cd -- {shlex.quote(cwd)} && exec setsid --wait bash -c {shlex.quote(supervisor)}'
        channel = transport.open_session(timeout=10)
        channel.settimeout(None)
        deadline = time.monotonic() + timeout

        def launch():
            try:
                channel.exec_command(command)
            except Exception:
                failed.append('unable to start Bash')
            finally:
                complete.set()

        sender = threading.Thread(target=launch, daemon=True)
        sender.start()
        handshake = bytearray()
        while True:
            if not active():
                raise InitError('Init script error: cancelled')
            if time.monotonic() >= deadline:
                raise InitError('Init script error: timed out')
            if failed:
                raise InitError('Init script error: ' + failed[0])
            if not transport.is_active():
                raise InitError('Init script error: SSH connection lost')
            if channel.recv_ready():
                chunk = channel.recv(32768)
                if pid is None:
                    handshake.extend(chunk)
                    if b'\n' in handshake:
                        line, rest = bytes(handshake).split(b'\n', 1)
                        if not re.fullmatch(rb'INIT_PID:[1-9][0-9]*', line):
                            raise InitError('Init script error: invalid startup handshake')
                        pid = int(line.split(b':')[1])
                        on_pid(pid)
                        stdout.append(rest)
                        sender.join(timeout=0.2)
                        complete.clear()

                        def send_script():
                            try:
                                channel.sendall(b'GO\n' + (text.encode('utf-8') if not path.strip() else b''))
                                channel.shutdown_write()
                            except Exception:
                                failed.append('SSH script transfer failed')
                            finally:
                                complete.set()

                        started = True
                        sender = threading.Thread(target=send_script, daemon=True)
                        sender.start()
                    elif len(handshake) > 128:
                        raise InitError('Init script error: invalid startup handshake')
                else:
                    stdout.append(chunk)
            if channel.recv_stderr_ready():
                stderr.append(channel.recv_stderr(32768))
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                code = channel.recv_exit_status()
                if code == 0 and started and not failed:
                    if complete.is_set():
                        return
                    time.sleep(0.01)
                    continue
                if code < 0:
                    raise InitError('Init script error: SSH connection lost')
                if code == 0 and not started:
                    raise InitError('Init script error: process exited before startup handshake')
                diagnostic = stderr.text().strip() or stdout.text().strip() or str(code)
                raise InitError('Init script error: ' + diagnostic)
            time.sleep(0.01)
    except BaseException as error:
        confirmed = stop_group(transport, pid) if pid else not started
        if isinstance(error, InitError):
            error.uncertain = not confirmed
            diagnostic = stderr.text().strip() or stdout.text().strip()
            if diagnostic and diagnostic not in str(error):
                error.args = (str(error) + '\n' + diagnostic,)
            raise
        raise InitError('Init script error: unable to start or transport interrupted', uncertain=not confirmed) from None
    finally:
        if channel is not None:
            channel.close()
        if sender is not None:
            sender.join(timeout=0.2)
        for name, output in (('stdout', stdout), ('stderr', stderr)):
            value = output.text()
            if value:
                on_log(f'Init {name}:\n{value}\n')
