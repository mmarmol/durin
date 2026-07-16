from durin.memory import dream_passes


class _Cfg:
    class memory:
        class embedding:
            model = "fake-model"


def test_dream_vector_index_none_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(dream_passes, "vector_index_available", lambda: False)
    assert dream_passes.dream_vector_index(tmp_path, _Cfg) is None


def test_dream_vector_index_builds_when_available(tmp_path, monkeypatch):
    built = {}

    class _FakeVI:
        def __init__(self, ws, provider):
            built["ws"] = ws
            built["provider_model"] = getattr(provider, "model", None)

    class _FakeProvider:
        def __init__(self, *, model):
            self.model = model

    def fake_provider_from_config(cfg):
        return _FakeProvider(model=cfg.memory.embedding.model)

    monkeypatch.setattr(dream_passes, "vector_index_available", lambda: True)
    monkeypatch.setattr(dream_passes, "VectorIndex", _FakeVI)
    monkeypatch.setattr(dream_passes, "provider_from_config", fake_provider_from_config)
    vi = dream_passes.dream_vector_index(tmp_path, _Cfg)
    assert isinstance(vi, _FakeVI)
    assert built["provider_model"] == "fake-model"
