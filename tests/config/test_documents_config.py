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


# The languages the engine can add beyond its built-in pack, verified against
# rapidocr 3.9.2's model table for the PP-OCRv5 mobile recognizers the engine
# is constructed with. Deliberately a copy of the schema's Literal values: if
# either side changes alone, these tests are what says so.
OCR_LANGUAGES = (
    "arabic", "cyrillic", "devanagari", "el", "eslav", "korean", "ta", "te", "th",
)


def test_ocr_language_defaults_to_none_the_built_in_pack():
    assert Config().documents.ocr.language is None


def test_ocr_language_accepts_every_curated_code():
    for code in OCR_LANGUAGES:
        cfg = Config.model_validate({"documents": {"ocr": {"language": code}}})
        assert cfg.documents.ocr.language == code


def test_ocr_language_rejects_an_unknown_code_naming_the_allowed_set():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        Config.model_validate({"documents": {"ocr": {"language": "klingon"}}})

    message = str(excinfo.value)
    for code in OCR_LANGUAGES:
        assert f"'{code}'" in message


def test_ocr_language_description_names_the_one_time_download():
    # Selecting the language is the consent for the one-time model download,
    # so the schema description (what `durin config schema` and the webui
    # show) must say that download happens, and from where.
    from durin.config.schema import DocumentsOcrConfig

    description = DocumentsOcrConfig.model_fields["language"].description
    assert description
    assert "download" in description.lower()
    assert "modelscope.cn" in description
