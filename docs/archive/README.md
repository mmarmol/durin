# docs/archive — historical reference

Docs in this directory are **frozen historical material**. They are NOT
sources of active direction; they exist so we can answer questions like
"why didn't we adopt strategy X?" or "what was the SWE-bench result
before we discontinued it?" without re-deriving from scratch.

For current direction, read these instead:

- `../roadmap.md` — what's shipped / what's next
- `../bitacora.md` — what we tried, what we discarded, and why (rolling log)
- `../03_memory_design.md` — design doc for Phase 2 memory (not yet implemented)
- `../archive/34_external_agents_review.md` — comparison vs OpenHands / OpenClaude / OpenCode / Hermes / pi
- `README.md` — current operational architecture

## Why each file was archived

| File | Status when archived | Why archived |
|---|---|---|
| `04_agent_strategies_catalog.md` | catalog of "candidate strategies" from May 2026 external-agent review | Planning role complete — the tools roadmap (item-by-item shipped in 01) absorbed the prioritised items; rejected strategies live in bitacora.md with their rationale |
| `05_log_swebench.md` | SWE-bench evaluation log from V5/V5b/V6 runs (May 2026) | SWE-bench discontinued 2026-05-18; benchmark infra removed in commit 99f0937. Log kept as ground-truth that "agent harness changes don't beat the base model on SWE-bench Lite" — a non-trivial result we may need to re-cite |
| `06_log_experiments.md` | Investigation-cycle hypothesis test log (V3–V9) | Conclusions absorbed into bitacora.md. Raw experiment narrative + telemetry tables kept here for the rare case we want to revisit whether a discarded mechanism (deliberation, posture vector, phase temperatures) deserves a second look under different conditions |
| `09_code_audit.md` | Tier 1+2 harness audit (May 2026) — 0 critical bugs, "ship-grade" | Point-in-time audit; findings resolved in follow-up commits |
| `09_daily_driver_plan.md` | CLI/TUI ergonomics roadmap D1–D9 | Phase complete — all shipped in v0.1.0a7; kept as the record of what that release contained |
| `10_smoke_test_report.md` | End-to-end smoke verification of Tier 1+2 with real LLM calls | Test passed; findings folded into the integration test suite |
| `10_textual_migration.md` | prompt_toolkit → Textual TUI migration plan (D5.1–D5.12) | Migration complete; current TUI design lives in ARCHITECTURE.md §11 |
| `11_secrets_design.md` | Secrets subsystem design (store, redaction, agent flow) | All phases shipped — store + `${secret:}` refs + redaction + `request_secret`; living security reference is ARCHITECTURE.md |
| `12_web_config_parity.md` | Plan to configure all of durin from the web dashboard | Shipped — secrets section, generic `/api/config`, schema-driven settings, refined IA |
| `13_interactive_tool_renders.md` | Custom renders for `ask_user_question` / `request_secret` | Shipped in both TUI and web — option panels + secure masked input |
| `08_memory_phase2_proposal.md` | Phase 2 memory design synthesis (May 2026) | Phase 2 superseded by entity-centric direction (doc 18). Concrete deliverables shipped via doc 19 Phase 0-5 + T1 wiring. Kept as the record of the three synthesis paths and why we picked the entity-centric one |
| `14_typed_entities_proposal.md` | Typed entities `type:value` proposal (May 2026) | §3.1+§3.2 (format + validation) implemented as Propuesta A; §3.3 (closed 9-type list) superseded by doc 18 §4 open vocabulary. Doc already carried its own supersession note |
| `21_integration_and_critical_review.md` | Plan to wire entity-centric pieces into the agent loop | Superseded by doc 23 (T1 cluster plan) + doc 24 (W1-W4 wiring + E2E). Execution complete |
| `22_critiques_validated_against_real_systems.md` | Validation of doc 21 critiques against 8 reference memory systems | Cumplió rol — fed doc 23's T1.x consolidated list. Kept as the record of which critiques survived contact with real implementations (Hermes, OpenClaw, OpenClaude, Cognee, Graphiti, Mem0, MemPalace, HippoRAG, A-Mem) |
| `23_t1_implementation_plan.md` | T1.1–T1.7 implementation by risk clusters (A mechanical, B algorithmic, C write/parse, D CLI) + glm peer review with G1-G13 fixes | Executed — clusters A through D shipped + verified live with glm-5.1. Final commit `31c9634` (phase T1 cluster D) closes it |
| `24_t1_wiring_e2e_tests.md` | T1 wiring gaps W1-W4 + E2E test plan + glm peer review (B1-M2) | Executed — W1+W2+W3+W4 shipped, 4 hermetic E2E tests added (`tests/memory/test_t1_wiring_e2e.py`), live-caught `_workspace_root` property bug fixed |
| `19_implementation_plan.md` | Phase 0–6 step-by-step implementation plan for the entity-centric memory model (foundations, dream, retrieval, drill-down, absorption, outcomes) | Executed — Phase 0-6 shipped + verified post-T1 wiring. §8 operational outcomes survive as `tests/integration/test_phase6_outcomes.py` (6 tests passing). §14 out-of-scope list mirrored in `archive/36_post_t1_state_and_t2_horizon.md` §1. Kept here as the historical record of the construction-phase decisions |

