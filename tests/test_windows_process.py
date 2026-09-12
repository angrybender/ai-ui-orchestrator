import ctypes
import os
import subprocess
import sys

import pytest

import windows_process as wp


class Function:
    """A fake ctypes function that also accepts ABI declarations."""
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class FakeAPI:
    def __init__(self):
        self.handle = 0x123456789ABC
        self.creation = 0x12345678ABCDEF01
        self.error = 5
        self.fail = None
        self.waits = [wp.WAIT_TIMEOUT, wp.WAIT_OBJECT_0]
        self.calls = []
        for name in ('OpenProcess', 'GetProcessTimes', 'WaitForSingleObject',
                     'TerminateProcess', 'CloseHandle'):
            setattr(self, name, Function(getattr(self, '_' + name)))

    def _OpenProcess(self, access, inherit, pid):
        self.calls.append(('open', access, inherit, pid))
        return 0 if self.fail == 'open' else self.handle

    def _GetProcessTimes(self, handle, creation, *rest):
        self.calls.append(('times', handle))
        assert handle == self.handle
        value = ctypes.cast(creation, ctypes.POINTER(wp.FILETIME)).contents
        value.dwLowDateTime = self.creation & 0xFFFFFFFF
        value.dwHighDateTime = self.creation >> 32
        return self.fail != 'times'

    def _WaitForSingleObject(self, handle, timeout):
        self.calls.append(('wait', handle, timeout))
        assert handle == self.handle
        return self.waits.pop(0)

    def _TerminateProcess(self, handle, code):
        self.calls.append(('terminate', handle, code))
        assert handle == self.handle
        return self.fail != 'terminate'

    def _CloseHandle(self, handle):
        self.calls.append(('close', handle))
        assert handle == self.handle
        return self.fail != 'close'


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    monkeypatch.setattr(wp, '_kernel32', None)
    monkeypatch.setattr(ctypes, 'WinDLL', lambda name, **kw: fake, raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: fake.error, raising=False)

    def win_error(code):
        error = OSError(code, 'Fake WinAPI error')
        error.winerror = code
        return error

    monkeypatch.setattr(ctypes, 'WinError', win_error, raising=False)
    return fake


def test_identity_full_filetime_and_abi(api):
    assert wp.process_identity(42) == str(api.creation)
    assert api.calls == [('open', 0x101000, False, 42), ('times', api.handle),
                         ('wait', api.handle, 0), ('close', api.handle)]
    assert api.OpenProcess.argtypes == [wp.DWORD, wp.BOOL, wp.DWORD]
    assert api.OpenProcess.restype is ctypes.c_void_p
    assert api.GetProcessTimes.argtypes == [wp.HANDLE] + [ctypes.POINTER(wp.FILETIME)] * 4
    assert api.GetProcessTimes.restype is wp.BOOL
    assert api.WaitForSingleObject.argtypes == [wp.HANDLE, wp.DWORD]
    assert api.WaitForSingleObject.restype is wp.DWORD
    assert api.TerminateProcess.argtypes == [wp.HANDLE, wp.DWORD]
    assert api.TerminateProcess.restype is wp.BOOL
    assert api.CloseHandle.argtypes == [wp.HANDLE]
    assert api.CloseHandle.restype is wp.BOOL
    assert ctypes.sizeof(wp.FILETIME) == 8


@pytest.mark.parametrize('terminate', [False, True])
@pytest.mark.parametrize('code', [87, 5, 6, 123])
def test_open_errors(api, terminate, code):
    api.fail, api.error = 'open', code
    call = lambda: wp.terminate_process(42, str(api.creation)) if terminate else wp.process_identity(42)
    if code == 87:
        assert call() is (True if terminate else None)
    else:
        with pytest.raises(OSError) as caught:
            call()
        assert caught.value.winerror == code
    assert len(api.calls) == 1


@pytest.mark.parametrize('terminate', [False, True])
def test_exited_handle(api, terminate):
    api.waits = [wp.WAIT_OBJECT_0]
    result = wp.terminate_process(42, str(api.creation)) if terminate else wp.process_identity(42)
    assert result is (True if terminate else None)
    assert not any(call[0] == 'terminate' for call in api.calls)
    assert api.calls[-1] == ('close', api.handle)


def test_mismatched_identity_does_not_kill_reused_pid(api):
    assert wp.terminate_process(42, str(api.creation + 1)) is True
    assert not any(call[0] == 'terminate' for call in api.calls)
    assert api.calls[-1] == ('close', api.handle)


@pytest.mark.parametrize('wait,expected', [(wp.WAIT_OBJECT_0, True), (wp.WAIT_TIMEOUT, False)])
def test_terminate_uses_same_handle_and_bounded_wait(api, wait, expected, monkeypatch):
    monkeypatch.setattr(wp, 'process_identity', lambda pid: pytest.fail('Must use the opened handle'))
    api.waits = [wp.WAIT_TIMEOUT, wait]
    assert wp.terminate_process(42, str(api.creation)) is expected
    assert api.calls == [('open', 0x101001, False, 42), ('times', api.handle),
                         ('wait', api.handle, 0), ('terminate', api.handle, 1),
                         ('wait', api.handle, 4000), ('close', api.handle)]


@pytest.mark.parametrize('failure', ['times', 'initial_wait', 'terminate', 'final_wait', 'close'])
def test_errors_raise_and_close_handle(api, failure):
    api.fail = failure
    if failure == 'initial_wait':
        api.waits = [wp.WAIT_FAILED]
    if failure == 'final_wait':
        api.waits = [wp.WAIT_TIMEOUT, wp.WAIT_FAILED]
    with pytest.raises(OSError):
        wp.terminate_process(42, str(api.creation))
    assert api.calls[-1] == ('close', api.handle)


@pytest.mark.parametrize('failure', ['times', 'wait', 'close'])
def test_identity_errors_raise_and_close_handle(api, failure):
    api.fail = failure
    if failure == 'wait':
        api.waits = [wp.WAIT_FAILED]
    with pytest.raises(OSError):
        wp.process_identity(42)
    assert api.calls[-1] == ('close', api.handle)


def test_unexpected_wait_is_error(api):
    api.waits = [128]
    with pytest.raises(OSError):
        wp.process_identity(42)
    assert api.calls[-1] == ('close', api.handle)


@pytest.mark.parametrize('pid', [-1, 0x100000000])
def test_pid_cannot_wrap(api, pid):
    with pytest.raises(OSError):
        wp.terminate_process(pid, 'token')
    assert not api.calls


@pytest.mark.skipif(sys.platform == 'win32', reason='Checks non-Windows lazy import')
def test_import_does_not_load_windows_dll():
    subprocess.run([sys.executable, '-c',
                    'import ctypes; ctypes.WinDLL = lambda *a, **k: 1/0; import windows_process'],
                   check=True, cwd=os.path.dirname(wp.__file__), timeout=10)


@pytest.mark.skipif(sys.platform != 'win32', reason='Requires real Windows WinAPI')
def test_real_windows_child():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        token = wp.process_identity(child.pid)
        assert token is not None and int(token) > 0xFFFFFFFF
        assert wp.process_identity(child.pid) == token
        assert wp.terminate_process(child.pid, str(int(token) + 1)) is True
        assert child.poll() is None
        assert wp.terminate_process(child.pid, token) is True
        child.wait(timeout=5)
        assert wp.process_identity(child.pid) is None
        assert wp.terminate_process(child.pid, token) is True
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
