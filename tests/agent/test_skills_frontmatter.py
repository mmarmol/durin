from durin.agent.skills_frontmatter import (
    ensure_durin,
    join_frontmatter,
    split_frontmatter,
)


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
