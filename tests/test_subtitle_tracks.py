"""A subtitle track per language, written into a Resolve timeline file
(app/pages/transcribe/drt.py) - no Resolve. The blobs and layout come from
a real Resolve 21.1 export."""

import os
import re
import tempfile
import unittest
import zipfile

import _paths  # noqa: F401
from pages.transcribe import drt
from pages.transcribe import subtitles as st

SEQ = "6afd8594-b2e2-46d2-abcd-f149aaa5a9ca"


def sequence_xml(subtitles: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!--DbAppVer="21.1.0.0017" DbPrjVer="17"-->
<Sm2SequenceContainer DbId="1a33767f-b63c-4e0b-b9a7-c0c983ea3ccd">
 <FieldsBlob/>
 <VideoTrackVec>
  <Element>
   <Sm2TiTrack DbId="2da3384d-46fa-43dd-8c1e-ab340828a0c6">
    <Type>0</Type>
    <Sequence>{SEQ}</Sequence>
    <Items/>
   </Sm2TiTrack>
  </Element>
 </VideoTrackVec>
 {subtitles}
 <GeometryTrackVec/>
</Sm2SequenceContainer>
"""


OLD_TRACK = """<SubtitleTrackVec>
  <Element>
   <Sm2TiTrack DbId="4a30d211-1527-43b0-9496-e20f12c2001a">
    <Items>
     <Element><Sm2TiGenerator DbId="be74"><Name>the old subtitle</Name><Start>86687</Start></Sm2TiGenerator></Element>
    </Items>
    <UserDefinedName/>
   </Sm2TiTrack>
  </Element>
 </SubtitleTrackVec>"""


class BlobTests(unittest.TestCase):
    def test_item_blob_is_resolves_own(self):
        self.assertEqual(drt.item_blob(86687),
                         "000000020000001b800a05a8019fa505121100000024789c6366606420040000b20005")
        self.assertTrue(drt.item_blob(148111).endswith("a8018f8509121100000024789c6366606420040000b20005"))
        small = bytes.fromhex(drt.item_blob(5))   # a shorter varint: the length field follows
        self.assertEqual(int.from_bytes(small[4:8], "big"), len(small) - 8)


class FrameTests(unittest.TestCase):
    def test_times_become_record_frames_without_overlaps(self):
        cues = [(4.0, 6.0, "second"), (1.0, 5.0, "first\nof two lines"), (7.0, 7.0, "blink"), (8.0, 9.0, "  ")]
        self.assertEqual(drt.to_frames(cues, 86400, 24.0),
                         [(86424, 72, "first\nof two lines"),   # cut short where "second" starts
                          (86496, 48, "second"),
                          (86568, 1, "blink")])               # at least a frame; the empty one is gone
        same_start = drt.to_frames([(1.0, 2.0, "a"), (1.0, 3.0, "b")], 0, 24.0)
        self.assertEqual(same_start, [(24, 1, "a"), (25, 47, "b")])


class TimelineFileTests(unittest.TestCase):
    def build(self, subtitles: str, tracks):
        folder = tempfile.mkdtemp()
        src, dst = os.path.join(folder, "in.drt"), os.path.join(folder, "out.drt")
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("project.xml", "<SM_Project/>")
            z.writestr("MediaPool/Master/MpFolder.xml", "<pool/>")
            z.writestr("SeqContainer/1a33767f.xml", sequence_xml(subtitles))
        drt.with_subtitle_tracks(src, dst, tracks, 86400, 24.0)
        with zipfile.ZipFile(dst) as z:
            return {name: z.read(name).decode("utf-8") for name in z.namelist()}

    def test_a_track_per_language_replaces_the_old_ones(self):
        out = self.build(OLD_TRACK, [drt.Track("English", [(1.0, 3.0, "Hello & <welcome>\nsecond line")]),
                                     drt.Track("Español", [(1.0, 3.0, "Hola"), (4.0, 5.5, "Adiós")])])
        self.assertEqual((out["project.xml"], out["MediaPool/Master/MpFolder.xml"]), ("<SM_Project/>", "<pool/>"))
        xml = out["SeqContainer/1a33767f.xml"]
        self.assertNotIn("the old subtitle", xml)
        self.assertEqual(re.findall(r"<UserDefinedName>([^<]*)</UserDefinedName>", xml), ["English", "Español"])
        self.assertEqual(re.findall(r"<Start>(\d+)</Start>", xml), ["86424", "86424", "86496"])
        # HTML for Resolve (so "<welcome>" isn't taken for a tag), then escaped again for the XML.
        self.assertIn("<Name>Hello &amp;amp; &amp;lt;welcome&amp;gt;&lt;br&gt;second line</Name>", xml)
        self.assertEqual(xml.count(f"<Sequence>{SEQ}</Sequence>"), 3)   # the video track's and both new ones
        self.assertIn(f"<FieldsBlob>{drt.item_blob(86424)}</FieldsBlob>", xml)
        self.assertEqual(len(set(re.findall(r'DbId="([^"]+)"', xml))), len(re.findall(r'DbId="', xml)))
        self.assertTrue(xml.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!--DbAppVer'))

    def test_a_timeline_with_no_subtitle_track_yet(self):
        xml = self.build("<SubtitleTrackVec/>", [drt.Track("Français", [(0.5, 1.5, "Bonjour")])])[
            "SeqContainer/1a33767f.xml"]
        self.assertIn("<UserDefinedName>Français</UserDefinedName>", xml)
        self.assertIn("<Start>86412</Start>", xml)

    def test_something_else_is_refused(self):
        folder = tempfile.mkdtemp()
        src = os.path.join(folder, "in.drt")
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("project.xml", "<SM_Project/>")
        with self.assertRaises(drt.DrtError):
            drt.with_subtitle_tracks(src, os.path.join(folder, "out.drt"), [], 0, 24.0)
        with open(src, "w") as f:
            f.write("not a zip")
        with self.assertRaises(drt.DrtError):
            drt.with_subtitle_tracks(src, os.path.join(folder, "out.drt"), [], 0, 24.0)


class SrtLinesTests(unittest.TestCase):
    def test_lines_can_stay_as_shown(self):
        text = "1\n00:00:01,000 --> 00:00:02,500\nFirst line\n<i>second</i>\n"
        self.assertEqual(st.parse_srt(text)[0]["text"], "First line second")
        self.assertEqual(st.parse_srt(text, keep_lines=True)[0], {"start": 1.0, "end": 2.5,
                                                                 "text": "First line\nsecond"})


if __name__ == "__main__":
    unittest.main()
