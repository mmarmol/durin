"""The shared pid liveness probe: durin.utils.process.pid_alive.

POSIX semantics are exercised with real processes and real ``os.kill`` calls;
the win32 branch is exercised as control flow only (see TestWin32ControlFlow's
docstring for why), plus one pin that the jobs subsystem and the gateway
daemon resolve to the very same probe object.
"""

import ctypes
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


def test_dead_for_a_pid_too_wide_for_the_platform():
    # Real call, no mocks: os.kill raises OverflowError (not OSError) for a
    # pid wider than the platform's pid type -- reachable in production
    # through a corrupted pid file. The totalized contract holds: an
    # impossible pid is an answer ("dead"), never an exception.
    assert pid_alive(2**63) is False


# ---------------------------------------------------------------------------
# win32 branch -- control flow pinned with sys.platform and ctypes faked.
# ---------------------------------------------------------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_STILL_ACTIVE = 259


class _FakeFn:
    """A fake ctypes function pointer: callable and, like the real thing,
    accepting ``argtypes``/``restype`` assignment."""

    def __init__(self, fn):
        self._fn = fn
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._fn(*args)


class _Kernel32:
    """Records the OpenProcess / GetExitCodeProcess / CloseHandle traffic.

    Deliberately exposes no ``GetLastError``: failure codes must be read via
    ``ctypes.get_last_error()`` (the per-call thread-local snapshot), so any
    regression to calling ``kernel32.GetLastError()`` directly dies here
    with an AttributeError instead of passing by luck.
    """

    def __init__(
        self, *, handle, last_error=0, exit_code=0, exit_code_ok=True, open_raises=None
    ):
        self._handle = handle
        self._last_error = last_error
        self._exit_code = exit_code
        self._exit_code_ok = exit_code_ok
        self._open_raises = open_raises
        self.open_calls = []
        self.closed = []
        self.OpenProcess = _FakeFn(self._open_process)
        self.GetExitCodeProcess = _FakeFn(self._get_exit_code)
        self.CloseHandle = _FakeFn(self._close_handle)

    def _open_process(self, access, inherit, pid):
        if self._open_raises is not None:
            raise self._open_raises  # marshaling refused: nothing was called
        self.open_calls.append((access, inherit, pid))
        return self._handle

    def _get_exit_code(self, handle, exit_code_ref):
        if not self._exit_code_ok:
            return 0  # failure leaves the output DWORD untouched
        exit_code_ref.value = self._exit_code
        return 1

    def _close_handle(self, handle):
        self.closed.append(handle)
        return 1


class _FakeCtypes:
    """Just enough of the ctypes surface the win32 branch touches.

    Deliberately exposes no ``windll``: the probe must build its own private
    ``WinDLL(..., use_last_error=True)`` binding rather than reach into the
    process-global ``ctypes.windll`` cache, so any regression back to the
    shared binding dies here with an AttributeError.
    """

    ArgumentError = ctypes.ArgumentError  # the real class, so except-clauses match

    class c_ulong:  # DWORD: prototype marker and output cell
        def __init__(self, value=0):
            self.value = value

    class c_long:  # BOOL prototype marker
        pass

    class c_void_p:  # HANDLE prototype marker
        pass

    def __init__(self, kernel32):
        self._kernel32 = kernel32

    def WinDLL(self, name, *, use_last_error=False):
        assert name == "kernel32"
        assert use_last_error is True, "kernel32 must be bound with use_last_error=True"
        return self._kernel32

    def get_last_error(self):
        return self._kernel32._last_error

    @staticmethod
    def byref(obj):
        return obj

    @staticmethod
    def POINTER(ctype):
        return ("POINTER", ctype)  # opaque marker, only ever stored in argtypes


def _probe_on_win32(kernel32, pid=1234):
    """Run pid_alive as if on win32: sys.platform patched, ctypes replaced,
    the module-level binding cache cleared, and os booby-trapped -- the win32
    branch must never reach for os.kill, because "signal 0" on Windows is
    CTRL_C_EVENT, not a probe."""

    def _forbidden_kill(*_a, **_kw):
        raise AssertionError("pid_alive must not touch os.kill on win32")

    # create=True keeps the cache patch working even against a module shape
    # that lacks it, so a regression fails on the pattern pins above rather
    # than on patch setup.
    with patch("durin.utils.process.sys") as m_sys, \
         patch("durin.utils.process.ctypes", _FakeCtypes(kernel32)), \
         patch("durin.utils.process._kernel32", None, create=True), \
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

    def test_dead_when_marshaling_rejects_the_arguments(self):
        # ctypes.ArgumentError from OpenProcess means marshaling refused the
        # arguments and no call was made (e.g. a pid that is not an int at
        # all). Contained to "dead": an argument the API can never receive
        # names nothing alive.
        k32 = _Kernel32(
            handle=99, open_raises=ctypes.ArgumentError("argument 3: wrong type")
        )
        assert _probe_on_win32(k32) is False
        assert k32.closed == []  # no call, no handle, nothing to close

    def test_a_pid_wider_than_a_dword_is_never_probed(self):
        # Windows pids are DWORDs, and ctypes does NOT range-check ints
        # against declared argtypes -- an oversized int is masked to its low
        # 32 bits, not rejected -- so the probe must answer "dead" up front,
        # before the pid can be truncated into some other process's pid.
        # The fake would happily report alive if asked: reaching the API at
        # all is the bug this test pins.
        k32 = _Kernel32(handle=99, exit_code=_STILL_ACTIVE)
        assert _probe_on_win32(k32, pid=2**63) is False
        assert k32.open_calls == []  # the impossible pid never reached the API
        assert k32.closed == []

    def test_the_binding_declares_the_prototypes(self):
        # argtypes/restype on all three functions. The one that bites on
        # 64-bit Windows is OpenProcess's restype: without c_void_p, ctypes
        # applies its default c_int restype and truncates HANDLEs to 32 bits.
        k32 = _Kernel32(handle=99, exit_code=_STILL_ACTIVE)
        _probe_on_win32(k32)
        assert k32.OpenProcess.restype is _FakeCtypes.c_void_p
        assert k32.OpenProcess.argtypes == (
            _FakeCtypes.c_ulong,  # DWORD dwDesiredAccess
            _FakeCtypes.c_long,  # BOOL bInheritHandle
            _FakeCtypes.c_ulong,  # DWORD dwProcessId
        )
        assert k32.GetExitCodeProcess.restype is _FakeCtypes.c_long
        assert k32.GetExitCodeProcess.argtypes == (
            _FakeCtypes.c_void_p,
            ("POINTER", _FakeCtypes.c_ulong),
        )
        assert k32.CloseHandle.restype is _FakeCtypes.c_long
        assert k32.CloseHandle.argtypes == (_FakeCtypes.c_void_p,)


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
