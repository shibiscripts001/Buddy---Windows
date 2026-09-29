"""Saved sidebar layouts keep their position when marker tools merge."""

import unittest

import _paths  # noqa: F401
from core import nav_layout


class Page:
    def __init__(self, tool_id):
        self.tool_id = tool_id


class MarkerManagerNavTests(unittest.TestCase):
    def test_old_entries_become_one_visible_tool_at_the_first_position(self):
        registry = [("Setup", Page("setup")), ("Export & Delivery", Page("marker_manager"))]
        saved = [{"type": "tool", "id": "setup", "visible": True},
                 {"type": "divider", "label": "Export & Delivery"},
                 {"type": "tool", "id": "stills_exporter", "visible": False},
                 {"type": "tool", "id": "youtube_chapters", "visible": True}]
        self.assertEqual(nav_layout.reconcile(saved, registry), [
            {"type": "tool", "id": "setup", "visible": True},
            {"type": "divider", "label": "Export & Delivery"},
            {"type": "tool", "id": "marker_manager", "visible": True},
        ])
