/* The Web tab's Downloads window (pages/web/downloads_window.py). Python
   keeps the list (newest first); this filters, sorts and draws it. Click to
   select (Ctrl and Shift for more), double-click to open, drag the
   selection out into Resolve - the drag itself is started in Python, as
   real files. */
"use strict";

const {el, icon, send} = Buddy;
const $ = id => document.getElementById(id);
const DRAG_START = 5;
const MB = 1024 * 1024, GB = 1024 * MB;
const DAY = 86400;

const KINDS = [["all", "All"], ["video", "Video"], ["audio", "Audio"], ["image", "Images"],
               ["document", "Documents"], ["archive", "Archives"], ["other", "Other"]];
const KIND_ICON = {video: "film", audio: "music", image: "image", document: "file", archive: "folder", other: "file"};
// [min, max) in bytes
const SIZES = {any: [0, Infinity], tiny: [0, MB], small: [MB, 10 * MB], medium: [10 * MB, 100 * MB],
               large: [100 * MB, GB], huge: [GB, Infinity]};
const SORTS = {
    new: (a, b) => b.time - a.time,
    old: (a, b) => a.time - b.time,
    big: (a, b) => b.size - a.size || b.time - a.time,
    small: (a, b) => a.size - b.size || b.time - a.time,
    name: (a, b) => a.name.localeCompare(b.name, undefined, {numeric: true, sensitivity: "base"}),
};

let all = [];                   // every download, newest first, as Python sent them
let shown = [];                 // what the filters let through, in the order drawn
let selected = new Set();       // their ids
let anchor = -1;
let press = null;               // {x, y, id, plain} while the button's down on a row
let dragging = false;
let drawn = "";                 // what the list was last drawn from
let pending = null;             // a list that arrived mid-drag

const filters = {kind: "all", size: "any", when: "any", sort: "new", search: ""};

$("folder").append(icon("folder"));

for (const key of ["size", "when", "sort"]) {
    $(key).onchange = () => { filters[key] = $(key).value; draw(true); };
}
$("search").oninput = () => { filters.search = $("search").value.trim().toLowerCase(); draw(true); };

// No browser drags of our own: Python carries the files.
document.addEventListener("dragstart", e => e.preventDefault());

function matches(d) {
    const [lo, hi] = SIZES[filters.size];
    if (filters.kind !== "all" && d.kind !== filters.kind) return false;
    if (filters.size !== "any" && (d.state === "active" || d.size < lo || d.size >= hi)) return false;
    if (filters.when !== "any") {
        const age = Date.now() / 1000 - d.time;
        const start = new Date(); start.setHours(0, 0, 0, 0);
        if (filters.when === "today" ? d.time < start.getTime() / 1000
            : age > (filters.when === "week" ? 7 : 30) * DAY) return false;
    }
    if (filters.search) {
        const hay = `${d.name} ${d.site}`.toLowerCase();
        if (!filters.search.split(/\s+/).every(w => hay.includes(w))) return false;
    }
    return true;
}

const dayLabel = seconds => {
    const date = new Date(seconds * 1000), today = new Date();
    today.setHours(0, 0, 0, 0);
    const days = Math.round((today - new Date(date).setHours(0, 0, 0, 0)) / (DAY * 1000));
    if (days === 0) return "Today";
    if (days === 1) return "Yesterday";
    return date.toLocaleDateString(undefined, {weekday: "long", month: "short", day: "numeric",
                                               year: date.getFullYear() === today.getFullYear() ? undefined : "numeric"});
};
const clock = seconds => new Date(seconds * 1000).toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit"});
const bytes = n => n < 1024 ? `${n} B` : n < MB ? `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`
    : n < GB ? `${(n / MB).toFixed(n < 10 * MB ? 1 : 0)} MB` : `${(n / GB).toFixed(1)} GB`;

function kindButtons() {
    const counts = {all: all.length};
    for (const d of all) counts[d.kind] = (counts[d.kind] || 0) + 1;
    const buttons = KINDS.filter(([k]) => k === "all" || counts[k] || k === filters.kind).map(([k, label]) => {
        const b = el("button", {type: "button", "aria-pressed": String(filters.kind === k)}, [
            label, el("span.dl-n", {text: ` ${counts[k] || 0}`, translate: "no"})]);
        b.onclick = () => { filters.kind = k; draw(true); };
        return b;
    });
    $("kinds").replaceChildren(...buttons);
}

