from durin.config.schema import MemoryConfig, MemoryEagerSurfaceConfig


def test_eager_surface_defaults():
    cfg = MemoryConfig().eager_surface
    assert cfg.freeze is True
    assert cfg.refresh_after_min == 0


def test_eager_surface_loads_from_camelcase_alias():
    # Base uses alias_generator=to_camel + populate_by_name, so the field
    # round-trips through memory.json under the camelCase `refreshAfterMin` key.
    cfg = MemoryEagerSurfaceConfig.model_validate({"refreshAfterMin": 15})
    assert cfg.refresh_after_min == 15
