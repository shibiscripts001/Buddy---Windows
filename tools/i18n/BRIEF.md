# Translating a part of Buddy

Repo: this repository (PySide6 desktop app; every screen is a web page in QtWebEngine).
Helper files are in `tools/i18n/` (called SCRATCH below).

Buddy now has an app-wide language setting. Read `app/core/i18n.py` (module docstring + the functions at the
bottom; skim a few TRANSLATIONS entries for style) and the "Languages" block near the top of `app/web/buddy.js`
before starting. The short version:

- **Code keeps writing English.** buddy.js receives the chosen language's strings and translates every web page's
  text nodes and `title` / `placeholder` / `aria-label` / `alt` attributes as they're drawn (a MutationObserver
  catches text added later). Python strings sent to a page with `self.emit(...)` (toasts, alerts, labels, status
  lines, settings fields) are translated the same way, once they're in the DOM.
- A text node is matched **as a whole** (whitespace runs collapsed, trimmed). `{name}` placeholders in a key stand
  for values that change: key `"Deleted '{name}'"` translates the text node `Deleted 'Sunset'` and keeps `Sunset`.
  Also automatic: `"..."` = `"…"`, `"Name:"` found from key `"Name"` (and vice versa), `"Rename…"` from `"Rename"`.
- Never translated: inputs/textarea values, contenteditable, `<code>`/`<kbd>` text, anything inside an element with
  `translate="no"`.

## Your job, for the files you were given

