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

/* Dragging a tab left or right, as in Chrome: it follows the pointer along
   the row, the others slide aside to make room, and on release it settles
   into its place and Python hears where that is ("move"). The row's own
   order isn't touched until the drop, so every tab's layout position
   (offsetLeft) stays what it was and the slots are read from that; only
   transforms move. Redraws from Python wait until it's dropped. */
const tabDrag = {id: null, node: null, x: 0, grab: 0, moved: false, justDropped: false, pending: null,
                 pointer: 0, from: 0, to: 0, others: [], gap: 0, timer: 0};
const SLIDE = "transform .14s ease";

function startTabDrag(e, node, id) {
    if (e.button !== 0 || e.target.closest("button")) return;      // its own buttons click as ever
    Object.assign(tabDrag, {id, node, x: e.clientX, moved: false});
    node.setPointerCapture(e.pointerId);
}

function dragLayout() {
    const d = tabDrag, row = $("tabs"), r = row.getBoundingClientRect();
    // Where the tab's left edge wants to be, in the row's scrolled content.
    const want = d.pointer - r.left + row.scrollLeft - d.grab;
    const left = Math.max(0, Math.min(want, row.scrollWidth - d.node.offsetWidth));
    d.node.style.transform = `translateX(${left - d.node.offsetLeft}px)`;
    const centre = left + d.node.offsetWidth / 2;
    d.to = d.others.filter(n => n.offsetLeft + n.offsetWidth / 2 < centre).length;
    const shift = d.node.offsetWidth + d.gap;
    d.others.forEach((n, i) => {
        // Past the dragged tab's old place and not yet past its new one: out of its way.
        const x = i >= d.from && i < d.to ? -shift : i >= d.to && i < d.from ? shift : 0;
        n.style.transform = x ? `translateX(${x}px)` : "";
    });
}

$("tabs").addEventListener("pointermove", e => {
    const d = tabDrag;
    if (d.id === null) return;
    d.pointer = e.clientX;
    if (!d.moved) {
        if (Math.abs(e.clientX - d.x) < DRAG_START) return;
        d.moved = true;
        const kids = [...$("tabs").children];
        d.others = kids.filter(n => n !== d.node);
        d.from = d.to = kids.indexOf(d.node);
        d.grab = d.x - d.node.getBoundingClientRect().left;
        d.gap = parseFloat(getComputedStyle($("tabs")).columnGap) || 0;
        d.node.classList.add("dragging");
        d.others.forEach(n => { n.style.transition = SLIDE; });
        // Near either end of a row that scrolls, it scrolls along - even
        // while the pointer holds still.
        d.timer = setInterval(() => {
            const row = $("tabs").getBoundingClientRect();
            if (d.pointer < row.left + 28) $("tabs").scrollLeft -= 10;
            else if (d.pointer > row.right - 28) $("tabs").scrollLeft += 10;
            else return;
            dragLayout();
        }, 16);
    }
    dragLayout();
});

function endTabDrag() {
    const d = tabDrag;
    if (d.id === null) return;
    clearInterval(d.timer);
    if (d.moved) {
        const row = $("tabs"), id = d.id, node = d.node, index = d.to;
        const was = node.getBoundingClientRect().left;
        // The row in its new order; the tab then eases from where it was let go.
        d.others.forEach(n => { n.style.transition = ""; n.style.transform = ""; });
        node.style.transform = "";
        row.insertBefore(node, d.others[index] || null);
        node.classList.remove("dragging");
        node.style.transition = "none";
        node.style.transform = `translateX(${was - node.getBoundingClientRect().left}px)`;
        node.getBoundingClientRect();
        node.style.transition = SLIDE;
        node.style.transform = "";
        setTimeout(() => { node.style.transition = ""; }, 200);
        send("move", {id, index});
        d.justDropped = true;                          // the click that ends a drag isn't a select
        setTimeout(() => { d.justDropped = false; }, 0);
    }
    d.id = d.node = null;
    d.others = [];
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
   into Resolve (the Downloads bin), or click to see the Downloads window. */
let press = null, dragging = false;
Buddy.on("toast", t => {
    clearTimeout(toastTimer);
    $("toast").hidden = false;
    $("toast").replaceChildren(icon("check"), el("span", {text: t.text, translate: "no"}));
    $("toast").dataset.id = t.id || "";
    $("toast").title = t.id ? "Drag into Resolve (it goes to the Downloads bin), or click to see your downloads"
        : "Click to see your downloads";
    toastTimer = setTimeout(() => { if (!dragging) $("toast").hidden = true; }, 8000);
});
$("toast").addEventListener("pointerdown", e => { if (e.button === 0) press = {x: e.clientX, y: e.clientY}; });
$("toast").addEventListener("pointermove", e => {
    if (!press || dragging || !(e.buttons & 1) || !$("toast").dataset.id) return;
    if (Math.hypot(e.clientX - press.x, e.clientY - press.y) < DRAG_START) return;
    dragging = true;
    press = null;
    send("drag_download", {id: $("toast").dataset.id});
});
$("toast").addEventListener("pointerup", () => {
    if (press && !dragging) send("downloads");
    press = null;
});
document.addEventListener("dragstart", e => e.preventDefault());
Buddy.on("drag_done", () => { dragging = false; press = null; });
