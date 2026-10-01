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

import errno
import importlib.util
import ipaddress
import os
import socket
import threading

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

# tiktoken reads its encodings from TIKTOKEN_CACHE_DIR and downloads a missing
# one from openaipublic.blob.core.windows.net. litellm ships the cl100k_base
# and o200k_base files and points TIKTOKEN_CACHE_DIR at them when its
# tokenizer module is first imported, so a test that counted tokens before
# anything in its process had imported that module downloaded the encoding
# (on a machine without it in tiktoken's own cache). Point it there before
# any test runs.
_litellm = importlib.util.find_spec("litellm")
if _litellm is not None and _litellm.submodule_search_locations:
    _tokenizers = os.path.join(
        next(iter(_litellm.submodule_search_locations)), "litellm_core_utils", "tokenizers")
    if os.path.isdir(_tokenizers):
        os.environ["TIKTOKEN_CACHE_DIR"] = _tokenizers


# No test reaches another host. The socket calls every client goes through
# are wrapped once, here at import, before pytest collects the test modules
# (and the modules they import) that could hold on to the originals: a
# connection or datagram to a non-loopback address, and a host-name lookup
# that would ask a DNS server, are refused with the error an offline machine
# gives and recorded, and ``_no_outbound_network`` fails the test that made
# them. Loopback, Unix sockets, IP literals (they need no lookup) and this
# machine's own name stay open.
_LOCAL_HOST_NAMES = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    socket.gethostname().lower(),
})


def _is_local_address(host) -> bool:
    try:
        ip = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    # The unspecified address (0.0.0.0, ::) reaches this machine when connected to.
    return ip.is_loopback or ip.is_unspecified


def _needs_lookup(host) -> bool:
    if host is None:
        return False
    name = host.decode() if isinstance(host, (bytes, bytearray)) else str(host)
    name = name.lower().rstrip(".")
    if not name or name in _LOCAL_HOST_NAMES:
        return False
    try:
        ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return True
    return False


def _install_network_guard() -> dict:
    """Wrap the socket calls once per process and return the guard's state.

    A test module that imports a helper from this file (``from tests.conftest
    import ...``) imports it a second time under another name; that import
    finds the guard already installed and shares its state, so the refusals
    a test causes reach the fixture that reports them."""
    installed = getattr(socket.getaddrinfo, "network_guard", None)
    if installed is not None:
        return installed
    guard: dict = {"open": False, "refusals": []}
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_getaddrinfo = socket.getaddrinfo
    real_gethostbyname = socket.gethostbyname
    real_gethostbyname_ex = socket.gethostbyname_ex

    def _refuse(what: str) -> str:
        guard["refusals"].append(f"{what} (thread {threading.current_thread().name})")
        return f"network access is refused in the test suite: {what}"

    def remote(sock, address) -> bool:
        return (
            not guard["open"]
            and sock.family in (socket.AF_INET, socket.AF_INET6)
            and isinstance(address, tuple)
            and not _is_local_address(address[0])
        )

    def lookup_refused(host) -> bool:
        return not guard["open"] and _needs_lookup(host)

    def connect(self, address):
        if remote(self, address):
            raise OSError(errno.ENETUNREACH, _refuse(f"connect to {address[0]}:{address[1]}"))
        return real_connect(self, address)

    def connect_ex(self, address):
        if remote(self, address):
            _refuse(f"connect to {address[0]}:{address[1]}")
            return errno.ENETUNREACH
        return real_connect_ex(self, address)

    def sendto(self, data, *args):
        if args and remote(self, args[-1]):
            raise OSError(errno.ENETUNREACH, _refuse(f"datagram to {args[-1][0]}:{args[-1][1]}"))
        return real_sendto(self, data, *args)

    def getaddrinfo(host, *args, **kwargs):
        if lookup_refused(host):
            raise socket.gaierror(socket.EAI_NONAME, _refuse(f"lookup of {host!r}"))
        return real_getaddrinfo(host, *args, **kwargs)

    def gethostbyname(name):
        if lookup_refused(name):
            raise socket.gaierror(socket.EAI_NONAME, _refuse(f"lookup of {name!r}"))
        return real_gethostbyname(name)

    def gethostbyname_ex(name):
        if lookup_refused(name):
            raise socket.gaierror(socket.EAI_NONAME, _refuse(f"lookup of {name!r}"))
        return real_gethostbyname_ex(name)

    getaddrinfo.network_guard = guard
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex
    return guard


