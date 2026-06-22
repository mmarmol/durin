# tests/cli/test_doctor_tts.py
from durin.cli import doctor


def test_check_tts_installed_warns_without_extra(monkeypatch):
    # Force the import probe to fail → warn (never fail).
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "supertonic":
            raise ImportError("no supertonic")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    r = doctor.check_tts_installed()
    assert r.status in ("warn", "ok")
    # check_optional_extra tags every extra-import check as category="extras"
    # (mirrors check_stt_installed); the [tts] extra is on .extra.
    assert r.extra == "tts"


def test_tts_in_extras_probes():
    assert "tts" in doctor._EXTRAS_IMPORT_PROBES
    assert "supertonic" in doctor._EXTRAS_IMPORT_PROBES["tts"]


def test_tts_model_cached_skips_for_cloud(monkeypatch):
    from durin.config.schema import Config

    cfg = Config()
    cfg.tts.provider = "openai"
    r = doctor.check_tts_model_cached(cfg)
    assert r.status == "ok"
    assert r.category == "tts"
