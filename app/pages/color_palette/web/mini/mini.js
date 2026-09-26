/* The mini palette window's view: one palette, 12 colours a page. Click one
   to copy it. Python (mini_palette_window.py) keeps everything. */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);
let STRINGS = {pin: "Pin on top", unpin: "Unpin", copied: "Copied {hex}"};
let pinned = false;

$("prev").append(icon("left"));
$("next").append(icon("right"));
$("pin").append(icon("pin"));
$("prev").onclick = () => send("page", {step: -1});
$("next").onclick = () => send("page", {step: 1});
$("pin").onclick = () => send("pin");

function pinTitle() { $("pin").title = pinned ? STRINGS.unpin : STRINGS.pin; }

Buddy.on("strings", s => { STRINGS = s; pinTitle(); });

Buddy.on("mini", m => {
    $("name").textContent = m.name;
    pinned = m.pinned;
    $("pin").setAttribute("aria-pressed", String(pinned));
    pinTitle();
    $("prev").disabled = m.page <= 0;
    $("next").disabled = m.page >= m.pages - 1;
    $("grid").replaceChildren(...m.swatches.map(sw => el("button.msw", {
        type: "button", title: sw.hex, style: `--sw:${sw.shown};--ink:${sw.ink}`,
        onclick: e => { send("copy", {index: sw.index}); tip(sw, e); },
    })));
});

let tipTimer = 0;
function tip(sw, e) {
    let node = $("copy-tip");
    if (!node) { node = el("div.copy-tip#copy-tip"); document.body.append(node); }
    node.textContent = STRINGS.copied.replace("{hex}", sw.hex);
    node.style.cssText = `--sw:${sw.shown};--ink:${sw.ink}`;
    const w = node.offsetWidth || 110;
    node.style.left = `${Math.max(4, Math.min(e.clientX + 10, innerWidth - w - 4))}px`;
    node.style.top = `${Math.max(4, Math.min(e.clientY + 14, innerHeight - 28))}px`;
    node.classList.add("show");
    clearTimeout(tipTimer);
    tipTimer = setTimeout(() => node.classList.remove("show"), 1100);
}
