# DaVinci Resolve Scripting API — Field Guide

What Buddy's tools have learned about Resolve's scripting API, and the
traps that aren't in Blackmagic's own reference (the `README.txt` in
Resolve's `Support\Developer\Scripting` folder). Read it before wiring a
new tool into Resolve or changing how an existing one talks to it.

Buddy doesn't read this file; it's for people working on the code.

---

## 1. Decision tree

- **A one-shot action on the open project** (import media, rename clips,
  make bins, export stills)? → Use the shell's shared in-process
  connection: `host.ensure_connected()` gives a `ResolveController`
  (`app/core/resolve_bridge.py`).
- **Polling Resolve on a timer for as long as Buddy is open** (Time
  Tracker)? → Do each check in a **short-lived subprocess**. See §3.
- **Manipulating Fusion's node graph** (Text+, BezierSpline, pasted
  shapes), not just the project / media pool / timeline API? → Read §5
  first. It's a much rougher API surface.

---

## 2. Connecting in-process

Buddy is a persistent app: it can be open before Resolve is, or after
Resolve has quit. A script launched from Resolve's Workspace > Scripts menu
can assume Resolve is running; Buddy can't. So `core/resolve_bridge.py`
first **probes** in a disposable child process (`core/resolve_probe_worker.py`,
exit code only), and imports `DaVinciResolveScript` in-process only once
that probe says Resolve is up. From then on, per-action calls go straight
through the in-process controller.

The bootstrap every connection shares:

```python
import os, sys

def _bootstrap_resolve_env():
    if "RESOLVE_SCRIPT_API" not in os.environ:
        os.environ["RESOLVE_SCRIPT_API"] = os.path.expandvars(
            r"%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
        )
    if "RESOLVE_SCRIPT_LIB" not in os.environ:
        os.environ["RESOLVE_SCRIPT_LIB"] = (
            r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
        )
    modules_dir = os.path.join(os.environ["RESOLVE_SCRIPT_API"], "Modules")
    if modules_dir not in sys.path:
        sys.path.append(modules_dir)
    # The bridges carry macOS paths too; they haven't been verified on a Mac.

import DaVinciResolveScript as dvr_script
resolve = dvr_script.scriptapp("Resolve")
```

Requires **Resolve Preferences > System > General > "External scripting
using" = Local**. It's the first thing to check when a connection fails
silently.

Which Resolve allows which connection:

- **Studio:** both. That covers Buddy launched from Workspace > Scripts
  (inside `fuscript.exe`) and a Buddy started outside Resolve (Start menu or
  desktop shortcut, the "start with Resolve" watcher). It also covers Buddy's
  own helper processes, such as Transcribe's `resolve_child.py`.
- **Free, up to 21.0.4:** only a Buddy launched from Workspace > Scripts.
  Nothing started outside Resolve can connect.
- **Free, 21.1 and later:** none. From 21.1 on, only Studio supports Python
  scripting, so Workspace > Scripts doesn't run Buddy either.

**Every step in the chain can return `None` instead of raising:**

```
resolve.GetProjectManager()          -> can be None
  .GetCurrentProject()               -> can be None (no project open)
    .GetMediaPool()                  -> can be None
    .GetCurrentTimeline()            -> can be None (no active timeline)
```

