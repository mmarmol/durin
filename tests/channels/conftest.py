"""Channel-test isolation.

Channel tests that hit ``/webui/bootstrap`` mint persisted API tokens via
``ApiTokenStore`` (``get_data_dir()/api_tokens.json``) and read/persist the
media HMAC secret there too. Without isolation they would write to the real
``~/.durin`` and accumulate junk tokens across runs. Point the data dir at a
per-test tmp directory so the suite never touches the developer's real store.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)
    yield
