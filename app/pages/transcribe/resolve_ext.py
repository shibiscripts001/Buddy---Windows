#!/usr/bin/env python3
"""
The Resolve half of Transcribe: render the timeline's audio, and put an SRT
on its subtitle track. Every call here was measured live on Resolve Studio
21.1 before being written (see the memory note "Resolve render + subtitle
API"); the ones that looked plausible and don't work are listed too, so
nobody "simplifies" back to them.

Rendering
  - WAV cannot be selected through the API (SetCurrentRenderFormatAndCodec
    ("wav", ...) is always False), so the audio comes out as an audio-only
    QuickTime: mov/ProRes422P with ExportVideo off and 16-bit LPCM audio.
    faster-whisper reads it directly.
  - StartRendering takes the job id itself; given a LIST it returns False.
    Rarely it returns False for a good job too, so it's retried a couple of
    times (START_TRIES) before giving up.
  - There is no GetRenderSettings(), so the user's Deliver settings are
    saved as a temporary preset first and loaded back after, whatever
    happens. Render mode is saved separately. The job is always deleted.

Subtitles
  - An SRT imports as one "Subtitle" media pool item; AppendToTimeline
    places each cue as its own subtitle clip, at SRT time = timeline start
    - EXCEPT the first time on a timeline that has anything on it: then it
    really appends, and every cue lands after the end of the timeline's
    content (12 minutes late on a 12-minute timeline; measured 2026-09-28,
    playhead at the start, 30 s in, at the end or untouched alike). Placed
    again after that, the same cues land exactly. So every placement is
    checked cue by cue against the SRT and redone if it's off - and if it's
    still off, taken off again (PLACE_TRIES): never left misplaced.
  - It needs a subtitle track to exist, and it ALWAYS lands on track 1 -
    new tracks, trackIndex, locking or disabling track 1 don't redirect it.
    So existing subtitles on track 1 are either replaced (the caller asks
    the user first) or the SRT is only saved to disk.
  - DeleteFolders resets the current bin, so the user's bin is restored
    last. This module never deletes a bin; imported SRTs are kept in a
    "Buddy Transcripts" bin so the user can see what was brought in.

A subtitle track per language (timeline_with_subtitles)
  - Placement can't be steered to another track: the destination moves
    with its subtitles when a track is inserted above, locked or disabled
    tracks make it fail, and subtitles appended to a used track go after
    its end. A full clipInfo (trackIndex + recordFrame) CRASHED Resolve.
  - So the timeline is exported as .drt, its subtitle tracks rewritten
    (drt.py) and imported back as a NEW timeline - importSourceClips=False
    with every Media Pool folder, as project_setup does, so it reuses the
    clips already there. The original timeline is left as it was.
  - ImportTimelineFromFile ignores timelineName for a .drt (the timeline is
    named after the file), so it's renamed with SetName afterwards.
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from dataclasses import dataclass

from . import drt
from . import subtitles as st

TRANSCRIPTS_BIN = "Buddy Transcripts"
PLACE_TRIES = 2            # the first can land after the timeline's end; the second hasn't yet
PLACE_TOLERANCE = 1        # frames a cue may be off (rounding) and still be where it belongs
START_TRIES = 3            # StartRendering: the first try plus two retries, never more
START_RETRY_WAIT = 1.0     # seconds between them


class TranscribeResolveError(RuntimeError):
    pass


class RenderCancelled(Exception):
    pass


@dataclass
class TimelineInfo:
    name: str
    start_frame: int
    end_frame: int
    fps: float
    start_timecode: str
    subtitle_items_on_track1: int

    @property
    def duration_seconds(self) -> float:
        return max(0, self.end_frame - self.start_frame) / (self.fps or 24.0)


class TranscribeController:
    """Wraps the shell's ResolveController for this tool's calls."""

    def __init__(self, controller):
        self.controller = controller

    # ------------------------------------------------------------- reads

    def _project(self):
        project = self.controller.current_project()
        if project is None:
            raise TranscribeResolveError("No project is open in Resolve.")
        return project

    def _timeline(self, project=None):
        timeline = (project or self._project()).GetCurrentTimeline()
        if timeline is None:
            raise TranscribeResolveError("Open a timeline in Resolve first.")
        return timeline

    def timeline_info(self) -> TimelineInfo:
        project = self._project()
        tl = self._timeline(project)
        try:
            fps = float(tl.GetSetting("timelineFrameRate") or project.GetSetting("timelineFrameRate") or 24)
        except (TypeError, ValueError):
            fps = 24.0
        return TimelineInfo(
            name=tl.GetName(),
            start_frame=int(tl.GetStartFrame()),
            end_frame=int(tl.GetEndFrame()),
            fps=fps,
            start_timecode=tl.GetStartTimecode() or "",
            subtitle_items_on_track1=len(self._subtitle_items(tl, 1)),
        )

    @staticmethod
    def _subtitle_items(tl, index):
        try:
            if (tl.GetTrackCount("subtitle") or 0) < index:
                return []
            return list(tl.GetItemListInTrack("subtitle", index) or [])
        except Exception:
            return []

    # ------------------------------------------------------------ render

    def render_timeline_audio(self, out_dir: str, progress=lambda pct: None,
                              cancelled=lambda: False, frame_range=None) -> str:
        """Render the current timeline's audio mix to an audio-only .mov in
        out_dir and return its path - the whole timeline, or frame_range
        (first, last) in timeline frames. Deliver settings, render mode and
        the render queue are left exactly as they were."""
        project = self._project()
        self._timeline(project)
        if project.IsRenderingInProgress():
            raise TranscribeResolveError(
                "Resolve is already rendering. Wait for it to finish, then try again.")
        os.makedirs(out_dir, exist_ok=True)
        name = f"buddy_transcribe_{uuid.uuid4().hex[:8]}"
        preset = f"_buddy_restore_{uuid.uuid4().hex[:8]}"
        mode_before = project.GetCurrentRenderMode()
        if not project.SaveAsNewRenderPreset(preset):
            raise TranscribeResolveError(
                "Couldn't save your current render settings, so Buddy won't change them. "
                "Nothing was rendered.")
        job = None
        try:
            if project.GetCurrentRenderMode() != 1:
                project.SetCurrentRenderMode(1)   # 1 = Single clip
            if not project.SetCurrentRenderFormatAndCodec("mov", "ProRes422P"):
                raise TranscribeResolveError("Resolve refused the audio render format.")
            span = ({"SelectAllFrames": False, "MarkIn": int(frame_range[0]),
                     "MarkOut": int(frame_range[1])} if frame_range else {"SelectAllFrames": True})
            ok = project.SetRenderSettings({
                **span,
                "TargetDir": out_dir,
                "CustomName": name,
                "ExportVideo": False,
                "ExportAudio": True,
                "AudioCodec": "lpcm",
                "AudioBitDepth": 16,
                "AudioSampleRate": 48000,
            })
            if not ok:
                raise TranscribeResolveError("Resolve refused the audio render settings.")
            job = project.AddRenderJob()
            if not job:
                raise TranscribeResolveError("Resolve couldn't add the audio render job.")
            if not self._start_rendering(project, job):
                raise TranscribeResolveError("Resolve didn't start rendering the timeline audio.")
            while project.IsRenderingInProgress():
                if cancelled():
                    project.StopRendering()
                    raise RenderCancelled()
                status = project.GetRenderJobStatus(job) or {}
                progress(int(status.get("CompletionPercentage") or 0))
                time.sleep(0.5)
            status = project.GetRenderJobStatus(job) or {}
            if status.get("JobStatus") != "Complete":
                raise TranscribeResolveError(
                    f"The audio render didn't complete ({status.get('JobStatus') or 'unknown status'}).")
            progress(100)
        finally:
            if job:
                project.DeleteRenderJob(job)
            project.LoadRenderPreset(preset)
            project.DeleteRenderPreset(preset)
            if project.GetCurrentRenderMode() != mode_before:
                project.SetCurrentRenderMode(mode_before)
        path = os.path.join(out_dir, name + ".mov")
        if not os.path.isfile(path):
            raise TranscribeResolveError(f"The render finished but {path} wasn't written.")
        return path

    @staticmethod
    def _start_rendering(project, job) -> bool:
        """StartRendering, tried up to START_TRIES times. Now and then Resolve
        refuses it for no reason it reports, and the same job starts fine a
        moment later (never reproduced on demand - 20 tries from every page,
        playing, minimized, from a thread all started first time). A refusal
        that started rendering anyway counts as started."""
        for attempt in range(START_TRIES):
            if attempt:
                time.sleep(START_RETRY_WAIT)
            if project.StartRendering(job) or project.IsRenderingInProgress():
                return True
        return False

    # --------------------------------------------------------- subtitles

    def place_subtitles(self, srt_path: str, replace_existing: bool = False) -> int:
        """Import srt_path and put it on subtitle track 1 of the current
        timeline. Returns how many subtitle clips were placed. Raises if
        track 1 already has subtitles and replace_existing is False - the
        page asks the user before passing True."""
        project = self._project()
        tl = self._timeline(project)
        mp = project.GetMediaPool()
        existing = self._subtitle_items(tl, 1)
        if existing and not replace_existing:
            raise TranscribeResolveError(
                f"Subtitle track 1 already has {len(existing)} subtitle(s).")

        previous_bin = mp.GetCurrentFolder()
        try:
            target_bin = self._transcripts_bin(mp)
            if target_bin is not None:
                mp.SetCurrentFolder(target_bin)
            items = mp.ImportMedia([srt_path]) or []
            if not items:
                raise TranscribeResolveError("Resolve couldn't import the subtitle file.")
            if (tl.GetTrackCount("subtitle") or 0) == 0:
                if not tl.AddTrack("subtitle"):
                    raise TranscribeResolveError("Resolve couldn't add a subtitle track.")
            if existing:
                tl.DeleteClips(existing)
            expected = self._cue_frames(srt_path, tl, project)
            off = 0
            for _try in range(PLACE_TRIES):
                mp.AppendToTimeline([items[0]])
                placed = self._subtitle_items(tl, 1)
                if not placed:
                    raise TranscribeResolveError("Resolve imported the subtitles but didn't place them.")
                off = self._misplaced_by(placed, expected)
                if off is None:
                    return len(placed)
                tl.DeleteClips(placed)   # somewhere else: off again, and once more
            seconds = off / self._fps(tl, project)
            raise TranscribeResolveError(
                f"Resolve put the subtitles {abs(seconds):.1f} s {'late' if seconds > 0 else 'early'}, "
                "so they were taken off again – the SRT file is saved.")
        finally:
            if previous_bin is not None:
                mp.SetCurrentFolder(previous_bin)

    @staticmethod
    def _fps(tl, project) -> float:
        try:
            return float(tl.GetSetting("timelineFrameRate") or project.GetSetting("timelineFrameRate") or 24)
        except (TypeError, ValueError):
            return 24.0

    @classmethod
    def _cue_frames(cls, srt_path, tl, project) -> list[int]:
        """Where each cue of the SRT belongs: the timeline frame its start
        is at (SRT 0 = the timeline's first frame), in time order."""
        with open(srt_path, encoding="utf-8-sig") as f:
            cues = st.parse_srt(f.read())
        fps, start = cls._fps(tl, project), int(tl.GetStartFrame())
        return sorted(start + round(c["start"] * fps) for c in cues)

    @staticmethod
    def _misplaced_by(placed, expected) -> int | None:
        """None if every subtitle placed starts where its cue belongs (within
        PLACE_TOLERANCE); else how many frames the furthest one is off (+:
        late). Compared in time order; if Resolve dropped or merged some,
        the first and last still have to line up."""
        got = sorted(int(item.GetStart()) for item in placed)
        if not got or not expected:
            return None
        pairs = list(zip(got, expected)) if len(got) == len(expected) else [(got[0], expected[0]),
                                                                              (got[-1], expected[-1])]
        worst = max((g - e for g, e in pairs), key=abs)
        return None if abs(worst) <= PLACE_TOLERANCE else worst

    def timeline_with_subtitles(self, tracks: list, work_dir: str) -> tuple[str, list[int]]:
        """A copy of the current timeline with one subtitle track per
        drt.Track (in order: track 1 first), imported as a new timeline and
        opened. Returns (its name, subtitles on each track)."""
        project = self._project()
        tl = self._timeline(project)
        mp = project.GetMediaPool()
        resolve = self.controller.resolve
        try:
            fps = float(tl.GetSetting("timelineFrameRate") or project.GetSetting("timelineFrameRate") or 24)
        except (TypeError, ValueError):
            fps = 24.0
        taken = {project.GetTimelineByIndex(i).GetName() for i in range(1, (project.GetTimelineCount() or 0) + 1)}
        name, n = f"{tl.GetName()} (Subtitles)", 2
        while name in taken:
            name, n = f"{tl.GetName()} (Subtitles {n})", n + 1
        os.makedirs(work_dir, exist_ok=True)
        folder = tempfile.mkdtemp(prefix="subtitles ", dir=work_dir)
        exported, built = os.path.join(folder, "timeline.drt"), os.path.join(folder, "subtitles.drt")
        try:
            if not tl.Export(exported, resolve.EXPORT_DRT, resolve.EXPORT_NONE) or not os.path.isfile(exported):
                raise TranscribeResolveError("Resolve couldn't export the timeline.")
            try:
                drt.with_subtitle_tracks(exported, built, tracks, int(tl.GetStartFrame()), fps)
            except drt.DrtError as exc:
                raise TranscribeResolveError(str(exc)) from None
            made = mp.ImportTimelineFromFile(built, {
                "timelineName": name, "importSourceClips": False,
                "sourceClipsFolders": self._all_folders(mp.GetRootFolder())})
            if not made:
                raise TranscribeResolveError("Resolve didn't import the new timeline.")
            made.SetName(name)   # a .drt comes in named after the file, whatever timelineName says
            project.SetCurrentTimeline(made)
            counts = [len(self._subtitle_items(made, i)) for i in range(1, len(tracks) + 1)]
            return made.GetName(), counts
        finally:
            for path in (exported, built):
                try:
                    os.remove(path)
                except OSError:
                    pass
            try:
                os.rmdir(folder)
            except OSError:
                pass

    @classmethod
    def _all_folders(cls, folder) -> list:
        """Every Media Pool folder - all of them, so the import finds each
        clip where it already is (see project_setup/resolve_ext.py)."""
        found = [folder]
        for sub in folder.GetSubFolderList() or []:
            found.extend(cls._all_folders(sub))
        return found

    @staticmethod
    def _transcripts_bin(mp):
        root = mp.GetRootFolder()
        for folder in root.GetSubFolderList() or []:
            if folder.GetName() == TRANSCRIPTS_BIN:
                return folder
        return mp.AddSubFolder(root, TRANSCRIPTS_BIN)
