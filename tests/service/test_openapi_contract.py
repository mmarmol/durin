"""Tests for the OpenAPI 3.1 contract (SP3).

(a) Drift — ``build_openapi()`` deep-equals the committed ``contract/openapi-v1.json``.
(b) Coverage — every ``@route``-decorated method across all services appears as an
    operation in the document.
(c) No dangling ``$ref`` — every ``$ref`` in the document resolves into
    ``components/schemas``.
(d) Scope validity — every ``x-required-scope`` value is a real ``Scope`` enum value
    or absent (meaning the route requires no auth).
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

from durin.service.catalog import SERVICE_CLASSES, build_catalog_registry
from durin.service.principal import Scope

_CONTRACT_PATH = Path(__file__).parent.parent.parent / "contract" / "openapi-v1.json"
_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"


def _load_gen_openapi():
    """Load scripts/gen_openapi.py as a module (not a package)."""
    spec = importlib.util.spec_from_file_location(
        "gen_openapi", _SCRIPTS_DIR / "gen_openapi.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_SCOPE_VALUES = {s.value for s in Scope}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_committed() -> dict:
    return json.loads(_CONTRACT_PATH.read_text())


def _all_route_specs():
    """Yield (service_name, method_name, RouteSpec) for every @route method."""
    registry = build_catalog_registry()
    for bound in registry.routes:
        yield bound.service_name, bound.handler.__name__, bound.spec


def _all_refs(doc: dict) -> set[str]:
    """Extract every #/components/schemas/<name> ref value from the document."""
    text = json.dumps(doc)
    return set(re.findall(r"#/components/schemas/(\w+)", text))


# ---------------------------------------------------------------------------
# (a) Drift check
# ---------------------------------------------------------------------------


def test_contract_matches_committed():
    """Generated document is byte-for-byte identical to the committed JSON."""
    gen = _load_gen_openapi()
    generated = gen._dump(gen.build_openapi())
    committed = _CONTRACT_PATH.read_text()
    assert generated == committed, (
        "contract/openapi-v1.json is out of date — run python scripts/gen_openapi.py"
    )


# ---------------------------------------------------------------------------
# (b) Every @route method appears in the document
# ---------------------------------------------------------------------------


def test_all_routes_present_in_document():
    """Every @route-decorated method is represented as an operation in the doc."""
    doc = _load_committed()
    # Build a set of (verb, path) from the document.
    doc_ops: set[tuple[str, str]] = set()
    for path, path_item in doc["paths"].items():
        for verb in path_item:
            doc_ops.add((verb.upper(), path))

    registry = build_catalog_registry()
    missing = []
    for bound in registry.routes:
        key = (bound.spec.verb.upper(), bound.spec.path)
        if key not in doc_ops:
            missing.append(f"{bound.spec.verb} {bound.spec.path}")

    assert not missing, f"Routes missing from contract: {missing}"


def test_route_count():
    """Document has the expected number of operations (one per @route)."""
    doc = _load_committed()
    op_count = sum(len(path_item) for path_item in doc["paths"].values())
    registry = build_catalog_registry()
    assert op_count == len(registry.routes)


# ---------------------------------------------------------------------------
# (c) No dangling $ref
# ---------------------------------------------------------------------------


def test_no_dangling_refs():
    """Every $ref resolves into components/schemas."""
    doc = _load_committed()
    refs = _all_refs(doc)
    schemas = set(doc["components"]["schemas"].keys())
    dangling = refs - schemas
    assert not dangling, f"Dangling $refs: {dangling}"


def test_no_defs_keys_in_document():
    """The document must not contain any '$defs' keys (all hoisted to components)."""
    text = _CONTRACT_PATH.read_text()
    assert '"$defs"' not in text


# ---------------------------------------------------------------------------
# (d) Scope validity
# ---------------------------------------------------------------------------


def test_scopes_are_valid():
    """Every x-required-scope value is a real Scope enum value."""
    doc = _load_committed()
    bad = []
    for path, path_item in doc["paths"].items():
        for verb, operation in path_item.items():
            scope = operation.get("x-required-scope")
            if scope is not None and scope not in _SCOPE_VALUES:
                bad.append(f"{verb.upper()} {path}: {scope!r}")
    assert not bad, f"Unknown scopes in contract: {bad}"


def test_all_service_classes_enumerated():
    """SERVICE_CLASSES contains all 16 expected service types."""
    assert len(SERVICE_CLASSES) == 16


def test_catalog_registry_has_no_duplicate_routes():
    """build_catalog_registry() raises no duplicate-route errors."""
    registry = build_catalog_registry()
    seen: set[tuple[str, str]] = set()
    for bound in registry.routes:
        key = (bound.spec.verb, bound.spec.path)
        assert key not in seen, f"Duplicate route: {key}"
        seen.add(key)
