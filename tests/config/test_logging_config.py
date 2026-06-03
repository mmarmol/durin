from durin.config.schema import Config


def test_logging_defaults():
    cfg = Config()
    assert cfg.logging.max_file_mb == 5
    assert cfg.logging.retention_days == 7


def test_logging_camel_and_snake_aliases():
    cfg = Config.model_validate({"logging": {"maxFileMb": 10, "retention_days": 3}})
    assert cfg.logging.max_file_mb == 10
    assert cfg.logging.retention_days == 3


def test_logging_bounds():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Config.model_validate({"logging": {"max_file_mb": 0}})
