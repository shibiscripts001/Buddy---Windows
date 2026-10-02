/* The shell's nav rail: the tools in the order Settings > Organize sidebar
   left them, with their headings and lines. Click one to show it. */
"use strict";

const {el, icon, send} = Buddy;

// A tool's mark beside its name (ToolPage.rail_badge): the Web tab playing
// sound, with a button to pause it - or paused, with one to play it again.
// The button comes first, so it stays put when the speaker comes and goes.
function badge(item) {
    if (item.badge !== "sound" && item.badge !== "paused") return [];
    const paused = item.badge === "paused";
    const label = paused ? "Play" : "Pause";
    // A span, not a button: it sits inside the tool's own button.
    const media = el("span.nav-media", {role: "button", tabindex: "0", title: label, "aria-label": label,
                                        onclick: e => { e.stopPropagation(); send("media", {id: item.id}); },
                                        onkeydown: e => {
                                            if (e.key === "Enter" || e.key === " ") {
                                                e.preventDefault(); e.stopPropagation(); send("media", {id: item.id});
                                            }
                                        }}, [icon(paused ? "play" : "pause")]);
    return paused ? [media]
        : [media, el("span.nav-badge", {title: "Playing sound", "aria-label": "Playing sound"}, [icon("volume")])];
}
const rail = document.getElementById("rail");

/* The width the longest entry needs - so a wider theme font never clips a
   name - plus room for a scrollbar, whose coming and going mustn't change it. */
function reportSize() {
    let widest = 0;
    for (const item of [...rail.children]) {
        const clone = item.cloneNode(true);
        clone.style.cssText = "position:absolute;visibility:hidden;width:max-content";
        rail.append(clone);
        widest = Math.max(widest, clone.getBoundingClientRect().width);
        clone.remove();
    }
    const style = getComputedStyle(rail);
    const chrome = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight)
                 + parseFloat(style.borderLeftWidth) + parseFloat(style.borderRightWidth);
    send("size", {width: Math.ceil(widest + chrome + 12)});
}

Buddy.on("rail", r => {
    rail.replaceChildren(...r.items.map(item => {
        if (item.type === "heading") return el("div.nav-heading", {text: item.label});
        if (item.type === "line") return el("div.nav-line", {role: "separator"});
        // Right-click: the tool's own menu (its Settings).
        return el("button.nav-item", {type: "button", "aria-current": item.active ? "page" : undefined,
                                      onclick: () => send("switch", {id: item.id}),
                                      oncontextmenu: e => { e.preventDefault(); send("menu", {id: item.id}); }},
                  [el("span", {text: item.label}), ...badge(item)]);
    }));
    reportSize();
});
Buddy.on("theme", () => requestAnimationFrame(reportSize));
document.fonts.ready.then(reportSize);