function draw(force) {
    shown = all.filter(matches).sort(SORTS[filters.sort]);
    const key = JSON.stringify([shown, filters.sort, [...selected]]) + all.length;
    if (!force && key === drawn) return;
    drawn = key;
    const ids = new Set(shown.map(d => d.id));
    selected = new Set([...selected].filter(id => ids.has(id)));
    kindButtons();
    const nodes = [];
    let day = "";
    shown.forEach((d, index) => {
        if (filters.sort === "new" || filters.sort === "old") {
            const label = dayLabel(d.time);
            if (label !== day) { day = label; nodes.push(el("div.dl-day", {text: label})); }
        }
        nodes.push(row(d, index));
    });
    if (!shown.length) {
        nodes.push(el("div.dl-empty", {text: !all.length ? "Nothing downloaded yet." : "Nothing matches."}));
    }
    $("list").replaceChildren(...nodes);
    mark();
}

function row(d, index) {
    const meta = [];
    if (d.state === "active") {
        meta.push(el("span", {text: d.progress === null ? (d.size ? bytes(d.size) : "Starting…")
            : `${Math.round(d.progress * 100)}% of ${bytes(d.size)}`}));
    } else {
        meta.push(el("span", {text: clock(d.time)}));
        if (d.site) meta.push(el("span.dl-site", {text: d.site, translate: "no"}));
        if (d.state === "failed") meta.push(el("span.dl-tag.bad", {text: "Failed"}));
        else if (!d.exists) meta.push(el("span.dl-tag", {text: "Moved or deleted"}));
        if (d.resolve) meta.push(el("span.dl-tag.ok", {text: "In Resolve", title: "Sent to the Downloads bin"}));
        if (d.private) meta.push(el("span.dl-tag", {text: "Private"}));
    }
    const main = [el("span.dl-name", {text: d.name, translate: "no", title: `${d.name}\n${d.folder}`}),
                  el("span.dl-meta", {}, meta)];
    if (d.state === "active") {
        main.push(el(`span.dl-bar${d.progress === null ? ".wait" : ""}`, {}, [
            el("i", {style: d.progress === null ? "" : `width:${Math.round(d.progress * 100)}%`})]));
    }
    const actions = el("span.dl-row-actions");
    if (d.state === "active") {
        actions.append(rowButton("close", "Cancel", () => send("cancel", {ids: [d.id]})));
    } else {
        if (d.exists) actions.append(rowButton("arrow", "Send to Resolve", () => send("send", {ids: [d.id]})));
        actions.append(rowButton("folder", "Show in folder", () => send("reveal", {ids: [d.id]})));
    }
    return el("div.dl-item", {role: "option", "data-id": d.id, "data-index": String(index),
                              "data-missing": String(d.state === "done" && !d.exists),
                              "aria-selected": String(selected.has(d.id))}, [
        el("span.dl-kind", {}, [icon(KIND_ICON[d.kind] || "file")]),
        el("span.dl-main", {}, main),
        d.sizeText || d.state === "active" ? el("span.dl-size", {text: d.state === "active" ? "" : d.sizeText}) : null,
        actions,
    ]);
}

function rowButton(name, title, run) {
    const b = el("button.btn.icon.ghost", {type: "button", title, "aria-label": title});
    b.append(icon(name));
    b.onclick = e => { e.stopPropagation(); run(); };
    b.addEventListener("pointerdown", e => e.stopPropagation());
    return b;
}

function mark() {
    for (const node of $("list").querySelectorAll(".dl-item")) {
        node.setAttribute("aria-selected", String(selected.has(node.dataset.id)));
    }
    const chosen = chosenItems();
    const sendable = chosen.filter(d => d.exists);
    $("actions").hidden = !chosen.length;
    $("send").disabled = !sendable.length;
    $("open").disabled = !sendable.length;
    $("reveal").disabled = chosen.length !== 1;
    $("delete").disabled = chosen.some(d => d.state === "active");
    const total = shown.reduce((sum, d) => sum + (d.state === "done" ? d.size : 0), 0);
    const n = shown.length;
    $("count").textContent = chosen.length ? `${chosen.length} of ${n} selected`
        : n === 1 ? `1 download${total ? ` - ${bytes(total)}` : ""}` : `${n} downloads${total ? ` - ${bytes(total)}` : ""}`;
    $("clear").hidden = !all.some(d => d.state !== "active");
}

