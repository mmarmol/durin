#!/usr/bin/env python3
"""Read-only reader-compatibility audit: parse every run manifest (through the
production `run_log.read_manifest` reader) and every work folder's provenance
(through `provenance.load`); report crashes and the reusable-artifact count.

This is NOT a migration gate: a nonzero `pre_existing_reusable` count is the
expected, healthy state once reuse is live in production, not a failure — it
is printed as information only. Exit status reflects crashes alone: 0 when
every manifest and work folder read cleanly, 1 when a reader crashed on
something (a genuine incompatibility).
Usage: python scripts/audit_run_compat.py /path/to/workspace [--json]"""
import json
import sys
from pathlib import Path
from durin.workflow import run_log
from durin.workflow.provenance import load


def audit(workspace_path):
    """Audit workspace run manifests and work folders.

    Returns tuple: (manifests_count, crashes_list, work_folders_count, pre_existing_reusable_count)
    """
    ws = Path(workspace_path)
    manifests_dir = ws / "workflows-runs"

    # Manifest audit — routed through the public run_log.read_manifest reader
    # (the same one production code uses), not a raw json.loads: a raw parse
    # would not catch a reader-specific incompatibility. The raw json.loads
    # fallback below only fires when the public reader returns None, purely to
    # name what actually went wrong for the crash report (read_manifest itself
    # swallows its own read/parse errors).
    manifests = []
    if manifests_dir.exists():
        manifests = sorted(manifests_dir.glob("*/*.json"))

    crashes = []
    reusable = 0
    total = 0

    for m in manifests:
        if m.name.startswith("."):
            continue
        total += 1
        d = run_log.read_manifest(ws, m.parent.name, m.stem)
        if d is None:
            try:
                json.loads(m.read_text())
            except Exception as e:  # noqa: BLE001
                crashes.append((str(m), repr(e)))
            continue
        _ = d.get("spec_hash"), d.get("durin_version")  # absent on old runs: fine
        for r in d.get("runs") or []:
            _ = r.get("model"), r.get("node_hash")

    # Work folder audit
    work_folders = 0
    workflow_dir = ws / ".workflow"
    if workflow_dir.exists():
        try:
            for wf in workflow_dir.iterdir():
                work = wf / "work"
                if work.is_dir():
                    work_folders += 1
                    for name, entry in load(work).items():
                        if entry.get("node_hash") and entry.get("model"):
                            reusable += 1
        except Exception as e:  # noqa: BLE001
            crashes.append((str(workflow_dir), repr(e)))

    return total, crashes, work_folders, reusable


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/audit_run_compat.py /path/to/workspace [--json]")
        sys.exit(1)

    workspace = sys.argv[1]
    json_output = "--json" in sys.argv

    total, crashes, work_folders, reusable = audit(workspace)

    # Print summary
    summary = f"manifests={total} crashes={len(crashes)} work_folders={work_folders} pre_existing_reusable={reusable}"
    print(summary)

    # Print crash details (unless --json)
    if not json_output:
        for path, error in crashes:
            print(f"CRASH {path} {error}")

    # Emit JSON if requested
    if json_output:
        result = {
            "manifests": total,
            "crashes": len(crashes),
            "work_folders": work_folders,
            "pre_existing_reusable": reusable,
        }
        print(json.dumps(result))

    # Exit status reflects crashes only — pre_existing_reusable is information,
    # not a gate: once reuse ships, a nonzero count is the expected healthy state.
    if crashes:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
