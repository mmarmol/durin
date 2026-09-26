"""The message an automation's help destination gets when a run needs a person.

It is posted into a channel like every other message durin writes there, so it
is in English like them, and the replies it offers are words the reply parser
(``parse_approval_reply``) accepts.
"""

from __future__ import annotations

import pytest

from durin.cli.commands import _automation_help_body
from durin.workflow.approval import parse_approval_reply

_SPANISH = ("Aprobación", "Pregunta", "Escalada", "Respondé", "aprobar", "rechazar", "escribí")


def test_an_approval_shows_the_ask_and_the_proposal_and_how_to_reply():
    body = _automation_help_body(
        "invoice-reminder", "approval", "Send this reminder?", "Dear Ana, invoice 1042 is due.")

    assert body.splitlines()[0] == "🔒 Approval pending — invoice-reminder"
    assert "Send this reminder?" in body and "Dear Ana, invoice 1042 is due." in body
    assert body.endswith(
        "Reply in this thread: approve · reject · or write the correction.")
    assert parse_approval_reply("approve") == "approve"
    assert parse_approval_reply("reject") == "reject"


def test_a_question_reads_as_a_question():
    assert _automation_help_body("triage", "question", "Which mailbox?", None) == (
        "❓ Question — triage\nWhich mailbox?")


def test_an_escalation_is_never_rendered_as_a_question():
    assert _automation_help_body("triage", "escalation", "Stuck after 5 attempts.", None) == (
        "⚠️ Escalation — triage\nStuck after 5 attempts.")


@pytest.mark.parametrize("kind", ["approval", "question", "escalation"])
def test_no_spanish_is_left(kind):
    body = _automation_help_body("a", kind, "text", "proposal")

    assert not any(word in body for word in _SPANISH)
