"""build_service_registry best-effort sweeps: stale loop claims are pruned
at gateway boot alongside the existing workflow/loops run_log crash-recovery
reconciles (durin/service/wiring.py)."""

from __future__ import annotations

import time
from dataclasses import dataclass

from durin.loops import claims
from durin.service.wiring import build_service_registry


@dataclass
class _FakeSessionManager:
    workspace: object


def test_stale_claim_is_pruned_on_registry_build(tmp_path):
    claims.register(tmp_path, key="digest-1", loop="l1", run_id="run1")
    # Backdate the registration well past the 7-day prune window.
    c = claims._load_claims(tmp_path)
    c["digest-1"]["registered_at"] = time.time() - 8 * 24 * 3600
    claims.claims_path(tmp_path).write_text(__import__("json").dumps(c))

    build_service_registry(config=None, session_manager=_FakeSessionManager(tmp_path))

    assert claims.lookup(tmp_path, "digest-1") is None


def test_fresh_claim_survives_registry_build(tmp_path):
    claims.register(tmp_path, key="digest-2", loop="l1", run_id="run1")

    build_service_registry(config=None, session_manager=_FakeSessionManager(tmp_path))

    assert claims.lookup(tmp_path, "digest-2") is not None
