# Project conventions for Claude Code

## Working / process docs go in `.workdocs/`, never in tracked `docs/`

`docs/` holds **published, maintained, public** documentation only: a `README.md`
overview, `guide/` (user docs: install, configuration, channels, providers), and
`internals/` (how-it-works architecture, per subsystem). Do not commit working or
process artifacts there. Maintainer-only docs that are **not for public
consumption** — the roadmap, the release manual, and the contributor guide — live
under `.workdocs/maintainers/` (gitignored), never in `docs/`.

Write working/process artifacts under the gitignored `.workdocs/` directory:

| Artifact | Where to write it |
|---|---|
| Implementation plans (writing-plans, subagent-driven-development) | `.workdocs/superpowers/plans/YYYY-MM-DD-<feature>.md` |
| Design specs (brainstorming) | `.workdocs/superpowers/specs/YYYY-MM-DD-<topic>-design.md` |
| Research notes | `.workdocs/research/` |
| QA / audit / code-review reports | `.workdocs/qa/` |
| Archived / superseded design docs | `.workdocs/archive/` |

This **overrides** the superpowers skills' default `docs/superpowers/...` paths:
when a skill says to save a plan to `docs/superpowers/plans/...`, save it to
`.workdocs/superpowers/plans/...` instead. `.workdocs/` is gitignored, so these
never get committed; their history (for anything that was previously tracked)
remains in `git log`.

## Code comments are self-contained — never reference docs

Code comments and docstrings must explain *what the code does and why* on their
own. They must **not** reference any documentation file — no `see docs/...`, no
`doc 11 §8e`, no section-number cross-refs, and never a `.workdocs/archive/` path.
Dependencies point one way only: **docs reference code, never code → docs**. A
doc gets renamed or restructured and a back-reference from code rots silently
into a dead pointer. If the rationale matters, state it inline in the comment.

## Documentation is part of the change

A change to a subsystem's behavior, public surface, config keys, LLM tools, or
module layout updates that subsystem's `docs/internals/` doc in the **same PR** —
a behavior change without its doc update is an incomplete change. Avoid
drift-prone facts in docs (hard counts, source line-number anchors, "last
updated" stamps); prefer "generated from X" / "see the route table" so the doc
does not silently fall out of sync.
