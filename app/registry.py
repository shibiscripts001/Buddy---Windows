#!/usr/bin/env python3
"""
The full list of tools Buddy will eventually hold, grouped and ordered the
way they should appear in the nav rail.

Tools still waiting to be ported are placeholders (see
pages/placeholder.py), so the complete planned layout is visible while the
rest get ported one at a time. Which ones are real is never written down
here - ToolPage.is_placeholder is the one source of truth.

Porting a tool = writing its own pages/<tool_id>/page.py (a ToolPage
subclass, see pages/base.py) and swapping its placeholder() call below
for the real import - the category/order here don't need to change.
"""

from pages.placeholder import make_placeholder_page
from pages.asset_manager.page import AssetManagerPage
from pages.audio_assistant.page import AudioAssistantPage
from pages.audit.page import AuditPage
from pages.buddy_network.page import BuddyNetworkPage
from pages.color_palette.page import ColorPalettePage
from pages.command_center.page import CommandCenterPage
from pages.dailies.page import DailiesPage
from pages.image_importer.page import ImageImporterPage
from pages.manual_chat.page import ManualChatPage
from pages.media_manager.page import MediaManagerPage
from pages.marker_manager.page import MarkerManagerPage
from pages.project_setup.page import ProjectSetupPage
from pages.svg_importer.page import SVGImporterPage
from pages.text_animator.page import AnimationPage
from pages.time_tracker.page import TimeTrackerPage
from pages.transcribe.page import TranscribePage
from pages.web.page import WebBrowserPage


def _placeholder(tool_id, display_name, category):
    return make_placeholder_page(tool_id, display_name, category)


# List of (category, page_cls), in nav order.
REGISTRY = [
    ("Ask", ManualChatPage),

    ("Audit", AuditPage),

    ("Setup", ProjectSetupPage),

    ("Media & Assets", AssetManagerPage),
    ("Media & Assets", ImageImporterPage),
    ("Media & Assets", SVGImporterPage),
    ("Media & Assets", MediaManagerPage),

    ("Editing Tools", DailiesPage),
    ("Editing Tools", AnimationPage),
    ("Editing Tools", TranscribePage),
    ("Editing Tools", AudioAssistantPage),
    ("Editing Tools", ColorPalettePage),

    ("Export & Delivery", MarkerManagerPage),

    ("Business", TimeTrackerPage),

    ("Web", WebBrowserPage),

    ("Command Center", CommandCenterPage),

    # "" = a plain line above it instead of a heading.
    ("", BuddyNetworkPage),
]
