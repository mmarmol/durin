"""``durin.providers.__getattr__`` must resolve real submodules via attribute
access (not just the lazy provider classes), so pytest's string-target
``monkeypatch.setattr`` over ``durin.providers.<submodule>.<attr>`` works even
when the submodule has not been imported yet."""

import pytest

import durin.providers as p


def test_getattr_resolves_real_submodule():
    mod = p.__getattr__("factory")
    assert mod.__name__ == "durin.providers.factory"


def test_getattr_still_exposes_lazy_provider_classes():
    assert p.__getattr__("AnthropicProvider").__name__ == "AnthropicProvider"


def test_getattr_unknown_name_raises_attribute_error():
    with pytest.raises(AttributeError):
        p.__getattr__("definitely_not_a_real_attribute_xyz")


def test_getattr_private_name_raises_without_import_attempt():
    with pytest.raises(AttributeError):
        p.__getattr__("_not_a_submodule")


def test_string_monkeypatch_of_submodule_attr_works(monkeypatch):
    # The exact pattern that used to fail in certain test subsets: pytest walks
    # durin -> providers -> codex_device_auth -> codex_token_present by getattr.
    monkeypatch.setattr(
        "durin.providers.codex_device_auth.codex_token_present", lambda: True
    )
    from durin.providers.codex_device_auth import codex_token_present

    assert codex_token_present() is True
