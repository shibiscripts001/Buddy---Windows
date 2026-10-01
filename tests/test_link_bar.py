"""The link bar's links (app/core/link_bar.py): what counts as an address,
the names links get, what survives the settings file, moving them, and
where the bar goes. No Qt."""

import unittest

import _paths  # noqa: F401
from core import link_bar as lb
from core import settings_form as sf


class AddressTests(unittest.TestCase):
    def test_a_bare_site_gets_https(self):
        self.assertEqual(lb.normalize_address("youtube.com"), "https://youtube.com")
        self.assertEqual(lb.normalize_address("  www.blackmagicdesign.com/support "),
                         "https://www.blackmagicdesign.com/support")
        self.assertEqual(lb.normalize_address("localhost:3000/app"), "https://localhost:3000/app")

    def test_web_addresses_are_kept_as_typed(self):
        self.assertEqual(lb.normalize_address("http://192.168.1.20:8080"), "http://192.168.1.20:8080")
        self.assertEqual(lb.normalize_address("https://example.com/a?b=c#d"), "https://example.com/a?b=c#d")

    def test_paths_are_kept_as_paths(self):
        self.assertEqual(lb.normalize_address(r"D:\Projects\Client"), r"D:\Projects\Client")
        self.assertEqual(lb.normalize_address(r'"D:\Footage"'), r"D:\Footage")
        self.assertEqual(lb.normalize_address(r"\\nas\media"), r"\\nas\media")
        self.assertEqual(lb.normalize_address("file:///D:/My%20Projects"), r"D:\My Projects")

    def test_other_schemes_and_junk_are_refused(self):
        for text in ("", "   ", None, 42, "javascript:alert(1)", "mailto:a@b.com", "ms-settings:display",
                     "ftp://example.com", "https://", "two words.com", "line\nbreak.com", "x" * 3000):
            self.assertIsNone(lb.normalize_address(text), text)


class NameTests(unittest.TestCase):
    def test_an_unnamed_link_is_named_for_its_site_or_folder(self):
        self.assertEqual(lb.default_name("https://www.youtube.com/watch?v=1"), "youtube.com")
        self.assertEqual(lb.default_name("https://Docs.Example.com"), "docs.example.com")
        self.assertEqual(lb.default_name("D:\\Projects\\Client A\\"), "Client A")
        self.assertEqual(lb.default_name("D:\\"), "D:")

    def test_names_are_the_users_own_tidied(self):
        link, why = lb.make_link("  My   Drive  ", "drive.google.com")
        self.assertIsNone(why)
        self.assertEqual(link, {"name": "My Drive", "url": "https://drive.google.com"})
        self.assertEqual(lb.make_link("", "drive.google.com")[0]["name"], "drive.google.com")
        self.assertEqual(len(lb.make_link("n" * 200, "a.com")[0]["name"]), lb.MAX_NAME)

    def test_a_bad_address_says_why(self):
        self.assertIsNone(lb.make_link("x", "")[0])
        self.assertIn("Type the address", lb.make_link("x", "  ")[1])
        self.assertIn("isn't a web address", lb.make_link("x", "mailto:a@b.com")[1])


class StoredTests(unittest.TestCase):
    def test_what_isnt_a_link_is_dropped(self):
        raw = [{"name": "YT", "url": "https://youtube.com"}, "junk", {"name": "bad", "url": "javascript:x"},
               {"url": "D:\\Footage"}, None, {"name": 5, "url": "example.com"}]
        self.assertEqual(lb.load_links(raw), [
            {"name": "YT", "url": "https://youtube.com"},
            {"name": "Footage", "url": "D:\\Footage"},
            {"name": "5", "url": "https://example.com"},
        ])
        self.assertEqual(lb.load_links(None), [])
        self.assertEqual(lb.load_links({"name": "x"}), [])

    def test_never_more_than_the_most(self):
        raw = [{"name": str(i), "url": f"site{i}.com"} for i in range(lb.MAX_LINKS + 10)]
        self.assertEqual(len(lb.load_links(raw)), lb.MAX_LINKS)

    def test_the_view_gets_each_links_kind(self):
        items = lb.view_items([{"name": "a", "url": "https://a.com"}, {"name": "b", "url": "D:\\b"}])
        self.assertEqual([i["kind"] for i in items], ["web", "path"])


class MoveTests(unittest.TestCase):
    def test_moving_a_link(self):
        links = ["a", "b", "c", "d"]
        self.assertTrue(lb.move(links, 0, 2))
        self.assertEqual(links, ["b", "c", "a", "d"])
        self.assertTrue(lb.move(links, 3, 0))
        self.assertEqual(links, ["d", "b", "c", "a"])

    def test_out_of_range_or_nowhere_moves_nothing(self):
        links = ["a", "b"]
        for index, to in ((0, 0), (-1, 1), (0, 2), (5, 0)):
            self.assertFalse(lb.move(links, index, to))
        self.assertEqual(links, ["a", "b"])


class PlacementTests(unittest.TestCase):
    def test_the_bottom_except_on_the_desktop(self):
        self.assertEqual(lb.placement("panes"), "bottom")
        self.assertEqual(lb.placement("desktop"), "top")


class SettingsTests(unittest.TestCase):
    def test_the_window_page_turns_it_on(self):
        window = next(p for p in sf.shell_pages({}, True) if p["id"] == "window")
        field = next(f for f in window["fields"] if f.get("key") == lb.SHOW_KEY)
        self.assertEqual(field["kind"], "check")
        self.assertFalse(field["value"])
        self.assertTrue(next(f for f in sf.shell_pages({lb.SHOW_KEY: True}, True)[1]["fields"]
                             if f.get("key") == lb.SHOW_KEY)["value"])
        self.assertEqual(sf.apply_shell({}, lb.SHOW_KEY, True), "linkbar")


if __name__ == "__main__":
    unittest.main()