_NETWORK_GUARD = _install_network_guard()


def pytest_collection_modifyitems(config, items):
    """Skip the ``real_model`` tests unless the run selects them by marker.

    They embed with the real model, which fastembed downloads (about 450 MB)
    on a machine that does not have it cached yet, so a default run, CI's
    included, never runs them; ``pytest -m real_model`` does."""
    if "real_model" in (config.getoption("markexpr") or ""):
        return
    skip = pytest.mark.skip(reason="embeds with the real model; select with -m real_model")
    for item in items:
        if item.get_closest_marker("real_model") is not None:
            item.add_marker(skip)


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


# The home of whoever runs the suite, read when this module is imported:
# before any test points HOME at a folder of its own.
_REAL_HOME = os.path.expanduser("~")


@pytest.fixture(scope="session")
def real_home():
    """The home directory of whoever runs the suite, for a test that reads a
    file a developer keeps there (a downloaded dataset). Every test runs with
    its own empty HOME; nothing writes here."""
    from pathlib import Path

    return Path(_REAL_HOME)


@pytest.fixture(autouse=True)
def _isolate_user_home(tmp_path_factory, monkeypatch):
    """Run every test with a throwaway home directory.

    ``Path.home()`` and ``os.path.expanduser`` read ``$HOME``, and code under
    test expands ``~`` paths: the default ``~/.durin`` when DURIN_HOME is
    unset, ``~/.cache/durin``, a workspace a test names as ``~/...``. With the
    real home, a test that left DURIN_HOME unset or resolved such a path
    created folders in the home of whoever ran the suite (``~/.durin/workspace``,
    ``~/custom-workspace``). Each test gets its own empty home, in a subfolder
    of the run's base temp. A test that reads a file from the real home takes
    its path from ``real_home``; one that needs the real home's behaviour sets
    ``HOME`` itself.
    """
    import tempfile

    homes = tmp_path_factory.getbasetemp() / "user_homes"
    homes.mkdir(exist_ok=True)
    home = tempfile.mkdtemp(prefix="home", dir=homes)
    monkeypatch.setenv("HOME", home)
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", home)


# The test this process ran last, named when a secret store turns up cached
# between tests.
_LAST_TEST: dict = {"nodeid": None}


