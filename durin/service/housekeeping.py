"""Workspace housekeeping the gateway repeats while it runs.

Two stores age out on their own schedule, and nothing else revisits them:

* approval records (``approval_store.expire_and_prune``): a pending request
  expires 14 days after it was filed, a record a killed run left ``approved``
  is closed as interrupted, and a resolved record is pruned 30 days later;
* automation claims (``automations.claims.prune``): a thread-to-run mapping
  that was never released (the process died first, or the counterpart never
  replied) is dropped after ``CLAIMS_MAX_AGE_S``.

``sweep_workspace`` runs both, each guarded so a failing store never stops the
other. The gateway runs it once at boot (``build_service_registry``) and then
every ``HOUSEKEEPING_INTERVAL_S`` through ``WorkspaceJanitor``: one asyncio
task, started with the gateway and stopped with it. Both sweeps are
cross-process safe, so a TUI or CLI working on the same workspace meanwhile
is not disturbed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from durin.agent import approval_store
from durin.automations import claims

# How often the janitor sweeps. Nothing here is urgent: a request expires
# after days and a claim after a week, so an hour of slack costs nothing.
HOUSEKEEPING_INTERVAL_S = 3600.0

# A claim still registered after a week belongs to a run that will never be
# answered through it. Claims are conversation-scoped, not tied to any queue
# setting, so a flat constant bounds them.
CLAIMS_MAX_AGE_S = 7 * 24 * 3600

# A sweep is a pass over a few small files; one still running after this is
# stuck (a lock never released), and the janitor stops waiting on it so the
# next interval still comes.
_SWEEP_TIMEOUT_S = 300.0

# How long stopping waits for the janitor's task to end once cancelled. It is
# awaiting either its sleep or a sweep's worker thread, both of which end the
# await at once, so this only bounds a pathological case.
_STOP_WAIT_S = 5.0


def sweep_workspace(workspace: Path | str) -> dict[str, Any]:
    """Expire and prune approval records, then prune stale automation claims.
    Returns what each did; a store that failed is logged and left out."""
    out: dict[str, Any] = {}
    try:
        out["approvals"] = approval_store.expire_and_prune(workspace)
    except Exception:  # noqa: BLE001 — one store failing must not stop the other
        logger.exception("housekeeping: approvals expire_and_prune failed")
    try:
        out["claims_released"] = len(claims.prune(workspace, max_age_s=CLAIMS_MAX_AGE_S))
    except Exception:  # noqa: BLE001
        logger.exception("housekeeping: automation claims prune failed")
    return out


class WorkspaceJanitor:
    """Run ``sweep_workspace`` every ``interval_s`` until stopped.

    It sleeps first: the boot sweep has just run. Each sweep runs off the
    event loop, in a worker thread, bounded by ``_SWEEP_TIMEOUT_S``.
    """

    def __init__(self, workspace_resolver: Callable[[], Path],
                 *, interval_s: float = HOUSEKEEPING_INTERVAL_S) -> None:
        self._workspace_resolver = workspace_resolver
        self._interval_s = interval_s
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start sweeping; a janitor already running is left as it is."""
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="workspace-janitor")

    async def stop(self) -> None:
        """Cancel the janitor and wait, bounded, for its task to end."""
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            # asyncio.wait never raises the task's own cancellation, so a
            # cancellation of the caller is the only one that propagates.
            async with asyncio.timeout(_STOP_WAIT_S):
                await asyncio.wait({task})
        except TimeoutError:
            logger.warning("housekeeping: the janitor did not stop within {}s", _STOP_WAIT_S)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                async with asyncio.timeout(_SWEEP_TIMEOUT_S):
                    counts = await asyncio.to_thread(sweep_workspace, self._workspace_resolver())
                logger.debug("housekeeping sweep: {}", counts)
            except TimeoutError:
                logger.warning("housekeeping: a sweep took over {}s; trying again next interval",
                               _SWEEP_TIMEOUT_S)
            except Exception:  # noqa: BLE001 — the janitor must outlive a bad pass
                logger.exception("housekeeping: sweep failed")
