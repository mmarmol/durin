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


# Starlette's TestClient defaults the ASGI scope's client address to
# ("testclient", 50000), which is NOT a loopback IP. durin's /webui/bootstrap
# gates unauthenticated ADMIN-token minting on a real localhost peer when no
# token_issue_secret is set (durin/api/asgi.py bootstrap_handler + _is_localhost).
# An in-process TestClient genuinely IS a local client, so default its peer to
# 127.0.0.1 for the whole suite. Tests exercising the remote-rejection path pass
# client=(...) explicitly.
import starlette.testclient as _starlette_testclient  # noqa: E402

_orig_testclient_init = _starlette_testclient.TestClient.__init__


def _testclient_init_localhost(self, *args, **kwargs):
    kwargs.setdefault("client", ("127.0.0.1", 0))
    return _orig_testclient_init(self, *args, **kwargs)


_starlette_testclient.TestClient.__init__ = _testclient_init_localhost


@pytest.fixture(autouse=True)
def _test_default_author_scope():
    with author_scope("agent_created"):
        yield
