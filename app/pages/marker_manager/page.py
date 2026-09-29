"""The Marker Manager's two existing tools, presented as sub-tabs."""

from PySide6.QtWidgets import QStackedWidget, QVBoxLayout

from pages.base import ToolPage
from pages.stills_exporter.page import StillsExporterPage
from pages.youtube_chapters.page import YouTubeChaptersPage


class MarkerManagerPage(ToolPage):
    tool_id = "marker_manager"
    display_name = "Marker Manager"
    category = "Export & Delivery"

    def build_ui(self):
        self.tabs = QStackedWidget(self)
        self.stills = StillsExporterPage(self.host)
        self.chapters = YouTubeChaptersPage(self.host)
        for page in (self.stills, self.chapters):
            page._marker_manager = self
            self.tabs.addWidget(page)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)

    def show_tab(self, tab):
        page = {"stills": self.stills, "chapters": self.chapters}.get(tab)
        if page is not None and page is not self.tabs.currentWidget():
            self.tabs.setCurrentWidget(page)
            if self.isVisible():
                page.on_shown()

    def on_shown(self):
        self.tabs.currentWidget().on_shown()

    def on_theme_changed(self):
        for page in (self.stills, self.chapters):
            page.on_theme_changed()

    def on_connection_changed(self, connected):
        for page in (self.stills, self.chapters):
            page.on_connection_changed(connected)

    def on_app_quitting(self):
        for page in (self.stills, self.chapters):
            page.on_app_quitting()
