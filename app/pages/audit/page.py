#!/usr/bin/env python3
"""
Audit - a main tab of its own, under its own heading. Only the page itself
so far: what it checks comes later.
"""

import os

from core.web_page import WebToolPage


class AuditPage(WebToolPage):
    tool_id = "audit"
    display_name = "Audit"
    category = "Audit"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
