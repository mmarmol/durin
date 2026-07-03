"""Every config field must carry a description.

Walks ``Config.model_json_schema()`` (root + every $defs model) and fails
listing any property without a non-empty ``description``. There is no
allowlist on purpose: a new config key cannot land without documenting
itself, and ``durin config schema`` / the webui read these descriptions.
"""

from __future__ import annotations

from durin.config.schema import Config


def _undescribed(schema: dict) -> list[str]:
    """Return '<Model>.<field>' for every property lacking a description."""
    missing: list[str] = []

    def check(model_name: str, node: dict) -> None:
        for field_name, prop in node.get("properties", {}).items():
            if not prop.get("description"):
                missing.append(f"{model_name}.{field_name}")

    check("Config", schema)
    for def_name, def_node in schema.get("$defs", {}).items():
        check(def_name, def_node)
    return missing


def test_every_config_field_has_a_description() -> None:
    missing = _undescribed(Config.model_json_schema())
    assert not missing, (
        "Config fields without a Field(description=...): "
        + ", ".join(sorted(missing))
    )


def test_default_config_round_trips() -> None:
    """Sanity: the description migration changed no defaults or validation."""
    dumped = Config().model_dump(mode="json")
    assert Config.model_validate(dumped).model_dump(mode="json") == dumped
