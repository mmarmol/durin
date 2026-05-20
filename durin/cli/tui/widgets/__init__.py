"""Textual widgets for the durin TUI."""

from durin.cli.tui.widgets.chat_view import ChatView, MessageBubble
from durin.cli.tui.widgets.footer_bar import FooterBar
from durin.cli.tui.widgets.header_bar import HeaderBar
from durin.cli.tui.widgets.input_area import (
    AtFileSuggester,
    InputArea,
    MultiModeSuggester,
    SlashCommandSuggester,
)

__all__ = [
    "AtFileSuggester",
    "ChatView",
    "FooterBar",
    "HeaderBar",
    "InputArea",
    "MessageBubble",
    "MultiModeSuggester",
    "SlashCommandSuggester",
]
