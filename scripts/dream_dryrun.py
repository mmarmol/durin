#!/usr/bin/env python
"""Phase 0.3 — Dream dry-run manual.

Valida la asunción A2/A3 (doc 19): un LLM puede producir consolidación
markdown coherente + commit message estructurado a partir de N entries
episódicas sobre una entidad.

Usa openclaw-aule como corpus fuente (ver doc 19 §13 Fuente A). Extrae
entries que mencionen una entidad target, las pasa por un dream prompt,
y guarda el output para inspección manual.

Iteración: ajustar PROMPT_TEMPLATE abajo y re-correr hasta que el output
sea satisfactorio. Versión final → durin/templates/dream/consolidator.md.

Usage:
    .venv/bin/python scripts/dream_dryrun.py --entity person:marcelo
    .venv/bin/python scripts/dream_dryrun.py --entity project:mxhero
    .venv/bin/python scripts/dream_dryrun.py --entity topic:helpjuice
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from durin.security.secrets import get_secret_store

CORPUS_DIRS = [
    Path("/Users/marcelo/git/openclaw-aule/workspace/memory"),
    Path("/Users/marcelo/git/openclaw-aule/workspace-elrond/memory"),
]


PROMPT_TEMPLATE = """Eres durin, asistente con sistema de memoria entity-centric.

Tu tarea: tomar N observaciones episódicas sobre la entidad `{entity_id}`
y producir DOS outputs:

