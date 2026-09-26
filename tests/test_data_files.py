"""Time Tracker / Color Palette / Asset Manager data files: saved atomically,
and a damaged file is recovered from its .bak or set aside with a warning -
never read as empty and then saved over."""

import json
import os
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.asset_manager import data_manager as am
from pages.color_palette.data_manager import DataManager as PaletteData
from pages.time_tracker.data_manager import DataManager as TrackerData


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class _TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


class TimeTrackerFilesTests(_TempDir):
    def test_round_trip_keeps_format(self):
        dm = TrackerData(self.dir)
        self.assertEqual(dm.load_warnings, [])
        entry = dm.add_manual_entry("Proj", "2026-01-01T10:00:00", "2026-01-01T11:00:00")
        dm.set_project_rate("Proj", 50.0, "EUR")
        dm.settings["weekly_goal_hours"] = 10.0
        dm.save_settings()

        self.assertEqual(_read(dm.entries_path), json.dumps(dm.entries, indent=2))
        again = TrackerData(self.dir)
        self.assertEqual(again.entries, [entry])
        self.assertEqual(again.project_rates, {"Proj": {"rate": 50.0, "currency": "EUR"}})
        self.assertEqual(again.settings["weekly_goal_hours"], 10.0)

    def test_truncated_entries_recovered_from_backup(self):
        dm = TrackerData(self.dir)
        first = dm.add_manual_entry("A", "2026-01-01T10:00:00", "2026-01-01T11:00:00")
        dm.add_manual_entry("B", "2026-01-02T10:00:00", "2026-01-02T11:00:00")
        _write(dm.entries_path, '[{"id": "x", "proj')
        again = TrackerData(self.dir)
        self.assertEqual(again.entries, [first])

    def test_damaged_entries_set_aside_not_wiped(self):
        path = os.path.join(self.dir, "entries.json")
        _write(path, '[{"id": "x", "proj')
        dm = TrackerData(self.dir)
        self.assertEqual(dm.entries, [])
        self.assertEqual(len(dm.load_warnings), 1)
        self.assertIn("entries.json.corrupt-1", dm.load_warnings[0])

        dm.add_manual_entry("New", "2026-01-01T10:00:00", "2026-01-01T11:00:00")
        self.assertEqual(_read(path + ".corrupt-1"), '[{"id": "x", "proj')

    def test_wrong_type_settings_set_aside(self):
        _write(os.path.join(self.dir, "settings.json"), "[1, 2]")
        dm = TrackerData(self.dir)
        self.assertIn("poll_interval_seconds", dm.settings)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "settings.json.corrupt-1")))
        self.assertEqual(len(dm.load_warnings), 1)

    def test_one_bad_rate_skips_only_that_project(self):
        _write(os.path.join(self.dir, "project_rates.json"),
               json.dumps({"Good": {"rate": 20, "currency": "USD"}, "Bad": "lots", "Old": 5}))
        dm = TrackerData(self.dir)
        self.assertEqual(dm.project_rates, {
            "Good": {"rate": 20.0, "currency": "USD"},
            "Old": {"rate": 5.0, "currency": "USD"},
        })


