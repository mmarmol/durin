"""Test-suite defaults.

Per ``durin/memory/provenance.py`` production code has NO implicit
author default — every memory write must wrap itself in
:func:`author_scope`. In test runtime we keep one explicit
convention: **tests model agent-observed writes by default**, so we
open ``author_scope("agent_created")`` around every test body
through this autouse fixture.

A test that needs to model human-authored writes overrides locally:

    def test_user_authored_path():
        with author_scope("user_authored"):
            store_memory(...)

The fixture's existence is itself the explicit declaration — making
the convention discoverable + grep-able instead of hidden inside a
``ContextVar`` default.
"""

from __future__ import annotations

import os

import pytest

from durin.memory.provenance import author_scope

# Deterministic CLI rendering for the whole suite.
#
# CI exports FORCE_COLOR (and runs with no TTY → 80-column default), which
# makes Typer/Rich inject ANSI color codes *inside* tokens and word-wrap
# output at 80 columns. That breaks substring assertions in CLI tests
# (`port 18791`, `memory/<class>/<id>`, `Health endpoint: http://…`) which
# are about content, not layout — they pass locally only because the dev
# shell happens to render plain + wide. Pin that rendering for everyone so
# the suite never depends on the terminal environment. Set at import time,
# before any Typer CliRunner is constructed or Rich reads the env.
os.environ.pop("FORCE_COLOR", None)
os.environ["NO_COLOR"] = "1"
os.environ["TERM"] = "dumb"
os.environ["COLUMNS"] = "200"


@pytest.fixture(autouse=True, scope="session")
def _testclient_localhost_peer():
    """Model the in-process Starlette TestClient as a localhost peer, suite-wide.

    Starlette's TestClient defaults the ASGI scope's client address to
    ("testclient", 50000), which is NOT a loopback IP, and its Host header to
    "testserver", which is not a loopback name; ``websocket_connect`` even
    ignores ``base_url`` and always sends "testserver". With no setup secret,
    durin's /webui/bootstrap mints an ADMIN token, and its socket takes an
    anonymous connection, only for a localhost peer reached under a loopback
    Host name. An in-process TestClient genuinely IS a local client, so model
    its peer as 127.0.0.1, its base URL as http://127.0.0.1, and send a
    relative socket URL to that base URL too. A test exercising the
    remote-rejection path passes ``client=(...)`` or ``base_url=...``
    explicitly (``setdefault`` leaves it untouched), and its sockets then go
    to that base URL.

    Session-scoped and self-undoing so the patch is tied to the pytest run, not a
    permanent import-time mutation. Safe because no test constructs a TestClient
    at module/collection time — they all build it inside fixtures/functions, which
    run after this fixture is set up.
    """
    from urllib.parse import urljoin

    import starlette.testclient as stc

    original_init = stc.TestClient.__init__
    original_ws_connect = stc.TestClient.websocket_connect

    def _init_with_localhost_peer(self, *args, **kwargs):
        kwargs.setdefault("client", ("127.0.0.1", 0))
        kwargs.setdefault("base_url", "http://127.0.0.1")
        return original_init(self, *args, **kwargs)

    def _ws_connect_to_base_url(self, url, *args, **kwargs):
        if "://" not in url:
            base = str(self.base_url)
            # http -> ws, https -> wss
            url = urljoin("ws" + base[len("http"):] if base.startswith("http") else base, url)
        return original_ws_connect(self, url, *args, **kwargs)

    stc.TestClient.__init__ = _init_with_localhost_peer
    stc.TestClient.websocket_connect = _ws_connect_to_base_url
    try:
        yield
    finally:
        stc.TestClient.__init__ = original_init
        stc.TestClient.websocket_connect = original_ws_connect


@pytest.fixture(autouse=True)
def _no_one_left_waiting():
    """Start and end every test with nobody waiting on a person.

    The in-turn waiter registry (``pending_answers``) and the approval
    hand-off map are module state. A test that fails while a turn waits on
    an approval or a question would leave its waiter registered, and the
    next test asking in the same chat would find a stale waiter or wait out
    the full answer timeout. ``reset`` cancels leftover waiters and clears
    the consumer flags; the hand-off map is emptied with them.
    """
    from durin.agent import approval, pending_answers

    pending_answers.reset()
    approval._HANDOFF_DECIDED_BY.clear()
    yield
    pending_answers.reset()
    approval._HANDOFF_DECIDED_BY.clear()


