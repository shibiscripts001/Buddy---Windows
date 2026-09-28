#!/usr/bin/env python3
"""
Audio Assistant - an easier alternative to Resolve's Fairlight page, for a clip's
volume, fades, a volume line to keyframe, noise reduction and voice clean-up. Only the
page itself so far: its tools come later.

What Resolve 21.1's scripting can reach (tried on a duplicate timeline): a clip's
volume, pan and pitch (SetProperties), fades in frames (SetFades), audio transitions
(AddTransition, category "audio"), loudness normalisation to a target
(Timeline.NormalizeAudioLevel - EBU R128, YouTube, Netflix, ...), a track's Voice
Isolation, and a clip's Voice Isolation and Dialogue Leveler - on the active timeline,
and not for every clip: a camera MXF's read None and refused, an MP4's and MP3's worked.
Not reachable: volume keyframes and Fairlight FX (de-esser, EQ, noise reduction), so
those would be Buddy's own processing, baked into a new audio file.
"""

import os

from core.web_page import WebToolPage


class AudioAssistantPage(WebToolPage):
    tool_id = "audio_assistant"
    display_name = "Audio Assistant"
    category = "Editing Tools"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
