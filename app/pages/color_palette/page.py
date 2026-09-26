#!/usr/bin/env python3
"""
Color Palette Manager - palettes, image extraction, a visualizer and
contrast/export tools. A web page (core/web_page.py): the view is
web/index.html + palette.js; everything it shows comes from view.py and
images.py, and every change is made here.

A general colour tool - it never reads or writes a Resolve project. Data
is the standalone's own DataManager (~/.color_palette_manager/), so
everything saved there is here.

What moved to the page, and what didn't:
  - Palettes, Generators, Extract, Visualize and Tools are the page's
    tabs. The UI mockup cards and the contrast checker (separate windows
    in the standalone) are part of Visualize and Tools; the six generators
    (harmony, theme, gradient, grayscale, neon/pastel, glass) open inside
    Generators, from
    generators.py, one of each kept for the session.
  - Colours are picked in the page's own picker; its Dropper, and the
    palettes' Screen Dropper, are screen_dropper.ScreenColorDropper.
  - The mini palette window floats over Resolve, so it's a window of its
    own - a web one (mini_palette_window.py, web/mini/).
  - Settings (language, focus overlay, mini window transparency, backup)
    are settings_panel.py, in the shell's Settings dialog.

The focus overlay (a full-screen dimming window behind the app) only shows
while this page is on screen - left on, it would dim the desktop behind
every other Buddy tool too.

Protocol:
    to the view    strings, state, library, history, extract, visualize,
                   tools, generator, picked, import_name, alert, toast
    from the view  tab, vision, search, toggle_palette, toggle_folder,
                   new_palette, new_folder, rename_palette, delete_palette,
                   rename_folder, delete_folder, move_palette, set_tags,
                   save_version, history, restore_version, add_color,
                   change_color, remove_color, undo_remove, move_color, copy,
                   dropper, mini, generator, gen, browse_image, paste_image,
                   drop_link, clear_image, extract_options, add_extracted,
                   create_from_image, export_strip, export_false_color,
                   vis_palette, vis_options, set_slot, swap_slots,
                   reset_slots, export_visualizer, import_file, import_commit,
                   export_options, export_file, copy_text, contrast_set,
                   contrast_palette, contrast_swap
"""

import os
import tempfile

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

from core.web_page import WebToolPage

from . import generators, images, view
from .color_engine import (
    EXPORT_FORMATS, _export_slug, apply_colorblind_filter, export_gradient_png, extract_from_image,
    extraction_image, import_palette_colors, relative_luminance, render_fixed_shape_visualization_image,
)
from .data_manager import DataManager
from .i18n import TRANSLATIONS, get_i18n, tr
from .settings_panel import ColorPaletteSettingsMixin

TABS = ("palettes", "generators", "extract", "visualize", "tools")
IMPORT_FILE_FILTER = (
    "Palette Files (*.json *.gpl *.ase *.aco *.swatches *.css *.scss *.txt *.js);;"
    "All Files (*)"
)
EXTRACT_DEFAULTS = {"count": 6, "weight": 0, "weighing": "Average", "false_color": False}


class _FetchWorker(QThread):
    """Downloads a dragged or pasted web image to a temp file."""

    fetched = Signal(int, str, str)   # generation, path, error

    def __init__(self, generation, link):
        super().__init__()
        self.generation = generation
        self.link = link

    def run(self):
        try:
            self.fetched.emit(self.generation, images.fetch_to_temp(self.link), "")
        except Exception as exc:  # noqa: BLE001 - reported to the user
            self.fetched.emit(self.generation, "", str(exc) or type(exc).__name__)


