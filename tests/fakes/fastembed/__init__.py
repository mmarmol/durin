"""A stand-in for the fastembed package, for tests: no model files, nothing
downloaded.

``TextEmbedding`` embeds a text as the normalized, hashed bag of its
lowercase words: texts that share words land near each other, and a text
gets the same vector in every process. That last part is why this is a
package on disk rather than an object in ``sys.modules``: the embedding
pool's workers are spawned processes, which import from the parent's
``sys.path``, so putting this directory first on it reaches them too.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Iterator

# The models durin offers, with fastembed's catalog fields durin reads.
_CATALOG: list[dict] = [
    {"model": "intfloat/multilingual-e5-small", "dim": 384, "size_in_GB": 0.45},
    {"model": "intfloat/multilingual-e5-large", "dim": 1024, "size_in_GB": 2.24},
    {"model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
     "dim": 384, "size_in_GB": 0.22},
    {"model": "sentence-transformers/all-MiniLM-L6-v2", "dim": 384, "size_in_GB": 0.09},
]

_WORD = re.compile(r"\w+")


def _vector(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    for word in _WORD.findall(text.lower()):
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        slot = int.from_bytes(digest[:4], "big") % dim
        vec[slot] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class TextEmbedding:
    def __init__(self, model_name: str, **kwargs) -> None:
        dims = {m["model"]: m["dim"] for m in _CATALOG}
        if model_name not in dims:
            raise ValueError(f"Model {model_name} is not supported in TextEmbedding.")
        self.model_name = model_name
        self.dim = dims[model_name]

    @classmethod
    def list_supported_models(cls) -> list[dict]:
        return [dict(m) for m in _CATALOG]

    @classmethod
    def add_custom_model(cls, model: str, dim: int, **kwargs) -> None:
        if all(m["model"] != model for m in _CATALOG):
            _CATALOG.append({"model": model, "dim": dim})

    def embed(self, documents: str | Iterable[str], batch_size: int = 256,
              **kwargs) -> Iterator[list[float]]:
        if isinstance(documents, str):
            documents = [documents]
        for text in documents:
            yield _vector(text, self.dim)
