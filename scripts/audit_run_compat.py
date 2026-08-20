#!/usr/bin/env python3
"""Read-only compatibility audit: parse every run manifest and work folder
with the new readers; report crashes, reusability classification, anomalies.
Usage: python scripts/audit_run_compat.py /path/to/workspace [--json]"""
import json
import sys
from pathlib import Path
from durin.workflow.provenance import load


def audit(workspace_path, json_output=False):
    """Audit workspace run manifests and work folders.

    Returns tuple: (manifests_count, crashes_list, work_folders_count, pre_existing_reusable_count)
    """
    ws = Path(workspace_path)
    manifests_dir = ws / "workflows-runs"

    # Manifest audit
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
        try:
            d = json.loads(m.read_text())
            _ = d.get("spec_hash"), d.get("durin_version")  # absent on old runs: fine
            for r in d.get("runs") or []:
                _ = r.get("model"), r.get("node_hash")
        except Exception as e:  # noqa: BLE001
            crashes.append((str(m), repr(e)))

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

    total, crashes, work_folders, reusable = audit(workspace, json_output)

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

    # Exit with status
    if crashes or reusable != 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
