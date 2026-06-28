---
name: skill-creator
description: >-
  Create or update AgentSkills following the full process: understand, survey peers,
  plan with the scriptability test, init, edit, verify, package. Use whenever the user
  wants to design, create, edit, improve, quality-audit (a skill's structure and
  format — not its security, which is `skill_audit`), fix, validate, test, or package a
  skill, or mentions SKILL.md, skill scripts, skill references, or a skill description
  or triggering problem — even when the user does not say 'skill' explicitly but wants
  to capture a repeatable workflow for reuse. Trigger on these intents regardless of
  the language the user writes in. TRIGGERS: create a skill, modify a skill, improve
  this skill, audit a skill's structure/quality, package the skill, the skill is not triggering, the
  skill fails, add this to the skill, turn this workflow into a skill.
---

# Skill Creator

This skill provides guidance for creating effective skills.

## About Skills

Skills are modular, self-contained packages that extend the agent's capabilities by
providing specialized knowledge, workflows, and tools. Think of them as "onboarding
guides" for specific domains or tasks—they transform the agent from a general-purpose
agent into a specialized agent equipped with procedural knowledge that no model can
fully possess.

### What Skills Provide

1. Specialized workflows - Multi-step procedures for specific domains
2. Tool integrations - Instructions for working with specific file formats or APIs
3. Domain expertise - Company-specific knowledge, schemas, business logic
4. Bundled resources - Scripts, references, and assets for complex and repetitive tasks

## Before building: is a skill even the right tool?

durin has three ways to capture a repeatable capability. Decide which fits *before*
authoring a skill — the default is **not** "a skill":

- **Code (a script)** — when the work is deterministic with closed branches (a fixed
  transform, a validation, a lookup). Prefer it wherever it applies: a script is exact,
  free to run, and cannot drift. The scriptability test below decides script-vs-prose
  *inside* a skill, but apply the same instinct first — if the whole capability is closed,
  it is a script, not a skill.
- **A workflow** — when it is a multi-step *agent* process whose value is fan-out over many
  independent items, independent verification (producer ≠ checker), or determinism across
  steps — the things one prompt handles badly. Use durin's workflow engine, not a skill;
  read the `workflows` skill. Do **not** write a skill that merely narrates a multi-step
  process that should be a workflow.
- **A skill (prose)** — when it is reusable knowledge or runtime judgment the agent applies
  itself: domain facts, conventions, a decision procedure, when-to-do-what guidance.

Closed computation → code. Orchestration with fan-out / verification / determinism →
workflow. Knowledge or judgment → skill. **These compose** — a skill may *bundle* a script
or a workflow as its mechanism (ship the definition in the skill and install it on first
use; see the `workflows` skill). What does not belong here is a prose-only skill that
merely narrates a process a workflow should run.

## Core Principles

### Concise is Key

The context window is a public good. Skills share the context window with everything
else the agent needs: system prompt, conversation history, other Skills' metadata, and
the actual user request.

**Default assumption: the agent is already very smart.** Only add context the agent
doesn't already have. Challenge each piece of information: "Does the agent really need
this explanation?" and "Does this paragraph justify its token cost?"

Prefer concise examples over verbose explanations.

### Code Over Instructions

The quality contract for every decision in this process is
[the skill rubric](references/skill-rubric.md). Its central criterion: for each
capability, ask whether the branches and inputs are closed. Closed branches mean a
script; runtime judgment means prose that explains why. The burden of proof is on
prose. Read the rubric before planning a skill's contents, and again whenever a
decision feels like style preference.

### Anatomy of a Skill

Every skill consists of a required SKILL.md file and optional bundled resources:

```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter metadata (required)
│   │   ├── name: (required)
│   │   └── description: (required)
│   └── Markdown instructions (required)
└── Bundled Resources (optional)
    ├── scripts/          - Executable code (Python/Bash/etc.)
    ├── references/       - Documentation intended to be loaded into context as needed
    └── assets/           - Files used in output (templates, icons, fonts, etc.)
```

