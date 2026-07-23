from durin.agent.skills_frontmatter import (
    ensure_durin,
    frontmatter_broken,
    join_frontmatter,
    recover_metadata,
    split_frontmatter,
)

_BROKEN = """---
name: x
description: use when drafting a note: claims must show an exhibit.
metadata:
  durin:
    mode: auto
    provenance:
      source: operator
---
BODY
"""


def test_split_returns_data_and_body():
    text = "---\nname: x\ndescription: d\n---\nBODY\n"
    data, body = split_frontmatter(text)
    assert data["name"] == "x"
    assert body == "BODY\n"


def test_split_no_frontmatter_returns_empty_dict():
    data, body = split_frontmatter("no frontmatter here")
    assert data == {}
    assert body == "no frontmatter here"


def test_round_trip_preserves_body_and_adds_durin_field():
    text = "---\nname: x\ndescription: d\n---\nBODY\n"
    data, body = split_frontmatter(text)
    ensure_durin(data)["mode"] = "auto"
    out = join_frontmatter(data, body)
    data2, body2 = split_frontmatter(out)
    assert body2 == "BODY\n"
    assert data2["metadata"]["durin"]["mode"] == "auto"
    assert data2["name"] == "x"


def test_ensure_durin_coerces_non_dict_metadata():
    data = {"metadata": "garbage"}
    durin = ensure_durin(data)
    durin["mode"] = "manual"
    assert data["metadata"]["durin"]["mode"] == "manual"


def test_frontmatter_broken_detects_unquoted_colon():
    assert frontmatter_broken(_BROKEN) is True
    assert frontmatter_broken("---\nname: x\n---\nBODY\n") is False
    assert frontmatter_broken("no frontmatter") is False


def test_recover_metadata_reads_durin_blob_under_broken_yaml():
    meta = recover_metadata(_BROKEN)
    assert meta["durin"]["provenance"]["source"] == "operator"
    assert meta["durin"]["mode"] == "auto"


def test_recover_metadata_empty_when_no_metadata_block():
    broken_no_meta = "---\ndescription: a note: with a colon.\n---\nBODY\n"
    assert recover_metadata(broken_no_meta) == {}
    assert recover_metadata("no frontmatter") == {}
