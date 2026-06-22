"""Cross-process safety tests for ApiTokenStore.

Two concurrent processes each issue a token → both tokens must survive
in the final file (without cross-process locking one write wins and the
other is silently lost).

Two concurrent processes call get_or_create_media_secret → they must
converge on the SAME secret (without locking they each mint a different
32-byte value and whichever writes last wins, invalidating the other's
signed URLs).

Cross-process lock ordering: both operations use cross_process_lock to prevent
last-writer-wins data loss.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path


def _issue_token(token_path: str, result_queue) -> None:
    """Worker: issue one token and put (token_id, plaintext) on the queue."""
    from durin.security.api_tokens import ApiTokenStore  # noqa: PLC0415

    store = ApiTokenStore(path=Path(token_path))
    tid, plaintext = store.issue(["admin"])
    result_queue.put((tid, plaintext))


def test_two_processes_no_lost_token(tmp_path: Path) -> None:
    """Both issued tokens must be resolvable after concurrent issue calls."""
    token_path = tmp_path / "api_tokens.json"
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    ps = [ctx.Process(target=_issue_token, args=(str(token_path), q)) for _ in range(2)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(20)

    results = [q.get_nowait() for _ in range(2)]

    from durin.security.api_tokens import ApiTokenStore  # noqa: PLC0415

    store = ApiTokenStore(path=token_path)
    for tid, plaintext in results:
        entry = store.resolve(plaintext)
        assert entry is not None, f"token {tid} was lost in concurrent issue"
        assert entry["token_id"] == tid


def _get_media_secret(token_path: str, result_queue) -> None:
    """Worker: call get_or_create_media_secret and put the hex on the queue."""
    from durin.security.api_tokens import ApiTokenStore  # noqa: PLC0415

    store = ApiTokenStore(path=Path(token_path))
    secret = store.get_or_create_media_secret()
    result_queue.put(secret.hex())


def test_two_processes_same_media_secret(tmp_path: Path) -> None:
    """Concurrent get_or_create_media_secret calls must return the same secret."""
    token_path = tmp_path / "api_tokens.json"
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    ps = [
        ctx.Process(target=_get_media_secret, args=(str(token_path), q))
        for _ in range(2)
    ]
    for p in ps:
        p.start()
    for p in ps:
        p.join(20)

    secrets = [q.get_nowait() for _ in range(2)]
    assert secrets[0] == secrets[1], (
        f"media secret split-brain: process 0 got {secrets[0]}, "
        f"process 1 got {secrets[1]}"
    )
