"""Pre-build the local speech engines so the first STT/TTS at use-time is
instant (no surprise download mid-conversation).

Shared by gateway startup (warm whatever is installed) and the
``durin voice warmup`` CLI command (which onboarding runs after installing the
speech extras, in a fresh process). Keeping the gating here — which extra each
engine needs, the local-provider check, the enabled check — means startup and
onboard cannot drift apart.
"""

from __future__ import annotations

# (label, ok, error) per engine that was actually attempted. Engines that are
# disabled, or local-but-not-installed, are skipped silently (absent from the
# list). A cloud provider warms to a no-op and reports ok.
WarmResult = tuple[str, bool, "str | None"]


async def warm_speech_services(
    transcription, transcription_cfg, speech, tts_cfg
) -> list[WarmResult]:
    """Warm the STT + TTS services and report what was attempted.

    ``transcription``/``speech`` are the built services (or None); the ``*_cfg``
    are their config blocks. A local engine is only warmed when its extra is
    importable; cloud providers warm to a no-op. Engine-build failures are
    captured per engine (never raised) so one failure can't abort the other.
    """
    from durin.extras import _module_present

    results: list[WarmResult] = []

    async def _warm(svc, cfg, module: str, label: str) -> None:
        if svc is None or not getattr(cfg, "enabled", False):
            return
        # Only a local engine needs its extra present; cloud providers no-op.
        if getattr(cfg, "provider", None) == "local" and not _module_present(module):
            return
        try:
            await svc.warmup()
            results.append((label, True, None))
        except Exception as e:  # noqa: BLE001 — report, don't abort the sibling
            results.append((label, False, str(e)))

    await _warm(transcription, transcription_cfg, "sherpa_onnx", "Transcription")
    await _warm(speech, tts_cfg, "supertonic", "Speech synthesis")
    return results
