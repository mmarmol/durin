"""glibc malloc introspection and trim via ctypes.

Long-running multi-threaded processes on glibc can hold gigabytes of
freed-but-unreturned memory: per-thread arenas keep pages after free()
and only the main arena's top chunk is trimmed automatically. mallinfo2
exposes how much of the arenas is live vs retained, and malloc_trim(0)
walks every arena releasing page runs of FREE chunks back to the OS —
live allocations are untouched by design, so a trim is always safe.
mallinfo2 keeps counting trimmed pages in its totals, so how much of the
arenas is actually in RAM is read separately, page by page.

Everything here degrades to None/False off glibc (macOS, musl): callers
treat "no signal" and "not glibc" identically.
"""
from __future__ import annotations

import ctypes
import os
import sys

__all__ = ["malloc_stats_mb", "malloc_trim"]

_MB = float(2**20)

# glibc's HEAP_MAX_SIZE on 64-bit: every heap of a non-main arena is mapped
# at a multiple of it and starts with a heap_info header whose first words
# are {ar_ptr, prev, size, mprotect_size}; an arena's own struct sits right
# after the header of its first heap. The main arena is the brk [heap].
_HEAP_ALIGN = 64 * 2**20

# mincore marks a resident page by the lowest bit of its byte; the other
# bits are reserved. Translating through this table keeps only that bit.
_RESIDENT_BIT = bytes(i & 1 for i in range(256))


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


def _arena_resident_bytes(arena_bytes: int) -> int | None:
    """Bytes of glibc's arenas that are in RAM, or None when the arenas
    cannot be found and fully accounted for.

    The heaps are located from /proc/self/maps and confirmed by their own
    headers, read through /proc/self/mem, so a mapping that vanishes
    mid-walk is a read error, never a crash. Residency is counted page by
    page with mincore. The heaps found must add up to mallinfo2's arena
    bytes (within a tenth, for arenas growing mid-walk): on a glibc laid
    out differently the reading is unknown rather than wrong."""
    mincore = _libc_symbol("mincore")
    if mincore is None:
        return None
    mincore.restype = ctypes.c_int
    mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
    page = os.sysconf("SC_PAGE_SIZE")

    def resident(start: int, end: int) -> int:
        pages = (end - start + page - 1) // page
        vec = ctypes.create_string_buffer(pages)
        if mincore(start, pages * page, vec) != 0:
            return 0
        return vec.raw.translate(_RESIDENT_BIT).count(1) * page

    try:
        with open("/proc/self/maps") as f:
            maps = f.read().splitlines()
        mem = os.open("/proc/self/mem", os.O_RDONLY)
    except OSError:
        return None

    def word(address: int) -> int:
        try:
            return int.from_bytes(os.pread(mem, 8, address), sys.byteorder)
        except OSError:
            return 0

    found = total = 0
    try:
        for line in maps:
            fields = line.split()
            start, end = (int(x, 16) for x in fields[0].split("-"))
            if fields[-1] == "[heap]":
                found += end - start
                total += resident(start, end)
            elif len(fields) == 5 and fields[1] == "rw-p":
                # Anonymous read-write memory: look for heap headers at
                # every heap-aligned address it covers.
                heap = -(-start // _HEAP_ALIGN) * _HEAP_ALIGN
                while heap < end:
                    arena, size, mapped = word(heap), word(heap + 16), word(heap + 24)
                    first = arena & ~(_HEAP_ALIGN - 1)
                    if (0 < size <= mapped <= _HEAP_ALIGN
                            and 0 < arena - first <= 64 and word(first) == arena):
                        found += size
                        total += resident(heap, heap + mapped)
                    heap += _HEAP_ALIGN
    finally:
        os.close(mem)
    return total if abs(found - arena_bytes) <= arena_bytes / 10 else None


def malloc_stats_mb() -> dict[str, float] | None:
    """One glibc allocator snapshot, or None when unavailable.

    ``system_mb`` — bytes the allocator holds from the OS (arenas + mmap);
    ``in_use_mb`` — bytes in live allocations (mmap'd blocks are always
    live: glibc unmaps them on free); ``free_mb`` — freed bytes the arenas
    retain, including pages a trim already returned to the OS (mallinfo2
    keeps those chunks in its books); ``resident_mb`` — the part of
    ``system_mb`` in RAM (mmap'd blocks count as resident), 0.0 when it
    cannot be read. ``resident_mb - in_use_mb`` is the freed memory
    ``malloc_trim`` can still give back.
    """
    fn = _libc_symbol("mallinfo2")
    if fn is None:
        return None
    fn.restype = _Mallinfo2
    fn.argtypes = []
    info = fn()
    resident = _arena_resident_bytes(info.arena)
    return {
        "system_mb": round((info.arena + info.hblkhd) / _MB, 1),
        "in_use_mb": round((info.uordblks + info.hblkhd) / _MB, 1),
        "free_mb": round(info.fordblks / _MB, 1),
        "resident_mb": (
            round((resident + info.hblkhd) / _MB, 1) if resident is not None else 0.0),
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
