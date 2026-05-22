#!/usr/bin/env python
"""Drive the durin TUI headlessly and print the rendered screen.

The agent-facing companion to ``durin.cli.tui.probe``: it shows the TUI
as plain text — no real terminal, no screenshots — so the chat view,
menus and modals can be verified directly from a shell.

Steps run in order; the final screen is always printed.

    python scripts/tui_smoke.py
    python scripts/tui_smoke.py press:ctrl+l
    python scripts/tui_smoke.py type:/help press:enter
    python scripts/tui_smoke.py --dump-each type:/ type:h type:e type:l type:p

Step grammar:  type:TEXT  |  press:KEY[,KEY]  |  pause
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running from a plain checkout, no install required.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from durin.cli.tui.app import DurinApp  # noqa: E402
from durin.cli.tui.probe import run_step, screen_text  # noqa: E402


def _parse_size(text: str) -> tuple[int, int]:
    width, _, height = text.lower().partition("x")
    return int(width), int(height)


def _dump(app: DurinApp, label: str) -> None:
    bar = "─" * 72
    print(f"\n{bar}\n  {label}\n{bar}")
    print(screen_text(app))


async def _run(args: argparse.Namespace) -> int:
    app = DurinApp(agent_loop=None)
    async with app.run_test(size=_parse_size(args.size)) as pilot:
        await pilot.pause()
        if args.dump_each:
            _dump(app, "boot")
        for index, step in enumerate(args.steps, start=1):
            try:
                await run_step(pilot, step)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            if args.dump_each:
                _dump(app, f"step {index}: {step}")
        if not args.dump_each:
            _dump(app, "final" if args.steps else "boot")
        if args.svg:
            Path(args.svg).write_text(app.export_screenshot(), encoding="utf-8")
            print(f"\n[svg screenshot written to {args.svg}]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Drive the durin TUI headlessly and print the screen.",
    )
    parser.add_argument(
        "steps",
        nargs="*",
        help="ordered steps: type:TEXT | press:KEY[,KEY] | pause",
    )
    parser.add_argument(
        "--size",
        default="100x32",
        help="terminal size as WxH (default 100x32)",
    )
    parser.add_argument(
        "--dump-each",
        action="store_true",
        help="print the screen after every step, not just at the end",
    )
    parser.add_argument(
        "--svg",
        metavar="PATH",
        help="also save a true-composite SVG screenshot",
    )
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
