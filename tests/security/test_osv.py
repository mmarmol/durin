"""Tests for the shared OSV malware-lookup helper (durin/security/osv.py)."""
from durin.security import osv


def test_query_malware_filters_mal_ids(monkeypatch):
    def fake_post(payload, timeout):
        return {"vulns": [{"id": "MAL-2024-1"}, {"id": "GHSA-xxxx"}, {"id": "MAL-2024-2"}]}
    monkeypatch.setattr(osv, "_post_query", fake_post)
    assert osv.query_malware("evil", "PyPI") == ["MAL-2024-1", "MAL-2024-2"]


def test_query_malware_clean(monkeypatch):
    monkeypatch.setattr(osv, "_post_query", lambda payload, timeout: {"vulns": [{"id": "GHSA-y"}]})
    assert osv.query_malware("ok", "npm") == []


def test_query_malware_empty_result(monkeypatch):
    monkeypatch.setattr(osv, "_post_query", lambda payload, timeout: {})
    assert osv.query_malware("ok", "PyPI") == []
