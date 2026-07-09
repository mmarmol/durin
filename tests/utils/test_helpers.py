from __future__ import annotations

from durin.utils.helpers import convert_gfm_tables


def test_convert_gfm_tables_to_bullets() -> None:
    text = (
        "Intro\n"
        "| Name | Age |\n"
        "| --- | --- |\n"
        "| Ana | 30 |\n"
        "| Luis | 25 |\n"
        "Outro"
    )
    result = convert_gfm_tables(text)
    assert "|" not in result
    assert "- **Ana** — Age: 30" in result
    assert "- **Luis** — Age: 25" in result
    assert result.startswith("Intro") and result.endswith("Outro")


def test_convert_gfm_tables_ignores_fenced_code() -> None:
    text = "```\n| a | b |\n| - | - |\n| 1 | 2 |\n```"
    assert convert_gfm_tables(text) == text


def test_convert_gfm_tables_passthrough_plain_text() -> None:
    assert convert_gfm_tables("no tables | here") == "no tables | here"


def test_convert_gfm_tables_single_column() -> None:
    text = "| H |\n| - |\n| v |"
    result = convert_gfm_tables(text)
    assert result == "- **v**"


def test_convert_gfm_tables_ignores_horizontal_rule_after_pipe_prose() -> None:
    text = "Run: foo | bar\n---\nThen check the | pipe output\nDone"
    assert convert_gfm_tables(text) == text
