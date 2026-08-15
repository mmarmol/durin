"""Process liveness probing, shared across subsystems.

The single home for "does this pid belong to a live process?". The jobs
registry's reconcile sweeps and the gateway daemon's status/stop flows all
import this one probe, so their answers cannot drift apart -- they did once,
when each carried its own copy.
"""

from __future__ import annotations

import ctypes
import os
import sys

__all__ = ["pid_alive"]

# Win32 constants, named as the API names them.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_STILL_ACTIVE = 259

# Cache for _get_kernel32 -- built on first use, which only ever happens on
# win32, so POSIX never loads the DLL (ctypes.WinDLL does not exist there).
_kernel32 = None


def _get_kernel32() -> ctypes.WinDLL:
    """The probe's private kernel32 binding, built on first use (win32 only).

    A private ``WinDLL`` with ``use_last_error=True`` instead of the shared
    ``ctypes.windll.kernel32``, because the failure code behind a NULL
    OpenProcess handle must be captured atomically with the failing call.
    Reading it afterwards with a ``GetLastError()`` call through the shared
    binding races everything that can touch the thread's LastError in
    between: ctypes drops and reacquires the GIL around every foreign call,
    and the GIL handoff plus whatever Python the interpreter interleaves on
    this thread (a GC pass runs arbitrary finalizers) may issue Win32 calls
    of their own; the first ``kernel32.GetLastError`` attribute lookup
    itself resolves the export through a *successful* GetProcAddress, which
    can overwrite the pending code before it is read; and ``ctypes.windll``
    hands every module in the process the same cached binding, so it can
    never safely carry per-caller prototypes or error-capture settings.
    With ``use_last_error=True`` ctypes saves LastError into thread-local
    storage as part of each call made through this binding, and
    ``ctypes.get_last_error()`` returns that snapshot -- no window, and
    nothing shared.

    The prototypes exist for 64-bit correctness: without an explicit
    ``c_void_p`` restype, ctypes applies its default ``c_int`` restype and
    truncates OpenProcess handles to 32 bits. On Windows ``c_ulong`` and
    ``c_long`` are exactly DWORD and BOOL (its C long is 32-bit).
    """
    global _kernel32
    if _kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            ctypes.c_ulong,  # DWORD dwDesiredAccess
            ctypes.c_long,  # BOOL bInheritHandle
            ctypes.c_ulong,  # DWORD dwProcessId
        )
        kernel32.OpenProcess.restype = ctypes.c_void_p  # HANDLE
        kernel32.GetExitCodeProcess.argtypes = (
            ctypes.c_void_p,  # HANDLE hProcess
            ctypes.POINTER(ctypes.c_ulong),  # LPDWORD lpExitCode
        )
        kernel32.GetExitCodeProcess.restype = ctypes.c_long  # BOOL
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)  # HANDLE hObject
        kernel32.CloseHandle.restype = ctypes.c_long  # BOOL
        _kernel32 = kernel32
    return _kernel32


def pid_alive(pid: int) -> bool:
    """Best-effort liveness probe: True iff *pid* names a live process.

    Totalized on purpose: every outcome is an answer, never an exception.
    A pid that exists but is not ours to inspect reads as alive; an answer
    the OS refuses to give reads as dead, and so does a pid too large for
    the OS to represent -- no live process can have it.
    """
    if sys.platform == "win32":
        # os.kill is not a probe on Windows. Signal 0 there is CTRL_C_EVENT
        # (they share the value), which CPython routes through
        # GenerateConsoleCtrlEvent -- that fails for any pid that is not a
        # console-group leader, and on CPython 3.11.0-3.13.0 (gh-58689) the
        # failure falls through to TerminateProcess: the "probe" kills the
        # probed process. Ask the process object itself instead.
        if pid < 0 or pid > 0xFFFFFFFF:
            # Windows pids are DWORDs; nothing live is named by a pid
            # outside that range. Checked here because ctypes does not do
            # it for us: an int wider than a declared 32-bit argtype is
            # masked to its low bits at the call boundary, not rejected,
            # and the masked value would probe whatever process happens to
            # own the truncated pid.
            return False
        kernel32 = _get_kernel32()
        try:
            handle = kernel32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
        except ctypes.ArgumentError:
            # ctypes refused to marshal the arguments, so no call was made
            # (e.g. a pid that is not an int at all). An argument the API
            # can never receive names nothing alive.
            return False
        if not handle:
            # ACCESS_DENIED means the process exists but is not ours to open
            # -- alive, the same answer PermissionError gets below. Any other
            # error (typically ERROR_INVALID_PARAMETER) means no such pid.
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            exit_code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            # STILL_ACTIVE is an exit-code sentinel, not a status flag: a
            # process that deliberately exits with code 259 reads as alive
            # here. Accepted -- the API's own documentation forbids exiting
            # with that code, and the alternative (WaitForSingleObject) costs
            # an extra syscall for no gain on a yes/no question. A failed
            # GetExitCodeProcess call reads as dead, per the totalized
            # contract above.
            return bool(ok) and exit_code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    # POSIX: signal 0 sends nothing -- it only checks that *pid* exists and
    # is signalable. PermissionError means the process exists but is owned
    # by another uid: alive, just not ours to signal. Any other OSError
    # (ProcessLookupError first among them) reads as dead, and so does the
    # OverflowError os.kill raises for a pid wider than the platform's pid
    # type (reachable through a corrupted pid file): no live process has
    # such a pid.
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OverflowError, OSError):
        return False
    return True
