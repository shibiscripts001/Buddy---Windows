"use strict";

const mediaTabs = document.getElementById("media-tabs");
mediaTabs.addEventListener("click", event => {
    const tab = event.target.closest("button[data-tab]");
    if (tab && tab.getAttribute("aria-selected") !== "true") {
        Buddy.send("switch_tab", {tab: tab.dataset.tab});
    }
});
