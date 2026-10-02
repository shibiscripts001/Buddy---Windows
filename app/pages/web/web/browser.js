/* The Web tab's bar (pages/web/page.py): its tabs, back and forward, the
   address, downloads and a menu. Python keeps the tabs and pages; this
   draws them and says what was clicked. A tab's right-click menu and the
   ... menu are Python's own. */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);
const DRAG_START = 5;

let state = {tabs: [], downloads: {recent: []}};
let editing = false;            // the address is being typed in: leave it alone
let shownTab = null;            // whose address it shows
let toastTimer = 0;

$("new").append(icon("plus"));
$("back").append(icon("left"));
$("forward").append(icon("right"));
$("reload").append(icon("refresh"));
$("downloads").prepend(icon("download"));
$("more").append(icon("more"));

$("new").onclick = () => send("new_tab");
$("new").oncontextmenu = e => { e.preventDefault(); send("new_tab", {private: true}); };
$("back").onclick = () => send("back");
$("forward").onclick = () => send("forward");
$("reload").onclick = () => send(activeTab()?.loading ? "stop" : "reload");
$("downloads").onclick = () => send("downloads");
$("more").onclick = () => send("more");
$("zoom").onclick = () => send("zoom_reset");

$("address-form").onsubmit = e => {
    e.preventDefault();
    editing = false;
    send("go", {text: $("address").value});
};
$("address").addEventListener("focus", () => {
    editing = true;
    send("sites");
    requestAnimationFrame(() => $("address").select());
});

/* Finishing what's typed: the best site starting with it (Python ranks
   them - browser.py ranked_sites - and sends them on focus), the rest
   selected so typing on replaces it and Backspace takes it away. The same
   rule as browser.py autofill - change one, change both. */
let sites = [];
Buddy.on("sites", s => { sites = s.sites || []; });

function autofill(typed) {
    if (!typed || typed.includes(" ") || typed.split("://").pop().includes("/")) return null;
    let lower = typed.toLowerCase();
    for (const prefix of ["https://", "http://"]) {
        if (lower.startsWith(prefix)) lower = lower.slice(prefix.length);
    }
    if (!lower) return null;
    const www = lower.startsWith("www.");
    for (const site of sites) {
        const candidate = www ? "www." + site : site;
        if (candidate.startsWith(lower) && candidate !== lower) return typed + candidate.slice(lower.length);
    }
    return null;
}

$("address").addEventListener("input", e => {
    // Only while adding to the end: deleting, or typing in the middle, is left alone.
    const input = $("address");
    if (!(e.inputType || "").startsWith("insert") || input.selectionStart !== input.value.length) return;
    const typed = input.value;
    const full = autofill(typed);
    if (full) {
        input.value = full;
        input.setSelectionRange(typed.length, full.length);
    }
});
$("address").addEventListener("blur", () => { editing = false; $("address").value = state.url || ""; });
$("address").addEventListener("keydown", e => {
    if (e.key === "Escape") { $("address").value = state.url || ""; $("address").blur(); }
});

// The wheel scrolls the tabs sideways when there are more than fit.
$("tabs").addEventListener("wheel", e => {
    if (!e.deltaY) return;
    e.preventDefault();
    $("tabs").scrollLeft += e.deltaY;
}, {passive: false});

new ResizeObserver(() => send("size", {height: $("bar").getBoundingClientRect().height})).observe($("bar"));

const activeTab = () => state.tabs.find(t => t.id === state.active);