#### SKILL.md (required)

- **Frontmatter** (YAML): `name` and `description`. These are the only fields the agent
  reads to decide when the skill gets used — the description is the skill's entire
  triggering surface (rubric block 3 covers how to design it).
- **Body** (Markdown): Instructions and guidance for using the skill. Only loaded AFTER
  the skill triggers (if at all).

The full list of accepted frontmatter keys and limits is enforced by
`scripts/quick_validate.py` — run it instead of memorizing rules. In durin, `metadata`
and `always` are also supported when needed, but avoid extra fields unless actually
required.

#### Scripts (`scripts/`)

Executable code for tasks with closed branches: deterministic, token-efficient, may be
executed without loading into context. Output and exit-code requirements are in
[the skill rubric](references/skill-rubric.md) (block 2). Scripts may still be read by
the agent for patching or environment-specific adjustments.

#### References (`references/`)

Documentation loaded as needed into context: schemas, API docs, domain knowledge,
detailed workflow guides. Keeps SKILL.md lean. Information lives in either SKILL.md or
references, never both. For files over ~100 lines, add a table of contents; for very
large files, include grep patterns in SKILL.md so the agent can search instead of
reading whole files.

#### Assets (`assets/`)

Files not meant to be loaded into context but used in the output the agent produces:
templates, images, fonts, boilerplate directories. Separates output resources from
documentation.

#### What to Not Include in a Skill

A skill should only contain files that directly support its functionality. Do NOT
create extraneous documentation or auxiliary files (README.md, INSTALLATION_GUIDE.md,
CHANGELOG.md, etc.). The skill contains what an AI agent needs to do the job — not
process notes, setup guides, or user-facing documentation.

### Progressive Disclosure Design Principle

Skills load in three levels to manage context efficiently:

1. **Metadata (name + description)** - Always in context (~100 words)
2. **SKILL.md body** - When skill triggers (budget: 500 lines, enforced as a validator
   warning)
3. **Bundled resources** - As needed (unlimited; scripts can execute without loading)

Keep SKILL.md to essentials. When splitting content out, link each file from SKILL.md
with a markdown link and say when to read it — the validator checks links resolve, and
warns about resource files the body never mentions. Splitting patterns and guidelines:
[creation-process.md](references/creation-process.md).

## Skill Creation Process

1. Understand the skill with concrete examples
2. Survey neighboring skills
3. Plan contents with the scriptability test
4. Initialize the skill (run init_skill.py)
5. Edit the skill (scripts first, then prose, then description)
6. Verify (run scripts, validate, dry-run)
7. Package and iterate

Follow these steps in order, skipping only with a clear reason.

### Skill Naming

- Use lowercase letters, digits, and hyphens only; normalize user-provided titles to
  hyphen-case (e.g., "Plan Mode" becomes `plan-mode`).
- Prefer short, verb-led phrases that describe the action.
- Namespace by tool when it improves clarity or triggering (e.g.,
  `gh-address-comments`, `linear-address-issue`).
- Name the skill folder exactly after the skill name (validator-enforced, with length
  and character rules — see `scripts/quick_validate.py`).

### Step 1: Understand the Skill with Concrete Examples

Skip this step only when the skill's usage patterns are already clearly understood.

To create an effective skill, clearly understand concrete examples of how it will be
used. These can come from direct user examples or generated examples validated with
user feedback. For an image-editor skill, relevant questions include: "What
functionality should it support?", "Can you give examples of how it would be used?",
"What would a user say that should trigger this skill?"

Avoid asking too many questions in a single message. Start with the most important and
follow up as needed. Conclude when there is a clear sense of the functionality the
skill should support.

### Step 2: Survey Neighboring Skills

