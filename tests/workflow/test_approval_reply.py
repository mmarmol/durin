"""Vocabulary for interpreting an approval-pause reply as approve/reject/revise."""

import pytest

from durin.workflow.approval import parse_approval_reply


@pytest.mark.parametrize("text,expected", [
    ("aprobar", "approve"), ("Approve", "approve"), ("APROBAR.", "approve"),
    ("ok", "approve"), ("sí", "approve"), ("si", "approve"), ("yes", "approve"),
    ("rechazar", "reject"), ("reject", "reject"), ("no", "reject"),
    ("sacale el monto del asunto", None), ("aprobar pero cambia X", None), ("", None),
])
def test_parse_approval_reply(text, expected):
    assert parse_approval_reply(text) == expected
