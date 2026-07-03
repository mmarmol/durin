"""Helpers for WebUI chat title generation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from loguru import logger

from durin.providers.base import LLMProvider
from durin.session.manager import Session, SessionManager
from durin.session.turn_lease import session_turn_lease
from durin.utils.helpers import truncate_text

WEBUI_SESSION_METADATA_KEY = "webui"
WEBUI_TITLE_METADATA_KEY = "title"
WEBUI_TITLE_USER_EDITED_METADATA_KEY = "title_user_edited"
TITLE_MAX_CHARS = 60


def mark_webui_session(session: Session, metadata: dict[str, Any]) -> bool:
    """Persist a WebUI marker only when the inbound websocket frame opted in."""
    if metadata.get(WEBUI_SESSION_METADATA_KEY) is not True:
        return False
    session.metadata[WEBUI_SESSION_METADATA_KEY] = True
    return True


def _strip_think_block(text: str) -> str:
    """Drop an in-band ``<think>…</think>`` block (reasoning models may leak
    one before the title; an unclosed block means the output is all
    reasoning)."""
    return re.sub(r"<think>.*?(</think>|$)", "", text, flags=re.DOTALL).strip()


def clean_generated_title(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    text = _strip_think_block(text)
    text = re.sub(r"^\s*(title|标题)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    text = text.strip().strip("\"'`“”‘’")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.rstrip("。.!！?？,，;；:")
    if len(text) > TITLE_MAX_CHARS:
        text = text[: TITLE_MAX_CHARS - 1].rstrip() + "…"
    return text


# Openers that mark the output as the model talking about the task (leaked
# reasoning / meta commentary) rather than a title. A reasoning model that
# spends its whole token budget thinking produces exactly this shape.
_META_OPENERS = (
    "the user", "the conversation", "this chat", "this conversation",
    "let me", "i need", "i should", "i will", "we need", "okay", "ok,",
    "first,", "generate a", "here is", "here's", "sure", "certainly",
    "el usuario", "la conversación", "este chat", "esta conversación",
)

# A real title fits in a few words; prose does not. Word-count only bounds
# above (CJK titles have no spaces and count as one "word").
_MAX_TITLE_WORDS = 12
# Raw output far beyond the display cap is prose/reasoning, not a title —
# truncating it would persist garbage like "The user wants a concise ti…".
_MAX_RAW_CHARS = 120


def is_plausible_title(raw: str | None) -> bool:
    """True when the model output looks like an actual chat title."""
    text = _strip_think_block((raw or "").strip())
    if not text:
        return False
    if len(text) > _MAX_RAW_CHARS:
        return False
    cleaned = clean_generated_title(text)
    if not cleaned or cleaned.lower().startswith("error"):
        return False
    lowered = cleaned.lower()
    if any(lowered.startswith(opener) for opener in _META_OPENERS):
        return False
    if len(cleaned.split()) > _MAX_TITLE_WORDS:
        return False
    return True


def fallback_title_from_user_text(user_text: str) -> str:
    """Derive a title from the user's first message when generation fails."""
    first_line = next(
        (line.strip() for line in user_text.splitlines() if line.strip()), "",
    )
    return clean_generated_title(first_line)


def _title_inputs(session: Session) -> tuple[str, str]:
    user_text = ""
    assistant_text = ""
    for message in session.messages:
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "user" and not user_text:
            user_text = content.strip()
        elif role == "assistant" and not assistant_text:
            assistant_text = content.strip()
        if user_text and assistant_text:
            break
    return user_text, assistant_text


