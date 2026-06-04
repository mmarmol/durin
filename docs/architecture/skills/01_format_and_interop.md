# durin SKILL.md — Format & Interop Contract

> For the whole skills subsystem (lifecycle, dreams, security, retrieval, runtime),
> see [`00_overview.md`](00_overview.md). This file is the SKILL.md **format** contract.
>
> Canonical reference for durin's skill document format. durin's `SKILL.md` **is the
> [agentskills.io](https://agentskills.io/specification) open standard** (the same format
> Hermes, OpenClaw, Pi, Claude Code, Codex, Cursor, and 30+ tools use) **plus durin's own
> behavior under the `metadata.durin.*` vendor namespace**. Decision + rationale:
> [`docs/superpowers/specs/2026-06-03-skill-interop-standard-design.md`](../../superpowers/specs/2026-06-03-skill-interop-standard-design.md).

## Why this matters

A skill is portable knowledge. Because durin speaks the same standard the ecosystem
converged on, importing a skill from any compliant tool or marketplace (~490k skills as
of 2026) is a near-no-op, and durin's own skills are usable elsewhere. The keystone is
**round-trip fidelity**: durin never drops a field it doesn't understand.

## On-disk layout

```
workspace/skills/<name>/
├── SKILL.md            # required — frontmatter + markdown body
├── references/         # optional — supporting docs, loaded on demand
├── scripts/            # optional — helper scripts
├── assets/             # optional — templates, data, images
└── templates/          # optional
```

`<name>` is the directory name and the skill's identifier. Subdirectories are preserved
(and copied on import) and reachable via `read_file`. Builtin skills live in durin's
package; a workspace copy of the same name takes precedence (forked on first write).

## Root frontmatter — the agentskills.io standard

| Field | Required | Meaning / durin behavior |
|---|---|---|
| `name` | **yes** | 1-64 chars, lowercase letters/digits/hyphens, matches the directory name. |
| `description` | **yes** | ≤1024 chars: what the skill does and when to use it. **Load-bearing for retrieval** — it is the searchable handle and the line shown in the working-set/catalog. |
| `version` | no | Semantic version. Preserved; surfaced in `list_skills_info`. |
| `license` | no | License id or reference. Preserved; surfaced in `list_skills_info`. |
| `compatibility` | no | Free-form environment requirements (≤500 chars). Preserved; advisory (not enforced). |
| `allowed-tools` | no | Space-separated pre-approved tools. Preserved; advisory (not enforced). |
| `platforms` | no | OS restriction: `[macos, linux, windows]`. **Honored** — a skill is hidden entirely on a non-matching OS. OpenClaw aliases accepted (`darwin`→macos, `win32`→windows). No field = all platforms. |
| `disable-model-invocation` / `disable_model_invocation` / `disableModelInvocation` | no | Truthy hides the skill from the model's catalog/working-set (still loadable programmatically). All three spellings honored. |

Any other root key (e.g. `author`, `tags`, `x-anything`) is **preserved untouched** across
durin edits, never required, never rejected.

## `metadata.durin.*` — durin's behavior namespace

durin keeps its own fields under `metadata.durin` (exactly the `metadata.<vendor>` pattern
Hermes uses with `metadata.hermes` and OpenClaw with `metadata.openclaw`):

| Field | Meaning |
|---|---|
| `metadata.durin.mode` | `manual` (user owns it; edits need approval) or `auto` (dream may author/patch it). |
| `metadata.durin.provenance` | `{ source, created_at, ... }` — e.g. `source: "dream"` (crystallized from use) or `source: "marketplace:<id>"` (imported). |
| `metadata.durin.requires` | `{ bins: [...], env: [...] }` — availability gate (CLI tools on PATH, env vars set). A skill with unmet requirements is shown as `(unavailable: …)`. |
| `metadata.durin.always` | Truthy → the skill's full body is always injected (the always-on tier), not just its name+description. |
| `metadata.durin.curated` | Dream-curation bookkeeping. |

Other vendors' `metadata.<vendor>.*` blocks (e.g. `metadata.hermes.requires_toolsets`) are
**preserved untouched** and ignored functionally — durin does not act on another vendor's
behavior fields.

## Round-trip fidelity guarantee

Every durin mutation — `dream_create_skill`, `apply_skill_edit`, `save_skill_content`,
`set_mode`, `mark_curated`, `dream_fuse_skills` — preserves all foreign frontmatter:
unknown root keys and every `metadata.<vendor>` block survive byte-equivalently. This holds
because writes route through `_update_md` (`split_frontmatter` → mutate the parsed dict →
`join_frontmatter(sort_keys=False)`) or overwrite user-supplied content verbatim. Enforced
by `tests/agent/test_skill_interop_roundtrip.py`.

**Consequence:** an imported Hermes/Claude-Code/marketplace skill can be edited, curated,
and re-exported by durin and still round-trips back to its origin without data loss.

## Import posture (forward reference)

Because durin shares the standard, importing is "copy the directory + stamp our namespace":
1. Fetch the skill (URL / GitHub / marketplace) — copy `SKILL.md` + any `references/`,
   `scripts/`, `assets/`, `templates/`.
2. Stamp `metadata.durin.provenance.source` + `metadata.durin.mode`.
3. Optionally map a foreign requirement declaration
   (`metadata.hermes.requires_*` / `required_environment_variables` / `metadata.openclaw.requires`)
   → `metadata.durin.requires` if durin should gate on it.

Everything else already works because the format is shared. The full import command is the
§6.B plan (separate).

## What durin deliberately does NOT do (yet)

- Enforce `allowed-tools` / `compatibility` (advisory, preserved).
- Honor another vendor's conditional-activation (`metadata.hermes.requires_toolsets`, etc.) —
  durin gates on its own `metadata.durin.requires` + the standard `platforms`.
- Validate/lint authored skills against the agentskills.io name/description constraints
  (a future export-quality check).
