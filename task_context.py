"""Remote task workspace preparation and requirements synchronization."""
import io
import posixpath
import stat


PROMPT = (
    "Complete the task described in requirements-${TASK_ID}/TASK.md.\n"
    "Read the task requirements and all relevant attachments from requirements-${TASK_ID}/ before starting.\n"
    "Work only within the current task directory."
)


def task_prompt(task_id):
    return PROMPT.replace('${TASK_ID}', task_id)


def _directory(sftp, path):
    if (not stat.S_ISDIR(sftp.lstat(path).st_mode)
            or posixpath.normpath(sftp.normalize(path)) != path):
        raise ValueError('Unsafe task directory.')


def prepare_directory(sftp, cwd, task_id, reuse):
    """Reuse only validated directories; never overwrite a new task's context."""
    name = 'requirements-' + task_id
    path = posixpath.join(cwd, name)
    if not reuse:
        try:
            sftp.mkdir(cwd)
        except OSError:
            # Report a context collision explicitly, even if the parent exists too.
            _directory(sftp, cwd)
            try:
                sftp.lstat(path)
            except FileNotFoundError:
                raise ValueError(f'Task directory {task_id} already exists.') from None
            raise ValueError(f'Task directory {name} already exists.') from None
    else:
        _directory(sftp, cwd)
    if posixpath.normpath(sftp.normalize(cwd)) != cwd:
        raise ValueError('Unsafe task directory.')
    if reuse:
        _directory(sftp, path)
    else:
        try:
            sftp.mkdir(path)
        except OSError:
            try:
                sftp.lstat(path)
            except OSError:
                raise
            raise ValueError(f'Task directory {name} already exists.') from None
        if posixpath.normpath(sftp.normalize(path)) != path:
            raise ValueError('Unsafe task directory.')
    return path


def sync_context(sftp, path, text, attachments, reuse, check=lambda: None):
    """Mirror application-owned requirements files before Init or ACP starts."""
    existing = set()
    if reuse:
        for entry in sftp.listdir_attr(path):
            name = entry.filename
            if not name or name in ('.', '..') or '/' in name or '\\' in name:
                raise ValueError('Unsafe requirements file name.')
            if not stat.S_ISREG(sftp.lstat(posixpath.join(path, name)).st_mode):
                raise ValueError('Unsafe requirements file: ' + name)
            existing.add(name)

    expected = {'TASK.md'} | {name for _, name in attachments}

    def replace(name, upload):
        check()
        target = posixpath.join(path, name)
        if name in existing:
            # Unlink first so overwriting a hard link cannot change files outside
            # the requirements directory. Symlinks were rejected above.
            sftp.remove(target)
        upload(target)

    for local, name in attachments:
        replace(name, lambda target: sftp.put(str(local), target))
    for name in sorted(existing - expected):
        check()
        sftp.remove(posixpath.join(path, name))
    replace('TASK.md', lambda target: sftp.putfo(io.BytesIO(text.encode('utf-8')), target))
    check()
