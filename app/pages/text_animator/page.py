#!/usr/bin/env python3
"""
Animation - the page for animating things in Resolve. Its top row of tabs is the
kind of animation; there is only Text+ so far, and it's empty: the Text+ tools
(font styling, placement, word layouts, animation presets) are tabs on Transcribe,
right after the Subtitle Conversion that makes the Text+ (see text_plus.py, which
Transcribe's page hosts, and the engine modules beside it).

It was the Text Animator page, and keeps that tool id so the sidebar arrangement
and saved settings carry over.
"""

import os

from core.web_page import WebToolPage


class AnimationPage(WebToolPage):
    tool_id = "text_animator"
    display_name = "Animation"
    category = "Editing Tools"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
