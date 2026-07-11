"""`durin workflow` — review and apply workflow self-improvement recommendations.

In manual mode the dream pass records proposed edits — a node's prompt, a script
node's inline command, or a failing script file's full content — instead of applying
them. This surfaces those recommendations and lets the user apply one: it writes the
proposed text into the definition (or the script file), versions the change, and
marks the recommendation applied.
"""

from __future__ import annotations

import typer

workflow_app = typer.Typer(help="Review and apply workflow self-improvement recommendations.")


def _workspace():
    from durin.config.loader import load_config

    return load_config().workspace_path


@workflow_app.command("recommendations")
def recommendations(
    name: str = typer.Argument(None, help="Workflow name; omit to list across all workflows."),
) -> None:
    """List open self-improvement recommendations."""
    from durin.workflow import run_log
    from durin.workflow import workflow_recommendations as wr

    workspace = _workspace()
    names = [name] if name else run_log.workflow_names_with_runs(workspace)
    found = False
    for n in names:
        for r in wr.open_recommendations(workspace, n):
            found = True
            if r.get("kind") == "structural":
                typer.echo(f"[{r['id']}] {n}: STRUCTURAL — never auto-applied; treat it in a session  (seen ×{r.get('count', 1)})")
                typer.echo(f"    reason    : {r.get('reason', '')}")
                typer.echo(f"    rejected  : {r.get('why_rejected', '')}")
                typer.echo(f"    evidence  : {r.get('diagnostic', '')}")
                continue
            if r.get("kind") == "script_file":
                typer.echo(f"[{r['id']}] {n}: script {r['script']}  (seen ×{r.get('count', 1)})")
                typer.echo(f"    reason   : {r['reason']}")
                if r.get("manual_only"):
                    typer.echo("    manual_only: never auto-applied even in auto mode")
                continue
            typer.echo(f"[{r['id']}] {n}: {r['target_id']}.{r['field']}  (seen ×{r.get('count', 1)})")
            typer.echo(f"    reason   : {r['reason']}")
            typer.echo(f"    proposed : {r['proposed'][:300]}")
    if not found:
        typer.echo("No open recommendations.")


@workflow_app.command("apply")
def apply(
    name: str = typer.Argument(..., help="Workflow name."),
    rec_id: str = typer.Argument(..., help="Recommendation id (from `recommendations`)."),
) -> None:
    """Apply a recommendation: update the definition, version it, mark it applied."""
    from durin.workflow.workflow_recommendations import apply_recommendation

    result = apply_recommendation(_workspace(), name, rec_id)
    if result.get("ok"):
        if "script" in result:
            typer.echo(f"Applied: {name} — script {result['script']} updated and versioned.")
        else:
            typer.echo(f"Applied: {name} — {result['target_id']}.{result['field']} updated and versioned.")
    else:
        typer.echo(f"Error: {result.get('error')}")
        raise typer.Exit(1)


@workflow_app.command("dismiss")
def dismiss(
    name: str = typer.Argument(..., help="Workflow name."),
    rec_id: str = typer.Argument(..., help="Recommendation id (from `recommendations`)."),
) -> None:
    """Dismiss a recommendation (terminal; an identical repeat proposal stays pinned to it)."""
    from durin.workflow.workflow_recommendations import dismiss_recommendation

    if dismiss_recommendation(_workspace(), name, rec_id):
        typer.echo(f"Dismissed: {name} [{rec_id}]")
    else:
        typer.echo("Error: no open recommendation with that id")
        raise typer.Exit(1)
