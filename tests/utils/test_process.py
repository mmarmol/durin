"""The shared pid liveness probe: durin.utils.process.pid_alive.

POSIX semantics are exercised with real processes and real ``os.kill`` calls;
the win32 branch is exercised as control flow only (see TestWin32ControlFlow's
docstring for why), plus one pin that the jobs subsystem and the gateway
daemon resolve to the very same probe object.
"""

import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from durin.utils.process import pid_alive

# ---------------------------------------------------------------------------
# POSIX semantics -- real calls wherever a real process can supply the answer.
# ---------------------------------------------------------------------------


def test_alive_for_the_current_process():
    assert pid_alive(os.getpid()) is True


def test_dead_for_a_reaped_process():
    # Spawn and wait on a child so its pid is deterministically dead, rather
    # than guessing an arbitrary "probably free" integer.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    assert pid_alive(proc.pid) is False


def test_alive_for_a_pid_owned_by_another_user(monkeypatch):
    # os.kill raises PermissionError when the pid exists but is owned by a
    # different uid -- that process is alive, just not ours to signal.
    def _raise_permission(pid, sig):
        raise PermissionError("not our process")

    monkeypatch.setattr("durin.utils.process.os.kill", _raise_permission)
    assert pid_alive(4242) is True


def test_dead_when_no_such_process(monkeypatch):
    def _raise_lookup(pid, sig):
        raise ProcessLookupError("no such process")

    monkeypatch.setattr("durin.utils.process.os.kill", _raise_lookup)
    assert pid_alive(4242) is False


def test_dead_on_any_other_oserror(monkeypatch):
    # Totalized on purpose: for a liveness question, an answer the kernel
    # will not give reads as "dead", it never escapes as an exception.
    def _raise_exotic(pid, sig):
        raise OSError("kernel said something exotic")

    monkeypatch.setattr("durin.utils.process.os.kill", _raise_exotic)
    assert pid_alive(4242) is False


# ---------------------------------------------------------------------------
# win32 branch -- control flow pinned with sys.platform and ctypes faked.
# ---------------------------------------------------------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_STILL_ACTIVE = 259


class _Kernel32:
    """Records the OpenProcess / GetExitCodeProcess / CloseHandle traffic."""

    def __init__(self, *, handle, last_error=0, exit_code=0, exit_code_ok=True):
        self._handle = handle
        self._last_error = last_error
        self._exit_code = exit_code
        self._exit_code_ok = exit_code_ok
        self.open_calls = []
        self.closed = []

    def OpenProcess(self, access, inherit, pid):  # noqa: N802 - WinAPI name
        self.open_calls.append((access, inherit, pid))
        return self._handle

    def GetLastError(self):  # noqa: N802 - WinAPI name
        return self._last_error

    def GetExitCodeProcess(self, handle, exit_code_ref):  # noqa: N802 - WinAPI name
        if not self._exit_code_ok:
            return 0  # failure leaves the output DWORD untouched
        exit_code_ref.value = self._exit_code
        return 1

    def CloseHandle(self, handle):  # noqa: N802 - WinAPI name
        self.closed.append(handle)
        return 1


class _FakeCtypes:
    """Just enough of the ctypes surface the win32 branch touches."""

    class c_ulong:
        def __init__(self, value=0):
            self.value = value

    def __init__(self, kernel32):
        self.windll = SimpleNamespace(kernel32=kernel32)

    @staticmethod
    def byref(obj):
        return obj


def _probe_on_win32(kernel32, pid=1234):
    """Run pid_alive as if on win32: sys.platform patched, ctypes replaced,
    and os booby-trapped -- the win32 branch must never reach for os.kill,
    because "signal 0" on Windows is CTRL_C_EVENT, not a probe."""

    def _forbidden_kill(*_a, **_kw):
        raise AssertionError("pid_alive must not touch os.kill on win32")

    with patch("durin.utils.process.sys") as m_sys, \
         patch("durin.utils.process.ctypes", _FakeCtypes(kernel32)), \
         patch("durin.utils.process.os", SimpleNamespace(kill=_forbidden_kill)):
        m_sys.platform = "win32"
        return pid_alive(pid)


class TestWin32ControlFlow:
    """Pins the win32 branch's control flow against a faked API surface.

    Real WinAPI semantics are untestable in this environment -- there is no
    Windows kernel here to answer OpenProcess or GetExitCodeProcess. These
    tests pin only what the code does with each documented API answer, not
    what Windows itself would answer.
    """

    def test_null_handle_with_access_denied_means_alive(self):
        # The process exists but is not ours to open -- alive, the same
        # answer PermissionError gets on POSIX.
        k32 = _Kernel32(handle=0, last_error=_ERROR_ACCESS_DENIED)
        assert _probe_on_win32(k32) is True
        assert k32.closed == []  # no handle was obtained, nothing to close

    def test_null_handle_with_any_other_error_means_dead(self):
        k32 = _Kernel32(handle=0, last_error=_ERROR_INVALID_PARAMETER)
        assert _probe_on_win32(k32) is False
        assert k32.closed == []

    def test_open_handle_with_still_active_means_alive(self):
        k32 = _Kernel32(handle=99, exit_code=_STILL_ACTIVE)
        assert _probe_on_win32(k32) is True
        assert k32.closed == [99]  # closed even on the True path

    def test_open_handle_with_a_real_exit_code_means_dead(self):
        k32 = _Kernel32(handle=99, exit_code=0)
        assert _probe_on_win32(k32) is False
        assert k32.closed == [99]

    def test_open_handle_with_a_failed_exit_code_read_means_dead(self):
        # GetExitCodeProcess returning FALSE leaves the DWORD untouched; the
        # totalized read is "dead", and the handle still gets closed.
        k32 = _Kernel32(handle=99, exit_code_ok=False)
        assert _probe_on_win32(k32) is False
        assert k32.closed == [99]

    def test_the_probe_asks_for_query_limited_information_only(self):
        # PROCESS_QUERY_LIMITED_INFORMATION is the narrowest right that can
        # answer "alive?" -- it works across elevation boundaries where the
        # broader PROCESS_QUERY_INFORMATION would be denied.
        k32 = _Kernel32(handle=99, exit_code=_STILL_ACTIVE)
        _probe_on_win32(k32, pid=4321)
        assert k32.open_calls == [(_PROCESS_QUERY_LIMITED_INFORMATION, False, 4321)]


# ---------------------------------------------------------------------------
# Anti-divergence pin -- this probe existed twice once, and the copies drifted.
# ---------------------------------------------------------------------------


def test_jobs_and_daemon_resolve_to_the_same_probe_object():
    """The jobs subsystem and the gateway daemon each carried a private copy
    of this probe, and their semantics drifted apart. Both must resolve to
    the very same function object -- not merely equivalent ones -- so a future
    edit lands on everyone or on no one."""
    from durin.cli import gateway_daemon
    from durin.jobs import ocr_worker

    assert ocr_worker.pid_alive is pid_alive
    assert gateway_daemon.pid_alive is pid_alive
