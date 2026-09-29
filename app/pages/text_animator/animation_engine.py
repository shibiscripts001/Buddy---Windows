"""The Text+ animations: whole-clip presets keyframed on the TextPlus tool itself,
and letter-by-letter ones on Fusion's text Follower modifier."""
import sys
import traceback
from typing import Any, Dict, List, Optional, Tuple


class FusionAnimationEngine:
    """Keyframes Text+ animations. Per-letter ones never split the text into clips or
    tools: the Follower animates each letter of one Text+ (see LETTER_PRESETS)."""

    # An UNSET Style is not the same as "Regular" to Fusion: its Inspector shows "--" for
    # Style, and leaving Style untouched on a freshly-created/never-styled TextPlus tool
    # lets Fusion compute its OWN default at render time, which is not necessarily
    # "Regular" (it can pick "Light" for Arial, a weight Arial doesn't actually ship,
    # rendering "Font Not Found: Arial Light"). See apply_text_style()'s `font_style` docs
    # below for when this fallback applies.
    _DEFAULT_STYLE_WHEN_UNSET = "Regular"

    @staticmethod
    def apply_text_style(
        text_tool: Any,
        font_name: Optional[str] = None,
        font_size: Optional[float] = None,
        color: Optional[Tuple[float, float, float]] = None,
        font_style: Optional[str] = None,
    ) -> Tuple[bool, List[str]]:
        """Applies Font, Size, Red1/Green1/Blue1 color, and (optionally) Style to a TextPlus
        tool's inputs.

        Defensive per-field: a failure setting one field doesn't block the others. Returns
        (True if at least one field was set successfully, log_messages).

        Applying a Font must not silently reset whatever Regular/Bold/Italic/Light weight the
        clip's Text+ was already using. A "Dump Selected Node Settings" capture of a
        Cascadia Code/Light clip shows the Template tool's own Inputs list "Font":
        "Cascadia Code" and "Style": "Light" as two separate, sibling top-level string inputs.

        `font_style` controls how Style is handled when `font_name` is also given:
          - `font_style=None` (the default): PRESERVE whatever Style the tool already had -
            read BEFORE changing Font (some hosts reset a family-specific Style the instant
            Font changes, since the old style name may not exist in the new family) and
            written back immediately after, mirroring `_delete_connected_tool()`'s "read old
            state before the write that might disturb it" discipline elsewhere in this file.
            Used by the general "Apply Font Style" flow, so switching fonts on a clip the user
            deliberately set to Bold/Italic in Fusion doesn't silently reset it. If nothing
            was actually set (GetInput('Style') comes back empty/falsy - Fusion's own
            Inspector displays "--" for this case), falls back to
            `_DEFAULT_STYLE_WHEN_UNSET` ("Regular") rather than leaving Style untouched - an
            unset Style is not the same as "Regular" to Fusion, which otherwise computes its
            own default at render time (it can pick "Light" for Arial - a weight Arial
            doesn't ship at all - producing "Font Not Found: Arial Light" even though this
            app never set "Light").
          - `font_style=<a string>`: set Style to EXACTLY that value instead, ignoring
            whatever was there before. The Media Pool Text+ template can carry a residual
            Style (e.g. "Light", left over from earlier edits) that the preserve-by-default
            behavior above would then
            faithfully copy onto every new/reset clip too - "Reset to Default"
            (ui.py's `_reset_font_to_default()`) and new-clip creation
            (`subtitle_engine.py`'s `_apply_text_and_styling()`) both pass
            `font_utils.DEFAULT_FONT_STYLE` ("Regular") here explicitly instead of relying on
            preservation, so they land on a known, predictable style instead of "whatever the
            template happened to have".

        That same dump's "AllInputsViaGetInputList" - a full walk of every input the tool
        exposes - has no list-of-valid-fonts/list-of-valid-styles anywhere: "Font"/"Style" are
        plain free-form strings with no discoverable enum of options via this scripting
        surface. So there's no known way to enumerate Fusion's own recognized font list
        from script - `ui.py`'s font picker (populated from `font_utils.py`, which filters
        installed system fonts down to ones backed by a real .ttf/.otf file, since DaVinci
        Resolve/Fusion has no separate bundled font catalog of its own either) is the only
        available source."""
        log_msgs: List[str] = []
        if text_tool is None or not hasattr(text_tool, "SetInput"):
            return False, ["  - [Text Style Error] TextPlus tool is None or has no SetInput."]

        any_success = False

        if font_name is not None:
            preserved_style = None
            if font_style is None:
                get_input_fn = getattr(text_tool, "GetInput", None)
                if callable(get_input_fn):
                    try:
                        preserved_style = get_input_fn("Style")
                    except Exception:
                        preserved_style = None
                # Falsy (None or "") means Style was never actually set - Fusion's own
                # Inspector shows "--" for this, and leaving it alone lets Fusion pick its
                # own default at render time (not reliably "Regular" - see docstring above).
                if not preserved_style:
                    preserved_style = FusionAnimationEngine._DEFAULT_STYLE_WHEN_UNSET

            try:
                text_tool.SetInput("Font", font_name)
                any_success = True
            except Exception as err:
                log_msgs.append(f"  - [Text Style Exception] SetInput('Font', '{font_name}'): {err}")

            style_to_write = font_style if font_style is not None else preserved_style
            if style_to_write is not None:
                try:
                    text_tool.SetInput("Style", style_to_write)
                except Exception as err:
                    log_msgs.append(
                        f"  - [Text Style Exception] Setting Style '{style_to_write}' after Font change: {err}"
                    )

        if font_size is not None:
            try:
                text_tool.SetInput("Size", font_size)
                any_success = True
            except Exception as err:
                log_msgs.append(f"  - [Text Style Exception] SetInput('Size', {font_size}): {err}")

        if color is not None and len(color) >= 3:
            for input_name, value in zip(["Red1", "Green1", "Blue1"], color[:3]):
                if value is None:
                    continue
                try:
                    text_tool.SetInput(input_name, value)
                    any_success = True
                except Exception as err:
                    log_msgs.append(f"  - [Text Style Exception] SetInput('{input_name}', {value}): {err}")

        return any_success, log_msgs

    # Shading element numbers for the optional styles (see the apply_*_style() docstrings).
    OUTLINE_ELEMENT = 2
    SHADOW_ELEMENT = 3
    BACKGROUND_ELEMENT = 4

    @staticmethod
    def disable_shading_element(text_tool: Any, element: int, label: str) -> Tuple[bool, List[str]]:
        """Turns a TextPlus shading element off (Enabled<N>=0). Used when the Outline/Shadow/
        Background group is unticked, so a clip that already had the element on (e.g. from an
        earlier apply) actually loses it instead of silently keeping it."""
        if text_tool is None or not hasattr(text_tool, "SetInput"):
            return False, [f"  - [{label} Style Error] TextPlus tool is None or has no SetInput."]
        try:
            text_tool.SetInput(f"Enabled{element}", 0)
            return True, []
        except Exception as err:
            return False, [f"  - [{label} Style Exception] SetInput('Enabled{element}', 0): {err}"]

    @staticmethod
    def apply_outline_style(
        text_tool: Any,
        color: Optional[Tuple[float, float, float]] = None,
        thickness: Optional[float] = None,
        opacity: Optional[float] = None,
    ) -> Tuple[bool, List[str]]:
        """Applies a stroke/outline around the TEXT GLYPHS THEMSELVES to a TextPlus tool's
        Shading Element 2 ("Border" - `ElementShape2 == 1`, distinct from Element 4's
        "Background" box shape `ElementShape4 == 2`).
        Not to be confused with ui.py's Background group's own (not yet mapped) "outline"
        fields, which would stroke the background BOX rather than the glyphs.

        Input names come from a "Dump Selected Node Settings" capture matched against
        Fusion's own Shading tab, as for apply_shadow_style()/apply_background_style().
        `opacity` maps to `Alpha2` (this element's own color alpha, not a
        separate `Opacity2` overall-element slider - the dump showed a genuinely modified
        `Alpha2` value, unlike Shadow/Background which use their `Opacity<N>` slider instead).

        Always enables the element (Enabled2=1); the page calls disable_shading_element()
        instead when its Text Outline group is unticked.
        Defensive per-field, same pattern as apply_text_style()."""
        log_msgs: List[str] = []
        if text_tool is None or not hasattr(text_tool, "SetInput"):
            return False, ["  - [Outline Style Error] TextPlus tool is None or has no SetInput."]

        any_success = False

        try:
            text_tool.SetInput("Enabled2", 1)
            any_success = True
        except Exception as err:
            log_msgs.append(f"  - [Outline Style Exception] SetInput('Enabled2', 1): {err}")

        if color is not None and len(color) >= 3:
            for input_name, value in zip(["Red2", "Green2", "Blue2"], color[:3]):
                if value is None:
                    continue
                try:
                    text_tool.SetInput(input_name, value)
                    any_success = True
                except Exception as err:
                    log_msgs.append(f"  - [Outline Style Exception] SetInput('{input_name}', {value}): {err}")

        if thickness is not None:
            try:
                text_tool.SetInput("Thickness2", thickness)
                any_success = True
            except Exception as err:
                log_msgs.append(f"  - [Outline Style Exception] SetInput('Thickness2', {thickness}): {err}")

        if opacity is not None:
            try:
                text_tool.SetInput("Alpha2", opacity)
                any_success = True
            except Exception as err:
                log_msgs.append(f"  - [Outline Style Exception] SetInput('Alpha2', {opacity}): {err}")

        return any_success, log_msgs

    @staticmethod
    def apply_shadow_style(
        text_tool: Any,
        color: Optional[Tuple[float, float, float]] = None,
        offset: Optional[Tuple[float, float]] = None,
        blur: Optional[float] = None,
        opacity: Optional[float] = None,
    ) -> Tuple[bool, List[str]]:
        """Applies Drop Shadow settings to a TextPlus tool's Shading Element 3 ("Shadow").

        TextPlus's "Shading" tab has up to 8 numbered shading elements (Element 1 is always
        the base text fill, already handled by apply_text_style() via Red1/Green1/Blue1);
        every OTHER property on an element is suffixed with that element's index, e.g.
        Red3/Green3/Blue3 for element 3's color. Input names come from a "Dump Selected Node
        Settings" capture matched against Fusion's own Shading tab - SaveSettings() alone
        silently omits some of these
        (Offset3, SoftnessX3/Y3), which is why the diagnostic now also walks GetInputList()
        directly (see diagnostics.py's _dump_all_inputs()). Always enables the element
        (Enabled3=1); the page calls disable_shading_element() instead when its Drop Shadow
        group is unticked.

        Defensive per-field, same pattern as apply_text_style()."""
        log_msgs: List[str] = []
        if text_tool is None or not hasattr(text_tool, "SetInput"):
            return False, ["  - [Shadow Style Error] TextPlus tool is None or has no SetInput."]

        any_success = False

        try:
            text_tool.SetInput("Enabled3", 1)
            any_success = True
        except Exception as err:
            log_msgs.append(f"  - [Shadow Style Exception] SetInput('Enabled3', 1): {err}")

        if color is not None and len(color) >= 3:
            for input_name, value in zip(["Red3", "Green3", "Blue3"], color[:3]):
                if value is None:
                    continue
                try:
                    text_tool.SetInput(input_name, value)
                    any_success = True
                except Exception as err:
                    log_msgs.append(f"  - [Shadow Style Exception] SetInput('{input_name}', {value}): {err}")

        if offset is not None and len(offset) >= 2:
            try:
                text_tool.SetInput("Offset3", [offset[0], offset[1]])
                any_success = True
            except Exception as err:
                log_msgs.append(f"  - [Shadow Style Exception] SetInput('Offset3', {list(offset)}): {err}")

        if blur is not None:
            for input_name in ("SoftnessX3", "SoftnessY3"):
                try:
                    text_tool.SetInput(input_name, blur)
                    any_success = True
                except Exception as err:
                    log_msgs.append(f"  - [Shadow Style Exception] SetInput('{input_name}', {blur}): {err}")

        if opacity is not None:
            try:
                text_tool.SetInput("Opacity3", opacity)
                any_success = True
            except Exception as err:
                log_msgs.append(f"  - [Shadow Style Exception] SetInput('Opacity3', {opacity}): {err}")

        return any_success, log_msgs

    # Fusion's Text+ "Level" dropdown for a shading element (Level<N>) controls what unit the
    # element applies to; "Level: Text" corresponds to Level4 == 0.0 in a node dump.
    # Background always uses "Text" (the whole text block as one box, not per-line/word/
    # character) - hardcoded by design, not user-configurable.
    _BACKGROUND_LEVEL_TEXT = 0

    @staticmethod
    def apply_background_style(
        text_tool: Any,
        color: Optional[Tuple[float, float, float]] = None,
        opacity: Optional[float] = None,
        corner_radius: Optional[float] = None,
        extend_horizontal: Optional[float] = None,
        extend_vertical: Optional[float] = None,
    ) -> Tuple[bool, List[str]]:
        """Applies Background settings to a TextPlus tool's Shading Element 4 ("Background"),
        with input names identified the same way as apply_shadow_style().

        Always forces Level4 to "Text" (see _BACKGROUND_LEVEL_TEXT) - fixed by design, not
        user-configurable. Outline color/width and the "override sizing" checkbox in ui.py's
        Background group have no known Fusion input (a node dump shows no separate outline
        for a background BOX - text outlines and background boxes
        appear to be distinct shading-element "shapes" via ElementShape<N>, so a background
        WITH its own outline likely needs a second shading element entirely, not a single
        extra property on this one) - intentionally left UI-only."""
        log_msgs: List[str] = []
        if text_tool is None or not hasattr(text_tool, "SetInput"):
            return False, ["  - [Background Style Error] TextPlus tool is None or has no SetInput."]

        any_success = False

        try:
            text_tool.SetInput("Enabled4", 1)
            any_success = True
        except Exception as err:
            log_msgs.append(f"  - [Background Style Exception] SetInput('Enabled4', 1): {err}")

        try:
            text_tool.SetInput("Level4", FusionAnimationEngine._BACKGROUND_LEVEL_TEXT)
            any_success = True
        except Exception as err:
            log_msgs.append(f"  - [Background Style Exception] SetInput('Level4', {FusionAnimationEngine._BACKGROUND_LEVEL_TEXT}): {err}")

        if color is not None and len(color) >= 3:
            for input_name, value in zip(["Red4", "Green4", "Blue4"], color[:3]):
                if value is None:
                    continue
                try:
                    text_tool.SetInput(input_name, value)
                    any_success = True
                except Exception as err:
                    log_msgs.append(f"  - [Background Style Exception] SetInput('{input_name}', {value}): {err}")

        if opacity is not None:
            try:
                text_tool.SetInput("Opacity4", opacity)
                any_success = True
            except Exception as err:
                log_msgs.append(f"  - [Background Style Exception] SetInput('Opacity4', {opacity}): {err}")

        if corner_radius is not None:
            try:
                text_tool.SetInput("Round4", corner_radius)
                any_success = True
            except Exception as err:
                log_msgs.append(f"  - [Background Style Exception] SetInput('Round4', {corner_radius}): {err}")

        if extend_horizontal is not None:
            try:
                text_tool.SetInput("ExtendHorizontal4", extend_horizontal)
                any_success = True
            except Exception as err:
                log_msgs.append(f"  - [Background Style Exception] SetInput('ExtendHorizontal4', {extend_horizontal}): {err}")

        if extend_vertical is not None:
            try:
                text_tool.SetInput("ExtendVertical4", extend_vertical)
                any_success = True
            except Exception as err:
                log_msgs.append(f"  - [Background Style Exception] SetInput('ExtendVertical4', {extend_vertical}): {err}")

        return any_success, log_msgs

    @staticmethod
    def _delete_connected_tool(tool: Any, input_name: str) -> List[str]:
        """Deletes whatever tool is currently connected to `tool`'s `input_name`, if any -
        e.g. a BezierSpline feeding a scalar input, or an XYPath modifier feeding a Point
        input. Shared by _create_and_connect_spline() (repeated Apply calls without a
        matching Remove Animations in between would otherwise accumulate stray keyframes on
        a connected spline instead of each Apply starting clean) and
        remove_animations_from_clip() - only disconnecting an input (`ConnectTo(None)`)
        without deleting whatever it had been connected to would leave orphaned
        spline/modifier tools in the comp that a later Apply could end up reusing (with
        their stale keyframes still intact)."""
        log_msgs: List[str] = []
        target_input = getattr(tool, input_name, None)
        get_connected_output_fn = getattr(target_input, "GetConnectedOutput", None)
        if not callable(get_connected_output_fn):
            return log_msgs
        try:
            connected = get_connected_output_fn()
        except Exception as err:
            log_msgs.append(f"  - [Cleanup Exception] {input_name}.GetConnectedOutput(): {err}")
            return log_msgs
        if connected is None:
            return log_msgs
        get_tool_fn = getattr(connected, "GetTool", None)
        try:
            connected_tool = get_tool_fn() if callable(get_tool_fn) else connected
        except Exception as err:
            log_msgs.append(f"  - [Cleanup Exception] {input_name}'s GetConnectedOutput().GetTool(): {err}")
            return log_msgs
        delete_fn = getattr(connected_tool, "Delete", None) if connected_tool is not None else None
        if callable(delete_fn):
            try:
                delete_fn()
                log_msgs.append(f"  - [Cleanup] Deleted stale tool previously connected to '{input_name}'.")
            except Exception as err:
                log_msgs.append(f"  - [Cleanup Exception] Deleting stale '{input_name}' connection: {err}")
        return log_msgs

    @staticmethod
    def _create_and_connect_spline(
        text_tool: Any, comp: Any, input_name: str, keyframes: Dict[float, float]
    ) -> Tuple[bool, List[str]]:
        """Creates a genuine keyframe animation curve for a scalar TextPlus input.

        Fusion's own Text+ template does NOT keyframe the TextPlus tool's inputs directly -
        a "Dump Selected Node Settings" capture shows "LayoutSize" and "Opacity1" each as an
        `Input` connected (`SourceOp`/`Source`) to a SEPARATE "BezierSpline" tool elsewhere
        in the composition, and THAT tool's own `KeyFrames` dict (keyed by string frame
        number, each entry at minimum `{"1": value}`) holds the actual curve. Keyframing the
        TextPlus tool's own input directly targets the wrong object entirely, so nothing
        animates.

        Unlike "Follower" (NOT creatable via comp.AddTool), "BezierSpline" appears in the
        dump as a real, independently-creatable tool, so `comp.AddTool("BezierSpline")` is
        expected to work.
        """
        log_msgs: List[str] = []
        if text_tool is None or comp is None or not keyframes:
            return False, ["  - [Spline Error] TextPlus tool, comp, or keyframes missing."]

        # Repeated Apply calls on the SAME clip (without Remove Animations in between)
        # accumulate stray keyframes on whatever spline ends up connected to `input_name`,
        # rather than each Apply starting clean - dumps of such a clip show an extra
        # keyframe (value 0.0, far from any neutral value this code would ever request) at a
        # frame this call never writes, which is absent on a freshly-Applied clip.
        # This function always creates a genuinely NEW spline below and reconnects to it, so
        # in principle the OLD spline should just become an orphaned no-op - but in practice
        # that isn't what happens. Rather than depend on the exact persistence mechanism,
        # this defensively deletes whatever's ALREADY connected to `input_name` first (see
        # _delete_connected_tool(), also reused by remove_animations_from_clip() so Remove
        # cleans up properly too, not just the next Apply), so every Apply is guaranteed to
        # start from a clean slate regardless of how the underlying accumulation was
        # happening.
        log_msgs.extend(FusionAnimationEngine._delete_connected_tool(text_tool, input_name))

        add_tool_fn = getattr(comp, "AddTool", None)
        if not callable(add_tool_fn):
            return False, ["  - [Spline Error] comp.AddTool is not available."]

        try:
            spline = add_tool_fn("BezierSpline")
            log_msgs.append(f"  - [Spline] comp.AddTool('BezierSpline') for '{input_name}' returned {spline!r}")
        except Exception as err:
            log_msgs.append(f"  - [Spline Exception] comp.AddTool('BezierSpline'): {err}")
            return False, log_msgs

        if spline is None or isinstance(spline, bool):
            log_msgs.append("  - [Spline Warning] comp.AddTool('BezierSpline') did not return a usable tool.")
            return False, log_msgs

        def _verify_via_save_settings(label: str) -> bool:
            """A node dump shows "KeyFrames" as a direct top-level property on a
            BezierSpline's SaveSettings() output - NOT nested under "Inputs" like a normal
            tool parameter. Plain getattr()/GetInput() readbacks are asymmetric with however
            the write actually happens and are unreliable, so verify via SaveSettings()
            (same mechanism the "Dump Selected Node Settings" diagnostic uses)."""
            save_settings_fn = getattr(spline, "SaveSettings", None)
            if not callable(save_settings_fn):
                log_msgs.append(f"  - [Spline {label} Verify] spline.SaveSettings not available.")
                return False
            try:
                saved = save_settings_fn()
            except Exception as err:
                log_msgs.append(f"  - [Spline {label} Verify Exception] SaveSettings(): {err}")
                return False
            kf_saved = saved.get("KeyFrames") if isinstance(saved, dict) else None
            log_msgs.append(f"  - [Spline {label} Verify] SaveSettings()['KeyFrames'] = {kf_saved!r}")
            return bool(kf_saved)

        # Connect FIRST, then keyframe - per community-documented Fusion scripting pattern
        # (Blackmagic/steakunderwater forums):
        #   comp.Merge1.Blend = comp.BezierSpline()
        #   comp.Merge1.Blend[1] = 1
        #   comp.Merge1.Blend[50] = 0
        # i.e. keyframes are set by bracket-indexing the TARGET INPUT itself (once connected),
        # NOT by touching the spline tool's own properties directly - which is what the H1/H2
        # fallbacks below do, and why neither works on its own.
        connected = False
        target_input = None
        try:
            target_input = getattr(text_tool, input_name)
            connect_fn = getattr(target_input, "ConnectTo", None)
            if callable(connect_fn):
                # In a node dump a connected input shows "SourceOp"/"Source" where "Source"
                # is "Value" for a BezierSpline - i.e. you connect to the spline's "Value"
                # OUTPUT, not the tool object itself. Connecting directly to the tool
                # returns False.
                connect_target = getattr(spline, "Value", None)
                if connect_target is not None:
                    res = connect_fn(connect_target)
                    log_msgs.append(f"  - [Spline] {input_name}.ConnectTo(spline.Value) returned {res!r}")
                    connected = bool(res)

                if not connected:
                    res2 = connect_fn(spline)
                    log_msgs.append(f"  - [Spline] {input_name}.ConnectTo(spline) [fallback] returned {res2!r}")
                    connected = bool(res2)
            else:
                log_msgs.append(f"  - [Spline Warning] '{input_name}' input object has no callable ConnectTo.")
        except Exception as err:
            log_msgs.append(f"  - [Spline Exception] {input_name}.ConnectTo: {err}")

        if not connected:
            return False, log_msgs

        log_msgs.append(f"  - Connected '{input_name}' to a new BezierSpline.")

        # H3 (community-documented): bracket-index the CONNECTED target input directly.
        times = sorted(keyframes.keys())
        h3_success_count = 0
        for t in times:
            try:
                target_input[t] = keyframes[t]
                h3_success_count += 1
            except Exception as err:
                log_msgs.append(f"  - [Spline H3 Exception] {input_name}[{t}] = {keyframes[t]}: {err}")
        log_msgs.append(f"  - [Spline H3] {h3_success_count}/{len(times)} bracket-index assignments on '{input_name}' raised no exception.")
        _verify_via_save_settings("H3")  # informational only - see note below

        # _verify_via_save_settings() is NOT required to pass for H3 to count as working: it
        # gives a false negative here, since SaveSettings()['KeyFrames'] never reflects these
        # keyframes even when they demonstrably play in Resolve's render (it reads
        # "SaveSettings()['KeyFrames'] = None" regardless of whether the animation actually
        # plays). Every H3 keyframe assignment raising no exception is trusted as success
        # on its own - the same bar this codebase uses for every other SetInput-based
        # operation (Font, Size, Center, colors, ...). The H1/H2 fallbacks and their own
        # verification calls remain for the genuine failure case where H3 itself raises
        # exceptions.
        keyframes_set = h3_success_count == len(times)

        # H1 fallback: bulk attribute-dict assignment on .KeyFrames, matching the dump's own
        # structure for this property (string-keyed frame numbers, {"1": value, "Flags": {...}}
        # entries) - does not work on its own, kept as a fallback.
        if not keyframes_set:
            kf_dict = {str(t): {"1": v, "Flags": {"Linear": True}} for t, v in keyframes.items()}
            try:
                spline.KeyFrames = kf_dict
            except Exception as err:
                log_msgs.append(f"  - [Spline H1 Exception] setattr(spline, 'KeyFrames', ...): {err}")
            keyframes_set = _verify_via_save_settings("H1")

        # H2 fallback: SetInput("Value", value, time) on the SPLINE tool itself.
        if not keyframes_set:
            success_count = 0
            for t in times:
                try:
                    spline.SetInput("Value", keyframes[t], t)
                    success_count += 1
                except Exception as err:
                    log_msgs.append(f"  - [Spline H2 Exception] spline.SetInput('Value', {keyframes[t]}, {t}): {err}")
            log_msgs.append(f"  - [Spline H2] {success_count}/{len(times)} SetInput('Value', value, time) calls raised no exception.")
            keyframes_set = success_count == len(times)

        if not keyframes_set:
            log_msgs.append(
                "  - [Spline Warning] Connection succeeded, but no keyframe-setting mechanism tried "
                "(H3 bracket-index, H1 attribute assignment, H2 SetInput(time=)) raised no exceptions – "
                "the connected spline may be flat/empty."
            )
        return keyframes_set, log_msgs

    # Tuning constants for the preset shapes below.
    # Pop and Bounce grow from this scale, not 0: a Text+ at LayoutSize exactly 0 rendered the
    # whole frame black over the video (seen on two clips; removing the animation fixed it).
    # A thousandth of full size is still nothing to see.
    _START_SCALE = 0.001
    _POP_OVERSHOOT_SCALE = 1.1  # kept small so Pop doesn't grow too far past its final size
    # before settling.
    _BOUNCE_FIRST_OVERSHOOT_SCALE = 1.25  # bigger than Pop's own overshoot, for a livelier feel
    _BOUNCE_UNDERSHOOT_SCALE = 0.92  # dips back below 1.0 between the two overshoots
    _BOUNCE_SECOND_OVERSHOOT_SCALE = 1.08  # a smaller second rebound before finally settling
    _FADE_HOLD_GAP_FRAMES = 1  # see _apply_smooth_fade()'s docstring
    _SLIDE_DISTANCE_FRACTION = 0.08  # modest offset so the slide stays subtle - a fraction
    # of composition width (From Left/Right) or height (From Top/Bottom).
    SLIDE_DIRECTIONS = ("From Left", "From Right", "From Top", "From Bottom")

    # Animation Speed slider: each preset's base duration (Pop=15, Bounce=20, Fade=15,
    # Slide=15 frames - see each preset's own `duration` default below) is used as-is for
    # "Slow" (scale 1.0), so the base feel is the slow end of the range. "Medium"/"Fast"
    # scale those same base durations down so the whole animation plays out over fewer
    # frames (a shorter keyframe span == a quicker, tighter-looking animation).
    SPEEDS = ("Fast", "Medium", "Slow")
    _SPEED_DURATION_SCALE = {"Fast": 0.4, "Medium": 0.65, "Slow": 1.0}
    _MIN_PRESET_DURATION_FRAMES = 3  # floor so "Fast" never collapses a preset to 0-1 frames

    @staticmethod
    def scaled_duration(base_duration: int, speed: str) -> int:
        """Scales a preset's base (Slow-speed) duration by the chosen Fast/Medium/Slow speed,
        floored at _MIN_PRESET_DURATION_FRAMES so a very short base duration can't be scaled
        down to something that no longer reads as an animation at all. Unrecognized `speed`
        values fall back to Slow (scale 1.0, i.e. the unscaled base duration) rather than
        raising, matching this codebase's existing defensive-per-field style."""
        scale = FusionAnimationEngine._SPEED_DURATION_SCALE.get(speed, 1.0)
        return max(FusionAnimationEngine._MIN_PRESET_DURATION_FRAMES, round(base_duration * scale))

    @staticmethod
    def apply_pop_preset_to_clip(text_tool: Any, comp: Any, start_frame: int = 0, duration: int = 15) -> Tuple[bool, List[str]]:
        """Whole-clip Pop animation: keyframes "LayoutSize" - per a node dump, the input
        Resolve's own Text+ template animates for a grow/settle scale effect (a hand-drawn
        curve there goes 1.0 -> 1.323 -> 1.118 -> 1.213 -> 1.134, i.e. exactly this kind of
        overshoot-and-settle motion). "TransformSize" looks like a plausible unconnected
        default-1.0 input, but its ConnectTo() call is rejected - "LayoutSize" is the one
        that is connectable. Distinct from "Size", which is the font-size parameter (default
        ~0.08 elsewhere in this codebase) - animating "Size" directly from 0 to ~1 would
        render text ~12x too large.

        The overshoot is _POP_OVERSHOOT_SCALE (1.1), kept small so Pop doesn't grow too far
        past its final size."""
        log_msgs: List[str] = []
        if text_tool is None:
            return False, ["  - [Pop Error] TextPlus tool is None."]

        t0 = start_frame
        t1 = start_frame + int(duration * 0.6)
        t2 = start_frame + duration
        keyframes = {t0: FusionAnimationEngine._START_SCALE, t1: FusionAnimationEngine._POP_OVERSHOOT_SCALE, t2: 1.0}

        success, kf_logs = FusionAnimationEngine._create_and_connect_spline(text_tool, comp, "LayoutSize", keyframes)
        log_msgs.extend(kf_logs)
        if success:
            log_msgs.append(f"  - Applied Pop preset keyframes for LayoutSize at t={t0}, {t1}, {t2}.")
        return success, log_msgs

    @staticmethod
    def apply_bounce_preset_to_clip(text_tool: Any, comp: Any, start_frame: int = 0, duration: int = 20) -> Tuple[bool, List[str]]:
        """Like Pop, but with an extra rebound for a bouncier feel. Keyframes "LayoutSize"
        (same input as Pop)
        through TWO overshoot cycles instead of Pop's one: grows past 1.0
        (_BOUNCE_FIRST_OVERSHOOT_SCALE), dips back below 1.0 (_BOUNCE_UNDERSHOOT_SCALE), a
        smaller second overshoot (_BOUNCE_SECOND_OVERSHOOT_SCALE), then finally settles at
        1.0."""
        log_msgs: List[str] = []
        if text_tool is None:
            return False, ["  - [Bounce Error] TextPlus tool is None."]

        t0 = start_frame
        t1 = start_frame + int(duration * 0.3)
        t2 = start_frame + int(duration * 0.5)
        t3 = start_frame + int(duration * 0.75)
        t4 = start_frame + duration
        keyframes = {
            t0: FusionAnimationEngine._START_SCALE,
            t1: FusionAnimationEngine._BOUNCE_FIRST_OVERSHOOT_SCALE,
            t2: FusionAnimationEngine._BOUNCE_UNDERSHOOT_SCALE,
            t3: FusionAnimationEngine._BOUNCE_SECOND_OVERSHOOT_SCALE,
            t4: 1.0,
        }

        success, kf_logs = FusionAnimationEngine._create_and_connect_spline(text_tool, comp, "LayoutSize", keyframes)
        log_msgs.extend(kf_logs)
        if success:
            log_msgs.append(f"  - Applied Bounce preset keyframes for LayoutSize at t={t0}, {t1}, {t2}, {t3}, {t4}.")
        return success, log_msgs

    @staticmethod
    def _apply_smooth_fade(text_tool: Any, comp: Any, start_frame: int, duration: int) -> Tuple[bool, List[str]]:
        """Shared fade-in implementation for both the Fade preset and Slide's own fade
        component - keyframes "Opacity1" 0.0 -> 1.0 (per a node dump, TextPlus's genuine
        opacity input, connected to a BezierSpline named "TemplateOpacity1" in Resolve's own
        default Text+ template - not "Opacity"/"Blend").

        With only two keyframes the fade visibly overshoots past 100% opacity ("bounces")
        before settling, instead of easing smoothly to a stop. A THIRD keyframe,
        holding the SAME final value (1.0) a moment after the ramp completes, is added so the
        curve has a flat run-out to blend into rather than an isolated endpoint - Bezier
        auto-tangent smoothing computes its tangent at a point from its neighbors, and a
        neighbor at the identical value pulls that tangent toward flat/zero, which is what
        prevents the curve from swinging past 1.0 on its way in. This is a value-based fix
        (more keyframes, no reliance on any interpolation-mode/Flags API), using only the
        bracket-index keyframe mechanism rather than undocumented spline-flag APIs."""
        log_msgs: List[str] = []
        if text_tool is None:
            return False, ["  - [Fade Error] TextPlus tool is None."]

        t0 = start_frame
        t1 = start_frame + duration
        t2 = t1 + FusionAnimationEngine._FADE_HOLD_GAP_FRAMES
        keyframes = {t0: 0.0, t1: 1.0, t2: 1.0}

        success, kf_logs = FusionAnimationEngine._create_and_connect_spline(text_tool, comp, "Opacity1", keyframes)
        log_msgs.extend(kf_logs)
        if success:
            log_msgs.append(f"  - Applied smooth fade-in keyframes for Opacity1 at t={t0}, {t1} (held flat from t={t2}).")
        return success, log_msgs

    @staticmethod
    def apply_fade_preset_to_clip(text_tool: Any, comp: Any, start_frame: int = 0, duration: int = 15) -> Tuple[bool, List[str]]:
        """Whole-clip Fade animation - see _apply_smooth_fade() for the actual keyframe shape
        and how it avoids overshooting past 100% opacity."""
        return FusionAnimationEngine._apply_smooth_fade(text_tool, comp, start_frame, duration)

    # Slide offset magnitude (see apply_slide_preset_to_clip()'s notes on the inputs
    # involved). +/-0.05 reads well as a modest offset in the closely-related
    # "CharacterOffset" coordinate space (a node dump shows both share a similar order of
    # magnitude for a comparable manual drag) - used here as a reasonable default.
    _SLIDE_DISTANCE_FRACTION = 0.05

    # Per-frame Slide motion connects DIRECTLY to the Template TextPlus tool's own "Pivot"
    # input. Pivot is a genuine existing input on the SAME TextPlus tool; Center/
    # CharacterOffset/LineOffset don't produce usable per-frame motion (see
    # apply_slide_preset_to_clip()). Why Pivot can move the rendered text at all despite
    # not being touched by any Angle/Size change: Pivot is the point in
    # the text's own bounding box that gets aligned to Center (default (0.5, 0.5) = the
    # block's own center aligns to Center) - shifting it changes which part of the block
    # coincides with the fixed Center, which visibly moves the text even with no rotation/
    # scale involved. Pivot's own neutral/default value is (0.5, 0.5) - the SAME convention
    # Center already uses (a node dump's "AllInputsViaGetInputList" shows the unconnected
    # default as {1: 0.5, 2: 0.5, 3: 0.0}).
    #
    # Pivot is driven via an "XYPath" modifier rather than a PolyPath+Displacement pair
    # (Pivot <- Path.Position <- Path.Displacement <- BezierSpline): building that pair via
    # comp.Paste() yields degenerate PolyLine results (two identical (0,0) points; a single
    # missing point; or comp.Paste() itself returning False and creating nothing at all),
    # for both Transform.Center and Pivot targets - comp.Paste()'s marshaling of the
    # compound "PolyLine" value is unreliable regardless of which tool/input ultimately
    # consumes it. XYPath is a documented Fusion modifier (steakunderwater "We Suck Less"
    # forum: "XYPath ... uses two independent BezierSplines for X and Y ... animated using
    # the SetKeyFrames() method"). XYPath splits a Point-type input into two independent
    # SCALAR splines (X, Y) - this COMPLETELY BYPASSES PolyLine. X/Y reduce to the exact same
    # scalar connect-then-bracket-index mechanism used for LayoutSize/Opacity1 -
    # _create_and_connect_spline() is reused unmodified.
    #
    # The attachment call, `tool.AddModifier(input_name, "XYPath")`, is the same one the
    # letter presets use for the text Follower (by its registry ID, "StyledTextFollower" -
    # see LETTER_PRESETS below).
    #
    # STATUS: EXPERIMENTAL - not yet verified against a live project. The log plus a
    # "Dump Selected Node Settings" capture of the Template tool show whether it took effect.
    _PIVOT_NEUTRAL = (0.5, 0.5)

    @staticmethod
    def _delete_named_tool(comp: Any, name: str) -> List[str]:
        """Best-effort deletes a previously-created tool by name - kept for
        remove_animations_from_clip()'s legacy cleanup of any "ATA_SlidePath" tool left over
        from the older PolyPath-based Pivot motion (see the note above
        apply_slide_preset_to_clip()). Tool.Delete() is a standard, well-documented
        Fusion scripting call - unlike this file's other exotic write attempts, this one is
        ordinary enough to trust on the same "no exception raised" bar, but still logged
        defensively."""
        log_msgs: List[str] = []
        if comp is None:
            return log_msgs
        existing = getattr(comp, name, None)
        if existing is None:
            return log_msgs
        delete_fn = getattr(existing, "Delete", None)
        if callable(delete_fn):
            try:
                delete_fn()
                log_msgs.append(f"  - [Cleanup] Deleted existing tool '{name}'.")
            except Exception as err:
                log_msgs.append(f"  - [Cleanup Exception] {name}.Delete(): {err}")
        else:
            log_msgs.append(f"  - [Cleanup Warning] Existing tool '{name}' has no callable Delete().")
        return log_msgs

    # Legacy name, kept only so remove_animations_from_clip() can clean up a stale tool left
    # on a clip by the older PolyPath-based approach - see the note above
    # apply_slide_preset_to_clip().
    _LEGACY_SLIDE_PATH_NAME = "ATA_SlidePath"

    @staticmethod
    def _attach_xy_path_modifier(tool: Any, input_name: str) -> Tuple[Optional[Any], List[str]]:
        """Attaches an XYPath modifier directly to `tool`'s `input_name` input via
        `tool.AddModifier(input_name, "XYPath")`. See the note above
        apply_slide_preset_to_clip() for why this differs from a PolyPath approach - it
        splits a Point-type input into two independent SCALAR splines, bypassing the
        compound "PolyLine" write that PolyPath depends on.

        `AddModifier('Pivot', 'XYPath')` can return a bare `True` rather than the modifier
        tool itself, as it does for the text Follower (see follower_of()). So the
        modifier is found through the input's connection instead:
        `tool.<input_name>.GetConnectedOutput()` should return the
        Output the input is now connected to, and `.GetTool()` on that Output should return
        the modifier tool owning it - both individually standard, commonly-documented Fusion
        scripting methods (Input:GetConnectedOutput(), Output:GetTool()), not fabricated ones.

        Returns (xy_modifier, log_msgs) - xy_modifier may be None on failure."""
        log_msgs: List[str] = []
        if tool is None:
            return None, ["  - [XYPath Error] tool is None."]

        add_modifier_fn = getattr(tool, "AddModifier", None)
        if not callable(add_modifier_fn):
            log_msgs.append("  - [XYPath Error] tool.AddModifier is not available.")
            return None, log_msgs

        try:
            result = add_modifier_fn(input_name, "XYPath")
            log_msgs.append(f"  - [XYPath] AddModifier('{input_name}', 'XYPath') returned {result!r}")
        except Exception as err:
            log_msgs.append(f"  - [XYPath Exception] AddModifier('{input_name}', 'XYPath'): {err}")
            return None, log_msgs

        if result is not None and not isinstance(result, bool):
            return result, log_msgs

        # AddModifier returned True/False/None rather than a usable tool - try to
        # locate the attached modifier via the input's own connection instead of trusting the
        # return value, as follower_of() does.
        if result is False:
            log_msgs.append("  - [XYPath Warning] AddModifier returned False – modifier likely not attached.")
            return None, log_msgs

        try:
            target_input = getattr(tool, input_name, None)
            get_connected_output_fn = getattr(target_input, "GetConnectedOutput", None)
            if not callable(get_connected_output_fn):
                log_msgs.append(f"  - [XYPath Warning] '{input_name}' input has no callable GetConnectedOutput.")
                return None, log_msgs

            connected_output = get_connected_output_fn()
            log_msgs.append(f"  - [XYPath] {input_name}.GetConnectedOutput() returned {connected_output!r}")
            if connected_output is None:
                log_msgs.append("  - [XYPath Warning] GetConnectedOutput() returned None – AddModifier's True may not reflect a real attach.")
                return None, log_msgs

            get_tool_fn = getattr(connected_output, "GetTool", None)
            if not callable(get_tool_fn):
                log_msgs.append("  - [XYPath Warning] Connected output has no callable GetTool – trying it directly as the modifier tool.")
                return connected_output, log_msgs

            modifier_tool = get_tool_fn()
            log_msgs.append(f"  - [XYPath] GetConnectedOutput().GetTool() returned {modifier_tool!r}")
            if modifier_tool is None or isinstance(modifier_tool, bool):
                log_msgs.append("  - [XYPath Warning] Could not resolve a usable modifier tool via GetConnectedOutput().GetTool().")
                return None, log_msgs
            return modifier_tool, log_msgs
        except Exception as err:
            log_msgs.append(f"  - [XYPath Exception] Locating modifier via GetConnectedOutput/GetTool: {err}")
            return None, log_msgs

    @staticmethod
    def _slide_direction_offset(direction: str) -> Tuple[float, float]:
        """Maps a SLIDE_DIRECTIONS value to an (dx, dy) offset from the neutral Pivot value,
        with a +/- _SLIDE_DISTANCE_FRACTION magnitude. Fusion's Y axis is bottom-up, so
        "From Top" needs a POSITIVE dy to start above the resting position."""
        d = FusionAnimationEngine._SLIDE_DISTANCE_FRACTION
        return {
            "From Left": (-d, 0.0),
            "From Right": (d, 0.0),
            "From Top": (0.0, d),
            "From Bottom": (0.0, -d),
        }.get(direction, (-d, 0.0))

    @staticmethod
    def apply_slide_preset_to_clip(
        text_tool: Any,
        comp: Any,
        target_center: Tuple[float, float] = (0.5, 0.5),
        direction: str = "From Left",
        start_frame: int = 0,
        duration: int = 15,
    ) -> Tuple[bool, List[str]]:
        """Slide-in animation: fades in (the SAME mechanism as the Fade preset -
        _apply_smooth_fade()) and sets its final, static Center position - both
        unconditionally safe.

        EXPERIMENTAL (not yet verified against a live project - see the block comment above
        apply_slide_preset_to_clip() for the full reasoning): attempts real per-frame slide
        MOTION by attaching an "XYPath" modifier directly to the Template TextPlus tool's own
        "Pivot" input (`tool.AddModifier("Pivot", "XYPath")`), then keyframing that
        modifier's independent "X"/"Y" scalar inputs. If this fails for any reason, the
        failure is isolated to `motion_success` only - the fade+static-Center behavior always
        still applies, so Slide never degrades below a plain fade-in.

        Approaches on TextPlus Point-type inputs that do NOT work:
          - Connecting "Center" directly to a "PolyPath" tool's "Position" output leaves
            Center permanently stuck/unresponsive to later repositioning (a connected input
            ignores ordinary SetInput() calls).
          - The same PolyPath mechanism targeting "LineOffset" (the input behind Fusion's own
            "Transform Offset X/Y" UI control) or "CharacterOffset" instead of Center: three
            different SetInput('PolyLine', ...) value shapes plus LoadSettings() fed the exact
            SaveSettings() round-trip shape all produce the byte-for-byte IDENTICAL result:
            Fusion's own default single-point path placeholder, completely unaffected
            regardless of mechanism or target input.
          - Bracket-indexing "Center" directly (`target_input[t] = value`, the technique that
            works for scalar inputs like LayoutSize/Opacity1) with NO external tool at all
            silently has zero effect; Center comes back as a plain static, unconnected value.
            Bracket-indexing an input that isn't already connected to something doesn't
            create real animation, the same as for scalars.
          - A separate Transform node inserted between Template and MediaOut,
            Transform.Center <- PolyPath.Position - structurally sound (it matches a
            manually-built working example) but depends on the same PolyPath/comp.Paste()
            mechanism as below.
          - Pivot <- PolyPath.Position <- PolyPath.Displacement <- BezierSpline: comp.Paste()
            creates real tools and correct connections on a live clip, but the compound
            "PolyLine" value itself comes out degenerate in different ways (two identical
            (0,0) points; a single point missing its pair entirely; comp.Paste() itself
            returning False and creating nothing) - the failure is in comp.Paste()'s
            marshaling of a compound nested value, independent of which tool/input consumes
            it.
        An XYPath modifier avoids the compound PolyLine value entirely, reducing to two
        independent SCALARS (X, Y) using the same connect-then-bracket-index mechanism that
        works for LayoutSize/Opacity1."""
        log_msgs: List[str] = []
        if text_tool is None:
            return False, ["  - [Slide Error] TextPlus tool is None."]

        if direction not in FusionAnimationEngine.SLIDE_DIRECTIONS:
            log_msgs.append(f"  - [Slide Warning] Unrecognized direction '{direction}' – defaulting to 'From Left'.")

        tx, ty = target_center[0], target_center[1]
        try:
            text_tool.SetInput("Center", [tx, ty])
        except Exception as err:
            log_msgs.append(f"  - [Slide Exception] SetInput('Center', [{tx}, {ty}]): {err}")

        fade_success, fade_logs = FusionAnimationEngine._apply_smooth_fade(text_tool, comp, start_frame, duration)
        log_msgs.extend(fade_logs)

        motion_success = False
        if comp is not None:
            neutral = FusionAnimationEngine._PIVOT_NEUTRAL
            dx, dy = FusionAnimationEngine._slide_direction_offset(direction)
            start_xy = (neutral[0] + dx, neutral[1] + dy)

            xy_modifier, xy_logs = FusionAnimationEngine._attach_xy_path_modifier(text_tool, "Pivot")
            log_msgs.extend(xy_logs)
            if xy_modifier is not None:
                # X/Y keyframed directly in Pivot-space (no PolyLine-style -0.5 shift - that
                # correction was specific to PolyPath's own Position-output convention, which
                # this mechanism doesn't use at all; unverified whether XYPath's X/Y share
                # Pivot's own (0.5, 0.5)-neutral convention directly - motion landing 0.5 off
                # on either axis would mean it needs the same kind of shift, and this is the
                # first place to look).
                x_success, x_logs = FusionAnimationEngine._create_and_connect_spline(
                    xy_modifier, comp, "X", {start_frame: start_xy[0], start_frame + duration: neutral[0]}
                )
                log_msgs.extend(x_logs)
                y_success, y_logs = FusionAnimationEngine._create_and_connect_spline(
                    xy_modifier, comp, "Y", {start_frame: start_xy[1], start_frame + duration: neutral[1]}
                )
                log_msgs.extend(y_logs)
                motion_success = x_success and y_success
                if motion_success:
                    log_msgs.append(
                        f"  - [EXPERIMENTAL] Applied Slide motion ({direction}) via an XYPath modifier on "
                        f"Template.Pivot, t={start_frame}->{start_frame + duration}. UNCONFIRMED against a "
                        "live project – verify the result with a fresh node dump."
                    )
            else:
                log_msgs.append("  - [Slide Warning] Could not attach an XYPath modifier – motion skipped, fade-only.")

        success = fade_success or motion_success
        if success:
            log_msgs.append(f"  - Applied Slide preset ({direction}) at t={start_frame}, {start_frame + duration}.")
        return success, log_msgs

    @staticmethod
    def remove_animations_from_clip(text_tool: Any, comp: Optional[Any] = None) -> Tuple[bool, List[str]]:
        """Reverses whatever Pop/Bounce/Fade/Slide or a letter preset did to ONE TextPlus
        tool - takes off a Follower (remove_follower: its text goes back first), disconnects the
        inputs those presets
        ever connect something to ("LayoutSize" for Pop/Bounce, "Opacity1" for Fade/Slide, and
        legacy "Center"/"LineOffset"/"CharacterOffset" cleanup - see below) and resets what it
        safely can back to its neutral, unanimated static value (LayoutSize=1.0 i.e. normal/
        unscaled, Opacity1=1.0 i.e. fully visible, LineOffset/CharacterOffset=[0, 0] i.e. no
        relative offset) - matching a Text+ clip that was never animated by this tab.

        Deliberately does NOT touch "Center"'s VALUE (or "Size", font size) at all - those
        belong to Timeline Layout/Bounding, an entirely separate concern from animation, and
        removing animations must leave positioning alone.
        "Center" only ever gets `ConnectTo(None)` (disconnect only, never SetInput) - there's
        no known-correct value to restore it to, so disconnecting it just freezes it at
        whatever it was last evaluating to. Slide never animates Center (see
        apply_slide_preset_to_clip()'s notes - Center-based per-frame motion doesn't work,
        so Slide only sets a static Center) - this disconnect is kept purely as legacy
        cleanup for any clip left with Center connected to a path or spline.

        `input.ConnectTo(None)` is the standard, widely-documented Fusion scripting idiom for
        disconnecting an input (unlike the LayoutSize/Opacity1/Center names themselves, which
        come from node dumps - see
        apply_pop_preset_to_clip()/apply_fade_preset_to_clip()/apply_slide_preset_to_clip()
        above) - the SetInput() reset that follows (where one is safe to apply) is what
        actually guarantees a correct visual result even if disconnecting silently has no
        effect for any reason.

        The "LineOffset"/"CharacterOffset" resets are ALSO legacy cleanup, not part of
        Slide's current mechanism: an older Slide connected one or the other via an external
        PolyPath tool (which never animates - see apply_slide_preset_to_clip()) - a clip
        carrying such a connection still needs this to fully clear it.

        If `comp` is provided, also reverses the Pivot-based motion mechanism (EXPERIMENTAL,
        see apply_slide_preset_to_clip()) - disconnects `text_tool.Pivot` and resets it to its
        neutral static value ([0.5, 0.5], the same convention Center already uses). The XYPath
        modifier is attached directly to Pivot (not a separate named comp-level tool), so
        disconnecting Pivot is sufficient to detach it -
        `_delete_named_tool(comp, _LEGACY_SLIDE_PATH_NAME)` is kept only to clean up a stale
        "ATA_SlidePath" tool left over from the older PolyPath-based approach. `comp` is
        optional (defaults to None) so existing
        callers that don't have it in scope keep working unchanged, just without this extra
        cleanup step."""
        log_msgs: List[str] = []
        if text_tool is None:
            return False, ["  - [Remove Animations Error] TextPlus tool is None."]

        # A letter-by-letter preset's Follower first: it holds the clip's text.
        any_success, follower_logs = FusionAnimationEngine.remove_follower(text_tool)
        log_msgs.extend(follower_logs)
        for input_name, neutral_value in (
            ("LayoutSize", 1.0),
            ("Opacity1", 1.0),
            ("LineOffset", [0.0, 0.0]),
            ("CharacterOffset", [0.0, 0.0]),
        ):
            # Delete whatever spline is connected BEFORE disconnecting (once disconnected,
            # GetConnectedOutput() has nothing left to find) - only disconnecting would leave
            # an orphaned spline in the comp that a later Apply's AddTool('BezierSpline') +
            # reconnect could end up alongside, rather than actually removing it.
            # _create_and_connect_spline() also self-heals this on the next Apply, but Remove
            # should clean up properly on its own.
            log_msgs.extend(FusionAnimationEngine._delete_connected_tool(text_tool, input_name))

            try:
                target_input = getattr(text_tool, input_name, None)
                disconnect_fn = getattr(target_input, "ConnectTo", None) if target_input is not None else None
                if callable(disconnect_fn):
                    disconnect_fn(None)
            except Exception as err:
                log_msgs.append(f"  - [Remove Animations Exception] {input_name}.ConnectTo(None): {err}")

            try:
                text_tool.SetInput(input_name, neutral_value)
                any_success = True
            except Exception as err:
                log_msgs.append(f"  - [Remove Animations Exception] SetInput('{input_name}', {neutral_value}): {err}")

        try:
            center_input = getattr(text_tool, "Center", None)
            center_disconnect_fn = getattr(center_input, "ConnectTo", None) if center_input is not None else None
            if callable(center_disconnect_fn):
                center_disconnect_fn(None)
        except Exception as err:
            log_msgs.append(f"  - [Remove Animations Exception] Center.ConnectTo(None): {err}")

        # Locate and delete the XYPath modifier attached to Pivot (and its own
        # "X"/"Y" child splines) BEFORE disconnecting Pivot - same reasoning as LayoutSize/
        # Opacity1 above. The modifier and its X/Y splines are separate tool entries in the
        # comp (as a node dump shows), so deleting the modifier alone would still leave
        # its two child splines orphaned.
        try:
            pivot_input = getattr(text_tool, "Pivot", None)
            get_connected_output_fn = getattr(pivot_input, "GetConnectedOutput", None)
            xy_modifier = None
            if callable(get_connected_output_fn):
                connected = get_connected_output_fn()
                if connected is not None:
                    get_tool_fn = getattr(connected, "GetTool", None)
                    xy_modifier = get_tool_fn() if callable(get_tool_fn) else connected
            if xy_modifier is not None:
                log_msgs.extend(FusionAnimationEngine._delete_connected_tool(xy_modifier, "X"))
                log_msgs.extend(FusionAnimationEngine._delete_connected_tool(xy_modifier, "Y"))
                xy_delete_fn = getattr(xy_modifier, "Delete", None)
                if callable(xy_delete_fn):
                    xy_delete_fn()
                    log_msgs.append("  - [Cleanup] Deleted the XYPath modifier previously attached to 'Pivot'.")
        except Exception as err:
            log_msgs.append(f"  - [Remove Animations Exception] Deleting Pivot's XYPath modifier: {err}")

        try:
            pivot_input = getattr(text_tool, "Pivot", None)
            pivot_disconnect_fn = getattr(pivot_input, "ConnectTo", None) if pivot_input is not None else None
            if callable(pivot_disconnect_fn):
                pivot_disconnect_fn(None)
            text_tool.SetInput("Pivot", list(FusionAnimationEngine._PIVOT_NEUTRAL))
        except Exception as err:
            log_msgs.append(f"  - [Remove Animations Exception] Pivot reset: {err}")

        if comp is not None:
            log_msgs.extend(FusionAnimationEngine._delete_named_tool(comp, FusionAnimationEngine._LEGACY_SLIDE_PATH_NAME))

        if any_success:
            log_msgs.append("  - Removed animation keyframes and reset LayoutSize/Opacity1/LineOffset/CharacterOffset to their neutral values.")
        return any_success, log_msgs

    # ---------------------------------------------------- letter by letter
    # The per-letter presets run on Fusion's text Follower, the modifier Resolve's own
    # "Rise Fade", "Scale Up" and "Drop In" titles are built on (Templates.drfx). Its
    # registry ID is "StyledTextFollower": AddModifier("StyledText", "Follower") attaches
    # nothing, which is why an earlier attempt here gave up (the fix measured by
    # github.com/samuelgursky/davinci-resolve-mcp; checked again on Studio 21.1). Once
    # attached, the words live on the Follower's "Text" and the Text+ "StyledText" reads
    # them from it - so taking it off puts them back first (remove_follower). Its own
    # inputs animate each letter, the next one "Delay" frames after the last: "Opacity1",
    # or "CharacterSizeX" and "CharacterSizeY" together, each connected to a BezierSpline
    # like the whole-clip presets above.
    FOLLOWER_ID = "StyledTextFollower"
    LETTER_PRESETS = ("Typewriter (Letters)", "Letter Fade (Letters)", "Letter Pop (Letters)")
    # Frames from one letter to the next at each speed - squeezed for a long line, so the
    # whole of it has arrived within _LETTER_REVEAL_FRAMES.
    _LETTER_DELAY = {"Fast": 1.0, "Medium": 2.0, "Slow": 3.0}
    _LETTER_REVEAL_FRAMES = {"Fast": 12, "Medium": 20, "Slow": 30}
    _LETTER_POP_OVERSHOOT = 1.15

    @staticmethod
    def follower_of(text_tool: Any) -> Optional[Any]:
        """The Follower driving this Text+'s text, or None."""
        try:
            output = text_tool.StyledText.GetConnectedOutput()
            tool = output.GetTool() if output is not None else None
        except Exception:
            return None
        if tool is None or getattr(tool, "ID", None) != FusionAnimationEngine.FOLLOWER_ID:
            return None
        return tool

    @staticmethod
    def attach_follower(text_tool: Any) -> Tuple[Optional[Any], List[str]]:
        """The Text+'s Follower - attached now if it has none. AddModifier's return isn't
        trusted: the StyledText input's connection says whether one is there."""
        existing = FusionAnimationEngine.follower_of(text_tool)
        if existing is not None:
            return existing, []
        try:
            text = text_tool.GetInput("StyledText")
            result = text_tool.AddModifier("StyledText", FusionAnimationEngine.FOLLOWER_ID)
        except Exception as err:
            return None, [f"  - [Follower Exception] AddModifier('StyledText', 'StyledTextFollower'): {err}"]
        follower = FusionAnimationEngine.follower_of(text_tool)
        logs = [f"  - [Follower] AddModifier('StyledText', 'StyledTextFollower') returned {result!r}; "
                f"{'attached' if follower is not None else 'nothing attached'}."]
        if follower is not None and isinstance(text, str) and text and not follower.GetInput("Text"):
            follower.SetInput("Text", text)
        return follower, logs

    @staticmethod
    def remove_follower(text_tool: Any) -> Tuple[bool, List[str]]:
        """Takes a per-letter animation off: the Follower's text goes back on the Text+
        itself, then the Follower and its splines are deleted. False if there wasn't one."""
        follower = FusionAnimationEngine.follower_of(text_tool)
        if follower is None:
            return False, []
        logs: List[str] = []
        try:
            text = follower.GetInput("Text")
            if not isinstance(text, str):
                text = text_tool.GetInput("StyledText")
        except Exception:
            text = None
        for input_name in ("Opacity1", "CharacterSizeX", "CharacterSizeY"):
            logs.extend(FusionAnimationEngine._delete_connected_tool(follower, input_name))
        try:
            text_tool.StyledText.ConnectTo(None)
            if isinstance(text, str):
                text_tool.SetInput("StyledText", text)
            follower.Delete()
            logs.append("  - [Cleanup] Put the text back on the Text+ and deleted its Follower.")
        except Exception as err:
            logs.append(f"  - [Remove Follower Exception] {err}")
            return False, logs
        return True, logs

    @staticmethod
    def letter_delay(text: str, speed: str) -> float:
        """Frames between one letter and the next: the speed's own pace, or less for a line
        too long to arrive in time at it."""
        steps = max(1, len(text or "") - 1)
        pace = FusionAnimationEngine._LETTER_DELAY.get(speed, FusionAnimationEngine._LETTER_DELAY["Slow"])
        reveal = FusionAnimationEngine._LETTER_REVEAL_FRAMES.get(speed, FusionAnimationEngine._LETTER_REVEAL_FRAMES["Slow"])
        return round(min(pace, reveal / steps), 3)

    @staticmethod
    def apply_letter_preset_to_clip(text_tool: Any, comp: Any, preset: str, speed: str = "Medium",
                                    start_frame: int = 0) -> Tuple[bool, List[str]]:
        """Typewriter (each letter appears, one after another), Letter Fade (each fades in)
        or Letter Pop (each grows from nothing with a little overshoot), on the Text+'s
        Follower."""
        if text_tool is None or comp is None:
            return False, ["  - [Letters Error] TextPlus tool or comp missing."]
        follower, logs = FusionAnimationEngine.attach_follower(text_tool)
        if follower is None:
            logs.append("  - [Letters Warning] The Follower modifier could not be attached.")
            return False, logs
        try:
            text = follower.GetInput("Text")
        except Exception:
            text = ""
        delay = FusionAnimationEngine.letter_delay(text if isinstance(text, str) else "", speed)
        try:
            follower.SetInput("Delay", delay)
        except Exception as err:
            logs.append(f"  - [Letters Exception] SetInput('Delay', {delay}): {err}")
            return False, logs
        t0 = start_frame
        if preset.startswith("Letter Pop"):
            duration = FusionAnimationEngine.scaled_duration(10, speed)
            try:
                follower.SetInput("TransformSize", 1)   # the size group on, as Resolve's Scale Up has it
            except Exception:
                pass
            keys = {t0: FusionAnimationEngine._START_SCALE,
                    t0 + max(1, int(duration * 0.6)): FusionAnimationEngine._LETTER_POP_OVERSHOOT,
                    t0 + duration: 1.0}
            ok = True
            # Width and height each get the curve: an expression tying Y to X (as Scale Up
            # has) set through the scripting API left every letter full height and thin.
            for axis in ("CharacterSizeX", "CharacterSizeY"):
                axis_ok, spline_logs = FusionAnimationEngine._create_and_connect_spline(follower, comp, axis, keys)
                logs.extend(spline_logs)
                ok = ok and axis_ok
        else:
            duration = 1 if preset.startswith("Typewriter") else FusionAnimationEngine.scaled_duration(12, speed)
            ok, spline_logs = FusionAnimationEngine._create_and_connect_spline(follower, comp, "Opacity1", {
                t0: 0.0, t0 + duration: 1.0, t0 + duration + FusionAnimationEngine._FADE_HOLD_GAP_FRAMES: 1.0})
            logs.extend(spline_logs)
        if ok:
            logs.append(f"  - Applied {preset}: {delay} frame(s) between letters, {duration} frame(s) each.")
        return ok, logs
