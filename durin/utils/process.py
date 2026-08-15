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


def pid_alive(pid: int) -> bool:
    """Best-effort liveness probe: True iff *pid* names a live process.

    Totalized on purpose: every outcome is an answer, never an exception.
    A pid that exists but is not ours to inspect reads as alive; an answer
    the OS refuses to give reads as dead.
    """
    if sys.platform == "win32":
        # os.kill is not a probe on Windows. Signal 0 there is CTRL_C_EVENT
        # (they share the value), which CPython routes through
        # GenerateConsoleCtrlEvent -- that fails for any pid that is not a
        # console-group leader, and on CPython 3.11.0-3.13.0 (gh-58689) the
        # failure falls through to TerminateProcess: the "probe" kills the
        # probed process. Ask the process object itself instead.
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            # ACCESS_DENIED means the process exists but is not ours to open
            # -- alive, the same answer PermissionError gets below. Any other
            # error (typically ERROR_INVALID_PARAMETER) means no such pid.
            return kernel32.GetLastError() == _ERROR_ACCESS_DENIED
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
    # (ProcessLookupError first among them) reads as dead.
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True