class ColorPalettePage(ColorPaletteSettingsMixin, WebToolPage):
    tool_id = "color_palette"
    display_name = "Color Palette Manager"
    category = "Editing Tools"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
    file_drops = True

    def build_state(self):
        self.data_mgr = self._make_data_manager()
        self._load_warnings = list(self.data_mgr.load_warnings)
        # Vision simulation is per session, as in the standalone (which
        # reset it on every launch). The generators use the page's.
        self.data_mgr.settings["colorblind_mode"] = "None"
        if not self.data_mgr.palettes:
            self.data_mgr.palettes["Default"] = []

        self.app = QApplication.instance()
        self.i18n = get_i18n()
        self.i18n.language = self.data_mgr.settings.get("language", "English")
        self.i18n.language_changed.connect(self._on_language_changed)

        self.tab = "palettes"
        # Palettes: one open (expanded) palette at a time - the "active"
        # palette that Extract's "Add to" and new colours go to.
        self.query = ""
        self.open_folders = set()
        self.current = next(iter(self.data_mgr.palettes))
        self.expanded = True
        self._last_removed = None

        self.image_path = None
        self._preview = None      # PIL, at most images.PREVIEW_SIZE
        self._work = None         # PIL, the small copy extraction runs on
        self.extracted = []
        self.extract_opts = dict(EXTRACT_DEFAULTS)
        self._fetch_generation = 0
        self._fetching = None     # the link being downloaded
        self._workers = []
        self._temp_files = []

        self.vis_palette = self.current
        self._snapshots = {}      # palette -> slot overrides when first shown this session
        self.export_palette = self.current
        self.export_format = next(iter(EXPORT_FORMATS))
        self.contrast_text = "#FFFFFF"
        self.contrast_bg = "#1C1C1C"
        self.contrast_palette = self.current
        self._pending_import = None

        self.focus_overlay = None
        self.mini_windows = {}
        self.generators = {}      # id -> generators.<Kind>, made when first opened
        self.gen_open = None      # the one the Generators tab shows, or None for the list
        self.gen_source = None    # the palette its "drag from" strip shows
        self.dropper = None

    def _make_data_manager(self):
        return DataManager()

    def web_ready(self):
        self._push_strings()
        self._push_all()

    def on_shown(self):
        if self._load_warnings:
            warnings, self._load_warnings = self._load_warnings, []
            self.emit("alert", {"title": tr(self.display_name), "text": "\n\n".join(warnings)})

    def on_app_quitting(self):
        self.data_mgr.save_settings()
        if self.focus_overlay:
            self.focus_overlay.close()
        for worker in self._workers:
            worker.wait(2000)
        for path in self._temp_files:
            self._remove_temp(path)

    # ------------------------------------------------------------- pushes --

    @property
    def vision(self):
        return self.data_mgr.settings.get("colorblind_mode", "None")

    def _names(self):
        return list(self.data_mgr.palettes)

    def _valid(self, name):
        return name if name in self.data_mgr.palettes else next(iter(self.data_mgr.palettes))

    def _push_strings(self):
        lang = self.i18n.language
        strings = {} if lang == "English" else {
            en: entry[lang] for en, entry in TRANSLATIONS.items() if lang in entry}
        self.emit("strings", {"language": lang, "strings": strings})

    def _push_state(self):
        self.emit("state", {"tab": self.tab, "vision": self.vision, "modes": view.VISION_MODES})

    def _push_all(self):
        self._push_state()
        self._push_library()
        self._push_extract()
        self._push_visualize()
        self._push_tools()
        self._push_generator()

    def refresh_all(self):
        """After any change to the palettes."""
        if self.current not in self.data_mgr.palettes:
            self.current = self._valid(self.current)
        self._push_library()
        self._push_extract()
        self._push_visualize()
        self._push_tools()
        self._push_generator()
        for win in self.mini_windows.values():
            win.refresh()

    def on_theme_changed(self):
        super().on_theme_changed()
        for win in self.mini_windows.values():
            win.on_theme_changed()

    def _push_library(self):
        data = view.library(self.data_mgr, self.query, self.open_folders,
                            self.current if self.expanded else None, self.vision)
        data.update(current=self.current, folders_all=list(self.data_mgr.palette_folders))
        self.emit("library", data)

    def _push_extract(self):
        opts = self.extract_opts
        preview = ""
        if self._preview is not None:
            preview = images.data_url(images.shown_preview(self._preview, opts["false_color"], self.vision))
        self.emit("extract", {
            "has_image": self._preview is not None,
            "name": os.path.basename(self.image_path) if self.image_path else "",
            "suggested": os.path.splitext(os.path.basename(self.image_path))[0] if self.image_path else "",
            "preview": preview,
            "colors": view.swatches(self.extracted, self.vision),
            "options": opts,
            "fetching": self._fetching,
            "imaging": images.IMAGING,
            "legend": images.false_color_legend(),
            "palettes": self._names(),
            "current": self.current,
        })

    def _push_visualize(self):
        name = self.vis_palette = self._valid(self.vis_palette)
        self._snapshots.setdefault(name, dict(self.data_mgr.visualizer_overrides.get(name, {})))
        slots = view.slot_colors(self.data_mgr, name)
        settings = self.data_mgr.settings
        self.emit("visualize", {
            "palette": name,
            "palettes": self._names(),
            "empty": not self.data_mgr.palettes.get(name),
            "tray": view.swatches(self.data_mgr.palettes.get(name, []), self.vision),
            "treemap": view.treemap(slots, self.vision),
            "mockup": view.mockup(slots, self.vision),
            "show_hex": settings.get("visualize_show_hex", True),
            "show_pct": settings.get("visualize_show_pct", True),
            "can_reset": self.data_mgr.visualizer_overrides.get(name, {}) != self._snapshots[name],
        })

    def _push_tools(self):
        name = self.export_palette = self._valid(self.export_palette)
        colors = self.data_mgr.palettes.get(name, [])
        tray = self.contrast_palette = self._valid(self.contrast_palette)
        self.emit("tools", {
            "palettes": self._names(),
            "palette": name,
            "formats": list(EXPORT_FORMATS),
            "format": self.export_format,
            "colors": view.swatches(colors, self.vision),
            "binary": EXPORT_FORMATS[self.export_format][2],
            "preview": view.export_preview(name, colors, self.export_format),
            "contrast": view.contrast(self.contrast_text, self.contrast_bg),
            "ct_palette": tray,
            "ct_colors": view.swatches(self.data_mgr.palettes.get(tray, []), self.vision),
        })

    def _on_language_changed(self, _lang):
        self._push_strings()
        self._push_all()

    def _alert(self, text, title=None):
        self.emit("alert", {"title": title or tr("Error"), "text": text})

    def _toast(self, text, **extra):
        self.emit("toast", dict(extra, text=text))

    # ------------------------------------------------------ page-wide --

    def on_tab(self, payload):
        tab = (payload or {}).get("tab")
        if tab in TABS:
            self.tab = tab
            self._push_state()

    def on_vision(self, payload):
        mode = (payload or {}).get("mode")
        if mode in view.VISION_MODES and mode != self.vision:
            self.data_mgr.settings["colorblind_mode"] = mode
            self.data_mgr.save_settings()
            self._push_state()
            self.refresh_all()

    def on_copy(self, payload):
        hex_code = view.normalize_hex((payload or {}).get("hex"))
        if hex_code:
            self.copy_to_clipboard(hex_code)

    def on_copy_text(self, payload):
        """An export's text, from the preview's Copy button."""
        text = str((payload or {}).get("text") or "")
        if text:
            self.copy_to_clipboard(text)

    def copy_to_clipboard(self, text):
        QApplication.clipboard().setText(text)

    # ------------------------------------------------------------ palettes --

    def _save_palettes(self):
        self.data_mgr.save_palette_data()
        self.refresh_all()

    def on_search(self, payload):
        self.query = str((payload or {}).get("query") or "")
        self._push_library()

    def on_toggle_palette(self, payload):
        name = (payload or {}).get("name")
        if name not in self.data_mgr.palettes:
            return
        if name == self.current and self.expanded:
            self.expanded = False
        else:
            self.current, self.expanded = name, True
        self._push_library()
        self._push_extract()

    def on_toggle_folder(self, payload):
        name = (payload or {}).get("name")
        if name in self.data_mgr.palette_folders:
            self.open_folders.symmetric_difference_update({name})
            self._push_library()

    def on_new_palette(self, payload):
        name = str((payload or {}).get("name") or "").strip()
        error = view.check_new_name(self.data_mgr.palettes, name)
        if error:
            return self._alert(tr(error))
        self.data_mgr.palettes[name] = []
        folder = (payload or {}).get("folder")
        if folder in self.data_mgr.palette_folders:
            self.data_mgr.move_palette_to_folder(name, folder)
            self.open_folders.add(folder)
        self.current, self.expanded = name, True
        self._save_palettes()

    def on_new_folder(self, payload):
        name = str((payload or {}).get("name") or "").strip()
        error = view.check_new_name(self.data_mgr.palette_folders, name, "folder")
        if error:
            return self._alert(tr(error))
        self.data_mgr.palette_folders[name] = []
        self.open_folders.add(name)
        self.data_mgr.save_palette_folders()
        self._push_library()

    def on_rename_palette(self, payload):
        old = (payload or {}).get("name")
        new = str((payload or {}).get("new") or "").strip()
        if old not in self.data_mgr.palettes or not new or new == old:
            return
        if new in self.data_mgr.palettes:
            return self._alert(tr("A palette with this name already exists."))
        self.data_mgr.rename_palette(old, new)
        for attr in ("current", "vis_palette", "export_palette"):
            if getattr(self, attr) == old:
                setattr(self, attr, new)
        if old in self._snapshots:
            self._snapshots[new] = self._snapshots.pop(old)
        win = self.mini_windows.pop(old, None)
        if win:
            self.mini_windows[new] = win
            win.set_palette_name(new)
        self.refresh_all()

    def on_delete_palette(self, payload):
        name = (payload or {}).get("name")
        if name not in self.data_mgr.palettes:
            return
        if len(self.data_mgr.palettes) <= 1:
            return self._alert(tr("You cannot delete the last remaining palette."), tr("Delete error"))
        self.data_mgr.delete_palette(name)
        self._snapshots.pop(name, None)
        win = self.mini_windows.pop(name, None)
        if win:
            win.close()
        if self.current == name:
            self.current, self.expanded = next(iter(self.data_mgr.palettes)), False
        self.refresh_all()
        self._toast(tr("Deleted '{name}'").format(name=name))

    def on_rename_folder(self, payload):
        old = (payload or {}).get("name")
        new = str((payload or {}).get("new") or "").strip()
        if old not in self.data_mgr.palette_folders or not new or new == old:
            return
        if new in self.data_mgr.palette_folders:
            return self._alert(tr("A folder with this name already exists."))
        # Rebuilt rather than popped and re-added, so the folder keeps its place.
        self.data_mgr.palette_folders = {
            (new if k == old else k): v for k, v in self.data_mgr.palette_folders.items()}
        self.data_mgr.save_palette_folders()
        if old in self.open_folders:
            self.open_folders.discard(old)
            self.open_folders.add(new)
        self._push_library()

    def on_delete_folder(self, payload):
        name = (payload or {}).get("name")
        if name in self.data_mgr.palette_folders:
            del self.data_mgr.palette_folders[name]
            self.data_mgr.save_palette_folders()
            self.open_folders.discard(name)
            self._push_library()

    def on_move_palette(self, payload):
        name = (payload or {}).get("name")
        folder = (payload or {}).get("folder")
        if name not in self.data_mgr.palettes:
            return
        if folder not in self.data_mgr.palette_folders:
            folder = None
        if folder == self.data_mgr.get_palette_folder(name):
            return
        self.data_mgr.move_palette_to_folder(name, folder)
        if folder:
            self.open_folders.add(folder)
        self._push_library()

    def on_set_tags(self, payload):
        name = (payload or {}).get("name")
        if name in self.data_mgr.palettes:
            self.data_mgr.set_palette_custom_tags(name, str((payload or {}).get("tags") or ""))
            self._push_library()

    def on_save_version(self, payload):
        name = (payload or {}).get("name")
        label = str((payload or {}).get("label") or "").strip()
        if name in self.data_mgr.palettes and label:
            self.data_mgr.add_palette_version_snapshot(name, label)
            self._push_library()
            self._toast(tr("Saved version '{label}'").format(label=label))

    def on_history(self, payload):
        name = (payload or {}).get("name")
        if name in self.data_mgr.palettes:
            self.emit("history", {"name": name, "versions": view.versions(self.data_mgr, name, self.vision),
                                  "colors": view.swatches(self.data_mgr.palettes[name], self.vision)})

    def on_restore_version(self, payload):
        name = (payload or {}).get("name")
        history = self.data_mgr.palette_versions.get(name, [])
        index = (payload or {}).get("index")
        if name in self.data_mgr.palettes and isinstance(index, int) and 0 <= index < len(history):
            self.data_mgr.palettes[name] = list(history[index]["colors"])
            self._save_palettes()
            self._toast(tr("Restored '{label}'").format(label=history[index]["label"]))

    def _colors(self, payload):
        name = (payload or {}).get("name")
        return name, self.data_mgr.palettes.get(name)

    def on_add_color(self, payload):
        name, colors = self._colors(payload)
        hex_code = view.normalize_hex((payload or {}).get("hex"))
        if colors is None:
            return
        if not hex_code:
            return self._alert(tr("That isn't a colour - use a hex code like #1A2B3C."))
        if not view.add_color(colors, hex_code):
            return self._toast(tr("{hex} is already in this palette").format(hex=hex_code))
        self._save_palettes()

    def on_change_color(self, payload):
        name, colors = self._colors(payload)
        index = (payload or {}).get("index")
        hex_code = view.normalize_hex((payload or {}).get("hex"))
        if colors is None or not hex_code or not isinstance(index, int) or not 0 <= index < len(colors):
            return
        if hex_code in colors and colors.index(hex_code) != index:
            return self._toast(tr("{hex} is already in this palette").format(hex=hex_code))
        colors[index] = hex_code
        self._save_palettes()

    def on_remove_color(self, payload):
        name, colors = self._colors(payload)
        index = (payload or {}).get("index")
        if colors is None or not isinstance(index, int) or not 0 <= index < len(colors):
            return
        hex_code = colors.pop(index)
        self._last_removed = (name, index, hex_code)
        self._save_palettes()
        self._toast(tr("Removed {hex}").format(hex=hex_code), undo="undo_remove")

    def on_undo_remove(self, _payload=None):
        if not self._last_removed:
            return
        name, index, hex_code = self._last_removed
        self._last_removed = None
        colors = self.data_mgr.palettes.get(name)
        if colors is not None and hex_code not in colors:
            colors.insert(min(index, len(colors)), hex_code)
            self._save_palettes()

    def on_move_color(self, payload):
        name, colors = self._colors(payload)
        src, dst = (payload or {}).get("from"), (payload or {}).get("to")
        if colors is not None and isinstance(src, int) and isinstance(dst, int) and view.move_color(colors, src, dst):
            self._save_palettes()

    # -------------------------------------------------- dropper & windows --

    def on_dropper(self, payload):
        """The screen colour dropper. `for` is "add" (straight into the
        palette `name`) or "picker" (back to the page's colour picker)."""
        target = dict(payload or {})
        from .screen_dropper import ScreenColorDropper

        hide = self.data_mgr.settings.get("hide_on_dropper", True)
        try:
            self.dropper = ScreenColorDropper(
                hide_parent_callback=(lambda: self.set_dropper_hidden(True)) if hide else None,
                show_parent_callback=(lambda: self.set_dropper_hidden(False)) if hide else None)
        except Exception as exc:  # noqa: BLE001 - reported to the user
            self.set_dropper_hidden(False)
            return self._alert(tr("Could not start the screen colour dropper:\n{0}").format(exc))
        # Handled a turn later, once the window is back and settled - doing
        # the redraw as it un-hides flashed it bright.
        self.dropper.color_sampled.connect(
            lambda hex_code: QTimer.singleShot(0, lambda: self._dropped(target, hex_code)))

    def _dropped(self, target, hex_code):
        hex_code = view.normalize_hex(hex_code)
        if not hex_code:
            return
        if target.get("for") == "add":
            self.on_add_color({"name": target.get("name"), "hex": hex_code})
        else:
            self.emit("picked", {"hex": hex_code})

    def set_dropper_hidden(self, hidden):
        """Get Buddy's window out of the way of the screen dropper. Fades
        rather than hides: hiding and re-showing a window on Windows
        flashes it bright for a frame."""
        window = self.window()
        if hidden:
            self._pre_dropper_opacity = window.windowOpacity()
            window.setWindowOpacity(0.0)
        else:
            window.setWindowOpacity(getattr(self, "_pre_dropper_opacity", 1.0))

    def on_mini(self, payload):
        name = (payload or {}).get("name")
        if name not in self.data_mgr.palettes:
            return
        from .mini_palette_window import MiniPaletteWindow
        from .ui_utils import give_own_taskbar_entry

        win = self.mini_windows.get(name)
        if win is None:
            # No Qt parent: a parented top-level is minimized in lockstep
            # with its owner on Windows. It's meant to float on its own.
            win = MiniPaletteWindow(self, name)
            win.closed.connect(self._mini_closed)
            give_own_taskbar_entry(win)
            self.mini_windows[name] = win
        else:
            win.refresh()
        win.show()
        win.raise_()
        win.activateWindow()

    def _mini_closed(self, name):
        self.mini_windows.pop(name, None)

    def apply_mini_palette_transparency(self):
        for win in self.mini_windows.values():
            win.apply_transparency()

    # ---------------------------------------------------------- generators --

    def _generator(self, gid):
        gen = self.generators.get(gid)
        if gen is None and gid in generators.IDS:
            gen = self.generators[gid] = generators.make(gid, self.data_mgr.settings, self.data_mgr.save_settings)
        return gen

    def on_generator(self, payload):
        """Opens one generator in the tab (id), or goes back to the list (none)."""
        gid = (payload or {}).get("id")
        self.gen_open = gid if gid in generators.IDS else None
        if self.gen_open:
            self._generator(self.gen_open)
        self._push_generator()

    def _push_generator(self):
        gen = self.generators.get(self.gen_open) if self.gen_open else None
        if gen is None:
            self.emit("generator", {"open": None})
            return
        vision = self.vision
        data = gen.view(lambda hex_code: view.swatch(hex_code, vision))
        for key in ("title", "create_label", "prompt_title"):
            if data.get(key):
                data[key] = tr(data[key])
        if isinstance(gen, generators.Gradient):
            image = apply_colorblind_filter(gen.render_image(), vision)
            data["image"] = images.data_url(image, quality=92)
            data["weight_label"] = tr(data["weight_label"]) if gen.weight == 0 else data["weight_label"]
        names = self._names()
        if self.gen_source not in self.data_mgr.palettes:
            self.gen_source = self.current if self.current in self.data_mgr.palettes else names[0]
        data.update(target=self.current, palettes=names, source=self.gen_source,
                    source_colors=view.swatches(self.data_mgr.palettes[self.gen_source], vision))
        self.emit("generator", {"open": self.gen_open, "view": data})

    def on_gen(self, payload):
        """One of the open generator's controls."""
        p = dict(payload or {})
        gen = self.generators.get(p.get("id"))
        if gen is None:
            return
        action = str(p.get("action") or "")
        if action == "source":
            if p.get("name") in self.data_mgr.palettes:
                self.gen_source = p["name"]
                self._push_generator()
        elif action == "add" and p.get("base") and hasattr(gen, "base"):
            self._gen_add([gen.base])
        elif action == "add":
            self._gen_add([gen.preview[i] for i in p.get("indexes") or []
                           if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(gen.preview)])
        elif action == "create":
            self._gen_create(gen, str(p.get("name") or "").strip())
        elif action == "export" and isinstance(gen, generators.Gradient):
            self._export_gradient(gen)
        else:
            if action == "add_palette":
                # The colours come from the palette itself, never the view.
                p["colors"] = list(self.data_mgr.palettes.get(p.get("name"), []))
            if gen.act(action, p):
                self._push_generator()

    def _gen_add(self, colors):
        """A generator's colours into the open palette (not already there)."""
        target = self.data_mgr.palettes.get(self.current)
        if target is None or not colors:
            return
        new = [c for c in dict.fromkeys(colors) if c not in target]
        if not new:
            return self._toast(tr("Already in {name}").format(name=self.current))
        target.extend(new)
        self._save_palettes()
        self._toast(tr("Added {count} to {name}").format(count=len(new), name=self.current), show=self.current)

    def _gen_create(self, gen, name):
        colors = gen.result()
        if not colors or not name:
            return
        if name in self.data_mgr.palettes:
            return self._alert(tr("A palette with this name already exists."))
        self.data_mgr.palettes[name] = colors
        self._save_palettes()
        self._toast(tr("Created {name}").format(name=name), show=name)

    def _export_gradient(self, gen):
        path, _ = QFileDialog.getSaveFileName(self, tr("Export gradient PNG"),
                                              self.data_mgr.default_export_path("gradient.png"), "PNG Files (*.png)")
        if not path:
            return
        self.data_mgr.remember_export_folder(path)
        if not path.lower().endswith(".png"):
            path += ".png"
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            export_gradient_png(path, gen.stops(), style=gen.style, angle=gen.angle, color_space=gen.mode,
                                easing=gen.easing, width=gen.png["width"], height=gen.png["height"],
                                bit_depth=gen.png["bit_depth"])
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return self._alert(tr("Failed to save PNG:\n{error}").format(error=exc), tr("Export failed"))
        finally:
            QApplication.restoreOverrideCursor()
        self._toast(tr("Saved {name}").format(name=os.path.basename(path)))

    # ------------------------------------------------------------- extract --

    def on_browse_image(self, _payload=None):
        path, _ = QFileDialog.getOpenFileName(self, tr("Choose image"), "", images.IMAGE_FILTER)
        if path:
            self.load_image(path)

    def on_files_dropped(self, paths):
        pictures = [p for p in paths if images.is_image_path(p)]
        if not pictures:
            return self._toast(tr("That isn't an image this can open."))
        self.tab = "extract"
        self._push_state()
        self.load_image(pictures[0])

    def on_drop_link(self, payload):
        link = str((payload or {}).get("link") or "").strip()
        if images.is_image_link(link):
            self._fetch(link)

    def on_paste_image(self, _payload=None):
        mime = QApplication.clipboard().mimeData()
        if mime.hasImage():
            qimg = QApplication.clipboard().image()
            if not qimg.isNull():
                fd, path = tempfile.mkstemp(suffix=".png", prefix="cpm_pasted_")
                os.close(fd)
                qimg.save(path, "PNG")
                self._temp_files.append(path)
                return self.load_image(path)
        if mime.hasUrls() and mime.urls():
            url = mime.urls()[0]
            if url.isLocalFile():
                return self.load_image(url.toLocalFile())
            return self._fetch(url.toString())
        if mime.hasText() and images.is_image_link(mime.text()):
            return self._fetch(mime.text().strip())
        self._toast(tr("Nothing to paste – copy an image, or a link to one, first."))

    def _fetch(self, link):
        self._fetch_generation += 1
        self._fetching = link if not link.startswith("data:") else tr("a pasted image")
        worker = _FetchWorker(self._fetch_generation, link)
        worker.fetched.connect(self._on_fetched)
        worker.finished.connect(self._reap_workers)
        self._workers.append(worker)
        worker.start()
        self._push_extract()

    def _on_fetched(self, generation, path, error):
        if generation != self._fetch_generation:
            if path:
                self._remove_temp(path)
            return
        self._fetching = None
        if error:
            self._push_extract()
            return self._alert(tr("Could not load image from URL:\n{error}").format(error=error),
                               tr("Download failed"))
        self._temp_files.append(path)
        self.load_image(path)

    def _reap_workers(self):
        self._workers = [w for w in self._workers if w.isRunning()]

    def load_image(self, path):
        if not images.IMAGING:
            return self._alert(tr("Image extraction requires PIL/Pillow."), tr("Unavailable"))
        try:
            preview = images.preview_image(path)
            work = extraction_image(path)
        except Exception as exc:  # noqa: BLE001 - reported to the user
            self._push_extract()
            return self._alert(tr("Could not load image file:\n{error}").format(error=exc))
        old = self.image_path
        self.image_path, self._preview, self._work = path, preview, work
        self._fetching = None
        if old and old != path and old in self._temp_files:
            self._temp_files.remove(old)
            self._remove_temp(old)
        self._extract()

    def on_clear_image(self, _payload=None):
        if self.image_path in self._temp_files:
            self._temp_files.remove(self.image_path)
            self._remove_temp(self.image_path)
        self.image_path = self._preview = self._work = None
        self.extracted = []
        self._push_extract()

    @staticmethod
    def _remove_temp(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def on_extract_options(self, payload):
        opts = self.extract_opts
        payload = payload or {}
        if "count" in payload:
            opts["count"] = max(2, min(12, int(payload["count"])))
        if "weight" in payload:
            opts["weight"] = max(0, min(100, int(payload["weight"])))
        if payload.get("weighing") in ("Average", "Balanced"):
            opts["weighing"] = payload["weighing"]
        if "false_color" in payload:
            opts["false_color"] = bool(payload["false_color"])
        self._extract()

    def _extract(self):
        if self._work is None:
            self.extracted = []
            return self._push_extract()
        opts = self.extract_opts
        try:
            colors = extract_from_image(self._work, opts["count"], opts["weight"] / 100.0, opts["weighing"])
        except Exception as exc:  # noqa: BLE001 - reported to the user
            colors = []
            self._alert(tr("Failed to extract colours:\n{error}").format(error=exc), tr("Extraction error"))
        self.extracted = sorted((c.upper() for c in colors), key=relative_luminance)
        self._push_extract()

    def on_add_extracted(self, payload):
        name = (payload or {}).get("name") or self.current
        hex_code = view.normalize_hex((payload or {}).get("hex"))
        colors = self.data_mgr.palettes.get(name)
        if colors is None or not hex_code:
            return
        if view.add_color(colors, hex_code):
            self._save_palettes()
            self._toast(tr("Added {hex} to '{name}'").format(hex=hex_code, name=name))
        else:
            self._toast(tr("{hex} is already in '{name}'").format(hex=hex_code, name=name))

    def on_create_from_image(self, payload):
        name = str((payload or {}).get("name") or "").strip()
        if not self.extracted:
            return
        error = view.check_new_name(self.data_mgr.palettes, name)
        if error:
            return self._alert(tr(error))
        self.data_mgr.palettes[name] = list(self.extracted)
        self.current, self.expanded = name, True
        self._save_palettes()
        self._toast(tr("Created '{name}'").format(name=name), show=name)

    def _save_path(self, title, filename, file_filter):
        path, _ = QFileDialog.getSaveFileName(
            self, title, os.path.join(self.data_mgr.get_export_start_dir(), filename), file_filter)
        if path:
            self.data_mgr.remember_export_folder(path)
        return path

    def on_export_strip(self, _payload=None):
        if not (images.IMAGING and self.image_path and self.extracted):
            return
        stem = os.path.splitext(os.path.basename(self.image_path))[0]
        out = self._save_path(tr("Export image + palette"), f"{stem}_palette.png", "PNG Image (*.png)")
        if not out:
            return
        try:
            images.save_with_palette(self.image_path, self.extracted, out)
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return self._alert(tr("Could not export composite image:\n{error}").format(error=exc), tr("Export error"))
        self._toast(tr("Saved {filename}").format(filename=os.path.basename(out)))

    def on_export_false_color(self, _payload=None):
        if not (images.IMAGING and self.image_path):
            return
        stem = os.path.splitext(os.path.basename(self.image_path))[0]
        out = self._save_path(tr("Export False Color image"), f"{stem}_falsecolor.png", "PNG Image (*.png)")
        if not out:
            return
        try:
            images.save_false_color(self.image_path, out)
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return self._alert(tr("Could not export False Color image:\n{error}").format(error=exc), tr("Export error"))
        self._toast(tr("Saved {filename}").format(filename=os.path.basename(out)))

    # ----------------------------------------------------------- visualize --

    def on_vis_palette(self, payload):
        name = (payload or {}).get("name")
        if name in self.data_mgr.palettes:
            self.vis_palette = name
            self._push_visualize()

    def on_vis_options(self, payload):
        settings = self.data_mgr.settings
        for key in ("show_hex", "show_pct"):
            if key in (payload or {}):
                settings[f"visualize_{key}"] = bool(payload[key])
        self.data_mgr.save_settings()
        self._push_visualize()

    def on_set_slot(self, payload):
        slot = (payload or {}).get("slot")
        hex_code = view.normalize_hex((payload or {}).get("hex"))
        if isinstance(slot, int) and 0 <= slot < view.SLOT_COUNT and hex_code:
            self.data_mgr.set_visualizer_slot_override(self.vis_palette, slot, hex_code)
            self._push_visualize()

    def on_swap_slots(self, payload):
        a, b = (payload or {}).get("a"), (payload or {}).get("b")
        if not all(isinstance(i, int) and 0 <= i < view.SLOT_COUNT for i in (a, b)) or a == b:
            return
        slots = view.slot_colors(self.data_mgr, self.vis_palette)
        self.data_mgr.set_visualizer_slot_override(self.vis_palette, a, slots[b])
        self.data_mgr.set_visualizer_slot_override(self.vis_palette, b, slots[a])
        self._push_visualize()

    def on_reset_slots(self, _payload=None):
        name = self.vis_palette
        if name in self._snapshots:
            self.data_mgr.visualizer_overrides[name] = dict(self._snapshots[name])
            self.data_mgr.save_visualizer_overrides()
            self._push_visualize()

    def on_export_visualizer(self, _payload=None):
        name = self.vis_palette
        if not self.data_mgr.palettes.get(name):
            return
        out = self._save_path(tr("Export visualizer PNG"), f"{name}_visualizer.png", "PNG Image (*.png)")
        if not out:
            return
        settings = self.data_mgr.settings
        img = render_fixed_shape_visualization_image(
            view.slot_colors(self.data_mgr, name), vision_mode=self.vision,
            show_hex=settings.get("visualize_show_hex", True), show_pct=settings.get("visualize_show_pct", True),
            width=1200, height=720)
        try:
            img.save(out, "PNG")
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return self._alert(tr("Could not save visualizer image:\n{error}").format(error=exc), tr("Export error"))
        self._toast(tr("Saved {filename}").format(filename=os.path.basename(out)))

    # --------------------------------------------------------------- tools --

    def on_import_file(self, _payload=None):
        path, _ = QFileDialog.getOpenFileName(self, tr("Import palette file"), "", IMPORT_FILE_FILTER)
        if not path:
            return
        try:
            colors = import_palette_colors(path)
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return self._alert(tr("Could not read this palette file:\n{error}").format(error=exc), tr("Import error"))
        self._pending_import = colors
        self.emit("import_name", {
            "suggested": os.path.splitext(os.path.basename(path))[0],
            "file": os.path.basename(path),
            "colors": view.swatches(colors, self.vision),
            "existing": {n: len(c) for n, c in self.data_mgr.palettes.items()},
        })

    def on_import_commit(self, payload):
        name = str((payload or {}).get("name") or "").strip()
        colors, self._pending_import = self._pending_import, None
        if colors is None or not name:
            return
        if name in self.data_mgr.palettes and not (payload or {}).get("overwrite"):
            return
        self.data_mgr.palettes[name] = colors
        self.export_palette = name
        self._save_palettes()
        self._toast(tr("Imported {count} colour(s) into '{name}'.").format(count=len(colors), name=name), show=name)

    def on_export_options(self, payload):
        payload = payload or {}
        if payload.get("palette") in self.data_mgr.palettes:
            self.export_palette = payload["palette"]
        if payload.get("format") in EXPORT_FORMATS:
            self.export_format = payload["format"]
        self._push_tools()

    def on_export_file(self, _payload=None):
        name = self.export_palette
        colors = self.data_mgr.palettes.get(name, [])
        if not colors:
            return self._alert(tr("This palette has no colours."), tr("Export info"))
        ext, generator, is_binary = EXPORT_FORMATS[self.export_format]
        out = self._save_path(tr("Export palette ({format})").format(format=self.export_format),
                              f"{_export_slug(name)}{ext}", f"Files (*{ext})")
        if not out:
            return
        content = generator(name, colors)
        try:
            if is_binary:
                with open(out, "wb") as f:
                    f.write(content)
            else:
                with open(out, "w", encoding="utf-8") as f:
                    f.write(content)
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return self._alert(tr("Could not write file:\n{error}").format(error=exc), tr("Export error"))
        self._toast(tr("Exported '{name}' to {filename}").format(name=name, filename=os.path.basename(out)))

    def on_contrast_set(self, payload):
        hex_code = view.normalize_hex((payload or {}).get("hex"))
        which = (payload or {}).get("which")
        if not hex_code or which not in ("text", "bg"):
            return
        if which == "text":
            self.contrast_text = hex_code
        else:
            self.contrast_bg = hex_code
        self._push_tools()

    def on_contrast_palette(self, payload):
        name = (payload or {}).get("name")
        if name in self.data_mgr.palettes:
            self.contrast_palette = name
            self._push_tools()

    def on_contrast_swap(self, _payload=None):
        self.contrast_text, self.contrast_bg = self.contrast_bg, self.contrast_text
        self._push_tools()

    # ------------------------------------------------------- focus overlay --

    def update_focus_overlay(self):
        enabled = self.data_mgr.settings.get("focus_mode_enabled", False)
        if not enabled or not self.isVisible():
            if self.focus_overlay:
                self.focus_overlay.close()
                self.focus_overlay = None
            return

        opacity = float(self.data_mgr.settings.get("focus_mode_opacity", 0.85))
        val = int(self.data_mgr.settings.get("focus_mode_color_val", 128))
        if not self.focus_overlay:
            self.focus_overlay = QWidget(
                None, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnBottomHint | Qt.Tool)
            self.focus_overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        window = self.window()
        screen = window.screen() or self.app.primaryScreen()
        self.focus_overlay.setGeometry(screen.geometry())
        self.focus_overlay.setStyleSheet(f"background-color: #{val:02x}{val:02x}{val:02x};")
        self.focus_overlay.setWindowOpacity(opacity)
        self.focus_overlay.show()
        window.raise_()

    def showEvent(self, event):
        super().showEvent(event)
        self.update_focus_overlay()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self.focus_overlay:
            self.focus_overlay.close()
            self.focus_overlay = None