1. **Find every user-visible English string**: HTML text and attributes, strings the JS draws (`el(..., {text:
   ...})`, `textContent =`, `title:`, `label:`, `placeholder:`, toasts, `Buddy.modal` / `Buddy.confirm` / `Buddy.menu`
   items, status lines, empty states), and Python strings that reach the screen (`self.emit` payloads, alert/confirm
   titles and texts, `ui.status`, `ui.alert`, settings fields from `settings_fields()` built with
   `core/settings_form.py`, error messages that end up displayed, busy-overlay messages given to
   `host.set_busy(True, "...")` / `pump_busy` / `set_busy_message`, tray `host.notify(title, message)`).
   `SCRATCH\candidates\<tool>.txt` is a rough machine-made starting list (file, repr of string) - noisy and
   incomplete; the code is the truth.
   **Leave out**: log/print/crash_log text, prompts sent to an LLM, identifiers, settings keys, event/action names,
   CSS, file extensions, URLs, timecodes, pure product names (Buddy, DaVinci Resolve, Resolve, Fusion, Text+, YouTube,
   Whisper, OTIO, SRT, ffmpeg...) and strings already covered (check with
   `python -c "import sys; sys.path.insert(0, 'app'); from core.i18n import translate; print(translate('Your text', '日本語'))"`
   from the repo root - it prints the English back if there's no translation).

2. **Write keys the way the text node will read on screen.** Where code builds a string from pieces, the key is
   the finished sentence with `{placeholders}` for the changing parts - e.g. JS `` `${n} markers found` `` or
   `n + " markers found"` → key `"{count} markers found"`; Python `f"Could not save {path}:\n{exc}"` → key
   `"Could not save {path}:\n{error}"`. Placeholder names: short, meaningful, `\w+` only, unique within a key.
   A key must have at least 2 letters outside its placeholders.

3. **Fix code where the text can't be matched or would be translated wrongly** (only in your files; keep the
   English wording the same unless a restructure needs it):
   - English plural/grammar glued from fragments (`` `${n} clip${n === 1 ? "" : "s"}` ``, `"Found " + what`)
     → whole alternatives: `` n === 1 ? "1 clip" : `${n} clips` `` (keys `"1 clip"`, `"{count} clips"`).
   - One sentence split over several text nodes/elements (`"Found "`, `<b>3</b>`, `" files"`) → make it one text
     node if you can, or give each piece a key that works standalone.
   - Inserted English words that vary (`` `Choose ${kind} files` `` where kind is "SVG"/"image") → a key per variant
     or a template whose placeholder is only ever a name/number.
   - **User text** (names people typed or chose: clips, bins, timelines, tracks, palettes, projects, files,
     folders, paths, people's names, chat messages, model answers, anything from the Resolve project or disk) must
     not be translated even if it happens to equal a key: put `translate: "no"` on the element in JS
     (`el("span.name", {text: clip.name, translate: "no"})`), `translate="no"` in HTML, or on the container
     (a list of user items). A `<select>` listing user data gets `translate="no"` on the select/options; a
     Settings `sf.select(...)` whose option labels are user data gets `raw=True`.
   - JS logic that compares displayed text (`btn.textContent === "Start"`) must compare state/data attributes
     instead, since the text will be translated.
   - Text drawn on a `<canvas>` or measured before display: wrap in `Buddy.t("...")`.
   - **Qt-native text** (not web): `QFileDialog.getOpenFileName(self, "Caption", ...)` captions → `tr("Caption")`,
     filter strings → `tr_filter("Images (*.png)")` (both from `core.i18n`), f-string captions →
     `tr("Choose the file for {name}").format(name=...)`. Also QAction/QMenu labels and `setWindowTitle` on
     non-WebDialog windows. (WebDialog titles, busy overlay and `host.notify` are already translated centrally -
     just include their keys.)

4. **Translate** every key into all 8 languages, into ONE new file `app/core/translations/<your-name>.json`
   (name given in your task), shape:
   ```json
   {
     "_comment": "Batch Clip Renamer. English -> each language; see core/i18n.py.",
     "Rename clips": {"日本語": "...", "Español": "...", "Deutsch": "...", "Français": "...", "한국인": "...",
                      "中文": "...", "العربية": "...", "Tiếng Việt": "..."}
   }
   ```
   Language keys exactly as above (`한국인` is Korean, `中文` is Simplified Chinese). Style: match the existing
   TRANSLATIONS - German formal "Sie", French "vous", Spanish "tú", Japanese polite です/ます, Korean 해요/합니다
   polite, Chinese 您 where a pronoun is needed, Arabic MSA, Vietnamese natural. Keep every `{placeholder}`
   exactly (you may move it), keep `\n` line breaks, keep product names, keyboard keys, file extensions and
   `…`. For DaVinci Resolve concepts use Resolve's own localized terms where you know them (Media Pool,
   Timeline, Bin, Marker, Clip, Render Queue, Edit page, Fusion...). Keep button labels short. Make sure each
   language is in its own script (no Hanja/Han in Korean, no Hangul/Kana in Chinese).
   **Write the file atomically** (other agents' test runs read this folder): build it in Python and
   `json.dump(..., ensure_ascii=False, indent=1)` to `<name>.json.tmp`, then `os.replace` to `<name>.json`. For
   a big tool, write in several passes, each pass rewriting the whole file atomically.

5. **Check your work**
   - `python SCRATCH\check_translations.py app\core\translations\<name>.json` - every language present,
     placeholders kept, scripts right, keys actually template-able.
   - `python SCRATCH\scan_page.py <tool_folder> [--settings]` from the repo root - opens the real page offscreen in
     Japanese (throwaway settings folder) and lists text still in English (`[hidden]` = not on screen right now,
     still needs translating). Use `--js "..."` to click into other tabs/states before the scan (e.g.
     `--js "document.querySelector('[data-tab=history]').click()"`). Get it down to only product names,
     user data and numbers. This only sees the first screen's state; read the code for the rest.
   - Tests: from the `tests` folder run `python -m unittest test_<your tool files> test_web_theme test_i18n`
     (find yours with `ls tests`). Update a test only where you deliberately changed English wording/structure.
     Don't run the whole suite (other agents are working in parallel).

## Rules
- Touch only the files listed in your task plus your one JSON file. Don't edit `core/i18n.py`, `buddy.js`, other
  tools, or other agents' JSON files. If something outside your files blocks you, say so in your report.
- Follow the repo's conventions (comments in the surrounding style, no new dependencies). No `type="number"`
  inputs. Don't reformat code you aren't changing.
- Don't commit.
- Report back briefly: JSON file name + number of keys, code changes made (file: what/why), anything you couldn't
  translate or had doubts about, and the final scan result.
