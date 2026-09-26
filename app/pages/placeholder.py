#!/usr/bin/env python3
"""Stand-in page for a tool that hasn't been ported into the shell yet -
shows up in the nav so the full planned layout is visible, but its
content just says so instead of pretending to work."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout

from pages.base import ToolPage


def make_placeholder_page(tool_id, display_name, category):
    class PlaceholderPage(ToolPage):
        pass

    PlaceholderPage.is_placeholder = True
    PlaceholderPage.tool_id = tool_id
    PlaceholderPage.display_name = display_name
    PlaceholderPage.category = category

    def build_ui(self):
        layout = QVBoxLayout(self)
        label = QLabel(f"{display_name}\n\nNot yet integrated into Buddy.")
        label.setAlignment(Qt.AlignCenter)
        label.setObjectName("subLabel")
        layout.addWidget(label)

    PlaceholderPage.build_ui = build_ui
    return PlaceholderPage
