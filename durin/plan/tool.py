"""Tools that the LLM calls to interact with the plan system.

NOTE: The canonical tool definitions live in durin/agent/tools/plan.py
(auto-discovered by the ToolLoader). This module is kept for backward
compatibility with any code that imports directly from durin.plan.tool.
"""

from durin.agent.tools.plan import SetExecutionModeTool, UpdatePlanTool  # noqa: F401