@pytest.fixture(autouse=True)
def _isolate_durin_home(tmp_path_factory, monkeypatch, request):
    """Run every test as a throwaway durin instance.

    ``durin_home()`` reads ``$DURIN_HOME`` with priority over ``Path.home()``,
    so an ambient DURIN_HOME (a dev shell) would leak into tests. Unset, it
    falls back to ``~/.durin``, which the throwaway HOME above keeps away from
    the real one (where a running daemon holds SQLite files and locks). Pin a
    fresh per-test durin home so tests see one instance whatever the ambient
    environment. A test that needs the default/unset behaviour controls
    DURIN_HOME itself (see ``tests/config/test_config_paths.py``).

    The secret store is cached in a module global
    (``durin.security.secrets._STORE``), built on first use for the config
    path active then. Kept across tests, a secret one test stored reached
    every later test in the process: an exec-scoped one showed up in the next
    test's exec environment. Each test starts with no store and builds its
    own, from its own home; the global is emptied again when the test ends.
    A store found cached when a test starts was built outside any test, by
    something a test left running or at import, and fails that test, naming
    the test that ran before it.
    """
    import tempfile
    from pathlib import Path

    import durin.config.loader as _loader
    import durin.security.secrets as _secrets

    # One folder per test, created with mkdtemp's random name inside a
    # subfolder of the run's base temp. tmp_path_factory.mktemp numbers its
    # folders by scanning every entry already in the base temp, so a fresh
    # numbered folder per test made each test's setup slower than the one
    # before it, quadratically over a full run.
    homes = tmp_path_factory.getbasetemp() / "durin_homes"
    homes.mkdir(exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="home", dir=homes))
    monkeypatch.setenv("DURIN_HOME", str(home))
    monkeypatch.setattr(_loader, "_current_config_path", None, raising=False)
    previous, _LAST_TEST["nodeid"] = _LAST_TEST["nodeid"], request.node.nodeid
    if _secrets._STORE is not None:
        stray, _secrets._STORE = _secrets._STORE.path, None
        pytest.fail(
            f"a secret store for {stray} was cached in "
            f"durin.security.secrets._STORE when this test started, after "
            f"{previous or 'no test'}: it was built outside any test, by "
            "something left running or at import.",
            pytrace=False,
        )
    monkeypatch.setattr(_secrets, "_STORE", None)
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
def _no_package_installs(monkeypatch):
    """Fail the test that reaches the extras installer, naming the command.

    ``ensure_extra`` (durin/extras.py) installs a missing optional extra
    through ``subprocess.run`` when a feature needs it, gated by
    ``install.auto_install_extras`` — on by default, so on in every test's
    fresh home. Code under test that reaches it for real (``durin doctor``
    reading the embedding catalog without fastembed did) installs into the
    environment running the suite, mid-run: every later test then sees the
    extra, and outcomes depend on test order. Here the installer's
    subprocess is refused with the error a failed install raises, and the
    test fails at teardown. A test that exercises the installer replaces
    ``subprocess.run`` on top of this with its own fake.
    """
    import subprocess
    import types

    import durin.extras as _extras

    refused: list = []

    def _refuse(cmd, *args, **kwargs):
        refused.append(cmd)
        raise subprocess.CalledProcessError(
            1, cmd, stderr="installing packages is refused in the test suite")

    monkeypatch.setattr(
        _extras, "subprocess",
        types.SimpleNamespace(run=_refuse, CalledProcessError=subprocess.CalledProcessError),
    )
    yield
    if refused:
        pytest.fail(
            "test reached the extras installer, which would have run: "
            + " ".join(str(part) for part in refused[0])
            + ". Tests never install packages: fake the missing extra, or "
            "the installer, at the boundary the code uses.",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _skill_writes_skip_the_vector_index(monkeypatch):
    """Skill writes keep the FTS index in step but never embed.

    Every skill write and curation stamp re-indexes the skill, and when the
    [memory] extra is importable that includes an embedding through the real
    model: a model load (a download, on a machine without it cached) and
    about a second per write, paid by skill tests that never look at
    vectors. The vector index has its own tests, which build it directly.
    """
    import durin.agent.skills_store as _skills_store

    monkeypatch.setattr(_skills_store, "_vector_index_for", lambda workspace: None)


@pytest.fixture(autouse=True)
def _remove_loguru_sinks_left_by_a_test():
    """Remove every loguru sink a test added and left behind.

    The gateway and dream-worker commands attach an enqueued JSONL file sink
    for the life of their process. A test process outlives each command, so
    every invocation left one more sink and writer thread behind, and every
    later INFO record was serialized into all of them.
    """
    from contextlib import suppress

    from loguru import logger

    before = set(logger._core.handlers)
    yield
    for handler_id in set(logger._core.handlers) - before:
        with suppress(ValueError):
            logger.remove(handler_id)


@pytest.fixture(autouse=True)
def _no_outbound_network(request):
    """Fail the test that tried to reach another host, naming the target.

    The socket guard installed at the top of this module refuses every
    connection to a non-loopback address and every host-name lookup that
    would ask a DNS server, and records each refusal. Code under test sees
    the error an offline machine gives; the test fails at teardown, because
    a test that reaches the network depends on it — slow, flaky, and gone
    offline. Tests fake the network at the boundary the code uses (the
    resolver, the HTTP client, the fetch function). Loopback stays open, so
    tests that serve on localhost keep working. A test marked ``network``
    (the opt-in live registry tests) or ``real_model`` (the opt-in tests that
    download the real embedding model when it is not cached) may reach the
    network.
    """
    _NETWORK_GUARD["open"] = any(
        request.node.get_closest_marker(name) is not None for name in ("network", "real_model"))
    _NETWORK_GUARD["refusals"].clear()
    yield
    _NETWORK_GUARD["open"] = False
    refusals = list(_NETWORK_GUARD["refusals"])
    _NETWORK_GUARD["refusals"].clear()
    if refusals:
        pytest.fail(
            "test tried to reach the network: " + "; ".join(refusals)
            + ". Fake the network at the boundary the code uses.",
            pytrace=False,
        )


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
