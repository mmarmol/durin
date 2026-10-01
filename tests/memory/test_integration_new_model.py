"""Cross-module integration + edge-case tests for the new memory model.

Composes the Phase 1-7 modules and the existing readers on shared state, plus
the edge cases the per-phase unit tests didn't cover. Surfaced two real fixes
(attribute search, the _merge_pages data loss) during the pre-Phase-8 review.
"""
import threading
from datetime import datetime, timezone

import pytest

from durin.memory.entity_page import EntityPage
from durin.memory.extract_dream import extract_entity
from durin.memory.field_patch import FieldPatch
from durin.memory.graph import build_memory_graph
from durin.memory.memory_writer import write_entity
from durin.memory.reference import chunk_by_tokens, ingest_reference
from durin.memory.search import search_memory

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)

_OLD_FORMAT = (
    "---\n"
    "type: company\n"
    "name: OldCo\n"
    "attributes:\n"
    "  hq: Boston\n"
    "provenance:\n"            # old shape: source_ref only, no author/extracted_at
    "  attributes:\n"
    "    hq:\n"
    "      source_ref: '[[old]]'\n"
    "author: user_authored\n"
    "---\n\n"
    "OldCo is an old-format entity page.\n"
)


def _write_raw(ws, ref, text):
    type_, _, slug = ref.partition(":")
    p = ws / "memory" / "entities" / type_ / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_old_format_entity_readable_and_in_graph(tmp_path):
    _write_raw(tmp_path, "company:oldco", _OLD_FORMAT)
    page = EntityPage.from_file(tmp_path / "memory/entities/company/oldco.md")
    assert page is not None and page.attributes["hq"] == "Boston"
    g = build_memory_graph(tmp_path, include_sessions=False)
    assert any(n["id"] == "company:oldco" for n in g["nodes"])


