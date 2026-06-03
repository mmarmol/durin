Update memory files based on the analysis below.
- [FILE] entries: add the described content to the appropriate file
- [FILE-REMOVE] entries: delete the corresponding content from memory files
- [SKILL] entries: create a new skill by calling skill_write

## File paths (relative to workspace root)
- SOUL.md
- USER.md
- memory/MEMORY.md
- skills/<name>/SKILL.md (for [SKILL] entries only)

Do NOT guess paths.

## Editing rules
- Edit directly — file contents provided below, no read_file needed
- Use exact text as old_text, include surrounding blank lines for unique match
- Batch changes to the same file into one edit_file call
- For deletions: section header + all bullets as old_text, new_text empty
- Surgical edits only — never rewrite entire files
- If nothing to update, stop without calling tools

## Skill creation rules (for [SKILL] entries)
- **Before authoring, look for prior art (registries):** use `skill_search` with a short phrase for the capability to get candidate hits. For a relevant hit, call `skill_acquire_seed(source=<the hit's ref>)`. If it returns a `seed`, ADAPT that body (fix names/paths, drop irrelevant parts) and pass the result as `skill_write`'s `content` — do not copy it verbatim. If it returns `{seed: null}` (needs consent or unfetchable), try another hit from the search results, or author from scratch. You never need to judge a candidate's safety — `skill_acquire_seed` only ever hands back risk-free prior art.
- Call skill_write(name, content, rationale) to create the skill — `content` is the full SKILL.md body, `rationale` records why it is worth creating (commit message). The store writes skills/<name>/SKILL.md with provenance and a commit.
- Before authoring, read_file `{{ skill_creator_path }}` for format reference (frontmatter structure, naming conventions, quality standards)
- **Dedup check**: read existing skills listed below to verify the new skill is not functionally redundant. Skip creation if an existing skill already covers the same workflow.
- Include YAML frontmatter with name and description fields
- Keep SKILL.md under 2000 words — concise and actionable
- Include: when to use, steps, output format, at least one example
- Do NOT overwrite existing skills — skip if the skill directory already exists
- Reference specific tools the agent has access to (read_file, edit_file, exec, web_search, etc.)
- Skills are instruction sets, not code — do not include implementation code

## Quality
- Every line must carry standalone value
- Concise bullets under clear headers
- When reducing (not deleting): keep essential facts, drop verbose details
- If uncertain whether to delete, keep but add "(verify currency)"