Null-check each hop and raise a `ResolveConnectionError` with a specific,
user-facing message ("No project is open in Resolve.", "No timeline is
open.") rather than letting a raw `None.GetX()` `AttributeError` bubble up.

### Launched from the Scripts menu

Buddy is deployed as `Buddy.py` plus `buddy.zip` in Resolve's
`Scripts\Utility` folder, and runs inside Resolve's script host
(`fuscript.exe`). That environment has quirks:

- Resolve runs scripts via something like `exec()` **without a real
  `__file__`**. The launcher falls back to
  `inspect.currentframe().f_code.co_filename`, then to a hard-coded path
  under `.../Scripts/Utility/`.
- No console is attached. Fatal errors must go to a message box and/or a
  log file under `%TEMP%`, never just a traceback on stderr. A native crash
  leaves nothing at all unless `faulthandler` is writing somewhere
  (`core/crash_log.py` → `~/.buddy/crash.log`).
- `Buddy.py` extracts the zip to `%TEMP%\Buddy\<hash of the zip's size and
  mtime>\`, so a new zip gets a fresh extraction automatically, puts it on
  `sys.path`, then runs `import main; main.main()`.

---

## 3. Polling through a subprocess

**The crash this avoids:** connecting to Resolve's scripting DLL
(`fusionscript.dll`) when Resolve isn't reachable can **hard-crash the
Python interpreter**: a native segfault, not a catchable exception. A
background poller running for hours will hit that state (Resolve closing,
restarting, sitting at the project manager).

How Time Tracker does it (`pages/time_tracker/resolve_bridge.py`,
`resolve_poll_worker.py`):

- The bridge never imports `DaVinciResolveScript` itself. It runs
  `sys.executable resolve_poll_worker.py` with `subprocess.run(...,
  timeout=...)` on a plain background `threading.Thread`, not `QProcess`,
  which can't take the custom `STARTUPINFO` flags below.
- The **worker** connects and queries in its own short-lived process:
  bootstrap → `scriptapp("Resolve")` → `GetProjectManager()` →
  `GetCurrentProject()` → `GetName()`, prints just the project name and
  exits 0/1.
- If the worker crashes, only that child dies. The parent sees empty
  stdout or a non-zero exit and treats the poll as "no project".
- **Results come back through stdout and the exit code only.** Take the
  **last non-blank line** of stdout, not the whole capture: Resolve's
  scripting library prints a startup banner to stdout the first time a
  process connects.
- The thread reports through a Qt `Signal(object, bool)`, which Qt delivers
  on the GUI thread.
- Set `STARTUPINFO` flags `_STARTF_FORCEOFFFEEDBACK` (0x80) and
  `CREATE_NO_WINDOW`. Otherwise Windows' "app starting" cursor spins again
  on every poll.
- **No `tasklist` "is Resolve.exe running" pre-check.** It would save a
  process per poll, but inside Resolve's script host a bare `tasklist`
  lookup can fail to see a Resolve that is running, which would report
  "offline" forever. Every poll runs the real worker.

**"Untitled Project":** `GetCurrentProject()` returns Resolve's default
"Untitled Project" while Resolve sits at the Project Manager, not just
inside a real project. Checking `GetCurrentPage()` to tell the two apart
made Resolve itself laggy and broke detection, so the rule is simply: a
project named exactly `"Untitled Project"` counts as no project. Cheap, no
extra API call; the cost is that a real project with that name is ignored.

---

## 4. Project / media pool / timeline API: gotchas by function

| Call | Gotcha |
|---|---|
| `GetCurrentProject()` / `GetMediaPool()` / `GetCurrentTimeline()` | Return `None` on failure, never raise. Null-check every hop. |
| `media_pool.ImportMedia(paths)` | Returns falsy/empty on failure with **no error detail**: it could mean bad paths, or that the target bin wasn't current when called. |
| `media_pool.AddSubFolder(parent, name)` | Can return `None` (Resolve refused to create the bin). Don't assume success. |
| `media_pool.SetCurrentFolder(folder)` | Returns a bool that's easy to ignore; a `False` here explains a later `ImportMedia` failure. |
| `bin.GetClipList()` / `GetSelectedClips()` | **Include the project's timelines as pseudo-clips**, indistinguishable from footage by `has_video`/`has_audio` (a timeline reports `Audio Ch > 0`). Tell them apart with `clip.GetClipProperty("Type") != "Timeline"`, or renumbering and renaming get thrown off. |
| `GetSelectedClips()` | **Missing on some older Resolve versions.** Wrap it in try/except and fall back to "all clips in the bin". |
| `clip.GetClipProperty(...)` | Can throw. If it does, treat the clip as real media: better to include something than silently drop it. |
| `timeline.GetSetting("timelineFrameRate")` | Returns a **string**; cast it with `float()`. |
| `timeline.AddMarker(frame, color, name, note, duration, customData)` | Returns a bool. Resolve **silently refuses a second marker on a frame that already has one**, which is the usual cause of `False`. |
| `timeline.GetMarkers()` | Can return `None` instead of `{}`. Keys are frame numbers. |
| `timeline.GrabStill()` | Reliable only after `resolve.OpenPage("color")`; re-fetch the timeline object after switching pages. Returns falsy per frame on failure: log it and carry on rather than abandoning the batch. |
| `gallery.GetCurrentStillAlbum()` / `album.ExportStills(...)` / `album.DeleteStills(...)` | All return booleans. Treat a failed export as fatal, a failed delete as a warning (a failed clean-up shouldn't undo a good export). |
| `clip.LinkProxyMedia(path)` | Returns `False` with no reason when Resolve won't take the proxy - the manual's requirements are a **supported codec** and the **same frame rate** as the source, and (measured on 21.1) the **same start timecode** as the clip's `Start TC`. ffmpeg drops a Sony MP4's timecode (it's on the `rtmd` track), so stamp `-timecode`. Report per clip and carry on (see `pages/project_setup/proxy.py`). |

---

## 5. Fusion node-graph API: much rougher

This applies only when manipulating Fusion's comp/tool graph directly
(Text Animator's Text+ work, SVG Importer's shapes) rather than the
project-level API above.

### Getting the right comp

There are **two unrelated ways** to reach a comp, and mixing them up is
the most common failure:

- `resolve.Fusion()` → `fusion.GetCurrentComp()` returns something only
  once that comp is the **active tab on the Fusion page**. It knows nothing
  about the Edit page's selection.
- `TimelineItem.GetFusionCompByIndex()` reaches a specific clip's comp
  from the Edit page, whatever the Fusion page shows. Use it for "the clip
  the user has selected" workflows.

`GetCurrentComp()` returning `None` almost always means the user isn't on
the Fusion page, not that the connection failed.

### Remote-object pitfalls

- **`hasattr()` lies.** A missing attribute on a Resolve/Fusion remote
  object returns `None` rather than raising, so `hasattr(obj, name)` is
  almost always `True`. Use `callable(getattr(obj, name, None))`.
- **Remote objects are unhashable** (`PyRemoteObject` raises on `hash()`).
  Don't use them as dict keys or set members; build an identity tuple from
  readable properties. Unit-test mocks (plain Python objects hash fine)
  never catch this.
- **Multi-value returns can be mangled over the external bridge.**
  `flow.GetPos(tool)`, documented to return an (x, y) tuple, comes back as
  a bare float. `Paste()` from a bridge connection silently does nothing
  beyond a small graph (a 44-tool graph pasted nothing, every time). The
  same calls behave correctly **inside Fusion's own interpreter**: run a
  worker script with `fusion.RunScript(path)` (the same context as a
  Workspace > Scripts script) and have it write its results to a temp JSON
  file that the app reads.
- **`fusion.ActionManager.GetActions()` isn't a usable automation
  surface.** Nearly all of its ~1100 actions come back as empty
  placeholders over the bridge, and pushing on it can destabilise the
  scripting connection until it crashes. UI automation (e.g. pywinauto)
  doesn't work against Resolve's themed Qt menus either.

### Editing the node graph

- `comp.AddTool("Follower")` returns `None`: a dead end for per-character
  or per-word text animation.
- **Keyframing that works:**
  1. `spline = comp.AddTool("BezierSpline")`
  2. `getattr(text_tool, input_name).ConnectTo(spline.Value)`: connect to
     the spline's **`Value` output**; connecting to the tool itself
     returns `False`.
  3. `getattr(text_tool, input_name)[frame] = value`: bracket-index the
     **connected input**, as in Fusion's own examples
     (`comp.Merge1.Blend[1] = 1`).
  - Repeated `SetInput(param, value, time)` calls do **not** build a
    curve: reading back returns the last value whatever the time.
  - `spline.SaveSettings()['KeyFrames']` doesn't reliably show keyframes
    that do work in a render. Treat "no exception" as success rather than
    reading the settings back.
  - Re-animating a clip without first removing what's connected to the
    input **piles up stray keyframes**: disconnect and delete the old
    spline first.
  - Animatable Text+ inputs: `LayoutSize` (not `TransformSize`) for scale,
    `Opacity1` (not `Opacity`/`Blend`).
  - Text+ `Size` is a fraction of the **composition width**, not height
    (`rendered cap height ≈ 0.4444 × Size × width`).
- **Pasting drops cross-group connections.** Fusion's clipboard text (Lua
  table syntax) silently loses any `Inputs` entry that references a node
  across a group boundary. That's a Fusion bug; verify wiring after a paste
  when groups are involved.
- **A group's output pin is `Output1`, not `Output`.** Connecting to the
  wrong name fails silently: no error, just a missing input downstream.
- None of Buddy's tools use `comp.Lock()`/`Unlock()` or
  `StartUndo()`/`EndUndo()`; they make plain `SetInput` calls and keep
  their own undo stacks.
- **Streaming UI changes will crash Resolve.** The API can't take
  real-time (60 fps) updates: push `SetInput` calls at discrete moments
  (e.g. on mouse release), never continuously (e.g. on every mouse move).
- A title inserted with `InsertFusionTitleIntoTimeline` doesn't register a
  reusable `MediaPoolItem`, and making one with `CreateCompoundClip` breaks
  things. Text Animator imports a Text+ template bin
  (`pages/text_animator/text_plus_template.drb`) instead.
- To match Resolve's own SVG/shape import, diff against a real native
  import captured with Fusion's "Dump Selected Node Settings". Guessing from
  a tool's inputs is unreliable (an unused `Polyline2` input looks like the
  mask shape and isn't).

---

## 6. Python and pip under Resolve

- `sys.executable` inside a Resolve-launched script is usually
  **`fuscript.exe`**, Resolve's script runner, not a general Python.
  `fuscript.exe -m pip install ...` fails with `pip.lua not found`.
- Install packages into the **real `python.exe`** Resolve uses. That's
  typically a per-user install, not the Python on `PATH`, for example:
  ```
  C:\Users\<you>\AppData\Local\Python\pythoncore-3.14-64\python.exe
  ```
  Confirm the path first (Buddy's launcher prints `sys.executable` in its
  error dialog and `%TEMP%\buddy_launch.log`), then:
  ```
  "<that path>\python.exe" -m pip install PySide6
  ```

---

## 7. Checklist for a new Resolve-connected tool

- [ ] Go through the shell's connection (`host.ensure_connected()`) for
      one-shot actions; don't import `DaVinciResolveScript` in a page.
- [ ] Null-check every hop: `GetProjectManager()` → `GetCurrentProject()`
      → `GetMediaPool()` / `GetCurrentTimeline()`, with a specific,
      user-facing error for each.
- [ ] Polling in the background? Use the subprocess pattern in §3.
- [ ] Touching the Fusion node graph? Read §5 in full first. Several of its
      traps (unhashable objects, `hasattr()`, mangled multi-value returns)
      are invisible to unit tests with plain Python mocks.

---

## Appendix: where each tool talks to Resolve

| Tool | Connection | Notable | Code |
|---|---|---|---|
| Shell | probe subprocess, then in-process | One shared controller for every page | `core/resolve_bridge.py`, `core/resolve_probe_worker.py` |
| Asset Manager | in-process | Imports files, optionally into a new bin | `pages/asset_manager/resolve_ext.py` |
| Project Setup | in-process | Bins, folder import, timeline population, sync, proxy rendering | `pages/project_setup/resolve_ext.py`, `pages/project_setup/proxy.py` |
| Batch Clip Renamer | in-process | Timeline pseudo-clips in bin listings; `GetSelectedClips()` differs by version | `pages/batch_clip_renamer/resolve_ext.py` |
| Image Importer | in-process | `ImportMedia` failures give no reason (bad path vs. bin not current) | `pages/image_importer/resolve_ext.py` |
| Media Relink | in-process | Finds and relinks offline clips | `pages/media_relink/resolve_ext.py` |
| Stills Exporter | in-process | `GrabStill()` needs `OpenPage("color")` first; duplicate markers are silently refused | `pages/stills_exporter/resolve_ext.py` |
| YouTube Chapters | in-process | Reads the open timeline's markers | `pages/youtube_chapters/resolve_ext.py` |
| Transcribe | in-process | Renders timeline audio, writes subtitle tracks | `pages/transcribe/resolve_ext.py` |
| Time Tracker | **subprocess per poll** | Crash isolation; "Untitled Project" rule | `pages/time_tracker/resolve_bridge.py`, `resolve_poll_worker.py` |
| SVG Importer | in-process, plus `RunScript` worker | `Paste()` size limit, mangled multi-value returns, `Output1`, cross-group paste bug | `pages/svg_importer/engine.py` |
| Text Animator | in-process, plus `RunScript` for diagnostics | Keyframing via `BezierSpline`; `hasattr()`/unhashable pitfalls; 60 fps crash | `pages/text_animator/` |
| Color Palette Manager | none | No scripting API use | — |
