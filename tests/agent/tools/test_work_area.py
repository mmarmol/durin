from pathlib import Path

from durin.agent.tools.work_area import (
    MANAGED_PREFIXES,
    session_work_dir,
    anchored_base,
)


def test_managed_prefixes_cover_workspace_dirs():
    for name in ("memory", "ingested", "skills", "sessions", "souls",
                 "workflows", "workflows-runs", "cron", "work"):
        assert name in MANAGED_PREFIXES


def test_session_work_dir_sanitizes_channel_key():
    ws = Path("/ws")
    assert session_work_dir(ws, "telegram:123") == ws / "work" / "telegram_123"


def test_anchored_base_managed_prefix_goes_to_workspace():
    ws, work = Path("/ws"), Path("/ws/work/s1")
    assert anchored_base("ingested", ws, work) == ws


def test_anchored_base_plain_name_goes_to_work():
    ws, work = Path("/ws"), Path("/ws/work/s1")
    assert anchored_base("scrape.py", ws, work) == work


def test_anchored_base_no_work_dir_falls_back_to_workspace():
    ws = Path("/ws")
    assert anchored_base("scrape.py", ws, None) == ws
