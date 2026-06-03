# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Unavailable skills need dependencies installed first — you can try installing them with apt/brew.

The skills above are your **most-used working set**, not the whole catalog. Skills are searchable memory: if nothing above covers the task, search (`memory_search` with `kind="skill"`) **before** proceeding or concluding that no skill exists. It returns matching procedures as `kind="skill"` hits (rendered under `=== SKILL: <name> ===`) — follow them as steps, don't cite them as facts.

If local skill search still finds nothing and the task is a **recurring or non-trivial workflow** (the kind you'd want to not reinvent next time), search the external registries with `skill_search` before reinventing it. To reuse a hit, fetch it with `skill_import(action="fetch", source=<ref>)` — that runs the security gate. If the gate clears it, adapt it into a new skill with `skill_write`. If the gate flags it (carries code, caution, or an un-allowlisted source), do **not** install it silently: present the candidates to the user with `ask_user_question` (recommended one first; say which need extra tools installed) and let them decide.

{{ skills_summary }}
