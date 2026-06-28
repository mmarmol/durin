"""The stdlib->loguru bridge guarantees durin's own ``logging.getLogger``
records reach loguru's sinks (and therefore gateway.log), instead of
vanishing into stdlib's last-resort stderr handler.

These tests exercise the real behaviour — a durin-named stdlib logger
emits, and we assert the record lands in a loguru ``serialize=True`` sink
that mimics gateway.log — rather than comparing strings.
"""
from __future__ import annotations

import json
import logging

from loguru import logger

from durin.utils.logging_bridge import redirect_durin_logging, redirect_lib_logging


def _read_jsonl(path) -> list[dict]:
    logger.complete()
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def _gateway_sink(path):
    """Add a JSONL sink mirroring gateway.log; returns the sink id."""
    return logger.add(
        str(path),
        serialize=True,
        level="INFO",
        filter=lambda record: record["extra"].setdefault("channel", "-") or True,
    )


def test_durin_stdlib_info_reaches_loguru_sink(tmp_path):
    sink_file = tmp_path / "gateway.log"
    sink_id = _gateway_sink(sink_file)
    try:
        redirect_durin_logging()
        logging.getLogger("durin.memory.absorption").info("absorb-info-marker")
    finally:
        logger.remove(sink_id)

    messages = [r["record"]["message"] for r in _read_jsonl(sink_file)]
    assert any("absorb-info-marker" in m for m in messages), messages


def test_durin_stdlib_warning_reaches_loguru_sink(tmp_path):
    sink_file = tmp_path / "gateway.log"
    sink_id = _gateway_sink(sink_file)
    try:
        redirect_durin_logging()
        logging.getLogger("durin.security.skill_judge").warning("judge-warn-marker")
    finally:
        logger.remove(sink_id)

    messages = [r["record"]["message"] for r in _read_jsonl(sink_file)]
    assert any("judge-warn-marker" in m for m in messages), messages


def test_record_name_labels_submodule(tmp_path):
    """The root/durin bridge labels each record with its own logger name,
    not a single fixed library tag, so submodules stay distinguishable."""
    sink_file = tmp_path / "gateway.log"
    sink_id = _gateway_sink(sink_file)
    try:
        redirect_durin_logging()
        logging.getLogger("durin.memory.indexer").info("indexer-marker")
    finally:
        logger.remove(sink_id)

    messages = [r["record"]["message"] for r in _read_jsonl(sink_file)]
    assert any("[durin.memory.indexer]" in m for m in messages), messages


def test_redirect_durin_is_idempotent(tmp_path):
    """Calling twice must not stack duplicate bridge handlers (which would
    double every record)."""
    redirect_durin_logging()
    redirect_durin_logging()
    bridges = [
        h for h in logging.getLogger("durin").handlers
        if h.__class__.__name__ == "_LoguruBridge"
    ]
    assert len(bridges) == 1

    sink_file = tmp_path / "gateway.log"
    sink_id = _gateway_sink(sink_file)
    try:
        logging.getLogger("durin.telemetry.logger").info("dup-check-marker")
    finally:
        logger.remove(sink_id)

    messages = [r["record"]["message"] for r in _read_jsonl(sink_file)]
    assert sum("dup-check-marker" in m for m in messages) == 1, messages


def test_named_lib_redirect_keeps_fixed_label(tmp_path):
    """The existing per-library redirect (nio/botpy/...) still tags records
    with the fixed library name, unaffected by the record-name fallback."""
    sink_file = tmp_path / "gateway.log"
    sink_id = _gateway_sink(sink_file)
    try:
        redirect_lib_logging("nio", level="WARNING")
        logging.getLogger("nio").warning("nio-warn-marker")
    finally:
        logger.remove(sink_id)
        # Reset the nio logger so the fixed-label handler doesn't leak into
        # other tests.
        logging.getLogger("nio").handlers = []
        logging.getLogger("nio").propagate = True

    messages = [r["record"]["message"] for r in _read_jsonl(sink_file)]
    assert any("[nio] nio-warn-marker" in m for m in messages), messages
