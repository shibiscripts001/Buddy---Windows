"""Word-by-word's Stack on their own tracks: where each selected clip goes
(word_by_word.stack_plan), the timeline file rewritten (transcribe/drt.py
restack_video_clips - its layout from a real Resolve 21.1 export) and the button
(text_plus.on_stack_words) against the fake Resolve of test_text_animator_page."""

import os
import re
import tempfile
import unittest
import zipfile

import _paths  # noqa: F401
from pages.transcribe import drt

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

SEQ = "3e7e3490-b2c8-4917-9fe0-3083267eab63"


def clip_xml(db, start, duration, text):
    # A Fusion Title as Resolve writes it, cut down: its Fusion composition, and a nested
    # <Element> the way some of Resolve's own fields hold one.
    return f"""     <Element>
      <Sm2TiVideoClip DbId="{db}">
       <FieldsBlob>000000020000002c800a170a13</FieldsBlob>
       <PrettyType>Fusion Title</PrettyType>
       <Name>{text}</Name>
       <Start>{start}</Start>
       <Duration>{duration}</Duration>
       <CompositionTable>
        <Sm2TiCompositionTable DbId="c-{db}">
         <CompositionBA>0a0b{text.encode().hex()}</CompositionBA>
        </Sm2TiCompositionTable>
       </CompositionTable>
       <MarkersVec>
        <Element>
         <Start>{start + 1}</Start>
        </Element>
       </MarkersVec>
      </Sm2TiVideoClip>
     </Element>
"""


def track_xml(db, items, name=""):
    body = f"<Items>\n{''.join(items)}    </Items>" if items else "<Items/>"
    return f"""  <Element>
   <Sm2TiTrack DbId="{db}">
    <FieldsBlob>000000010000000100000012004e0075006d004c00610079006500720073000000020000000000</FieldsBlob>
    <Type>0</Type>
    <Sequence>{SEQ}</Sequence>
    {body}
    <FusionCompHolderItems/>
    <UserDefinedName>{name}</UserDefinedName>
    <LayersVec/>
   </Sm2TiTrack>
  </Element>
"""


TRANSITION = """     <Element>
      <Sm2TiVideoTransition DbId="tr-1">
       <Start>86500</Start>
       <Duration>12</Duration>
      </Sm2TiVideoTransition>
     </Element>
"""

SUBTITLES = """ <SubtitleTrackVec>
  <Element>
   <Sm2TiTrack DbId="st-1">
    <Type>2</Type>
    <Items/>
   </Sm2TiTrack>
  </Element>
 </SubtitleTrackVec>"""


def sequence():
    footage = clip_xml("v1-footage", 86400, 716, "C1867.MP4")
    words = [clip_xml("w-You", 86400, 10, "You"), clip_xml("w-see", 86410, 10, "see"),
             clip_xml("w-this", 86420, 10, "this"), clip_xml("w-later", 86440, 10, "later")]
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!--DbAppVer="21.1.0.0017" DbPrjVer="17"-->
<Sm2SequenceContainer DbId="seq-container">
 <FieldsBlob/>
 <VideoTrackVec>
{track_xml("t-1", [footage], "Footage")}{track_xml("t-2", words + [TRANSITION], "Words")} </VideoTrackVec>
 <AudioTrackVec/>
{SUBTITLES}
 <GeometryTrackVec/>
