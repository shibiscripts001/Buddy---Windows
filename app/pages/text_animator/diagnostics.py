"""Live Fusion node diagnostics: dumps SaveSettings() for whatever is currently selected in
Fusion's Flow view to a JSON file, for calibrating generation code against ground truth instead
of guessing Fusion's internal data model (tool/modifier structure, keyframe representation,
exact input names).

Ported from the "Dump Selected Node Settings" mechanism in the SVG Importer project, which uses
the same technique successfully: `resolve.Fusion()` gives a separate connection that can run an
arbitrary script INSIDE Fusion's own execution context via `RunScript()`, where `SaveSettings()`
is available and returns a tool's complete real settings tree (including connected modifiers and
their keyframe data) - this is a fundamentally different, more direct API surface than the
Resolve Edit-page `resolve`/timeline/mediaPool object graph used everywhere else in this project.
"""
import json
import os
import tempfile
import time
import uuid
from typing import Any, Optional, Tuple

# This source runs INSIDE Fusion (via fusion.RunScript), not in our own process - it only has
# access to whatever globals Fusion's script execution context provides (fusion/comp/app), not
# anything from autotext_animator itself.
_DUMP_WORKER_SOURCE = r'''
import json
import traceback

RESULT_PATH = __RESULT_PATH__


def _find_comp():
    """fusion.GetCurrentComp() (and app.GetCurrentComp()) only return something once the
    target clip's composition is actually loaded as the active tab in Fusion's OWN page UI -
    they know nothing about the Edit page's timeline/current-clip selection, which lives in a
    completely separate API object graph (see this module's docstring). If the user hasn't
    switched DaVinci to the Fusion page for that clip, every one of these returns None, which
    is the single most common reason this diagnostic fails.
    GetCompList() is tried last as a defensive fallback for the case where a comp IS open in
    Fusion but isn't reported as "current" for some reason - picks the first one available
    rather than guessing which of several is the intended target."""
    g = globals()
    fu = g.get("fusion")
    if fu is not None:
        try:
            c = fu.GetCurrentComp()
            if c is not None:
                return c
        except Exception:
            pass
    c = g.get("comp")
    if c is not None:
        return c
    ap = g.get("app")
    if ap is not None:
        try:
            c = ap.GetCurrentComp()
            if c is not None:
                return c
        except Exception:
            pass
    if fu is not None:
        try:
            comp_list = fu.GetCompList()
            if comp_list:
                return next(iter(comp_list.values()))
        except Exception:
            pass
    return None


def _probe_rendered_bounds(t, comp):
    """Tries several plausible ways to get a tool's real RENDERED pixel bounding box
    (Fusion's Domain of Definition / DataWindow) at the current time, rather than assuming
    one - the scripting-context call is documented (community sources) as `GetDoD()`,
    distinct from the `.DataWindow` property used in Expressions, but the exact object it's
    called on and its argument signature isn't confirmed. Each hypothesis is tried and
    recorded independently (the same defensive multi-hypothesis pattern used for keyframe
    and Follower inputs), so whichever one actually works becomes visible in the dump
    instead of guessed. This is real, precise ground truth for how a Size value maps to
    actual on-screen pixels."""
    current_time = None
    try:
        current_time = comp.CurrentTime
    except Exception:
        current_time = None

    hypotheses = []

    def record(label, fn):
        try:
            hypotheses.append({"label": label, "ok": True, "value": fn()})
        except Exception as exc:
            hypotheses.append({"label": label, "ok": False, "error": str(exc)})

    record("t.GetDoD(current_time)", lambda: t.GetDoD(current_time))
    record("t.GetDoD()", lambda: t.GetDoD())
    record("t.Output.GetDoD(current_time)", lambda: t.Output.GetDoD(current_time))
    record("t.Output.GetDoD()", lambda: t.Output.GetDoD())
    record("t.Output.GetValue(current_time).DataWindow", lambda: t.Output.GetValue(current_time).DataWindow)
    record("t.Output.GetValue().DataWindow", lambda: t.Output.GetValue().DataWindow)

    return {"current_time": current_time, "hypotheses": hypotheses}


def _dump_all_inputs(t):
    """Reads every input's CURRENT value directly off the tool via GetInputList()+GetInput(),
    regardless of whether SaveSettings() chose to serialize it. GetInputList() returns
    genuine per-input objects with a real `.ID` attribute (matched against known input
    names like "StyledText"). This exists because SaveSettings() can silently omit some
    shading-element inputs the user has explicitly changed from default (e.g. a Shadow
    element's Offset/Softness X+Y) - GetInputList() is the fallback that can't miss them,
    since it walks the tool's actual input set rather than whatever SaveSettings decided to
    include."""
    all_inputs = {}
    try:
        input_list = t.GetInputList()
    except Exception:
        return {"_error": traceback.format_exc()}
    if not input_list:
        return all_inputs
    for key, inp in input_list.items():
        try:
            input_id = inp.ID
        except Exception:
            input_id = str(key)
        try:
            all_inputs[input_id] = t.GetInput(input_id)
        except Exception as exc:
            all_inputs[input_id] = {"_error": str(exc)}
    return all_inputs


result = {"ok": False, "message": ""}
try:
    c = _find_comp()
    if c is None:
        raise RuntimeError(
            "Couldn't find an active composition. This almost always means DaVinci is still "
            "on the Edit page - Fusion's own \"current comp\" only updates once you actually "
            "switch to the Fusion page (bottom page tabs) with the Text+ clip open there. "
            "Select the clip on the timeline, click the Fusion page tab, select the node in "
            "the Flow view, then click \"Dump Selected Node Settings\" again."
        )
    tools_dict = c.GetToolList(True)
    tools = list(tools_dict.values()) if tools_dict else []
    if not tools:
        result["message"] = "No tools are currently selected in Fusion's Flow view."
    else:
        dumped = {}
        for t in tools:
            try:
                name = t.GetAttrs()["TOOLS_Name"]
            except Exception:
                name = str(t)
            entry = {}
            try:
                entry["SaveSettings"] = t.SaveSettings()
            except Exception:
                entry["SaveSettings"] = {"_error": traceback.format_exc()}
            try:
                entry["AllInputsViaGetInputList"] = _dump_all_inputs(t)
            except Exception:
                entry["AllInputsViaGetInputList"] = {"_error": traceback.format_exc()}
            try:
                entry["RenderedBoundsProbe"] = _probe_rendered_bounds(t, c)
            except Exception:
                entry["RenderedBoundsProbe"] = {"_error": traceback.format_exc()}
            dumped[name] = entry
        result["ok"] = True
        result["message"] = f"Dumped settings for {len(dumped)} tool(s)."
        result["tools"] = dumped
except Exception:
    result["message"] = "Dump failed inside Fusion:\n" + traceback.format_exc()

with open(RESULT_PATH, "w") as f:
    json.dump(result, f, indent=2, default=str)
'''


