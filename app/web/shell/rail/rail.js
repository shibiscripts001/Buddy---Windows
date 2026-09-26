/* The shell's nav rail: the tools in the order Settings > Organize sidebar
   left them, with their headings and lines. Click one to show it. */
"use strict";

const {el, send} = Buddy;
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
        return el("button.nav-item", {type: "button", text: item.label, "aria-current": item.active ? "page" : undefined,
                                      onclick: () => send("switch", {id: item.id})});
    }));
    reportSize();
});
Buddy.on("theme", () => requestAnimationFrame(reportSize));
document.fonts.ready.then(reportSize);
