"""CI build script: generate durin/agent/data/mcp_catalog.json.

Run as:
    PYTHONPATH=<worktree> python scripts/build_mcp_catalog.py

Requires a GitHub token (GITHUB_TOKEN env var or gh CLI) for enrichment.
"""
from durin.agent.mcp_catalog_build import main

if __name__ == "__main__":
    main()
