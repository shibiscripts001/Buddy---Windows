"""Starting Transcribe's audio render (app/pages/transcribe/resolve_ext.py
render_timeline_audio) against a fake Resolve whose StartRendering refuses
a set number of times first - what Resolve now and then does for a job that
starts fine a moment later. It's retried, but only START_TRIES times."""

import os
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.transcribe import resolve_ext
from pages.transcribe.resolve_ext import START_TRIES, TranscribeController, TranscribeResolveError


class Project:
    def __init__(self, refusals, starts_anyway=False):
        self.refusals, self.starts_anyway = refusals, starts_anyway
        self.start_calls, self.rendering, self.out = 0, False, None
        self.presets, self.jobs = set(), []

    # What render_timeline_audio reads and sets around the render.
    def GetCurrentTimeline(self):
        return object()

    def GetCurrentRenderMode(self):
        return 0

    def SetCurrentRenderMode(self, mode):
        return True

    def SaveAsNewRenderPreset(self, name):
        self.presets.add(name)
        return True

    def LoadRenderPreset(self, name):
        return name in self.presets

    def DeleteRenderPreset(self, name):
        self.presets.discard(name)
        return True

    def SetCurrentRenderFormatAndCodec(self, fmt, codec):
        return True

    def SetRenderSettings(self, settings):
        self.out = os.path.join(settings["TargetDir"], settings["CustomName"] + ".mov")
        return True

    def AddRenderJob(self):
        self.jobs.append("job-1")
        return "job-1"

    def DeleteRenderJob(self, job):
        self.jobs.remove(job)
        return True

    def StartRendering(self, job):
        self.start_calls += 1
        if self.start_calls <= self.refusals:
            self.rendering = self.starts_anyway
            return False
        self.rendering = True
        return True

    def IsRenderingInProgress(self):
        if self.rendering:          # renders instantly: in progress until it's asked once
            self.rendering = False
            with open(self.out, "wb"):
                pass
            return True
        return False

    def GetRenderJobStatus(self, job):
        return {"JobStatus": "Complete" if os.path.exists(self.out) else "Ready"}


class Controller:
    def __init__(self, project):
        self.project = project

    def current_project(self):
        return self.project


class RenderStartTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        patcher = mock.patch.object(resolve_ext, "START_RETRY_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def render(self, project):
        return TranscribeController(Controller(project)).render_timeline_audio(self.dir)

    def test_starts_first_time(self):
        project = Project(refusals=0)
        self.assertTrue(os.path.isfile(self.render(project)))
        self.assertEqual(project.start_calls, 1)

    def test_refused_then_started_is_retried(self):
        project = Project(refusals=START_TRIES - 1)
        self.assertTrue(os.path.isfile(self.render(project)))
        self.assertEqual(project.start_calls, START_TRIES)

    def test_gives_up_after_start_tries(self):
        project = Project(refusals=100)
        with self.assertRaisesRegex(TranscribeResolveError, "didn't start rendering"):
            self.render(project)
        self.assertEqual(project.start_calls, START_TRIES)
        self.assertEqual(project.jobs, [])          # the job is still cleaned up
        self.assertEqual(project.presets, set())    # and the user's settings restored

    def test_refused_but_rendering_counts_as_started(self):
        project = Project(refusals=1, starts_anyway=True)
        self.assertTrue(os.path.isfile(self.render(project)))
        self.assertEqual(project.start_calls, 1)

    def test_retries_stay_small(self):
        self.assertLessEqual(START_TRIES, 3)       # a first try plus at most two retries


if __name__ == "__main__":
    unittest.main()
