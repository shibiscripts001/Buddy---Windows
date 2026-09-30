#!/usr/bin/env python3
"""
Command Center - a main tab of its own, under its own heading. Only the page
itself so far: it will hold hot key combos that run Buddy's actions.
"""

import os

from core.web_page import WebToolPage


class CommandCenterPage(WebToolPage):
    tool_id = "command_center"
    display_name = "Command Center"
    category = "Command Center"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
