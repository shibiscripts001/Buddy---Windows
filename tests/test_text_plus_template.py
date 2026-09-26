"""Text Animator's Text+ template (app/pages/text_animator/subtitle_engine.py):
the file Buddy ships, and adding it to a Media Pool that has no Text+ -
against fake Resolve objects."""

import re
import unittest
import zipfile

import _paths  # noqa: F401
from pages.text_animator import subtitle_engine as se


class FakeClip:
    def __init__(self, name, kind):
        self.name, self.kind = name, kind

    def GetName(self):
        return self.name

    def GetClipProperty(self):
        return {"Type": self.kind}


class FakeFolder:
    def __init__(self, name, clips=(), subs=()):
        self.name, self.clips, self.subs = name, list(clips), list(subs)

    def GetName(self):
        return self.name

    def GetClipList(self):
        return self.clips

    def GetSubFolderList(self):
        return self.subs


class FakePool:
    def __init__(self, root, import_works=True):
        self.root, self.current, self.import_works = root, root.subs[0] if root.subs else root, import_works
        self.imported_into = None

    def GetRootFolder(self):
        return self.root

    def GetCurrentFolder(self):
        return self.current

    def SetCurrentFolder(self, folder):
        self.current = folder
        return True

    def ImportFolderFromFile(self, path):
        self.imported_into = self.current
        if self.import_works:
            self.current.subs.append(FakeFolder(se.TEMPLATE_BIN, [FakeClip("Text+", "Fusion Title")]))
        return self.import_works


class TemplateFileTests(unittest.TestCase):
    def test_one_text_plus_and_nothing_from_the_pc_it_came_from(self):
        with zipfile.ZipFile(se.TEMPLATE_FILE) as z:
            files = {name: z.read(name).decode("utf-8") for name in z.namelist()}
        pool = "".join(t for n, t in files.items() if n.startswith("MediaPool/"))
        self.assertEqual(pool.count("<Sm2MpGenerator"), 1)
        self.assertIn("<Name>Buddy Text+</Name>", pool)
        project = files["project.xml"]
        self.assertIn("<User/>", project)
        self.assertIn("<SysId/>", project)
        self.assertNotRegex(project, r"<User>[^<]+</User>|<SysId>[^<]+</SysId>|New Project")
        self.assertIsNone(re.search(r"[A-Za-z]:\\\\|/Users/", "".join(files.values())))


class AddTemplateTests(unittest.TestCase):
    def test_added_at_the_top_and_the_selected_bin_put_back(self):
        selected = FakeFolder("Footage")
        root = FakeFolder("Master", subs=[selected])
        pool = FakePool(root)
        clip = se.TextPlusGenerator()._add_text_plus_template(pool)
        self.assertEqual((clip.GetName(), pool.imported_into, pool.current), ("Text+", root, selected))

    def test_nothing_when_the_import_fails(self):
        root = FakeFolder("Master", subs=[FakeFolder("Footage")])
        self.assertIsNone(se.TextPlusGenerator()._add_text_plus_template(FakePool(root, import_works=False)))
        self.assertIsNone(se.TextPlusGenerator()._add_text_plus_template(None))

    def test_only_a_real_title_counts_as_the_template(self):
        find = se.TextPlusGenerator()._get_media_pool_text_plus_item
        timeline_only = FakeFolder("Master", [FakeClip("Title sequence", "Timeline")])
        self.assertIsNone(find(FakePool(timeline_only)))
        template = FakeClip("Text+", "Fusion Title")
        root = FakeFolder("Master", [FakeClip("Title sequence", "Timeline")], [FakeFolder("Bin", [template])])
        self.assertIs(find(FakePool(root)), template)


class FakeSubtitleClip:
    """What Resolve 21.1 gives for a subtitle clip: False for every property."""

    def __init__(self, props=None):
        self.props = props or {}

    def GetProperty(self, key=None):
        return self.props.get(key, False)


class StyleTests(unittest.TestCase):
    def test_resolves_false_isnt_size_zero_and_black(self):
        extract = se.SubtitleExtractor()._extract_styling
        self.assertEqual(extract(FakeSubtitleClip()), (None, None, None))
        self.assertEqual(extract(FakeSubtitleClip({"Size": 0, "Font": ""})), (None, None, None))
        self.assertEqual(extract(FakeSubtitleClip({"Font": "Meiryo", "Size": "0.06", "Red": 255, "Green": 0,
                                                   "Blue": 51})), ("Meiryo", 0.06, (1.0, 0.0, 0.2)))

    def test_resolves_line_separator_becomes_a_real_line_break(self):
        self.assertEqual(se.normalize_line_breaks("興 味に"), "興\n味に")
        self.assertEqual(se.normalize_line_breaks("a\r\nb\rc d"), "a\nb\nc\nd")
        self.assertIsNone(se.normalize_line_breaks(None))

        class Clip:
            def GetName(self):
                return "first line second line"

            def GetStart(self):
                return 100

            def GetEnd(self):
                return 150

            def GetProperty(self, key=None):
                return False

        class Timeline:
            def GetTrackCount(self, kind):
                return 1

            def GetItemListInTrack(self, kind, index):
                return [Clip()]

        [sub] = se.SubtitleExtractor().extract_subtitles_from_track(Timeline(), 1)
        self.assertEqual(sub.text, "first line\nsecond line")

    def test_a_font_with_the_characters(self):
        from pages.text_animator import font_utils as fu
        self.assertEqual(fu.script_of(["米は日本酒", "大事とか"]), "ja")   # kana anywhere: all Japanese
        self.assertEqual((fu.script_of(["你好"]), fu.script_of(["안녕하세요"]), fu.script_of(["Hello"])),
                         ("zh", "ko", ""))
        self.assertEqual(fu.font_for_texts(["ということで"], {"Meiryo", "Arial"}), "Meiryo")
        self.assertEqual(fu.font_for_texts(["ということで"], {"Arial"}), "")
        self.assertEqual(fu.font_for_texts(["Hello"], None), "")

    def test_the_conversion_asks_fusion_which_fonts_it_has(self):
        class Fonts:
            def GetFontList(self):
                return {"Arial": {}, "Yu Gothic": {}, "Meiryo": {}}

        class Fusion:
            FontManager = Fonts()

        class Resolve:
            def Fusion(self):
                return Fusion()

        logs = []
        gen = se.TextPlusGenerator(Resolve())
        subs = [se.SubtitleData("こんにちは", 0, 24), se.SubtitleData("OK", 30, 50)]
        self.assertEqual(gen._default_font_for(subs, logs), "Yu Gothic")
        self.assertIn("Yu Gothic", logs[0])
        self.assertEqual(gen._default_font_for([se.SubtitleData("Hello", 0, 24)], []), "")


if __name__ == "__main__":
    unittest.main()
