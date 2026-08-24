"""Belt-and-suspenders telemetry isolation for the automations test suite.

Layered on top of the session-scoped ``_isolate_telemetry_dir`` fixture in
``tests/conftest.py``: every automation dispatch entrypoint
(``fire``/``try_fire``/``answer``/``answer_nowait``/``stop``) binds its own
``automation:<name>`` session telemetry logger when it runs outside a live
agent turn (``AutomationsRuntime._bind_automations_telemetry``), so nearly
every test in this directory touches
``durin.telemetry.logger.get_session_logger``.

Session-scoped, not function-scoped, and mirrors ``tests/conftest.py``'s own
``tmp_path_factory`` + direct-assignment pattern (``monkeypatch`` is
function-scoped and cannot back a session fixture) for the same reason that
fixture is session-scoped: a `_spawn`ed background task (a queue drain, a
chain fire, an `answer_nowait` continuation) can still be running after the
test function that started it has already returned and torn down its own
function-scoped fixtures — a function-scoped guard here would already have
reverted by the time such a task's own telemetry write actually happens.
Named distinctly from ``_isolate_telemetry_dir`` (the ``tests/conftest.py``
fixture this one layers on top of) on purpose: a fixture defined in a test
module shadows a same-named one from a parent conftest for every test
collected in that module, so reusing that exact name for this file's own
fixture would make it invisible everywhere `pytest` resolves this
conftest's own name over the parent's. Three modules in this directory
(``test_runtime.py``, ``test_matcher.py``, ``test_hooks.py``) separately
define their OWN function-scoped, autouse fixture — named
``_per_test_telemetry_dir``, not ``_isolate_telemetry_dir`` — for a
different reason: those tests read back a single test's own JSONL events,
which needs a fresh, single-file directory per test rather than the one
shared directory this session-scoped fixture (or the suite-wide one in
``tests/conftest.py``) hands out for the whole run. The two layers coexist
without conflict — different names, different scopes, both applying to
those three files' tests at once — this one is the belt that still holds
even where a test's own per-test fixture, or a leftover background task
outliving it, would not be enough on its own.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_automations_telemetry_dir(tmp_path_factory):
    import durin.telemetry.logger as telemetry_logger

    original = telemetry_logger._DEFAULT_DIR
    telemetry_logger._DEFAULT_DIR = tmp_path_factory.mktemp("automations_telemetry")
    try:
        yield
    finally:
        telemetry_logger._DEFAULT_DIR = original
