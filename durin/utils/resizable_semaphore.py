"""An asyncio concurrency gate whose limit can change at runtime.

``asyncio.Semaphore`` fixes its count at construction; durin needs lane/ceiling
caps that a config hot-reload can raise or lower live. This wraps an internal
``asyncio.Semaphore`` and adjusts it by releasing extra permits (to raise) or
withholding permits as holders exit (to lower). ``limit <= 0`` means unlimited:
the gate becomes a no-op so an operator can turn a lane's cap off.
"""

from __future__ import annotations

import asyncio


class ResizableSemaphore:
    def __init__(self, limit: int, *, name: str = "") -> None:
        self.name = name
        self._limit = limit if limit > 0 else 0  # 0 => unlimited (no gating)
        self._sem: asyncio.Semaphore | None = (
            asyncio.Semaphore(self._limit) if self._limit > 0 else None
        )
        self._active = 0
        # Permits to withhold on the next N releases, to shrink the live cap
        # without yanking a permit out from under an in-flight holder.
        self._to_reduce = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        return self._active

    async def __aenter__(self) -> "ResizableSemaphore":
        if self._sem is not None:
            await self._sem.acquire()
        self._active += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._active -= 1
        if self._sem is None:
            return
        if self._to_reduce > 0:
            self._to_reduce -= 1  # withhold this permit → effective cap drops by 1
        else:
            self._sem.release()

    def set_limit(self, new_limit: int) -> None:
        """Change the cap live. Sync — safe to call from the loop thread (e.g.
        config hot-reload). Raising wakes waiters immediately; lowering takes
        effect as current holders exit."""
        new_limit = new_limit if new_limit > 0 else 0
        if new_limit == self._limit:
            return
        # (un)limited transitions: rebuild. Rare; only on turning a cap fully off/on.
        if self._limit == 0 or new_limit == 0:
            self._limit = new_limit
            self._to_reduce = 0
            if new_limit == 0:
                self._sem = None
            else:
                # New permits = new cap minus whatever is already in flight.
                self._sem = asyncio.Semaphore(max(0, new_limit - self._active))
            return
        delta = new_limit - self._limit
        self._limit = new_limit
        if delta > 0:
            for _ in range(delta):
                if self._to_reduce > 0:
                    self._to_reduce -= 1  # cancel a pending shrink first
                else:
                    self._sem.release()   # add a permit, waking a waiter
        else:
            self._to_reduce += -delta