class ColorPaletteFilesTests(_TempDir):
    def test_round_trip_keeps_format(self):
        dm = PaletteData(self.dir)
        dm.palettes = {"Mine": ["#112233"]}
        dm.save_palette_data()
        dm.move_palette_to_folder("Mine", "Work")
        dm.set_palette_custom_tags("Mine", "warm, dark")
        dm.add_palette_version_snapshot("Mine", "v1")
        dm.set_visualizer_slot_override("Mine", 0, "#FFFFFF")
        dm.settings["language"] = "Deutsch"
        dm.save_settings()

        self.assertEqual(_read(dm.palette_data_path), json.dumps({"Mine": ["#112233"]}, indent=2))
        again = PaletteData(self.dir)
        self.assertEqual(again.load_warnings, [])
        self.assertEqual(again.palettes, {"Mine": ["#112233"]})
        self.assertEqual(again.palette_folders, {"Work": ["Mine"]})
        self.assertEqual(again.palette_custom_tags, {"Mine": ["warm", "dark"]})
        self.assertEqual(again.palette_versions["Mine"][0]["label"], "v1")
        self.assertEqual(again.get_visualizer_slot_override("Mine", 0), "#FFFFFF")
        self.assertEqual(again.settings["language"], "Deutsch")

    def test_truncated_palettes_recovered_from_backup(self):
        dm = PaletteData(self.dir)
        dm.palettes = {"Mine": ["#112233"]}
        dm.save_palette_data()
        dm.palettes["Other"] = ["#000000"]
        dm.save_palette_data()
        _write(dm.palette_data_path, '{"Mine": ["#11')
        self.assertEqual(PaletteData(self.dir).palettes, {"Mine": ["#112233"]})

    def test_damaged_palettes_set_aside_not_wiped(self):
        path = os.path.join(self.dir, "palettes.json")
        _write(path, '{"Mine": ["#11')
        dm = PaletteData(self.dir)
        self.assertIn("Cinematic Look", dm.palettes)  # the demo palettes
        self.assertEqual(len(dm.load_warnings), 1)
        self.assertIn("palettes.json.corrupt-1", dm.load_warnings[0])

        dm.save_palette_data()
        self.assertEqual(_read(path + ".corrupt-1"), '{"Mine": ["#11')

    def test_old_settings_migration_still_applies(self):
        _write(os.path.join(self.dir, "settings.json"), json.dumps({"theme_preset": "Material Default"}))
        self.assertEqual(PaletteData(self.dir).settings["theme_preset"], "Default")


class AssetManagerFilesTests(_TempDir):
    def setUp(self):
        super().setUp()
        patches = {
            "DATA_DIR": self.dir,
            "ASSET_STORE_PATH": os.path.join(self.dir, "assets.json"),
            "PROJECT_STORE_PATH": os.path.join(self.dir, "projects.json"),
        }
        for name, value in patches.items():
            p = mock.patch.object(am, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.assets_path = am.ASSET_STORE_PATH
        self.projects_path = am.PROJECT_STORE_PATH

    def test_round_trip_keeps_format(self):
        lib = am.AssetLibrary()
        asset_id = lib.add(os.path.join(self.dir, "clip.mov"))
        lib.save()
        projects = am.ProjectLibrary()
        project_id = projects.create("Show")
        projects.add_asset(project_id, asset_id)
        projects.save()

        self.assertEqual(_read(self.assets_path), json.dumps(list(lib.assets.values()), indent=2))
        self.assertEqual(am.AssetLibrary().assets, lib.assets)
        self.assertEqual(am.ProjectLibrary().projects[project_id]["asset_ids"], [asset_id])

    def test_truncated_library_recovered_from_backup(self):
        lib = am.AssetLibrary()
        lib.add(os.path.join(self.dir, "a.png"))
        lib.save()
        saved = dict(lib.assets)
        lib.add(os.path.join(self.dir, "b.png"))
        lib.save()
        _write(self.assets_path, '[{"id": "')
        self.assertEqual(am.AssetLibrary().assets, saved)

    def test_damaged_library_set_aside_not_wiped(self):
        _write(self.assets_path, '[{"id": "')
        lib = am.AssetLibrary()
        self.assertEqual(lib.assets, {})
        self.assertEqual(len(lib.load_warnings), 1)
        self.assertIn("assets.json.corrupt-1", lib.load_warnings[0])

        lib.add(os.path.join(self.dir, "new.png"))
        lib.save()
        self.assertEqual(_read(self.assets_path + ".corrupt-1"), '[{"id": "')

    def test_entry_without_id_skipped_alone(self):
        good = {"id": "a1", "path": "C:/x/a.png", "name": "a.png", "ext": ".png",
                "category": "Image", "date_added": "2026-01-01 10:00"}
        _write(self.assets_path, json.dumps([good, {"path": "C:/x/b.png"}]))
        lib = am.AssetLibrary()
        self.assertEqual(lib.assets, {"a1": good})
        self.assertEqual(len(lib.load_warnings), 1)

        _write(self.projects_path, json.dumps([{"name": "No id"}, {"id": "p1", "name": "P", "asset_ids": ["a1"]}]))
        projects = am.ProjectLibrary()
        self.assertEqual(list(projects.projects), ["p1"])


if __name__ == "__main__":
    unittest.main()
