"""A script command that ends its own process with a signal, for tests of how
a script's ending by a signal is treated."""

from __future__ import annotations

import shlex
import sys


def self_signal_command(sig: str, *, before: str = "") -> str:
    """A shell command whose process ends itself with ``SIG<sig>``, after
    running ``before`` when given.

    The process is ended by the signal whatever dispositions it inherited. A
    parent that ignores a signal (a CI runner or a background job often
    ignores SIGQUIT and SIGINT) hands that on to its children, and a shell
    cannot stop ignoring a signal it inherited as ignored, so ``kill -QUIT $$``
    then does nothing and the script exits 0. Python can put the default action
    back, and ``exec`` makes it the script's own process.
    """
    reset = "" if sig == "KILL" else f"signal.signal(signal.SIG{sig}, signal.SIG_DFL); "
    code = f"import os, signal; {reset}os.kill(os.getpid(), signal.SIG{sig})"
    prefix = f"{before}; " if before else ""
    return f"{prefix}exec {shlex.quote(sys.executable)} -c {shlex.quote(code)}"
