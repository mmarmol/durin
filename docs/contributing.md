# Contributing to durin

This guide covers the development workflow: setting up a local checkout,
isolating test state, running the test suite, keeping the API contract in sync,
building the webui, and getting changes merged.

For install options (PyPI, extras, first-time configuration), see
[docs/guide/install.md](guide/install.md).
For the release process, see [docs/releasing.md](releasing.md).

---

## Development setup

### Clone and editable install

```bash
git clone git@github.com:mmarmol/durin.git
cd durin
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,mcp,web,oauth]"
```

The `[dev]` extra pulls in `pytest`, `ruff`, and related tooling.
Add other extras as needed (e.g. `[memory]` for vector-retrieval work;
see [docs/guide/install.md](guide/install.md) for the full extras table).

The editable install does **not** build the webui SPA.
If you need the dashboard locally, build it separately — see
[Building the webui](#building-the-webui) below.

---

## DURIN_HOME isolation

`DURIN_HOME` selects the instance data root (config, memory, sessions, keys).
When unset it falls back to `~/.durin` — your live daily-driver instance.

**Always set `DURIN_HOME` to a throwaway directory before running tests or
any dev command that writes state.** Running without it will mix dev writes
into your live instance.

```bash
export DURIN_HOME=/tmp/durin-dev
```

Set this in your shell profile or `.envrc` so you never forget.
The test suite (`tests/conftest.py`) enforces its own throwaway `DURIN_HOME`
per test, but your terminal sessions do not get that protection automatically.

---

## Running tests

From the repo root (with `.venv` active):

```bash
python -m pytest tests/ -q
```

### In a git worktree

When working in a git worktree, use the worktree's own Python interpreter.
The bare `pytest` script resolves via the editable `.pth` in the **main**
checkout's site-packages, which imports the wrong tree.

```bash
.venv/bin/python -m pytest tests/ -q
```

### CI skips heavy extras

CI installs `[dev,mcp,web,oauth,slack,discord]` but skips `[memory]` and
`[local]` — those pull large model files not suitable for CI runners.
Tests that require those extras guard themselves with
`pytest.importorskip` or a `skipif` condition; they pass locally with the
extra installed and skip cleanly in CI without it.

---

## Regenerating the OpenAPI contract

When you add or remove an `@route` in `durin/service/`, regenerate the
committed contract and the generated TypeScript types:

```bash
PYTHONPATH=. python scripts/gen_openapi.py
cd webui && bun run gen:api-types
```

`scripts/gen_openapi.py` reads the live `@route` table from `durin.service`
and writes `contract/openapi-v1.json`.
`bun run gen:api-types` in `webui/` then regenerates
`webui/src/lib/api-types.ts` from that file
(script: `openapi-typescript ../contract/openapi-v1.json -o src/lib/api-types.ts`).

**Critical:** run the generator with `PYTHONPATH=.` pointing at the worktree.
Without it, the generator imports whichever `durin` package Python resolves
first — often the main checkout's editable install — and writes a contract
reflecting the wrong route table. `--check` will still pass against that
stale output, so the drift goes undetected until review.

To verify the committed contract matches the current code without writing:

```bash
PYTHONPATH=. python scripts/gen_openapi.py --check
```

Both the updated `contract/openapi-v1.json` and `webui/src/lib/api-types.ts`
belong in the same PR as the route change.

---

## Building the webui

The webui is managed with **bun** (preferred) or npm.

```bash
cd webui
bun install
bun run build
```

The compiled SPA lands at `durin/web/dist/` and is served by
`durin gateway`. A wheel built without the SPA will serve a 404 for the
dashboard — never set `DURIN_SKIP_WEBUI_BUILD=1` for release wheels.

To run the webui dev server against a live gateway:

```bash
cd webui
bun run dev
```

---

## PR and branch-protection flow

`main` is a protected branch. Direct pushes are blocked for everyone,
including the repo owner.

The required workflow:

1. Create a branch from `main`.
2. Push the branch and open a pull request.
3. CI runs the `test` job (path-filtered: Python-relevant changes only).
   The `test` check must pass before the PR can be merged.
4. Merge via **squash** — keep the commit history on `main` linear.

Doc changes that describe a code change ship in the **same PR** as the
code — do not open separate "docs:" follow-up PRs.

---

## Linting

```bash
ruff check .
ruff format .
```

CI does not enforce format as a blocking check, but PRs should be
lint-clean before review.