1. **Página markdown consolidada** para la entidad. Schema:
   - Frontmatter YAML con: `type`, `name`, `aliases` (array de variantes textuales), `identifiers` (dict opcional con claves email/phone/slack/github/jira si aplica — sólo cuando aparecen en las entries), `dream_processed_through`, `created_at`, `updated_at`.
   - Cuerpo: secciones markdown libres (## Current state, ## History, ## Background, etc.) según el contenido.
   - Si hay contradicciones temporales, marcar en prosa: "previously X / now Y" o "until <fecha> X, since <fecha> Y".
   - NO claims YAML estructurados — todo en prosa natural.
   - Linkear sources en el cuerpo o en sección "## Sources" al final.
   - Para `type: person`: extraer agresivamente identifiers (emails, phones, slack IDs, github users) — son críticos para desempate cross-system.

2. **Commit message** que explique la consolidación. Schema:
   - Subject line: `Consolidate {entity_id} (rev N)` (asume rev 1 para esta primera consolidación).
   - Cuerpo en lenguaje natural explicando QUÉ se consolidó y POR QUÉ.
   - Trailers estructurados al final:
     - `Sources: <list of episodic ids>`
     - `Entities-touched: {entity_id}`
     - `Entities-referenced: <other entities mentioned>`
     - `Dream-session: <timestamp>`
     - `Cursor-before: 0`
     - `Cursor-after: <msg_idx of last entry processed>`

Output FORMATO ESTRICTO:

```
===PAGE===
<contenido markdown de la página, incluyendo frontmatter>
===COMMIT===
<contenido del commit message, subject + body + trailers>
===END===
```

---

ENTIDAD A CONSOLIDAR: `{entity_id}`

OBSERVACIONES EPISÓDICAS ({n_entries} entries):

{entries_text}

---

Produce los dos outputs en el formato indicado arriba. Sé conciso pero
preserva los facts importantes."""


def extract_candidates(text: str) -> list[str]:
    """Extrae líneas que empiezan con `- Candidate:` del archivo openclaw."""
    candidates = []
    for line in text.split("\n"):
        m = re.match(r"^\s*-\s*Candidate:\s*(.+)$", line)
        if m:
            candidates.append(m.group(1).strip())
    return candidates


def entries_for_entity(entity_id: str, max_entries: int = 50) -> list[dict]:
    """Extrae entries del corpus openclaw-aule que mencionen la entidad.

    Devuelve lista de dicts con keys: id, ts, text.
    """
    _, slug = entity_id.split(":", 1)
    # Compilar pattern de match (case-insensitive sobre nombre + variantes).
    if entity_id == "person:marcelo":
        pattern = re.compile(r"\b(Marcelo|marcelo)\b", re.IGNORECASE)
    elif entity_id == "project:mxhero":
        pattern = re.compile(r"\b(mxHero|mxhero|mx[\s_-]?Hero)\b", re.IGNORECASE)
    elif entity_id == "project:openclaw":
        pattern = re.compile(r"\b(openclaw|OpenClaw)\b", re.IGNORECASE)
    elif entity_id == "topic:helpjuice":
        pattern = re.compile(r"\b(helpjuice|Helpjuice|help[\s_-]?juice)\b", re.IGNORECASE)
    elif entity_id == "topic:slack-routing":
        pattern = re.compile(r"\b(slack.{0,5}thread|thread.{0,5}rout|slack.{0,8}rout)\b", re.IGNORECASE)
    else:
        # Genérico: usar slug
        pattern = re.compile(re.escape(slug), re.IGNORECASE)

    entries = []
    for corpus_dir in CORPUS_DIRS:
        if not corpus_dir.exists():
            continue
        for md_file in sorted(corpus_dir.glob("2026-*.md")):
            content = md_file.read_text(encoding="utf-8", errors="ignore")
            ts_match = re.match(r"^(\d{4}-\d{2}-\d{2})", md_file.stem)
            ts = ts_match.group(1) if ts_match else md_file.stem
            for i, candidate in enumerate(extract_candidates(content)):
                if pattern.search(candidate):
                    entries.append({
                        "id": f"{md_file.stem}-{i:03d}",
                        "ts": ts,
                        "text": candidate[:600],  # truncar entries muy largas
                    })
                    if len(entries) >= max_entries:
                        return entries
    return entries


def format_entries_for_prompt(entries: list[dict]) -> str:
    """Formatear entries como texto plano numerado."""
    lines = []
    for entry in entries:
        lines.append(f"- [{entry['ts']} / {entry['id']}] {entry['text']}")
    return "\n".join(lines)


def invoke_glm(prompt: str, model: str = "glm-5.1") -> tuple[str, float]:
    """Invocar glm-5.1 via zhipu (OpenAI-compatible). Devuelve (output, elapsed_s)."""
    store = get_secret_store()
    entry = store.get("ZHIPU_API_KEY")
    if entry is None:
        raise RuntimeError("ZHIPU_API_KEY no encontrada en secret store")
    api_key = entry.value
    import litellm
    t0 = datetime.utcnow()
    response = litellm.completion(
        model=f"openai/{model}",
        messages=[{"role": "user", "content": prompt}],
        api_key=api_key,
        api_base="https://api.z.ai/api/coding/paas/v4",
        temperature=0.1,
    )
    elapsed = (datetime.utcnow() - t0).total_seconds()
    return response.choices[0].message.content, elapsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entity", required=True,
                        help="Entity ID, ej: person:marcelo, project:mxhero")
    parser.add_argument("--max-entries", type=int, default=30)
    parser.add_argument("--model", default="glm-5.1")
    parser.add_argument("--output-dir", default="docs/research")
    args = parser.parse_args()

    print(f"=== Phase 0.3 — Dream dry-run ===")
    print(f"Entity:       {args.entity}")
    print(f"Model:        {args.model}")
    print(f"Max entries:  {args.max_entries}")
    print()

    # Extract entries
    print("Extracting entries from openclaw-aule corpus…")
    entries = entries_for_entity(args.entity, max_entries=args.max_entries)
    print(f"Found {len(entries)} entries.")
    if not entries:
        print("ERROR: no entries found.")
        return 1
    print()

    # Build prompt
    entries_text = format_entries_for_prompt(entries)
    prompt = PROMPT_TEMPLATE.format(
        entity_id=args.entity,
        n_entries=len(entries),
        entries_text=entries_text,
    )
    print(f"Prompt size: {len(prompt)} chars / ~{len(prompt) // 4} tokens approx")
    print(f"\nInvoking {args.model}…")
    try:
        output, elapsed = invoke_glm(prompt, model=args.model)
    except Exception as exc:
        print(f"ERROR invoking LLM: {exc}")
        return 1
    print(f"Done in {elapsed:.1f}s. Output: {len(output)} chars / ~{len(output) // 4} tokens.")

    # Save
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_entity = args.entity.replace(":", "_")
    out_file = out_dir / f"dream_dryrun_{safe_entity}.md"

    with open(out_file, "w") as f:
        f.write(f"# Dream dry-run — {args.entity}\n\n")
        f.write(f"**Date**: {datetime.utcnow().isoformat()}Z\n")
        f.write(f"**Model**: {args.model}\n")
        f.write(f"**Entries**: {len(entries)}\n")
        f.write(f"**Prompt size**: {len(prompt)} chars\n")
        f.write(f"**Output size**: {len(output)} chars\n")
        f.write(f"**Elapsed**: {elapsed:.1f}s\n\n")
        f.write("## LLM output\n\n")
        f.write(output)
        f.write("\n\n---\n\n## Prompt (for reference)\n\n```\n")
        f.write(prompt)
        f.write("\n```\n")

    print(f"\nSaved: {out_file}")
    print("\n=== Output preview ===\n")
    print(output[:1500])
    if len(output) > 1500:
        print(f"\n... ({len(output) - 1500} more chars saved)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