async def maybe_generate_webui_title(
    *,
    sessions: SessionManager,
    session_key: str,
    provider: LLMProvider,
    model: str,
) -> bool:
    """Generate and persist a short title for WebUI-owned sessions only.

    The LLM call runs OUTSIDE the turn lease (it can be long; holding the
    lock across a network call would block concurrent turns for seconds).
    The save runs INSIDE the lease, after a reload, so it cannot clobber a
    concurrent turn's whole-file write and cannot overwrite a title set by
    the user while the LLM was running.

    """
    # --- Pre-flight check (outside lease; uses cached/recent state) -------
    session = sessions.get_or_create(session_key)
    if session.metadata.get(WEBUI_SESSION_METADATA_KEY) is not True:
        return False
    if session.metadata.get(WEBUI_TITLE_USER_EDITED_METADATA_KEY) is True:
        return False
    current_title = session.metadata.get(WEBUI_TITLE_METADATA_KEY)
    if isinstance(current_title, str) and current_title.strip():
        # A stored auto-title that looks like leaked reasoning (e.g. "The
        # user wants a concise title for the chat. The conversati…") is
        # regenerated instead of kept forever. User-edited titles are
        # already protected by the flag above.
        if is_plausible_title(current_title):
            return False

    user_text, assistant_text = _title_inputs(session)
    if not user_text:
        return False

    prompt = (
        "Generate a concise title for this chat.\n"
        "Rules:\n"
        "- Use the same language as the user when practical.\n"
        "- 3 to 8 words.\n"
        "- No quotes.\n"
        "- No punctuation at the end.\n"
        "- Return only the title.\n\n"
        f"User: {truncate_text(user_text, 1_000)}"
    )
    if assistant_text:
        prompt += f"\nAssistant: {truncate_text(assistant_text, 1_000)}"

    # --- LLM call: runs OUTSIDE the lease --------------------------------
    # max_tokens is generous because reasoning models spend part of the
    # budget thinking in-band; a tight cap truncates mid-reasoning and the
    # "title" becomes leaked meta text. One retry, then a deterministic
    # fallback from the user's first message — a session never keeps a
    # reasoning fragment as its name.
    title = ""
    for _attempt in range(2):
        try:
            response = await provider.chat_with_retry(
                [
                    {
                        "role": "system",
                        "content": (
                            "You write short, neutral chat titles. "
                            "Return only the title text — no reasoning, "
                            "no preamble, no quotes."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                model=model,
                max_tokens=512,
                temperature=0.2,
                retry_mode="standard",
            )
        except Exception:
            logger.debug("Failed to generate webui session title for {}", session_key, exc_info=True)
            return False
        if is_plausible_title(response.content):
            title = clean_generated_title(response.content)
            break
        logger.debug(
            "webui title-gen: implausible title output for {} (attempt {}): {!r}",
            session_key, _attempt + 1, (response.content or "")[:80],
        )
    if not title:
        title = fallback_title_from_user_text(user_text)
    if not title:
        return False

    # --- Save: acquire lease, reload, re-check guards, then write --------
    # Reload is required so we commit on top of the latest disk state (not the
    # pre-LLM snapshot).  The guards are re-checked on the reloaded session so
    # a user rename that arrived while the LLM was running is respected.
    session_path = sessions._get_session_path(session_key)
    try:
        async with session_turn_lease(session_path):
            fresh = sessions.reload(session_key)
            if fresh.metadata.get(WEBUI_TITLE_USER_EDITED_METADATA_KEY) is True:
                return False
            existing = fresh.metadata.get(WEBUI_TITLE_METADATA_KEY)
            # A concurrent plausible title wins; an implausible one (leaked
            # reasoning) is exactly what this regeneration replaces.
            if isinstance(existing, str) and existing.strip() and is_plausible_title(existing):
                return False
            fresh.metadata[WEBUI_TITLE_METADATA_KEY] = title
            sessions.save(fresh)
    except TimeoutError:
        logger.debug(
            "webui title-gen: lease timeout for {}, title not saved", session_key
        )
        return False
    return True


async def maybe_generate_webui_title_after_turn(
    *,
    channel: str,
    metadata: dict[str, Any],
    sessions: SessionManager,
    session_key: str,
    provider: LLMProvider,
    model: str,
) -> bool:
    if channel != "websocket" or metadata.get(WEBUI_SESSION_METADATA_KEY) is not True:
        return False
    return await maybe_generate_webui_title(
        sessions=sessions,
        session_key=session_key,
        provider=provider,
        model=model,
    )
