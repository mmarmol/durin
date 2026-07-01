import asyncio

from durin.utils.resizable_semaphore import ResizableSemaphore


def test_bounds_concurrency():
    async def run():
        sem = ResizableSemaphore(2)
        active = {"n": 0, "max": 0}

        async def worker():
            async with sem:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
                await asyncio.sleep(0.02)
                active["n"] -= 1

        await asyncio.gather(*(worker() for _ in range(6)))
        assert active["max"] == 2

    asyncio.run(run())


def test_unlimited_when_non_positive():
    async def run():
        sem = ResizableSemaphore(0)
        active = {"n": 0, "max": 0}

        async def worker():
            async with sem:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
                await asyncio.sleep(0.02)
                active["n"] -= 1

        await asyncio.gather(*(worker() for _ in range(5)))
        assert active["max"] == 5  # no gating

    asyncio.run(run())


def test_raise_limit_wakes_waiters():
    async def run():
        sem = ResizableSemaphore(1)
        order = []

        async def worker(i):
            async with sem:
                order.append(i)
                await asyncio.sleep(0.05)

        tasks = [asyncio.create_task(worker(i)) for i in range(3)]
        await asyncio.sleep(0.01)   # 1 running, 2 waiting
        assert len(order) == 1
        sem.set_limit(3)            # raise → the 2 waiters admit immediately
        await asyncio.sleep(0.01)
        assert len(order) == 3
        await asyncio.gather(*tasks)

    asyncio.run(run())


def test_lower_limit_applies_as_holders_finish():
    async def run():
        sem = ResizableSemaphore(3)
        started, gate = [], asyncio.Event()

        async def worker(i):
            async with sem:
                started.append(i)
                await gate.wait()

        tasks = [asyncio.create_task(worker(i)) for i in range(3)]
        await asyncio.sleep(0.01)
        assert len(started) == 3
        sem.set_limit(1)            # lower while 3 are in-flight
        assert sem.limit == 1
        gate.set()                  # let all three finish
        await asyncio.gather(*tasks)
        # after draining, only 1 may run at a time
        started.clear()
        run2 = {"n": 0, "max": 0}

        async def w2():
            async with sem:
                run2["n"] += 1
                run2["max"] = max(run2["max"], run2["n"])
                await asyncio.sleep(0.02)
                run2["n"] -= 1

        await asyncio.gather(*(w2() for _ in range(4)))
        assert run2["max"] == 1

    asyncio.run(run())


def test_unlimited_to_limited_with_overshoot():
    # Regression: transitioning from unlimited (0) to a cap LOWER than the
    # in-flight count must converge to the new cap, not permanently over-admit.
    async def run():
        sem = ResizableSemaphore(0)  # unlimited
        gate = asyncio.Event()
        started = []

        async def worker(i):
            async with sem:
                started.append(i)
                await gate.wait()

        tasks = [asyncio.create_task(worker(i)) for i in range(5)]
        await asyncio.sleep(0.01)
        assert len(started) == 5  # all admitted while unlimited
        sem.set_limit(2)          # tighten below the 5 in flight
        assert sem.limit == 2
        gate.set()
        await asyncio.gather(*tasks)

        run2 = {"n": 0, "max": 0}

        async def w2():
            async with sem:
                run2["n"] += 1
                run2["max"] = max(run2["max"], run2["n"])
                await asyncio.sleep(0.02)
                run2["n"] -= 1

        await asyncio.gather(*(w2() for _ in range(6)))
        assert run2["max"] == 2  # converged to the new cap

    asyncio.run(run())