def test_concurrent_same_field_precedence_user_wins(tmp_path):
    # Two writers race on the SAME field; the user value must win regardless of
    # commit order (precedence holds under optimistic CAS retry).
    write_entity(tmp_path, "company:x",
                 [FieldPatch(kind="body_append", value="seed", author="agent",
                             source_ref="s", at=NOW)], create=True)

    def w(author, val):
        write_entity(tmp_path, "company:x",
                     [FieldPatch(kind="attribute", key="hq", value=val,
                                 author=author, source_ref="s", at=NOW)])

    ts = [threading.Thread(target=w, args=("agent", "AgentVal")),
          threading.Thread(target=w, args=("user", "UserVal"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    page = EntityPage.from_file(tmp_path / "memory/entities/company/x.md")
    assert page.attributes["hq"] == "UserVal"


def test_attribute_and_relation_searchable(tmp_path):
    # Dream-extracted attributes + relation targets must be findable by search
    # (the warm grep used to only see name/aliases/body).
    write_entity(tmp_path, "company:acme",
                 [FieldPatch(kind="attribute", key="hq", value="Boston",
                             author="dream", source_ref="s", at=NOW),
                  FieldPatch(kind="relation", value={"to": "person:alex", "type": "ceo"},
                             author="agent", source_ref="s", at=NOW)],
                 create=True, name="Acme")
    assert [r for r in search_memory(tmp_path, "Boston") if r.class_name == "entity_page"]
    assert [r for r in search_memory(tmp_path, "alex") if r.class_name == "entity_page"]


def test_reference_edge_cases(tmp_path):
    assert chunk_by_tokens("") == []
    assert len(chunk_by_tokens("a short note")) == 1
    assert ingest_reference(tmp_path, "Empty", "").chunk_count == 0
    assert ingest_reference(tmp_path, "OneWord", "supercalifragilistic").chunk_count == 1


def test_extract_handles_malformed_llm_output(tmp_path):
    write_entity(tmp_path, "company:x",
                 [FieldPatch(kind="body_append", value="b", author="agent",
                             source_ref="s", at=NOW)], create=True)
    for bad in ["not json", "[1,2,3]", "```\ngarbage\n```", '{"a":{"nested":1}}']:
        res = extract_entity(tmp_path, "company:x", "turns", llm_invoke=lambda p, **k: bad)
        assert res.committed is False


def test_uncommitted_on_disk_entity_errors_not_clobbers(tmp_path):
    # An entity present in the working tree but not committed (a manual / Obsidian
    # edit) is invisible to memory_writer (which reads HEAD). create=False must
    # ERROR rather than silently proceed — the safe failure. (The create=True
    # clobber is the human-edit phase; see checkpoint_prephase8_readiness.md.)
    _write_raw(tmp_path, "company:oldco", _OLD_FORMAT)
    with pytest.raises(FileNotFoundError):
        write_entity(tmp_path, "company:oldco",
                     [FieldPatch(kind="relation", value={"to": "person:x", "type": "ceo"},
                                 author="agent", source_ref="s", at=NOW)])


# --- comprehensive searchability across entity types + retrieval mechanisms ---

def _reindex_all(ws):
    from durin.memory.indexer import reindex_one_file
    for p in (ws / "memory" / "entities").rglob("*.md"):
        reindex_one_file(ws, p)


def _A(k, v):
    return FieldPatch(kind="attribute", key=k, value=v, author="dream", source_ref="s", at=NOW)


def _grep(ws, q, want):
    return any(want in r.uri for r in search_memory(ws, q))


def _fts(ws, q, want_slug):
    from durin.memory.fts_index import FTSIndex
    with FTSIndex.open(ws) as idx:
        return any(want_slug in str(getattr(h, "uri", h)) for h in idx.search(q, limit=20))


def test_all_entity_types_searchable_grep_and_fts(tmp_path):
    # open type vocabulary + diverse attribute kinds (string / int / list /
    # relation / body), found by both the grep and FTS paths.
    write_entity(tmp_path, "person:alex", [_A("role", "founder")], create=True, name="Alex Panagides")
    write_entity(tmp_path, "company:mxhero",
                 [FieldPatch(kind="alias", value="mxHERO", author="agent", source_ref="s", at=NOW),
                  _A("hq_country", "Argentina"), _A("founding_year", 2012),
                  _A("products", ["mail2cloud", "supervisor"]),
                  FieldPatch(kind="relation", value={"to": "company:box", "type": "partner"},
                             author="agent", source_ref="s", at=NOW)],
                 create=True, name="mxHERO Inc")
    write_entity(tmp_path, "topic:smtp", [_A("default_port", 587)], create=True, name="SMTP")
    write_entity(tmp_path, "stance:rigor",
                 [FieldPatch(kind="body_append", value="Prefer rigor over speed.",
                             author="agent", source_ref="s", at=NOW)], create=True, name="Rigor")
    _reindex_all(tmp_path)

    cases = [("Panagides", "person:alex"), ("mxHERO", "company:mxhero"),
             ("Argentina", "company:mxhero"), ("2012", "company:mxhero"),
             ("supervisor", "company:mxhero"), ("box", "company:mxhero"),
             ("587", "topic:smtp"), ("rigor over speed", "stance:rigor")]
    for q, want in cases:
        assert _grep(tmp_path, q, want), f"grep miss: {q} -> {want}"
        assert _fts(tmp_path, q, want.split(":")[1]), f"fts miss: {q} -> {want}"


def test_dream_transformed_entity_fully_searchable(tmp_path):
    # After agent-author -> extract (attributes) -> refine (merge), every datum
    # (agent body/relation, dream attributes, refine-merged alias) survives and
    # is searchable by grep + FTS.
    from durin.memory.extract_dream import extract_entity
    from durin.memory.refine_dream import run_refine

    write_entity(tmp_path, "company:globex",
                 [FieldPatch(kind="alias", value="Globex Corp", author="agent", source_ref="s", at=NOW),
                  FieldPatch(kind="relation", value={"to": "person:hank", "type": "founded_by"},
                             author="agent", source_ref="s", at=NOW),
                  FieldPatch(kind="body_append", value="An energy conglomerate.",
                             author="agent", source_ref="s", at=NOW)],
                 create=True, name="Globex")
    extract_entity(tmp_path, "company:globex", "facts",
                   llm_invoke=lambda p, **k: '{"founding_year":1989,"headquarters":"Cypress Creek"}')
    write_entity(tmp_path, "company:globex_incorporated",
                 [FieldPatch(kind="alias", value="Globex Corp", author="agent", source_ref="s", at=NOW)],
                 create=True, name="Globex Incorporated")
    assert run_refine(tmp_path,
                      llm_invoke=lambda p, **k: "===VERDICT===\nsame\n===CONFIDENCE===\n98\n"
                                                "===REASONING===\nx\n===END===")["merged"]
    page = EntityPage.from_file(tmp_path / "memory/entities/company/globex.md")
    assert "conglomerate" in page.body                       # agent body
    assert page.attributes.get("founding_year") == 1989       # dream attribute
    assert "Globex Incorporated" in page.aliases              # refine-merged alias
    assert {"to": "person:hank", "type": "founded_by"} in page.relations

    _reindex_all(tmp_path)
    for q, want in [("Cypress Creek", "company:globex"), ("1989", "company:globex"),
                    ("Globex Incorporated", "company:globex"), ("hank", "company:globex"),
                    ("conglomerate", "company:globex")]:
        assert _grep(tmp_path, q, want), f"grep miss: {q}"
        assert _fts(tmp_path, q, "globex"), f"fts miss: {q}"


# mxHERO and two other companies, so a vector check has to rank mxHERO first
# instead of getting back the only page there is. The others each share a
# little with a query: Globex names Argentina in its body, Initech has an HQ
# country and a cloud industry of its own.
_COMPANIES = {
    "mxhero": ("mxHERO", {"hq_country": "Argentina"}, "An email-to-cloud company."),
    "globex": ("Globex", {}, "A steel conglomerate with a sales office in Argentina."),
    "initech": ("Initech", {"hq_country": "United States", "industry": "cloud payroll"},
                "A payroll software vendor."),
}


def _index_companies(ws, vi, companies=_COMPANIES):
    for slug, (name, attributes, body) in companies.items():
        patches = [_A(k, v) for k, v in attributes.items()]
        patches.append(FieldPatch(kind="body_append", value=body, author="agent",
                                  source_ref="s", at=NOW))
        write_entity(ws, f"company:{slug}", patches, create=True, name=name)
        path = ws / f"memory/entities/company/{slug}.md"
        page = EntityPage.from_file(path)
        vi.upsert_entity_page(entity_ref=f"company:{slug}", name=page.name,
                              aliases=list(page.aliases), body=page.body, path=path,
                              attributes=dict(page.attributes), relations=list(page.relations))


def _top_hit(vi, query):
    hits = vi.search(query, top_k=5)
    return str(hits[0].get("id", "")) if hits else None


def test_vector_search_finds_attributes_and_body(tmp_path, embedding_model):
    # The embedding includes attributes/relations, not just the body. The
    # `embedding_model` fixture embeds with the fastembed stand-in, which
    # matches shared words: no model is loaded or downloaded.
    from durin.memory.embedding import FastembedProvider
    from durin.memory.vector_index import VectorIndex

    vi = VectorIndex(tmp_path, FastembedProvider(model=embedding_model))
    _index_companies(tmp_path, vi)
    assert _top_hit(vi, "hq country Argentina") == "company:mxhero"        # attribute, embedded
    assert _top_hit(vi, "an email to cloud company") == "company:mxhero"   # body, embedded


# What each company does and nothing else: no country or other attribute that
# could lean toward a Spanish query, so only the meaning of a body can put
# mxHERO first. Initech's is about the cloud, Crumbs' about mail.
_WHAT_THEY_DO = {
    "mxhero": ("mxHERO", {}, "An email-to-cloud company."),
    "initech": ("Initech", {}, "A cloud payroll software vendor."),
    "crumbs": ("Crumbs", {}, "A bakery that ships bread by mail."),
    "globex": ("Globex", {}, "A steel conglomerate."),
}


@pytest.mark.real_model
def test_vector_search_recall_crosses_languages(tmp_path):
    """The vector path adds SEMANTIC recall the token paths miss: a Spanish
    query finds the English page that means the same. Only a real multilingual
    model shows that, so this embeds with the real E5 model, its query and
    passage prefixes included, and runs only when selected:
    ``pytest -m real_model``. The model loads from fastembed's cache, which a
    first run fills by downloading it (about 450 MB)."""
    pytest.importorskip("fastembed")
    from durin.memory.embedding import FastembedProvider
    from durin.memory.vector_index import VectorIndex, vector_index_available

    if not vector_index_available():
        pytest.skip("vector index unavailable in this environment")
    vi = VectorIndex(tmp_path, FastembedProvider(model="intfloat/multilingual-e5-small"))
    _index_companies(tmp_path, vi, _WHAT_THEY_DO)
    # "email company in the cloud": no word in common with any page
    assert _top_hit(vi, "empresa de correo electrónico en la nube") == "company:mxhero"
