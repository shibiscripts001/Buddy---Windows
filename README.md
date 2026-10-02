# Buddy

A single PySide6 desktop app that hosts a set of DaVinci Resolve tools, plus a
chat agent that answers questions about Resolve from the official reference
manual and can read — and, if you let it, change — your open project.

Buddy replaces a folder of one-tool scripts with one entry in Resolve's
**Workspace > Scripts > Utility** menu. Each tool started as a standalone
script and has been ported into the shell.

## Status

Buddy's integrated tools include the following. Audit and parts of Audio Assistant are still in progress:

- Animation (formerly Text Animator; Previews puts motion presets on the clips selected in Resolve - stills, video, Text+ - with the In on each clip's first frame and the Out on its last, and an Editor for presets of your own. Animate by plays a preset line by line, word by word or letter by letter on a Text+, a stagger apart in the order chosen, through Fusion's text Follower (`pages/text_animator/units.py`). Its Titles tab is empty for now - the Text+ tools are on Subtitles)
- Ask Buddy (chat agent)
- Audio Assistant (new; its Timeline tab mirrors the audio tracks with each clip's waveform, and sets clip volume, pan, fades, loudness to a target, Voice Isolation and the Dialogue Leveler, and a volume curve drawn on the clip that becomes Resolve keyframes - effects are coming)
- Audit (new; the page only for now - its tools are coming)
- Asset Manager
- Color Palette Manager
- Command Center (new; system-wide hot keys - or a Run button - for markers, jumping between them, copying the timecode or the frame, clip colours, Animation presets, saving a timeline version and removing gaps (experimental). Hot keys are Windows-only for now (`core/hotkeys.py`); see `pages/command_center/actions.py`)
- Dailies (named source tapes, continuous Media Pool review, notes and metadata)
- Image Importer
- Media Manager (Batch Clip Renamer and Media Relink tabs)
- Project Setup (bins, folder import, timeline populate, multicam sync, proxy rendering)
- Marker Manager (Stills Exporter and YouTube Chapters tabs)
- SVG Importer
- Time Tracker
- Subtitles (Transcribe, Translate, Subtitle Conversion and the Text+ styling, layout, word-by-word and animation tabs included)
- Web (new; a light browser - tabs (drag to reorder), private tabs (Ctrl+Shift+N), YouTube quality, search, new-tab shortcuts, a pause button on a tab that plays sound, Google sign-in, sign-ins saved encrypted with the Windows account (`pages/web/cookie_vault.py`), downloads that drag into the Media Pool, ad and tracker blocking with EasyList, EasyPrivacy and uBlock Origin's filter lists (`pages/web/filters.py`). Background tabs sleep after a while, except ones playing sound, kept awake or on a never-sleep site. Its sound ducks while Resolve plays (Windows; `core/audio_sessions.py`), and a speaker beside "Web" in the sidebar shows when it plays. Built on the Chromium Buddy already ships, so it can't play H.264 or DRM video; see `pages/web/`)

The bug button in the header (and on the desktop layout's taskbar) sends a
bug report - what went wrong, up to six screenshots, and Buddy's, the
system's and Resolve's versions - to the Buddy Network server, with Buddy
Network on or off. The owner reads them in Buddy Network's Admin panel
(Bugs tab). See `core/bug_report.py` and `server/bugs.py`.

## Background behavior (Time Tracker)

Time Tracker's port made Buddy itself a background app, not just a one-shot
action tool:

- **System tray.** Closing the window's `[X]` minimizes Buddy to the tray
  instead of quitting — tracking keeps running. Right-click the tray icon to
  reopen or quit for real. Turn this off in Settings > Window if you'd rather
  the `[X]` just quit, like every other tool here.
- **Single instance.** Relaunching from Resolve's Scripts menu while Buddy is
  already running (e.g. minimized to tray) just raises the existing window
  instead of starting a second one polling Resolve in parallel.
- **Start Buddy automatically when Resolve starts** (opt-in, Settings >
  Window): a small background watcher waits for Resolve's process to appear,
  then launches Buddy straight into the tray so tracking is already running
  by the time you're in a project.

These three are shell-level (`core/shell_window.py`,
`core/single_instance.py`, `core/startup_manager.py` +
`core/resolve_watcher.py`), not specific to Time Tracker — any future
background tool gets them for free. Windows only for now.

## Installing (for users)

Double-click **BuddySetup-<version>.exe** and click through. No admin rights
needed. It puts Buddy in DaVinci Resolve's **Workspace > Scripts** menu, and
installs what Buddy runs on only if the PC is missing it: Python 3.13 from
python.org (checksum-verified) and Buddy's Python packages, downloaded while
installing (up to ~330 MB, so it needs internet). Then open Resolve and pick
**Workspace > Scripts > Buddy**.

It can also add a Start menu entry (on by default) and a desktop shortcut
(off by default) that open the same Buddy without going through Resolve.
Either way it's one app: opening it a second way brings up the copy already
running.

**Which Resolve works which way:**

| Resolve | Workspace > Scripts | Start menu / desktop shortcut |
| --- | --- | --- |
| Studio | Yes | Yes |
| Free, up to 21.0.4 | Yes | Opens, but can't reach Resolve |
| Free, 21.1 and later | No | Opens, but can't reach Resolve |

- Only **DaVinci Resolve Studio** lets Buddy connect from outside Resolve
  (a shortcut, or "Start Buddy automatically when Resolve starts"). Turn on
  **Preferences > System > General > "External scripting using" > Local**.
- The free version runs Buddy only from **Workspace > Scripts**, and only up
  to **21.0.4**. From **21.1** on, only Studio supports Python scripts.
- Where Buddy can't reach Resolve, it still opens, and the tools that don't
  use Resolve still work. The ones that do say they can't reach it.
- In the free version, open Buddy from Workspace > Scripts first. If a copy
  started from a shortcut is already running, the menu only brings that one
  up, so quit it from the system tray first.

Windows may show "Windows protected your PC" because the installer isn't
code-signed: **More info > Run anyway**. Uninstall from Settings > Apps; it
removes Buddy and leaves Python, its packages and your `~/.buddy` settings.

Rather not run the installer? [manual_install.txt](manual_install.txt) walks
through the same steps by hand - every feature, Buddy Network included.

## Updates

Once installed, Buddy updates itself. Once a day it looks at the latest GitHub
release, and when there's a newer one an **Update** button appears in the
header: it shows what changed, downloads `buddy.zip` (and `Buddy.py`, if that
changed) from the release, checks each is exactly the size and SHA-256 the
release's `buddy-update.json` says, keeps the old one, and restarts Buddy.
Settings > Updates turns the daily check off, checks now, or rolls
back to the version the last update replaced. A release that needs a package
Buddy doesn't have yet sends you to its installer instead. Updates aren't
signed: they're trusted as far as GitHub and the account publishing the
releases are (see `app/core/updater.py`).

## Building the installer

```bash
python build_installer.py
```

Needs Inno Setup 6 or 7. Writes `dist/BuddySetup-<version>.exe` (~3 MB) using
the version in `VERSION`.

## Releasing

Change the number in `VERSION` on `main` (e.g. `1.0.0` -> `1.0.1`) and commit -
GitHub's web editor is fine. GitHub Actions (`.github/workflows/release.yml`)
then runs the tests, builds the installer on a clean Windows machine and
publishes release `v1.0.1` with `BuddySetup-1.0.1.exe` attached - and `buddy.zip`, `Buddy.py` and
`buddy-update.json` (`build_update_manifest.py`), which running Buddys update from. Pushes that
don't change `VERSION` release nothing, and an existing version is never
overwritten. It packages what is on `main`, so push changes before bumping. The installer script is
`installer/Buddy.iss`; the packages it installs are `installer/requirements.txt`
(a test fails if Buddy imports a package that isn't listed there).

## Requirements

- DaVinci Resolve — only needed for the tools that talk to it. Studio works
  from Workspace > Scripts and from outside Resolve; the free version only
  from Workspace > Scripts, and only up to 21.0.4 (see
  [Installing](#installing-for-users))
- Python 3.10+
- PySide6, plus Pillow, NumPy, openpyxl, pynput and PyMuPDF for some tools

```bash
pip install -r installer/requirements.txt
```

## Running it

For development, straight from the source tree:

```bash
python app/main.py
```

## Deploying to Resolve

```bash
python build_buddy_zip.py --deploy
```

That builds `buddy.zip` and copies it, with the `Buddy.py` launcher stub, into

```
%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility
```

Restart Resolve and **Buddy** appears under Workspace > Scripts > Utility.
Re-run the command after any code change — Resolve loads the zip, not the
source tree.

PySide6 has to be installed for the interpreter *Resolve* uses, which is not
always the one on your PATH. If it isn't, the launcher says so and names the
exact interpreter.

## Subtitles

Turns the current timeline's dialogue into subtitles, and translates them, locally - nothing is
sent to the internet (unless you choose AI translation with a cloud model). Transcribe Timeline renders the timeline's audio mix,
runs Whisper (faster-whisper) on it, builds readable subtitles from the word
timings (42 characters a line, two lines, breaking at sentences and
clauses), and puts them on subtitle track 1 as well as saving an SRT in
`~/.buddy/transcribe/output/`. Its Subtitle Conversion tab turns them into
Text+ clips, and the tabs after it style, place and animate them.

- **One-time setup** (Subtitles > Setup): installs the engine into its
  own environment in `~/.buddy/transcribe/`, built from the Python Buddy runs
  on. About 1.45 GB with an NVIDIA GPU (mostly CUDA libraries), about 110 MB
  without. Then a model - downloaded, or reused from a folder another app
  already has:
  - Whisper Large v3 (3.1 GB), Large v3 Turbo (1.6 GB), Small (0.48 GB):
    every language.
  - Distil-Whisper Large v3.5 (1.51 GB, MIT): English only, about three
    times faster than Large v3 on a GPU.
  - NVIDIA Parakeet v3 (0.67 GB, CC-BY 4.0): 25 European languages, runs on
    the CPU at ~35x real time, punctuates by itself (English "uh"/"um" are
    dropped). No hotwords.
  - **Auto** (with Parakeet and a Whisper model installed): detects the
    language, then uses Parakeet where it can and Whisper everywhere else.
- **Hardware**: runs on the GPU with an NVIDIA card, and on the CPU on any
  other Windows PC or Mac. The CPU path is tested on Windows; Macs are not
  tested yet.
- **Your project**: Resolve's API has no way to read render settings, so they
  are saved as a temporary preset before the audio render and loaded back
  after; the render job is removed. Resolve only lets scripts place subtitles
  on subtitle track 1, so if that track already has subtitles Buddy asks
  before replacing them, or keeps just the SRT file.
- **Translate**: Translate Last Transcript (or Translate an SRT File...)
  writes one SRT per chosen language to a folder you pick - or
  automatically after each transcription. Three engines, picked under
  Translate > Model:
  - **NLLB-200** (Meta), locally, through the same engine: 200 languages.
    Get a model in Set up... (600M 0.62 GB, 1.3B 1.38 GB recommended,
    3.3B 3.36 GB). Licensed CC-BY-NC 4.0 - non-commercial use only.
  - **MADLAD-400 3B** (Google), locally: about 180 of those languages
    (2.95 GB, Nextcloud's CTranslate2 conversion). Licensed Apache 2.0, so
    fine for client and commercial work. Best with an NVIDIA GPU; on a CPU
    it's a few seconds per batch of sentences.
  - **AI translation**: the model Ask Buddy is set up with (Claude, Gemini,
    an OpenAI-compatible service, or a local Ollama). It translates 40
    sentences at a time with the lines before them, so names and tone stay
    consistent, and it's given the Names and terms field as a glossary.
    Needs no local engine. A cloud model gets the transcript's text under
    your own key (and may charge for it) - Buddy asks once per provider,
    and never sends it during an automatic translation until you have.
  Whole sentences are translated, then re-cut into timed subtitles; Chinese
  and Japanese are wrapped by character with native punctuation. Import the
  files onto other subtitle tracks yourself (File > Import > Subtitle) -
  scripts can only place subtitles on track 1.

## Ask Buddy

The chat page answers Resolve questions from the DaVinci Resolve 21 Reference
Manual and cites chapter and page, so you can check it. Type `help` in the
chat for the current capability list — it is generated from the code, so it
cannot drift.

Conversations are saved locally in `~/.buddy/ask_buddy/conversations.json`.
Use **Chats** to search past messages or rename a conversation. Click a manual
citation to open the manual PDF at that page - the PDF the bundle was built
from (Buddy asks where it is once, for a bundle built before that was
recorded). Image questions keep small preview thumbnails in saved chats.

**Model providers** (bring your own key, set in Settings): Google Gemini,
Anthropic Claude, OpenAI-compatible endpoints (OpenRouter, Groq, LM Studio…),
or Ollama running locally.

**Manual data.** Retrieval reads a bundle in `~/.buddy/manual/bundle/`. Buddy
runs fine without it and says so in the status line; answers are then
ungrounded and carry no citations.

To build or refresh it when a new manual comes out, download the Reference
Manual PDF, then either open **Settings > Ask Buddy > Rebuild from PDF...**,
drop the PDF in and press Build, or run:

```bash
python build_manual_bundle.py DavinciManual.pdf
```

A full manual takes about 8 minutes. The builder needs `pymupdf` and
`pymupdf4llm` (`pip install pymupdf pymupdf4llm`, for the interpreter Resolve
uses if you build from inside Buddy), and Ollama running with `embeddinggemma`
pulled for semantic search. Without Ollama it builds a keyword-only bundle
and says so. The previous bundle is kept as `bundle.previous`, and a stopped
or failed build leaves the current one untouched.

**Reading your project.** With Resolve running, it can read your project and
timeline settings, which edition you have, what is on the timeline, any clips
whose frame rate or resolution does not match it, and your markers. Mismatches
are computed in Python rather than left to the model.
The detected Free or Studio edition is included with every question while
Buddy is connected to Resolve; without a connection, the edition is marked
unknown. **Check project** scans the current timeline for frame-rate and
upscaling mismatches and missing local source paths, then suggests next
steps. **Explain clip** focuses on the selected timeline clip or the video
clip under the playhead before answering.

### Changing your project

Off by default, and behind two independent gates:

1. **Consent.** A checkbox in Settings, which opens a dialog explaining the
   risk and asks you to type a sentence out in full. Consent is never cached —
   unticking the box revokes it and re-ticking asks again.
2. **Per-action approval.** The model only ever *proposes*. Buddy reads your
   project, builds a card listing every item that would actually change, and
   waits for you to press Apply. Actions that move or destroy work you did not
   name are flagged, and say what they will do before you press anything.

Even with consent on, nothing reaches your project without that second click.

## Layout

```
app/
  main.py            entry point
  core/              shell window, theming, settings, Resolve bridge,
                     tool knowledge base, write-consent gate
  pages/             one package per tool; base.py defines the ToolPage contract
  registry.py        which tools exist, and which are real
Buddy.py             launcher stub for Resolve's Scripts menu
build_buddy_zip.py   builds buddy.zip, and deploys it with --deploy
build_installer.py   builds the Windows installer (installer/, dist/)
build_manual_bundle.py  builds the manual bundle from the PDF (same as the
                     Settings button; pages/manual_chat/bundle_builder.py)
tests/               automated tests for the pure logic (see Tests)
docs/RESOLVE_API_GUIDE.md  Resolve scripting API traps, for anyone working on the code
```

Themes live in `core/theme.py` as a Theme → Subtheme hierarchy: **Default**
(DaVinci Resolve's own look - colours, control sizes and the Open Sans type
matched to Resolve 21's UI, so Buddy sits in Resolve as if it were part
of it; the default for new installs) with DaVinci and the colour variants
Blue, Teal, Green, Yellow, Orange, Purple and Pink (Resolve's greys washed
toward the colour, which takes the place of Resolve's red), **Don't be evil** (dark,
softly rounded), **Retro** (flat, square) with dark
(Mulberry - its default - and Licorice) and light paper (Peach, Bubblegum)
palettes, and **Modern** (soft-rounded product-dashboard look: a navy-teal glow behind slate cards and
one vivid accent) with Midnight, Aurora, Graphite and the light Daylight -
each theme also has a Custom palette. (Settings files store the original
keys `Resolve` / `Default` / `Retro` / `SaaS`; the names shown are labels.)
Open Sans ships in `app/assets/fonts/` under the SIL Open Font License
(`OFL.txt` beside it). Status colours follow the palette's
lightness, so a dark subtheme gets dark-surface status text.

## Tests

```
python -m unittest discover tests
```

Plain Python - no Qt, Resolve or models needed - and about 3 seconds
(`pytest tests` works too). They cover what can go subtly wrong without
anything looking broken: subtitle wrapping and timing (seeded randomized
runs checking no lost words, overlaps or over-long lines), translation
re-timing, Chinese/Japanese line breaking, SRT parsing, the language tables,
model-folder identification, and the transcription worker's text clean-up.
Run them after changing anything in `app/pages/transcribe/`.

## Notes

Working on a tool that talks to Resolve? Read
[`docs/RESOLVE_API_GUIDE.md`](docs/RESOLVE_API_GUIDE.md) first: it covers the
scripting API's silent failures and the Fusion node-graph traps that
Blackmagic's reference doesn't mention.

`buddy.zip` is not tracked — it is a build artifact, rebuilt by the command
above.

## License

Free to use for anything, including paid work, and free to change and share.
What you can't do is sell it: charging for Buddy or a modified copy, or for a
product or service whose value comes mainly from it, isn't allowed. The
[licence](LICENSE) is MIT with the Commons Clause, which is source-available
rather than OSI "open source". The Open Sans font keeps its own SIL Open Font
License (`app/assets/fonts/OFL.txt`).
