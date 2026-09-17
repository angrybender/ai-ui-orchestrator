"""Validate Init without executing it; use the same SFTP path resolver as runtime."""
import stat
import paramiko
from settings.config import Config
from settings.validation.remote_server import _split_host
from task_init import resolve_script_path


def validate(values):
    path = values.get('init_script_path', '')
    text = values.get('init_script_text', '')
    if not isinstance(path, str) or not isinstance(text, str):
        return ['Init script path and text must be strings']
    if path.strip() and text.strip():
        return ['init_script_path: specify either path or text, not both']
    if not path.strip():
        return []
    ssh = sftp = None
    try:
        config = Config.snapshot(('remote_server.host', 'remote_server.username',
                                  'remote_server.password', 'remote_server.ssh_key'))
        host, port = _split_host(config['remote_server.host'])
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        key = config.get('remote_server.ssh_key')
        auth = {'key_filename': key} if key else {'password': config.get('remote_server.password')}
        ssh.connect(hostname=host, port=port, username=config['remote_server.username'],
                    **auth, allow_agent=False, look_for_keys=False, timeout=10,
                    banner_timeout=10, auth_timeout=10)
        sftp = ssh.open_sftp()
        sftp.get_channel().settimeout(10)
        if not stat.S_ISREG(sftp.stat(resolve_script_path(sftp, path)).st_mode):
            return ['init_script_path: remote path is not a file']
    except Exception:
        return ['init_script_path: remote server unavailable or file not found']
    finally:
        if sftp is not None:
            sftp.close()
        if ssh is not None:
            ssh.close()
    return []
