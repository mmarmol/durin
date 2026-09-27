"""Redaction of structured tool results (dicts and nested lists)."""

from __future__ import annotations

from durin.security.secrets import SecretRedactor

TOKEN = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
STORED = "stored-secret-value-123456"


def _redactor() -> SecretRedactor:
    return SecretRedactor({"MY_SECRET": STORED}, patterns=True)


def test_every_string_inside_a_structured_result_is_redacted() -> None:
    result = {
        "results": [
            {"path": "a.env", "content": f"KEY={TOKEN}"},
            {"path": "b.txt", "meta": {"note": f"uses {STORED}", "tags": [f"t-{STORED}"]}},
        ],
        "sectioned_rendered": f"... {TOKEN} ...",
    }

    out = _redactor().redact(result)

    flat = repr(out)
    assert TOKEN not in flat
    assert STORED not in flat
    # Structure and non-secret values are untouched.
    assert out["results"][0]["path"] == "a.env"
    assert out["results"][1]["meta"]["tags"][0].startswith("t-")


def test_image_blocks_pass_through_byte_for_byte() -> None:
    payload = "data:image/png;base64," + "A" * 64
    blocks = [
        {"type": "text", "text": f"see {TOKEN}"},
        {"type": "image_url", "image_url": {"url": payload}},
    ]

    out = _redactor().redact(blocks)

    assert TOKEN not in out[0]["text"]
    assert out[1] == {"type": "image_url", "image_url": {"url": payload}}
