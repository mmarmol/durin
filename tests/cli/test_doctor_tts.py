# tests/cli/test_doctor_tts.py
from durin.cli import doctor


def test_check_tts_installed_warns_without_extra(monkeypatch):
    # Force the import probe to fail → warn (never fail). check_optional_extra
    # uses importlib.import_module, so we must patch THAT (a builtins.__import__
    # patch is inert — import_module bypasses it).
    real_import_module = doctor.importlib.import_module

    def fake_import_module(name, *a, **k):
        if name == "supertonic":
            raise ImportError("no supertonic")
        return real_import_module(name, *a, **k)

    monkeypatch.setattr(doctor.importlib, "import_module", fake_import_module)
    r = doctor.check_tts_installed()
    assert r.status == "warn"  # extra forced-missing → warn (never ok/fail)
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
