"""
durin - A lightweight AI agent framework
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
import tomllib


def _read_pyproject_version() -> str | None:
    """Read the source-tree version when package metadata is unavailable."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.exists():
        return None
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data.get("project", {}).get("version")


def _resolve_version() -> str:
    try:
        return _pkg_version("durin")
    except PackageNotFoundError:
        # Source checkouts often import durin without installed dist-info.
        return _read_pyproject_version() or "0.2.0"


__version__ = _resolve_version()
__logo__ = "🐈"


def __getattr__(name: str):
    if name in ("Durin", "RunResult"):
        from durin.durin_sdk import Durin, RunResult
        globals()["Durin"] = Durin
        globals()["RunResult"] = RunResult
        return globals()[name]
    raise AttributeError(f"module 'durin' has no attribute {name!r}")


__all__ = ["Durin", "RunResult"]
