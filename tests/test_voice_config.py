from durin.config.schema import VoiceConfig


def test_voice_config_defaults():
    c = VoiceConfig()
    assert c.enabled is True
    assert c.barge_in is True
    assert c.vad_threshold == 0.5            # proven (Silero)
    assert c.end_of_turn_silence_ms == 700   # proven
    assert c.idle_timeout_s == 300           # proven
    assert c.spoken_render.mode == "model_led"
    assert c.spoken_render.long_threshold_words == 60


def test_voice_config_parses_overrides():
    c = VoiceConfig.model_validate(
        {"barge_in": False, "spoken_render": {"mode": "verbatim", "pointer": "El detalle está en pantalla."}}
    )
    assert c.barge_in is False
    assert c.spoken_render.mode == "verbatim"
    assert c.spoken_render.pointer == "El detalle está en pantalla."


def test_legacy_aux_summary_mode_coerces_to_model_led():
    # An earlier build persisted mode="aux_summary" (never wired; it always
    # degraded to model_led). Such a config must still load, with that behavior.
    c = VoiceConfig.model_validate({"spoken_render": {"mode": "aux_summary"}})
    assert c.spoken_render.mode == "model_led"
