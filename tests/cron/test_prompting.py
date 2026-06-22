from durin.cron.prompting import build_cron_turn_prompt


def test_reminder_mode_wraps_with_delivery_framing():
    out = build_cron_turn_prompt("reminder", "water the plants")
    assert "water the plants" in out
    assert "reminder" in out.lower()


def test_task_mode_is_raw_prompt():
    out = build_cron_turn_prompt("task", "pull the top bug and open a draft PR")
    assert out.strip() == "pull the top bug and open a draft PR"
