"""Opening the manual PDF at a cited page (pages/manual_chat/manual_pdf.py)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401
from pages.manual_chat import manual_pdf


class ManualPdfTests(unittest.TestCase):
    def test_the_pdf_is_the_one_the_bundle_was_built_from(self):
        with tempfile.TemporaryDirectory() as folder:
            pdf = Path(folder) / "Manual.pdf"
            pdf.write_bytes(b"%PDF-1.7")
            other = Path(folder) / "Other.pdf"
            other.write_bytes(b"%PDF-1.7")
            (Path(folder) / "meta.json").write_text(json.dumps({"source": {"path": str(pdf)}}))
            self.assertEqual(manual_pdf.manual_pdf(folder, str(other)), pdf)

    def test_an_older_bundle_falls_back_to_the_remembered_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            pdf = Path(folder) / "Manual.pdf"
            pdf.write_bytes(b"%PDF-1.7")
            (Path(folder) / "meta.json").write_text(json.dumps({"source": {"file": "Manual.pdf"}}))
            self.assertEqual(manual_pdf.manual_pdf(folder, str(pdf)), pdf)
            self.assertIsNone(manual_pdf.manual_pdf(folder, str(Path(folder) / "gone.pdf")))
            self.assertIsNone(manual_pdf.manual_pdf("", ""))

    def test_each_app_is_given_the_page_its_own_way(self):
        pdf = Path("C:/Manuals/Resolve Manual.pdf")
        chrome = manual_pdf.command_for(r"C:\Program Files\Google\Chrome\Application\chrome.exe", pdf, 1195)
        self.assertTrue(chrome[1].startswith("file:///") and chrome[1].endswith("Manual.pdf#page=1195"))
        self.assertIn("%20", chrome[1])
        self.assertEqual(manual_pdf.command_for(r"C:\Adobe\Acrobat.exe", pdf, 7)[1:3], ["/A", "page=7"])
        self.assertEqual(manual_pdf.command_for(r"C:\SumatraPDF.exe", pdf, 7)[1:3], ["-page", "7"])
        self.assertIsNone(manual_pdf.command_for(r"C:\Apps\SomeViewer.exe", pdf, 7))

    def test_a_mac_goes_to_the_page_in_a_browser_else_preview(self):
        pdf = Path("/Users/me/Manual.pdf")
        chrome = manual_pdf._MAC_BROWSERS[0]
        with mock.patch.object(manual_pdf, "IS_WINDOWS", False),                 mock.patch.object(manual_pdf.sys, "platform", "darwin"),                 mock.patch.object(manual_pdf.subprocess, "Popen") as popen:
            with mock.patch.object(manual_pdf.os.path, "isfile", lambda p: p == chrome):
                self.assertTrue(manual_pdf.open_at_page(pdf, 12))
            self.assertEqual(popen.call_args[0][0][0], chrome)
            self.assertTrue(popen.call_args[0][0][1].endswith("Manual.pdf#page=12"))
            with mock.patch.object(manual_pdf.os.path, "isfile", lambda p: False):
                self.assertFalse(manual_pdf.open_at_page(pdf, 12))
            self.assertEqual(popen.call_args[0][0], ["open", str(pdf)])


if __name__ == "__main__":
    unittest.main()
