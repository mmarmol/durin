"""A session the extract or derived-from pass could not process is named in
the log.

Both passes are best-effort per session: an exception is caught, counted in
``out["errors"]`` and the pass moves on. The CLI then printed "N session(s)
errored (see logs)", but nothing had been logged — a run on the box reported
two errored sessions with no way to tell which, or why. One warning per
failed session, naming it and the error.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from durin.memory import dream_passes


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "sessions").mkdir()
    (tmp_path / "memory").mkdir()
    for name in ("telegram_good", "telegram_bad"):
        (tmp_path / "sessions" / f"{name}.jsonl").write_text(
            '{"role": "user", "content": "hi"}\n', encoding="utf-8",
        )
    return tmp_path


def _capture():
    records: list = []
    sink = logger.add(lambda m: records.append(m.record), level="WARNING")
    return records, sink


def test_extract_pass_names_the_session_it_could_not_process(tmp_path: Path, monkeypatch) -> None:
    ws = _workspace(tmp_path)

    def fake_extract(workspace, jsonl_path, **kwargs):
        if jsonl_path.stem == "telegram_bad":
            raise ValueError("corrupt transcript at line 3")
        return {"extracted": [], "discovered": []}

    monkeypatch.setattr(dream_passes, "run_extract_for_session", fake_extract)
    records, sink = _capture()
    try:
        out = dream_passes.run_extract_pass(ws, llm_invoke=lambda *a, **k: "")
    finally:
        logger.remove(sink)

    assert out["errors"] == [{"session": "telegram_bad", "error": "corrupt transcript at line 3"}]
    named = [r for r in records if "telegram_bad" in r["message"]]
    assert len(named) == 1
    assert named[0]["level"].name == "WARNING"
    assert "corrupt transcript at line 3" in named[0]["message"]


def test_derived_from_pass_names_the_session_it_could_not_process(tmp_path: Path, monkeypatch) -> None:
    ws = _workspace(tmp_path)

    def fake_link(workspace, jsonl_path, **kwargs):
        if jsonl_path.stem == "telegram_bad":
            raise RuntimeError("document store unavailable")
        return {"linked": []}

    monkeypatch.setattr(dream_passes, "link_derived_from_for_session", fake_link)
    records, sink = _capture()
    try:
        out = dream_passes.run_derived_from_pass(ws, llm_invoke=lambda *a, **k: "")
    finally:
        logger.remove(sink)

    assert out["errors"] == [{"session": "telegram_bad", "error": "document store unavailable"}]
    named = [r for r in records if "telegram_bad" in r["message"]]
    assert len(named) == 1 and "document store unavailable" in named[0]["message"]
