# 27 — LoCoMo benchmark harness

> First objective benchmark of durin's entity-centric memory against
> published numbers. LoCoMo (Maharana et al. 2024,
> [snap-research/locomo](https://github.com/snap-research/locomo)) is
> the standard for long-conversation QA over multi-session
> conversations. Published SOTA on full N=1540: HyperMem 92.73%, Mem0
> 92.5%, A-MEM ~47% (research/16c §6).
>
> This doc captures: what the harness does, how to run it, how to
> read the results, the gaps / caveats to be honest about, and what
> we still don't measure.

---

## §1 — Design principles

- **Workspace isolation per QA**. Every QA gets a fresh
  `bench-workspaces/locomo/<run>/<qa_id>/` so the benchmark never
  pollutes the user's real `~/.durin/workspace/`. Re-runs start clean.
- **Telemetry captured per QA**. Each QA binds its own
  `TelemetryLogger` writing to `telemetry/<qa_id>.jsonl`. Every
  `memory.recall`, `memory.store`, `cache.usage`, `compaction.*`,
  `tool.*` event durin emits while answering lands in that file —
  attached to the trace, reachable for analysis. **The user's
  starting requirement.**
- **Full trace per QA**. `traces/<qa_id>.json` carries the question,
  expected answer, agent answer, judge verdict + reasoning, tool
  calls with args + result previews, iterations, stop reason,
  duration, workspace path, telemetry path. Enough to debug WHY a
  QA failed without re-running.
- **Rule-based failure categorisation**. After the run, an analyzer
  pass classifies each fail into `retrieval_miss_empty`,
  `retrieval_miss_irrelevant`, `no_retrieval`, `synthesis_error`,
  `hallucination`, `judge_error_possible`, `timeout`, `error`,
  `unknown`. Per-failure markdown under `failures/<qa_id>.md` with
  telemetry summary + tool calls.
- **Replayable per QA**. `locomo_replay.py <trace_path>` re-runs ONE
  QA against current code, replaces the trace + telemetry, saves
  `<qa>.previous.json` so you can diff before/after.
- **Reproducible across commits**. Output dir embeds date + commit
  SHA short hash. `manifest.json` carries the full args + commit +
  durin version. Stratified subset is seeded (default 42), so
  re-running with the same seed picks the same QAs.

---

## §2 — Pipeline shape

```
locomo10.json (dataset)
        │
        ▼
 locomo_dataset.load_dataset → list[QA]
        │
        ▼
 stratified_subset(per_category=5, seed=42) → 25 QAs across 5 categories
        │
        ▼
 ┌──── per QA loop ───────────────────────────────┐
 │ locomo_harness.run_qa:                         │
 │   1. Fresh workspace                           │
 │   2. store_memory bulk-seed conversation       │
 │      (turns → episodic entries, tagged with    │
 │      person:<slug>, source_refs=session/<id>)  │
 │   3. Bind TelemetryLogger → telemetry/<qa>.jsonl │
 │   4. Build AgentLoop + bus + dispatch QA       │
 │   5. Drain outbound until final answer         │
 │   6. Extract tool_calls + iterations + ctx     │
 │      from session messages                     │
 │                                                │
 │ locomo_judge.judge_answer:                     │
 │   LLM call → ===SCORE===/===CONFIDENCE===/     │
 │   ===REASONING=== envelope                     │
 │                                                │
 │ Persist traces/<qa>.json with trace + verdict  │
 └────────────────────────────────────────────────┘
        │
        ▼
 locomo_analyze.analyze_run:
   rule-based categorisation → failures/<qa>.md
   aggregate by category + failure type → summary.json
```

---

## §3 — Output directory

```
bench-results/locomo/<YYYY-MM-DD_HHMMSS>_<commit8>/
├── manifest.json              # args + commit + durin version + ts
├── summary.json               # aggregate score, by_category, failure_breakdown
├── traces/
│   └── <qa_id>.json           # full per-QA trace + verdict
├── telemetry/
│   └── <qa_id>.jsonl          # every event durin emitted during this QA
├── failures/
│   └── <qa_id>.md             # rendered failure analysis (one per fail)
└── workspaces/                # GC'd unless --keep-workspaces
```

Per-failure markdown carries:
- Question / expected / got
- Judge score + confidence + reasoning
- Run shape (iterations / stop / duration / context size)
- **Telemetry summary** (event-type counts, cache ratio)
- Tool calls table with args + result previews
- `memory.recall.vector` detail (queries, hits, reranking)
- Links back to raw trace + telemetry

---

## §4 — How to run

**One-time setup**:

```
mkdir -p ~/.cache/durin && \
  curl -L -o ~/.cache/durin/locomo10.json \
    https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json
```

**Stratified subset (default: 5 per category = 25 QAs total)**:

```
python -m scripts.benchmark.locomo_run \
  --data-path ~/.cache/durin/locomo10.json
```

**Smaller subset for harness validation (1 per category = 5 QAs)**:

```
python -m scripts.benchmark.locomo_run \
  --data-path ~/.cache/durin/locomo10.json \
  --per-category 1
```

**Single QA (debugging)**:

```
python -m scripts.benchmark.locomo_run \
  --data-path ~/.cache/durin/locomo10.json \
  --qa-id conv-5-q3
```

**Resume a crashed run**:

```
python -m scripts.benchmark.locomo_run \
  --data-path ~/.cache/durin/locomo10.json \
  --resume-into bench-results/locomo/<run> \
  --resume
```

**Replay a single failed QA after a fix**:

```
python -m scripts.benchmark.locomo_replay \
  --data-path ~/.cache/durin/locomo10.json \
  bench-results/locomo/<run>/traces/conv-5-q3.json
```

**Re-analyze an old run with updated rules** (zero LLM cost):

```
python -c "from scripts.benchmark.locomo_analyze import analyze_run; \
  from pathlib import Path; \
  print(analyze_run(Path('bench-results/locomo/<run>')))"
```

---

## §5 — How to read the results

### `summary.json`

```json
{
  "n_total": 25,
  "n_pass": 11,
  "n_fail": 14,
  "score": 0.440,
  "judge_failed": 0,
  "by_category": {
    "single_hop": {"n": 5, "pass": 4, "score": 0.800},
    "multi_hop":  {"n": 5, "pass": 1, "score": 0.200},
    "temporal":   {"n": 5, "pass": 2, "score": 0.400},
    "open_domain":{"n": 5, "pass": 3, "score": 0.600},
    "adversarial":{"n": 5, "pass": 1, "score": 0.200}
  },
  "failure_breakdown": {
    "retrieval_miss_irrelevant": 7,
    "synthesis_error":           3,
    "no_retrieval":              2,
    "hallucination":             1,
    "judge_error_possible":      1
  }
}
```

**Read it backwards**: the `failure_breakdown` tells you where to
invest. If `retrieval_miss_irrelevant` dominates → tune embeddings
or rerank weight. If `synthesis_error` dominates → improve the
consolidator prompt or context shape. If `hallucination` dominates
→ tighten anti-hallucination instructions in the system prompt.
If `judge_error_possible` is high → revisit the judge prompt or
adopt a more rigorous scoring rubric.

### Per-failure markdown

Open `failures/<qa_id>.md` for the full trace. Telemetry summary
shows what tools fired and how many cache hits — useful for
distinguishing "the agent didn't try" from "the agent tried but
retrieval missed".

---

## §6 — Caveats (DO read before quoting numbers)

These caveats matter — without them the score is misleading:

1. **Subset, not full**. Published numbers are over N=1540. A 25-QA
   stratified subset has ~17pp 95% CI on a per-category basis. Use
   relative deltas across commits, not absolute scores.
2. **glm-5.1 ≠ Mem0/HyperMem stack**. Winners use GPT-4 / Claude
   Sonnet. Model quality is a confounder.
3. **Judge bias**. glm-5.1 judges glm-5.1 answers (same-provider
   self-consistency risk). Future iteration: cross-model judge
   (e.g. claude-haiku-4.5 as judge).
4. **Bulk seeding ≠ live conversation**. We pre-seed the entire
   transcript via `store_memory` rather than running the agent
   through every turn. Standard for memory benchmarks; matches what
   Mem0 / HyperMem do, but means we're testing the read-side only.
5. **No ablation in v1**. We don't compare `memory.enabled=true`
   vs `false` (full-context baseline). The Mem0 production report
   suggests the gap may be marginal — worth measuring.
6. **Binary scoring**. v1 score is {0, 1}, no partial credit.
   LoCoMo paper uses F1 over reference tokens for some categories;
   we deferred for simplicity.

---

## §7 — Status — what we have, what we don't

**Have**:
- ✓ Dataset loader + stratified subset
- ✓ Per-QA workspace isolation
- ✓ Bulk memory seeding from conversation transcript
- ✓ Per-QA telemetry captured to dedicated JSONL
- ✓ Full trace JSON per QA
- ✓ LLM-as-judge with reasoning
- ✓ Rule-based failure categorisation
- ✓ Per-failure markdown with telemetry summary
- ✓ Resume after crash (`--resume`)
- ✓ Single-QA replay (`locomo_replay.py`)
- ✓ Reproducible across runs (seeded subset, commit-tagged dir)

**Don't have yet** (M1-M7 below, by likely priority):

- **M1**: ablation switch (memory on/off) so we can quantify lift.
- **M2**: LLM-driven failure classification for `unknown` category
  (rule-based first pass, LLM second pass).
- **M3**: cross-model judge (different model than agent) to reduce
  self-consistency bias.
- **M4**: F1 / partial-credit scoring per LoCoMo paper §4.
- **M5**: variance estimate across seeds (run with seed=42, 43, 44
  → see how stable the score is on the same code).
- **M6**: comparison view: `bench compare <run_a> <run_b>` → diff
  scores by category, list QAs that flipped pass↔fail.
- **M7**: live tool-call event capture during the run (today we
  reconstruct from `session.messages` after the run — works but
  loses sub-call timing). Tie into `memory.recall.vector` telemetry
  for the `ranking` / `reordered` fields.

---

## §8 — Why no Hugging Face / pip dependency for dataset

LoCoMo is a public JSON file (~few MB). Adding a runtime download
to the benchmark harness means the harness needs network to run,
and is unrepro if the upstream changes. The `--data-path` arg keeps
the dataset under user control: download once, cache locally, pin
to a specific snapshot if reproducibility across long timespans
matters.

---

## §9 — Comparison to published numbers (when first real run lands)

| System | LoCoMo full N=1540 | LongMemEval | EverMemBench |
|---|---|---|---|
| HyperMem (SOTA) | 92.73% | n/a | n/a |
| Mem0 | 92.5% (~6900 tok/query) | 94.4% | n/a |
| GAAMA | 78.9% | n/a | n/a |
| A-MEM | ~47% | n/a | n/a |
| HippoRAG | 69.9% | n/a | n/a |
| **durin (baseline, N=25 subset, glm-5.1)** | TBD | n/a | n/a |

Will be filled by the first real run. Initial expectation
(unhedged): for single-hop ~60-80% should be reachable on day one
given the architecture we have; multi-hop / temporal / adversarial
are where the gap with the SOTA will be most visible.

---

## Last updated: 2026-05-24 (harness shipped, first real run pending)