@pytest.fixture(autouse=True)
def _isolate_durin_home(tmp_path_factory, monkeypatch):
    """Run every test as a throwaway durin instance.

    ``durin_home()`` reads ``$DURIN_HOME`` with priority over ``Path.home()``,
    so an ambient DURIN_HOME (a dev shell) would leak into tests, and an unset
    one would point them at the real ``~/.durin`` — colliding with a running
    daemon (SQLite/lock contention) and polluting the live deploy. Pin a fresh
    per-test home so the suite never touches ``~/.durin`` and passes under any
    ambient environment. A test that needs the default/unset behaviour controls
    DURIN_HOME itself (see ``tests/config/test_config_paths.py``).
    """
    import durin.config.loader as _loader

    home = tmp_path_factory.mktemp("durin_home")
    monkeypatch.setenv("DURIN_HOME", str(home))
    monkeypatch.setattr(_loader, "_current_config_path", None, raising=False)
    yield


@pytest.fixture(autouse=True)
def _ban_repo_root_writes():
    """Fail the test that drops a file into the repo root, naming it.

    Production code builds paths by formatting whatever it was handed:
    ``cross_process_lock`` does ``Path(f"{target}.lock")``. Hand it a mock and
    the f-string yields the mock's *repr*, which is a relative path — so the
    lock file is created in the process CWD, i.e. the repo root, under a name
    like ``<MagicMock name='SessionManager()._get_session_path()'>.lock``. A
    mock is ``isinstance(..., os.PathLike)``, so no type check at the lock
    catches this; only the artifact on disk reveals it.

    Those files are invisible in ``git status`` (``.gitignore`` excludes them
    so one never reaches a commit), which is exactly why the leak survived for
    so long: nothing failed, and the mess was silently swept aside instead of
    the test being fixed. This turns it back into a loud failure at the moment
    it happens, attributed to the test that caused it.

    The rule is deliberately general — no test may create anything in the repo
    root, whatever the mechanism. Tests write to ``tmp_path`` or the isolated
    ``DURIN_HOME``. Names the pytest/coverage machinery owns are exempt. A
    leaked *file* is deleted so the tree is left clean; a leaked directory is
    reported but left alone rather than recursively removed, since guessing
    what is safe to delete is how a guard becomes the accident.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    tooling = {".pytest_cache", ".coverage", "__pycache__"}
    before = set(os.listdir(root))
    yield
    leaked = sorted(n for n in set(os.listdir(root)) - before if n not in tooling)
    for name in leaked:
        entry = root / name
        if entry.is_file():
            entry.unlink()
    if leaked:
        pytest.fail(
            "test wrote into the repo root: "
            + ", ".join(repr(n) for n in leaked)
            + ". Tests write under tmp_path or DURIN_HOME. A '<MagicMock ...>' "
            "name means a mocked path object reached real filesystem code — "
            "give the double a real tmp_path instead of mocking the path away.",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _test_default_author_scope():
    with author_scope("agent_created"):
        yield


@pytest.fixture(autouse=True)
def _ban_catalog_network_fetch(monkeypatch):
    """Suite-wide ban on the catalog refreshers' real network fetch.

    Both refresh schedulers (MCP + provider models) fetch IMMEDIATELY in
    their background thread when the local cache is missing or overdue —
    which in a test's throwaway DURIN_HOME is always. Any test that
    constructs an AgentLoop with a real Config would therefore spawn threads
    hitting github.com / models.dev. Replace each module's ``_default_fetch``
    with a raiser (the failure is swallowed by the refreshers' keep-prior-data
    contract); a test exercising refresh behavior injects its own fake fetch
    or monkeypatches ``_default_fetch`` on top of this.
    """

    def _banned(url: str) -> bytes:
        raise RuntimeError(f"catalog network fetch banned in tests (url={url})")

    import durin.agent.mcp_catalog_refresh as _mcr
    import durin.providers.catalog_refresh as _pcr

    monkeypatch.setattr(_mcr, "_default_fetch", _banned)
    monkeypatch.setattr(_pcr, "_default_fetch", _banned)


@pytest.fixture(autouse=True)
def _restore_loguru_durin_activation():
    """Keep loguru's ``durin`` namespace enabled across test boundaries.

    The ``serve`` and ``agent`` CLI commands call ``logger.disable("durin")``
    when not run verbosely (durin/cli/commands.py) — a deliberate, process-wide
    side effect that quiets durin's internal logs for a long-running command.
    loguru's enable/disable state is global, like the stdlib ``logging`` config,
    so in a single pytest process that mutation outlives the invoking test. A
    later test that asserts on loguru output (the MCP server→client logging,
    sampling, and spawn-policy harnesses) would then see an empty sink.

    Restore the production default (``durin`` enabled) after every test so one
    test's activation state can never suppress another's log assertions.
    """
    from loguru import logger

    try:
        yield
    finally:
        logger.enable("durin")


@pytest.fixture(autouse=True)
def _cancel_restart_watchdogs_after_test(monkeypatch):
    """Never let a restart watchdog timer outlive its test.

    ``arm_restart_deadline`` (durin/utils/restart.py) starts a real daemon
    ``threading.Timer`` — 30s by default — that calls ``reexec()``, a real
    ``os.execv()``, when it fires. A test that exercises ``/restart``'s
    fallback or gateway path (most don't specifically guard against this)
    would otherwise leave that timer ticking in the background once the
    test itself returns; if the rest of the suite runs long enough for it
    to fire, it replaces the whole pytest process outright — observed as
    an abrupt, signature-less interruption partway through a full-directory
    run, with no fix-under-test involved at all. Every call this process
    makes during a test is tracked here and cancelled at teardown; a test
    that wants the real firing behavior (the watchdog itself) still gets
    it, since cancellation only matters for a timer that hasn't fired yet.
    """
    import durin.utils.restart as _restart_mod

    created: list = []
    real_arm = _restart_mod.arm_restart_deadline

    def _tracking_arm(*args, **kwargs):
        timer = real_arm(*args, **kwargs)
        created.append(timer)
        return timer

    monkeypatch.setattr(_restart_mod, "arm_restart_deadline", _tracking_arm)
    # Both call sites import the name directly, so each holds its own
    # binding to the original function — patching the defining module
    # above does not reach them. Default raising=True: if either import
    # ever gets renamed, this must fail loudly rather than silently stop
    # covering that call site.
    monkeypatch.setattr("durin.cli.commands.arm_restart_deadline", _tracking_arm)
    monkeypatch.setattr("durin.command.builtin.arm_restart_deadline", _tracking_arm)
    yield
    for timer in created:
        timer.cancel()


@pytest.fixture(autouse=True)
def _reset_reexec_guard():
    """Reset restart.py's one-shot re-exec guard before each test.

    ``reexec()`` (durin/utils/restart.py) execs only once per process, so the
    normal restart path and its watchdog timer can't both replace it. That
    guard is a plain module-level flag with no per-test scope: once any test
    exercises the real ``reexec()`` (even with ``os.execv`` mocked away), it
    stays tripped for the rest of the suite, and a later test's own restart
    path would then silently skip its ``os.execv`` call.

    The lock is reset too, fresh, every test: the loser of the race decides
    under it but blocks forever outside it, so the lock itself is never held
    long-term by a legitimate call — but a test is exactly what deliberately
    drives a loser into that forever-block, and starting every test on a
    known-fresh ``Lock()`` (rather than whatever the previous test method
    left behind) is a one-line guarantee against a class of hang that a
    single flag reset wouldn't catch.
    """
    import threading

    import durin.utils.restart as _restart_mod

    _restart_mod._reexeced = False
    _restart_mod._reexec_lock = threading.Lock()


@pytest.fixture(autouse=True)
def _fresh_transcript_writer(monkeypatch):
    """Give every test its own display-transcript writer.

    The production writer is a process-wide singleton that buffers events
    until a drain. A test that sends frames without flushing (anything that
    persists a reply, including to a chat nobody watches) would otherwise
    leave buffered rows that a later test's flush writes into *its* data dir,
    under the same session key — an order-dependent extra row."""
    from durin.utils import webui_transcript

    monkeypatch.setattr(webui_transcript, "_WRITER", None)


def write_webui_transcript(session_key: str, *events: dict) -> None:
    """Persist webui transcript events through the production writer.

    Several suites need transcript lines on disk to exercise reading, paging,
    replay or deletion. Writing the JSONL by hand would pin those tests to a
    format the writer could drift away from, so they go through the real
    ``TranscriptWriter`` — the same path ``durin/channels/websocket.py`` uses.
    """
    import asyncio

    from durin.utils import webui_transcript as wt

    async def _run() -> None:
        writer = wt.get_transcript_writer()
        for event in events:
            writer.enqueue(session_key, event)
        await writer.flush(session_key)

    asyncio.run(_run())