function pick(index, e) {
    const id = shown[index].id;
    if (e.shiftKey && anchor >= 0) {
        if (!e.ctrlKey) selected.clear();
        const [a, b] = [Math.min(anchor, index), Math.max(anchor, index)];
        for (let i = a; i <= b; i++) selected.add(shown[i].id);
    } else if (e.ctrlKey || e.metaKey) {
        if (selected.has(id)) selected.delete(id); else selected.add(id);
        anchor = index;
    } else {
        selected = new Set([id]);
        anchor = index;
    }
    mark();
}

const rowOf = e => e.target.closest(".dl-item");
const chosenItems = () => shown.filter(d => selected.has(d.id));
const chosenIds = () => chosenItems().map(d => d.id);
const modified = e => e.shiftKey || e.ctrlKey || e.metaKey;
const indexOf = node => +node.dataset.index;

$("list").addEventListener("pointerdown", e => {
    const node = rowOf(e);
    if (!node || e.button !== 0 || dragging) return;
    const index = indexOf(node);
    // As in Explorer: pressing on something not selected selects it, so it
    // can be dragged at once; a press on part of a selection keeps all of
    // it for a drag and narrows to that one only if it turns out a click.
    if (!selected.has(shown[index].id) || modified(e)) pick(index, e);
    press = {x: e.clientX, y: e.clientY, index, plain: !modified(e)};
});

$("list").addEventListener("pointermove", e => {
    if (!press || dragging || !(e.buttons & 1)) return;
    if (Math.hypot(e.clientX - press.x, e.clientY - press.y) < DRAG_START) return;
    const ids = chosenItems().filter(d => d.exists).map(d => d.id);
    press = null;
    if (!ids.length) return;
    dragging = true;
    send("drag", {ids});
});

$("list").addEventListener("pointerup", () => {
    if (press && !dragging && press.plain && selected.size > 1) {
        selected = new Set([shown[press.index].id]);
        anchor = press.index;
        mark();
    }
    press = null;
});

$("list").addEventListener("dblclick", e => {
    const node = rowOf(e);
    if (node) send("open", {ids: [shown[indexOf(node)].id]});
});

$("list").addEventListener("click", e => {
    if (!rowOf(e) && !modified(e)) { selected.clear(); mark(); }
});

$("folder").onclick = () => send("folder");
$("clear").onclick = () => send("clear");
$("send").onclick = () => send("send", {ids: chosenItems().filter(d => d.exists).map(d => d.id)});
$("open").onclick = () => send("open", {ids: chosenItems().filter(d => d.exists).map(d => d.id)});
$("reveal").onclick = () => send("reveal", {ids: chosenIds()});
$("remove").onclick = () => send("remove", {ids: chosenIds()});
$("delete").onclick = () => send("delete", {ids: chosenIds()});

document.addEventListener("keydown", e => {
    const typing = e.target === $("search");
    if (e.key === "Escape") {
        if (typing && $("search").value) { $("search").value = ""; filters.search = ""; draw(true); }
        else send("close");
    } else if (e.key === "Enter" && selected.size && !typing) {
        send("open", {ids: chosenItems().filter(d => d.exists).map(d => d.id)});
    } else if (e.key === "Delete" && selected.size && !typing) {
        send("delete", {ids: chosenIds()});
    } else if (!typing && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
        e.preventDefault();
        selected = new Set(shown.map(d => d.id));
        mark();
    } else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && shown.length && !typing && e.target.tagName !== "SELECT") {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : -1;
        const at = anchor < 0 ? (step > 0 ? 0 : shown.length - 1) : Math.min(shown.length - 1, Math.max(0, anchor + step));
        pick(at, {});
        $("list").querySelector(`[data-index="${at}"]`)?.scrollIntoView({block: "nearest"});
        $("list").focus();
    }
});

Buddy.on("downloads", state => {
    if (dragging) { pending = state; return; }       // redrawn once the drag is over
    all = state.items;
    $("folder").title = `Open ${state.folder}`;
    draw(false);
});

Buddy.on("drag_done", () => {
    dragging = false;
    press = null;
    if (pending) { all = pending.items; pending = null; draw(false); }
});
