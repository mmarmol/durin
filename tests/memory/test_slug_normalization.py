"""Slug normalization contract.

The slug derivation pipeline is:
  1. Unicode NFC normalize
  2. Transliterate non-Latin scripts to Latin
  3. Lowercase
  4. Replace whitespace and punctuation with single underscores
  5. Strip leading/trailing underscores
  6. Truncate to 64 chars

On slug collision, the caller appends a numeric suffix (`_2`, `_3`, ...).
"""

from __future__ import annotations

from pathlib import Path

from durin.memory.entities import (
    resolve_slug_collision,
    slugify_name,
)

# ---------------------------------------------------------------------------
# slugify_name — the pure transformation (no I/O, no collision check)
# ---------------------------------------------------------------------------


class TestSlugifyName:
    def test_plain_ascii_word(self) -> None:
        assert slugify_name("Marcelo") == "marcelo"

    def test_spaces_to_underscore(self) -> None:
        assert slugify_name("Marcelo Marmol") == "marcelo_marmol"

    def test_diacritics_stripped(self) -> None:
        """Spec example: Mármol → marmol."""
        assert slugify_name("Marcelo Mármol") == "marcelo_marmol"

    def test_punctuation_to_underscore(self) -> None:
        """Spec example: auth middleware leak (high sev) →
        auth_middleware_leak_high_sev."""
        result = slugify_name("auth middleware leak (high sev)")
        assert result == "auth_middleware_leak_high_sev"

    def test_consecutive_punctuation_collapses(self) -> None:
        assert slugify_name("a---b---c") == "a_b_c"

    def test_mixed_case_lowered(self) -> None:
        assert slugify_name("AcmeCorp Q4 Renewal") == "acmecorp_q4_renewal"

    def test_strips_leading_trailing_underscores(self) -> None:
        assert slugify_name("  ___marcelo___  ") == "marcelo"

    def test_chinese_transliterated(self) -> None:
        """CJK is romanized; the exact transliteration depends on
        unidecode's table, but the output must be ASCII, lowercase,
        underscore-separated, non-empty."""
        result = slugify_name("马塞洛")
        assert result, "CJK input must produce a non-empty slug"
        assert result == result.lower()
        # ASCII only
        assert all(ord(c) < 128 for c in result)
        # No leading/trailing underscores
        assert not result.startswith("_") and not result.endswith("_")

    def test_cyrillic_transliterated(self) -> None:
        # Москва (Moscow) — transliterates to something like "moskva"
        result = slugify_name("Москва")
        assert result, "Cyrillic input must produce a non-empty slug"
        assert all(ord(c) < 128 for c in result)

    def test_truncates_to_64(self) -> None:
        very_long = "marcelo " * 20  # 160 chars before truncation
        result = slugify_name(very_long)
        assert len(result) <= 64
        # No trailing underscore from a chopped word
        assert not result.endswith("_")

    def test_empty_name_falls_back(self) -> None:
        # An empty / all-punctuation name still has to produce a usable slug.
        assert slugify_name("") == "unnamed"
        assert slugify_name("   ") == "unnamed"
        assert slugify_name("!!!") == "unnamed"

    def test_emoji_dropped(self) -> None:
        result = slugify_name("Project Alpha 🚀")
        assert result == "project_alpha"

    def test_digits_preserved(self) -> None:
        assert slugify_name("Q4 2026 Retro") == "q4_2026_retro"


# ---------------------------------------------------------------------------
# resolve_slug_collision — appends _2, _3, ... when slug exists on disk
# ---------------------------------------------------------------------------


class TestResolveSlugCollision:
    def test_no_collision_returns_base(self, tmp_path: Path) -> None:
        type_dir = tmp_path / "memory" / "entities" / "person"
        type_dir.mkdir(parents=True)
        assert resolve_slug_collision(tmp_path, "person", "marcelo") == "marcelo"

    def test_first_collision_suffix_2(self, tmp_path: Path) -> None:
        type_dir = tmp_path / "memory" / "entities" / "person"
        type_dir.mkdir(parents=True)
        (type_dir / "marcelo.md").write_text("placeholder", encoding="utf-8")
        assert resolve_slug_collision(tmp_path, "person", "marcelo") == "marcelo_2"

    def test_second_collision_suffix_3(self, tmp_path: Path) -> None:
        type_dir = tmp_path / "memory" / "entities" / "person"
        type_dir.mkdir(parents=True)
        (type_dir / "marcelo.md").write_text("a", encoding="utf-8")
        (type_dir / "marcelo_2.md").write_text("b", encoding="utf-8")
        assert resolve_slug_collision(tmp_path, "person", "marcelo") == "marcelo_3"

    def test_collision_includes_archive(self, tmp_path: Path) -> None:
        """An archived slug is still 'taken' — collision must skip it
        to avoid reviving an absorbed page's URI."""
        (tmp_path / "memory" / "entities" / "person").mkdir(parents=True)
        archive_dir = tmp_path / "memory" / "archive" / "entities" / "person"
        archive_dir.mkdir(parents=True)
        (archive_dir / "marcelo.md").write_text("archived", encoding="utf-8")
        assert resolve_slug_collision(tmp_path, "person", "marcelo") == "marcelo_2"

    def test_collision_different_type_independent(self, tmp_path: Path) -> None:
        """Slugs are scoped by type — `person:marcelo` and `topic:marcelo`
        are different entities and do not collide."""
        (tmp_path / "memory" / "entities" / "person").mkdir(parents=True)
        (tmp_path / "memory" / "entities" / "person" / "marcelo.md").write_text(
            "p", encoding="utf-8",
        )
        # Topic with same slug — no collision.
        assert resolve_slug_collision(tmp_path, "topic", "marcelo") == "marcelo"
