"""Post-hoc analysis pass over a benchmark run directory.

Reads ``traces/<qa_id>.json`` + ``telemetry/<qa_id>.jsonl`` for every
QA in a run, categorises the failures with rule-based heuristics
(zero LLM cost), writes per-failure markdown for human inspection,
and refreshes the aggregate ``summary.json``.

Keeps the harness simple: the harness only captures raw signal. This
script + its heuristics evolve independently — re-running ``analyze``
against an old benchmark folder reclassifies with the latest rules
without re-spending tokens.

Failure categories (rule-based v1):

- ``timeout``: harness hit per-QA wall-clock cap.
- ``error``: harness raised an exception.
- ``no_retrieval``: agent didn't call any memory_* tool. Only
  meaningful when the QA category is single_hop / multi_hop /
  temporal — open_domain might legitimately answer from world
  knowledge; adversarial expects "I don't know".
- ``retrieval_miss_empty``: memory_search was called but every call
  returned 0 hits.
- ``retrieval_miss_irrelevant``: memory_search returned hits but
  none mention the ground-truth answer's key tokens (cheap n-gram
  overlap check; not a semantic test).
- ``hallucination``: agent answered confidently for an adversarial
  question that has no answer.
- ``judge_error_possible``: judge confidence < 60 OR the answer and
  the expected share ≥3 distinctive tokens. Flagged for human review.
- ``synthesis_error``: tools were called and returned relevant data
  but the answer is still wrong. Multi-hop / temporal most common.
- ``unknown``: didn't match any rule. Surfaces as "needs deeper look".
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["analyze_run"]

logger = logging.getLogger(__name__)


@dataclass
class _Telemetry:
    events: list[dict[str, Any]]

    def count(self, type_prefix: str) -> int:
        return sum(1 for e in self.events if str(e.get("type", "")).startswith(type_prefix))

    def memory_search_calls(self) -> list[dict[str, Any]]:
        return [
            e.get("data") or {} for e in self.events
            if e.get("type") == "memory.recall"
        ]

    def memory_recall_vectors(self) -> list[dict[str, Any]]:
        return [
            e.get("data") or {} for e in self.events
            if e.get("type") == "memory.recall.vector"
        ]

    def cache_usage_summary(self) -> dict[str, Any]:
        events = [e.get("data") or {} for e in self.events if e.get("type") == "cache.usage"]
        if not events:
            return {"calls": 0}
        ratios = [e.get("cache_ratio_pct", 0) for e in events]
        return {
            "calls": len(events),
            "avg_cache_ratio_pct": round(sum(ratios) / len(ratios), 1),
        }


def analyze_run(run_dir: Path) -> dict[str, Any]:
    """Walk the run dir, categorise failures, write markdowns, return summary.

    Idempotent — running twice produces the same output. Existing
    failure markdowns are overwritten.
    """
    run_dir = Path(run_dir)
    traces_dir = run_dir / "traces"
    telemetry_dir = run_dir / "telemetry"
    failures_dir = run_dir / "failures"
    failures_dir.mkdir(parents=True, exist_ok=True)
    # Clean stale failure files so renamed/recovered ones disappear.
    for old in failures_dir.glob("*.md"):
        old.unlink()

    by_category_scores: dict[str, list[float]] = defaultdict(list)
    failure_counts: Counter[str] = Counter()
    total = 0
    total_pass = 0
    total_judge_failed = 0

    if not traces_dir.is_dir():
        raise FileNotFoundError(f"no traces/ directory in {run_dir}")

    for trace_path in sorted(traces_dir.glob("*.json")):
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning("skipping malformed trace %s: %s", trace_path, exc)
            continue
        total += 1

        verdict = trace.get("verdict") or {}
        if not verdict:
            total_judge_failed += 1
            failure_counts["no_verdict"] += 1
            continue
        score = float(verdict.get("score", 0.0))
        by_category_scores[trace.get("category", "?")].append(score)
        if score >= 1.0:
            total_pass += 1
            continue

        # Load telemetry for this QA (best-effort).
        tel_path = telemetry_dir / f"{trace_path.stem}.jsonl"
        tel = _load_telemetry(tel_path)

        cat = _classify_failure(trace, verdict, tel)
        failure_counts[cat] += 1

        _write_failure_markdown(failures_dir / f"{trace_path.stem}.md",
                                trace, verdict, tel, cat)

    # Per-category summary
    by_category: dict[str, dict[str, Any]] = {}
    for cat, scores in by_category_scores.items():
        passes = sum(1 for s in scores if s >= 1.0)
        by_category[cat] = {
            "n": len(scores),
            "pass": passes,
            "score": round(passes / len(scores), 3) if scores else 0.0,
        }

    summary = {
        "n_total": total,
        "n_pass": total_pass,
        "n_fail": total - total_pass,
        "score": round(total_pass / total, 3) if total else 0.0,
        "judge_failed": total_judge_failed,
        "by_category": by_category,
        "failure_breakdown": dict(failure_counts.most_common()),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8",
    )
    return summary


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def _classify_failure(
    trace: dict[str, Any],
    verdict: dict[str, Any],
    tel: _Telemetry,
) -> str:
    stop = trace.get("stop_reason", "")
    if stop == "timeout":
        return "timeout"
    if trace.get("error"):
        return "error"

    # Adversarial: ground truth is "not discussed" / "no info" but agent
    # produced a concrete answer → hallucination.
    if trace.get("category") == "adversarial":
        got = (trace.get("got") or "").lower()
        if not _looks_like_refusal(got):
            return "hallucination"

    tool_calls = trace.get("tool_calls") or []
    memory_calls = [tc for tc in tool_calls if (tc.get("tool") or "").startswith("memory_")]
    recall_calls = tel.memory_search_calls()

    expected_tokens = _significant_tokens(trace.get("expected", ""))
    got_tokens = _significant_tokens(trace.get("got", ""))

    # Judge error possible: judge said wrong, but answer overlaps ≥3
    # distinctive tokens with expected. OR judge confidence < 60.
    overlap = len(expected_tokens & got_tokens)
    judge_conf = int(verdict.get("confidence", 100))
    if overlap >= 3 or judge_conf < 60:
        return "judge_error_possible"

    if not memory_calls:
        # Open-domain might legitimately answer from world knowledge.
        # Treat no_retrieval as a category-specific signal only for
        # categories that REQUIRE memory.
        if trace.get("category") in {"open_domain"}:
            return "synthesis_error"
        return "no_retrieval"

    # Recall calls fired but returned nothing.
    if recall_calls and all(int(c.get("result_count", 0)) == 0 for c in recall_calls):
        return "retrieval_miss_empty"

    # Retrieved something — does any result mention the ground-truth?
    # Cheap n-gram overlap against tool result previews.
    retrieved_text = " ".join(str(tc.get("result_preview") or "") for tc in memory_calls).lower()
    if expected_tokens and not any(t in retrieved_text for t in expected_tokens):
        return "retrieval_miss_irrelevant"

    return "synthesis_error"


_REFUSAL_HINTS = (
    "i don't know", "i don't have", "no information", "not discussed",
    "not mentioned", "no record", "cannot determine", "unable to",
    "no menciona", "no se menciona", "no aparece", "sin información",
)


def _looks_like_refusal(text: str) -> bool:
    text = text.lower()
    return any(h in text for h in _REFUSAL_HINTS)


_TOKEN_RE = re.compile(r"[A-Za-z0-9]{3,}")
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "have", "has",
    "was", "were", "are", "you", "your", "they", "them", "their", "but",
    "what", "when", "where", "why", "how", "who", "which", "did", "does",
    "yes", "not", "all", "any", "out", "into", "about", "answer",
})


def _significant_tokens(text: str) -> set[str]:
    """Lowercase tokens ≥3 chars, filter trivial English stopwords."""
    return {
        t.lower() for t in _TOKEN_RE.findall(text)
        if t.lower() not in _STOPWORDS
    }


# ---------------------------------------------------------------------------
# telemetry + markdown
# ---------------------------------------------------------------------------


def _load_telemetry(tel_path: Path) -> _Telemetry:
    if not tel_path.is_file():
        return _Telemetry(events=[])
    events: list[dict[str, Any]] = []
    try:
        for line in tel_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return _Telemetry(events=[])
    return _Telemetry(events=events)


def _write_failure_markdown(
    out_path: Path,
    trace: dict[str, Any],
    verdict: dict[str, Any],
    tel: _Telemetry,
    category: str,
) -> None:
    lines: list[str] = []
    lines.append(
        f"# {trace.get('qa_id')} — {trace.get('category')} — FAIL ({category})"
    )
    lines.append("")
    lines.append(f"## Question\n{trace.get('question', '')}")
    lines.append("")
    lines.append(f"## Expected\n{trace.get('expected', '')}")
    lines.append("")
    lines.append(f"## Got\n{trace.get('got', '') or '_(empty)_'}")
    lines.append("")
    lines.append(
        f"## Judge\n"
        f"- score: **{verdict.get('score')}** · confidence: {verdict.get('confidence')}\n"
        f"- reasoning: {verdict.get('reasoning', '')}"
    )
    lines.append("")
    lines.append(f"## Run shape")
    lines.append(f"- iterations: {trace.get('iterations')}")
    lines.append(f"- stop_reason: {trace.get('stop_reason')}")
    lines.append(f"- duration_s: {trace.get('duration_s'):.2f}")
    lines.append(f"- context_chars_final: {trace.get('context_chars_final')}")
    if trace.get("error"):
        lines.append(f"- **error**: {trace.get('error')}")
    lines.append("")

    # Telemetry summary
    cache = tel.cache_usage_summary()
    lines.append("## Telemetry summary")
    counts = Counter(e.get("type", "?") for e in tel.events)
    if counts:
        for typ, n in counts.most_common(12):
            lines.append(f"- `{typ}`: {n}")
    else:
        lines.append("- _(no events captured)_")
    if cache.get("calls"):
        lines.append(
            f"- cache: {cache['calls']} calls, avg ratio {cache.get('avg_cache_ratio_pct', '?')}%"
        )
    lines.append("")

    # Tool calls — main debugging surface
    tool_calls = trace.get("tool_calls") or []
    lines.append("## Tool calls")
    if not tool_calls:
        lines.append("_(none)_")
    else:
        for i, tc in enumerate(tool_calls, 1):
            lines.append(f"### {i}. `{tc.get('tool')}`")
            args = tc.get("args") or ""
            if len(args) > 400:
                args = args[:400] + "…"
            lines.append(f"**args**: `{args}`")
            res = tc.get("result_preview") or ""
            if len(res) > 800:
                res = res[:800] + "…"
            lines.append(f"**result**: {res}")
            lines.append("")

    # memory.recall.vector detail — reordered / ranking
    vrows = tel.memory_recall_vectors()
    if vrows:
        lines.append("## memory.recall.vector detail")
        for r in vrows[:8]:
            lines.append(
                f"- query: `{r.get('query', '')[:60]}` · "
                f"hits: {r.get('hit_count')} · "
                f"ranking: {r.get('ranking', 'default')} · "
                f"reordered: {r.get('reordered', False)}"
            )
        lines.append("")

    lines.append("---")
    lines.append(
        f"Raw trace: [`traces/{trace.get('qa_id')}.json`](../traces/{trace.get('qa_id')}.json)  "
    )
    lines.append(
        f"Raw telemetry: [`telemetry/{trace.get('qa_id')}.jsonl`](../telemetry/{trace.get('qa_id')}.jsonl)"
    )

    out_path.write_text("\n".join(lines), encoding="utf-8")
