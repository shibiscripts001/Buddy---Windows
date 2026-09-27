"""Open one tool page offscreen in a language and list the text still in
English. Settings live in a throwaway home folder - never the real ones.

    python scan_page.py <tool_id> [language] [--settings] [--wait ms] [--js "code run before the scan"]

tool_id: a folder under app/pages (e.g. youtube_chapters), or "shell" for the
header/rail/Settings with a stand-in tool.
Prints one line per untranslated string: [hidden] marks text not on screen
right now (another tab, a closed panel) - it still needs a translation.
"""
import argparse
import os
import sys
import tempfile

ap = argparse.ArgumentParser()
ap.add_argument("tool")
ap.add_argument("language", nargs="?", default="日本語")
ap.add_argument("--settings", action="store_true", help="also scan the Settings window with this tool open")
ap.add_argument("--wait", type=int, default=2500)
ap.add_argument("--js", default="")
args = ap.parse_args()

home = tempfile.mkdtemp(prefix="buddy-scan-")
os.environ["USERPROFILE"] = home
os.environ["HOME"] = home
os.environ["APPDATA"] = os.path.join(home, "AppData", "Roaming")
os.environ["LOCALAPPDATA"] = os.path.join(home, "AppData", "Local")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.stdout.reconfigure(encoding="utf-8")

REPO = os.environ.get("BUDDY_REPO", os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
sys.path.insert(0, os.path.join(REPO, "app"))

from unittest import mock  # noqa: E402

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

import core.shell_window as shell_window  # noqa: E402
from core import settings_store  # noqa: E402
from core.settings_dialog import SettingsDialog  # noqa: E402
from pages.base import ToolPage  # noqa: E402
from registry import REGISTRY  # noqa: E402

SCAN = r"""
(() => {
  const out = [];
  const SKIP = 'script, style, textarea, code, kbd, [translate="no"], .notranslate';
  const visible = n => { const e = n.nodeType === 1 ? n : n.parentElement; return !!(e && e.getClientRects().length); };
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  for (let n = w.currentNode; n; n = w.nextNode()) {
    if (n.nodeType === 1) {
      if (n.closest(SKIP) && !n.matches('textarea, code, kbd')) continue;
      for (const a of ['title', 'placeholder', 'aria-label', 'alt']) {
        const v = n.getAttribute(a);
        if (v && /[A-Za-z]{2}/.test(v) && !/[぀-鿿가-힯]/.test(v)) out.push((visible(n) ? '' : '[hidden] ') + '@' + a + ': ' + v);
      }
      continue;
    }
    const p = n.parentElement;
    if (!p || p.closest(SKIP) || p.isContentEditable) continue;
    const t = n.data.replace(/\s+/g, ' ').trim();
    if (/[A-Za-z]{2}/.test(t) && !/[぀-鿿가-힯]/.test(t)) out.push((visible(n) ? '' : '[hidden] ') + t);
  }
  return JSON.stringify(out);
})()
"""


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def js(view, code):
    loop, out = QEventLoop(), {}
    view.page().runJavaScript(code, 0, lambda r: (out.update(r=r), loop.quit()))
    QTimer.singleShot(8000, loop.quit)
    loop.exec()
    return out.get("r")


def report(title, view):
    import json
    raw = js(view, SCAN)
    items = json.loads(raw) if raw else []
    seen = []
    for i in items:
        if i not in seen:
            seen.append(i)
    print(f"== {title}: {len(seen)} untranslated")
    for i in seen:
        print("  " + i)


class _Stand(ToolPage):
    tool_id, display_name, category = "alpha", "Alpha", "Tools"

    def build_ui(self):
        QVBoxLayout(self).addWidget(QWidget())


class Mem(dict):
    def save(self):
        pass


def main():
    app = QApplication.instance() or QApplication([])
    if args.tool == "shell":
        entries = [("Tools", _Stand)]
    else:
        entries = [(c, cls) for c, cls in REGISTRY if cls.__module__.split(".")[1] == args.tool]
        if not entries:
            sys.exit(f"no tool {args.tool!r}")
    shared = Mem(settings_store.DEFAULT_SHARED_SETTINGS, language=args.language, _language_adopted=True)
    patches = [mock.patch.object(shell_window, "SharedSettings", lambda: shared),
               mock.patch.object(shell_window, "resolve_connect", side_effect=RuntimeError("no Resolve")),
               mock.patch.object(shell_window.AnnouncementChecker, "start", lambda self: None),
               mock.patch.object(shell_window.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False)]
    for p in patches:
        p.start()
    win = shell_window.ShellWindow(app, entries)
    win.show()
    wait(args.wait)
    page = next(iter(win.pages.values()))
    if args.tool == "shell":
        for cls in (shell_window.HeaderView, shell_window.RailView):
            chrome = win.findChild(cls)
            if chrome is not None:
                report(cls.__name__, chrome.view)
    else:
        if args.js:
            js(page.view, args.js)
            wait(800)
        report(args.tool, page.view)
    if args.settings or args.tool == "shell":
        dialog = SettingsDialog(win, shared, lambda: None, page)
        dialog.show()
        wait(1500)
        report("settings", dialog.view)
        dialog.close()
    for p in patches:
        p.stop()
    sys.stdout.flush()
    os._exit(0)


main()