def build_dump_worker_script(result_path: str) -> str:
    """Writes the dump worker script to a temp file and returns its path."""
    script_path = os.path.join(tempfile.gettempdir(), f"fusion_dump_worker_{uuid.uuid4().hex}.py")
    source = _DUMP_WORKER_SOURCE.replace("__RESULT_PATH__", repr(result_path))
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(source)
    return script_path


def dump_selected_node_settings(resolve_instance: Any, timeout_seconds: float = 10.0) -> Tuple[bool, str, Optional[str]]:
    """Runs the dump worker inside Fusion via resolve.Fusion().RunScript(), waits for its result
    file, and saves a copy to the user's Desktop.

    Returns (success, message, saved_file_path). saved_file_path is None on failure.
    """
    if resolve_instance is None:
        return False, "Resolve API instance not connected.", None

    fusion_getter = getattr(resolve_instance, "Fusion", None)
    if not callable(fusion_getter):
        return False, "resolve.Fusion() is not available on this host.", None

    try:
        fusion = fusion_getter()
    except Exception as err:
        return False, f"resolve.Fusion() raised: {err}", None

    run_script_fn = getattr(fusion, "RunScript", None) if fusion is not None else None
    if not callable(run_script_fn):
        return False, "fusion.RunScript() is not available – cannot run the dump worker.", None

    script_path = None
    result_path = os.path.join(tempfile.gettempdir(), f"fusion_dump_result_{uuid.uuid4().hex}.json")
    try:
        script_path = build_dump_worker_script(result_path)
        run_script_fn(script_path)

        data = None
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if os.path.exists(result_path):
                time.sleep(0.15)
                try:
                    with open(result_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    break
                except Exception:
                    pass
            time.sleep(0.2)

        if data is None:
            return False, f"Fusion didn't report back within {timeout_seconds:.0f}s.", None

        if not data.get("ok"):
            return False, data.get("message", "Unknown failure."), None

        out_path = os.path.join(
            os.path.expanduser("~"), "Desktop", f"fusion_node_dump_{uuid.uuid4().hex[:8]}.json"
        )
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data.get("tools"), f, indent=2, default=str)
            return True, f"{data.get('message', 'Done.')} Saved to: {out_path}", out_path
        except Exception:
            return True, data.get("message", "Done.") + " (Could not save to Desktop.)", None
    finally:
        for p in (script_path, result_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
