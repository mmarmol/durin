import base64
import json

import pytest

pytest.importorskip("oauth_cli_kit")

from durin.providers import openai_codex_provider as ocp


def _make_jwt(account_id: str) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


def test_headers_use_codex_cli_originator():
    h = ocp._build_headers("acct_1", "tok")
    assert h["originator"] == "codex_cli_rs"
    assert h["User-Agent"].startswith("codex_cli_rs")


def test_headers_recover_account_id_from_jwt_when_missing():
    access = _make_jwt("acct_77")
    h = ocp._build_headers(None, access)
    assert h["chatgpt-account-id"] == "acct_77"


def test_default_model_is_gpt55():
    assert ocp.OpenAICodexProvider().get_default_model() == "openai-codex/gpt-5.5"