</Sm2SequenceContainer>
"""


def tracks_of(xml):
    """[[(name, start, duration)] per video track], [UserDefinedName per track], [DbId per track]."""
    vec = re.search(r"<VideoTrackVec>(.*?)</VideoTrackVec>", xml, re.S).group(1)
    tracks = drt._elements(vec)
    clips = [[(re.search(r"<Name>(.*?)</Name>", b).group(1), drt._start_of(b),
               int(re.search(r"<Duration>(\d+)</Duration>", b).group(1)))
              for b in drt._elements(re.search(r"<Items>(.*?)</Items>|<Items/>", t, re.S).group(1) or "")
              if "Sm2TiVideoClip" in b] for t in tracks]
    names = [re.search(r"<UserDefinedName>(.*?)</UserDefinedName>|<UserDefinedName/>", t).group(1) or "" for t in tracks]
    ids = [re.search(r'<Sm2TiTrack DbId="([^"]+)"', t).group(1) for t in tracks]
    return clips, names, ids


class PlanTests(unittest.TestCase):
    def setUp(self):
        from pages.text_animator.word_by_word import stack_plan
        self.plan = stack_plan

    def test_each_goes_on_the_lowest_free_track_from_its_own_up(self):
        clips = [(1, 0, 100), (2, 0, 10), (2, 10, 20), (2, 20, 30), (2, 40, 50)]
        plan = self.plan(clips, [(2, 0, 10), (2, 10, 20), (2, 20, 30)], 30)
        self.assertEqual(plan, {(2, 0): (2, 30), (2, 10): (3, 20), (2, 20): (4, 10)})

    def test_something_in_the_way_is_stepped_over(self):
        clips = [(2, 0, 10), (2, 10, 20), (3, 5, 60)]
        plan = self.plan(clips, [(2, 0, 10), (2, 10, 20)], 30)
        self.assertEqual(plan, {(2, 0): (2, 30), (2, 10): (4, 20)})     # track 3 is busy till 60

    def test_a_clip_after_the_playhead_is_no_obstacle(self):
        plan = self.plan([(2, 0, 10), (2, 30, 40)], [(2, 0, 10)], 30)
        self.assertEqual(plan, {(2, 0): (2, 30)})

    def test_they_end_at_the_playhead_longer_or_shorter(self):
        plan = self.plan([(1, 0, 10), (2, 5, 90)], [(1, 0, 10), (2, 5, 90)], 40)
        self.assertEqual(plan, {(1, 0): (1, 40), (2, 5): (2, 35)})


class DrtTests(unittest.TestCase):
    MOVES = {(2, 86400): (2, 30), (2, 86410): (3, 20), (2, 86420): (4, 10)}

    def test_the_clips_go_to_their_tracks_the_rest_stays(self):
        xml = drt.restack_video_clips(sequence(), self.MOVES)
        clips, names, ids = tracks_of(xml)
        self.assertEqual(clips, [[("C1867.MP4", 86400, 716)],
                                 [("You", 86400, 30), ("later", 86440, 10)],
                                 [("see", 86410, 20)],
                                 [("this", 86420, 10)]])
        self.assertEqual(names, ["Footage", "Words", "", ""])          # new tracks, no name of their own
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual(ids[:2], ["t-1", "t-2"])
        self.assertIn("Sm2TiVideoTransition", xml)                      # what isn't moved is kept
        self.assertIn(f"0a0b{'see'.encode().hex()}", xml)               # the composition goes with its clip
        self.assertEqual(xml.count("<Sm2TiVideoClip "), 5)
        self.assertIn(SUBTITLES, xml)
        self.assertTrue(xml.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!--DbAppVer'))

    def test_nothing_but_the_moves_changes(self):
        xml = drt.restack_video_clips(sequence(), {})
        self.assertEqual(tracks_of(xml)[0], tracks_of(sequence())[0])

    def test_a_clip_not_there_changes_nothing(self):
        with self.assertRaises(drt.DrtError) as caught:
            drt.restack_video_clips(sequence(), {(2, 99999): (3, 10)})
        self.assertIn("nothing was changed", str(caught.exception))

    def test_a_file_in_another_form_is_refused(self):
        with self.assertRaises(drt.DrtError):
            drt.restack_video_clips("<Sm2SequenceContainer/>", self.MOVES)

    def test_the_file_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            src, dst = os.path.join(folder, "a.drt"), os.path.join(folder, "b.drt")
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("project.xml", "<SM_Project/>")
                z.writestr("SeqContainer/seq.xml", sequence())
            drt.with_video_clips_restacked(src, dst, self.MOVES)
            with zipfile.ZipFile(dst) as z:
                self.assertEqual(z.read("project.xml"), b"<SM_Project/>")
                self.assertEqual(len(tracks_of(z.read("SeqContainer/seq.xml").decode())[0]), 4)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ButtonTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        from test_text_animator_page import Clip, Host, HostPage, Timeline, Tool
        from pages.text_animator.text_plus import TextPlusTools
        s = 86400
        self.footage = Clip("C1867.MP4", s, s + 700, None)
        self.words = [Clip(w, s + i * 3, s + i * 3 + 3, Tool(w)) for i, w in enumerate(["You", "have", "to", "see"])]
        self.host = Host(Timeline([[self.footage], self.words]))
        self.host.timeline.playhead = "01:00:00:20"                     # frame 86420
        hp = HostPage(self.host)
        hp.tab = "wordbyword"
        self.hp = hp
        self.page = TextPlusTools(hp)
        self.addCleanup(self.page.deleteLater)
        self.plans = []
        self.page._stack_into_new_timeline = lambda _r, _t, plan: (self.plans.append(plan), "Promo (Word-by-word)")[1]

    def last(self, name):
        return [p for n, p in self.hp.sent if n == name][-1]

    def test_the_selected_text_plus_are_stacked_in_a_new_timeline(self):
        self.host.timeline.selected = [self.footage] + self.words        # the footage isn't Text+
        self.page.on_stack_words()
        s = 86400
        self.assertEqual(self.plans, [{(2, s): (2, 20), (2, s + 3): (3, 17), (2, s + 6): (4, 14), (2, s + 9): (5, 11)}])
        self.assertEqual(self.last("toast")["text"], "Stacked 4 Text+ clips on 4 tracks in 'Promo (Word-by-word)'")

    def test_a_word_left_out_before_the_playhead_is_stepped_over(self):
        self.host.timeline.selected = self.words[:2]                    # "to" and "see" stay on track 2
        self.page.on_stack_words()
        self.assertEqual(self.plans, [{(2, 86400): (3, 20), (2, 86403): (4, 17)}])
        self.assertEqual(self.host.busy, [True, False])

    def test_nothing_selected_is_said(self):
        self.page.on_stack_words()
        self.assertEqual(self.plans, [])
        self.assertIn("Select the Text+ clips", self.last("toast")["text"])

    def test_a_clip_starting_after_the_playhead_is_said(self):
        self.host.timeline.playhead = "01:00:00:05"                     # "to" and "see" start after it
        self.host.timeline.selected = list(self.words)
        self.page.on_stack_words()
        self.assertEqual(self.plans, [])
        self.assertIn("'to' starts at or after the playhead", self.last("toast")["text"])

    def test_an_older_resolve_is_said(self):
        del type(self.host.timeline).GetSelectedClips
        try:
            self.page.on_stack_words()
        finally:
            from test_text_animator_page import Timeline
            Timeline.GetSelectedClips = lambda tl: list(tl.selected)
        self.assertIn("21.0.4", self.last("toast")["text"])

    def test_selected_clips_without_words_are_not_stacked(self):
        self.words[1].tool.inputs["StyledText"] = ""                    # emptied by Resolve's Inspector
        self.host.timeline.selected = list(self.words)
        self.page.on_stack_words()
        self.assertEqual(self.plans, [])
        text = self.last("alert")["text"]
        self.assertIn("1 of the 4 selected Text+ clips have no text", text)
        self.assertIn("Inspector", text)

    def test_a_failure_is_said_and_nothing_else(self):
        def fail(*_a):
            raise drt.DrtError("Resolve didn't import the new timeline.")
        self.page._stack_into_new_timeline = fail
        self.host.timeline.selected = self.words[:2]
        self.page.on_stack_words()
        self.assertEqual(self.last("alert")["text"], "Resolve didn't import the new timeline.")
        self.assertEqual(self.host.busy, [True, False])


if __name__ == "__main__":
    unittest.main()
