import pytest
from durin.souls.store import SoulStore, DEFAULT_SLUG


def test_default_slug_maps_to_soul_md(tmp_path):
    (tmp_path / "SOUL.md").write_text("# Soul\nI am the default.", encoding="utf-8")
    store = SoulStore(tmp_path)
    assert store.read(DEFAULT_SLUG) == "# Soul\nI am the default."
    assert store.exists(DEFAULT_SLUG)


def test_named_soul_roundtrip(tmp_path):
    store = SoulStore(tmp_path)
    store.write("researcher", "# Soul\nI am a research analyst.")
    assert (tmp_path / "souls" / "researcher.md").exists()
    assert store.read("researcher") == "# Soul\nI am a research analyst."


def test_list_includes_default_and_named(tmp_path):
    (tmp_path / "SOUL.md").write_text("x", encoding="utf-8")
    store = SoulStore(tmp_path)
    store.write("engineer", "y")
    store.write("tutor", "z")
    assert store.list() == ["default", "engineer", "tutor"]


def test_read_missing_returns_empty(tmp_path):
    assert SoulStore(tmp_path).read("nope") == ""


def test_invalid_slug_rejected(tmp_path):
    with pytest.raises(ValueError):
        SoulStore(tmp_path).read("../etc/passwd")


def test_cannot_delete_default(tmp_path):
    with pytest.raises(ValueError):
        SoulStore(tmp_path).delete(DEFAULT_SLUG)
