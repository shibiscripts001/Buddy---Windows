"""Transcribe: the choices (plan.py, no Qt) and the web page driven with
fake jobs and a fake Resolve - settings in a temp file, never the real
~/.buddy/transcribe/settings.json, and nothing is installed or run."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _paths  # noqa: F401
from pages.transcribe import env_setup as es
from pages.transcribe import plan

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

ALL = {"large-v3": "/m/large", "parakeet-v3": "/m/parakeet", "distil-large-v3.5": "/m/distil", "small": "/m/small"}


class PlanTests(unittest.TestCase):
    def test_model_menu(self):
        options, chosen = plan.model_options(ALL, "large-v3", "")
        self.assertEqual([o["id"] for o in options][:2], ["auto", "large-v3"])
        self.assertEqual(chosen, "auto")
        options, chosen = plan.model_options({"small": "/m/small"}, "large-v3", "large-v3")
        self.assertEqual(([o["id"] for o in options], chosen), (["small"], "small"))
        self.assertEqual(plan.model_options({}, "large-v3", ""), ([], ""))

    def test_plans(self):
        p, why = plan.plan_for("auto", ALL, "de")
        self.assertEqual((p["engine"], p["language"], why), ("auto", "de", None))
        p, why = plan.plan_for("parakeet-v3", ALL, "")
        self.assertEqual((p["engine"], p["whisper"]), ("parakeet", "/m/large"))    # Whisper names the language
        self.assertIn("doesn't cover Japanese", plan.plan_for("parakeet-v3", ALL, "ja")[1])
        p, _ = plan.plan_for("distil-large-v3.5", ALL, "")
        self.assertEqual(p["language"], "en")                                     # English-only: told so
        self.assertIn("only understands English", plan.plan_for("distil-large-v3.5", ALL, "fr")[1])
        self.assertIn("isn't installed", plan.plan_for("large-v3-turbo", ALL, "")[1])

    def test_mixed_needs_a_multilingual_whisper(self):
        p, _ = plan.plan_for("auto", ALL, plan.MIXED, ["en", "ja"])
        self.assertEqual((p["engine"], p["whisper"], p["language"]), ("whisper", "/m/large", ""))
        self.assertIn("at least two", plan.plan_for("auto", ALL, plan.MIXED, ["en"])[1])
        self.assertIn("can't be told", plan.plan_for("parakeet-v3", ALL, plan.MIXED, ["en", "de"])[1])
        self.assertIn("multilingual Whisper", plan.plan_for("auto", {"parakeet-v3": "/p"}, plan.MIXED, ["en", "de"])[1])
        self.assertEqual(plan.mixed_label(["en", "ja"]), "Mixed: English + Japanese")

    def test_targets(self):
        targets, notes = plan.targets_for(["eng_Latn", "jpn_Jpan", "fra_Latn"], "eng_Latn", "nllb")
        self.assertEqual(targets, ["jpn_Jpan", "fra_Latn"])
        self.assertIn("already spoken", notes[0])
        with mock.patch.object(plan.L, "MADLAD_CODES", {"fra_Latn": "fr"}):
            targets, notes = plan.targets_for(["jpn_Jpan", "fra_Latn"], "eng_Latn", "madlad")
        self.assertEqual(targets, ["fra_Latn"])
        self.assertIn("MADLAD-400 doesn't have it", notes[0])

    def test_translation_menu_always_offers_ai(self):
        options, chosen = plan.translation_options({}, "AI translation", "")
        self.assertEqual(([o["id"] for o in options], chosen), ([plan.AI_ID], plan.AI_ID))
        options, chosen = plan.translation_options({"nllb-1.3b": "/t"}, "AI", "")
        self.assertEqual(chosen, "nllb-1.3b")

    def test_setup_rows_and_output(self):
        rows = plan.setup_rows(es.MODELS, {"small": str(es.MODELS_DIR / "small")}, {"large-v3": "C:/hf/large"}, "large-v3")
        by_id = {r["id"]: r for r in rows}
        self.assertEqual((by_id["small"]["installed"], by_id["small"]["where"]), (True, "Buddy's folder"))
        self.assertEqual((by_id["large-v3"]["found"], by_id["large-v3"]["recommended"]), ("C:/hf/large", True))
        self.assertEqual(plan.setup_line(plan.MARK + '{"type":"progress","done":5e8,"total":1e9}')[:2], ("progress", 0.5))
        self.assertEqual(plan.setup_line("Collecting faster-whisper"), ("log", "Collecting faster-whisper"))
        self.assertIsNone(plan.setup_line("  some pip noise"))

    def test_settings_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = plan.Settings(Path(tmp) / "sub" / "settings.json")
            self.assertEqual(s.get("max_chars"), 42)
            s["max_chars"] = 30
            s.save()
            self.assertEqual(plan.Settings(Path(tmp) / "sub" / "settings.json").get("max_chars"), 30)


# --------------------------------------------------------------- fakes --

if HAVE_QT:
    class FakeJob(QObject):
        """A job that does nothing until the test says how it ended."""
        stage = Signal(str)
        progress = Signal(int)
        note = Signal(str)
        succeeded = Signal(dict)
        failed = Signal(str)
        line = Signal(str)
        finished = Signal()
        made = []

        def __init__(self, *args, **kwargs):
            super().__init__()
            self.args, self.kwargs, self.cancelled = args, kwargs, False
            FakeJob.made.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

        def wait(self, _ms=0):
            return True

        def succeed(self, result):
            self.succeeded.emit(result)
            self.finished.emit()

    class FakeProbe(QObject):
        done = Signal(object)
        finished = Signal()

        def __init__(self, extra, hw=None):
            super().__init__()

        def start(self):
            self.done.emit({"hw": es.Hardware("Windows", "AMD64", True, "RTX Test"),
                            "env": es.EnvStatus(True, "Ready.", {"faster_whisper": "1.2.1"}),
                            "models": dict(ALL), "found": {}, "tmodels": {"nllb-1.3b": "/t/nllb"}, "tfound": {}})
            self.finished.emit()

        def wait(self, _ms=0):
            return True


class FakeResolve:
    existing = 0
    placed = []
    timeline, empty = "Interview", 4     # Subtitle Conversion's view of the timeline
    subtitles = 12
    converted = []

    def __init__(self, controller):
        pass

    def timeline_info(self):
        return SimpleNamespace(name="Interview", duration_seconds=600, start_timecode="01:00:00:00",
                               subtitle_items_on_track1=FakeResolve.existing)

    def place_subtitles(self, srt, replace_existing=False):
        FakeResolve.placed.append((srt, replace_existing))
        return 12

    def conversion_tracks(self):
        return {"timeline": FakeResolve.timeline, "video": 3, "subtitle": 1, "empty": FakeResolve.empty}

    def subtitles_to_text_plus(self, sub_track, video_track, log=lambda msg: None):
        from pages.transcribe.resolve_ext import TranscribeResolveError
        if not FakeResolve.subtitles:
            raise TranscribeResolveError(f"Subtitle track {sub_track} has no subtitles.")
        log("  - [Diagnostic] Timeline methods present: all")     # too much detail for the log
        log("  - [Setup] Found a Text+ template in the Media Pool; using it for exact placement.")
        FakeResolve.converted.append((sub_track, video_track))
        return FakeResolve.subtitles


class Mem(dict):
    def save(self):
        pass


class Host:
    shared_settings = {"theme": "Resolve"}

    def __init__(self):
        # resolve=None: the Text+ tabs' own calls find no timeline and do nothing.
        self.controller, self.connected = SimpleNamespace(resolve=None), True
        self.busy = []
        self.tools = {}

    def ensure_connected(self):
        return self.controller

    def tool_settings(self, tool_id, defaults=None):
        return self.tools.setdefault(tool_id, Mem(defaults or {}))

    def theme_tokens(self):
        from core.theme import get_theme_tokens
        return get_theme_tokens("Resolve")

    def set_busy(self, on, message=None):
        self.busy.append(on)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from pages.transcribe import page as page_mod
        self.page_mod = page_mod
        tmp = Path(self._tmp.name)
        FakeJob.made.clear()
        FakeResolve.existing, FakeResolve.placed = 0, []
        FakeResolve.timeline, FakeResolve.empty, FakeResolve.subtitles, FakeResolve.converted = "Interview", 4, 12, []
        self._patch(page_mod.TranscribePage, "_settings_path", lambda s: tmp / "settings.json")
        self._patch(page_mod.jobs, "ProbeJob", FakeProbe)
        for name in ("TranscribeJob", "TranslateJob", "AITranslateJob", "SetupJob"):
            self._patch(page_mod.jobs, name, FakeJob)
        self._patch(page_mod, "TranscribeController", FakeResolve)
        self.host = Host()
        self.page = page_mod.TranscribePage(self.host)
        self.addCleanup(self.page.deleteLater)
        self.events = []
        self.page.emit = lambda name, payload=None: self.events.append((name, payload))
        self.tmp = tmp

    def _patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def last(self, name):
        return [p for n, p in self.events if n == name][-1]

    def test_ready_after_the_check(self):
        self.page._rescan()
        self.assertTrue(self.page.ready)
        self.assertEqual(self.last("options")["model"], "auto")
        self.assertTrue(self.last("setup")["env_ready"])
        self.assertEqual(self.page.settings.path, self.tmp / "settings.json")

    def test_options_are_saved(self):
        self.page.on_option({"key": "max_chars", "value": 99})
        self.page.on_option({"key": "max_lines", "value": 1})
        self.page.on_option({"key": "language", "value": plan.MIXED})       # no languages chosen yet: refused
        self.assertEqual(self.page.settings.get("language"), "")
        self.page.on_mixed({"codes": ["en", "ja"]})
        self.assertEqual(plan.Settings(self.tmp / "settings.json").get("language"), plan.MIXED)
        o = self.last("options")
        self.assertEqual((o["max_chars"], o["max_lines"], o["mixed_label"]), (60, 1, "Mixed: English + Japanese"))

    def _run_and_finish(self):
        self.page.on_run()
        job = FakeJob.made[-1]
        srt = self.tmp / "out.srt"
        srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
        job.succeed({"srt": str(srt), "transcript": str(self.tmp / "t.json"), "cues": 12, "language": "en",
                     "languages": {}, "device": "cuda", "engine": "whisper", "duration": 600, "seconds": 60,
                     "name": "out"})
        return job

    def test_transcribe_places_on_an_empty_track(self):
        job = self._run_and_finish()
        self.assertEqual(job.args[1], "Interview")
        self.assertEqual(FakeResolve.placed[-1][1], False)
        r = self.last("result")
        self.assertEqual((r["kind"], r["placed"]), ("transcribe", 12))
        self.assertIn("12 subtitles on subtitle track 1", r["message"])

    def test_a_full_track_is_only_replaced_when_the_user_says(self):
        FakeResolve.existing = 30
        self._run_and_finish()
        ask = self.last("ask")
        self.assertEqual((ask["kind"], ask["count"]), ("replace", 30))
        self.assertEqual(FakeResolve.placed, [])
        self.page.on_answer({"id": ask["id"], "ok": False})
        self.assertEqual(FakeResolve.placed, [])
        self.assertIn("not added", self.last("result")["message"])

        self._run_and_finish()
        self.page.on_answer({"id": self.last("ask")["id"], "ok": True})
        self.assertEqual(FakeResolve.placed[-1][1], True)

    def test_nothing_else_runs_while_a_job_does(self):
        self.page.on_run()
        self.assertEqual(len(FakeJob.made), 1)
        self.page._dispatch("run", None)
        self.page._dispatch("install_env", None)
        self.assertEqual(len(FakeJob.made), 1)
        self.page._dispatch("stop", None)
        self.assertTrue(FakeJob.made[0].cancelled)

    def test_cloud_translation_asks_first_and_never_automatically(self):
        self.page.settings["translate_targets"] = ["jpn_Jpan"]
        self.page.settings["translate_model"] = plan.AI_ID
        client = SimpleNamespace(validate=lambda: None, provider="anthropic", model="claude-test")
        self._patch(self.page_mod.TranscribePage, "_ai_client", lambda s: client)
        self._patch(self.page_mod.TranscribePage, "_ai_is_local", lambda s: False)
        self._patch(self.page_mod.TranscribePage, "_ai_provider", lambda s: "anthropic")
        transcript = self.tmp / "t.json"
        transcript.write_text('{"segments": [{"start": 0, "end": 2, "text": "Hello there.", '
                              '"words": [{"start": 0, "end": 1, "word": "Hello"}, {"start": 1, "end": 2, "word": " there."}]}]}',
                              encoding="utf-8")
        last = {"transcript": str(transcript), "name": "t", "srt": "", "language": "en", "languages": []}
        self.page._translate_transcript(last, quiet=True)
        self.assertEqual(FakeJob.made, [])
        self.assertIn("hasn't been OK'd", self.page._log[-1]["text"])
        self.page._translate_transcript(last)
        ask = self.last("ask")
        self.assertEqual(ask["kind"], "consent")
        self.page.on_answer({"id": ask["id"], "ok": True})
        self.assertEqual(len(FakeJob.made), 1)
        self.assertTrue(self.page.settings.get("ai_consent_anthropic"))

    def test_subtitle_conversion_suggests_the_topmost_empty_track(self):
        self.page.on_shown()
        c = self.last("convert")
        self.assertEqual((c["video"], c["subtitle"], c["sub_track"], c["target_track"]), (3, 1, 1, 4))

    def test_a_chosen_target_track_is_kept_until_used(self):
        self.page.on_shown()
        self.page.on_conv_option({"key": "target_track", "value": 2})
        FakeResolve.empty = 3                                           # e.g. a track emptied meanwhile
        self.page.on_shown()                                            # back from another page
        self.page.on_refresh_timeline()
        self.assertEqual(self.last("convert")["target_track"], 2)
        self.page.on_convert()
        self.assertEqual(FakeResolve.converted, [(1, 2)])               # the track chosen, not a new one
        self.assertEqual(self.last("convert")["target_track"], 3)       # used: back to the suggestion
        self.page.on_conv_option({"key": "target_track", "value": 2})
        FakeResolve.timeline = "Another timeline"
        self.page.on_shown()
        self.assertEqual(self.last("convert")["target_track"], 3)

    def test_converting_says_what_happened_and_offers_the_animation_page(self):
        self.page.on_shown()
        self.page.on_conv_option({"key": "sub_track", "value": 2})
        self.page.on_convert()
        self.assertEqual(FakeResolve.converted, [(2, 4)])
        self.assertEqual(self.host.busy, [True, False])
        self.assertEqual(self.last("toast"), {"text": "12 Text+ clips on video track 4", "style": True})
        texts = [e["text"] for e in self.page._log]
        self.assertIn("[Setup] Found a Text+ template in the Media Pool; using it for exact placement.", texts)
        self.assertFalse([t for t in texts if "Diagnostic" in t])

        FakeResolve.subtitles = 0
        self.page.on_convert()
        self.assertEqual(self.last("alert")["text"], "Subtitle track 2 has no subtitles.")
        self.assertEqual(self.host.busy[-1], False)

    def test_the_text_plus_tabs_are_hosted_here(self):
        self.assertEqual(self.page_mod.TABS, ("subtitles", "translate", "convert", "style", "layout",
                                              "animation", "words", "setup"))
        tools = self.page.textplus
        with mock.patch.object(tools, "tab_shown") as shown:
            self.page.on_tab({"tab": "style"})
            shown.assert_called_once_with()
        self.assertEqual(tools.tab, "style")
        self.page.on_run()                                                # a job running...
        self.page._dispatch("tp_set", {"name": "font_size", "value": 0.2})
        self.assertEqual(self.host.tools["text_animator"]["font_size"], 0.2)   # ...doesn't hold Text+ back
        self.assertEqual(self.last("tp_options")["sliders"]["font_size"]["value"], 0.2)
        self.page._dispatch("tp_nonsense", {})                            # unknown: ignored

    def test_setup_download_and_using_a_copy(self):
        self.page.on_download({"id": "large-v3-turbo"})
        job = FakeJob.made[-1]
        self.assertEqual(job.args, (es.download_model, "large-v3-turbo"))
        self.page._on_setup_line(plan.MARK + '{"type":"progress","done":8e8,"total":1.6e9}')
        self.assertEqual(self.last("job")["progress"], 50)
        job.succeeded.emit({})  # SetupJob's succeeded carries nothing; the fake's is a dict signal
        job.finished.emit()
        self.assertIn("downloaded", self.last("toast")["text"])
        self.page.found = {"small": "C:/hf/small"}
        self.page.on_use_copy({"id": "small", "path": "C:/elsewhere"})       # not what was found: ignored
        self.assertEqual(self.page.settings.get("extra_models"), {})
        self.page.on_use_copy({"id": "small", "path": "C:/hf/small"})
        self.assertEqual(self.page.settings.get("extra_models"), {"small": "C:/hf/small"})


if __name__ == "__main__":
    unittest.main()
