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
    "LLMInvoke",
    "LLMResponse",
    "default_llm_invoke",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class DreamError(Exception):
    """Raised when consolidation fails (LLM bad output, IO, etc.).

    ``triggered_quarantine`` is set by ``DreamConsolidator.apply`` when
    the failure was the third structural strike for this entity (i.e.
    ``record_failure`` just set ``dream_quarantine`` on the page). The
    runner reads this attribute to increment its
    ``entities_quarantined`` counter for ``memory.dream.end``
    telemetry (audit A5).
    """

    def __init__(self, *args, triggered_quarantine: bool = False) -> None:
        super().__init__(*args)
        self.triggered_quarantine = triggered_quarantine


@dataclass
class EntryRef:
    """One episodic entry passed to the consolidator."""

    id: str
    timestamp: str           # ISO-ish (date or full datetime)
    text: str
    entities: list[str] = field(default_factory=list)


@dataclass
class ConsolidationResult:
    """v2 LLM output parsed into actionable pieces.

    The LLM emits a JSON Patch + a body delta + a commit message; the
    actual page text is only known *after* the patch is applied to
    the on-disk entity page. ``page_text`` is populated by
    :meth:`DreamConsolidator.apply` after the write succeeds, so it
    reflects what actually landed on disk (post-cursor-override).

    Token counters (audit A5) sum across every LLM call made during
    ``consolidate_entity`` for this entity — that is, the initial
    call plus any parse-retry calls. The DreamRunner aggregates these
    across all entities in a pass for the ``memory.dream.end`` event.
    """

    parsed_output: Any  # ParsedDreamOutput; typed lazily to avoid cycle
    commit_subject: str
    commit_body: str
    commit_trailers: dict[str, list[str]] = field(default_factory=dict)
    raw_output: str = ""                           # original LLM response, for audit
    # A5: token accounting per consolidation. `llm_call_count` includes
    # successful parse + every parse-retry. Defaults make existing
    # call sites (e.g. tests that build a ConsolidationResult by hand)
    # compatible without churn.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_call_count: int = 0
    # G2 invariant: timestamp of the last entry sent to the LLM in this
    # batch. :meth:`DreamConsolidator.apply` forces the entity page's
    # ``dream_processed_through`` to this value (overriding whatever the
    # LLM put in ``Cursor-after``) so callers can safely batch large
    # entry sets without silent data loss.
    batch_last_ts: str | None = None
    # Populated by ``apply()`` after a successful write. Empty string
    # before apply runs (callers that need the post-apply rendering
    # call apply() first).
    page_text: str = ""

    def raw_output_commit_message(self) -> str:
        """Return the LLM-emitted commit message text (between
        ``===COMMIT===`` and ``===END===``)."""
        return getattr(self.parsed_output, "commit_message", "")


def _trailer_value(
    trailers: dict[str, list[str]], key: str,
) -> str:
    """Return the first value for *key* in *trailers*, or ``""``."""
    values = trailers.get(key) or []
    for v in values:
        if v:
            return v
    return ""


# ---------------------------------------------------------------------------
# Pluggable LLM invocation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMResponse:
    """Outcome of one LLM call: generated text + token accounting.

    Token counts are best-effort — providers that don't report usage
    leave them at 0. The Dream cost alarm (doc 08 §3 R3) computes
    `dream_llm_cost_per_day_usd` from `llm_input_tokens_total` and
    `llm_output_tokens_total` aggregated across one pass; missing
    counts under-report cost (safe-failure direction).
    """

    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMInvoke(Protocol):
    """Protocol for any callable that takes prompt + model → response.

    Returns an :class:`LLMResponse` carrying both the generated text
    and the token usage (for cost telemetry, audit A5). Implementations
    that cannot report usage should leave token counts at 0.
    """

    def __call__(self, prompt: str, *, model: str) -> LLMResponse: ...


