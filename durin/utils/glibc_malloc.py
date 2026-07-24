"""glibc malloc introspection and trim via ctypes.

Long-running multi-threaded processes on glibc can hold gigabytes of
freed-but-unreturned memory: per-thread arenas keep pages after free()
and only the main arena's top chunk is trimmed automatically. mallinfo2
exposes how much of the arenas is live vs retained, and malloc_trim(0)
walks every arena releasing page runs of FREE chunks back to the OS —
live allocations are untouched by design, so a trim is always safe.

Everything here degrades to None/False off glibc (macOS, musl): callers
treat "no signal" and "not glibc" identically.
"""
from __future__ import annotations

import ctypes
import sys

__all__ = ["malloc_stats_mb", "malloc_trim"]

_MB = float(2**20)


class _Mallinfo2(ctypes.Structure):
    # struct mallinfo2 from <malloc.h>, glibc >= 2.33 (size_t fields; the
    # legacy int-field mallinfo overflows past 2GB and is not worth wrapping).
    _fields_ = [
        ("arena", ctypes.c_size_t),      # non-mmap bytes taken from the OS
        ("ordblks", ctypes.c_size_t),
        ("smblks", ctypes.c_size_t),
        ("hblks", ctypes.c_size_t),
        ("hblkhd", ctypes.c_size_t),     # bytes in mmap'd allocations
        ("usmblks", ctypes.c_size_t),
        ("fsmblks", ctypes.c_size_t),
        ("uordblks", ctypes.c_size_t),   # bytes in live arena allocations
        ("fordblks", ctypes.c_size_t),   # freed bytes retained in arenas
        ("keepcost", ctypes.c_size_t),
    ]


def _libc_symbol(name: str) -> ctypes._CFuncPtr | None:
    """A callable for a libc symbol, or None when unavailable (non-Linux,
    or a libc without the symbol, e.g. musl)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        return getattr(ctypes.CDLL(None), name)
    except (OSError, AttributeError):
        return None


def malloc_stats_mb() -> dict[str, float] | None:
    """One glibc allocator snapshot, or None when unavailable.

    ``system_mb`` — bytes the allocator holds from the OS (arenas + mmap);
    ``in_use_mb`` — bytes in live allocations (mmap'd blocks are always
    live: glibc unmaps them on free); ``free_mb`` — freed bytes the arenas
    retain. A large ``free_mb`` is exactly the memory ``malloc_trim``
    can give back.
    """
    fn = _libc_symbol("mallinfo2")
    if fn is None:
        return None
    fn.restype = _Mallinfo2
    fn.argtypes = []
    info = fn()
    return {
        "system_mb": round((info.arena + info.hblkhd) / _MB, 1),
        "in_use_mb": round((info.uordblks + info.hblkhd) / _MB, 1),
        "free_mb": round(info.fordblks / _MB, 1),
    }


def malloc_trim() -> bool:
    """Release free glibc arena pages back to the OS; True when any memory
    was returned, False otherwise (including off-glibc no-op)."""
    fn = _libc_symbol("malloc_trim")
    if fn is None:
        return False
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_size_t]
    return bool(fn(0))
