# Buddy translations: handoff

Status as of 2026-09-27. Nothing is committed yet; all changes are in the working tree on `main`.
The full test suite passes (668 tests).

## Done

**How it works**
- `app/core/i18n.py` holds your translation doc, merged with any `app/core/translations/*.json` files
  (same format, one file per part of the app). It has `translate()`, `tr()` for Qt text, and `web_strings()`.
  - Keys are matched with runs of whitespace treated as one space.
  - `"..."` matches `"…"`, and `"Name:"` is found from `"Name"` (and the other way round).
  - `{placeholders}` stand for values that change: `"Deleted '{name}'"` matches `Deleted 'Sunset'` and keeps the name.
- **Web pages (every screen):**
  - `core/web_page.py` sends each view the current language's strings (the `"i18n"` event), on load and on every change.
  - `app/web/buddy.js` (the "Languages" block) translates text, `title`, `placeholder`, `aria-label` and `alt` as they
    are drawn. A MutationObserver catches text added later.
  - The code keeps writing English.
  - Never touched: input and textarea values, contenteditable, `<code>`/`<kbd>`, and anything inside `translate="no"`.
- **Qt text:**
  - Dialog window titles are translated through `WebDialog`.
  - The tray menu, tray notifications and the busy overlay are translated in `shell_window.py`, `busy_overlay.py` and `main.py`.

**The Settings dropdown**
- A Language dropdown is always the last section in Settings, whichever tool is open (`settings_form.language_fields`).
- The choice is stored as `language` in `~/.buddy/settings.json`.
- Changing it updates every open window straight away, with no restart.

**Color Palette**
- Its own Language dropdown is gone; it now follows the app-wide setting.
- If someone had already picked a language there, that choice becomes the app-wide language once.
- `pages/color_palette/i18n.py` was deleted; its strings are in `core/i18n.py`.

**Your doc**
- 6 mistakes fixed:
  - The Chinese "Settings" entry was actually Korean (설정); it's now 设置.
  - Five Korean entries had a Chinese character mixed in: 색盲 is now 색맹, 무작위化 is now 무작위화.
- The dropdown shows 한국어 for Korean. The saved key stays "한국인", so existing settings still work.
- Arabic keeps the left-to-right layout (your choice).

**Tests**
- `tests/test_i18n.py` covers the matching rules and data checks: every string has all 8 languages, placeholders
  are kept, and each language uses its own script.
- It also covers the dropdown, and a real Settings page drawn in Japanese:
  - typed values are left alone;
  - switching back restores English;
  - text added later is translated too.

## Translations (done, 2026-09-27)

Every tool now has its own file in `app/core/translations/`, about 2,180 strings on top of your 281:

| File | Strings | File | Strings |
|---|---|---|---|
| `shell.json` | 122 | `common.json` | 55 |
| `buddy_network.json` | 442 | `manual_chat.json` (Ask Buddy) | 166 |
| `project_setup.json` | 241 | `transcribe.json` | 217 |
| `time_tracker.json` | 167 | `text_animator.json` | 144 |
| `asset_manager.json` | 134 | `color_palette.json` | 106 |
| `media_relink.json` | 81 | `image_importer.json` | 75 |
| `svg_importer.json` | 72 | `stills_exporter.json` | 69 |
| `batch_clip_renamer.json` | 52 | `youtube_chapters.json` | 34 |

`scan_page.py` on each tool, with `--settings`, finds nothing left in English except the things below.
`unfinished-agent-edits.patch` was reviewed and applied to Text Animator; it can be deleted.

**Still English, on purpose**
- Product and format names (Buddy, DaVinci Resolve, PNG, SRT, Lottie…), keyboard keys (Esc, Ctrl), and the theme
  names Don't be evil, Nova and DaVinci.
