"""Deterministic security scan for skills (§8.C). Body-first. Honest finite
recall — feeds the human gate, not a guarantee. v2 = LLM-judge semantic layer.
Rule set curated from SkillSpector (Apache-2.0) / Prompt-Shield / AgentShield /
SkillSieve. Ported SkillSpector categories: data_exfiltration, privilege_escalation,
excessive_agency, tool_misuse (regex) + the AST behavioral pass (skill_ast).

Layering / multilingual contract: the natural-language regex rules below
(prompt_injection / hidden_instructions) are a FAST ENGLISH PRE-FILTER, not the
recall layer. Non-English or paraphrased injection is caught by the LLM judge
(durin/security/skill_judge.py), which runs on durin's own models and is the
multilingual / semantic recall layer. Do NOT add per-language NL token lists here
— extend the judge instead (project rule: heuristic detectors must not be
English-only)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from durin.agent.skills_frontmatter import split_frontmatter
from durin.security.osv import query_malware

_SEV = {"info": 0, "caution": 1, "high": 2, "dangerous": 3}

# install-spec kind -> OSV ecosystem. Unmapped kinds (brew/apt/download) skip OSV.
_OSV_ECOSYSTEM = {"pip": "PyPI", "uv": "PyPI", "npm": "npm", "node": "npm",
                  "cargo": "crates.io", "go": "Go"}
_VERDICT = {0: "safe", 1: "caution", 2: "dangerous", 3: "dangerous"}  # high+ -> dangerous


@dataclass
class Finding:
    category: str
    severity: str   # info|caution|high|dangerous
    where: str
    detail: str


@dataclass
class ScanReport:
    findings: list[Finding] = field(default_factory=list)
    tools: list = field(default_factory=list)
    judge_verdict: str | None = None

    @property
    def verdict(self) -> str:
        if self.judge_verdict:
            return self.judge_verdict
        if not self.findings:
            return "safe"
        return _VERDICT[max(_SEV[f.severity] for f in self.findings)]


# Invisible / direction-spoofing codepoints (ASCII-source via \u escapes):
#   U+200B-U+200F zero-width + RTL/LTR marks
#   U+202A-U+202E bidi embeddings + overrides (incl. RLO smuggling)
#   U+2060-U+2064 word-joiner / invisible operators
#   U+FEFF        BOM / zero-width no-break space
#   U+E0000-U+E007F Unicode Tags block (steganographic instruction smuggling)
_UNICODE_RE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\U000e0000-\U000e007f]"
)

# Rules are (regex, category, severity, detail), grouped per category so adding
# a category is a localized change (layout mirrors SkillSpector's static_patterns_*).
# `_BODY_RULES` (run against SKILL.md) and `_CODE_RULES` (run against bundled
# scripts) are composed from these groups; scan_skill consumes the composed lists.

# --- body categories (SKILL.md) ---
PROMPT_INJECTION_RULES = [
    (r"(?i)ignore\s+(all\s+|the\s+)?(previous|prior|above)\s+instructions", "prompt_injection", "dangerous", "ignore-previous-instructions"),
    (r"(?i)\byou\s+are\s+now\b|\bdisregard\s+(your|the|all)\s+(system|safety|previous)|\bact\s+as\s+(a\s+)?(dan|jailbroken|unrestricted)", "prompt_injection", "dangerous", "role-override/jailbreak"),
    (r"(?i)do\s+not\s+(tell|inform|notify|mention\s+to)\s+the\s+user", "prompt_injection", "high", "covert-action directive"),
]
HIDDEN_INSTRUCTION_RULES = [
    # Hidden instruction in an HTML comment — only when it ADDRESSES the model
    # (ai/assistant/claude/llm near an imperative) or contains an injection
    # phrase. A bare "ignore"/"run" in a comment (e.g. `ascii-guard-ignore`, a
    # build note) is NOT flagged — that was a false-positive source on real skills.
    (r"(?is)<!--.*?(?:\b(?:ai|assistant|claude|llm)\b.*?\b(?:ignore|run|exec|execute|delete|send|post|fetch|disregard)\b|\bignore\s+(?:all\s+|the\s+|prior\s+|previous\s+|above\s+)*instructions|\byou\s+are\s+now\b|\bdisregard\s+(?:all|previous|the)\b).*?-->", "hidden_instructions", "high", "AI-directed instruction inside HTML comment"),
]
SENSITIVE_PATH_RULES = [
    # A *mention* of a sensitive path (e.g. SSH-key setup docs) is caution, not
    # dangerous — legit setup skills reference ~/.ssh. Real theft is the path
    # read INSIDE a script combined with exfil (caught by dangerous_code).
    (r"~/\.ssh\b|~/\.aws/credentials|~/\.aws\b|~/\.gnupg\b|~/\.env\b|/etc/passwd|/etc/shadow", "sensitive_path", "caution", "sensitive path reference"),
]
SECRET_RULES = [
    (r"AKIA[0-9A-Z]{16}|\bsk-[A-Za-z0-9]{20,}|\bghp_[A-Za-z0-9]{36}|-----BEGIN [A-Z ]*PRIVATE KEY-----", "secrets", "caution", "hardcoded secret"),
]

# --- code categories (bundled scripts) ---
DANGEROUS_CODE_RULES = [
    (r"(?:curl|wget)\s+[^\n|]*\|\s*(?:ba)?sh", "dangerous_code", "dangerous", "fetch-and-execute (curl|bash)"),
    (r"\brm\s+-rf?\s+[~/]|\bmkfs\b|\bdd\s+if=", "dangerous_code", "dangerous", "destructive command"),
    (r"\beval\s*\(|\bexec\s*\(|\bos\.system\s*\(|subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True", "dangerous_code", "dangerous", "dynamic/shell exec"),
    (r"/dev/tcp/|\bnc\s+-[a-z]*e|\bncat\s+-[a-z]*e", "dangerous_code", "dangerous", "reverse shell"),
    (r"(?is)os\.dup2\s*\([^\n]*\.fileno\s*\(", "dangerous_code", "dangerous", "fd dup to socket (reverse shell)"),
    (r"(?i)\bpty\.spawn\s*\(", "dangerous_code", "dangerous", "pty.spawn (reverse shell primitive)"),
    (r"\bos\.environ\b|\bprocess\.env\b", "dangerous_code", "caution", "environment access (exfil-adjacent)"),
    (r"\batob\s*\(|\bbase64\.b64decode\s*\(|(?:\\x[0-9a-fA-F]{2}){8,}", "dangerous_code", "caution", "obfuscation (base64/hex)"),
]

DATA_EXFILTRATION_RULES = [
    # outbound POST/PUT whose payload pulls env / credentials / local files
    (r"(?is)\b(?:curl|wget)\b[^\n]*?(?:-X\s*(?:POST|PUT)|--data|-d)\b[^\n]*?(?:\$\(\s*env\b|\benv\b|/etc/passwd|~/\.aws|~/\.ssh|process\.env)", "data_exfiltration", "dangerous", "outbound POST of env/credentials"),
    # piping a sensitive read straight into a network tool
    (r"(?is)\b(?:cat|tar|zip)\b[^\n|]*?(?:~/\.ssh|~/\.aws|\.env|id_rsa)[^\n|]*\|\s*(?:curl|wget|nc|ncat)\b", "data_exfiltration", "dangerous", "sensitive read piped to network"),
    # DNS / webhook beacon of host data
    (r"(?i)\b(?:dig|nslookup)\b[^\n]*\$\(", "data_exfiltration", "caution", "DNS exfil shape"),
    # python stdlib http exfil (requests/httpx/aiohttp/urllib) carrying a secret/env read —
    # the curl/wget rules above miss the entire Python networking stack.
    (r"(?is)\b(?:requests|httpx|aiohttp|urllib)\b[^\n]*?\.(?:post|put|patch|get|urlopen|urlretrieve|request)\s*\([^\n]*?(?:\.ssh|\.aws|\.env|id_rsa|/etc/passwd|environ|getenv)", "data_exfiltration", "dangerous", "python http exfil of secrets/env"),
]

PRIVILEGE_ESCALATION_RULES = [
    (r"(?i)\bchmod\s+(?:[ugoa]*\+s|\d*[4-7]\d{3})\b", "privilege_escalation", "dangerous", "setuid/setgid bit"),
    (r"(?i)\b(?:sudo|doas)\b[^\n]*(?:/etc/sudoers|tee\s+/etc/|>>\s*/etc/)", "privilege_escalation", "dangerous", "sudoers/system-file escalation"),
    (r"(?i)/etc/sudoers(?:\.d)?\b", "privilege_escalation", "caution", "sudoers reference"),
]
EXCESSIVE_AGENCY_RULES = [
    (r"(?i)(?:~/Library/LaunchAgents|/etc/systemd/system|crontab\s+-|/etc/cron)", "excessive_agency", "high", "persistence/autostart write"),
    (r"(?i)\brm\s+-rf?\s+(?:/(?!tmp)|\$HOME|~)\S*", "excessive_agency", "dangerous", "mass deletion outside workspace"),
]
TOOL_MISUSE_RULES = [
    (r"(?i)--dangerously-skip-permissions|--no-sandbox|DISABLE_[A-Z_]*GUARD|--yes-i-really", "tool_misuse", "high", "safety/guardrail bypass flag"),
]

# Composed lists consumed by scan_skill (preserves existing behavior exactly).
_BODY_RULES = PROMPT_INJECTION_RULES + HIDDEN_INSTRUCTION_RULES + SENSITIVE_PATH_RULES + SECRET_RULES
_CODE_RULES = (DANGEROUS_CODE_RULES + DATA_EXFILTRATION_RULES
               + PRIVILEGE_ESCALATION_RULES + EXCESSIVE_AGENCY_RULES + TOOL_MISUSE_RULES)


def _apply(text: str, where: str, rules) -> list[Finding]:
    return [Finding(c, s, where, d) for rx, c, s, d in rules if re.search(rx, text)]


# Install-spec safe-pattern allowlists (OpenClaw src/agents/skills/frontmatter.ts:28-110).
# An install spec is an import vector; reject anything outside the proven-safe shapes.
_BREW_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@+._/-]*$")
_GO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~+\-/]*(?:@[A-Za-z0-9][A-Za-z0-9._~+\-/]*)?$")
_UV_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]=<>!~+,]*$")
_NPM_SCOPED = re.compile(r"^@[a-z0-9][a-z0-9._~-]*/[a-z0-9][a-z0-9._~-]*$")
_NPM_UNSCOPED = re.compile(r"^[a-z0-9][a-z0-9._~-]*$")


def _bad(v, *frags):  # any forbidden substring present, or leading dash / blank
    return any(f in v for f in frags) or v.startswith("-") or not v.strip()


def validate_install_specs(data: dict) -> list[Finding]:
    out: list[Finding] = []
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        return out
    for vendor, blob in meta.items():
        if not isinstance(blob, dict):
            continue
        specs = blob.get("install")
        if not isinstance(specs, list):
            continue
        where = f"metadata.{vendor}.install"
        for spec in specs:
            if not isinstance(spec, dict):
                out.append(Finding("install_spec", "dangerous", where, "install entry is not a mapping"))
                continue
            kind = str(spec.get("kind", ""))
            if kind in ("brew", "apt", "pip", "cargo"):
                # package-manager installs: validate the package/formula name
                # against a safe pattern (no traversal / flags). brew/apt/pip/cargo
                # are common, legit package managers a skill may declare.
                val = str(spec.get("formula") or spec.get("cask") or spec.get("package") or "")
                if _bad(val, "..", "\\", "://") or (val and not _BREW_RE.match(val)):
                    out.append(Finding("install_spec", "dangerous", where, f"unsafe {kind} package {val!r}"))
            elif kind == "go":
                val = str(spec.get("module") or "")
                if _bad(val, "..", "\\", "://") or (val and not _GO_RE.match(val)):
                    out.append(Finding("install_spec", "dangerous", where, f"unsafe go module {val!r}"))
            elif kind == "uv":
                val = str(spec.get("package") or "")
                if _bad(val, "..", "\\", "://") or (val and not _UV_RE.match(val)):
                    out.append(Finding("install_spec", "dangerous", where, f"unsafe uv package {val!r}"))
            elif kind in ("node", "npm"):
                val = str(spec.get("package") or "")
                if ("://" in val or "#" in val or ":" in val or val.startswith("-")
                        or not (_NPM_SCOPED.match(val) or _NPM_UNSCOPED.match(val.split("@")[0] or val))):
                    out.append(Finding("install_spec", "dangerous", where, f"unsafe npm spec {val!r}"))
            elif kind == "download":
                url = str(spec.get("url") or "")
                if not re.match(r"^https?://", url) or any(c.isspace() for c in url):
                    out.append(Finding("install_spec", "dangerous", where, f"unsafe download url {url!r}"))
            # Unknown kinds are NOT flagged: we can't validate what we don't
            # model, and flagging every unmodeled installer false-positives on
            # legit skills. A code-carrying skill is gated by the carries-code
            # confirm regardless.

            # Supply-chain malware lookup (OSV) for mappable language ecosystems.
            # Fail-open: a network/timeout error never blocks a scan.
            ecosystem = _OSV_ECOSYSTEM.get(kind)
            if ecosystem:
                pkg = str(spec.get("package") or spec.get("module")
                          or spec.get("formula") or "").split("@")[0].strip()
                if pkg:
                    try:
                        mal = query_malware(pkg, ecosystem)
                    except Exception:  # noqa: BLE001 — fail-open, never block a scan
                        mal = []
                    if mal:
                        out.append(Finding("supply_chain", "dangerous", where,
                                           f"package {pkg!r} has malware advisory {', '.join(mal[:3])}"))
    return out


# Skills put runnable code under scripts/ by agentskills.io convention, but an
# attacker is not bound by convention — the scan must see code WHEREVER it lives
# (root, lib/, utils/, ...). An extension allowlist (+ a shebang sniff for
# extensionless scripts) keeps the walk off data/markdown noise. SKILL.md is
# scanned separately by the body rules.
_CODE_EXTENSIONS = frozenset({
    ".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts",
    ".rb", ".pl", ".php", ".lua", ".ps1",
})


def _is_code_file(p: Path) -> bool:
    if p.suffix.lower() in _CODE_EXTENSIONS:
        return True
    if p.suffix == "":  # extensionless: sniff a shebang
        try:
            with p.open("rb") as fh:
                return fh.read(2) == b"#!"
        except OSError:
            return False
    return False


def iter_code_files(skill_dir: Path):
    """Yield code files anywhere in the skill tree (not just scripts/), skipping SKILL.md."""
    skill_dir = Path(skill_dir)
    for p in sorted(skill_dir.rglob("*")):
        if p.is_file() and p.name != "SKILL.md" and _is_code_file(p):
            yield p


def scan_skill(skill_dir: Path) -> ScanReport:
    skill_dir = Path(skill_dir)
    rep = ScanReport()
    md = skill_dir / "SKILL.md"
    if md.is_file():
        data, body = split_frontmatter(md.read_text(encoding="utf-8"))
        rep.findings += _apply(body, "SKILL.md", _BODY_RULES)
        rep.findings += validate_install_specs(data)
        if _UNICODE_RE.search(body):
            rep.findings.append(Finding("unicode_smuggling", "dangerous", "SKILL.md",
                                        "invisible/bidi/tags unicode in body"))
    for p in iter_code_files(skill_dir):
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(p.relative_to(skill_dir))
        rep.findings += _apply(txt, rel, _CODE_RULES)
        # AST behavioral pass for Python scripts. Local import breaks the
        # skill_ast <-> skill_scan import cycle (skill_ast imports Finding).
        if p.suffix == ".py":
            from durin.security.skill_ast import scan_python_ast
            rep.findings += scan_python_ast(txt, rel)
        # secrets + sensitive paths also matter inside code files
        rep.findings += _apply(txt, rel, [r for r in _BODY_RULES if r[1] in ("secrets", "sensitive_path")])
    return rep
