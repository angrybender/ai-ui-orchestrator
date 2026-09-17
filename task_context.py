"""Remote task workspace and exclusive requirements directory preparation."""
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
