"""DreamConsolidator — entity-centric consolidation via LLM.

Phase 2 vertical slice per ``docs/19_implementation_plan.md`` §4.

Pipeline:
1. Caller provides ``entity_ref`` (e.g. ``"person:marcelo"``) + list
   of episodic entries that mention it (post-cursor).
2. ``consolidate_entity()`` reads the existing entity page (if any),
   builds the prompt from
   ``durin/templates/dream/consolidator.md``, invokes the LLM, parses
   the ``===PAGE===`` and ``===COMMIT===`` sections out of the
   response.
3. ``apply()`` writes the page atomically, commits via :class:`GitRepo`
   with the LLM-generated commit message, and refreshes the
   :class:`AliasIndex` sidecar.

The LLM call is delegated to a pluggable ``llm_invoke`` callable so
the consolidator can be unit-tested with a fake. Production wires it
to litellm + the provider config from ``durin.security.secrets`` +
``durin.config``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol

from durin.memory.aliases_index import AliasIndex
from durin.memory.entity_page import EntityPage
from durin.utils.git_repo import GitRepo, NothingToCommitError

__all__ = [
    "ConsolidationResult",
    "DreamConsolidator",
    "DreamError",
    "EntryRef",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class DreamError(Exception):
    """Raised when consolidation fails (LLM bad output, IO, etc.)."""


@dataclass
class EntryRef:
    """One episodic entry passed to the consolidator."""

    id: str
    timestamp: str           # ISO-ish (date or full datetime)
    text: str
    entities: list[str] = field(default_factory=list)


@dataclass
class ConsolidationResult:
    """LLM output parsed into actionable pieces."""

    page_text: str                                 # full markdown for the entity file
    commit_subject: str
    commit_body: str
    commit_trailers: dict[str, list[str]] = field(default_factory=dict)
    raw_output: str = ""                           # original LLM response, for audit


# ---------------------------------------------------------------------------
# Pluggable LLM invocation
# ---------------------------------------------------------------------------


class LLMInvoke(Protocol):
    """Protocol for any callable that takes prompt + model → response."""

    def __call__(self, prompt: str, *, model: str) -> str: ...


def default_llm_invoke(prompt: str, *, model: str = "glm-5.1") -> str:
    """Production-default LLM invocation via litellm + zhipu coding plan.

    Reads the API key from durin's secret store. Uses the OpenAI-
    compatible adapter (``openai/<model>``) with ``api_base`` override
    pointing at ``https://api.z.ai/api/coding/paas/v4``.
    """
    # Lazy imports so import-time isn't paid by callers that pass their own.
    from durin.security.secrets import get_secret_store

    store = get_secret_store()
    entry = store.get("ZHIPU_API_KEY")
    if entry is None:
        raise DreamError("ZHIPU_API_KEY missing from secret store")
    api_key = entry.value

    import litellm

    response = litellm.completion(
        model=f"openai/{model}",
        messages=[{"role": "user", "content": prompt}],
        api_key=api_key,
        api_base="https://api.z.ai/api/coding/paas/v4",
        temperature=0.1,
    )
    return response.choices[0].message.content


# ---------------------------------------------------------------------------
# DreamConsolidator
# ---------------------------------------------------------------------------


# Same shape as scripts/dream_dryrun.py PROMPT_TEMPLATE but lives as a
# tracked artifact in durin/templates/dream/consolidator.md (Phase 0.3).
_PROMPT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "templates" / "dream" / "consolidator.md"
)


_SECTION_PAGE = re.compile(
    r"===PAGE===\s*\n(.+?)\n===COMMIT===\s*\n(.+?)(?:\n===END===|\Z)",
    re.DOTALL,
)


class DreamConsolidator:
    """Coordinates LLM-driven consolidation + persistence + index refresh.

    The class is **stateless across calls** — caller decides which
    entity to consolidate and supplies the entries. This lets us
    unit-test the consolidation logic in isolation from "what entries
    to feed it" (which lives in higher layers / future work).
    """

    def __init__(
        self,
        workspace: Path,
        *,
        model: str = "glm-5.1",
        llm_invoke: LLMInvoke | None = None,
        alias_index: AliasIndex | None = None,
        git_repo: GitRepo | None = None,
        vector_index: object | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.memory_root = self.workspace / "memory"
        self.entities_root = self.memory_root / "entities"
        self.model = model
        self._llm_invoke = llm_invoke or default_llm_invoke
        # Lazily constructed; tests can inject.
        self._alias_index = alias_index
        self._git_repo = git_repo
        # Vector index is optional — None means "don't try to index pages",
        # which is fine for tests that don't need vector search. Production
        # passes a real VectorIndex; the consolidator updates the index
        # after each successful apply() so pages become searchable.
        self._vector_index = vector_index

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def consolidate_entity(
        self,
        entity_ref: str,
        entries: list[EntryRef],
    ) -> ConsolidationResult:
        """Build prompt, call LLM, parse output. Pure (no disk writes)."""
        if not entries:
            raise DreamError(f"no entries provided for {entity_ref}")
        if ":" not in entity_ref:
            raise DreamError(
                f"entity_ref must be '<type>:<value>': {entity_ref!r}"
            )

        current_page = self._read_existing_page(entity_ref)
        prompt = self._build_prompt(entity_ref, entries, current_page)
        raw = self._llm_invoke(prompt, model=self.model)
        return self._parse_response(raw)

    def apply(
        self,
        entity_ref: str,
        result: ConsolidationResult,
    ) -> str | None:
        """Persist: write entity page, git commit, refresh alias index.

        Returns the commit SHA, or ``None`` if there were no changes
        to commit (idempotent re-run on identical content).
        """
        type_, slug = entity_ref.split(":", 1)
        page_path = self.entities_root / type_ / f"{slug}.md"

        # Idempotence: if existing page is identical, no-op early.
        if page_path.exists():
            existing_text = page_path.read_text(encoding="utf-8")
            if existing_text == result.page_text:
                logger.info("dream apply: no changes for %s", entity_ref)
                return None

        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(result.page_text, encoding="utf-8")

        repo = self._get_git_repo()
        repo.init(
            gitignore_patterns=[
                "*.lance/",
                "vectors/",
                ".aliases.json",
                ".usage.json",
                ".usage/",
                ".dream.lock",
                ".locks/",
            ]
        )
        try:
            sha = repo.commit(
                subject=result.commit_subject,
                body=result.commit_body,
                trailers={k: v for k, v in result.commit_trailers.items()},
                paths=[page_path],
                author="durin-dream",
                author_email="dream@durin.local",
            )
        except NothingToCommitError:
            sha = None

        # Refresh alias index — even on no-commit (alias_index might be
        # stale relative to file). Parse the just-written page.
        # In-memory only (per doc 23 T1.4): no save() to disk; the next
        # process boot rebuilds from disk.
        idx = self._get_alias_index()
        page = EntityPage.from_text(result.page_text)
        if page is not None:
            idx.refresh_for(page, slug=slug)
            # Vector index: only upsert if a real index was provided. We
            # don't auto-construct one because that pulls in fastembed
            # (heavy dep) — the caller decides whether vector retrieval
            # is enabled, same as memory.enabled in config.
            if self._vector_index is not None:
                try:
                    self._vector_index.upsert_entity_page(
                        entity_ref=entity_ref,
                        name=page.name,
                        aliases=list(page.aliases),
                        body=page.body,
                        path=page_path,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "dream apply: vector index upsert failed for %s: %s",
                        entity_ref, exc,
                    )
        else:
            logger.warning(
                "dream apply: wrote unparseable page for %s — alias_index not updated",
                entity_ref,
            )

        return sha

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        entity_ref: str,
        entries: list[EntryRef],
        current_page: str | None,
    ) -> str:
        """Compose the prompt from the tracked template + dynamic vars.

        Template is read fresh each call so iteration on the template
        is reflected without restarting any service.
        """
        template = self._read_prompt_template()

        entries_text_lines: list[str] = []
        for entry in entries:
            entities_str = ", ".join(entry.entities) if entry.entities else ""
            tag_suffix = f" [tags: {entities_str}]" if entities_str else ""
            entries_text_lines.append(
                f"- [{entry.timestamp} / {entry.id}]{tag_suffix} {entry.text}"
            )
        entries_text = "\n".join(entries_text_lines)

        prompt_vars = {
            "entity_id": entity_ref,
            "n_entries": str(len(entries)),
            "entries_text": entries_text,
            "current_page": current_page or "(no existing page — this is the first consolidation)",
        }

        # Simple {placeholder} substitution since the template is YAML/markdown
        # — Python's str.format would fight with literal braces in markdown.
        out = template
        for key, value in prompt_vars.items():
            out = out.replace("{" + key + "}", value)
        return out

    def _read_prompt_template(self) -> str:
        """Read the prompt template file. Falls back to an inline default."""
        try:
            text = _PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
        except OSError:
            return _INLINE_TEMPLATE_FALLBACK
        # The template doc has explanatory prose + ``` code fence with the
        # actual prompt. Extract the code block when present; otherwise
        # use the whole file.
        fence_match = re.search(r"```\s*\n(.*?)\n```", text, re.DOTALL)
        return fence_match.group(1) if fence_match else text

    def _read_existing_page(self, entity_ref: str) -> str | None:
        type_, slug = entity_ref.split(":", 1)
        path = self.entities_root / type_ / f"{slug}.md"
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    @staticmethod
    def _parse_response(raw: str) -> ConsolidationResult:
        """Extract ===PAGE=== and ===COMMIT=== sections."""
        # Some LLMs wrap the whole thing in a ```fence. Strip it if so.
        stripped = raw.strip()
        if stripped.startswith("```"):
            # Strip first fence line and trailing fence
            lines = stripped.split("\n")
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines)

        match = _SECTION_PAGE.search(stripped)
        if not match:
            raise DreamError(
                "LLM response missing ===PAGE=== / ===COMMIT=== markers"
            )
        page_text = match.group(1).strip() + "\n"
        commit_text = match.group(2).strip()

        # Split commit into subject + body + trailers.
        from durin.utils.git_repo import _split_message

        subject, body, trailers = _split_message(commit_text)
        return ConsolidationResult(
            page_text=page_text,
            commit_subject=subject,
            commit_body=body,
            commit_trailers=trailers,
            raw_output=raw,
        )

    def _get_git_repo(self) -> GitRepo:
        if self._git_repo is None:
            self._git_repo = GitRepo(
                self.memory_root,
                default_author="durin-dream",
                default_email="dream@durin.local",
            )
        return self._git_repo

    def _get_alias_index(self) -> AliasIndex:
        if self._alias_index is None:
            self._alias_index = AliasIndex(self.memory_root)
            # Rebuild from disk each boot (per doc 23 T1.4 + G14: no
            # persistent sidecar). Sub-second for typical corpus sizes.
            self._alias_index.build()
        return self._alias_index


# Fallback used when durin/templates/dream/consolidator.md is missing.
# Kept minimal — the on-disk template is the source of truth.
_INLINE_TEMPLATE_FALLBACK = """Eres durin, asistente con sistema de memoria entity-centric.

Tu tarea: tomar N observaciones episódicas sobre la entidad `{entity_id}`
y producir DOS outputs en formato:

===PAGE===
<markdown completo de la página entity, incluyendo frontmatter YAML>
===COMMIT===
<commit subject, body, trailers como Sources/Entities-touched/Cursor-after>
===END===

Entidad: {entity_id}
Página actual: {current_page}
Observaciones ({n_entries}):
{entries_text}
"""
