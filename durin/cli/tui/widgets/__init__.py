"""Textual widgets for the durin TUI (D5.2)."""

from durin.cli.tui.widgets.chat_view import ChatView, MessageBubble
from durin.cli.tui.widgets.footer_bar import FooterBar
from durin.cli.tui.widgets.header_bar import HeaderBar
from durin.cli.tui.widgets.input_area import InputArea

__all__ = [
    "ChatView",
    "FooterBar",
    "HeaderBar",
    "InputArea",
    "MessageBubble",
]