def default_llm_invoke(prompt: str, *, model: str = "glm-5.1") -> LLMResponse:
    """Production-default LLM invocation via litellm + zhipu coding plan.

    Reads the API key from durin's secret store. Uses the OpenAI-
    compatible adapter (``openai/<model>``) with ``api_base`` override
    pointing at ``https://api.z.ai/api/coding/paas/v4``.

    Returns the generated text plus prompt/completion tokens extracted
    from ``response.usage``. Some upstream providers/proxies omit the
    usage block; in that case tokens fall back to 0 so the dream cost
    telemetry reports zero (under-report) rather than crashing.
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
    usage = getattr(response, "usage", None)
    prompt_tokens = 0
    completion_tokens = 0
    if usage is not None:
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    return LLMResponse(
        text=response.choices[0].message.content,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


# ---------------------------------------------------------------------------
# Commit-message extraction (split LLM commit string into subject/body/trailers)
# ---------------------------------------------------------------------------


_TRAILER_LINE_RE = re.compile(
    r"^([A-Z][A-Za-z0-9-]+):\s*(.*)$",
)


def _extract_commit_parts(
    commit_message: str,
) -> tuple[str, str, dict[str, list[str]]]:
    """Split an LLM-emitted commit message into subject, body, trailers.

    The convention (doc 05 §11 + commit_format.md):

        <subject — max 70 chars>

        <optional multi-paragraph body>

        Sources: foo.md, bar.md
        Cursor-after: 2026-05-26T...
        Entities-touched: person:marcelo

    Lines that match ``<Capitalized-Word>: <value>`` at the END of the
    message are trailers. Anything between the first line and the
    trailer block is the body. The first line is the subject.

    Returns ``(subject, body, trailers)`` where ``trailers`` is a
    ``{key: [values]}`` map (multi-valued for keys that legitimately
    repeat in git, though we don't expect any in practice).
    """
    text = commit_message.strip("\n")
    if not text:
        return "", "", {}
    lines = text.splitlines()
    subject = lines[0].strip()

    # Walk back from the bottom collecting trailer lines until we hit
    # a non-trailer line.
    trailers: dict[str, list[str]] = {}
    body_end = len(lines)
    for i in range(len(lines) - 1, 0, -1):
        line = lines[i].strip()
        if not line:
            # blank line — could be the separator before trailers or in
            # the body. If we've already started collecting trailers,
            # this blank line marks the body/trailer boundary; stop.
            if trailers:
                body_end = i
                break
            else:
                # blank line within the body; keep looking for trailers
                # but record this as the potential body end if we hit
                # a trailer next.
                continue
        m = _TRAILER_LINE_RE.match(line)
        if not m:
            # Non-trailer non-empty line — body content. Stop collecting.
            body_end = i + 1
            break
        key, value = m.group(1), m.group(2).strip()
        trailers.setdefault(key, []).insert(0, value)
    else:
        # Walked all the way back to line 1 (the body has only trailer
        # lines, no actual body).
        body_end = 1

    body_lines = lines[1:body_end]
    # Strip blank lines around the body.
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    body = "\n".join(body_lines)
    return subject, body, trailers


# ---------------------------------------------------------------------------
# DreamConsolidator
# ---------------------------------------------------------------------------


class DreamConsolidator:
    """Coordinates LLM-driven consolidation + persistence + index refresh.

    The class is **stateless across calls** — caller decides which
    entity to consolidate and supplies the entries. This lets us
    unit-test the consolidation logic in isolation from "what entries
    to feed it" (which lives in higher layers / future work).

    v2 (Phase 1.9): prompt + parse goes through the new pipeline
    (``dream_prompt_builder`` + ``dream_patch_parser``) and apply
    delegates to ``dream_apply.apply_dream_output`` +
    ``dream_archive_consumed.archive_consumed_episodic`` +
    ``dream_quarantine`` for failure bookkeeping + ``dream_commit_message``
    for the canonical trailer block.
    """

    # G11: input budget per call. 50 entries × ~100 tokens = ~5000
    # input tokens; plus prompt (~2000) + current_page (~1500) ≈ 8500
    # tokens total. Well within 32K+ context windows. Take *newest*
    # entries when capping (assumes caller passes them in any order;
    # we sort by timestamp before slicing).
    MAX_ENTRIES_PER_CALL = 50

    # G10/retries: 3 attempts on parse failure with feedback-in-prompt.
    MAX_RETRIES = 3

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
        """Build prompt, call LLM, parse v2 output. Pure (no disk writes).

        Per doc 05 + doc 23 §9:
        - G11: caps entries at ``MAX_ENTRIES_PER_CALL`` (newest first
          by timestamp).
        - G2: tags the result with ``batch_last_ts`` so :meth:`apply`
          can force the cursor to the last entry of the batch,
          overriding whatever the LLM put in ``Cursor-after`` (prevents
          silent data loss when N > cap).
        - G10/retry: retries up to ``MAX_RETRIES`` on parse failure,
          feeding the error back into the prompt.
        """
        from durin.memory.dream_patch_parser import (
            DreamPatchParseError,
            parse_dream_output,
        )

        if not entries:
            raise DreamError(f"no entries provided for {entity_ref}")
        if ":" not in entity_ref:
            raise DreamError(
                f"entity_ref must be '<type>:<value>': {entity_ref!r}"
            )

        # G11: cap entries — take the newest by timestamp.
        if len(entries) > self.MAX_ENTRIES_PER_CALL:
            logger.info(
                "dream: capping %s from %d to %d entries (newest first)",
                entity_ref, len(entries), self.MAX_ENTRIES_PER_CALL,
            )
            entries = sorted(entries, key=lambda e: e.timestamp)[
                -self.MAX_ENTRIES_PER_CALL:
            ]
        # G2: remember the last ts of the actual batch we're sending.
        batch_last_ts = entries[-1].timestamp if entries else None

        current_page = self._read_existing_page(entity_ref)

        last_error: str | None = None
        # A5: accumulate tokens across retries — the prompt is sent
        # again on each parse-retry, so cost compounds with retries.
        total_prompt_tokens = 0
        total_completion_tokens = 0
        call_count = 0
        for attempt in range(self.MAX_RETRIES):
            prompt = self._build_prompt(entity_ref, entries, current_page)
            if last_error is not None:
                prompt += (
                    f"\n\n[Previous attempt failed with error: {last_error}]\n"
                    "Please produce a strictly-formatted response with "
                    "===PATCH===, ===BODY_DELTA===, ===COMMIT===, "
                    "===END=== markers and a JSON array of patch ops "
                    "(each carrying a `provenance` field)."
                )
            response = self._llm_invoke(prompt, model=self.model)
            call_count += 1
            # Tolerate legacy llm_invoke implementations that return a
            # bare str (pre-A5 protocol). The dataclass case is the
            # normal one; the str fallback is a compat shim for
            # third-party callers that haven't migrated.
            if isinstance(response, LLMResponse):
                raw = response.text
                total_prompt_tokens += response.prompt_tokens
                total_completion_tokens += response.completion_tokens
            else:
                raw = str(response)
            try:
                parsed = parse_dream_output(raw)
            except DreamPatchParseError as exc:
                last_error = str(exc)
                logger.warning(
                    "dream consolidate attempt %d/%d failed: %s",
                    attempt + 1, self.MAX_RETRIES, exc,
                )
                continue

            subject, body, trailers = _extract_commit_parts(
                parsed.commit_message,
            )
            return ConsolidationResult(
                parsed_output=parsed,
                commit_subject=subject,
                commit_body=body,
                commit_trailers=trailers,
                raw_output=raw,
                batch_last_ts=batch_last_ts,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                llm_call_count=call_count,
            )

        raise DreamError(
            f"dream failed after {self.MAX_RETRIES} attempts: {last_error}"
        )

    def apply(
        self,
        entity_ref: str,
        result: ConsolidationResult,
        *,
        trigger: str = "manual",
    ) -> str | None:
        """Persist: apply patch, archive consumed, commit, refresh indices.

        v2 flow (doc 05 §6 + d4-d10):

          1. Ensure a placeholder entity page exists on disk so the
             applier has something to mutate.
          2. ``apply_dream_output`` validates ops, copies to
             ``.md.bak``, applies the JSON Patch, appends the body
             delta, and re-renders the page atomically. On structural
             failure the file is rolled back and a typed
             ``DreamApplyResult`` flows back here.
          3. On success: record provenance, archive consumed episodic
             entries, clear the quarantine counter, and commit the
             resulting page to ``memory/.git/`` with the canonical
             trailer block.
          4. On structural failure: increment the quarantine counter
             via :func:`record_failure` (only structural kinds count
             per doc 05 §12.5) and re-raise as :class:`DreamError`
             so the caller knows this entity skipped.

        G2 invariant (doc 05 §6.1): after the patch is applied, force
        the on-disk page's ``dream_processed_through`` to
        ``result.batch_last_ts`` — the timestamp of the latest entry
        the LLM was *given*, not whatever ``Cursor-after`` the LLM
        emitted. Defends against silent data loss when the LLM
        processed only a subset of a multi-entry batch.

        Returns the commit SHA, or ``None`` when nothing changed
        (idempotent re-run on identical content).
        """
        from durin.memory.dream_apply import (
            DreamApplyError,
            DreamApplyFailureKind,
            apply_dream_output,
        )
        from durin.memory.dream_archive_consumed import (
            archive_consumed_episodic,
        )
        from durin.memory.dream_commit_message import (
            CommitTrailers,
            finalize_commit_message,
        )
        from durin.memory.dream_quarantine import (
            clear_failures,
            record_failure,
        )

        type_, slug = entity_ref.split(":", 1)
        page_path = self.entities_root / type_ / f"{slug}.md"

        # 1) Ensure a target page exists for the applier to mutate.
        # First Dream pass for a brand-new entity hits a placeholder
        # we create here.
        if not page_path.exists():
            placeholder = EntityPage(
                type=type_, name=slug.replace("_", " ").title() or slug,
                aliases=[],
            )
            placeholder.save(page_path)

        # 2) Apply the patch + body delta atomically.
        cursor_for_telemetry = (
            result.batch_last_ts or _trailer_value(
                result.commit_trailers, "Cursor-after",
            )
            or ""
        )
        apply_result = apply_dream_output(
            workspace=self.workspace,
            entity_ref=entity_ref,
            parsed=result.parsed_output,
            trigger=trigger,
            cursor_after=cursor_for_telemetry,
        )

        if apply_result.failure_kind is not None:
            # Structural failures increment the quarantine counter. A5:
            # propagate whether THIS failure crossed the 3-strike
            # threshold so the runner can count quarantined entities.
            triggered = False
            try:
                page = EntityPage.from_file(page_path)
                if page is not None and apply_result.failure_kind in {
                    DreamApplyFailureKind.VALIDATION,
                    DreamApplyFailureKind.PATCH_RUNTIME,
                    DreamApplyFailureKind.ROUND_TRIP,
                }:
                    triggered = record_failure(page, apply_result.failure_kind)
                    page.save(page_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "dream apply: could not record quarantine for %s: %s",
                    entity_ref, exc,
                )
            raise DreamError(
                f"apply_dream_output failed ({apply_result.failure_kind.value}): "
                f"{apply_result.error_message}",
                triggered_quarantine=triggered,
            )

        # 3) G2 — force cursor to batch_last_ts AFTER the patch has
        # applied. Bumps `dream_processed_through` + clears the
        # quarantine counter (success resets per §12.5).
        page = EntityPage.from_file(page_path)
        if page is None:
            # apply_dream_output's round-trip guard makes this very
            # unlikely; defensive raise.
            raise DreamError(
                f"page unreadable after apply for {entity_ref}"
            )
        if (
            result.batch_last_ts is not None
            and page.dream_processed_through != result.batch_last_ts
        ):
            page.dream_processed_through = result.batch_last_ts
        clear_failures(page)
        page.save(page_path)
        # Stash the post-apply page text back into the result so
        # callers + tests can inspect what landed.
        result.page_text = page_path.read_text(encoding="utf-8")

        # 4) Commit to git with the canonical trailer block.
        repo = self._get_git_repo()
        repo.init(
            gitignore_patterns=[
                "*.lance/", "vectors/", ".aliases.json",
                ".usage.json", ".usage/", ".dream.lock", ".locks/",
            ]
        )
        # Build the final commit message via the hybrid module so
        # Trigger+Run-id always land + missing LLM trailers are
        # backfilled from runner state (doc 05 §11).
        import uuid
        sources_list = result.commit_trailers.get("Sources") or []
        sources = [
            s.strip() for s in ",".join(sources_list).split(",")
            if s.strip()
        ]
        if not sources:
            # Backfill from parsed patch provenance — guarantees the
            # trailer is informative even when the LLM omitted it.
            seen: set[str] = set()
            for op in (result.parsed_output.patch_ops or []):
                prov = op.get("provenance") if isinstance(op, dict) else None
                if isinstance(prov, str) and prov not in seen:
                    seen.add(prov)
                    sources.append(prov)
        trailers = CommitTrailers(
            sources=sources,
            cursor_after=(
                result.batch_last_ts
                or _trailer_value(result.commit_trailers, "Cursor-after")
                or ""
            ),
            entities_touched=entity_ref,
            trigger=trigger,
            run_id=str(uuid.uuid4()),
        )
        final_commit = finalize_commit_message(
            result.raw_output_commit_message(),
            trailers=trailers,
        )

        # `repo.commit` expects subject/body/trailers split. Re-parse
        # what we just rendered so the runner's trailers are
        # authoritative.
        subj, body_text, trailer_dict = _extract_commit_parts(final_commit)
        try:
            sha = repo.commit(
                subject=subj,
                body=body_text,
                trailers={k: v for k, v in trailer_dict.items()},
                paths=[page_path],
                author="durin-dream",
                author_email="dream@durin.local",
            )
        except NothingToCommitError:
            sha = None

        # 5) Archive consumed episodic entries + drop their vector rows.
        archive_consumed_episodic(
            workspace=self.workspace,
            entity_ref=entity_ref,
            parsed=result.parsed_output,
            vector_index=self._vector_index,
        )

        # 6) Refresh alias index + vector index for the entity page.
        idx = self._get_alias_index()
        if page is not None:
            idx.refresh_for(page, slug=slug)
            if self._vector_index is not None:
                try:
                    self._vector_index.upsert_entity_page(
                        entity_ref=entity_ref,
                        name=page.name,
                        aliases=list(page.aliases),
                        body=page.body,
                        path=page_path,
                        attributes=dict(page.attributes),
                        relations=list(page.relations),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "dream apply: vector index upsert failed for %s: %s",
                        entity_ref, exc,
                    )

        # 7) Re-index FTS5 for the entity page (doc 02 §6.2). E5:
        # `trigger="dream_apply"` so capacity dashboards can split
        # this burst from the steady watcher stream.
        try:
            from durin.memory.indexer import reindex_one_file
            reindex_one_file(
                self.workspace, page_path, trigger="dream_apply",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "dream apply: FTS reindex failed for %s: %s",
                entity_ref, exc,
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

        Delegates to :func:`durin.memory.dream_prompt_builder.build_dream_prompt`
        which assembles the multi-file v2 prompt package (consolidator
        template + rules + commit_format + json_patch_reference + 6
        examples) and fills the input slots.

        The legacy ``_read_prompt_template`` + inline-fence extraction
        is no longer used — the v2 template is the prompt as-is (no
        wrapping fence) and the builder reads all auxiliary files
        directly from ``durin/templates/dream/``.

        Inputs that are not yet wired by the runner
        (``existing_attribute_keys``, ``existing_relation_types``,
        ``existing_uris``, ``recent_history``) are passed as empty —
        Phase 1 deliverables 9 and 10 will populate them from disk +
        ``git log``.
        """
        from durin.memory.dream_prompt_builder import (
            DreamPromptContext,
            build_dream_prompt,
        )

        entries_lines: list[str] = []
        for entry in entries:
            entities_str = ", ".join(entry.entities) if entry.entities else ""
            tag_suffix = f" [tags: {entities_str}]" if entities_str else ""
            entries_lines.append(
                f"[{entry.timestamp} / {entry.id}]{tag_suffix} {entry.text}"
            )

        ctx = DreamPromptContext(
            entity_id=entity_ref,
            existing_page_content=(
                current_page
                or "(no existing page — this is the first consolidation)"
            ),
            existing_attribute_keys=(),
            existing_relation_types=(),
            existing_uris=(),
            recent_history="",
            entries=tuple(entries_lines),
        )
        return build_dream_prompt(ctx)


    def _read_existing_page(self, entity_ref: str) -> str | None:
        type_, slug = entity_ref.split(":", 1)
        path = self.entities_root / type_ / f"{slug}.md"
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _get_git_repo(self) -> GitRepo:
        if self._git_repo is None:
            self._git_repo = GitRepo(
                self.memory_root,
                default_author="durin-dream",
                default_email="dream@durin.local",
            )
        return self._git_repo

    def _get_alias_index(self) -> AliasIndex:
        # Injected index (tests) takes precedence; otherwise resolve the
        # workspace-shared instance from durin.memory.aliases_cache
        # (doc 25 §2.C). The shared map is mutated in place by
        # refresh_for / remove during apply(), so memory_search and
        # EntityAbsorption see this dream's writes immediately without
        # explicit invalidation.
        if self._alias_index is not None:
            return self._alias_index
        from durin.memory.aliases_cache import get_shared_alias_index

        return get_shared_alias_index(self.memory_root)


