"""Small, handle-based Windows process helpers (safe to import on other OSes)."""
from __future__ import annotations

import ctypes

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
ERROR_INVALID_PARAMETER = 87
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF

DWORD = ctypes.c_uint32
HANDLE = ctypes.c_void_p
BOOL = ctypes.c_int32


class FILETIME(ctypes.Structure):
    _fields_ = [('dwLowDateTime', DWORD), ('dwHighDateTime', DWORD)]


_kernel32 = None


def _api():
    global _kernel32
    if _kernel32 is None:
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
        api.OpenProcess.restype = HANDLE
        api.GetProcessTimes.argtypes = [HANDLE] + [ctypes.POINTER(FILETIME)] * 4
        api.GetProcessTimes.restype = BOOL
        api.WaitForSingleObject.argtypes = [HANDLE, DWORD]
        api.WaitForSingleObject.restype = DWORD
        api.TerminateProcess.argtypes = [HANDLE, ctypes.c_uint32]
        api.TerminateProcess.restype = BOOL
        api.CloseHandle.argtypes = [HANDLE]
        api.CloseHandle.restype = BOOL
        _kernel32 = api
    return _kernel32


def _error():
    return ctypes.WinError(ctypes.get_last_error())


def _open(api, pid, access):
    # Do not silently truncate a PID to DWORD and act on a different process.
    if not 0 <= pid <= 0xFFFFFFFF:
        raise OSError(ERROR_INVALID_PARAMETER, 'PID is outside the DWORD range')
    handle = api.OpenProcess(access, False, pid)
    if not handle:
        error = _error()
        if error.winerror != ERROR_INVALID_PARAMETER:
            raise error
    return handle


def _exited(api, handle, timeout=0):
    result = api.WaitForSingleObject(handle, timeout)
    if result == WAIT_OBJECT_0:
        return True
    if result == WAIT_TIMEOUT:
        return False
    if result == WAIT_FAILED:
        raise _error()
    raise OSError(f'Unexpected process wait result: {result}')


def _identity(api, handle):
    creation, exit_time, kernel, user = (FILETIME() for _ in range(4))
    if not api.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                               ctypes.byref(kernel), ctypes.byref(user)):
        raise _error()
    if _exited(api, handle):
        return None
    return str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)


def _close(api, handle):
    if not api.CloseHandle(handle):
        raise _error()


def process_identity(pid: int) -> str | None:
    """Return full creation FILETIME as decimal text; None means absent/exited.

    Access failures and other WinAPI errors raise OSError, never imply absence.
    """
    api = _api()
    handle = _open(api, pid, PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)
    if not handle:
        return None
    try:
        return _identity(api, handle)
    finally:
        _close(api, handle)


def terminate_process(pid: int, expected_token: str) -> bool:
    """Stop only the matching process, using one handle even if its PID is reused.

    True means the original is absent, exited, or has a different creation token.
    False means termination was requested but not confirmed within 4000 ms.
    WinAPI errors raise OSError.
    """
    api = _api()
    handle = _open(api, pid, PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)
    if not handle:
        return True
    try:
        token = _identity(api, handle)
        if token is None or token != expected_token:
            return True
        if not api.TerminateProcess(handle, 1):
            raise _error()
        return _exited(api, handle, 4000)
    finally:
        _close(api, handle)
