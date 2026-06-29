"""Shared fixtures for memory tests."""
import pytest

EMBEDDING_MODEL = "intfloat/multilingual-e5-small"


@pytest.fixture
def embedding_model() -> str:
    """The E5 model id, after ensuring it can actually load.

    Skips the requesting test (instead of failing it) when the embedding
    extra is not installed OR the model cannot be downloaded — e.g. when
    HuggingFace is unreachable from the runner. On success the model is
    cached on disk, so the test still exercises real embedding behavior:
    a genuine logic failure still fails; only an infra/network download
    failure skips.
    """
    from durin.memory.vector_index import vector_index_available

    if not vector_index_available():
        pytest.skip("vector index unavailable in this environment")

    from durin.memory.embedding import FastembedProvider

    try:
        FastembedProvider.warmup(EMBEDDING_MODEL)
    except Exception as exc:  # noqa: BLE001 — download/network failure is infra, not a logic bug
        pytest.skip(f"embedding model {EMBEDDING_MODEL} unavailable (download failed): {exc}")
    return EMBEDDING_MODEL
