"""Tests for the boot-time loops -> automations converter.

Called at gateway boot, before durin.automations.cron_sync.sync_all (see
durin.cli.commands) — these tests drive it directly. Fixtures are written as
plain JSON matching exactly the shape the (now-deleted) loops package's own
store.save_loop used to write, so the on-disk shape being migrated is the
same one a live loops installation would have had, not a hand-guessed
approximation.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from durin.automations import run_log
from durin.automations.migrate import migrate_loops
from durin.automations.spec import Delivery, Help, Life
from durin.automations.store import automations_dir, load_automation
from durin.cron.service import CronService
from durin.cron.types import CronJob, CronPayload, CronSchedule


def loops_dir(workspace: Path) -> Path:
    return Path(workspace) / "loops"


def _guard_support_tickets_dict():
    return {
        "name": "guard-support-tickets",
        "workflow": "support-ticket-guard",
        "enabled": True,
        "goal": {
            "intent": "every open support ticket gets a same-day human reply",
            "checks": [
                {"kind": "script", "required": True, "command": "check_ticket_sla.sh"},
            ],
            "checks_sufficient": True,
        },
        "triggers": [
            {
                "source": "channel",
                "channel": "email",
                "filters": {"subject_contains": "ticket"},
                "match": "always_new",
                "correlate": r"ticket-(\d+)",
            },
        ],
        "concurrency": "parallel",
        "stuck_after": 5,
        "operator_channel": "slack",
        "operator_to": "#support-ops",
    }


def _save_loop(tmp_path, data: dict):
    """Write a loop definition exactly as the deleted loops package's
    store.save_loop would have (one JSON file per loop, keyed by name) —
    every fixture dict below is already the canonical on-disk shape
    (loop_to_dict's own output shape), so no parse/serialize round-trip
    is needed to reproduce it."""
    d = loops_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{data['name']}.json").write_text(json.dumps(data), encoding="utf-8")


def _cron_loop_dict(name="nightly-brief", workflow="nightly-brief-wf"):
    return {
        "name": name,
        "workflow": workflow,
        "goal": {"intent": "a fresh briefing lands every morning"},
        "triggers": [
            {"source": "cron", "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}},
        ],
    }


def test_guard_support_tickets_shaped_loop_converts_with_documented_mapping(tmp_path):
    _save_loop(tmp_path, _guard_support_tickets_dict())

    warnings: list[str] = []
    sink = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        actions = migrate_loops(tmp_path)
    finally:
        logger.remove(sink)

    spec = load_automation(tmp_path, "guard-support-tickets")
    assert spec.name == "guard-support-tickets"
    assert spec.workflow == "support-ticket-guard"
    assert spec.enabled is True
    assert spec.concurrency == "parallel"

    assert len(spec.triggers) == 1
    trig = spec.triggers[0]
    assert trig.source == "channel"
    assert trig.channel == "email"
    assert trig.filters == {"subject_contains": "ticket"}
    assert trig.match == "always_new"
    assert trig.correlate == r"ticket-(\d+)"
    assert trig.semantic is None

    assert spec.delivery == Delivery(channel="slack", to="#support-ops", notify="failures_only")
    assert spec.help == Help(channel="slack", to="#support-ops")
    assert spec.life == Life(
        intent="every open support ticket gets a same-day human reply",
        achieved_when="any_completed",
        max_attempts=5,
        on_stuck="notify",
    )

    # goal checks are dropped with a logged warning naming the check and
    # stating the exit-0-labeler equivalent — never a "binary gate" fix.
    assert any("check_ticket_sla.sh" in w for w in warnings)
    assert any("exit-0 labeler" in w for w in warnings)
    assert any("cases" in w for w in warnings)
    assert any("ACHIEVED" in w and "NOT_ACHIEVED" in w for w in warnings)
    assert any("support-ticket-guard" in w for w in warnings)
    assert not any("binary gate" in w for w in warnings)

    assert any("guard-support-tickets" in a and "migrated" in a for a in actions)
    assert any("exit-0 labeler" in a for a in actions)


def test_cron_trigger_synthesizes_schedule_task_and_logs(tmp_path):
    _save_loop(tmp_path, _cron_loop_dict())

    logs: list[str] = []
    sink = logger.add(lambda message: logs.append(str(message)), level="INFO")
    try:
        actions = migrate_loops(tmp_path)
    finally:
        logger.remove(sink)

    spec = load_automation(tmp_path, "nightly-brief")
    assert len(spec.triggers) == 1
    trig = spec.triggers[0]
    assert trig.source == "schedule"
    assert trig.schedule == {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}
    assert trig.task == "Run the nightly-brief-wf workflow"

    assert any("nightly-brief" in line and "Run the nightly-brief-wf workflow" in line for line in logs)
    assert any("nightly-brief" in a and "Run the nightly-brief-wf workflow" in a for a in actions)


def test_runs_dir_moved_and_old_status_vocabulary_tolerated(tmp_path):
    _save_loop(tmp_path, _guard_support_tickets_dict())

    old_runs_dir = tmp_path / "loops-runs" / "guard-support-tickets"
    old_runs_dir.mkdir(parents=True)
    record = {
        "schema": 1, "run_id": "r1", "loop": "guard-support-tickets",
        "status": "no_goal",  # loops-only status, absent from every automations status tuple
        "started_at": 111.0, "finished_at": 222.0,
    }
    (old_runs_dir / "r1.json").write_text(json.dumps(record), encoding="utf-8")

    migrate_loops(tmp_path)

    new_run_file = tmp_path / "automations-runs" / "guard-support-tickets" / "r1.json"
    assert new_run_file.exists()
    assert not old_runs_dir.exists()
    assert json.loads(new_run_file.read_text(encoding="utf-8")) == record

    # The point of the move: automations' run_log reads must not choke on a
    # status vocabulary it doesn't know about.
    assert run_log.read_run(tmp_path, "guard-support-tickets", "r1") == record
    listed = run_log.list_runs(tmp_path, "guard-support-tickets")
    assert listed == [record]
    assert run_log.list_all_runs(tmp_path) == [record]
    assert run_log.active_runs(tmp_path, "guard-support-tickets") == []
    assert run_log.consecutive_unachieved(tmp_path, "guard-support-tickets") == 0


def test_claims_moved(tmp_path):
    _save_loop(tmp_path, _guard_support_tickets_dict())
    loops_claims = {"digest:abc": {"loop": "guard-support-tickets", "run_id": "r1", "registered_at": 1.0}}
    (loops_dir(tmp_path) / "claims.json").write_text(json.dumps(loops_claims), encoding="utf-8")

    errors: list[str] = []
    sink = logger.add(lambda message: errors.append(str(message)), level="ERROR")
    try:
        actions = migrate_loops(tmp_path)
    finally:
        logger.remove(sink)

    dst = automations_dir(tmp_path) / "claims.json"
    assert dst.exists()
    assert json.loads(dst.read_text(encoding="utf-8")) == loops_claims
    assert not (tmp_path / "loops-migrated" / "claims.json").exists()

    # claims.json sits beside the loop definitions but is not one — it must
    # never be glob-matched into the definition-conversion loop and reported
    # as an unparseable loop file.
    assert errors == []
    assert not any("claims" in a and "skipped" in a for a in actions)


def test_claims_already_exists_branch_leaves_and_logs(tmp_path):
    _save_loop(tmp_path, _guard_support_tickets_dict())
    automations_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    existing = {"digest:existing": {"automation": "other", "run_id": "r9", "registered_at": 2.0}}
    (automations_dir(tmp_path) / "claims.json").write_text(json.dumps(existing), encoding="utf-8")
    loops_claims = {"digest:abc": {"loop": "guard-support-tickets", "run_id": "r1", "registered_at": 1.0}}
    (loops_dir(tmp_path) / "claims.json").write_text(json.dumps(loops_claims), encoding="utf-8")

    actions = migrate_loops(tmp_path)

    # untouched — the pre-existing automations claims file wins
    assert json.loads((automations_dir(tmp_path) / "claims.json").read_text(encoding="utf-8")) == existing
    # left in place, relocated only by the whole-dir rename
    migrated = tmp_path / "loops-migrated" / "claims.json"
    assert migrated.exists()
    assert json.loads(migrated.read_text(encoding="utf-8")) == loops_claims
    assert any("claims.json already exists" in a for a in actions)


def test_queue_moved(tmp_path):
    _save_loop(tmp_path, _guard_support_tickets_dict())
    queue_dir = loops_dir(tmp_path) / "queue"
    queue_dir.mkdir(parents=True)
    (queue_dir / "guard-support-tickets.jsonl").write_text(
        json.dumps({"queued_at": 1.0, "text": "hi"}) + "\n", encoding="utf-8"
    )

    migrate_loops(tmp_path)

    dst = automations_dir(tmp_path) / "queue" / "guard-support-tickets.jsonl"
    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == json.dumps({"queued_at": 1.0, "text": "hi"}) + "\n"
    assert not (tmp_path / "loops-migrated" / "queue").exists()


def test_loops_dir_renamed_to_loops_migrated(tmp_path):
    _save_loop(tmp_path, _guard_support_tickets_dict())

    migrate_loops(tmp_path)

    assert not loops_dir(tmp_path).exists()
    migrated = tmp_path / "loops-migrated"
    assert migrated.is_dir()
    assert (migrated / "guard-support-tickets.json").exists()


def test_cron_jobs_pruned_when_service_given(tmp_path):
    _save_loop(tmp_path, _guard_support_tickets_dict())
    cron = CronService(tmp_path / "cron" / "jobs.json")
    loop_job = CronJob(
        id="loop:guard-support-tickets:0",
        name="loop guard-support-tickets trigger 0",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(kind="loop_trigger", loop="guard-support-tickets"),
    )
    cron.register_system_job(loop_job)
    other_job = cron.add_job("reminder", CronSchedule(kind="every", every_ms=60_000), "ping me")

    actions = migrate_loops(tmp_path, cron_service=cron)

    assert cron.get_job("loop:guard-support-tickets:0") is None
    assert cron.get_job(other_job.id) is not None
    assert any("loop:guard-support-tickets:0" in a for a in actions)


def test_cron_jobs_skipped_and_logged_when_no_service(tmp_path):
    _save_loop(tmp_path, _guard_support_tickets_dict())

    logs: list[str] = []
    sink = logger.add(lambda message: logs.append(str(message)), level="INFO")
    try:
        actions = migrate_loops(tmp_path, cron_service=None)
    finally:
        logger.remove(sink)

    assert any("cron" in a.lower() and "skip" in a.lower() for a in actions)
    assert any("cron" in line.lower() for line in logs)


def test_second_call_is_noop(tmp_path):
    _save_loop(tmp_path, _guard_support_tickets_dict())

    first = migrate_loops(tmp_path)
    assert first != []

    second = migrate_loops(tmp_path)
    assert second == []


def test_no_loops_dir_is_noop(tmp_path):
    assert migrate_loops(tmp_path) == []


def test_malformed_loop_json_skipped_rest_convert(tmp_path):
    _save_loop(tmp_path, _guard_support_tickets_dict())
    d = loops_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.json").write_text("{not valid json", encoding="utf-8")

    errors: list[str] = []
    sink = logger.add(lambda message: errors.append(str(message)), level="ERROR")
    try:
        actions = migrate_loops(tmp_path)
    finally:
        logger.remove(sink)

    # the malformed file is skipped and logged, never fatal to the rest
    assert load_automation(tmp_path, "guard-support-tickets").name == "guard-support-tickets"
    assert not (automations_dir(tmp_path) / "broken.json").exists()
    assert any("broken" in e for e in errors)
    assert any("broken" in a for a in actions)
