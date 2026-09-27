# Absorb judge prompt — v4

> LLM judge deciding what to do with TWO entity pages that collide (they
> share an alias, or are very close in embeddings). Used by the refine pass
> `durin/memory/refine_dream.py::run_refine`.
>
> Designed adversarially: the shared alias is NECESSARY and NOT sufficient.
> The judge defaults to "different" when content evidence is weak. Includes
> timestamps on each page to mitigate self-consistency bias when
> `judge_model == dream_model`.
>
> v2: besides the verdict, the judge proposes a **resolution** — which page
> survives a merge, clearer keys, who owns a contested alias, and the typed
> relation between related pages. New verdict `related`: distinct identities
> where one is a part, version or specialization of the other.
>
> v3: adds `{relation_guide}` — the rule for telling "same" apart from
> "related", and the relation-type guide (is-a vs. composition vs. usage),
> shared word for word with the Tier-2 judge (`pair_resolution.py`). That is
> why `judge_template_fingerprint()` also hashes that text: changing it
> re-judges the cached pairs just like a template change does.
>
> v4: the template itself is now in English (it was Spanish through v3) —
> project convention is English code and docs, and this same change already
> re-judges the verdict cache once (the fingerprint now covers
> `{relation_guide}`), so translating now costs nothing extra. Meaning,
> placeholders and the envelope are unchanged; item 2's `relation` field no
> longer lists its own example labels and instead defers to
> `{relation_guide}` above it.
>
> Expected output: `===VERDICT===` (one of `same` / `different` / `related` /
> `unclear`), `===CONFIDENCE===` (integer 0-100), `===REASONING===` (1-3
> sentences), `===RESOLUTION===` (JSON object, `{}` when there is nothing to
> propose), ending with `===END===`.
>
> Variables to substitute:
> - `{shared_aliases}` — list of aliases both refs share
> - `{ref_a}`, `{ref_b}` — entity refs (e.g. `person:marcelo`)
> - `{page_a_block}`, `{page_b_block}` — each with a temporal-metadata
>   header plus the page's body
> - `{relation_guide}` — the relation guide shared with the Tier-2 judge,
>   injected verbatim at render time

---

## Template

```
You are durin, evaluating what to do with TWO entity pages that collide.

IMPORTANT: both pages share at least one alias ("{shared_aliases}") or are
very close semantically. This is NECESSARY but NOT sufficient to merge:
- Two people can both be named "Marcelo".
- Two projects can share an acronym.
- A casual alias ("admin", "user") can appear on unrelated entities.

Default to "different" when content evidence is weak. The penalty for a
false positive (an incorrect merge) is high — the information is preserved
under archive/, but the slug moves and semantic search is affected.

## Page A: {ref_a}

{page_a_block}

## Page B: {ref_b}

{page_b_block}

## Your task

1) Decide the relation between A and B, based on CONTENT (not just alias):

- same — they describe the SAME real entity. Strong signals (any one is
  enough): identifiers that match literally (email, github, slack, jira,
  phone); consistent biographical or factual detail; one page refers to the
  other as itself.
- related — they are DISTINCT entities but one is a part, version, edition,
  instance or specialization of the other (an edition and the game it
  belongs to; a specific rule and the general one that contains it). Not
  "same": merging them would lose the distinction.
- different — distinct entities with no such structural relation. Signals:
  factual contradictions; disconnected contexts; non-overlapping time
  periods; mere homonymy.
- unclear — the evidence is not enough to decide.

{relation_guide}
2) Propose the resolution (all optional; only what the content justifies):

- survivor (same only): the ref whose key is the clearest and most
  canonical.
- renames: a clearer slug or name when the current one is cryptic or
  ambiguous ("5e" → "dnd-5e"). Slug in lowercase, digits and hyphens; never
  change the type. On same, only the survivor may be renamed.
- alias_moves: for an alias that actually belongs to only ONE of the two
  (keep_on: that ref), or that is junk — OCR noise, fragments, broken
  variants — (keep_on: "none"). Legitimate homonyms (a first name two
  people share) stay on both: do not move them.
- relation (related only): {{"from": <the more specific one>, "type": <see
  the relation-type guide above>, "to": <the more general one>}}.

Answer in exactly this format (no text before or after):

===VERDICT===
same | different | related | unclear
===CONFIDENCE===
<integer 0-100 — how sure you are of your verdict>
===REASONING===
<1-3 short sentences explaining the decision and each proposed operation. Cite concrete signals seen.>
===RESOLUTION===
{{"survivor": "<ref>", "renames": {{"<ref>": {{"slug": "<slug>", "name": "<name>"}}}}, "alias_moves": [{{"alias": "<alias>", "keep_on": "<ref>|both|none"}}], "relation": {{"from": "<ref>", "type": "<type>", "to": "<ref>"}}}}
(omit keys that do not apply; {{}} if you propose nothing)
===END===
```
