"""OpenAPI 3.1 contract generator.

Reads the ``@route`` table + pydantic models declared in the service layer and
emits an OpenAPI 3.1 document.  This is the ONLY source of ``contract/openapi-v1.json``;
do not hand-edit that file.

Usage::

    # Regenerate the committed contract:
    python scripts/gen_openapi.py

    # Drift-check (exit 1 if current code differs from committed contract):
    python scripts/gen_openapi.py --check

Schema merging
--------------
Each pydantic model's ``model_json_schema(ref_template="#/components/schemas/{model}")``
already rewrites ``$ref`` values to ``#/components/schemas/<Name>``.  The ``$defs``
block carries the inline sub-model schemas.  The generator:

1. Strips ``$defs`` from each model's top-level schema.
2. Adds both the top-level schema and all ``$defs`` entries to ``components/schemas``.
3. The resulting document has no ``$defs`` keys anywhere — only ``$ref`` pointers
   into ``#/components/schemas``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def _collect_schemas(
    model: type,
    components: dict[str, dict],
) -> str:
    """Add ``model`` and all its nested sub-models to ``components``.

    Returns the ``$ref`` string pointing to the model's top-level schema in
    ``components/schemas``.  Idempotent: re-collecting the same model is a
    no-op (schemas are identical for the same pydantic class).
    """
    schema = model.model_json_schema(
        ref_template="#/components/schemas/{model}", by_alias=False
    )
    # Hoist sub-model definitions — pydantic puts them in "$defs".
    for name, defn in schema.pop("$defs", {}).items():
        components[name] = defn
    # Register the top-level model itself.
    name = schema.get("title", model.__name__)
    components[name] = schema
    return f"#/components/schemas/{name}"


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------


def build_openapi() -> dict:
    """Walk the service route table and produce an OpenAPI 3.1 document dict."""
    from durin.service.catalog import build_catalog_registry

    registry = build_catalog_registry()

    paths: dict[str, dict] = {}
    components: dict[str, dict] = {}

    for bound in registry.routes:
        spec = bound.spec
        verb = spec.verb.lower()
        path = spec.path

        operation: dict = {
            "summary": spec.summary,
            "operationId": f"{bound.service_name}_{bound.handler.__name__}",
        }

        if spec.scope is not None:
            operation["x-required-scope"] = spec.scope

        if spec.request_model is not None:
            ref = _collect_schemas(spec.request_model, components)
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": ref},
                    }
                },
            }

        if spec.response_model is not None:
            ref = _collect_schemas(spec.response_model, components)
            operation["responses"] = {
                "200": {
                    "description": "Success",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": ref},
                        }
                    },
                }
            }
        else:
            operation["responses"] = {"200": {"description": "Success"}}

        paths.setdefault(path, {})[verb] = operation

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "durin API",
            "version": "v1",
        },
        "paths": dict(sorted(paths.items())),
        "components": {
            "schemas": dict(sorted(components.items())),
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_CONTRACT_PATH = Path(__file__).parent.parent / "contract" / "openapi-v1.json"


def _dump(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate or drift-check contract/openapi-v1.json."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the generated contract differs from the committed file.",
    )
    args = parser.parse_args()

    doc = build_openapi()
    generated = _dump(doc)

    if args.check:
        if not _CONTRACT_PATH.exists():
            print(
                f"ERROR: {_CONTRACT_PATH} does not exist. "
                "Run python scripts/gen_openapi.py to generate it.",
                file=sys.stderr,
            )
            sys.exit(1)
        committed = _CONTRACT_PATH.read_text()
        if generated != committed:
            print(
                "ERROR: contract/openapi-v1.json is out of date.\n"
                "Run python scripts/gen_openapi.py to regenerate it.",
                file=sys.stderr,
            )
            sys.exit(1)
        print("OK: contract/openapi-v1.json is up to date.")
    else:
        _CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONTRACT_PATH.write_text(generated)
        route_count = sum(
            len(verbs) for verbs in doc["paths"].values()
        )
        schema_count = len(doc["components"]["schemas"])
        print(
            f"Wrote {_CONTRACT_PATH} "
            f"({route_count} operations, {schema_count} schemas)"
        )


if __name__ == "__main__":
    main()
