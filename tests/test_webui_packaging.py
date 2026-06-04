"""The build hook must never let a wheel ship without the bundled webui SPA.

The default `python -m build` (sdist→wheel) silently dropped durin/web/dist/ once,
producing a wheel that serves a 404 dashboard. `_wheel_has_spa` backs the finalize
guard that fails the build in that case.
"""
import pytest

# hatch_build imports hatchling (a build-system dep, not installed in the test env /
# CI). Skip this module there; it still runs locally where the build tooling exists.
pytest.importorskip("hatchling")

import hatch_build  # noqa: E402


def test_wheel_with_spa_is_detected():
    names = [
        "durin/__init__.py",
        "durin/web/dist/index.html",
        "durin/web/dist/assets/index-abc.js",
    ]
    assert hatch_build._wheel_has_spa(names) is True


def test_wheel_without_spa_is_rejected():
    names = [
        "durin/__init__.py",
        "durin/web/__init__.py",
        "durin/channels/websocket.py",
    ]
    assert hatch_build._wheel_has_spa(names) is False


def test_empty_wheel_has_no_spa():
    assert hatch_build._wheel_has_spa([]) is False
