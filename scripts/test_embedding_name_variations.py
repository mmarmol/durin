#!/usr/bin/env python
"""Phase 0.1 — Test embedding similarity para variaciones de nombre.

Valida la asunción A1 de doc 19: el embedding model actual
(paraphrase-multilingual-MiniLM-L12-v2) acerca razonablemente
variaciones de nombre que el alias index del L1 light esperaría
capturar — pares como `Marcelo`/`marcelo`/`Marcelito` /
`mmarmol@mxhero.com`.

Si esta asunción se sostiene, alias expansion explícito es nice-to-have.
Si no se sostiene (sim < 0.30 para email/name, o < 0.75 para
lowercase), alias expansion es bloqueante de día 1.

Output: tabla de pares con cosine similarity, evaluación pass/fail,
y verdict final por categoría.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# Permitir ejecutar desde checkout sin install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from durin.memory.embedding import FastembedProvider


# Cada par lleva categoría y threshold mínimo esperado per doc 19 §2.0.1.
PAIRS: list[tuple[str, str, str, float]] = [
    # (a, b, categoría, threshold_min_esperado)

    # Lowercase/case variations — esperado: sim > 0.85
    ("Marcelo", "marcelo", "case", 0.85),
    ("durin", "Durin", "case", 0.85),
    ("María", "maria", "case", 0.85),
    ("mxHero", "mxhero", "case", 0.85),

    # Truncamiento / variantes de nombre — esperado: sim > 0.70
    ("Marcelo Marmol", "Marcelo M.", "truncate", 0.70),
    ("Marcelo Marmol", "Marcelo", "truncate", 0.70),
    ("María García", "María", "truncate", 0.70),

    # Nicknames y diminutivos — esperado: sim > 0.50 (lower bar)
    ("Marcelo", "Marcelito", "nickname", 0.50),

    # Cross-form email vs name — esperado: sim > 0.50, fail crítico si < 0.30
    ("Marcelo Marmol", "mmarmol@mxhero.com", "email", 0.50),
    ("María García", "mgarcia@empresa.com", "email", 0.50),

    # Project slug variantes — esperado: sim > 0.70
    ("durin", "durin-agent", "project_slug", 0.70),
    ("project:durin", "durin", "project_slug", 0.70),
    ("project:durin", "Durin", "project_slug", 0.70),

    # Descripción larga vs nombre — esperado: sim > 0.40 (puede ser bajo)
    ("durin", "el proyecto durin que estamos construyendo", "desc", 0.40),

    # Multilingüe (español/inglés/cjk para validar fortaleza del model)
    ("oficina", "office", "multilang", 0.50),
    ("perro", "dog", "multilang", 0.50),
    ("会议", "meeting", "multilang", 0.50),  # 会议 = meeting/conference
    ("プロジェクト", "project", "multilang", 0.50),  # プロジェクト = project

    # Contraejemplos (deben dar BAJA similarity — sim < 0.50)
    ("Marcelo", "María", "negative_person", 0.50),
    ("durin", "hermes", "negative_project", 0.50),
    ("python", "javascript", "negative_topic", 0.50),
    ("pytest", "lunes", "negative_random", 0.50),
]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity entre dos vectores."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def main() -> int:
    print("=" * 80)
    print("Phase 0.1 — Embedding name variation test")
    print("Model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    print("=" * 80)
    print()

    # Warmup explícito; primera invocación descarga el modelo si no está.
    provider = FastembedProvider()
    print(f"Provider model: {provider.model_name}, dim={provider.dimensions}")
    print()

    # Embed cada string una sola vez (cache simple).
    unique = sorted({s for pair in PAIRS for s in pair[:2]})
    print(f"Embedding {len(unique)} unique strings…", flush=True)
    vectors = {s: v for s, v in zip(unique, provider.embed(unique))}
    print("Done.\n")

    # Resultados por par.
    results: list[tuple[str, str, str, float, float, bool]] = []
    for a, b, category, threshold in PAIRS:
        sim = cosine(vectors[a], vectors[b])
        # Para negativos, el "pass" es sim < threshold.
        if category.startswith("negative"):
            passed = sim < threshold
        else:
            passed = sim > threshold
        results.append((a, b, category, sim, threshold, passed))

    # Imprimir tabla.
    print(f"{'A':<30s} | {'B':<35s} | {'cat':<18s} | sim    | thr   | pass")
    print("-" * 110)
    for a, b, category, sim, threshold, passed in results:
        mark = "✓" if passed else "✗"
        op = "<" if category.startswith("negative") else ">"
        print(
            f"{a[:30]:<30s} | {b[:35]:<35s} | {category:<18s} | "
            f"{sim:.3f} | {op} {threshold:.2f} | {mark}"
        )

    # Resumen por categoría.
    print()
    print("=" * 80)
    print("Resumen por categoría")
    print("=" * 80)
    categories: dict[str, list[bool]] = {}
    for _, _, cat, _, _, passed in results:
        categories.setdefault(cat, []).append(passed)
    for cat in sorted(categories):
        passes = sum(categories[cat])
        total = len(categories[cat])
        status = "✓" if passes == total else "✗" if passes == 0 else "~"
        print(f"  {status} {cat:<22s}: {passes}/{total}")

    # Verdict final per doc 19.
    print()
    print("=" * 80)
    print("Verdict per doc 19 §2.0.1")
    print("=" * 80)

    # Reglas de fail crítico:
    email_pair = next(r for r in results if r[2] == "email" and "mmarmol" in r[1])
    case_pair = next(r for r in results if r[0] == "Marcelo" and r[1] == "marcelo")

    fail_critical = False

    if email_pair[3] < 0.30:
        print(f"✗ CRITICAL: email/name pair {email_pair[0]}/{email_pair[1]} = "
              f"{email_pair[3]:.3f} < 0.30")
        print("  → doc 18 §7 L1 light: alias expansion es BLOQUEANTE día 1")
        fail_critical = True
    elif email_pair[3] < 0.50:
        print(f"~ WARN: email/name pair {email_pair[0]}/{email_pair[1]} = "
              f"{email_pair[3]:.3f} < 0.50")
        print("  → alias expansion es necesario pero embedding hace algo de trabajo")

    if case_pair[3] < 0.75:
        print(f"✗ CRITICAL: case variant {case_pair[0]}/{case_pair[1]} = "
              f"{case_pair[3]:.3f} < 0.75")
        print("  → el embedding model elegido NO sirve para alias resolution; "
              "reevaluar")
        fail_critical = True

    overall_pass = sum(r[5] for r in results)
    overall_total = len(results)
    print()
    print(f"Overall: {overall_pass}/{overall_total} pairs match expected threshold")

    if not fail_critical and overall_pass / overall_total >= 0.70:
        print()
        print("✓ A1 sostiene: embeddings ayudan con variations razonablemente.")
        print("  L1 light alias expansion sigue siendo necesaria pero NO bloqueante.")
        return 0
    elif fail_critical:
        print()
        print("✗ A1 FALLA: doc 18 §7 necesita revisión.")
        return 1
    else:
        print()
        print("~ A1 marginal: revisar resultados arriba, posible ajuste de threshold.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
