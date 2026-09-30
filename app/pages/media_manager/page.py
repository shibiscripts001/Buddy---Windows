"""The Media Manager's two existing tools, presented as sub-tabs.

Batch Clip Renamer and Media Relink keep their own pages, settings and tool
ids; this page stacks them and each one's view draws the same tab row
(web/tabs.js), whose click comes back here through its on_switch_tab. The
shell opens a tab by its tool's id too (switch_tool("media_relink") - Ask
Buddy's "Open Media Relink"), through SUB_TOOLS and show_tool().
"""

from PySide6.QtWidgets import QStackedWidget, QVBoxLayout

from pages.base import ToolPage
from pages.batch_clip_renamer.page import BatchClipRenamerPage
from pages.media_relink.page import MediaRelinkPage


class MediaManagerPage(ToolPage):
    tool_id = "media_manager"
    display_name = "Media Manager"
    category = "Media & Assets"
    # tab -> the tool on it, in tab order
    TABS = (("renamer", BatchClipRenamerPage), ("relink", MediaRelinkPage))
    SUB_TOOLS = tuple(page_cls.tool_id for _tab, page_cls in TABS)

    def build_ui(self):
        self.tabs = QStackedWidget(self)
        self.by_tab = {}
        for tab, page_cls in self.TABS:
            page = page_cls(self.host)
            page._media_manager = self
            self.by_tab[tab] = page
            self.tabs.addWidget(page)
        self.renamer, self.relink = self.by_tab["renamer"], self.by_tab["relink"]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)

    def show_tab(self, tab):
        page = self.by_tab.get(tab)
        if page is not None and page is not self.tabs.currentWidget():
            self.tabs.setCurrentWidget(page)
            if self.isVisible():
                page.on_shown()

    def show_tool(self, tool_id):
        """The tab holding the tool with this id."""
        for tab, page in self.by_tab.items():
            if page.tool_id == tool_id:
                self.show_tab(tab)

    def on_shown(self):
        self.tabs.currentWidget().on_shown()

    def on_theme_changed(self):
        for page in self.by_tab.values():
            page.on_theme_changed()

    def on_connection_changed(self, connected):
        for page in self.by_tab.values():
            page.on_connection_changed(connected)

    def on_app_quitting(self):
        for page in self.by_tab.values():
            page.on_app_quitting()