function tabNode(tab) {
    const marks = [];
    if ((tab.audible && !tab.muted) || tab.paused) {
        // Pause and play whatever the page is playing, without opening it -
        // before the speaker, so it stays put when the speaker goes.
        const label = tab.paused ? "Play" : "Pause";
        const media = el("button.wb-mark.wb-media", {type: "button", title: label, "aria-label": label});
        media.append(icon(tab.paused ? "play" : "pause"));
        media.onclick = e => { e.stopPropagation(); send("media", {id: tab.id}); };
        marks.push(media);
    }
    if (tab.audible || tab.muted) {
        const title = tab.muted ? "Unmute tab" : state.ducked ? "Lowered while another app plays sound - click to mute"
            : "Mute tab";
        const sound = el(`button.wb-mark.wb-sound${state.ducked && !tab.muted ? ".ducked" : ""}`,
                         {type: "button", title, "aria-label": tab.muted ? "Unmute tab" : "Mute tab"});
        sound.append(icon(tab.muted ? "mute" : "volume"));
        sound.onclick = e => { e.stopPropagation(); send("mute", {id: tab.id}); };
        marks.push(sound);
    }
    if (tab.asleep) marks.push(el("span.wb-mark", {title: "Asleep - it reloads when you open it"}, [icon("moon")]));
    else if (tab.awake || tab.never) marks.push(el("span.wb-mark", {title: tab.why || "Kept awake"}, [icon("eye")]));
    const close = el("button.wb-close", {type: "button", title: "Close tab (Ctrl+W)", "aria-label": "Close tab"});
    close.append(icon("close"));
    close.onclick = e => { e.stopPropagation(); send("close", {id: tab.id}); };
    const face = tab.loading ? el("span.wb-spin")
        : tab.icon ? el("img.wb-icon", {src: tab.icon, alt: "", draggable: "false"})
        : icon(tab.private ? "private" : "globe");
    if (tab.private && tab.icon && !tab.loading) marks.unshift(el("span.wb-mark.wb-private", {title: "Private tab"}, [icon("private")]));
    // translate="no": a page's title is the site's, not Buddy's.
    const node = el("div.wb-tab", {role: "tab", "aria-selected": String(tab.id === state.active),
                                   title: tab.why ? `${tab.title}\n${tab.why}` : tab.title,
                                   "data-asleep": String(!!tab.asleep), "data-private": String(!!tab.private)}, [
        face, el("span.wb-title", {text: tab.title, translate: "no"}), ...marks, close,
    ]);
    node.onclick = () => { if (!tabDrag.justDropped) send("select", {id: tab.id}); };
    node.addEventListener("pointerdown", e => startTabDrag(e, node, tab.id));
    // Middle-click closes. Its press is taken too, or Chromium starts
    // scrolling the tab row instead.
    node.onmousedown = e => { if (e.button === 1) e.preventDefault(); };
    node.onauxclick = e => { if (e.button === 1) { e.preventDefault(); send("close", {id: tab.id}); } };
    node.oncontextmenu = e => { e.preventDefault(); send("menu", {id: tab.id}); };
    return node;
}

/* Dragging a tab left or right moves it in the row: it follows the
   pointer, the others make room, and Python hears where it ended up
   ("move"). Redraws from Python wait until it's dropped. */
const tabDrag = {id: null, node: null, x: 0, moved: false, justDropped: false, pending: null};

function startTabDrag(e, node, id) {
    if (e.button !== 0 || e.target.closest("button")) return;      // its own buttons click as ever
    Object.assign(tabDrag, {id, node, x: e.clientX, moved: false});
    node.setPointerCapture(e.pointerId);
}

$("tabs").addEventListener("pointermove", e => {
    const d = tabDrag;
    if (d.id === null) return;
    if (!d.moved && Math.abs(e.clientX - d.x) < DRAG_START) return;
    if (!d.moved) { d.moved = true; d.node.classList.add("dragging"); }
    const others = [...$("tabs").children].filter(n => n !== d.node);
    const before = others.find(n => { const r = n.getBoundingClientRect(); return e.clientX < r.left + r.width / 2; });
    if (before ? d.node.nextSibling !== before : $("tabs").lastChild !== d.node) $("tabs").insertBefore(d.node, before || null);
    // Near either end of a row that scrolls: scroll it along.
    const row = $("tabs").getBoundingClientRect();
    if (e.clientX < row.left + 24) $("tabs").scrollLeft -= 12;
    else if (e.clientX > row.right - 24) $("tabs").scrollLeft += 12;
});

