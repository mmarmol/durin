# docs/archive — historical reference

Docs in this directory are **frozen historical material**. They are NOT
sources of active direction; they exist so we can answer questions like
"why didn't we adopt strategy X?" or "what was the SWE-bench result
before we discontinued it?" without re-deriving from scratch.

For current direction, read these instead:

- `../01_roadmap.md` — what's shipped / what's next
- `../02_bitacora.md` — what we tried, what we discarded, and why (rolling log)
- `../03_memory_design.md` — design doc for Phase 2 memory (not yet implemented)
- `../07_external_agents_review.md` — comparison vs OpenHands / OpenClaude / OpenCode / Hermes / pi
- `../ARCHITECTURE.md` — current operational architecture

## Why each file was archived

| File | Status when archived | Why archived |
|---|---|---|
| `04_agent_strategies_catalog.md` | catalog of "candidate strategies" from May 2026 external-agent review | Planning role complete — the tools roadmap (item-by-item shipped in 01) absorbed the prioritised items; rejected strategies live in 02_bitacora.md with their rationale |
| `05_log_swebench.md` | SWE-bench evaluation log from V5/V5b/V6 runs (May 2026) | SWE-bench discontinued 2026-05-18; benchmark infra removed in commit 99f0937. Log kept as ground-truth that "agent harness changes don't beat the base model on SWE-bench Lite" — a non-trivial result we may need to re-cite |
| `06_log_experiments.md` | Investigation-cycle hypothesis test log (V3–V9) | Conclusions absorbed into 02_bitacora.md. Raw experiment narrative + telemetry tables kept here for the rare case we want to revisit whether a discarded mechanism (deliberation, posture vector, phase temperatures) deserves a second look under different conditions |

## Policy

- Files in `archive/` should not be edited. If new information changes the
  conclusions, write a new entry in `02_bitacora.md` referencing the archived
  doc, rather than rewriting history.
- If a doc starts attracting active updates, it has stopped being archive
  material — move it back to `../` with a fresh purpose statement.
