"use strict";

const markerTabs = document.getElementById("marker-tabs");
markerTabs.addEventListener("click", event => {
    const tab = event.target.closest("button[data-tab]");
    if (tab && tab.getAttribute("aria-selected") !== "true") {
        Buddy.send("switch_tab", {tab: tab.dataset.tab});
    }
});
