"""Tests for output_spill helper (Sprint A / T4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from durin.agent.tools.output_spill import (
    _SPILL_SUBDIR,
    truncate_with_spill,
)
from durin.agent.tools.shell import ExecTool
from durin.telemetry.logger import (
    TelemetryLogger,
    bind_telemetry,
    reset_telemetry,
)

# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


class TestTruncateWithSpill:

    def test_short_content_returns_unchanged(self, tmp_path: Path):
        content = "small"
        rendered, meta = truncate_with_spill(content, "tool", tmp_path, max_chars=100)
        assert rendered == content
        assert meta["spilled"] is False
        assert meta["original_chars"] == len(content)
        # No spill dir created when not needed.
        assert not (tmp_path / _SPILL_SUBDIR).exists()

    def test_large_content_spills_to_workspace(self, tmp_path: Path):
        content = "A" * 5000 + "BCD" + "E" * 5000  # 10003 chars
        rendered, meta = truncate_with_spill(content, "exec", tmp_path, max_chars=200)
        assert meta["spilled"] is True
        assert meta["original_chars"] == 10003
        assert "chars omitted" in rendered
        spill_path = Path(meta["spill_path"])
        assert spill_path.exists()
        assert spill_path.parent == (tmp_path / _SPILL_SUBDIR).resolve()
        # Spilled content matches original
        assert spill_path.read_text() == content

    def test_rendered_contains_head_and_tail(self, tmp_path: Path):
        # max_chars=2000 mirrors a realistic threshold; with the 200-char
        # footer reserve, head=1400 and tail=400 chars, so both markers fit.
        head_marker = "HEAD_MARKER_AT_START"
        tail_marker = "TAIL_MARKER_AT_END"
        content = head_marker + "M" * 5000 + tail_marker
        rendered, _ = truncate_with_spill(content, "exec", tmp_path, max_chars=2000)
        assert head_marker in rendered
        assert tail_marker in rendered

    def test_rendered_contains_read_file_hint(self, tmp_path: Path):
        content = "x" * 5000
        rendered, meta = truncate_with_spill(content, "exec", tmp_path, max_chars=200)
        assert "read_file" in rendered
        assert meta["spill_path"] in rendered

    def test_no_workspace_falls_back_to_tmp(self, tmp_path: Path, monkeypatch):
        """Without a workspace, spills go to /tmp/durin_spills/."""
        # Use monkeypatch to redirect the fallback dir so we don't litter /tmp.
        from durin.agent.tools import output_spill as os_mod

        fake_tmp = tmp_path / "fake_tmp_durin_spills"
        original = os_mod._spill_root

        def patched_root(workspace):
            if workspace is None:
                return fake_tmp
            return original(workspace)

        monkeypatch.setattr(os_mod, "_spill_root", patched_root)
        content = "x" * 5000
        rendered, meta = truncate_with_spill(content, "exec", workspace=None, max_chars=200)
        assert meta["spilled"] is True
        assert Path(meta["spill_path"]).parent == fake_tmp

    def test_spill_write_failure_falls_back_gracefully(self, tmp_path: Path, monkeypatch):
        """When the spill write raises, content is still truncated, just w/o ref."""

        # Patch the atomic write helper (the spill write path) to raise.
        def boom(*args, **kwargs):
            raise OSError("simulated write failure")

        monkeypatch.setattr(
            "durin.agent.tools.output_spill.atomic_write_text", boom
        )
        content = "x" * 5000
        rendered, meta = truncate_with_spill(content, "exec", tmp_path, max_chars=200)
        assert meta["spilled"] is False
        assert meta["spill_error"] is not None
        assert "spill write failed" in rendered
        # Truncation still applied so context doesn't blow up.
        assert len(rendered) < 5000


# ---------------------------------------------------------------------------
# A4 — redaction happens before the spill write
# ---------------------------------------------------------------------------


class TestSpillRedaction:

    def test_redact_applied_before_spill_write(self, tmp_path: Path):
        """When a redactor is supplied, the spilled file is redacted — the
        raw secret never lands on disk (A4)."""
        secret = "supersecretvalue123456"
        content = secret + "X" * 6000

        def fake_redact(text: str) -> str:
            return text.replace(secret, "«redacted:K»")

        rendered, meta = truncate_with_spill(
            content, "exec", tmp_path, max_chars=200, redact=fake_redact
        )
        assert meta["spilled"] is True
        spill_text = Path(meta["spill_path"]).read_text()
        assert secret not in spill_text            # not leaked to disk
        assert "«redacted:K»" in spill_text
        assert secret not in rendered              # nor in the returned head/tail

    def test_no_redactor_keeps_content_verbatim(self, tmp_path: Path):
        """Default (no redact) preserves the original spill behaviour."""
        content = "Z" * 6000
        _, meta = truncate_with_spill(content, "exec", tmp_path, max_chars=200)
        assert Path(meta["spill_path"]).read_text() == content


class TestExecToolSpillRedactionIntegration:

    @pytest.mark.asyncio
    async def test_exec_spill_redacts_stored_exec_secret(self, tmp_path, monkeypatch):
        """End-to-end: an exec-scoped secret echoed into a spilled exec output
        is redacted on disk, not just in the returned string (A4)."""
        import shlex
        import sys

        from durin.security import secrets as _secrets
        from durin.security.secrets import SecretStore

        config_path = tmp_path / "config.json"
        _secrets._STORE = None
        monkeypatch.setattr(
            "durin.config.loader.get_config_path", lambda: config_path
        )
        secret_val = "deploytokenSUPERsecretvalue123456"
        store = SecretStore(path=tmp_path / "secrets.json")
        store.put("DEPLOY_TOKEN", value=secret_val, service="x", scope=["exec"])
        _secrets.get_secret_store(reload=True)

        script = tmp_path / "leak.py"
        script.write_text(
            "import os\nprint(os.environ['DEPLOY_TOKEN'])\nprint('A' * 14000)\n"
        )
        cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"

        tool = ExecTool(working_dir=str(tmp_path))
        result = await tool.execute(command=cmd)

        spills = list((tmp_path / _SPILL_SUBDIR).glob("*.txt"))
        assert spills, "expected the large output to spill"
        spill_text = spills[0].read_text()
        assert secret_val not in spill_text      # A4: not on disk
        assert secret_val not in result          # nor in the model-facing result
        _secrets._STORE = None


# ---------------------------------------------------------------------------
# ExecTool integration
# ---------------------------------------------------------------------------


class TestExecToolSpillIntegration:

    @pytest.mark.asyncio
    async def test_large_exec_output_spilled(self, tmp_path: Path):
        """ExecTool spills outputs over _MAX_OUTPUT to <workspace>/.durin/spills/."""
        import shlex
        import sys

        # Generate well over 10K chars
        script = tmp_path / "noisy.py"
        script.write_text("print('A' * 8000); print('B' * 8000)\n")
        cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"

        tool = ExecTool(working_dir=str(tmp_path))
        result = await tool.execute(command=cmd)

        assert "chars omitted" in result
        assert "read_file" in result
        spill_dir = tmp_path / _SPILL_SUBDIR
        assert spill_dir.exists()
        spilled = list(spill_dir.iterdir())
        assert len(spilled) >= 1
        # Spilled file contains the full output
        content = spilled[0].read_text()
        assert "A" * 8000 in content

    @pytest.mark.asyncio
    async def test_small_exec_output_not_spilled(self, tmp_path: Path):
        import shlex
        import sys

        tool = ExecTool(working_dir=str(tmp_path))
        result = await tool.execute(command=f"{shlex.quote(sys.executable)} -c \"print('hi')\"")
        assert "chars omitted" not in result
        assert not (tmp_path / _SPILL_SUBDIR).exists()

    @pytest.mark.asyncio
    async def test_spill_emits_telemetry(self, tmp_path: Path):
        import shlex
        import sys

        log_path = tmp_path / "tel.jsonl"
        logger = TelemetryLogger(log_path)

        script = tmp_path / "noisy.py"
        script.write_text("print('A' * 12000)\n")
        cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"

        tool = ExecTool(working_dir=str(tmp_path))
        token = bind_telemetry(logger)
        try:
            await tool.execute(command=cmd)
        finally:
            reset_telemetry(token)

        events = [json.loads(line) for line in log_path.read_text().splitlines() if line]
        spill_events = [e for e in events if e["type"] == "tool.exec.spill"]
        assert len(spill_events) == 1
        data = spill_events[0]["data"]
        assert data["spilled"] is True
        assert data["original_chars"] > 10000
