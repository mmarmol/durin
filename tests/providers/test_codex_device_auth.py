import base64
import json
import time

import pytest

pytest.importorskip("oauth_cli_kit")

from durin.providers import codex_device_auth as cda


def _make_jwt(claims: dict) -> str:
    def seg(d: dict) -> str:
        raw = json.dumps(d).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


def test_account_id_from_jwt_reads_nested_claim():
    tok = _make_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct_123"}})
    assert cda.account_id_from_jwt(tok) == "acct_123"


def test_account_id_from_jwt_tolerates_garbage():
    assert cda.account_id_from_jwt("not-a-jwt") is None
    assert cda.account_id_from_jwt("") is None


def test_expiry_ms_from_jwt_uses_exp_claim():
    exp = int(time.time()) + 3600
    tok = _make_jwt({"exp": exp})
    assert cda.expiry_ms_from_jwt(tok) == exp * 1000


def test_expiry_ms_from_jwt_falls_back_when_missing(monkeypatch):
    monkeypatch.setattr(cda.time, "time", lambda: 1000.0)
    tok = _make_jwt({"no": "exp"})
    assert cda.expiry_ms_from_jwt(tok) == (1000 + 3600) * 1000
