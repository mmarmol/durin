"""Hatch build hook that bundles the webui (Vite) into durin/web/dist.

Triggered automatically by `python -m build` (and any other hatch-driven build)
so published wheels and sdists ship a fresh webui without requiring developers
to remember `cd webui && bun run build` beforehand.

Behaviour:

- Skips for editable installs (`pip install -e .`). Editable mode is for Python
  development; webui contributors use `cd webui && bun run dev` (Vite HMR) and
  do not need a packaged `dist/`.
- No-op when `webui/package.json` is absent (e.g. installing from an sdist that
  already contains a prebuilt `durin/web/dist/`).
- Skips when `DURIN_SKIP_WEBUI_BUILD=1` is set.
- Skips when `durin/web/dist/index.html` already exists, unless
  `DURIN_FORCE_WEBUI_BUILD=1` is set.
- Uses `bun` when available, otherwise falls back to `npm`. The chosen tool
  performs `install` followed by `run build`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_SPA_ENTRYPOINT = "durin/web/dist/index.html"


def _wheel_has_spa(names) -> bool:
    """True if a wheel namelist bundles the webui SPA entrypoint."""
    return any(n.endswith(_SPA_ENTRYPOINT) for n in names)


class WebUIBuildHook(BuildHookInterface):
    PLUGIN_NAME = "webui-build"

    def finalize(self, version: str, build_data: dict, artifact_path: str) -> None:
        # Guard: a wheel without the bundled webui SPA serves a 404 dashboard, and
        # the default `python -m build` (sdist→wheel) can silently drop
        # durin/web/dist/. Fail loudly here instead of shipping a broken wheel.
        if self.target_name != "wheel" or version == "editable":
            return
        if os.environ.get("DURIN_SKIP_WEBUI_BUILD") == "1":
            self.app.display_info(
                "[webui-build] SPA-in-wheel assert skipped via DURIN_SKIP_WEBUI_BUILD=1"
            )
            return
        import zipfile

        try:
            with zipfile.ZipFile(artifact_path) as zf:
                names = zf.namelist()
        except Exception:  # noqa: BLE001 — never mask the real build error
            return
        if not _wheel_has_spa(names):
            raise RuntimeError(
                f"[webui-build] built wheel {artifact_path} is MISSING "
                f"{_SPA_ENTRYPOINT} — it would serve a 404 dashboard. Build the wheel "
                "directly (`python -m build --wheel`) so the SPA is force-included, or "
                "set DURIN_SKIP_WEBUI_BUILD=1 to intentionally ship without it."
            )

    def initialize(self, version: str, build_data: dict) -> None:  # noqa: D401
        root = Path(self.root)
        webui_dir = root / "webui"
        package_json = webui_dir / "package.json"
        dist_dir = root / "durin" / "web" / "dist"
        index_html = dist_dir / "index.html"

        # `pip install -e .` builds an editable wheel; skip the (slow) webui
        # bundle since editable installs target Python development and webui
        # work uses `bun run dev` instead.
        if self.target_name == "wheel" and version == "editable":
            self.app.display_info(
                "[webui-build] skipped for editable install "
                "(use `cd webui && bun run build` to bundle webui manually)"
            )
            return

        if os.environ.get("DURIN_SKIP_WEBUI_BUILD") == "1":
            self.app.display_info("[webui-build] skipped via DURIN_SKIP_WEBUI_BUILD=1")
            return

        if not package_json.is_file():
            self.app.display_info(
                "[webui-build] no webui/ source tree, assuming prebuilt durin/web/dist/"
            )
            return

        force = os.environ.get("DURIN_FORCE_WEBUI_BUILD") == "1"
        if index_html.is_file() and not force:
            self.app.display_info(
                f"[webui-build] reusing existing build at {dist_dir} "
                "(set DURIN_FORCE_WEBUI_BUILD=1 to rebuild)"
            )
            return

        runner = self._pick_runner()
        if runner is None:
            raise RuntimeError(
                "[webui-build] neither `bun` nor `npm` is available on PATH; "
                "install one or set DURIN_SKIP_WEBUI_BUILD=1 to bypass."
            )

        self.app.display_info(f"[webui-build] using {runner} to build webui")
        self._run([runner, "install"], cwd=webui_dir)
        self._run([runner, "run", "build"], cwd=webui_dir)

        if not index_html.is_file():
            raise RuntimeError(
                f"[webui-build] build finished but {index_html} is missing; "
                "check webui/vite.config.ts outDir."
            )
        self.app.display_info(f"[webui-build] webui ready at {dist_dir}")

    @staticmethod
    def _pick_runner() -> str | None:
        for candidate in ("bun", "npm"):
            if shutil.which(candidate):
                return candidate
        return None

    def _run(self, cmd: list[str], *, cwd: Path) -> None:
        self.app.display_info(f"[webui-build] $ {' '.join(cmd)} (cwd={cwd})")
        try:
            subprocess.run(cmd, cwd=cwd, check=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"[webui-build] command failed ({exc.returncode}): {' '.join(cmd)}"
            ) from exc
