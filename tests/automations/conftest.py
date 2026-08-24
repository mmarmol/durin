"""Belt-and-suspenders telemetry isolation for the automations test suite.

Layered on top of the session-scoped ``_isolate_telemetry_dir`` fixture in
``tests/conftest.py``: every automation dispatch entrypoint
(``fire``/``try_fire``/``answer``) binds its own ``automation:<name>``
session telemetry logger when it runs outside a live agent turn
(``AutomationsRuntime._bind_automations_telemetry``), so nearly every test in
this directory touches ``durin.telemetry.logger.get_session_logger``.
Re-patching ``_DEFAULT_DIR`` here, function-scoped and autouse, means a test
collected under this directory can never reach the real
``~/.cache/durin/telemetry`` even if something elsewhere in a broader suite
run has reset the session-scoped patch — the two guards are independent, so
one surviving is enough. Mirrors the pattern in
``tests/tools/test_workflow_runs_tool.py``'s ``cost_workspace`` fixture.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_automations_telemetry_dir(tmp_path, monkeypatch):
    import durin.telemetry.logger as telemetry_logger

    monkeypatch.setattr(telemetry_logger, "_DEFAULT_DIR", tmp_path / "_telemetry")
