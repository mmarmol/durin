"""G3 verification — multilingual retrieval scenarios.

Per glm-5.1 R5: run **before** implementing any G3 variant to decide
whether to skip G3 (go to G1), implement lightweight G3.a, or pursue
heavier G3.b. The protocol:

- pass_rate ≥ 70% → skip G3, go to G1 directly
- pass_rate 40-69% → G3.a (semantic entity expansion) + G1 in parallel
- pass_rate < 40% → discuss G3.b (LLM rewriting) or accelerate G1

Each scenario is a tiny workspace with ONE seed memory + N queries.
Pass = the seed memory appears in top-3 of search results.

Two modes are tested side-by-side to isolate effect:
  - **full**: durin's full pipeline (vector + AliasIndex + RRF rerank)
  - **baseline**: direct vector search only (no entity_ranker)

Difference between modes tells us whether the entity_ranker is the
load-bearing component or whether the multilingual embedding alone
handles the scenario.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.WARNING)

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

from durin.agent.tools.memory_search import MemorySearchTool  # noqa: E402
from durin.agent.tools.context import ToolContext  # noqa: E402
from durin.config.loader import load_config  # noqa: E402
from durin.memory.embedding import FastembedProvider  # noqa: E402
from durin.memory.store import store_memory  # noqa: E402
from durin.memory.vector_index import VectorIndex, vector_index_available  # noqa: E402


@dataclass(frozen=True)
class Scenario:
    """One verification scenario.

    ``seed`` is the markdown body the harness stores; ``entities`` are
    the entity refs the seed is tagged with (mirrors how daily-driver
    writes look). ``queries`` is the list of phrasings we test —
    each must surface the seed in top-3 to count as pass.
    """
    name: str
    description: str
    seed: str
    entities: tuple[str, ...]
    queries: tuple[str, ...]


SCENARIOS: list[Scenario] = [
    Scenario(
        name="E1_cross_lingual_same_entity",
        description="Same entity, ES vs EN queries, single memory",
        seed="Caroline moved to Boston for university in 2017 and lives there since.",
        entities=("person:caroline",),
        queries=(
            "Where does Caroline live?",
            "¿Dónde vive Caroline?",
            "Caroline lives where",
            "ubicación de Caroline",
        ),
    ),
    Scenario(
        name="E2_morphological_variation",
        description="Plural↔singular, conjugations, ES morphology",
        seed="Tuvimos una reunión importante con Marcelo el martes en el café",
        entities=("person:marcelo",),
        queries=(
            "reuniones con Marcelo",   # plural
            "reunión con Marcelo",     # singular exacto
            "Marcelo meeting",         # cross-lingual
            "junta con Marcelo",       # sinónimo ES
        ),
    ),
    Scenario(
        name="E3_code_switching",
        description="Spanish + English mix common in LATAM tech",
        seed="Marcelo's email is marcelo@mxhero.com for work contact",
        entities=("person:marcelo",),
        queries=(
            "¿Marcelo's email?",
            "email de Marcelo",
            "correo Marcelo",
            "mail address de Marcelo",
        ),
    ),
    Scenario(
        name="E4_implicit_entity",
        description="Pronoun / no explicit entity name in query",
        seed="Marcelo trabaja en durin y dukai como founder técnico",
        entities=("person:marcelo", "project:durin", "project:dukai"),
        queries=(
            "qué proyectos tiene Marcelo",  # entity present
            "what does Marcelo work on",    # entity present
            "founder de durin",             # entity:durin
            "Marcelo's startups",           # generic ref
        ),
    ),
    Scenario(
        name="E5_synonym_paraphrase",
        description="Predicate paraphrase, same entity",
        seed="Marcelo está casado con Susana desde 2010 y tienen dos hijos",
        entities=("person:marcelo", "person:susana"),
        queries=(
            "esposa de Marcelo",      # ES exact
            "Marcelo's wife",         # EN
            "pareja de Marcelo",      # sinónimo ES
            "con quién está casado Marcelo",   # parafraseo verbal
        ),
    ),
    # G3.b stress tests — designed to fail with vocabulary-rigid
    # retrieval, expected to pass once LLM rewriter expands the query.
    Scenario(
        name="E6_orthogonal_vocabulary",
        description="Query vocabulary != memory body vocabulary",
        seed="Joanna took a sunset pic during a hike near Fort Wayne last summer",
        entities=("person:joanna",),
        queries=(
            "What state did Joanna visit?",   # state ≠ Fort Wayne
            "Joanna's summer travel",         # travel ≠ hike
            "What place did Joanna photograph?",  # photograph ≠ pic
        ),
    ),
    Scenario(
        name="E7_cjk_query_english_memory",
        description="CJK query against English memory",
        seed="Caroline lives in Boston since 2017",
        entities=("person:caroline",),
        queries=(
            "卡罗琳住在哪里?",            # Chinese: where does Caroline live?
            "キャロラインはどこに住んでいますか?",  # Japanese
            "캐롤라인은 어디에 살아요?",      # Korean
        ),
    ),
    Scenario(
        name="E8_code_switching",
        description="Mixed-language queries common in LATAM/Asia tech",
        seed="Marcelo's email is marcelo@mxhero.com for work contact",
        entities=("person:marcelo",),
        queries=(
            "Marcelo的email是什么?",  # Chinese-English mix
            "El email of Marcelo",    # Spanish-English mix
            "邮箱 de Marcelo",        # Chinese-Spanish mix
        ),
    ),
    Scenario(
        name="E9_hanzi_script_mismatch",
        description="Memory in Traditional Hanzi, query in Simplified",
        seed="馬塞洛住在波士頓從2017年開始",  # Traditional
        entities=("person:marcelo",),
        queries=(
            "马塞洛住在哪里?",   # Simplified
            "馬塞洛住在哪裡?",   # Traditional (control)
        ),
    ),
    Scenario(
        name="E10_katakana_foreign_name",
        description="Foreign name in katakana, cross-script query",
        seed="キャロラインはボストンに住んでいる",  # Katakana standard
        entities=("person:caroline",),
        queries=(
            "キャロラインの住所は?",       # Katakana (normal)
            "Caroline lives where?",       # English (cross-script)
        ),
    ),
    Scenario(
        name="E12_korean_formality",
        description="Korean query with formality variant",
        seed="캐롤라인은 보스턴에 살아요",  # Casual
        entities=("person:caroline",),
        queries=(
            "캐롤라인은 어디에 살아?",     # Casual
            "캐롤라인은 어디에 사세요?",   # Honorific
        ),
    ),
]


@dataclass
class QueryResult:
    scenario: str
    query: str
    full_pass: bool       # top-3 in full pipeline (vector + entity_ranker)
    full_position: int | None
    baseline_pass: bool   # top-3 in raw vector (no entity ranker)
    baseline_position: int | None


# Two distractor sets to measure entity_ranker's real impact under
# different conditions:
#
# - "shared": distractors mention the SAME entities as the target.
#   Realistic daily-driver (many entries about same person), but
#   neutralizes entity_ranker because all entries tie on entity match.
#
# - "unique": distractors mention DIFFERENT entities than the target.
#   Isolates entity_ranker effect — boosted entries are the only ones
#   matching the query's entity.

_DISTRACTORS_SHARED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("John works as a fireman in his city and joined the brigade last year",
     ("person:john",)),
    ("Susana started a pottery class on Tuesdays in the community center",
     ("person:susana",)),
    ("Caroline went to a yoga retreat in Costa Rica during the summer",
     ("person:caroline",)),  # SHARED with E1 (caroline)
    ("Marcelo was thinking about adopting a dog from the shelter",
     ("person:marcelo",)),  # SHARED with E2-E5 (marcelo)
    ("The team had a code review meeting on Monday about the refactor",
     ("topic:codereview",)),
    ("Joanna and Nate played video games for hours last weekend",
     ("person:joanna", "person:nate")),
    ("Evan twisted his ankle while running and had to rest for a week",
     ("person:evan",)),
    ("Melanie organized a fundraiser for the local animal shelter",
     ("person:melanie",)),
    ("Calvin bought a new guitar and started practicing every day",
     ("person:calvin",)),
    ("The kitchen renovation took two months and cost more than expected",
     ("topic:renovation",)),
)

_DISTRACTORS_UNIQUE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("John works as a fireman in his city and joined the brigade last year",
     ("person:john",)),
    ("Susana started a pottery class on Tuesdays in the community center",
     ("person:susana",)),
    ("Penelope went to a yoga retreat in Costa Rica during the summer",
     ("person:penelope",)),
    ("Bob was thinking about adopting a dog from the shelter",
     ("person:bob",)),
    ("The team had a code review meeting on Monday about the refactor",
     ("topic:codereview",)),
    ("Joanna and Nate played video games for hours last weekend",
     ("person:joanna", "person:nate")),
    ("Evan twisted his ankle while running and had to rest for a week",
     ("person:evan",)),
    ("Melanie organized a fundraiser for the local animal shelter",
     ("person:melanie",)),
    ("Calvin bought a new guitar and started practicing every day",
     ("person:calvin",)),
    ("The kitchen renovation took two months and cost more than expected",
     ("topic:renovation",)),
)

# Default to shared (more realistic), CLI flag switches to unique.
_DISTRACTORS = _DISTRACTORS_SHARED


def _seed_workspace(
    workspace: Path, scenario: Scenario, provider: FastembedProvider,
) -> str:
    """Drop distractors + seed + build the vector index. Returns the
    target URI so the caller can locate the seed in result lists.

    A test with ONE entry trivially returns it as top-1; that doesn't
    measure ranking quality. We add 10 distractor entries so the
    target must actually win the ranking. This mirrors daily-driver +
    LoCoMo bench where queries compete against many memories.
    """
    import datetime
    # Seed distractors first; all entries share the same date so the
    # entity_ranker's recency-boost doesn't tilt the playing field.
    SHARED_DATE = datetime.date(2024, 1, 1)
    for i, (text, ents) in enumerate(_DISTRACTORS):
        store_memory(
            workspace,
            content=text,
            class_name="episodic",
            headline=f"distractor {i}: {text[:50]}",
            entities=list(ents),
            valid_from=SHARED_DATE,
        )
    # Snapshot file set BEFORE seeding target, so we can identify the
    # target's auto-generated id afterwards (sorted-by-stem doesn't
    # work — store_memory hashes content into an opaque id).
    ep_dir = workspace / "memory" / "episodic"
    pre_target_ids = {p.stem for p in ep_dir.glob("*.md")} if ep_dir.exists() else set()
    # Seed the target.
    store_memory(
        workspace,
        content=scenario.seed,
        class_name="episodic",
        headline=f"seed: {scenario.seed[:60]}",
        entities=list(scenario.entities),
        valid_from=SHARED_DATE,
    )
    post_target_ids = {p.stem for p in ep_dir.glob("*.md")}
    new_ids = post_target_ids - pre_target_ids
    if not new_ids:
        # store_memory deduped — fallback: take any episodic for the
        # scenario's first entity, since the duplicate is conceptually
        # equivalent for retrieval testing.
        raise RuntimeError(
            f"could not identify target for {scenario.name}: "
            f"pre={len(pre_target_ids)}, post={len(post_target_ids)}"
        )
    target_id = next(iter(new_ids))

    vi = VectorIndex(workspace, provider)
    vi.rebuild_from_workspace()
    return f"memory/episodic/{target_id}"


def _position_in_results(target_uri: str, results: list[dict]) -> int | None:
    for i, r in enumerate(results, start=1):
        if r.get("uri") == target_uri:
            return i
    return None


async def _run_full(tool: MemorySearchTool, query: str) -> list[dict]:
    out = await tool.execute(query=query, scope="dreamed", level="warm")
    return out.get("results", []) if isinstance(out, dict) else []


def _run_baseline(vi: VectorIndex, query: str) -> list[dict]:
    """Vector-only, no entity_ranker. Top-10 to match full pipeline."""
    rows = vi.search(query, top_k=10)
    # Shape rows like the full pipeline (uri field needs uri prefix)
    out = []
    for row in rows:
        rid = row.get("id", "")
        class_name = row.get("class_name", "")
        if class_name and class_name != "entity_page":
            uri = f"memory/{class_name}/{rid}"
        else:
            uri = rid
        out.append({"uri": uri, **{k: v for k, v in row.items() if k != "id"}})
    return out


def _make_tool(workspace: Path, model: str) -> MemorySearchTool:
    """Create a MemorySearchTool wired to the real vector index.

    Forces ``memory.enabled = True`` so the vector path activates —
    same fix the bench harness applies (commit ffe1518). Without this
    the tool degrades to grep substring on raw query, which fails on
    rephrased queries even though vector retrieval handles them fine.
    """
    from durin.config.loader import load_config
    app_cfg = load_config()
    app_cfg.memory.enabled = True
    ctx = ToolContext.__new__(ToolContext)
    ctx.workspace = workspace
    ctx.config = app_cfg.tools
    ctx.app_config = app_cfg
    return MemorySearchTool.create(ctx)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path,
                        default=_REPO / "bench-results/g3_verification.json")
    parser.add_argument("--top-n", type=int, default=3,
                        help="Position threshold for 'pass' (default 3)")
    parser.add_argument("--distractors", choices=("shared", "unique"),
                        default="shared",
                        help="Distractor set: 'shared' entities (realistic, "
                             "neutralizes entity_ranker) or 'unique' "
                             "(isolates entity_ranker effect).")
    args = parser.parse_args()

    if not vector_index_available():
        sys.exit("lancedb not available")

    # Swap distractor set based on flag (default shared).
    global _DISTRACTORS
    _DISTRACTORS = (_DISTRACTORS_SHARED if args.distractors == "shared"
                    else _DISTRACTORS_UNIQUE)
    print(f"Distractor set: {args.distractors} ({len(_DISTRACTORS)} entries)")

    cfg = load_config()
    model = cfg.memory.embedding.model
    provider = FastembedProvider(model=model)

    all_results: list[QueryResult] = []

    with tempfile.TemporaryDirectory(prefix="g3verify_") as tmpd:
        tmp_root = Path(tmpd)
        for scenario in SCENARIOS:
            workspace = tmp_root / scenario.name
            workspace.mkdir(parents=True, exist_ok=True)
            print(f"\n=== {scenario.name} — {scenario.description} ===")
            target_uri = _seed_workspace(workspace, scenario, provider)
            tool = _make_tool(workspace, model)
            vi = VectorIndex(workspace, provider)

            for query in scenario.queries:
                full_rows = asyncio.run(_run_full(tool, query))
                base_rows = _run_baseline(vi, query)
                full_pos = _position_in_results(target_uri, full_rows)
                base_pos = _position_in_results(target_uri, base_rows)
                full_pass = full_pos is not None and full_pos <= args.top_n
                base_pass = base_pos is not None and base_pos <= args.top_n
                all_results.append(QueryResult(
                    scenario=scenario.name,
                    query=query,
                    full_pass=full_pass,
                    full_position=full_pos,
                    baseline_pass=base_pass,
                    baseline_position=base_pos,
                ))
                mark = "✓" if full_pass else "✗"
                base_mark = "✓" if base_pass else "✗"
                fp = full_pos if full_pos else "—"
                bp = base_pos if base_pos else "—"
                print(f"  {mark} full=#{fp:<3} | {base_mark} baseline=#{bp:<3}  | {query}")

    # Aggregate
    print("\n" + "=" * 70)
    print("AGGREGATE — full pipeline vs baseline (vector-only)")
    print("=" * 70)
    by_scenario_full: dict[str, list[bool]] = defaultdict(list)
    by_scenario_base: dict[str, list[bool]] = defaultdict(list)
    for r in all_results:
        by_scenario_full[r.scenario].append(r.full_pass)
        by_scenario_base[r.scenario].append(r.baseline_pass)

    total_full = sum(1 for r in all_results if r.full_pass)
    total_base = sum(1 for r in all_results if r.baseline_pass)
    n = len(all_results)
    print(f"{'scenario':35s}  {'full':>10s}  {'baseline':>10s}")
    for s in SCENARIOS:
        fp = by_scenario_full[s.name]
        bp = by_scenario_base[s.name]
        print(f"{s.name:35s}  {sum(fp)}/{len(fp):<7d}  {sum(bp)}/{len(bp)}")
    print("-" * 70)
    print(f"{'TOTAL':35s}  {total_full}/{n} ({total_full*100/n:.1f}%)"
          f"   {total_base}/{n} ({total_base*100/n:.1f}%)")

    # Decision protocol per glm R5
    rate = total_full * 100 / n
    print()
    if rate >= 70:
        print(f"DECISION: full pipeline @ {rate:.1f}% ≥ 70% → SKIP G3, go to G1 directly.")
    elif rate >= 40:
        print(f"DECISION: full pipeline @ {rate:.1f}% in 40-69% → G3.a + G1 in parallel.")
    else:
        print(f"DECISION: full pipeline @ {rate:.1f}% < 40% → consider G3.b or accelerate G1.")

    # Write JSON
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps([
        {
            "scenario": r.scenario,
            "query": r.query,
            "full_pass": r.full_pass,
            "full_position": r.full_position,
            "baseline_pass": r.baseline_pass,
            "baseline_position": r.baseline_position,
        }
        for r in all_results
    ], indent=2, ensure_ascii=False))
    print(f"\nWrote {len(all_results)} records to {args.out}")


if __name__ == "__main__":
    main()
