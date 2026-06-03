"""Deterministic security scan for skills (§8.C). Body-first. Honest finite
recall — feeds the human gate, not a guarantee. v2 = LLM-judge semantic layer.
Rule set curated from SkillSpector / Prompt-Shield / AgentShield / SkillSieve."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from durin.agent.skills_frontmatter import split_frontmatter

_SEV = {"info": 0, "caution": 1, "high": 2, "dangerous": 3}
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

    @property
    def verdict(self) -> str:
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

# (regex, category, severity, detail) — applied to the SKILL.md body
_BODY_RULES = [
    (r"(?i)ignore\s+(all\s+|the\s+)?(previous|prior|above)\s+instructions", "prompt_injection", "dangerous", "ignore-previous-instructions"),
    (r"(?i)\byou\s+are\s+now\b|\bdisregard\s+(your|the|all)\s+(system|safety|previous)|\bact\s+as\s+(a\s+)?(dan|jailbroken|unrestricted)", "prompt_injection", "dangerous", "role-override/jailbreak"),
    (r"(?i)do\s+not\s+(tell|inform|notify|mention\s+to)\s+the\s+user", "prompt_injection", "high", "covert-action directive"),
    (r"(?is)<!--.*?\b(ai|assistant|claude|ignore|run|exec|system|do not)\b.*?-->", "hidden_instructions", "high", "instruction inside HTML comment"),
    (r"~/\.ssh\b|~/\.aws/credentials|~/\.aws\b|~/\.gnupg\b|~/\.env\b|/etc/passwd|/etc/shadow", "sensitive_path", "high", "sensitive path reference"),
    (r"AKIA[0-9A-Z]{16}|\bsk-[A-Za-z0-9]{20,}|\bghp_[A-Za-z0-9]{36}|-----BEGIN [A-Z ]*PRIVATE KEY-----", "secrets", "caution", "hardcoded secret"),
]

# applied to bundled script files
_CODE_RULES = [
    (r"(?:curl|wget)\s+[^\n|]*\|\s*(?:ba)?sh", "dangerous_code", "dangerous", "fetch-and-execute (curl|bash)"),
    (r"\brm\s+-rf?\s+[~/]|\bmkfs\b|\bdd\s+if=", "dangerous_code", "dangerous", "destructive command"),
    (r"\beval\s*\(|\bexec\s*\(|\bos\.system\s*\(|subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True", "dangerous_code", "dangerous", "dynamic/shell exec"),
    (r"/dev/tcp/|\bnc\s+-[a-z]*e|\bncat\s+-[a-z]*e", "dangerous_code", "dangerous", "reverse shell"),
    (r"\bos\.environ\b|\bprocess\.env\b", "dangerous_code", "high", "environment access (exfil-adjacent)"),
    (r"\batob\s*\(|\bbase64\.b64decode\s*\(|(?:\\x[0-9a-fA-F]{2}){8,}", "dangerous_code", "caution", "obfuscation (base64/hex)"),
]


def _apply(text: str, where: str, rules) -> list[Finding]:
    return [Finding(c, s, where, d) for rx, c, s, d in rules if re.search(rx, text)]


def scan_skill(skill_dir: Path) -> ScanReport:
    skill_dir = Path(skill_dir)
    rep = ScanReport()
    md = skill_dir / "SKILL.md"
    if md.is_file():
        _, body = split_frontmatter(md.read_text(encoding="utf-8"))
        rep.findings += _apply(body, "SKILL.md", _BODY_RULES)
        if _UNICODE_RE.search(body):
            rep.findings.append(Finding("unicode_smuggling", "dangerous", "SKILL.md",
                                        "invisible/bidi/tags unicode in body"))
    scripts = skill_dir / "scripts"
    if scripts.is_dir():
        for p in sorted(scripts.rglob("*")):
            if not p.is_file():
                continue
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = str(p.relative_to(skill_dir))
            rep.findings += _apply(txt, rel, _CODE_RULES)
            # secrets + sensitive paths also matter inside scripts
            rep.findings += _apply(txt, rel, [r for r in _BODY_RULES if r[1] in ("secrets", "sensitive_path")])
    return rep