### 2026-05-30 batch (docs reorganization — moved out of `docs/` root + `docs/architecture/memory/`)

| File | Status when archived | Why archived |
|---|---|---|
| `30_arch_memory_legacy.md` (was `docs/arch/memory.md`) | Mini-overview of the memory subsystem that lived next to other `arch/` subsystem docs | Duplicated `docs/architecture/memory/` which is the canonical, module-by-module deep dive. The `arch/` folder was renamed `architecture/` in the same change; this single doc moved here because the duplication was the actual confusion source the reorg fixed |
| `31_memory_implementation_roadmap.md` (was `docs/architecture/memory/09_implementation_roadmap.md`) | Phase 0-6 implementation plan + telemetry rollout sequence for the memory subsystem | All phases shipped. Kept here so the audit trail of "in what order did we ship the subsystem" survives |
| `32_memory_audit_reconciliation.md` (was `docs/architecture/memory/11_audit_reconciliation.md`) | Per-audit-finding reconciliation log (A1-A11, E1-E32, F1-F23, G1-G6, H1-H7) | All audits closed. The closure narratives are the historical record of what each commit fixed — kept verbatim |
| `33_memory_phase_progress_review.md` (was `docs/architecture/memory/99_phase_progress_review.md`) | Phase-by-phase progress + completion-criteria review | Historical by definition |
| `34_external_agents_review.md` (was `docs/07_external_agents_review.md`) | Comparative review of external agent harnesses (OpenHands / OpenClaude / OpenCode / Hermes / pi) | Snapshot from May 19, 2026. Specific findings absorbed into design decisions — bitacora carries the live "what we learned from each" |
| `35_entity_centric_plan.md` (was `docs/18_entity_centric_plan.md`) | The consolidated entity-centric design (principles, schema, retrieval) that drove Phase 0-6 + T1 wiring | Plan executed; the system implements it. Living references in `docs/architecture/memory/01_data_and_entities.md` + `03_search_pipeline.md`. Kept as the original-direction document |
| `36_post_t1_state_and_t2_horizon.md` (was `docs/25_post_t1_state_and_t2_horizon.md`) | T1 close-out state + T2 horizon (what shipped, what's still aspirational) | Snapshot at T1 completion. T2 items still live: those that moved forward are in current docs; those deferred are in `20_pendings.md` |
| `37_memory_graph_view.md` (was `docs/26_memory_graph_view.md`) | Design doc for the webui memory graph view (Obsidian-style) | Shipped — `webui/src/components/MemoryGraphView.tsx` + `useMemoryGraph.ts` exist and consume `/api/memory/graph`. Kept here so design decisions (force-graph vs alternatives, node labelling) survive the doc move |
| `38_locomo_benchmark.md` (was `docs/27_locomo_benchmark.md`) | LoCoMo benchmark harness description | Bench infrastructure shipped at `scripts/benchmark/`. Doc is outdated post H22-H30 (proportional sampling, fail audit, retry pass, P11 self-recovery); the harness itself is the canonical reference. Kept here as the original "how to run the bench" intro |
| `39_locomo_results_and_sota_gap.md` (was `docs/28_locomo_results_and_sota_gap.md`) | First real LoCoMo run + analysis of the gap vs published SOTA (May 27, ~57% baseline) | Outdated by subsequent benches (current 75% post-H30; +18pp cumulative). Kept as the snapshot of where we started + what we thought the gap was at the time |
| `40_exploracion_datos_y_relaciones.md` (was `docs/29_exploracion_datos_y_relaciones.md`) | Exploration of "what do we store and how is it related" — narrative walk-through of the entity-centric data model | Exploratory writeup that informed the entity-centric design + retrieval choices. Living spec is `docs/architecture/memory/01_data_and_entities.md`; this exists as the original thinking |

## Policy

- Files in `archive/` should not be edited. If new information changes the
  conclusions, write a new entry in `bitacora.md` referencing the archived
  doc, rather than rewriting history.
- If a doc starts attracting active updates, it has stopped being archive
  material — move it back to `../` with a fresh purpose statement.
