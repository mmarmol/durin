# Releasing durin

This is the maintainer manual for cutting a release. End-users want
[INSTALL.md](INSTALL.md) instead.

The distribution name on PyPI is **`durin-agent`**. The import package
name stays `durin` and the CLI command is still `durin`.

---

## Release pipeline at a glance

```
git tag v0.1.0a1 → push → .github/workflows/release.yml fires
                                              │
                       ┌──────────────────────┼──────────────────────┐
                       ▼                      ▼                      ▼
              python -m build         GitHub Release          PyPI publish
              (sdist + wheel)         (artifacts attached,    (via OIDC trusted
                                       notes auto-gen)         publishing)
```

The workflow only fires on tags matching `v[0-9]+.[0-9]+.[0-9]+*` (so
`v0.1.0a1`, `v0.2.0`, `v1.0.0rc2` all trigger it; a doc-only tag like
`docs-2026-05` does not).

---

## One-time setup

### PyPI trusted publishing (no API tokens)

1. Visit <https://pypi.org/manage/account/publishing/>.
2. Add a new pending publisher with:
   - **PyPI project name**: `durin-agent`
   - **Owner**: `mmarmol`
   - **Repository name**: `durin`
   - **Workflow name**: `release.yml`
   - **Environment name**: `pypi`
3. Save. The first successful release will register the project; from
   then on every tagged build publishes automatically.

If you skip this step, the workflow still creates the GitHub Release —
only the `pypi-publish` job fails (and is marked `continue-on-error: true`
so the release isn't blocked).

### TestPyPI dry-run (optional)

To validate the pipeline without burning a real version number, add a
`pypi-publish-test` job that points to TestPyPI:

```yaml
url: https://test.pypi.org/p/durin-agent
# in the publish step:
repository-url: https://test.pypi.org/legacy/
```

Set up the matching pending publisher at
<https://test.pypi.org/manage/account/publishing/>.

---

## Cutting a release

1. Make sure `main` is green (`gh run list --workflow=ci.yml --limit=1`).
2. Bump the version in `pyproject.toml`:
   ```toml
   [project]
   version = "0.1.0a2"   # PEP 440 — aN/bN/rcN for pre-releases
   ```
3. Commit and push:
   ```bash
   git commit -am "chore: bump to 0.1.0a2"
   git push origin main
   ```
4. Tag and push the tag:
   ```bash
   git tag v0.1.0a2
   git push origin v0.1.0a2
   ```
5. Watch the workflow:
   ```bash
   gh run watch
   ```
6. Once green, verify:
   ```bash
   pipx install --pre durin-agent==0.1.0a2
   durin --version
   ```

The tag and the `pyproject.toml` version **must** match — the workflow
fails fast if they don't, to prevent shipping a wheel that doesn't
correspond to the named release.

### Version naming (PEP 440)

| Stage | Example | `pip install` |
|---|---|---|
| Alpha | `0.1.0a1` | `pip install --pre durin-agent` |
| Beta | `0.1.0b2` | `pip install --pre durin-agent` |
| Release candidate | `0.1.0rc1` | `pip install --pre durin-agent` |
| Final | `0.1.0` | `pip install durin-agent` |
| Post-release fix | `0.1.0.post1` | `pip install durin-agent` |

Pre-releases are filtered out by pip unless `--pre` is passed, so users
on `pip install durin-agent` keep getting stable versions even while
alphas exist on PyPI.

---

## Yanking a bad release

If a published release turns out broken:

1. **Don't delete it.** PyPI immutability means the version number is
   burned forever. Cutting `0.1.0a2` after pulling `0.1.0a1` is fine,
   but reusing `0.1.0a1` is not allowed.
2. From the PyPI project page, click **Manage → release → Yank**.
   Yanked releases stay downloadable by explicit pin (`durin-agent==0.1.0a1`)
   but are filtered out of `pip install durin-agent`.
3. Cut a fresh patch (`0.1.0a2`) with the fix.

---

## Local dry-run

To test the build end-to-end without pushing:

```bash
# Build sdist + wheel.
DURIN_SKIP_WEBUI_BUILD=1 python -m build

# Install the wheel into a clean venv to verify it's self-contained.
python -m venv /tmp/durin-dryrun && source /tmp/durin-dryrun/bin/activate
pip install dist/durin_agent-0.1.0a1-py3-none-any.whl
durin --version
durin doctor
deactivate
```

If `durin doctor` reports anything beyond config absence (which is
expected in a fresh venv that hasn't run `durin onboard`), the build is
not ready to ship.

---

Last updated: 2026-05-20 (D8).
