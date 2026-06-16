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
    ("testclient", 50000), which is NOT a loopback IP. durin's /webui/bootstrap
    gates unauthenticated ADMIN-token minting on a real localhost peer when no
    token_issue_secret is set (durin/api/asgi.py bootstrap_handler + _is_localhost).
    An in-process TestClient genuinely IS a local client, so model its peer as
    127.0.0.1. A test exercising the remote-rejection path passes ``client=(...)``
    explicitly (``setdefault`` leaves it untouched).

    Session-scoped and self-undoing so the patch is tied to the pytest run, not a
    permanent import-time mutation. Safe because no test constructs a TestClient
    at module/collection time — they all build it inside fixtures/functions, which
    run after this fixture is set up.
    """
    import starlette.testclient as stc

    original_init = stc.TestClient.__init__

    def _init_with_localhost_peer(self, *args, **kwargs):
        kwargs.setdefault("client", ("127.0.0.1", 0))
        return original_init(self, *args, **kwargs)

    stc.TestClient.__init__ = _init_with_localhost_peer
    try:
        yield
    finally:
        stc.TestClient.__init__ = original_init


@pytest.fixture(autouse=True)
def _test_default_author_scope():
    with author_scope("agent_created"):
        yield


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