function endTabDrag() {
    const d = tabDrag;
    if (d.id === null) return;
    if (d.moved) {
        d.node.classList.remove("dragging");
        send("move", {id: d.id, index: [...$("tabs").children].indexOf(d.node)});
        d.justDropped = true;                          // the click that ends a drag isn't a select
        setTimeout(() => { d.justDropped = false; }, 0);
    }
    d.id = d.node = null;
    if (d.pending) { const s = d.pending; d.pending = null; drawBrowser(s); }
}
$("tabs").addEventListener("pointerup", endTabDrag);
$("tabs").addEventListener("pointercancel", endTabDrag);

Buddy.on("browser", s => {
    if (tabDrag.moved && tabDrag.id !== null) { tabDrag.pending = s; return; }
    drawBrowser(s);
});

function drawBrowser(s) {
    state = s;
    $("tabs").replaceChildren(...s.tabs.map(tabNode));
    const tab = activeTab();
    $("bar").classList.toggle("private", !!(tab && tab.private));
    $("address").placeholder = tab && tab.private ? "Search or type an address - private" : "Search or type an address";
    // Left alone while it's typed in - unless that was another tab's.
    if (!editing || s.active !== shownTab) $("address").value = s.url || "";
    shownTab = s.active;
    $("back").disabled = !s.back;
    $("forward").disabled = !s.forward;
    const loading = !!(tab && tab.loading);
    $("reload").replaceChildren(icon(loading ? "close" : "refresh"));
    $("reload").title = loading ? "Stop" : "Reload (F5)";
    $("progress").style.width = loading ? `${Math.max(8, tab.progress)}%` : "0";
    $("progress").classList.toggle("on", loading);
    $("zoom").hidden = s.zoom === 100;
    $("zoom").textContent = `${s.zoom}%`;
    $("memory").hidden = s.memory === null || s.memory === undefined;
    $("memory").textContent = `${s.memory} MB`;
    const d = s.downloads;
    $("badge").hidden = !d.busy;
    $("badge").textContent = String(d.busy);
    $("downloads").title = d.busy ? (d.progress === null ? "Downloading…" : `Downloading… ${Math.round(d.progress * 100)}%`)
        : "Downloads";
    const selected = $("tabs").querySelector('[aria-selected="true"]');
    if (selected) selected.scrollIntoView({block: "nearest", inline: "nearest"});
}

Buddy.on("focus_address", () => {
    send("sites");                      // what it may finish with, fresh
    $("address").focus();
    $("address").select();
});

/* A finished download: its name for a few seconds - drag it straight
   into the Media Pool, or click to see the folder. */
let press = null, dragging = false;
Buddy.on("toast", t => {
    clearTimeout(toastTimer);
    $("toast").hidden = false;
    $("toast").replaceChildren(icon("check"), el("span", {text: t.text, translate: "no"}));
    $("toast").dataset.index = t.download === null || t.download === undefined ? "" : String(t.download);
    $("toast").title = "Drag into Resolve's Media Pool, or click to see your downloads";
    toastTimer = setTimeout(() => { if (!dragging) $("toast").hidden = true; }, 8000);
});
$("toast").addEventListener("pointerdown", e => { if (e.button === 0) press = {x: e.clientX, y: e.clientY}; });
$("toast").addEventListener("pointermove", e => {
    if (!press || dragging || !(e.buttons & 1) || $("toast").dataset.index === "") return;
    if (Math.hypot(e.clientX - press.x, e.clientY - press.y) < DRAG_START) return;
    dragging = true;
    press = null;
    send("drag_download", {index: Number($("toast").dataset.index)});
});
$("toast").addEventListener("pointerup", () => {
    if (press && !dragging) send("downloads");
    press = null;
});
document.addEventListener("dragstart", e => e.preventDefault());
Buddy.on("drag_done", () => { dragging = false; press = null; });
