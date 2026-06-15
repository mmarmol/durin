"""Textual widgets for the durin TUI."""

from durin.cli.tui.widgets.activity_cluster import ActivityCluster
from durin.cli.tui.widgets.chat_view import ChatView, MessageBubble
from durin.cli.tui.widgets.completions_hint import CompletionsHint
from durin.cli.tui.widgets.footer_bar import FooterBar
from durin.cli.tui.widgets.header_bar import HeaderBar
from durin.cli.tui.widgets.input_area import (
    AtFileSuggester,
    InputArea,
    MultiModeSuggester,
    SlashCommandSuggester,
)
from durin.cli.tui.widgets.tool_call_bubble import ToolCallBubble
from durin.cli.tui.widgets.working_indicator import WorkingIndicator

__all__ = [
    "ActivityCluster",
    "AtFileSuggester",
    "ChatView",
    "CompletionsHint",
    "FooterBar",
    "HeaderBar",
    "InputArea",
    "MessageBubble",
    "MultiModeSuggester",
    "SlashCommandSuggester",
    "ToolCallBubble",
    "WorkingIndicator",
]