List the skills already in the target catalog and read 2-3 neighbors closest in domain.
Two questions: should this be an extension of an existing skill instead of a new
sibling? And what tone/structure do peers use that this skill should match? Extending
beats creating when the new capability shares triggers with an existing skill.

### Step 3: Plan Contents with the Scriptability Test

For each concrete example from step 1, walk the scriptability cascade in
[the skill rubric](references/skill-rubric.md): closed branches mean a script,
parameterizable branches mean a script with arguments, runtime judgment means prose
that explains why. The output of this step is the resource list — which scripts, which
references, which assets — and, for every planned prose section, the judgment it
encodes that disqualifies a script.

Examples of the analysis:

1. "Help me rotate this PDF" — fixed transform, closed branches: a
   `scripts/rotate_pdf.py` belongs in the skill.
2. "Build me a dashboard" — same boilerplate every time: an `assets/` template
   directory belongs in the skill.
3. "How many users logged in today?" — same schema rediscovery every time: a
   `references/schema.md` belongs in the skill; choosing the query is judgment and
   stays prose.

### Step 4: Initialize the Skill

Skip only if the skill already exists (then continue to the next step). When creating
from scratch, always run `init_skill.py` — it generates a template directory with
everything a skill requires. Script paths below are relative to this skill's own
directory — resolve them to absolute paths when running from elsewhere:

```bash
scripts/init_skill.py my-skill --path ./workspace/skills
scripts/init_skill.py my-skill --path ./workspace/skills --resources scripts,references
scripts/init_skill.py my-skill --path ./workspace/skills --resources scripts --examples
```

For durin, custom skills live under the active workspace `skills/` directory so they
are discovered automatically at runtime (for example,
`workspace/skills/my-skill/SKILL.md`).

After initialization, customize SKILL.md and add resources. If you used `--examples`,
replace or delete placeholder files.

### Step 5: Edit the Skill

The skill is being created for another instance of the agent to use. Include what is
beneficial and non-obvious: procedural knowledge, domain-specific details, reusable
assets.

Author the skill entirely in English — name, description, body, script output, and
comments — regardless of the language the conversation happens in (rubric block 3
covers how the description stays language-agnostic for triggering).

Work in this order:

1. **Scripts first.** Implement and run each one. Output discipline is rubric block 2:
   terse by default, documented exit codes, one-line errors, compact JSON, `--verbose`
   opt-in only.
2. **Then prose.** Instructions for using the scripts, plus the judgment-bearing parts
   only. Always use imperative form. Explain why, not just what — the agent
   generalizes from reasons, not from rigid commands.
3. **Then the description**, against the trigger-query methodology in
   [the skill rubric](references/skill-rubric.md): ~10 should-trigger queries, ~10
   near-miss should-NOT-trigger queries, adjust until the boundary is right. All
   "when to use" information goes in the description, never in the body.

If user-provided materials are needed (brand assets, schemas, templates), collect them
in this step.

### Step 6: Verify

Three gates, in order:

1. Every bundled script has been executed at least once with realistic input.
2. `scripts/quick_validate.py` passes — fix warnings too unless you can say why not.
3. Dry-run: dispatch 1-2 realistic prompts to a subagent that gets only the skill —
   methodology in [creation-process.md](references/creation-process.md). If the
   subagent wrote helper code the skill does not bundle, go back to step 3: that code
   was a missing script.

### Step 7: Package and Iterate

Once development is complete, package the skill into a distributable .skill file:

```bash
scripts/package_skill.py path/to/skill-folder
scripts/package_skill.py path/to/skill-folder ./dist
```

The packaging script validates first (same checks as `quick_validate.py`, plus
packaging rules such as symlink rejection) and only creates the archive if validation
passes. The .skill file is a zip with a .skill extension, named after the skill.

After real usage, iterate: notice struggles or inefficiencies, identify how SKILL.md or
bundled resources should change, implement, and re-verify (step 6). Repeated helper
code written by skill users is the strongest signal — it marks a missing script.
