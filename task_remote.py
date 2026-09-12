"""Remote task cleanup before permanent database deletion."""
import errno
from pathlib import Path
import posixpath
import re
import stat
import unicodedata

import paramiko

from agent_remote import CONNECT_TIMEOUT, RemoteAgentError, _close, _name


def delete_task_directory(task_id, config, *, allow_connection_unavailable=False):
    ssh = sftp = None
    try:
        host = config.get("remote_server.host")
        username = config.get("remote_server.username")
        base = config.get("tasks.base_dir")
        key = config.get("remote_server.ssh_key") or ""
        password = config.get("remote_server.password") or ""
        if not _name(task_id) or not all(isinstance(v, str) for v in (host, username, base, key, password)):
            raise ValueError
        if not host.strip() or not username.strip() or not base.startswith("/") or "\\" in base:
            raise ValueError
        if any(unicodedata.category(c).startswith("C") for c in host + username + base):
            raise ValueError
        if not (key or password) or (key and (not Path(key).is_absolute() or not Path(key).is_file())):
            raise ValueError
        port = 22
        if host.startswith("["):
            match = re.fullmatch(r"\[([^\[\]]+)\](?::([0-9]+))?", host)
            if not match:
                raise ValueError
            host, number = match.groups()
            port = int(number) if number else 22
        elif host.count(":") == 1:
            host, number = host.rsplit(":", 1)
            port = int(number)
        if not host or any(c.isspace() for c in host) or not 1 <= port <= 65535:
            raise ValueError
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        auth = {"key_filename": key} if key else {"password": password}
        try:
            ssh.connect(hostname=host, port=port, username=username, **auth,
                        allow_agent=False, look_for_keys=False, timeout=CONNECT_TIMEOUT,
                        auth_timeout=CONNECT_TIMEOUT, banner_timeout=CONNECT_TIMEOUT)
        except (OSError, EOFError, paramiko.SSHException):
            if allow_connection_unavailable:
                return
            raise
        ssh.get_transport().set_keepalive(30)
        sftp = ssh.open_sftp()
        sftp.get_channel().settimeout(CONNECT_TIMEOUT)
        try:
            base = posixpath.normpath(sftp.normalize(base))
        except OSError as error:
            if allow_connection_unavailable and error.errno == errno.ENOENT:
                return
            raise
        if not base.startswith("/"):
            raise ValueError
        target = posixpath.join(base, task_id)
        if posixpath.dirname(posixpath.normpath(target)) != base:
            raise ValueError
        try:
            mode = sftp.lstat(target).st_mode
        except OSError as error:
            if error.errno == errno.ENOENT:
                return
            raise
        if not stat.S_ISDIR(mode) or posixpath.normpath(sftp.normalize(target)) != target:
            raise ValueError

        def remove_directory(path):
            for entry in sftp.listdir_attr(path):
                if not _name(entry.filename):
                    raise ValueError
                child = posixpath.join(path, entry.filename)
                if stat.S_ISDIR(sftp.lstat(child).st_mode):
                    if posixpath.normpath(sftp.normalize(child)) != child:
                        raise ValueError
                    remove_directory(child)
                else:
                    sftp.remove(child)
            sftp.rmdir(path)

        remove_directory(target)
        try:
            sftp.lstat(target)
        except OSError as error:
            if error.errno == errno.ENOENT:
                return
            raise
        raise ValueError
    except Exception:
        raise RemoteAgentError("Не удалось подключиться к удалённой машине или удалить папку задачи. Задача сохранена в БД.") from None
    finally:
        _close(sftp)
        _close(ssh)
