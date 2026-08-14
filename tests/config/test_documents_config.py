"""The documents config section: OCR toggle and the extraction limits."""

from durin.config.schema import Config


def test_documents_section_exists_with_defaults():
    cfg = Config()
    assert cfg.documents.ocr.enabled is False
    assert cfg.documents.ocr.inline_max_pages == 5
    assert cfg.documents.max_file_size_mb == 50
    assert cfg.documents.max_text_chars == 200_000


def test_ocr_is_off_by_default():
    # The extra is not installed by default; the toggle must not imply it is.
    assert Config().documents.ocr.enabled is False


def test_inline_max_pages_accepts_zero_to_disable_inline_ocr():
    cfg = Config.model_validate({"documents": {"ocr": {"inline_max_pages": 0}}})
    assert cfg.documents.ocr.inline_max_pages == 0


def test_inline_max_pages_rejects_negative():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.model_validate({"documents": {"ocr": {"inline_max_pages": -1}}})


def test_camel_case_alias_is_accepted():
    cfg = Config.model_validate(
        {"documents": {"ocr": {"inlineMaxPages": 12}, "maxFileSizeMb": 80}}
    )
    assert cfg.documents.ocr.inline_max_pages == 12
    assert cfg.documents.max_file_size_mb == 80
