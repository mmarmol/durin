"""Tests for the mtime-validated load_config cache (durin/config/loader.py)."""

from __future__ import annotations

from durin.config import loader
from durin.config.loader import load_config, mutate_config, save_config


def _prime(cfg_path):
    cfg = load_config(cfg_path)
    save_config(cfg, cfg_path)
    # First post-save load migrates the legacy monolith to the split layout,
    # mutating the on-disk state mid-read — so its pre-read snapshot is stale
    # by design and the NEXT load re-reads once. Prime to steady state.
    load_config(cfg_path)
    return load_config(cfg_path)


def test_load_config_cached_between_calls(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    _prime(cfg_path)

    calls = {"n": 0}
    real = loader._is_split_layout

    def counting(path=None):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(loader, "_is_split_layout", counting)
    load_config(cfg_path)
    assert calls["n"] == 0  # served from cache: no disk probe of the layout


def test_load_config_cache_invalidated_by_write(tmp_path):
    cfg_path = tmp_path / "config.json"
    c1 = _prime(cfg_path)
    c1.gateway.port = 45678
    save_config(c1, cfg_path)
    c2 = load_config(cfg_path)
    assert c2.gateway.port == 45678


def test_load_config_cache_invalidated_by_foreign_write(tmp_path):
    """A write NOT routed through save_config (another process editing a
    split-topic file directly) must also invalidate via mtime/size."""
    cfg_path = tmp_path / "config.json"
    c1 = _prime(cfg_path)
    c1.gateway.port = 45678
    save_config(c1, cfg_path)
    assert load_config(cfg_path).gateway.port == 45678

    split = loader._split_dir(cfg_path)
    gateway_file = split / "gateway.json"
    assert gateway_file.exists()
    gateway_file.write_text('{"port": 45679}', encoding="utf-8")
    assert load_config(cfg_path).gateway.port == 45679


def test_load_config_hit_returns_independent_copy(tmp_path):
    cfg_path = tmp_path / "config.json"
    _prime(cfg_path)
    a = load_config(cfg_path)
    b = load_config(cfg_path)
    a.gateway.port = 50000
    assert b.gateway.port != 50000
    assert load_config(cfg_path).gateway.port != 50000


def test_mutate_config_roundtrip_with_cache(tmp_path):
    cfg_path = tmp_path / "config.json"
    _prime(cfg_path)

    def _set(cfg):
        cfg.gateway.port = 40123

    mutate_config(_set, cfg_path)
    assert load_config(cfg_path).gateway.port == 40123
