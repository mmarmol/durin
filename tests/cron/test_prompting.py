from durin.cron.prompting import build_cron_turn_prompt


def test_reminder_mode_wraps_with_delivery_framing():
    out = build_cron_turn_prompt("reminder", "water the plants")
    assert "water the plants" in out
    assert "reminder" in out.lower()


def test_task_mode_is_raw_prompt():
    out = build_cron_turn_prompt("task", "pull the top bug and open a draft PR")
    assert out.strip() == "pull the top bug and open a draft PR"


def test_late_run_gets_a_note_naming_how_late_it_is():
    now = 1_700_000_000_000
    scheduled = now - 30 * 60 * 1000
    out = build_cron_turn_prompt("reminder", "water the plants", scheduled_at_ms=scheduled, now_ms=now)
    assert "water the plants" in out
    assert "late" in out.lower()
    assert "30 min" in out


def test_on_time_run_has_no_late_note():
    now = 1_700_000_000_000
    out = build_cron_turn_prompt("reminder", "water the plants", scheduled_at_ms=now - 30_000, now_ms=now)
    assert "late" not in out.lower()


def test_task_mode_late_note_keeps_the_raw_prompt_first():
    now = 1_700_000_000_000
    out = build_cron_turn_prompt("task", "open a PR", scheduled_at_ms=now - 10 * 60 * 1000, now_ms=now)
    assert out.startswith("open a PR")
    assert "late" in out.lower()