- The ~200 target languages in Transcribe's picker (each is shown in its own name or in English).
- Deep technical logs: Text Animator's activity log, Project Setup's engine diagnostics, SVG Importer's details log.
- Ask Buddy's per-action proposal summaries (built from the model's own tool calls).
- Anything a server or the AI model sends (Buddy Network server errors and announcements, model answers).

**Code changes made along the way** (all tests pass: 668)
- Plurals: every glued plural is now whole alternatives (`"1 clip"` / `"{count} clips"`).
- User data (clip, bin, palette, project, file and room names; chat messages; notes; model answers) is marked
  `translate="no"` where it's drawn.
- Sentences built from pieces are whole sentences now, or separate text nodes (spans, a `parts` list for log
  lines, or `
`-separated lines that Buddy Network's dialogs split into paragraphs).
- The `QFileDialog` captions and filters go through `tr()` / `tr_filter()`.
- Project Setup's Info check compares `dataset.help`, not the button text. The startup message in
  `settings_dialog.py` is two whole sentences.
- Ask Buddy's help footer now says "To see this again any time, type `help`." (the old wording split round a
  code tag). Buddy Network's rules list has one reworded line for the same reason.

**New in the i18n system**
- A placeholder whose name starts with `t_` has its value translated too (exact lookup), for values that are
  Buddy's own words: `"Grab stills at the {t_color} markers"`. Never use it for anything a person named. Works in
  both `core/i18n.py` and `buddy.js`.
- `Buddy.menu` items take `raw: true` (the label is user data), and `settings_form.info(label, value, raw=True)`
  does the same for an info row.
- `core.i18n.format_when(datetime, english_format, style)` writes a date in the chosen language's own style
  (QLocale). English keeps the strftime format exactly. Buddy Network uses it for day headers, message times,
  ban dates and saved chats.

**Worth a native speaker's look.** The translations are machine-made and haven't been reviewed by a person. The
long safety texts in Buddy Network (encryption, transfer files, bans) and the Korean/Arabic register matter most.

**New tools in `tools/i18n/`**
- `add_translations.py <name> <lines.txt> [--comment "X"]`: merges lines of
  `English || ja || es || de || fr || ko || zh || ar || vi` into `app/core/translations/<name>.json`, then checks it.
  In the text, `
` stands for a newline and `#` starts a comment.
- `missing_keys.py <folder under app/>`: lists `tr()` / `T()` / `Buddy.t()` strings that have no Japanese.
- `unused_keys.py <name> <folder under app/>`: lists keys whose text can't be found in the source. It's a rough
  check, and plural pairs and composed sentences show up as expected noise.
- `strings_in.py <file> <CallName...>` and `sentences_in.py <files...>`: pull a file's visible strings, as a
  starting list.

**Adding a new string later.** Write the English in code as usual. Add a line to a text file, run
`add_translations.py <tool> file.txt`, then run `scan_page.py <tool>` to confirm.

## How to continue

`BRIEF.md` is the full brief the agents were given: rules, style, register and what to skip. In it, "SCRATCH" means
this `tools/i18n/` folder. Work one tool at a time:

1. Read the tool's HTML, JS and Python. Collect the visible English and write keys the way the text reads on screen,
   using `{placeholders}` for the parts that change.
2. Fix any code spots like the ones above.
3. Write `app/core/translations/<tool>.json` with all 8 languages.
4. Check the file (every language present, placeholders kept, right script):
   ```bash
   python tools/i18n/check_translations.py app/core/translations/<tool>.json
   ```
5. List what's still English on the tool's page. This opens the real page offscreen in Japanese and uses a
   throwaway settings folder, never your real one. Add `--settings` to also scan the Settings window, or
   `--js "..."` to click into other tabs first:
   ```bash
   python tools/i18n/scan_page.py <tool_folder>
   ```
6. Run the tests:
   ```bash
   cd tests && python -m unittest discover .
   ```

The scripts find the repo from their own location. `tools/` is a dev aid and isn't packaged into `buddy.zip`,
which only takes `app/`.
