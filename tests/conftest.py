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

import pytest

from durin.memory.provenance import author_scope


@pytest.fixture(autouse=True)
def _test_default_author_scope():
    with author_scope("agent_created"):
        yield
