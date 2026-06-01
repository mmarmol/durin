"""B4 + B5: the embedding provider is shared between the agent loop and the
Dream daemon thread, so its lazy first-use path must be thread-safe.

Two races, both on first use of a *shared* ``FastembedProvider``:

- B5: ``_register_custom_models`` mutates the module-level ``_REGISTERED_CUSTOM``
  set with a check-then-act, and ``add_custom_model`` raises ``ValueError`` on a
  re-registration that is caught only for ``ImportError`` — so a concurrent
  second registration propagates out of provider init.
- B4: ``embed``'s ``if self._model is None: self._load()`` is check-then-act, so
  two threads both construct the (heavy) ONNX model.

Both tests inject a fake ``fastembed`` whose hot call ``sleep``s to widen the
window, making the interleave deterministic rather than timing-dependent.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from contextlib import contextmanager

import durin.memory.embedding as em

_E5 = "intfloat/multilingual-e5-small"


def _make_fastembed(*, catalog_models: list[str], slow: float):
    """Build a fake ``fastembed.TextEmbedding`` with instrumented hot calls."""

    class _Fake:
        add_calls: list[str] = []
        _added: set[str] = set()
        init_count = 0

        @staticmethod
        def list_supported_models() -> list[dict]:
            return [{"model": m, "dim": 384} for m in catalog_models]

        @staticmethod
        def add_custom_model(*, model: str, **_kw) -> None:
            time.sleep(slow)  # widen the check→add window
            if model in _Fake._added:
                # Mirror real fastembed: re-registration is an error.
                raise ValueError(f"{model} already registered")
            _Fake._added.add(model)
            _Fake.add_calls.append(model)

        def __init__(self, model_name: str | None = None, **_kw) -> None:
            type(self).init_count += 1
            time.sleep(slow)
            self.model_name = model_name

        def embed(self, texts: list[str]):
            for i, _ in enumerate(texts):
                yield [float(i)] * 384

    return _Fake


@contextmanager
def _inject(fake):
    em._CATALOG_CACHE = None
    em._REGISTERED_CUSTOM = set()
    fm = types.ModuleType("fastembed")
    fm.TextEmbedding = fake  # type: ignore[attr-defined]
    fc = types.ModuleType("fastembed.common")
    fmd = types.ModuleType("fastembed.common.model_description")
    fmd.PoolingType = type("PoolingType", (), {"MEAN": "MEAN", "CLS": "CLS"})
    fmd.ModelSource = type("ModelSource", (), {"__init__": lambda self, **kw: None})
    sys.modules["fastembed"] = fm
    sys.modules["fastembed.common"] = fc
    sys.modules["fastembed.common.model_description"] = fmd
    try:
        yield
    finally:
        for m in ("fastembed", "fastembed.common", "fastembed.common.model_description"):
            sys.modules.pop(m, None)
        em._CATALOG_CACHE = None
        em._REGISTERED_CUSTOM = set()


def test_register_custom_models_is_concurrency_safe():
    """B5: two threads registering at once must not double-register."""
    # Catalog WITHOUT the custom model, so registration actually runs.
    fake = _make_fastembed(catalog_models=["some/other-model"], slow=0.05)
    errors: list[Exception] = []

    def worker():
        try:
            em._register_custom_models()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    with _inject(fake):
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert not errors, f"concurrent registration raised: {errors}"
    assert fake.add_calls.count(_E5) == 1, f"registered {fake.add_calls.count(_E5)}x"


def test_shared_provider_loads_model_only_once_under_concurrency():
    """B4: two threads embedding on one provider must load the model once."""
    # Catalog WITH the model, so construction validates and registration skips.
    fake = _make_fastembed(catalog_models=[_E5], slow=0.05)

    with _inject(fake):
        provider = em.FastembedProvider(model=_E5)
        fake.init_count = 0  # ignore any construction during validation

        def worker():
            provider.embed(["hello world"])

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert fake.init_count == 1, f"model constructed {fake.init_count}x"
