# Project conventions for Claude Code

## Working / process docs go in `.workdocs/`, never in tracked `docs/`

`docs/` holds **published, maintained** documentation only: a `README.md` overview,
`guide/` (user docs: install, configuration, channels, providers), `internals/`
(how-it-works architecture, per subsystem), plus `roadmap.md`, `releasing.md`, and
`contributing.md`. Do not commit working or process artifacts there.

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

## Design rationale belongs in `docs/architecture/`, not in code comments to archive

If live code needs to cite *why* it does something, that rationale must live in a
maintained `docs/architecture/` doc, and the code comment must point there — never
at a `.workdocs/archive/` file. A code reference reaching into the archive is a
signal that `docs/architecture/` has a gap; fill the gap, then cite the arch doc.
